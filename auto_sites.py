"""Automation sites (stage 1): websites the admin saves with the proxy for their country and the login to use,
and the steps recorded for each one by walking through the site from Telegram (a screenshot after every page,
the admin says what to do next). Running the steps for customers is stage 2.

Passwords are stored encrypted (Fernet). The key comes from SITE_SECRETS_KEY if set, otherwise from BOT_TOKEN,
so the database (and its nightly backup) never holds them readable. Steps keep placeholders, never secrets:
{username} / {password} are the site login, any other {name} is asked from the customer at run time.
"""

import asyncio
import base64
import hashlib
import json
import os
import re
import sqlite3
from datetime import datetime

from cryptography.fernet import Fernet, InvalidToken

FIELDS = ("label", "url", "proxy_server", "proxy_username", "proxy_password", "login_username", "login_password")
SECRET_FIELDS = ("proxy_password", "login_password")
IP_CHECK_URL = "https://ipinfo.io/json"
VIEWPORT = {"width": 1280, "height": 900}
# The numbered grid drawn on recording screenshots, for buttons with no text: `tap 17` clicks what is in square 17.
# "fine" has four times as many smaller squares, for crowded pages.
GRID_SIZES = {"normal": (16, 12), "fine": (32, 24)}
GRID_COLS, GRID_ROWS = GRID_SIZES["normal"]
CLICKABLES_MAX = 40  # how many the 🔘 Buttons list offers (the visible part of the page)
SCROLL_PX = 600
STEP_WAIT_SECONDS_MAX = 30
ACTION_TIMEOUT_MS = 15_000


def init(db_path: str):
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS auto_sites (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            label TEXT NOT NULL,
            url TEXT NOT NULL,
            proxy_server TEXT,
            proxy_username TEXT,
            proxy_password TEXT,
            login_username TEXT,
            login_password TEXT,
            steps_json TEXT,
            success_text TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.commit()
    conn.close()


# ── secrets ──────────────────────────────────────────────────────────

def _fernet() -> Fernet:
    seed = os.environ.get("SITE_SECRETS_KEY") or os.environ.get("BOT_TOKEN") or ""
    if not seed:
        raise RuntimeError("no SITE_SECRETS_KEY or BOT_TOKEN to encrypt saved passwords with")
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(("auto-sites:" + seed).encode()).digest()))


def encrypt(value: str) -> str:
    return _fernet().encrypt(value.encode()).decode() if value else ""


def decrypt(value: str):
    """The plain value, "" for none, or None when it can't be read (the key changed: enter it again)."""
    if not value:
        return ""
    try:
        return _fernet().decrypt(value.encode()).decode()
    except (InvalidToken, ValueError):
        return None


def mask(value) -> str:
    if value is None:
        return "⚠️ unreadable, enter it again"
    return "•••••• (saved)" if value else "—"


# ── storage ──────────────────────────────────────────────────────────

def _row_to_site(names, row) -> dict:
    site = dict(zip(names, row))
    for f in SECRET_FIELDS:
        site[f] = decrypt(site.get(f) or "")
    try:
        site["steps"] = json.loads(site.get("steps_json") or "[]")
    except ValueError:
        site["steps"] = []
    return site


def list_sites(db_path: str) -> list:
    conn = sqlite3.connect(db_path)
    cur = conn.execute("SELECT * FROM auto_sites ORDER BY label COLLATE NOCASE, id")
    names = [c[0] for c in cur.description]
    sites = [_row_to_site(names, r) for r in cur.fetchall()]
    conn.close()
    return sites


def get_site(db_path: str, site_id: int):
    conn = sqlite3.connect(db_path)
    cur = conn.execute("SELECT * FROM auto_sites WHERE id = ?", (site_id,))
    names = [c[0] for c in cur.description]
    row = cur.fetchone()
    conn.close()
    return _row_to_site(names, row) if row else None


def add_site(db_path: str, data: dict) -> int:
    now = datetime.utcnow().isoformat(timespec="seconds")
    values = {f: (encrypt(data.get(f) or "") if f in SECRET_FIELDS else (data.get(f) or "")) for f in FIELDS}
    conn = sqlite3.connect(db_path)
    cur = conn.execute(
        f"INSERT INTO auto_sites ({', '.join(FIELDS)}, steps_json, created_at, updated_at) "
        f"VALUES ({', '.join('?' for _ in FIELDS)}, '[]', ?, ?)",
        [values[f] for f in FIELDS] + [now, now],
    )
    conn.commit()
    site_id = cur.lastrowid
    conn.close()
    return site_id


def update_field(db_path: str, site_id: int, field: str, value: str):
    if field not in FIELDS:
        raise ValueError(f"unknown field {field}")
    stored = encrypt(value) if field in SECRET_FIELDS else value
    conn = sqlite3.connect(db_path)
    conn.execute(
        f"UPDATE auto_sites SET {field} = ?, updated_at = ? WHERE id = ?",
        (stored, datetime.utcnow().isoformat(timespec="seconds"), site_id),
    )
    conn.commit()
    conn.close()


