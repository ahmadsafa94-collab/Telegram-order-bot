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


_DURATION_RE = re.compile(r"\s*[-–—(:,|]?\s*\b\d+\s*(?:years?|yrs?|months?|weeks?|days?)\b\s*\)?", re.I)


def title_only(name: str) -> str:
    """Column A without the duration (it's in B): 'iMD VIP - 6 Months' → 'iMD VIP', 'France-Clinicalkey Student
    (EMC Consult) webproxy - 1 Year' → 'France-Clinicalkey Student (EMC Consult) webproxy' (owner, 16 Nov 2026)."""
    t = _DURATION_RE.sub(" ", name or "")
    t = re.sub(r"\s{2,}", " ", t).strip()
    t = re.sub(r"\s*([-–—+])\s*([-–—])\s*", r" \1 ", t)          # 'A - - B' left behind → 'A - B'
    return re.sub(r"^[\s\-–—+:,|]+|[\s\-–—+:,|(]+$", "", t).strip() or (name or "")


def build_row(name: str, item_id: str, delivered: datetime, message: str, access: str, coworker: str,
              order_id: int, user_id: int, username: str) -> list:
    d = delivered.date()
    dur = duration_code(name)
    end = expiry_date(d, dur)
    user, password = credentials(message)
    renew = (d + timedelta(days=UPTODATE_RENEW_DAYS)).strftime("%d%m%y") if is_uptodate(item_id, name) else "Auto"
    return [title_only(name), dur, renew, d.strftime("%d/%m/%y"), end.strftime("%d/%m/%y") if end else "", user, password,
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


def enqueue_row_below(db_path: str, tab: str, row: list, username: str, number: str = ""):
    """A row that goes right under `username`'s UpToDate row (the one a renewal changes), e.g. the new year of a
    renewed subscription (owner, 20 Nov 2026); at the bottom when the username isn't there."""
    conn = sqlite3.connect(db_path)
    conn.execute("INSERT INTO sheet_outbox (kind, tab, payload, created_at) VALUES ('row_below', ?, ?, ?)",
                 (tab, json.dumps({"row": row, "username": username, "number": number}), datetime.utcnow().isoformat(timespec="seconds")))
    conn.commit()
    conn.close()


def _payload_rows(kind: str, payload: str) -> list:
    data = json.loads(payload)
    return data if kind == "rows" else [data["row"]] if kind == "row_below" else [data]


def queued_rows(db_path: str, tab: str) -> list:
    """Every row queued for a tab so far (sent or not), from single, batched and placed entries."""
    conn = sqlite3.connect(db_path)
    out = []
    for kind, payload in conn.execute("SELECT kind, payload FROM sheet_outbox WHERE tab = ? AND kind IN ('row', 'rows', 'row_below')", (tab,)):
        out.extend(_payload_rows(kind, payload))
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
    row with the old username, only if just one row has it (18 Nov 2026: with the owner's own rows a username can be on
    several products; guessing could change the wrong one). F and G get the new username and password (an empty one is
    left as it is)."""
    rows = await read_tab(tab)
    order, old = str(payload.get("order_id") or ""), (payload.get("old_user") or "").strip().lower()
    cell = lambda r, i: (r[i] if len(r) > i else "").strip()
    same_order = [i for i, r in enumerate(rows) if i > 0 and cell(r, 9) == order]
    hit = next((i for i in reversed(same_order) if cell(rows[i], 5).lower() == old), None)
    if hit is None and len(same_order) == 1:
        hit = same_order[0]
    if hit is None and old:
        same_user = [i for i, r in enumerate(rows) if i > 0 and cell(r, 5).lower() == old]
        if len(same_user) > 1:
            return (f"order #{order} isn't in the sheet (rows {', '.join(str(i + 1) for i in same_user[::-1][:6])} have the username "
                    f"{payload.get('old_user')}, {len(same_user)} in all; change the right one by hand)")
        hit = same_user[0] if same_user else None
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


# ── the owner's own rows from the Mini App (18 Nov 2026) ─────────────

class RowChanged(Exception):
    """The row isn't what the Mini App showed any more (edited or moved in Google Sheets meanwhile)."""


def clean_cells(cells) -> list:
    """13 cells as text (A–M), each trimmed and at most 500 characters; missing ones are empty."""
    vals = [("" if v is None else str(v)).strip()[:500] for v in list(cells or [])[:len(HEADER)]]
    return vals + [""] * (len(HEADER) - len(vals))


def fill_expiry(cells: list) -> list:
    """E left empty: worked out from B (1y, 6m, '1 Year') and D (dd/mm/yy), like a delivered row."""
    if not cells[4] and cells[1] and cells[3]:
        d = parse_sheet_date(cells[3])
        dur = cells[1] if re.fullmatch(r"\d+[ymwd]", cells[1]) else duration_code(cells[1])
        end = expiry_date(d, dur) if d else None
        if end:
            cells = cells[:4] + [end.strftime("%d/%m/%y")] + cells[5:]
    return cells


async def add_row(tab: str, cells: list) -> int:
    """A row the owner typed in the Mini App, at the bottom of the tab; not pink (pink is for rows the bot added).
    Returns its row number."""
    async with _flush_lock:   # not in the middle of the outbox's own writes
        return await _add_row(tab, cells)


async def _add_row(tab: str, cells: list) -> int:
    gid = await _ensure_tab(tab)
    resp = await _call("POST", f"{SHEETS_API}/{sheet_id()}/values/{_a1(tab, 'A:M')}:append",
                       params={"valueInputOption": "RAW", "insertDataOption": "INSERT_ROWS"}, json={"values": [cells]})
    m = re.search(r"!A(\d+)", resp.json().get("updates", {}).get("updatedRange", ""))
    n = int(m.group(1)) if m else None
    if n:   # a new row can take the colour of the one above it (often a pink bot row)
        white = {"red": 1.0, "green": 1.0, "blue": 1.0}
        try:
            await _call("POST", f"{SHEETS_API}/{sheet_id()}:batchUpdate", json={"requests": [{"repeatCell": {
                "range": {"sheetId": gid, "startRowIndex": n - 1, "endRowIndex": n, "startColumnIndex": 0, "endColumnIndex": len(HEADER)},
                "cell": {"userEnteredFormat": {"backgroundColor": white}}, "fields": "userEnteredFormat.backgroundColor"}}]})
        except Exception as exc:   # the row is in: don't make the owner add it twice over a colour
            log.warning("Delivered sheet: row %s added, its colour not reset: %s", n, exc)
    return n


async def edit_row(tab: str, n: int, cells: list, expect: list = None) -> list:
    """Row n gets the cells the owner changed in the Mini App; the others are left as they are (a date the owner
    typed in Google Sheets stays a date). `expect` is the row as the Mini App showed it: if the sheet has something
    else there now, nothing is written (RowChanged). Returns the changed column letters."""
    async with _flush_lock:   # the outbox can't change the row between the check and the write
        return await _edit_row(tab, n, cells, expect)


async def _edit_row(tab: str, n: int, cells: list, expect) -> list:
    rows = await read_tab(tab)
    if n < 2 or n > len(rows):
        raise LookupError(f"row {n} isn't in the sheet")
    now = clean_cells(rows[n - 1])
    if expect is not None and clean_cells(expect) != now:
        raise RowChanged(f"row {n} was changed in the sheet since you opened it")
    data, letters = [], []
    for i, (old, new) in enumerate(zip(now, cells)):
        if old != new:
            letters.append(chr(65 + i))
            data.append({"range": f"'{tab}'!{chr(65 + i)}{n}", "values": [[new]]})
    if data:
        await _call("POST", f"{SHEETS_API}/{sheet_id()}/values:batchUpdate", json={"valueInputOption": "RAW", "data": data})
    return letters


_UTD_ROW_RE = re.compile(r"(?:\butd|\bu2d\b|\bup\s*-?\s*(?:to|2)\s*-?\s*dat|آپتودیت|ابتوديت)", re.I)
# UpToDate AI codes on their own ("Uptodate AI (Official)", "UpToDate Advanced AI"): AI right after the name. An account
# with the AI add-on ("UTD VIP + AI") is still an UpToDate account.
_UTD_AI_ONLY_RE = re.compile(r"(?:\butd|\bup\s*-?\s*(?:to|2)\s*-?\s*date?)[\s_\-]*(?:(?:advanced|expert)[\s_\-]*)?a\.?\s?i\b", re.I)
# Other products the shop sells (and a few more): a row named after one of these is never an UpToDate account.
_OTHER_PRODUCT_RE = re.compile(
    r"\b(?:amboss|mksap|acp|access\s*medicine|bmj|board\s*vitals|clinical\s*key|dyna\s*med|embase|jama|lexi\s*comp|"
    r"toolkit|micromedex|nejm|sanford|science\s*direct|scopus|springer|stat\s*dx|visual\s*dx|imd|medscape|osmosis|"
    r"uworld|pathoma|kaplan|epocrates|elsevier|ovid|wiley|cochrane|aafp|nbme|lecturio|sketchy|anki|chatgpt|gpt|"
    r"web\s*of\s*science|pepid|grammarly|turnitin|complete\s*anatomy)\b", re.I)


def is_uptodate_row(account: str) -> bool:
    """Column A names an UpToDate account (UTD VIP, Uptodate Online, UTD VIP + AI …), not UpToDate AI codes alone nor
    another product (18 Nov 2026)."""
    a = account or ""
    return bool(_UTD_ROW_RE.search(a)) and not _UTD_AI_ONLY_RE.search(a)


def _norm(text: str) -> str:
    """Letters and digits only, lower case: 'IMD_VIP', 'imd vip' and 'iMD-VIP' are the same."""
    return re.sub(r"[^0-9a-z]", "", (text or "").lower())


_catalog_keys = None


def _other_product_keys() -> list:
    """Every non-UpToDate product the shop sells (panel_catalog.json, names without the duration), normalised."""
    global _catalog_keys
    if _catalog_keys is None:
        keys = set()
        try:
            path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "panel_catalog.json")
            with open(path, encoding="utf-8") as fh:
                for prod in json.load(fh).get("products", []):
                    name = title_only(prod.get("name") or "")
                    if name and not _UTD_ROW_RE.search(name):
                        keys.add(_norm(name))
        except Exception as exc:
            log.warning("Delivered sheet: couldn't read the product list: %s", exc)
        _catalog_keys = sorted(k for k in keys if len(k) >= 4)
    return _catalog_keys


