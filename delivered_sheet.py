"""The delivered-subscriptions sheet (15 Nov 2026): the owner's Google Sheet (in their Drive) holds every delivered
subscription, typed by hand for years. The bot adds a light-pink row for each subscription it delivers, updates the
renewal date when @Utdreg_bot renews an UpToDate account, shows the sheet in the admin Mini App (after the sheet
password) and sends it as a password-protected .xlsx.

The Google Sheet is the only copy: the owner's own rows and edits are read live. The bot reaches it with a Google
service account (GOOGLE_SERVICE_ACCOUNT_JSON, the key file's JSON) that the sheet (DELIVERED_SHEET_ID) is shared with.
Rows wait in the `sheet_outbox` table until Google takes them, so a delivery never waits on Google and nothing is lost
while it's unreachable or not set up yet.

Columns (main tab, and the "iMD" tab for iMD deliveries, where H is the serial used):
A subscription  B duration (1y, 6m, 3m)  C renewal (Auto, or ddmmyy 90 days after delivery for UpToDate)
D delivered (dd/mm/yy)  E expires (dd/mm/yy)  F username  G password  H access  I coworker or "Me"  J order
K Telegram id  L Telegram username  M renewals (R numbers)
"""
import asyncio
import base64
import hashlib
import hmac
import io
import json
import logging
import os
import re
import sqlite3
import time
from datetime import date, datetime, timedelta

log = logging.getLogger(__name__)

SHEETS_API = os.environ.get("GOOGLE_SHEETS_API", "https://sheets.googleapis.com/v4/spreadsheets").rstrip("/")
DRIVE_API = os.environ.get("GOOGLE_DRIVE_API", "https://www.googleapis.com/drive/v3/files").rstrip("/")
SCOPES = "https://www.googleapis.com/auth/spreadsheets https://www.googleapis.com/auth/drive.readonly"
MAIN_TAB = os.environ.get("DELIVERED_SHEET_TAB", "Sheet1")
IMD_TAB = os.environ.get("DELIVERED_SHEET_IMD_TAB", "iMD")
HEADER = ["Account", "Duration", "Renewal Date", "Date created", "Date end", "Username", "Pass", "Uni",
          "Coworker", "Order", "Telegram ID", "Telegram username", "Renewals"]
PINK = {"red": 1.0, "green": 0.87, "blue": 0.92}          # light pink: rows the bot added
UPTODATE_RENEW_DAYS = 90
END_WINDOW_DAYS = 10                                       # a next renewal this close to (or after) expiry: "End"
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


# ── setup ────────────────────────────────────────────────────────────

def sheet_id() -> str:
    return (os.environ.get("DELIVERED_SHEET_ID") or "").strip()


def _service_account():
    raw = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON") or ""
    if not raw.strip():
        return None
    try:
        sa = json.loads(raw)
    except ValueError:
        try:
            sa = json.loads(base64.b64decode(raw).decode())    # pasted as base64
        except Exception:
            return None
    return sa if isinstance(sa, dict) and sa.get("client_email") and sa.get("private_key") else None


def configured() -> bool:
    return bool(sheet_id() and _service_account())


def not_configured_reason() -> str:
    missing = [n for n, ok in (("GOOGLE_SERVICE_ACCOUNT_JSON", _service_account()), ("DELIVERED_SHEET_ID", sheet_id())) if not ok]
    return "the sheet isn't connected yet (" + ", ".join(missing) + " not set on Railway)"