def save_steps(db_path: str, site_id: int, steps: list, success_text: str = ""):
    conn = sqlite3.connect(db_path)
    conn.execute(
        "UPDATE auto_sites SET steps_json = ?, success_text = ?, updated_at = ? WHERE id = ?",
        (json.dumps(steps), success_text or "", datetime.utcnow().isoformat(timespec="seconds"), site_id),
    )
    conn.commit()
    conn.close()


def set_success_text(db_path: str, site_id: int, text: str):
    """Changes what counts as success: the first `expect` step (the renewal's confirmation; later checks, e.g.
    "logged out", stay as recorded) and the site's success text."""
    site = get_site(db_path, site_id)
    if not site:
        return
    texts = split_texts(text)
    steps, done = [], False
    for step in site["steps"]:
        if step.get("action") == "expect" and not done:
            step, done = dict(step, texts=texts), True
        steps.append(step)
    save_steps(db_path, site_id, steps, " | ".join(texts))


def main_success_text(steps: list, fallback: str = "") -> str:
    """The success text shown for a site: its first check's texts."""
    first = next((s for s in steps if s.get("action") == "expect"), None)
    return " | ".join(first["texts"]) if first else fallback


def delete_site(db_path: str, site_id: int) -> bool:
    conn = sqlite3.connect(db_path)
    cur = conn.execute("DELETE FROM auto_sites WHERE id = ?", (site_id,))
    conn.commit()
    conn.close()
    return cur.rowcount > 0


def validate_field(field: str, value: str):
    """Returns an error message, or None. "" (sent as "-") means none for the optional fields."""
    if field == "label" and not value:
        return "The name can't be empty."
    if field == "url" and not re.match(r"^https?://\S+\.\S+", value):
        return "Send the full link, starting with https://"
    if field == "proxy_server" and value:
        if not re.match(r"^((https?|socks5)://)?[\w.\-]+:\d{2,5}$", value):
            return "Send it as host:port, e.g. 1.2.3.4:8080 (or http://host:port). Send - for no proxy."
    return None


def proxy_settings(site: dict):
    """Playwright proxy settings for a site, or None for a direct connection."""
    server = (site.get("proxy_server") or "").strip()
    if not server:
        return None
    if "://" not in server:
        server = "http://" + server
    proxy = {"server": server}
    if site.get("proxy_username"):
        proxy["username"] = site["proxy_username"]
        proxy["password"] = site.get("proxy_password") or ""
    return proxy


# ── steps ────────────────────────────────────────────────────────────

INSTRUCTION_HELP = (
    "Reply with what to do on this page, one action per message:\n"
    "• click Sign in   (the text on a button or link)\n"
    "• fill Email = {username}   (the field's label or placeholder)\n"
    "• fill Password = {password}\n"
    "• fill Username = {customer_username:test@x.com}   (the customer's login, taken from their order;\n"
    "  test@x.com is used only now) · fill Password = {customer_password:Test123}\n"
    "• fill Code = {code:ABC123}   (any other {name} is asked when it runs)\n"
    "• select Country = Turkey\n"
    "• check I agree\n"
    "• press Enter\n"
    "• wait 5   (seconds)\n"
    "• goto https://…\n"
    "• tap 17   (what's in grid square 17, for a button with no text)\n"
    "• buttons   (list what can be clicked here, then tap one)\n"
    "• fields   (number the typing fields, then: fill #2 = {customer_username:test})\n"
    "• scroll down · scroll up\n"
    "• expect Access extended | Erişim uzatıldı   (check the page shows one of these; a run stops if not)\n"
    "• done Access extended | Erişim uzatıldı   (finish; that text shows success, any of the | options)\n"
    "• undo (forget the last step) · cancel"
)

_PLACEHOLDER_RE = re.compile(r"\{(\w+)(?::([^}]*))?\}")


def split_texts(text: str) -> list:
    """'A | B' → ['A', 'B']: success texts with alternatives (e.g. the Turkish and English page)."""
    return [t.strip() for t in (text or "").split("|") if t.strip()]