def row_kind(account: str) -> str:
    """'utd' (named as an UpToDate account), 'other' (another product, or UpToDate AI codes alone: never renewed
    here) or 'unknown' (any other name, e.g. just the university: may be the UpToDate account). Another product is
    one of the known names (`_OTHER_PRODUCT_RE`) or one the shop sells: its name inside A, or A (5+ letters) inside
    its name, ignoring spaces, dashes and underscores ('Complete Anatomy', 'IMD_VIP', 'Dynamedex')."""
    a = account or ""
    if _UTD_AI_ONLY_RE.search(a):
        return "other"
    if _UTD_ROW_RE.search(a):
        return "utd"
    if _OTHER_PRODUCT_RE.search(a) or _OTHER_PRODUCT_RE.search(re.sub(r"[_\-]", " ", a)):
        return "other"
    n = _norm(a)
    if n and any(k in n or (len(n) >= 5 and n in k) for k in _other_product_keys()):
        return "other"
    if any(w in n for w in ("imdvip", "amboss", "lexicomp", "lexidrug", "clinicalkey", "statdx", "uworld", "visualdx")):
        return "other"
    return "unknown"


def _renewal_rank(row) -> int:
    """Which of a username's UpToDate rows a renewal goes to (higher first, then the newest): a row we renew (C a
    date or empty) before one that ended ("End") or renews by itself ("Auto"); among those, just the newest."""
    c = (row[2] if len(row) > 2 else "").strip().lower()
    return 0 if c in ("auto", "end") else 1