def init(db_path: str):
    conn = sqlite3.connect(db_path)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS sheet_outbox (
            id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, tab TEXT, payload TEXT NOT NULL,
            created_at TEXT NOT NULL, done_at TEXT, tries INTEGER NOT NULL DEFAULT 0, error TEXT)"""
    )
    conn.commit()
    conn.close()


# ── Google ───────────────────────────────────────────────────────────

_token = {"value": None, "exp": 0.0}


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


async def _access_token(http) -> str:
    """An OAuth token for the service account (a signed JWT exchanged at its token_uri), cached for ~50 minutes."""
    if _token["value"] and time.time() < _token["exp"] - 60:
        return _token["value"]
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding
    sa = _service_account()
    token_uri = sa.get("token_uri") or "https://oauth2.googleapis.com/token"
    now = int(time.time())
    head = _b64(json.dumps({"alg": "RS256", "typ": "JWT"}).encode())
    claims = _b64(json.dumps({"iss": sa["client_email"], "scope": SCOPES, "aud": token_uri, "iat": now, "exp": now + 3600}).encode())
    key = serialization.load_pem_private_key(sa["private_key"].encode(), password=None)
    sig = _b64(key.sign(f"{head}.{claims}".encode(), padding.PKCS1v15(), hashes.SHA256()))
    resp = await http.post(token_uri, data={"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                                             "assertion": f"{head}.{claims}.{sig}"})
    if resp.status_code != 200:
        raise RuntimeError(f"Google sign-in failed ({resp.status_code}): {resp.text[:200]}")
    data = resp.json()
    _token.update(value=data["access_token"], exp=time.time() + float(data.get("expires_in", 3600)))
    return _token["value"]


async def _call(method: str, url: str, **kw):
    import httpx
    async with httpx.AsyncClient(timeout=60) as http:
        token = await _access_token(http)
        resp = await http.request(method, url, headers={"Authorization": f"Bearer {token}"}, **kw)
    if resp.status_code >= 400:
        raise RuntimeError(f"Google answered {resp.status_code}: {resp.text[:300]}")
    return resp


def _a1(tab: str, rng: str) -> str:
    from urllib.parse import quote
    return quote(f"'{tab}'!{rng}", safe="")


_tabs = {}   # title -> numeric sheetId
_cols = {}   # title -> number of columns the tab has


async def _tab_ids() -> dict:
    resp = await _call("GET", f"{SHEETS_API}/{sheet_id()}", params={"fields": "sheets.properties"})
    _tabs.clear()
    _cols.clear()
    for s in resp.json().get("sheets", []):
        p = s["properties"]
        _tabs[p["title"]] = p["sheetId"]
        _cols[p["title"]] = int((p.get("gridProperties") or {}).get("columnCount") or 26)
    return _tabs


async def _ensure_tab(tab: str) -> int:
    """The tab's numeric id; the iMD tab is created (with the header) the first time it's needed."""
    if tab not in _tabs:
        await _tab_ids()
    if tab not in _tabs:
        resp = await _call("POST", f"{SHEETS_API}/{sheet_id()}:batchUpdate",
                           json={"requests": [{"addSheet": {"properties": {"title": tab}}}]})
        _tabs[tab] = resp.json()["replies"][0]["addSheet"]["properties"]["sheetId"]
        _cols[tab] = 26
        await _call("PUT", f"{SHEETS_API}/{sheet_id()}/values/{_a1(tab, 'A1:M1')}",
                    params={"valueInputOption": "RAW"}, json={"values": [HEADER]})
    # Google refuses ranges past a tab's last column (the owner's Sheet1 came from Excel with only A–L), so the
    # tab gets the missing columns first.
    if _cols.get(tab, 26) < len(HEADER):
        await _call("POST", f"{SHEETS_API}/{sheet_id()}:batchUpdate", json={"requests": [{"appendDimension": {
            "sheetId": _tabs[tab], "dimension": "COLUMNS", "length": len(HEADER) - _cols[tab]}}]})
        _cols[tab] = len(HEADER)
    return _tabs[tab]


async def _ensure_header(tab: str):
    """Columns I–M get their titles if the header row doesn't have them yet (the owner's sheet had A–H)."""
    await _ensure_tab(tab)
    resp = await _call("GET", f"{SHEETS_API}/{sheet_id()}/values/{_a1(tab, 'A1:M1')}")
    row = (resp.json().get("values") or [[]])[0]
    if len(row) < len(HEADER) or any(not str(v).strip() for v in row[8:]):
        new = [row[i] if i < len(row) and str(row[i]).strip() else HEADER[i] for i in range(len(HEADER))]
        await _call("PUT", f"{SHEETS_API}/{sheet_id()}/values/{_a1(tab, 'A1:M1')}",
                    params={"valueInputOption": "RAW"}, json={"values": [new]})


async def _append_pink(tab: str, row: list) -> int:
    return await _append_pink_rows(tab, [row])


async def _append_pink_rows(tab: str, rows: list) -> int:
    """Appends the rows (light pink) and returns the first one's row number."""
    gid = await _ensure_tab(tab)
    resp = await _call("POST", f"{SHEETS_API}/{sheet_id()}/values/{_a1(tab, 'A:M')}:append",
                       params={"valueInputOption": "RAW", "insertDataOption": "INSERT_ROWS"}, json={"values": rows})
    rng = resp.json().get("updates", {}).get("updatedRange", "")
    m = re.search(r"!A(\d+)", rng)
    n = int(m.group(1)) if m else None
    if n:
        await _call("POST", f"{SHEETS_API}/{sheet_id()}:batchUpdate", json={"requests": [{"repeatCell": {
            "range": {"sheetId": gid, "startRowIndex": n - 1, "endRowIndex": n - 1 + len(rows), "startColumnIndex": 0, "endColumnIndex": len(HEADER)},
            "cell": {"userEnteredFormat": {"backgroundColor": PINK}}, "fields": "userEnteredFormat.backgroundColor"}}]})
    return n