def parse_instruction(text: str, grid: tuple = None) -> dict:
    """One admin message → a step. Raises ValueError with what's wrong."""
    text = " ".join((text or "").split())
    verb, _, rest = text.partition(" ")
    verb = verb.lower()
    if verb in ("click", "check"):
        if not rest:
            raise ValueError(f"Say what to {verb}, e.g. {verb} Sign in")
        return {"action": verb, "target": rest.strip("\"'")}
    if verb in ("fill", "type", "select"):
        target, eq, value = rest.partition("=")
        if not eq or not target.strip():
            raise ValueError(f"Use {verb} <field> = <value>, e.g. fill Email = {{username}}")
        return {"action": "select" if verb == "select" else "fill", "target": target.strip().strip("\"'"), "value": value.strip()}
    if verb == "expect":
        texts = split_texts(rest)
        if not texts:
            raise ValueError("Say what the page should show, e.g. expect Access extended | Erişim uzatıldı")
        return {"action": "expect", "texts": texts}
    if verb == "press":
        if not rest:
            raise ValueError("Say which key, e.g. press Enter")
        return {"action": "press", "key": rest.strip()}
    if verb == "wait":
        try:
            seconds = float(rest)
        except ValueError:
            raise ValueError("Use wait <seconds>, e.g. wait 5")
        return {"action": "wait", "seconds": max(0.0, min(seconds, STEP_WAIT_SECONDS_MAX))}
    if verb == "tap":
        cols, rows = grid or (GRID_COLS, GRID_ROWS)
        if not rest.isdigit() or not 1 <= int(rest) <= cols * rows:
            raise ValueError(f"Use tap <square number>, 1 to {cols * rows} (the numbers on the screenshot's grid)")
        return {"action": "tap", "cell": int(rest), "grid": [cols, rows]}
    if verb == "scroll":
        direction = rest.lower() or "down"
        if direction not in ("down", "up"):
            raise ValueError("Use scroll down or scroll up")
        return {"action": "scroll", "direction": direction}
    if verb == "goto":
        if not re.match(r"^https?://\S+", rest):
            raise ValueError("Use goto https://…")
        return {"action": "goto", "url": rest}
    raise ValueError("I didn't understand that.\n\n" + INSTRUCTION_HELP)


def stored_step(step: dict, site: dict) -> dict:
    """What gets saved: samples dropped from placeholders ({code:ABC} → {code}), and a typed-in site password
    replaced by {password} so it never sits in the steps."""
    out = dict(step)
    if "value" in out:
        value = _PLACEHOLDER_RE.sub(lambda m: "{" + m.group(1) + "}", out["value"])
        if site.get("login_password") and value == site["login_password"]:
            value = "{password}"
        if site.get("login_username") and value == site["login_username"]:
            value = "{username}"
        out["value"] = value
    return out


def resolve_value(value: str, site: dict, customer: dict = None) -> str:
    """Fills the placeholders for this run: the site login, customer values, or the sample given while
    recording."""
    customer = customer or {}

    def sub(m):
        name, sample = m.group(1), m.group(2)
        if name == "username":
            return site.get("login_username") or ""
        if name == "password":
            return site.get("login_password") or ""
        if name in customer:
            return str(customer[name])
        if sample is not None:
            return sample
        raise ValueError(f"{{{name}}} needs a value to use now: write {{{name}:SAMPLE}}")

    return _PLACEHOLDER_RE.sub(sub, value or "")


def cell_center(cell: int, grid: tuple = None) -> tuple:
    """The page position (x, y) of the middle of grid square `cell` (numbered from 1, left to right, top down)."""
    cols, rows = grid or (GRID_COLS, GRID_ROWS)
    w, h = VIEWPORT["width"] / cols, VIEWPORT["height"] / rows
    row, col = divmod(cell - 1, cols)
    return round(col * w + w / 2), round(row * h + h / 2)


# What the customer filled in for their order (fulfilment info), under the names steps use. A renewal order has
# login_username/login_password, a new-account order username/password/email.
CUSTOMER_ALIASES = {
    "customer_username": ("login_username", "username", "email"),
    "customer_password": ("login_password", "password"),
    "customer_email": ("email", "login_username"),
}
SITE_LOGIN_PLACEHOLDERS = ("username", "password")


def placeholders(steps: list) -> list:
    """The values a run needs from outside (customer details etc.), in the order the steps use them."""
    names = []
    for step in steps:
        for m in _PLACEHOLDER_RE.finditer(step.get("value") or ""):
            if m.group(1) not in SITE_LOGIN_PLACEHOLDERS and m.group(1) not in names:
                names.append(m.group(1))
    return names


def customer_values(info: dict) -> dict:
    """An order's details as placeholder values: the raw fields plus the customer_* names."""
    values = {k: str(v) for k, v in (info or {}).items() if v not in (None, "") and not isinstance(v, (dict, list))}
    for alias, keys in CUSTOMER_ALIASES.items():
        if alias not in values:
            found = next((values[k] for k in keys if values.get(k)), None)
            if found:
                values[alias] = found
    return values


def placeholder_prompt(name: str) -> str:
    return {
        "customer_username": "the customer's username or email on the website",
        "customer_password": "the customer's password on the website",
        "customer_email": "the customer's email",
    }.get(name, name.replace("_", " "))


def describe(step: dict) -> str:
    a = step.get("action")
    if a == "expect":
        return "expect " + " | ".join(step.get("texts") or [])
    if a == "tap":
        if not step.get("cell"):  # chosen from the 🔘 Buttons list
            return f"tap {step.get('what') or 'button'}"
        return f"tap {step.get('cell')}" + (f" ({step['what']})" if step.get("what") else "")
    if a == "scroll":
        return f"scroll {step.get('direction', 'down')}"
    if a in ("click", "check"):
        return f"{a} {step['target']}"
    if a in ("fill", "select"):
        return f"{a} {step['target']} = {step.get('value', '')}"
    if a == "press":
        return f"press {step['key']}"
    if a == "wait":
        return f"wait {step['seconds']:g}"
    if a == "goto":
        return f"goto {step['url']}"
    return json.dumps(step)