def _renewal_order(account: str, renewal: str, row_no: int) -> tuple:
    """Sort key (highest first) for the row a renewal changes: a row we renew (C a date or empty) before End / Auto,
    then a row named UpToDate before one with another name, then the newest. Other products' rows aren't candidates."""
    return (_renewal_rank(["", "", renewal]), row_kind(account) == "utd", row_no)


async def find_username(username: str) -> list:
    """Rows of the main tab whose username (F) is this one, ignoring case and spaces: [{row, account, uni, ...}],
    UpToDate rows first, each group newest (lowest in the sheet) first. For @Utdreg_bot's ➕ New renewal (16 Nov 2026)."""
    rows = await read_tab(MAIN_TAB)
    want = (username or "").strip().lower()
    cell = lambda r, i: (r[i] if len(r) > i else "").strip()
    hits = [{"row": i + 1, "account": cell(r, 0), "duration": cell(r, 1), "renewal": cell(r, 2), "created": cell(r, 3),
             "ends": cell(r, 4), "uni": cell(r, 7)}
            for i, r in enumerate(rows) if i > 0 and want and cell(r, 5).lower() == want]
    # the same order as a renewal picks its row: the first one is the row an approved renewal changes; other
    # products' rows come last
    rank = lambda h: (row_kind(h["account"]) != "other",) + _renewal_order(h["account"], h["renewal"], h["row"])
    return sorted(hits, key=rank, reverse=True)


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


