"""UpToDate renewals, numbered R1, R2, … apart from shop orders. They come from the admin's 🔄 Renew UpToDate
button or from the support chatbot's requests, run through one of the Automation sites (auto_sites) and are
completed only when the admin approves the result. The UpToDate password is stored encrypted (same key as the
sites) and never shown again.
"""

import re
import sqlite3
from datetime import datetime

import auto_sites

OPEN_STATUSES = ("requested", "running", "awaiting_approval")
STATUS_LABELS = {
    "requested": "📥 waiting for you",
    "running": "▶️ running",
    "awaiting_approval": "👀 waiting for your approval",
    "completed": "✅ completed",
    "failed": "❌ failed",
    "declined": "✋ not done this way",
    "cancelled": "🚫 cancelled",
}
COLUMNS = (
    "source", "status", "customer_user_id", "customer_label", "utd_username", "utd_password", "shop_order_id",
    "note", "site_id", "site_label", "attempts", "confirmation", "screenshot_file_id", "request_photo_file_id",
    "created_at", "updated_at", "completed_at",
)


def init(db_path: str):
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS renewals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source TEXT NOT NULL,
            status TEXT NOT NULL,
            customer_user_id INTEGER,
            customer_label TEXT,
            utd_username TEXT NOT NULL,
            utd_password TEXT,
            shop_order_id INTEGER,
            note TEXT,
            site_id INTEGER,
            site_label TEXT,
            attempts INTEGER NOT NULL DEFAULT 0,
            confirmation TEXT,
            screenshot_file_id TEXT,
            request_photo_file_id TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            completed_at TEXT
        )
        """
    )
    conn.commit()
    conn.close()


def _now() -> str:
    return datetime.utcnow().isoformat(timespec="seconds")


def _row(names, row) -> dict:
    r = dict(zip(names, row))
    r["utd_password"] = auto_sites.decrypt(r.get("utd_password") or "")
    r["number"] = f"R{r['id']}"
    return r


def create(db_path: str, source: str, utd_username: str, utd_password: str, customer_user_id: int = None,
           customer_label: str = "", shop_order_id: int = None, note: str = "", status: str = "requested") -> int:
    now = _now()
    conn = sqlite3.connect(db_path)
    cur = conn.execute(
        "INSERT INTO renewals (source, status, customer_user_id, customer_label, utd_username, utd_password, "
        "shop_order_id, note, attempts, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)",
        (source, status, customer_user_id, customer_label or "", utd_username, auto_sites.encrypt(utd_password or ""),
         shop_order_id, note or "", now, now),
    )
    conn.commit()
    rid = cur.lastrowid
    conn.close()
    return rid


def get(db_path: str, rid: int):
    conn = sqlite3.connect(db_path)
    cur = conn.execute("SELECT * FROM renewals WHERE id = ?", (rid,))
    names = [c[0] for c in cur.description]
    row = cur.fetchone()
    conn.close()
    return _row(names, row) if row else None


def update(db_path: str, rid: int, **fields):
    fields = {k: v for k, v in fields.items() if k in COLUMNS}
    if "utd_password" in fields:
        fields["utd_password"] = auto_sites.encrypt(fields["utd_password"] or "")
    fields["updated_at"] = _now()
    conn = sqlite3.connect(db_path)
    conn.execute(
        f"UPDATE renewals SET {', '.join(f'{k} = ?' for k in fields)} WHERE id = ?", [*fields.values(), rid]
    )
    conn.commit()
    conn.close()


def _select(db_path: str, where: str = "", params=(), limit: int = 15) -> list:
    conn = sqlite3.connect(db_path)
    cur = conn.execute(f"SELECT * FROM renewals {where} ORDER BY id DESC LIMIT ?", (*params, limit))
    names = [c[0] for c in cur.description]
    rows = [_row(names, r) for r in cur.fetchall()]
    conn.close()
    return rows


def recent(db_path: str, limit: int = 15) -> list:
    return _select(db_path, limit=limit)


def requests(db_path: str) -> list:
    """Requests from the chatbot the admin hasn't acted on yet."""
    return _select(db_path, "WHERE status = 'requested' AND source = 'chatbot'", limit=50)


def open_request_for(db_path: str, customer_user_id: int, utd_username: str):
    """An unfinished renewal for this customer and UpToDate account (so a repeated request isn't duplicated)."""
    rows = _select(
        db_path,
        f"WHERE customer_user_id = ? AND LOWER(utd_username) = LOWER(?) AND status IN ({','.join('?' * len(OPEN_STATUSES))})",
        (customer_user_id, utd_username, *OPEN_STATUSES), limit=1,
    )
    return rows[0] if rows else None


def search(db_path: str, query: str, limit: int = 15) -> list:
    """By renewal number (R12 or 12), UpToDate username, customer Telegram ID or @username / name."""
    q = (query or "").strip()
    m = re.fullmatch(r"[Rr#]?(\d+)", q)
    if m:
        num = int(m.group(1))
        return _select(db_path, "WHERE id = ? OR customer_user_id = ? OR shop_order_id = ?", (num, num, num), limit)
    like = f"%{q.lstrip('@').lower()}%"
    return _select(db_path, "WHERE LOWER(utd_username) LIKE ? OR LOWER(customer_label) LIKE ?", (like, like), limit)


def describe(r: dict) -> str:
    """The log entry, for the admin (never the password)."""
    lines = [
        f"🔄 Renewal {r['number']} · {STATUS_LABELS.get(r['status'], r['status'])}",
        f"UpToDate account: {r['utd_username']}",
        "Asked by: " + ("you (🔄 Renew UpToDate)" if r["source"] == "admin"
                        else f"chatbot customer {r.get('customer_label') or ''} (ID {r.get('customer_user_id')})"),
    ]
    if r.get("shop_order_id"):
        lines.append(f"Shop order: #{r['shop_order_id']}")
    if r.get("note"):
        lines.append(f"Note: {r['note']}")
    if r.get("site_label"):
        lines.append(f"Site: {r['site_label']} · attempts: {r.get('attempts') or 0}")
    if r.get("confirmation"):
        lines.append(f"The site said: “{r['confirmation']}”")
    lines.append(f"Requested: {(r.get('created_at') or '')[:16].replace('T', ' ')} UTC")
    if r.get("completed_at"):
        lines.append(f"Completed: {r['completed_at'][:16].replace('T', ' ')} UTC")
    return "\n".join(lines)


def short(r: dict) -> str:
    icon = STATUS_LABELS.get(r["status"], "?").split(" ")[0]
    return f"{icon} {r['number']} · {r['utd_username']} · {(r.get('created_at') or '')[:10]}"