# ── browser ──────────────────────────────────────────────────────────

class StepFailed(Exception):
    def __init__(self, number: int, step: dict, cause: Exception):
        self.number, self.step, self.cause = number, step, cause
        super().__init__(f"step {number} ({describe(step)}): {str(cause).splitlines()[0][:300] if str(cause) else cause.__class__.__name__}")


async def run_site(site: dict, customer: dict) -> dict:
    """Runs all of a site's saved steps with this customer's values, through its proxy. Never claims success
    without seeing a success text: the `expect` steps (checked where they are in the path, e.g. right after the
    renewal and before logging out), or else the site's success text on the last page.
    Returns {ok, done, total, error, screenshot, title, confirmation, checks_passed}."""
    steps = site.get("steps") or []
    has_checks = any(s.get("action") == "expect" for s in steps)
    session = Session(site)
    result = {"ok": False, "done": 0, "total": len(steps), "error": "", "screenshot": None, "title": "",
              "confirmation": "", "checks_passed": 0}
    try:
        await session.start()
        result["done"] = await session.replay(steps, customer)
        if has_checks:
            result["ok"] = True
        elif site.get("success_text"):
            found = await session.matching_text(split_texts(site["success_text"]))
            result["ok"] = found is not None
            if found:
                session.confirmations.append(found)
            else:
                result["error"] = f"the page doesn't show “{site['success_text']}”"
        else:
            result["ok"], result["unverified"] = True, True  # no success text recorded: only the screenshot tells
    except StepFailed as exc:
        result["done"], result["error"] = exc.number - 1, str(exc)
    except Exception as exc:
        result["error"] = f"couldn't open the site: {str(exc).splitlines()[0][:300] if str(exc) else exc.__class__.__name__}"
    finally:
        result["checks_passed"] = len(session.confirmations)
        # The first check is the one that matters (the renewal's confirmation, with its date).
        result["confirmation"] = session.confirmations[0] if session.confirmations else ""
        try:
            if session.page:
                result["screenshot"] = await session.screenshot()
                result["title"] = await session.title()
        except Exception:
            pass
        await session.close()
    return result