def _utd_row(rows: list, username: str) -> tuple:
    """(index in rows of the UpToDate row a renewal of `username` goes to, None) or (None, why not)."""
    want = (username or "").strip().lower()
    cell = lambda r, i: (r[i] if len(r) > i else "").strip()
    mine = [i for i, r in enumerate(rows) if i > 0 and want and cell(r, 5).lower() == want]
    utd = [i for i in mine if row_kind(cell(rows[i], 0)) != "other"]
    if not utd:
        if mine:
            others = ", ".join(f"row {i + 1}: {cell(rows[i], 0) or '(no name)'}" for i in mine[::-1][:3])
            return None, f"{username} isn't in column F on an UpToDate row (only {others})"
        return None, f"{username} isn't in column F"
    return max(utd, key=lambda i: _renewal_order(cell(rows[i], 0), cell(rows[i], 2), i)), ""


async def _insert_below(db_path: str, oid: int, tab: str, payload: dict, retry: bool) -> str:
    """The row goes in a new (pink) row right under the old subscription's row: the one whose M has this renewal's R
    number (set just before by _apply_renewal), else the username's UpToDate row; at the bottom when it isn't there.
    Inserted and filled in one request, so a failure never leaves an empty row behind."""
    row = payload["row"]
    if retry and not await _rows_not_in_sheet(db_path, tab, oid, [row]):
        return "already in the sheet, not added again"
    rows = await read_tab(tab)
    want, number = (payload.get("username") or "").strip().lower(), (payload.get("number") or "").strip()
    cell = lambda r, i: (r[i] if len(r) > i else "").strip()
    marked = [i for i, r in enumerate(rows) if i > 0 and number and cell(r, 5).lower() == want
              and number in [x.strip() for x in cell(r, 12).split(",")]]
    hit = marked[-1] if marked else None
    if hit is None:
        hit, why = _utd_row(rows, payload.get("username"))
        if hit is None:
            return f"{await _send_rows(db_path, oid, tab, [row], retry=False)} (at the bottom: {why})"
    gid = await _ensure_tab(tab)
    n = hit + 2                                    # the sheet row number of the new row
    cells = [{"userEnteredValue": {"stringValue": "" if v is None else str(v)},
              "userEnteredFormat": {"backgroundColor": PINK}} for v in row]
    await _call("POST", f"{SHEETS_API}/{sheet_id()}:batchUpdate", json={"requests": [
        {"insertDimension": {"range": {"sheetId": gid, "dimension": "ROWS", "startIndex": n - 1, "endIndex": n},
                             "inheritFromBefore": False}},
        {"updateCells": {"start": {"sheetId": gid, "rowIndex": n - 1, "columnIndex": 0}, "rows": [{"values": cells}],
                         "fields": "userEnteredValue,userEnteredFormat"}}]})
    return f"row {n} (below row {n - 1}, {payload.get('username')})"


async def _apply_renewal(tab: str, payload: dict) -> str:
    """The row of that UpToDate account gets the next renewal date (owner, 18 Nov 2026). The same username can also be
    on another product's row (Amboss, iMD, UpToDate AI codes …), which is never changed. A row named UTD / UpToDate
    comes first, then a row with any other name (e.g. only the university); among them the newest one we renew
    (C a date or empty), else the newest (`_renewal_order`)."""
    rows = await read_tab(tab)
    hit, why = _utd_row(rows, payload.get("username"))
    if hit is None:
        return why
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


def _row_key(row) -> tuple:
    """What identifies a delivered row in the sheet: product, delivery date, username, order number."""
    cell = lambda i: str(row[i]).strip().lower() if len(row) > i and row[i] is not None else ""
    return cell(0), cell(3), cell(5), cell(9)


async def _rows_not_in_sheet(db_path: str, tab: str, oid: int, rows: list) -> list:
    """The rows that aren't in the tab yet. Used only when retrying: Google may have saved a row and still
    answered with an error (or too late), and appending it again would add it twice. Counted, not just looked up:
    a row only counts as already there if the sheet has more rows like it than the queue has sent (two units of
    the same product on one order, or a second delivery the same day, are separate rows)."""
    from collections import Counter
    in_sheet = Counter(_row_key(r) for r in await read_tab(tab))
    conn = sqlite3.connect(db_path)
    sent = Counter()
    for kind, payload in conn.execute("SELECT kind, payload FROM sheet_outbox WHERE tab = ? AND kind IN ('row', 'rows', 'row_below') "
                                      "AND done_at IS NOT NULL AND id != ?", (tab, oid)):
        sent.update(_row_key(r) for r in _payload_rows(kind, payload))
    conn.close()
    missing = []
    for r in rows:
        k = _row_key(r)
        if in_sheet[k] > sent[k]:
            in_sheet[k] -= 1        # this one landed on the earlier try
        else:
            missing.append(r)
    return missing