async def read_tab(tab: str) -> list:
    """All rows of a tab as text, as the sheet shows them (the owner's edits included)."""
    await _ensure_tab(tab)
    resp = await _call("GET", f"{SHEETS_API}/{sheet_id()}/values/{_a1(tab, 'A1:M')}",
                       params={"valueRenderOption": "FORMATTED_VALUE"})
    return [[str(v) for v in r] for r in resp.json().get("values", [])]


async def export_xlsx() -> bytes:
    resp = await _call("GET", f"{DRIVE_API}/{sheet_id()}/export", params={"mimeType": XLSX})
    return resp.content


def encrypt_xlsx(data: bytes, password: str) -> bytes:
    """The workbook locked with `password` (Excel/Sheets ask for it to open the file)."""
    from msoffcrypto.format.ooxml import OOXMLFile
    out = io.BytesIO()
    OOXMLFile(io.BytesIO(data)).encrypt(password, out)
    return out.getvalue()


# ── rows ─────────────────────────────────────────────────────────────

def duration_code(name: str) -> str:
    """'iMD VIP - 6 Months' → 6m, 'Uptodate Online 1 Year' → 1y, '2 Years' → 2y."""
    m = re.search(r"(\d+)\s*(years?|yrs?|y\b|months?|mo\b|m\b|weeks?|days?)", name or "", re.I)
    if not m:
        return ""
    unit = m.group(2).lower()
    return f"{m.group(1)}{'y' if unit.startswith('y') else 'm' if unit.startswith('m') else 'w' if unit.startswith('w') else 'd'}"


def _add_months(d: date, months: int) -> date:
    import calendar
    y, m = divmod(d.month - 1 + months, 12)
    y, m = d.year + y, m + 1
    return d.replace(year=y, month=m, day=min(d.day, calendar.monthrange(y, m)[1]))


def expiry_date(delivered: date, duration: str):
    """1y from 16/11/2026 → 15/11/2027 (the day before the same date)."""
    m = re.fullmatch(r"(\d+)([ymwd])", duration or "")
    if not m:
        return None
    n, u = int(m.group(1)), m.group(2)
    end = (_add_months(delivered, 12 * n) if u == "y" else _add_months(delivered, n) if u == "m"
           else delivered + timedelta(weeks=n) if u == "w" else delivered + timedelta(days=n))
    return end - timedelta(days=1)