class Session:
    """One browser on the site, through its proxy."""

    def __init__(self, site: dict):
        self.site = site
        self._pw = self._browser = self.context = self.page = None
        self.confirmations = []  # what each passed `expect` step found on the page

    async def start(self, open_url: bool = True):
        from playwright.async_api import async_playwright
        self._pw = await async_playwright().start()
        launch = {"headless": True, "args": ["--no-sandbox"]}
        if os.environ.get("CHROMIUM_PATH"):  # tests only
            launch["executable_path"] = os.environ["CHROMIUM_PATH"]
        proxy = proxy_settings(self.site)
        if proxy:
            launch["proxy"] = proxy
        self._browser = await self._pw.chromium.launch(**launch)
        self.context = await self._browser.new_context(viewport=VIEWPORT)
        self.page = await self.context.new_page()
        if open_url:
            await self.page.goto(self.site["url"], timeout=60_000)
            await self.settle()
        return self

    async def settle(self):
        """Waits for what a step started (e.g. a form being sent) to finish loading, so the next step doesn't cut
        it off."""
        await asyncio.sleep(0.3)  # a click's navigation may not have started yet
        for state, timeout in (("load", 20_000), ("networkidle", 10_000)):
            try:
                await self.page.wait_for_load_state(state, timeout=timeout)
            except Exception:
                pass

    async def exit_ip(self) -> dict:
        """Where this browser's traffic comes out: {ip, country, city, org}."""
        page = await self.context.new_page()
        try:
            await page.goto(IP_CHECK_URL, timeout=30_000)
            data = json.loads(await page.inner_text("body"))
            return {k: data.get(k, "") for k in ("ip", "country", "city", "org")}
        finally:
            await page.close()

    async def _field(self, t: str, kind: str, selector: str = None):
        """The input/dropdown/tick box meant by `t`: its label, placeholder, name or id, or the field inside or
        right after something showing that text. Never a box or label around the field."""
        page = self.page
        if selector:
            loc = page.locator(selector)
            if await loc.count() == 1 and await loc.first.evaluate(_FIELD_OK_JS, kind):
                return loc.first
        candidates = [page.get_by_label(t), page.get_by_placeholder(t), page.locator(f'[name="{t}"]')]
        if re.match(r"^[A-Za-z][\w-]*$", t):
            candidates.append(page.locator(f"#{t}"))
        for loc in candidates:
            try:
                n = await loc.count()
            except Exception:
                continue
            for i in range(min(n, 5)):
                el = loc.nth(i)
                try:
                    if await el.evaluate(_FIELD_OK_JS, kind):
                        return el
                    inner = el.locator(_FIELD_SELECTOR)  # a wrapper around the field: use the field in it
                    for j in range(min(await inner.count(), 3)):
                        if await inner.nth(j).evaluate(_FIELD_OK_JS, kind):
                            return inner.nth(j)
                except Exception:
                    continue
        try:
            if await page.evaluate(_FIND_FIELD_JS, [t, kind]):
                return page.locator('[data-medic-field="1"]').first
        except Exception:
            pass
        return None

    async def fields(self) -> list:
        """The typing fields, dropdowns and tick boxes on the visible part of the page, top to bottom:
        [{n, label, kind, left, top, selector}]."""
        try:
            found = await self.page.evaluate(_FIELDS_JS, CLICKABLES_MAX)
        except Exception:
            return []
        for i, f in enumerate(found, 1):
            f["n"] = i
        return found

    async def _first(self, candidates):
        for loc in candidates:
            try:
                if await loc.count() > 0:
                    return loc.first
            except Exception:
                continue
        return None

    async def perform(self, step: dict, customer: dict = None):
        """Does one step on the current page. Raises with a readable reason when it can't."""
        page, a = self.page, step["action"]
        if a == "click":
            t = step["target"]
            el = await self._first([
                page.get_by_role("button", name=t), page.get_by_role("link", name=t),
                page.get_by_text(t, exact=True), page.get_by_text(t), page.get_by_label(t),
            ])
            if not el:
                raise ValueError(f'nothing on the page says "{t}"')
            await el.click(timeout=ACTION_TIMEOUT_MS)
        elif a in ("fill", "select", "check"):
            t = step["target"]
            el = await self._field(t, a, step.get("selector"))
            if not el:
                raise ValueError(f'no {"dropdown" if a == "select" else "tick box" if a == "check" else "typing field"} '
                                 f'called "{t}" on the page (type fields to list them)')
            if a == "fill":
                await el.fill(resolve_value(step.get("value", ""), self.site, customer), timeout=ACTION_TIMEOUT_MS)
            elif a == "select":
                await el.select_option(label=resolve_value(step.get("value", ""), self.site, customer), timeout=ACTION_TIMEOUT_MS)
            else:
                await el.check(timeout=ACTION_TIMEOUT_MS)
        elif a == "tap":
            el = None
            if step.get("selector"):
                loc = page.locator(step["selector"])
                if await loc.count() == 1 and await loc.first.is_visible():
                    el = loc.first
            if el:
                await el.click(timeout=ACTION_TIMEOUT_MS)
            else:
                x, y = (step["x"], step["y"]) if "x" in step else cell_center(step["cell"], step.get("grid"))
                await page.mouse.click(x, y)
        elif a == "scroll":
            await page.mouse.wheel(0, SCROLL_PX if step.get("direction", "down") == "down" else -SCROLL_PX)
            await asyncio.sleep(0.5)
        elif a == "expect":
            snippet = await self.matching_text(step.get("texts") or [])
            if snippet is None:
                raise ValueError("the page doesn't show " + " or ".join(f"“{t}”" for t in step.get("texts") or []))
            self.confirmations.append(snippet)
            return  # nothing changed on the page: no need to wait for it
        elif a == "press":
            await page.keyboard.press(step["key"])
        elif a == "wait":
            await asyncio.sleep(step["seconds"])
        elif a == "goto":
            try:
                await page.goto(step["url"], timeout=60_000)
            except Exception as exc:
                # Cut off by the previous step's page still loading (ERR_ABORTED / interrupted): try once more.
                if "ERR_ABORTED" not in str(exc) and "interrupted" not in str(exc):
                    raise
                await self.settle()
                await page.goto(step["url"], timeout=60_000)
        else:
            raise ValueError(f"unknown action {a}")
        await self.settle()

    async def screenshot(self, grid: tuple = None, marks: list = None) -> bytes:
        """The visible part of the page. With `grid` (cols, rows), numbered squares drawn on it for `tap N`; with
        `marks` (from clickables()), each listed element numbered for the 🔘 Buttons list."""
        if not grid and not marks:
            return await self.page.screenshot(type="jpeg", quality=70)
        try:
            if marks:
                await self.page.evaluate(_MARKS_JS, [[m["n"], m["left"], m["top"]] for m in marks])
            else:
                await self.page.evaluate(_GRID_JS, list(grid))
        except Exception:
            return await self.page.screenshot(type="jpeg", quality=70)
        try:
            return await self.page.screenshot(type="jpeg", quality=75)
        finally:
            try:
                await self.page.evaluate("() => { const g = document.getElementById('__medic_grid'); if (g) g.remove(); }")
            except Exception:
                pass

    async def clickables(self) -> list:
        """What can be clicked on the visible part of the page, top to bottom: [{n, label, x, y, left, top, selector}]
        (selector only when it finds exactly that element)."""
        try:
            found = await self.page.evaluate(_CLICKABLES_JS, CLICKABLES_MAX)
        except Exception:
            return []
        for i, c in enumerate(found, 1):
            c["n"] = i
        return found

    def step_from_clickable(self, c: dict) -> dict:
        step = {"action": "tap", "x": c["x"], "y": c["y"], "what": c["label"]}
        if c.get("selector"):
            step["selector"] = c["selector"]
        return step

    async def resolve_tap(self, step: dict) -> dict:
        """Turns `tap N` into what to click: the clickable element in that square nearest its middle (or the middle
        itself), with a unique id/label/name selector when it has one so a later run finds it even if the page
        shifts a little."""
        grid = tuple(step.get("grid") or (GRID_COLS, GRID_ROWS))
        x, y = cell_center(step["cell"], grid)
        out = {"action": "tap", "cell": step["cell"], "grid": list(grid), "x": x, "y": y}
        w, h = VIEWPORT["width"] / grid[0], VIEWPORT["height"] / grid[1]
        try:
            info = await self.page.evaluate(_ELEMENT_AT_JS, [x - w / 2, y - h / 2, x + w / 2, y + h / 2])
        except Exception:
            info = None
        if info:
            out["x"], out["y"] = info.get("x", x), info.get("y", y)
            if info.get("selector") and info.get("unique"):
                out["selector"] = info["selector"]
            out["what"] = info.get("text") or info.get("selector") or info.get("tag") or ""
        return out

    async def page_has_text(self, text: str) -> bool:
        return await self.matching_text(split_texts(text) or [text]) is not None

    async def matching_text(self, texts: list):
        """The page's line (with the next one) containing the first of `texts` it shows, e.g. the expiry date
        under "Uzaktan Erişim Uzatıldı"; None if it shows none of them."""
        try:
            body = await self.page.inner_text("body")
        except Exception:
            return None
        lines = [" ".join(l.split()) for l in body.splitlines() if l.strip()]
        for t in texts:
            want = " ".join(t.split()).casefold()
            if not want:
                continue
            for i, line in enumerate(lines):
                if want in line.casefold():
                    return " ".join(lines[i:i + 2])[:300]
            if want in " ".join(lines).casefold():  # spread over lines
                return t
        return None

    async def title(self) -> str:
        try:
            return (await self.page.title()) or self.page.url
        except Exception:
            return ""

    async def replay(self, steps: list, customer: dict = None) -> int:
        """Performs saved steps in order. Returns how many were done; raises StepFailed on the first that fails."""
        for i, step in enumerate(steps, 1):
            try:
                await self.perform(step, customer)
            except Exception as exc:
                raise StepFailed(i, step, exc) from exc
        return len(steps)

    async def close(self):
        for closer in (self._browser, self._pw):
            try:
                if closer is self._pw and closer:
                    await closer.stop()
                elif closer:
                    await closer.close()
            except Exception:
                pass