async def _send_rows(db_path: str, oid: int, tab: str, rows: list, retry: bool) -> str:
    if retry:
        missing = await _rows_not_in_sheet(db_path, tab, oid, rows)
        if not missing:
            return f"already in the sheet ({len(rows)} row{'s' if len(rows) != 1 else ''}), not added again"
        rows = missing
    first = await _append_pink_rows(tab, rows)
    return f"row {first}" if len(rows) == 1 else f"{len(rows)} rows from row {first}"


async def flush(db_path: str, notify=None, quiet: set = ()) -> int:
    """Sends what's waiting, oldest first. A failed entry is kept for the next try; the ones after it are still
    sent unless the next one fails too (then Google is down: stop and try again later), so one bad entry can't
    hold up the queue. A retried row is only added if it isn't in the sheet already. Returns how many went.
    Entries in `quiet` aren't reported when not found: the caller tells the admin itself (@Utdreg_bot's reply)."""
    if not configured():
        return 0
    async with _flush_lock:
        conn = sqlite3.connect(db_path)
        pending = conn.execute("SELECT id, kind, tab, payload, tries FROM sheet_outbox WHERE done_at IS NULL ORDER BY id").fetchall()
        conn.close()
        sent = failed_in_a_row = 0
        header_ok = False
        held_back = False
        for oid, kind, tab, payload, tries in pending:
            if held_back and kind in ("edit", "renewal", "row_below"):
                break   # it may change a row that is still waiting: keep the order, try again later
            try:
                if not header_ok:
                    await _ensure_header(MAIN_TAB)
                    header_ok = True
                data = json.loads(payload)
                note = (await _apply_renewal(tab, data) if kind == "renewal" else await _apply_edit(tab, data) if kind == "edit"
                        else await _insert_below(db_path, oid, tab, data, retry=tries > 0) if kind == "row_below"
                        else await _send_rows(db_path, oid, tab, data, retry=tries > 0) if kind == "rows"
                        else await _send_rows(db_path, oid, tab, [data], retry=tries > 0))
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
                failed_in_a_row = 0
            except Exception as exc:
                conn = sqlite3.connect(db_path)
                conn.execute("UPDATE sheet_outbox SET tries = tries + 1, error = ? WHERE id = ?", (str(exc)[:300], oid))
                conn.commit()
                conn.close()
                log.warning("Delivered sheet: couldn't send %s #%s (try %s): %s", kind, oid, tries + 1, exc)
                if notify and tries + 1 == 5:
                    await notify(f"⚠️ Delivered sheet: couldn't write to the Google Sheet for a while ({str(exc)[:200]}). "
                                 "The rows are kept and sent once it works again.")
                failed_in_a_row += 1
                held_back = True
                if failed_in_a_row >= 2:
                    break   # two in a row: Google itself is failing, not this entry
        return sent


def pending_count(db_path: str) -> int:
    conn = sqlite3.connect(db_path)
    n = conn.execute("SELECT COUNT(*) FROM sheet_outbox WHERE done_at IS NULL").fetchone()[0]
    conn.close()
    return n


# ── the sheet password ───────────────────────────────────────────────

# Fingerprint (Telegram's BiometricManager, 16 Nov 2026): the phone keeps a random key in its secure storage that only
# the admin's fingerprint releases; here only its hash and the sheet password encrypted with it are kept, so a
# fingerprint unlock can still lock the .xlsx with the real password.

def bio_wrap(password: str) -> tuple:
    """(token for the phone, record to keep)."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    token = _b64(os.urandom(32))
    salt, nonce = os.urandom(16), os.urandom(12)
    key = hashlib.sha256(salt + token.encode()).digest()
    ct = AESGCM(key).encrypt(nonce, password.encode(), None)
    return token, {"id": hashlib.sha256(token.encode()).hexdigest(), "salt": _b64(salt), "ct": _b64(nonce + ct),
                   "at": datetime.utcnow().isoformat(timespec="seconds")}


def bio_unwrap(token: str, records: list):
    """The sheet password for a phone's key, or None."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    if not isinstance(token, str) or not token:
        return None
    rid = hashlib.sha256(token.encode()).hexdigest()
    rec = next((r for r in records or [] if hmac.compare_digest(r.get("id", ""), rid)), None)
    if not rec:
        return None
    unb = lambda s: base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))
    try:
        blob = unb(rec["ct"])
        key = hashlib.sha256(unb(rec["salt"]) + token.encode()).digest()
        return AESGCM(key).decrypt(blob[:12], blob[12:], None).decode()
    except Exception:
        return None


def bio_id(token: str) -> str:
    return hashlib.sha256((token or "").encode()).hexdigest()

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