def is_uptodate(item_id: str, name: str) -> bool:
    """UpToDate accounts are renewed by us every 90 days (not UpToDate AI codes, which are delivered as codes)."""
    text = f"{item_id} {name}".lower()
    return ("uptodate" in text or item_id.startswith("up_")) and " ai" not in f" {name.lower()}" and "_ai" not in item_id


_USER_RE = re.compile(r"^[\s•\-*👤🔑📧]*?(?:user\s*name|username|user|login|e-?mail(?:\s*address)?|kullanıcı\s*adı)\s*[:：=\-–]\s*(.+?)\s*$", re.I | re.M)
_PASS_RE = re.compile(r"^[\s•\-*👤🔑🔒]*?(?:pass\s*word|password|pass|pw|şifre)\s*[:：=\-–]\s*(.+?)\s*$", re.I | re.M)
_CODE_RE = re.compile(r"/Redeem\s+(\S+)", re.I)


def credentials(message: str) -> tuple:
    """(username, password) as delivered, read from the delivery message ('Username: x' / 'Password: y' lines;
    a redeem code counts as the username)."""
    text = message or ""
    u, p = _USER_RE.search(text), _PASS_RE.search(text)
    user = u.group(1) if u else (_CODE_RE.search(text).group(1) if _CODE_RE.search(text) else "")
    return user, (p.group(1) if p else "")


def build_row(name: str, item_id: str, delivered: datetime, message: str, access: str, coworker: str,
              order_id: int, user_id: int, username: str) -> list:
    d = delivered.date()
    dur = duration_code(name)
    end = expiry_date(d, dur)
    user, password = credentials(message)
    renew = (d + timedelta(days=UPTODATE_RENEW_DAYS)).strftime("%d%m%y") if is_uptodate(item_id, name) else "Auto"
    return [name, dur, renew, d.strftime("%d/%m/%y"), end.strftime("%d/%m/%y") if end else "", user, password,
            access or "", coworker or "Me", str(order_id), str(user_id), (username or "").lstrip("@"), ""]


def enqueue_row(db_path: str, tab: str, row: list):
    conn = sqlite3.connect(db_path)
    conn.execute("INSERT INTO sheet_outbox (kind, tab, payload, created_at) VALUES ('row', ?, ?, ?)",
                 (tab, json.dumps(row), datetime.utcnow().isoformat(timespec="seconds")))
    conn.commit()
    conn.close()


def enqueue_rows(db_path: str, tab: str, rows: list):
    """Many rows sent as one append (the backfill of old deliveries), so Google's per-minute limit isn't hit."""
    conn = sqlite3.connect(db_path)
    for i in range(0, len(rows), 200):
        conn.execute("INSERT INTO sheet_outbox (kind, tab, payload, created_at) VALUES ('rows', ?, ?, ?)",
                     (tab, json.dumps(rows[i:i + 200]), datetime.utcnow().isoformat(timespec="seconds")))
    conn.commit()
    conn.close()


def queued_rows(db_path: str, tab: str) -> list:
    """Every row queued for a tab so far (sent or not), from single and batched entries."""
    conn = sqlite3.connect(db_path)
    out = []
    for kind, payload in conn.execute("SELECT kind, payload FROM sheet_outbox WHERE tab = ? AND kind IN ('row', 'rows')", (tab,)):
        data = json.loads(payload)
        out.extend(data if kind == "rows" else [data])
    conn.close()
    return out


def enqueue_renewal(db_path: str, username: str, number: str, when: str) -> int:
    conn = sqlite3.connect(db_path)
    cur = conn.execute("INSERT INTO sheet_outbox (kind, tab, payload, created_at) VALUES ('renewal', ?, ?, ?)",
                 (MAIN_TAB, json.dumps({"username": username, "number": number, "date": when}),
                  datetime.utcnow().isoformat(timespec="seconds")))
    conn.commit()
    conn.close()
    return cur.lastrowid