_GRID_JS = """([cols, rows]) => {
  const old = document.getElementById('__medic_grid'); if (old) old.remove();
  const g = document.createElement('div'); g.id = '__medic_grid';
  g.style.cssText = 'position:fixed;inset:0;z-index:2147483647;pointer-events:none;display:grid;' +
    `grid-template-columns:repeat(${cols},1fr);grid-template-rows:repeat(${rows},1fr);`;
  for (let i = 1; i <= cols * rows; i++) {
    const c = document.createElement('div');
    c.style.cssText = 'border:1px solid rgba(255,0,80,.35);position:relative;';
    const n = document.createElement('span'); n.textContent = i;
    n.style.cssText = 'position:absolute;left:1px;top:1px;font:bold ' + (cols > 20 ? 9 : 11) + 'px/1 Arial,sans-serif;color:#fff;' +
      'background:rgba(220,0,70,' + (cols > 20 ? '.5' : '.75') + ');padding:1px 3px;border-radius:3px;';
    c.appendChild(n); g.appendChild(c);
  }
  document.documentElement.appendChild(g);
}"""

_ELEMENT_AT_JS = r"""([x0, y0, x1, y1]) => {
  // The clickable element in this grid square nearest its middle (small icon buttons are rarely right in the
  // middle), else whatever is at the middle. Returns where to click and how to find it again.
  const cx = (x0 + x1) / 2, cy = (y0 + y1) / 2;
  const sel = 'button,a,[role=button],[role=link],[role=menuitem],[role=tab],input,select,textarea,label,summary,[onclick],[tabindex]';
  let best = null, bestD = Infinity;
  for (const el of document.querySelectorAll(sel)) {
    const r = el.getBoundingClientRect();
    if (r.width < 2 || r.height < 2 || r.right <= x0 || r.left >= x1 || r.bottom <= y0 || r.top >= y1) continue;
    const st = getComputedStyle(el);
    if (st.visibility === 'hidden' || st.display === 'none' || st.pointerEvents === 'none') continue;
    const dx = Math.max(r.left - cx, 0, cx - r.right), dy = Math.max(r.top - cy, 0, cy - r.bottom);
    const d = dx * dx + dy * dy;
    if (d < bestD) { bestD = d; best = el; }
  }
  let el = best, px = cx, py = cy;
  if (el) {
    const r = el.getBoundingClientRect();
    px = Math.round(Math.min(Math.max(r.left + r.width / 2, x0), x1 - 1));
    py = Math.round(Math.min(Math.max(r.top + r.height / 2, y0), y1 - 1));
  } else {
    el = document.elementFromPoint(cx, cy);
    if (!el) return {x: Math.round(cx), y: Math.round(cy)};
  }
  const esc = (v) => v.replace(/\\/g, '\\\\').replace(/"/g, '\\"');
  let selector = null;
  if (el.id) selector = '#' + CSS.escape(el.id);
  else for (const a of ['aria-label', 'title', 'name', 'data-testid', 'alt', 'href']) {
    const v = el.getAttribute(a);
    if (v) { selector = el.tagName.toLowerCase() + '[' + a + '="' + esc(v) + '"]'; break; }
  }
  const text = ((el.innerText || el.value || el.getAttribute('aria-label') || el.getAttribute('title') || '') + '').trim().slice(0, 40);
  return {x: px, y: py, selector, clickable: !!best,
          unique: selector ? document.querySelectorAll(selector).length === 1 : false,
          tag: el.tagName.toLowerCase(), text};
}"""

