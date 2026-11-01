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
# The numbered grid drawn on recording screenshots, for buttons with no text: `tap 17` clicks square 17's middle.
GRID_COLS, GRID_ROWS = 16, 12
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
    "• fill Code = {code:ABC123}   ({code} is asked from the customer later; ABC123 is used only now)\n"
    "• select Country = Turkey\n"
    "• check I agree\n"
    "• press Enter\n"
    "• wait 5   (seconds)\n"
    "• goto https://…\n"
    "• tap 17   (the middle of grid square 17, for a button with no text)\n"
    "• scroll down · scroll up\n"
    "• done You have renewed   (finish; the text shows success)\n"
    "• undo (forget the last step) · cancel"
)

_PLACEHOLDER_RE = re.compile(r"\{(\w+)(?::([^}]*))?\}")


def parse_instruction(text: str) -> dict:
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
        if not rest.isdigit() or not 1 <= int(rest) <= GRID_COLS * GRID_ROWS:
            raise ValueError(f"Use tap <square number>, 1 to {GRID_COLS * GRID_ROWS} (the numbers on the screenshot's grid)")
        return {"action": "tap", "cell": int(rest)}
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


def cell_center(cell: int) -> tuple:
    """The page position (x, y) of the middle of grid square `cell` (numbered from 1, left to right, top down)."""
    w, h = VIEWPORT["width"] / GRID_COLS, VIEWPORT["height"] / GRID_ROWS
    row, col = divmod(cell - 1, GRID_COLS)
    return round(col * w + w / 2), round(row * h + h / 2)


def describe(step: dict) -> str:
    a = step.get("action")
    if a == "tap":
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

class Session:
    """One browser on the site, through its proxy."""

    def __init__(self, site: dict):
        self.site = site
        self._pw = self._browser = self.context = self.page = None

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
        try:
            await self.page.wait_for_load_state("networkidle", timeout=10_000)
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
            candidates = [page.get_by_label(t), page.get_by_placeholder(t), page.locator(f'[name="{t}"]')]
            if re.match(r"^[\w-]+$", t):
                candidates.append(page.locator(f"#{t}"))
            el = await self._first(candidates)
            if not el:
                raise ValueError(f'no field called "{t}" on the page')
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
                x, y = (step["x"], step["y"]) if "x" in step else cell_center(step["cell"])
                await page.mouse.click(x, y)
        elif a == "scroll":
            await page.mouse.wheel(0, SCROLL_PX if step.get("direction", "down") == "down" else -SCROLL_PX)
            await asyncio.sleep(0.5)
        elif a == "press":
            await page.keyboard.press(step["key"])
        elif a == "wait":
            await asyncio.sleep(step["seconds"])
        elif a == "goto":
            await page.goto(step["url"], timeout=60_000)
        else:
            raise ValueError(f"unknown action {a}")
        await self.settle()

    async def screenshot(self, grid: bool = False) -> bytes:
        """The visible part of the page; with `grid`, numbered squares drawn on it for `tap N`."""
        if not grid:
            return await self.page.screenshot(type="jpeg", quality=70)
        try:
            await self.page.evaluate(_GRID_JS, [GRID_COLS, GRID_ROWS])
        except Exception:
            return await self.page.screenshot(type="jpeg", quality=70)
        try:
            return await self.page.screenshot(type="jpeg", quality=75)
        finally:
            try:
                await self.page.evaluate("() => { const g = document.getElementById('__medic_grid'); if (g) g.remove(); }")
            except Exception:
                pass

    async def resolve_tap(self, step: dict) -> dict:
        """Turns `tap N` into what to click: the clickable element in that square nearest its middle (or the middle
        itself), with a unique id/label/name selector when it has one so a later run finds it even if the page
        shifts a little."""
        x, y = cell_center(step["cell"])
        out = {"action": "tap", "cell": step["cell"], "x": x, "y": y}
        w, h = VIEWPORT["width"] / GRID_COLS, VIEWPORT["height"] / GRID_ROWS
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
        try:
            return text.lower() in (await self.page.inner_text("body")).lower()
        except Exception:
            return False

    async def title(self) -> str:
        try:
            return (await self.page.title()) or self.page.url
        except Exception:
            return ""

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
    n.style.cssText = 'position:absolute;left:2px;top:1px;font:bold 11px/1 Arial,sans-serif;color:#fff;' +
      'background:rgba(220,0,70,.75);padding:1px 3px;border-radius:3px;';
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