def outbox_result(db_path: str, oid: int):
    """(done, note) for one outbox entry."""
    conn = sqlite3.connect(db_path)
    row = conn.execute("SELECT done_at, error FROM sheet_outbox WHERE id = ?", (oid,)).fetchone()
    conn.close()
    return (bool(row and row[0]), (row[1] if row else "") or "")


def enqueue_edit(db_path: str, tab: str, order_id: int, old_user: str, user: str, password: str):
    """A delivery's username/password were edited in the bot: the sheet row follows (15 Nov 2026)."""
    conn = sqlite3.connect(db_path)
    conn.execute("INSERT INTO sheet_outbox (kind, tab, payload, created_at) VALUES ('edit', ?, ?, ?)",
                 (tab, json.dumps({"order_id": order_id, "old_user": old_user, "user": user, "password": password}),
                  datetime.utcnow().isoformat(timespec="seconds")))
    conn.commit()
    conn.close()


async def _apply_edit(tab: str, payload: dict) -> str:
    """The row of that order (column J), the one with the old username if the order has several; else the newest
    row with the old username. F and G get the new username and password (an empty one is left as it is)."""
    rows = await read_tab(tab)
    order, old = str(payload.get("order_id") or ""), (payload.get("old_user") or "").strip().lower()
    cell = lambda r, i: (r[i] if len(r) > i else "").strip()
    same_order = [i for i, r in enumerate(rows) if i > 0 and cell(r, 9) == order]
    hit = next((i for i in reversed(same_order) if cell(rows[i], 5).lower() == old), None)
    if hit is None and len(same_order) == 1:
        hit = same_order[0]
    if hit is None and old:
        hit = max((i for i, r in enumerate(rows) if i > 0 and cell(r, 5).lower() == old), default=None)
    if hit is None:
        return f"order #{order} isn't in the sheet"
    n, data = hit + 1, []
    if payload.get("user"):
        data.append({"range": f"'{tab}'!F{n}", "values": [[payload["user"]]]})
    if payload.get("password"):
        data.append({"range": f"'{tab}'!G{n}", "values": [[payload["password"]]]})
    if data:
        await _call("POST", f"{SHEETS_API}/{sheet_id()}/values:batchUpdate", json={"valueInputOption": "RAW", "data": data})
    return f"row {n}: username/password updated"


# ── renewals (@Utdreg_bot) ───────────────────────────────────────────