_CLICKABLES_JS = r"""(max) => {
  const sel = 'button,a[href],[role=button],[role=link],[role=menuitem],[role=tab],[role=checkbox],[role=radio],' +
    'input[type=submit],input[type=button],input[type=checkbox],input[type=radio],summary,[onclick]';
  const W = window.innerWidth, H = window.innerHeight, out = [], seen = new Set();
  const esc = (v) => v.replace(/\\/g, '\\\\').replace(/"/g, '\\"');
  for (const el of document.querySelectorAll(sel)) {
    const r = el.getBoundingClientRect();
    if (r.width < 4 || r.height < 4 || r.bottom <= 0 || r.right <= 0 || r.top >= H || r.left >= W) continue;
    const st = getComputedStyle(el);
    if (st.visibility === 'hidden' || st.display === 'none' || st.pointerEvents === 'none' || +st.opacity === 0) continue;
    if (el.closest('#__medic_grid')) continue;
    const x = Math.round(Math.min(Math.max(r.left + r.width / 2, 1), W - 1));
    const y = Math.round(Math.min(Math.max(r.top + r.height / 2, 1), H - 1));
    const hit = document.elementFromPoint(x, y);
    if (hit && hit !== el && !el.contains(hit) && !hit.contains(el)) continue;  // covered by something else
    const key = x + ',' + y;
    if (seen.has(key)) continue;
    seen.add(key);
    let label = ((el.innerText || el.value || '') + '').replace(/\s+/g, ' ').trim();
    const named = el.getAttribute('aria-label') || el.getAttribute('title') || el.getAttribute('alt') || '';
    if (!label) label = named;
    else if (named && label.length <= 3 && !label.includes(named)) label = label + ' ' + named;  // "⚙" -> "⚙ Settings"
    if (!label) { const img = el.querySelector('img[alt],[aria-label],[title]');
      if (img) label = img.getAttribute('alt') || img.getAttribute('aria-label') || img.getAttribute('title') || ''; }
    if (!label) label = '(icon, no text)';
    let selector = null;
    if (el.id) selector = '#' + CSS.escape(el.id);
    else for (const a of ['aria-label', 'title', 'name', 'data-testid', 'alt', 'href']) {
      const v = el.getAttribute(a);
      if (v) { selector = el.tagName.toLowerCase() + '[' + a + '="' + esc(v) + '"]'; break; }
    }
    if (selector && document.querySelectorAll(selector).length !== 1) selector = null;
    out.push({label: label.slice(0, 40), x, y, left: Math.max(Math.round(r.left), 0), top: Math.max(Math.round(r.top), 0), selector});
  }
  out.sort((a, b) => (a.top - b.top) || (a.left - b.left));
  return out.slice(0, max);
}"""

_MARKS_JS = r"""(marks) => {
  const old = document.getElementById('__medic_grid'); if (old) old.remove();
  const g = document.createElement('div'); g.id = '__medic_grid';
  g.style.cssText = 'position:fixed;inset:0;z-index:2147483647;pointer-events:none;';
  for (const [n, left, top] of marks) {
    const m = document.createElement('span'); m.textContent = n;
    m.style.cssText = `position:absolute;left:${Math.max(left - 6, 0)}px;top:${Math.max(top - 8, 0)}px;` +
      'font:bold 13px/1 Arial,sans-serif;color:#fff;background:#0a7cff;padding:2px 5px;border-radius:9px;' +
      'box-shadow:0 0 0 2px #fff;';
    g.appendChild(m);
  }
  document.documentElement.appendChild(g);
}"""

_FIELD_SELECTOR = 'input,textarea,select,[contenteditable=""],[contenteditable="true"],[role=checkbox],[role=textbox]'

_FIELD_OK_JS = r"""(e, kind) => {
  const r = e.getBoundingClientRect(), st = getComputedStyle(e);
  if (r.width < 2 || r.height < 2 || st.visibility === 'hidden' || st.display === 'none') return false;
  if (e.disabled) return false;
  const tag = e.tagName, type = (e.getAttribute('type') || 'text').toLowerCase();
  if (kind === 'select') return tag === 'SELECT';
  if (kind === 'check') return (tag === 'INPUT' && (type === 'checkbox' || type === 'radio')) || e.getAttribute('role') === 'checkbox';
  if (tag === 'TEXTAREA' || e.isContentEditable || e.getAttribute('role') === 'textbox') return !e.readOnly;
  return tag === 'INPUT' && !e.readOnly &&
    !['button', 'submit', 'reset', 'checkbox', 'radio', 'hidden', 'image', 'file', 'range', 'color'].includes(type);
}"""

_FIND_FIELD_JS = r"""([t, kind]) => {
  const ok = """ + _FIELD_OK_JS + r""";
  const norm = (s) => (s || '').replace(/\s+/g, ' ').trim().toLowerCase();
  const want = norm(t);
  // whole words only: "username field" may mean a field named "username", but a one-letter id like "u" mustn't
  // match whatever text happens to contain that letter
  const wantWords = want.split(/[^\p{L}\p{N}_]+/u).filter((w) => w.length > 2);
  const fields = [...document.querySelectorAll('input,textarea,select,[contenteditable],[role=checkbox],[role=textbox]')]
    .filter((e) => ok(e, kind));
  const names = (e) => [e.getAttribute('aria-label'), e.getAttribute('placeholder'), e.getAttribute('name'), e.id,
    e.getAttribute('title'), e.getAttribute('autocomplete'), e.getAttribute('data-testid'),
    e.labels ? [...e.labels].map((l) => l.innerText).join(' ') : ''].map(norm);
  let pick = fields.find((e) => names(e).includes(want)) ||
             fields.find((e) => names(e).some((n) => n && (n.includes(want) || wantWords.includes(n))));
  if (!pick) {
    // Text on the page (a label-like element), then the first matching field after it.
    const textEls = [...document.body.querySelectorAll('label,span,div,p,td,th,dt,strong,b,h1,h2,h3,h4,h5,h6')]
      .filter((e) => [...e.childNodes].some((c) => c.nodeType === 3 && norm(c.textContent)))
      .filter((e) => norm(e.innerText) === want || norm(e.innerText).startsWith(want));
    for (const le of textEls) {
      pick = fields.find((f) => le.compareDocumentPosition(f) & Node.DOCUMENT_POSITION_FOLLOWING);
      if (pick) break;
    }
  }
  document.querySelectorAll('[data-medic-field]').forEach((e) => e.removeAttribute('data-medic-field'));
  if (!pick) return false;
  pick.setAttribute('data-medic-field', '1');
  return true;
}"""

_FIELDS_JS = r"""(max) => {
  const W = window.innerWidth, H = window.innerHeight, out = [];
  const norm = (s) => (s || '').replace(/\s+/g, ' ').trim();
  const esc = (v) => v.replace(/\\/g, '\\\\').replace(/"/g, '\\"');
  for (const e of document.querySelectorAll('input,textarea,select,[contenteditable],[role=checkbox],[role=textbox]')) {
    const tag = e.tagName, type = (e.getAttribute('type') || 'text').toLowerCase();
    if (tag === 'INPUT' && ['button', 'submit', 'reset', 'hidden', 'image', 'file', 'range', 'color'].includes(type)) continue;
    const r = e.getBoundingClientRect(), st = getComputedStyle(e);
    if (r.width < 2 || r.height < 2 || st.visibility === 'hidden' || st.display === 'none' || e.disabled) continue;
    if (r.bottom <= 0 || r.right <= 0 || r.top >= H || r.left >= W) continue;
    const kind = tag === 'SELECT' ? 'dropdown' : (type === 'checkbox' || type === 'radio' || e.getAttribute('role') === 'checkbox') ? 'tick box'
               : type === 'password' ? 'password' : 'text';
    const label = norm(e.labels && e.labels.length ? [...e.labels].map((l) => l.innerText).join(' ') : '') ||
      norm(e.getAttribute('aria-label')) || norm(e.getAttribute('placeholder')) || norm(e.getAttribute('name')) ||
      norm(e.id) || norm(e.getAttribute('title')) || '(no label)';
    let selector = null;
    if (e.id) selector = '#' + CSS.escape(e.id);
    else if (e.getAttribute('name')) selector = tag.toLowerCase() + '[name="' + esc(e.getAttribute('name')) + '"]';
    if (selector && document.querySelectorAll(selector).length !== 1) selector = null;
    out.push({label: label.slice(0, 40), kind, left: Math.max(Math.round(r.left), 0), top: Math.max(Math.round(r.top), 0), selector});
  }
  out.sort((a, b) => (a.top - b.top) || (a.left - b.left));
  return out.slice(0, max);
}"""