def parse_sheet_date(text: str):
    t = (text or "").strip()
    for fmt in ("%d/%m/%y", "%d/%m/%Y", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%d.%m.%Y", "%d.%m.%y", "%d-%m-%Y", "%d-%m-%y", "%d%m%y"):
        try:
            return datetime.strptime(t, fmt).date()
        except ValueError:
            continue
    return None


def next_renewal(renewed: date, expiry) -> str:
    """90 days after this renewal, ddmmyy; "End" when that is within 10 days of the expiry or after it."""
    nxt = renewed + timedelta(days=UPTODATE_RENEW_DAYS)
    if expiry and nxt >= expiry - timedelta(days=END_WINDOW_DAYS):
        return "End"
    return nxt.strftime("%d%m%y")


async def _apply_renewal(tab: str, payload: dict) -> str:
    rows = await read_tab(tab)
    want = (payload.get("username") or "").strip().lower()
    hit = max((i for i, r in enumerate(rows) if i > 0 and len(r) > 5 and r[5].strip().lower() == want), default=None)
    if hit is None:
        return f"{payload.get('username')} isn't in column F"
    row = rows[hit]
    renewed = datetime.fromisoformat(payload["date"]).date() if payload.get("date") else date.today()
    expiry = parse_sheet_date(row[4]) if len(row) > 4 else None
    value = next_renewal(renewed, expiry)
    rs = (row[12] if len(row) > 12 else "").strip()
    rs = ", ".join(x for x in (rs, payload.get("number") or "") if x)
    n = hit + 1
    await _call("POST", f"{SHEETS_API}/{sheet_id()}/values:batchUpdate", json={"valueInputOption": "RAW", "data": [
        {"range": f"'{tab}'!C{n}", "values": [[value]]}, {"range": f"'{tab}'!M{n}", "values": [[rs]]}]})
    return f"row {n}: renewal date {value}, {payload.get('number')}"


# ── sending ──────────────────────────────────────────────────────────

_flush_lock = asyncio.Lock()


async def flush(db_path: str, notify=None, quiet: set = ()) -> int:
    """Sends what's waiting, oldest first; stops at the first failure (kept for the next try). Returns how many went.
    Entries in `quiet` aren't reported when not found: the caller tells the admin itself (@Utdreg_bot's reply)."""
    if not configured():
        return 0
    async with _flush_lock:
        conn = sqlite3.connect(db_path)
        pending = conn.execute("SELECT id, kind, tab, payload, tries FROM sheet_outbox WHERE done_at IS NULL ORDER BY id").fetchall()
        conn.close()
        sent = 0
        for oid, kind, tab, payload, tries in pending:
            try:
                if not sent:
                    await _ensure_header(MAIN_TAB)
                data = json.loads(payload)
                note = (await _apply_renewal(tab, data) if kind == "renewal" else await _apply_edit(tab, data) if kind == "edit"
                        else f"{len(data)} rows from row {await _append_pink_rows(tab, data)}" if kind == "rows"
                        else f"row {await _append_pink(tab, data)}")
                conn = sqlite3.connect(db_path)
                conn.execute("UPDATE sheet_outbox SET done_at = ?, error = ? WHERE id = ?",
                             (datetime.utcnow().isoformat(timespec="seconds"), note, oid))
                conn.commit()
                conn.close()
                log.info("Delivered sheet: %s %s → %s", kind, tab, note)
                if kind == "renewal" and notify and "isn't in column F" in note and oid not in quiet:
                    await notify(f"⚠️ 📑 Delivered sheet: renewal {data.get('number')}: {note}, so nothing was changed "
                                 "there. Enter its renewal date in the sheet yourself.")
                if kind == "edit" and notify and "isn't in the sheet" in note:
                    await notify(f"📑 Delivered sheet: the edited details of {note}, so they weren't changed there.")
                sent += 1
            except Exception as exc:
                conn = sqlite3.connect(db_path)
                conn.execute("UPDATE sheet_outbox SET tries = tries + 1, error = ? WHERE id = ?", (str(exc)[:300], oid))
                conn.commit()
                conn.close()
                log.warning("Delivered sheet: couldn't send %s #%s (try %s): %s", kind, oid, tries + 1, exc)
                if notify and tries + 1 == 5:
                    await notify(f"⚠️ Delivered sheet: couldn't write to the Google Sheet for a while ({str(exc)[:200]}). "
                                 "The rows are kept and sent once it works again.")
                break
        return sent


def pending_count(db_path: str) -> int:
    conn = sqlite3.connect(db_path)
    n = conn.execute("SELECT COUNT(*) FROM sheet_outbox WHERE done_at IS NULL").fetchone()[0]
    conn.close()
    return n


# ── the sheet password ───────────────────────────────────────────────

def hash_password(password: str, salt: bytes = None) -> str:
    salt = salt or os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 200_000)
    return f"pbkdf2${_b64(salt)}${_b64(digest)}"


def check_password(password: str, stored: str) -> bool:
    try:
        _, salt, digest = stored.split("$")
        salt_b = base64.urlsafe_b64decode(salt + "=" * (-len(salt) % 4))
        return hmac.compare_digest(hash_password(password, salt_b), stored)
    except Exception:
        return False
