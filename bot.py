"""
Telegram Order Bot — cart + checkout + manual payment confirmation
with receipt photo upload.

Built for markets like Lebanon where Telegram's native Payments API
(Stripe-backed) isn't available. The bot collects the order, shows
payment instructions (transfer details and/or a payment link), asks
the customer to upload a screenshot of their receipt after paying,
forwards that receipt to you, and lets you confirm or reject with a
tap — which then automatically messages the customer back.

-------------------------------------------------------------------------
SETUP
-------------------------------------------------------------------------
1. pip install -r requirements.txt
2. Create a bot with @BotFather, get the token.
3. Get your own numeric Telegram user ID (message @userinfobot).
4. Set BOT_TOKEN and ADMIN_CHAT_ID as environment variables
   (in Railway: Variables tab). Do NOT hardcode the token in this file
   or paste it anywhere public — if it leaks, revoke it via BotFather
   (/mybots -> your bot -> API Token -> Revoke) and generate a new one.
5. Edit the MENU dict below with your real items/prices.
6. Edit PAYMENT_INSTRUCTIONS with your real payment details.
7. Run: python bot.py

The bot stores orders in a local SQLite file (orders.db) — no external
DB needed to get started.
-------------------------------------------------------------------------
"""

import json
import logging
import os
import re
import sqlite3
from datetime import datetime

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LabeledPrice,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    PersistenceInput,
    PicklePersistence,
    PreCheckoutQueryHandler,
    filters,
)

# ------------------------------------------------------------------
# CONFIG — edit these
# ------------------------------------------------------------------

BOT_TOKEN = os.environ.get("BOT_TOKEN", "PUT_YOUR_BOT_TOKEN_HERE")
ADMIN_CHAT_ID = int(os.environ.get("ADMIN_CHAT_ID", "0"))  # your Telegram user ID

DB_PATH = os.environ.get("DB_PATH", os.path.join(os.path.dirname(__file__), "orders.db"))

CURRENCY = "$"

# Telegram Stars pricing: ~50 Stars per $1 (based on the in-app purchase
# packages, e.g. 100 Stars = $2.00). Adjust if Telegram's pricing changes.
STAR_RATE = 50

# Your menu. Keys are short item IDs, values are (display name, price).
MENU = {
    "item1": ("Uptodate Online", 20.00),
    "imd_new_6m": ("iMD VIP New Account - 6 Months", 50.00),
    "imd_new_1y": ("iMD VIP New Account - 1 Year", 75.00),
    "imd_renew_6m": ("iMD VIP Renewal - 6 Months", 50.00),
    "imd_renew_1y": ("iMD VIP Renewal - 1 Year", 75.00),
    "item3": ("Uptodate Online + Offline", 30.00),
    "item4": ("Amboss Full Access - 1 year", 85.00),
}

# ------------------------------------------------------------------
# iMD FORM COLLECTION
# ------------------------------------------------------------------

# All four iMD variants trigger the guided collection flow.
IMD_TRIGGER_ITEMS = {"imd_new_6m", "imd_new_1y", "imd_renew_6m", "imd_renew_1y"}
IMD_NEW_ITEMS = {"imd_new_6m", "imd_new_1y"}
IMD_RENEW_ITEMS = {"imd_renew_6m", "imd_renew_1y"}

# Maps each item id to which serial pool it should draw from.
IMD_DURATION_MAP = {
    "imd_new_6m": "6m",
    "imd_new_1y": "1y",
    "imd_renew_6m": "6m",
    "imd_renew_1y": "1y",
}

IMD_REGISTER_URL = "https://imedicaldoctor.net/register/index.php"
IMD_RENEW_URL = "https://imedicaldoctor.net/ess.php"

# Confirmed directly from the register page's HTML source.
# Real field names (not the visible labels): username, password,
# passwordverify (no underscore), email, serial. There's also a required
# hidden field, `register`, sent with an empty value.
IMD_REGISTER_FIELD_MAP = {
    "username": "username",
    "password": "password",
    "verify_password": "passwordverify",
    "email": "email",
    "serial": "serial",
}

# Confirmed directly from the renewal page's HTML source. Same field names
# as the register page's username/serial, plus a required hidden field,
# `extend`, submitted with an empty value.
IMD_RENEW_FIELD_MAP = {
    "username": "username",
    "serial": "serial",
}

# ------------------------------------------------------------------
# RESULT CLASSIFICATION
# ------------------------------------------------------------------
# After submitting, we read the resulting page text and match it against
# these keyword lists to decide what actually happened. Anything that
# doesn't match a known pattern is treated as UNKNOWN — the customer is
# NOT sent credentials and the admin is asked to check manually. That's
# deliberate: silently reporting success when we can't confirm it is the
# worst outcome (it's what caused a bad serial to be reported as a
# successful renewal).
#
# These keyword lists are best-effort guesses at iMD's wording. To make
# classification reliable, trigger each failure case once and check what
# the page actually says, then add those exact phrases here.
IMD_SUCCESS_KEYWORDS = [
    # Exact phrases iMD uses (confirmed):
    "account is created successfully",     # registration
    "account is extended successfully",    # renewal/extension
    # Looser variants, in case the wording shifts slightly:
    "created successfully", "extended successfully",
    "successfully", "success",
]
IMD_SERIAL_ERROR_KEYWORDS = [
    # Exact phrases iMD uses (confirmed):
    "there is no such serial",
    "this serial has been used before",
    # Looser variants, in case the wording shifts slightly:
    "no such serial", "serial has been used", "invalid serial",
    "serial not", "wrong serial", "serial is not", "serial already",
    "used serial", "expired serial", "serial does not",
]
IMD_USERNAME_ERROR_KEYWORDS = [
    # Exact phrases iMD uses (confirmed):
    "this username is taken",          # registration: name already exists
    "username is not valid",           # renewal: no account with that name
    # Looser variants, in case the wording shifts slightly:
    "username is taken", "pick another username", "username already",
    "username exists", "not valid", "user not found",
    "username not found", "no such user", "invalid username",
    "username does not",
]
IMD_EMAIL_ERROR_KEYWORDS = [
    "email already", "email is taken", "email exists", "invalid email",
    "email address is",
]

# ------------------------------------------------------------------
# NON-iMD SUBSCRIPTIONS
# ------------------------------------------------------------------
# Everything that isn't iMD is fulfilled manually within 48 hours. The
# bot still collects the customer's details up front so the admin has
# everything needed.

GENERIC_PASSWORD_SYMBOLS = "@_#$"

GENERIC_RULES_TEXT = (
    "• Username: at least 6 characters\n"
    "• Password: at least 8 characters, including at least one number, "
    f"one capital letter, and one symbol from {GENERIC_PASSWORD_SYMBOLS}"
)

DELIVERY_48H_MESSAGE = (
    "✅ Thanks! Your subscription will be delivered here within the next 48 hours.\n\n"
    "If you haven't heard from us after 48 hours, please contact support from the menu."
)

# The fields collected for non-iMD subscriptions, in order.
GENERIC_FIELDS = [
    ("first_name", "First name:"),
    ("last_name", "Last name:"),
    ("email", "Email address:"),
    ("username", "Desired username (at least 6 characters):"),
    (
        "password",
        "Desired password (at least 8 characters, with at least one number, "
        f"one capital letter, and one symbol from {GENERIC_PASSWORD_SYMBOLS}):",
    ),
]

# Used when re-prompting a customer who has an unfinished order.
IMD_FIELD_PROMPTS = {
    "prev_username": "Reply with your previous iMD username:",
    "email": "Reply with your email address:",
    "username": "Reply with your desired username:",
    "password": "Reply with your desired password:",
}


def validate_generic_username(value: str):
    """Returns None if valid, or an error message explaining the rule."""
    if len(value) < 6:
        return "Username must be at least 6 characters long. Please choose another one:"
    return None


def validate_generic_password(value: str):
    """Returns None if valid, or an error message explaining the rules."""
    problems = []
    if len(value) < 8:
        problems.append("at least 8 characters")
    if not any(c.isdigit() for c in value):
        problems.append("at least one number")
    if not any(c.isupper() for c in value):
        problems.append("at least one capital letter")
    if not any(c in GENERIC_PASSWORD_SYMBOLS for c in value):
        problems.append(f"at least one symbol from {GENERIC_PASSWORD_SYMBOLS}")
    if problems:
        return (
            "That password doesn't meet the requirements. It needs "
            + ", ".join(problems)
            + ".\n\nPlease choose another password:"
        )
    return None


# Payment instructions shown to the customer at checkout.
PAYMENT_INSTRUCTIONS = (
    "Choose how you'd like to pay below.\n\n"
    "After paying, tap *I've Paid* and upload a screenshot of your "
    "receipt. We'll verify it and confirm your order."
)

# Card payment link (Visa/Mastercard).
PAYMENT_LINK_BASE_URL = "https://payments.suyool.com/pay/g401_MD"

# Countries offered under "Pay using local payment methods."
LOCAL_PAYMENT_COUNTRIES = ["Lebanon", "Jordan", "India", "Ghana", "Pakistan", "Europe", "USA", "KSA", "Russia"]

# Payment instructions per country. Wrapped in backticks where possible so
# the ID/number is tap-to-copy in Telegram.
LOCAL_PAYMENT_INSTRUCTIONS = {
    "Lebanon": (
        "Whish to Whish\n\n"
        "WhishMoney Number: (Tap to copy)\n`81666579`"
    ),
    "Jordan": (
        "Tap on one of the cliq IDs below to copy:\n\n"
        "CLIQ ALIAS: `WKS777`\n(Orange Money)\nWALEED SHAQFEH\n\n"
        "CLIQ ALIAS: `WKS999`\n(Etihad bank)\nWALEED SHAQFEH\n\n"
        "CLIQ ALIAS: `WKS555`\n(Zain Cash)\nMohammad shamalti\n\n"
        "🧾 After the payment is done, please send a screenshot of the receipt "
        "and your name on cliq.\n\n"
        "We will reach out back at the earliest to register your account. Please be patient."
    ),
    "India": (
        "UPI ID (tap to copy)\n`s4005194160889795@slc`\n\n"
        "Name: Shilpaben karetiya\n\n"
        "‼️ Important Remarks:\n\n"
        "➖ This ID is valid for the next 48 hours. Confirm with us before you do "
        "the payment if you are doing it after 48 hours.\n\n"
        "➖ Upon completion of the payment, please send the full receipt (with the "
        "transaction ID or UTR shown) and await our response and confirmation. "
        "We will contact you promptly to proceed with your account registration.\n\n"
        "➖ STRICTLY don't mention anything regarding the subscription in the "
        "comments/remarks of the payment. Just leave it empty.\n\n"
        "➖ If you could not complete the payment in whole, do it in parts. "
        "Eg. If the app didn't allow you to send 4000 Rupees, send 2000 rupees twice."
    ),
    "Ghana": (
        "Tap on the number to copy:\n\n"
        "📲 `0257505632`\nMTN Mobile Money\nSaibu Mahamadu\n\n"
        "📲 `0204860754`\nTelecel/Vodafone\nSaibu Mahamadu\n\n"
        "Strictly do not call the number please. Just do the payment."
    ),
    "Pakistan": "Local payment details for Pakistan go here.",
    "Europe": (
        "Revolut (visa to visa)\n\n"
        "1) Open your Revolut app/website\n"
        "2) Click \"Transfer\" > \"+ New\" > \"Card recipient\"\n"
        "3) Enter the details\n\n"
        "Card Number: `5413525250267271`\n"
        "Name: Alijon Karimov\n"
        "Country: Tajikistan"
    ),
    "USA": "Local payment details for the USA (Zelle, etc.) go here.",
    "KSA": (
        "(Tap to copy)\n\n"
        "`SA8710000006857309000101`\n\n"
        "`SA0510000062300187719603`\n\n"
        "Bank: Ahli Bank\n"
        "Name: Jamil Hajji\n\n"
        "Please make sure the purpose of the payment be Friends and family or "
        "personal NOT goods or services."
    ),
    "Russia": (
        "1- Open the Sberbank app/website.\n"
        "2- Navigate to the \"Payments\" section.\n"
        "3- Select \"International wire transfers.\"\n"
        "4- Choose the option to transfer to a card or account using the "
        "recipient's phone number.\n\n"
        "Payment Details:\n\n"
        "Country: Tajikistan\n"
        "Phone number: `+992002373232`\n"
        "Name: Alijon Karimov\n"
        "Recipient Bank: Alif Bank"
    ),
}

# Crypto wallet addresses shown under "Pay with Cryptocurrency."
CRYPTO_INSTRUCTIONS = (
    "*Crypto Wallet Address*\n"
    "(Tap on the wallet address to copy)\n\n"
    "*USDT (BEP20 Network)*\n`0xefbab9265bb3a22492e4a100c27791da385eeb37`\n\n"
    "*USDT (TRC20 Network)*\n`TM8JhfHfaxNgBRs3benrTkWAz6Zprk9QmF`\n\n"
    "*Bitcoin (BTC Network)*\n`18rALgWKZZPMgGjVBCDBPgnnpdaj8rEQ2x`\n\n"
    "Bybit UID: `65577310`\n\n"
    "OKX UID: `847060953866125494`\n\n"
    "BingX UID: `10276647`"
)

# India's QR code image, sent alongside the India payment instructions.
# Upload the QR image to your repo at this exact path (assets/india_qr.jpg)
# for it to be sent automatically.
INDIA_QR_PATH = os.path.join(os.path.dirname(__file__), "assets", "india_qr.jpg")

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


# ------------------------------------------------------------------
# DATABASE
# ------------------------------------------------------------------

def db_init():
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            username TEXT,
            items_json TEXT NOT NULL,
            total REAL NOT NULL,
            status TEXT NOT NULL DEFAULT 'awaiting_payment',
            created_at TEXT NOT NULL
        )
        """
    )
    # Added later for record-keeping — ALTER TABLE is skipped if the column
    # already exists, so this is safe to run on every startup.
    for column, coltype in [("credentials", "TEXT"), ("delivered_at", "TEXT")]:
        try:
            conn.execute(f"ALTER TABLE orders ADD COLUMN {column} {coltype}")
        except sqlite3.OperationalError:
            pass  # column already exists

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS serials (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            duration TEXT NOT NULL,
            code TEXT NOT NULL UNIQUE,
            status TEXT NOT NULL DEFAULT 'available',
            used_for_order INTEGER,
            used_at TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS deliveries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            item_id TEXT,
            message TEXT NOT NULL,
            delivered_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS fulfilment (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            item_id TEXT NOT NULL,
            unit_no INTEGER NOT NULL DEFAULT 1,
            state TEXT NOT NULL DEFAULT 'needs_info',
            info_json TEXT,
            created_at TEXT NOT NULL,
            UNIQUE(order_id, item_id, unit_no)
        )
        """
    )
    conn.commit()
    conn.close()


def db_add_delivery(order_id: int, user_id: int, item_id: str, message: str):
    """Records one delivered subscription. An order with several products
    produces several rows here, so 'My Subscriptions' can list them
    separately instead of lumping a whole order into one entry."""
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT INTO deliveries (order_id, user_id, item_id, message, delivered_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (order_id, user_id, item_id, message, datetime.utcnow().isoformat()),
    )
    conn.commit()
    conn.close()


def db_user_deliveries(user_id: int):
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT id, order_id, item_id, message, delivered_at FROM deliveries "
        "WHERE user_id = ? ORDER BY id DESC",
        (user_id,),
    ).fetchall()
    conn.close()
    return rows


def db_get_delivery(delivery_id: int, user_id: int):
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT message FROM deliveries WHERE id = ? AND user_id = ?",
        (delivery_id, user_id),
    ).fetchone()
    conn.close()
    return row[0] if row else None




# Fulfilment states: 'needs_info' -> we still need details from the customer
#                   'awaiting_delivery' -> details in, waiting on us
#                   'delivered' -> done
#
# One row per purchased UNIT, not per product: ordering two Uptodate
# subscriptions creates two independent rows, each collecting its own
# details and delivered separately.

def db_add_fulfilment_items(order_id: int, user_id: int, items: dict):
    """Records every purchased unit of a paid order so the fulfilment queue
    lives in the database rather than in memory — a redeploy would
    otherwise wipe it and silently strand half-finished orders."""
    conn = sqlite3.connect(DB_PATH)
    for item_id, qty in items.items():
        for unit_no in range(1, int(qty) + 1):
            try:
                conn.execute(
                    "INSERT INTO fulfilment (order_id, user_id, item_id, unit_no, state, created_at) "
                    "VALUES (?, ?, ?, ?, 'needs_info', ?)",
                    (order_id, user_id, item_id, unit_no, datetime.utcnow().isoformat()),
                )
            except sqlite3.IntegrityError:
                pass  # already recorded
    conn.commit()
    conn.close()


def db_next_needs_info(order_id: int):
    """The next unit in this order still waiting on customer details.
    Returns (fulfilment_id, item_id) or None. iMD units come first
    because they're delivered instantly."""
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT id, item_id FROM fulfilment WHERE order_id = ? AND state = 'needs_info' ORDER BY id",
        (order_id,),
    ).fetchall()
    conn.close()
    for fid, item_id in rows:
        if item_id in IMD_TRIGGER_ITEMS:
            return fid, item_id
    return rows[0] if rows else None


def db_set_fulfilment_state(fulfilment_id: int, state: str):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("UPDATE fulfilment SET state = ? WHERE id = ?", (state, fulfilment_id))
    conn.commit()
    conn.close()


def db_set_fulfilment_info(fulfilment_id: int, info: dict):
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "UPDATE fulfilment SET info_json = ? WHERE id = ?", (json.dumps(info), fulfilment_id)
    )
    conn.commit()
    conn.close()


def db_get_fulfilment(fulfilment_id: int):
    """Returns (id, order_id, user_id, item_id, unit_no, state, info_json)."""
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT id, order_id, user_id, item_id, unit_no, state, info_json "
        "FROM fulfilment WHERE id = ?",
        (fulfilment_id,),
    ).fetchone()
    conn.close()
    return row


def db_order_undelivered_items(order_id: int):
    """Units of this order not yet delivered: (fulfilment_id, item_id, state)."""
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT id, item_id, state FROM fulfilment WHERE order_id = ? AND state != 'delivered' ORDER BY id",
        (order_id,),
    ).fetchall()
    conn.close()
    return rows


def db_user_pending_items(user_id: int):
    """Units this customer has paid for that aren't delivered yet.
    Returns (fulfilment_id, order_id, item_id, unit_no, state)."""
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT f.id, f.order_id, f.item_id, f.unit_no, f.state FROM fulfilment f "
        "JOIN orders o ON o.id = f.order_id "
        "WHERE f.user_id = ? AND f.state != 'delivered' "
        "AND o.status NOT IN ('cancelled', 'rejected') "
        "ORDER BY f.order_id DESC, f.id",
        (user_id,),
    ).fetchall()
    conn.close()
    return rows


def db_all_pending_items():
    """Every undelivered unit across all customers, for the admin view.
    Returns (fulfilment_id, order_id, user_id, username, item_id, unit_no, state)."""
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT f.id, f.order_id, f.user_id, o.username, f.item_id, f.unit_no, f.state "
        "FROM fulfilment f JOIN orders o ON o.id = f.order_id "
        "WHERE f.state != 'delivered' AND o.status = 'paid' "
        "ORDER BY f.order_id, f.id",
    ).fetchall()
    conn.close()
    return rows


def db_user_delivered_units(user_id: int):
    """Delivered units with their collected info, for the customer lookup."""
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT f.id, f.order_id, f.item_id, f.unit_no, f.info_json FROM fulfilment f "
        "WHERE f.user_id = ? AND f.state = 'delivered' ORDER BY f.order_id DESC, f.id",
        (user_id,),
    ).fetchall()
    conn.close()
    return rows


def db_add_serials(duration: str, codes: list) -> int:
    """Adds serials to the pool. Duplicate codes are silently skipped
    (UNIQUE constraint). Returns how many were actually added."""
    conn = sqlite3.connect(DB_PATH)
    added = 0
    for code in codes:
        try:
            conn.execute(
                "INSERT INTO serials (duration, code, status) VALUES (?, ?, 'available')",
                (duration, code),
            )
            added += 1
        except sqlite3.IntegrityError:
            pass  # duplicate code, skip
    conn.commit()
    conn.close()
    return added


def db_list_serials(duration: str = None, status: str = "available"):
    """Returns the actual serial codes, not just a count.
    Pass duration=None for both pools, status=None for every status."""
    conn = sqlite3.connect(DB_PATH)
    query = "SELECT duration, code, status, used_for_order FROM serials WHERE 1=1"
    params = []
    if duration:
        query += " AND duration = ?"
        params.append(duration)
    if status:
        query += " AND status = ?"
        params.append(status)
    query += " ORDER BY duration, id"
    rows = conn.execute(query, params).fetchall()
    conn.close()
    return rows


def db_count_serials(duration: str) -> int:
    conn = sqlite3.connect(DB_PATH)
    count = conn.execute(
        "SELECT COUNT(*) FROM serials WHERE duration = ? AND status = 'available'", (duration,)
    ).fetchone()[0]
    conn.close()
    return count


def db_pop_serial(duration: str):
    """Reserves one available serial for this duration (marks it 'pending'
    so it can't be double-assigned). Returns (serial_id, code) or None if
    the pool is empty. Call db_finalize_serial afterward to either confirm
    it as used or release it back to available."""
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT id, code FROM serials WHERE duration = ? AND status = 'available' ORDER BY id LIMIT 1",
        (duration,),
    ).fetchone()
    if not row:
        conn.close()
        return None
    serial_id, code = row
    conn.execute("UPDATE serials SET status = 'pending' WHERE id = ?", (serial_id,))
    conn.commit()
    conn.close()
    return serial_id, code


def db_finalize_serial(serial_id: int, order_id: int, success: bool):
    conn = sqlite3.connect(DB_PATH)
    if success:
        conn.execute(
            "UPDATE serials SET status = 'used', used_for_order = ?, used_at = ? WHERE id = ?",
            (order_id, datetime.utcnow().isoformat(), serial_id),
        )
    else:
        conn.execute("UPDATE serials SET status = 'available' WHERE id = ?", (serial_id,))
    conn.commit()
    conn.close()


def db_create_order(user_id: int, username: str, items: dict, total: float) -> int:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.execute(
        "INSERT INTO orders (user_id, username, items_json, total, status, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (user_id, username, json.dumps(items), total, "awaiting_payment", datetime.utcnow().isoformat()),
    )
    conn.commit()
    order_id = cur.lastrowid
    conn.close()
    return order_id


def db_get_order(order_id: int):
    # Explicit column list (not SELECT *) so existing code that unpacks this
    # tuple keeps working even after new columns are added to the table.
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT id, user_id, username, items_json, total, status, created_at "
        "FROM orders WHERE id = ?",
        (order_id,),
    ).fetchone()
    conn.close()
    return row


def db_save_delivery(order_id: int, credentials_text: str):
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "UPDATE orders SET status = ?, credentials = ?, delivered_at = ? WHERE id = ?",
        ("delivered", credentials_text, datetime.utcnow().isoformat(), order_id),
    )
    conn.commit()
    conn.close()


def db_update_status(order_id: int, status: str):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("UPDATE orders SET status = ? WHERE id = ?", (status, order_id))
    conn.commit()
    conn.close()



def db_user_orders(user_id: int, limit: int = 5):
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT id, items_json, total, status, created_at FROM orders "
        "WHERE user_id = ? ORDER BY id DESC LIMIT ?",
        (user_id, limit),
    ).fetchall()
    conn.close()
    return rows


# ------------------------------------------------------------------
# HELPERS
# ------------------------------------------------------------------

def format_cart(cart: dict) -> str:
    if not cart:
        return "Your cart is empty."
    lines = []
    total = 0.0
    for item_id, qty in cart.items():
        name, price = MENU[item_id]
        subtotal = price * qty
        total += subtotal
        lines.append(f"{qty}x {name} — {CURRENCY}{subtotal:.2f}")
    lines.append(f"\n*Total: {CURRENCY}{total:.2f}*")
    return "\n".join(lines)


def cart_total(cart: dict) -> float:
    return sum(MENU[item_id][1] * qty for item_id, qty in cart.items())


def menu_keyboard() -> InlineKeyboardMarkup:
    rows = []
    for item_id, (name, price) in MENU.items():
        if item_id in IMD_TRIGGER_ITEMS:
            continue  # shown via the "🎓 iMD VIP" submenu button instead
        rows.append(
            [InlineKeyboardButton(f"{name} — {CURRENCY}{price:.2f}", callback_data=f"add:{item_id}")]
        )
    rows.append([InlineKeyboardButton("🎓 iMD VIP", callback_data="imd_menu")])
    rows.append([InlineKeyboardButton("🛒 View Cart / Checkout", callback_data="view_cart")])
    return InlineKeyboardMarkup(rows)


async def imd_menu_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Shows the New Account / Renew Account choice."""
    query = update.callback_query
    await query.answer()
    buttons = [
        [InlineKeyboardButton("🆕 Buy New Account", callback_data="imd_type:new")],
        [InlineKeyboardButton("🔄 Renew Previous Account", callback_data="imd_type:renew")],
        [InlineKeyboardButton("⬅️ Back", callback_data="back_to_menu")],
    ]
    await query.edit_message_text("iMD VIP — what would you like to do?", reply_markup=InlineKeyboardMarkup(buttons))


async def imd_type_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Shows the 6 months / 1 year choice for the chosen type."""
    query = update.callback_query
    await query.answer()
    imd_type = query.data.split(":", 1)[1]  # "new" or "renew"

    six_month_id = f"imd_{imd_type}_6m"
    one_year_id = f"imd_{imd_type}_1y"
    six_month_price = MENU[six_month_id][1]
    one_year_price = MENU[one_year_id][1]

    buttons = [
        [InlineKeyboardButton(f"6 Months — {CURRENCY}{six_month_price:.2f}", callback_data=f"add:{six_month_id}")],
        [InlineKeyboardButton(f"1 Year — {CURRENCY}{one_year_price:.2f}", callback_data=f"add:{one_year_id}")],
        [InlineKeyboardButton("⬅️ Back", callback_data="imd_menu")],
    ]
    label = "New Account" if imd_type == "new" else "Renew Previous Account"
    await query.edit_message_text(f"iMD VIP — {label}. Choose a duration:", reply_markup=InlineKeyboardMarkup(buttons))


# Persistent bottom keyboard labels (must match exactly between the keyboard
# and the handler that checks incoming text against them).
BUY_LABEL = "🛒 Buy New Subscription"
MY_SUBS_LABEL = "📋 My Subscriptions"
BASKET_LABEL = "🧺 Check the Basket and Pay"
SUPPORT_LABEL = "🆘 Support"

# Admin-only panel labels.
A_VIEW_SERIALS = "🔑 View Serials"
A_ADD_SERIALS = "➕ Add Serials"
A_REMOVE_SERIAL = "🗑 Remove Serial"
A_RECENT_ORDERS = "📊 Recent Orders"
A_PENDING = "⏳ Pending Orders"
A_FIND_ORDER = "🔎 Find Order"
A_FIND_CUSTOMER = "👤 Find Customer"
A_CUSTOMER_VIEW = "🛍 Customer Menu"

ADMIN_LABELS = [
    A_VIEW_SERIALS, A_ADD_SERIALS, A_REMOVE_SERIAL,
    A_RECENT_ORDERS, A_PENDING, A_FIND_ORDER, A_FIND_CUSTOMER, A_CUSTOMER_VIEW,
]


def main_menu_keyboard() -> ReplyKeyboardMarkup:
    """A persistent keyboard pinned to the bottom of the chat — stays visible
    across every message, not just the one it was attached to."""
    return ReplyKeyboardMarkup(
        [[BUY_LABEL, MY_SUBS_LABEL], [BASKET_LABEL], [SUPPORT_LABEL]],
        resize_keyboard=True,
    )


def admin_menu_keyboard() -> ReplyKeyboardMarkup:
    """The admin's persistent panel — every admin function as a button, so
    none of the slash commands need to be typed from memory."""
    return ReplyKeyboardMarkup(
        [
            [A_VIEW_SERIALS, A_ADD_SERIALS],
            [A_REMOVE_SERIAL, A_RECENT_ORDERS],
            [A_PENDING],
            [A_FIND_ORDER, A_FIND_CUSTOMER],
            [A_CUSTOMER_VIEW],
        ],
        resize_keyboard=True,
    )


def cart_keyboard(cart: dict = None) -> InlineKeyboardMarkup:
    """Basket view: a remove button per item, plus add-more and checkout."""
    rows = []
    if cart:
        for item_id, qty in cart.items():
            name = MENU.get(item_id, (item_id, 0))[0]
            rows.append(
                [InlineKeyboardButton(f"❌ Remove {name} ({qty})", callback_data=f"remove:{item_id}")]
            )
    rows.append([InlineKeyboardButton("➕ Add more items", callback_data="back_to_menu")])
    if cart:
        rows.append([InlineKeyboardButton("🗑 Clear basket", callback_data="clear_cart")])
        rows.append([InlineKeyboardButton("✅ Checkout", callback_data="checkout")])
    return InlineKeyboardMarkup(rows)


def admin_review_keyboard(order_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ Confirm Payment", callback_data=f"admin_confirm:{order_id}"),
                InlineKeyboardButton("❌ Reject", callback_data=f"admin_reject:{order_id}"),
            ]
        ]
    )


# ------------------------------------------------------------------
# HANDLERS — customer side
# ------------------------------------------------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.setdefault("cart", {})
    if update.effective_user.id == ADMIN_CHAT_ID:
        await update.message.reply_text(
            "Admin panel — use the buttons below any time.",
            reply_markup=admin_menu_keyboard(),
        )
        return
    await update.message.reply_text(
        "Welcome! Use the buttons below any time.",
        reply_markup=main_menu_keyboard(),
    )


async def main_menu_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles taps on the persistent bottom keyboard: Buy, My Subscriptions,
    Support."""
    text = update.message.text

    if text == BUY_LABEL:
        context.user_data.setdefault("cart", {})
        await update.message.reply_text(
            "Tap an item below to add it to your order.", reply_markup=menu_keyboard()
        )

    elif text == MY_SUBS_LABEL:
        await show_my_subscriptions(update, context)

    elif text == BASKET_LABEL:
        cart = context.user_data.setdefault("cart", {})
        await update.message.reply_text(
            format_cart(cart), parse_mode=ParseMode.MARKDOWN, reply_markup=cart_keyboard(cart)
        )

    elif text == SUPPORT_LABEL:
        await update.message.reply_text(
            "Tap below to contact support:",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("Open Support Chat", url="https://t.me/uptodate_admin")]]
            ),
        )


def subs_menu_keyboard() -> InlineKeyboardMarkup:
    """The two-way split under 'My Subscriptions'."""
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("✅ Registered Subscriptions", callback_data="subs_registered")],
            [InlineKeyboardButton("⏳ Pending Orders", callback_data="subs_pending")],
        ]
    )


def subscriptions_keyboard(user_id: int):
    """One button per delivered subscription — a multi-product order shows
    up as several separate entries, not one lumped-together button."""
    rows = db_user_deliveries(user_id)
    if not rows:
        return None
    buttons = []
    for delivery_id, order_id, item_id, message, delivered_at in rows:
        name = MENU.get(item_id, (None,))[0] if item_id else None
        if not name:
            # Legacy rows saved before per-item tracking: fall back to the
            # order's product names so the button is still meaningful
            # rather than an opaque "Order #N".
            order = db_get_order(order_id)
            if order:
                try:
                    items = json.loads(order[3])
                    name = ", ".join(MENU[i][0] for i in items if i in MENU)
                except (TypeError, ValueError):
                    name = None
            name = name or f"Order #{order_id}"
        label = name + (f" ({delivered_at[:10]})" if delivered_at else "")
        buttons.append([InlineKeyboardButton(label[:60], callback_data=f"mysub:{delivery_id}")])
    buttons.append([InlineKeyboardButton("⬅️ Back", callback_data="subs_menu")])
    return InlineKeyboardMarkup(buttons)


def pending_keyboard(user_id: int):
    """One button per ordered-but-undelivered item."""
    rows = db_user_pending_items(user_id)
    if not rows:
        return None
    buttons = []
    for fid, order_id, item_id, unit_no, state in rows:
        name = MENU.get(item_id, (item_id,))[0]
        suffix = f" #{unit_no}" if unit_no > 1 else ""
        buttons.append(
            [InlineKeyboardButton(f"#{order_id} — {name}{suffix}"[:60], callback_data=f"pend:{fid}")]
        )
    buttons.append([InlineKeyboardButton("⬅️ Back", callback_data="subs_menu")])
    return InlineKeyboardMarkup(buttons)


async def show_my_subscriptions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Top level of 'My Subscriptions' — registered vs pending."""
    await update.message.reply_text(
        "What would you like to see?", reply_markup=subs_menu_keyboard()
    )


async def subs_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("What would you like to see?", reply_markup=subs_menu_keyboard())


async def subs_registered(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Lists delivered subscriptions, one per product."""
    query = update.callback_query
    await query.answer()
    keyboard = subscriptions_keyboard(query.from_user.id)
    back = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="subs_menu")]])

    if not keyboard:
        await query.edit_message_text(
            "You don't have any active subscriptions yet.", reply_markup=back
        )
        return

    await query.edit_message_text(
        "Your subscriptions — tap one to see its details:", reply_markup=keyboard
    )


async def subs_pending(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Lists orders that haven't been delivered yet."""
    query = update.callback_query
    await query.answer()
    keyboard = pending_keyboard(query.from_user.id)
    back = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="subs_menu")]])

    if not keyboard:
        await query.edit_message_text("You don't have any pending orders.", reply_markup=back)
        return

    await query.edit_message_text(
        "Your pending orders — tap one for its status:", reply_markup=keyboard
    )


async def pending_detail(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Shows what's holding up a pending item: either the bot is waiting on
    information from the customer (in which case it re-asks for exactly
    the missing field), or it's with us and they just need to wait."""
    query = update.callback_query
    await query.answer()
    fulfilment_id = int(query.data.split(":", 1)[1])

    row = db_get_fulfilment(fulfilment_id)
    back = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="subs_pending")]])
    if not row:
        await query.edit_message_text("That order is no longer pending.", reply_markup=back)
        return

    _, order_id, user_id, item_id, unit_no, state, info_json = row
    name = MENU.get(item_id, (item_id,))[0]

    order = db_get_order(order_id)
    status = order[5] if order else None

    if status in ("awaiting_payment", "awaiting_receipt", "awaiting_confirmation"):
        if status == "awaiting_confirmation":
            text = (
                f"⏳ *{name}* (Order #{order_id})\n\n"
                "We've received your receipt and are verifying your payment. "
                "You'll hear from us as soon as it's confirmed."
            )
        else:
            text = (
                f"⏳ *{name}* (Order #{order_id})\n\n"
                "We haven't received your payment yet. Please complete the payment, "
                "then tap *I've Paid* and upload your receipt."
            )
        await query.edit_message_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=back)
        return

    if state == "needs_info":
        # Re-ask for whatever this unit is still missing.
        reg_field = context.user_data.get("awaiting_registration_field")
        gen_field = context.user_data.get("awaiting_generic_field")

        if reg_field and context.user_data.get("registration_fulfilment_id") == fulfilment_id:
            prompt = IMD_FIELD_PROMPTS.get(reg_field, "Please send the requested information:")
        elif gen_field and context.user_data.get("generic_fulfilment_id") == fulfilment_id:
            prompt = dict(GENERIC_FIELDS).get(gen_field, "Please send the requested information:")
        else:
            # Nothing in progress for this unit — restart its collection.
            await query.edit_message_text(
                f"📝 *{name}* (Order #{order_id})\n\nWe still need some details from you.",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=back,
            )
            await process_next_in_queue(context, query.from_user.id, order_id)
            return

        await query.edit_message_text(
            f"📝 *{name}* (Order #{order_id})\n\nWe still need some information from you.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=back,
        )
        await context.bot.send_message(chat_id=query.from_user.id, text=prompt)
        return

    await query.edit_message_text(
        f"⏳ *{name}* (Order #{order_id})\n\n"
        "Your payment is confirmed and we have everything we need. Your account is being "
        "prepared and will be delivered here shortly — please bear with us.\n\n"
        "If it's been more than 48 hours, contact support from the menu.",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=back,
    )


async def my_subscription_detail(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Replays the delivery message for one of the customer's subscriptions."""
    query = update.callback_query
    await query.answer()
    delivery_id = int(query.data.split(":", 1)[1])

    message = db_get_delivery(delivery_id, query.from_user.id)
    back = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="subs_registered")]])

    if not message:
        await query.edit_message_text("Details for that subscription aren't available.", reply_markup=back)
        return

    await query.edit_message_text(message, reply_markup=back)


async def menu_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    cart = context.user_data.setdefault("cart", {})

    if data.startswith("add:"):
        # Answer with the toast text FIRST — Telegram only honours the first
        # answerCallbackQuery per tap, so answering blankly up top would
        # silently swallow this popup.
        item_id = data.split(":", 1)[1]
        cart[item_id] = cart.get(item_id, 0) + 1
        await query.answer("✅ Added to the basket", show_alert=False)
        await query.edit_message_reply_markup(reply_markup=menu_keyboard())
        return

    await query.answer()

    if data.startswith("remove:"):
        item_id = data.split(":", 1)[1]
        if item_id in cart:
            cart[item_id] -= 1
            if cart[item_id] <= 0:
                del cart[item_id]
        await query.edit_message_text(
            format_cart(cart), parse_mode=ParseMode.MARKDOWN, reply_markup=cart_keyboard(cart)
        )
        return

    if data == "view_cart":
        await query.edit_message_text(
            format_cart(cart), parse_mode=ParseMode.MARKDOWN, reply_markup=cart_keyboard(cart)
        )
        return

    if data == "back_to_menu":
        await query.edit_message_text("Tap an item below to add it to your order.", reply_markup=menu_keyboard())
        return

    if data == "clear_cart":
        cart.clear()
        await query.edit_message_text("Basket cleared. Tap an item to start again.", reply_markup=menu_keyboard())
        return

    if data == "checkout":
        await start_checkout(update, context)
        return

    await query.edit_message_reply_markup(reply_markup=menu_keyboard())


def checkout_view(order_id: int):
    """Builds the (text, keyboard) for the main checkout screen. Reused so
    'back' buttons from sub-screens can rebuild the same view."""
    order = db_get_order(order_id)
    if not order:
        return "Order not found.", None

    _, user_id, username, items_json, total, status, created_at = order
    items = json.loads(items_json)
    lines = [f"{qty}x {MENU[i][0]} — {CURRENCY}{MENU[i][1] * qty:.2f}" for i, qty in items.items()]
    text = (
        f"*Order #{order_id} created*\n"
        f"Your Telegram ID: `{user_id}`\n\n"
        + "\n".join(lines)
        + f"\n\n*Total: {CURRENCY}{total:.2f}*\n\n"
        + PAYMENT_INSTRUCTIONS
    )

    buttons = [
        [InlineKeyboardButton("⭐ Pay with Telegram Stars", callback_data=f"pay_stars:{order_id}")],
        [InlineKeyboardButton("💳 Pay using Visa/Mastercard", url=PAYMENT_LINK_BASE_URL)],
        [InlineKeyboardButton("🌍 Pay using local payment methods", callback_data=f"local_pay:{order_id}")],
        [InlineKeyboardButton("₿ Pay with Cryptocurrency", callback_data=f"pay_crypto:{order_id}")],
        [InlineKeyboardButton("✅ I've Paid", callback_data=f"paid:{order_id}")],
        [InlineKeyboardButton("✖️ Cancel order", callback_data=f"cancel:{order_id}")],
    ]
    return text, InlineKeyboardMarkup(buttons)


async def start_checkout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    cart = context.user_data.get("cart", {})

    if not cart:
        await query.edit_message_text("Your cart is empty. Add something first!", reply_markup=menu_keyboard())
        return

    total = cart_total(cart)
    user = query.from_user
    order_id = db_create_order(user.id, user.username or user.first_name, cart, total)
    context.user_data["last_order_id"] = order_id

    text, keyboard = checkout_view(order_id)
    await query.edit_message_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard)

    # clear the cart now that the order has been placed
    context.user_data["cart"] = {}


async def local_pay_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Shows the country picker for local payment methods."""
    query = update.callback_query
    await query.answer()
    order_id = int(query.data.split(":", 1)[1])

    rows = []
    for i in range(0, len(LOCAL_PAYMENT_COUNTRIES), 2):
        pair = LOCAL_PAYMENT_COUNTRIES[i:i + 2]
        rows.append([
            InlineKeyboardButton(country, callback_data=f"local_country:{order_id}:{country}")
            for country in pair
        ])
    rows.append([InlineKeyboardButton("⬅️ Back", callback_data=f"back_to_checkout:{order_id}")])

    await query.edit_message_text(
        "Which country are you paying from?",
        reply_markup=InlineKeyboardMarkup(rows),
    )


async def local_country_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Shows local payment instructions for the chosen country."""
    query = update.callback_query
    await query.answer()
    _, order_id_str, country = query.data.split(":", 2)
    order_id = int(order_id_str)

    instructions = LOCAL_PAYMENT_INSTRUCTIONS.get(
        country, "Contact us directly for payment instructions in your country."
    )
    text = (
        f"*Payment instructions — {country}*\n\n"
        f"{instructions}\n\n"
        "After paying, tap *I've Paid* below."
    )
    buttons = [
        [InlineKeyboardButton("✅ I've Paid", callback_data=f"paid:{order_id}")],
        [InlineKeyboardButton("⬅️ Back", callback_data=f"local_pay:{order_id}")],
    ]
    await query.edit_message_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(buttons))

    # India includes a QR code image — send it as a follow-up photo if present.
    if country == "India" and os.path.exists(INDIA_QR_PATH):
        try:
            with open(INDIA_QR_PATH, "rb") as qr_file:
                await context.bot.send_photo(chat_id=query.message.chat_id, photo=qr_file)
        except Exception:
            logger.exception("Failed to send India QR code image")


async def pay_crypto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Shows crypto wallet addresses for payment."""
    query = update.callback_query
    await query.answer()
    order_id = int(query.data.split(":", 1)[1])

    buttons = [
        [InlineKeyboardButton("✅ I've Paid", callback_data=f"paid:{order_id}")],
        [InlineKeyboardButton("⬅️ Back", callback_data=f"back_to_checkout:{order_id}")],
    ]
    await query.edit_message_text(
        CRYPTO_INSTRUCTIONS, parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(buttons)
    )


async def back_to_checkout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    order_id = int(query.data.split(":", 1)[1])
    text, keyboard = checkout_view(order_id)
    await query.edit_message_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard)


async def pay_with_stars(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Sends a native Telegram Stars invoice for the given order."""
    query = update.callback_query
    await query.answer()
    order_id = int(query.data.split(":", 1)[1])
    order = db_get_order(order_id)

    if not order:
        await context.bot.send_message(chat_id=query.message.chat_id, text="Order not found.")
        return

    total = order[4]  # id, user_id, username, items_json, total, status, created_at
    star_amount = max(1, round(total * STAR_RATE))

    await context.bot.send_invoice(
        chat_id=query.message.chat_id,
        title=f"Order #{order_id}",
        description=f"Payment for order #{order_id}",
        payload=f"order_{order_id}",
        provider_token="",  # empty string is required for Telegram Stars (XTR)
        currency="XTR",
        prices=[LabeledPrice(label=f"Order #{order_id}", amount=star_amount)],
    )


async def precheckout_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Telegram calls this right before charging the user — final validation point."""
    pre_checkout_query = update.pre_checkout_query
    payload = pre_checkout_query.invoice_payload

    if not payload.startswith("order_"):
        await pre_checkout_query.answer(ok=False, error_message="Invalid order.")
        return

    order_id = int(payload.split("_", 1)[1])
    order = db_get_order(order_id)

    if not order:
        await pre_checkout_query.answer(ok=False, error_message="Order not found — please start over.")
        return

    await pre_checkout_query.answer(ok=True)


async def successful_payment_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Fires automatically once a Stars payment completes — no manual confirmation needed."""
    payload = update.message.successful_payment.invoice_payload
    order_id = int(payload.split("_", 1)[1])

    db_update_status(order_id, "paid")

    await update.message.reply_text(
        f"✅ Payment received via Telegram Stars for order #{order_id}! We're preparing it now.\n"
        f"Your Telegram ID: {update.effective_user.id} (keep this for any support requests)."
    )

    order = db_get_order(order_id)
    if order and ADMIN_CHAT_ID:
        _, user_id, username, items_json, total, status, created_at = order
        items = json.loads(items_json)
        lines = [f"{qty}x {MENU[i][0]}" for i, qty in items.items()]
        text = (
            f"⭐ Stars payment received — Order #{order_id}\n"
            f"From: @{username or user_id}\n\n"
            + "\n".join(lines)
            + f"\n\nTotal: {CURRENCY}{total:.2f}\n"
            "Paid automatically via Telegram Stars — no action needed."
        )
        await context.bot.send_message(chat_id=ADMIN_CHAT_ID, text=text)

        await start_order_fulfilment(context, order_id, user_id, items)


async def order_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    action, order_id_str = query.data.split(":", 1)
    order_id = int(order_id_str)
    order = db_get_order(order_id)

    if not order:
        await query.edit_message_text("Order not found.")
        return

    if action == "paid":
        # Don't notify the admin yet — first ask the customer for a receipt photo.
        db_update_status(order_id, "awaiting_receipt")
        context.user_data["awaiting_receipt_for_order"] = order_id
        await query.edit_message_text(
            f"Order #{order_id}: please upload a *photo* of your payment receipt "
            "(screenshot is fine) as your next message here.",
            parse_mode=ParseMode.MARKDOWN,
        )

    elif action == "cancel":
        db_update_status(order_id, "cancelled")
        await query.edit_message_text(f"Order #{order_id} cancelled.")


async def receipt_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles a photo sent by the customer after tapping 'I've Paid'."""
    order_id = context.user_data.get("awaiting_receipt_for_order")

    if not order_id:
        # Not expecting a receipt right now — ignore stray photos.
        return

    order = db_get_order(order_id)
    if not order:
        await update.message.reply_text("Order not found — please start over with /start.")
        context.user_data.pop("awaiting_receipt_for_order", None)
        return

    db_update_status(order_id, "awaiting_confirmation")
    context.user_data.pop("awaiting_receipt_for_order", None)

    await update.message.reply_text(
        f"Thanks! Receipt received for order #{order_id}. "
        "We're verifying it and will confirm shortly."
    )

    await notify_admin_receipt(context, order, update.message.photo[-1].file_id)


async def notify_admin_receipt(context: ContextTypes.DEFAULT_TYPE, order_row, photo_file_id: str):
    order_id, user_id, username, items_json, total, status, created_at = order_row
    items = json.loads(items_json)
    lines = [f"{qty}x {MENU[i][0]}" for i, qty in items.items()]
    # Plain text on purpose — usernames or item names can contain characters
    # (like _ or *) that break Telegram's Markdown parser and silently fail
    # to send. No parse_mode here avoids that entirely.
    caption = (
        f"🧾 Receipt received — Order #{order_id}\n"
        f"From: {username or 'unknown'} (ID: {user_id})\n\n"
        + "\n".join(lines)
        + f"\n\nTotal: {CURRENCY}{total:.2f}\n\n"
        "Check the receipt, then confirm or reject below."
    )
    if ADMIN_CHAT_ID:
        try:
            await context.bot.send_photo(
                chat_id=ADMIN_CHAT_ID,
                photo=photo_file_id,
                caption=caption,
                reply_markup=admin_review_keyboard(order_id),
            )
        except Exception:
            logger.exception("Failed to notify admin for order #%s", order_id)
    else:
        logger.warning("ADMIN_CHAT_ID not set — no admin notified for order #%s", order_id)


async def my_orders(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = db_user_orders(update.effective_user.id)
    if not rows:
        await update.message.reply_text("You have no orders yet.")
        return
    lines = []
    for order_id, items_json, total, status, created_at in rows:
        lines.append(f"#{order_id} — {CURRENCY}{total:.2f} — {status}")
    await update.message.reply_text("Your recent orders:\n" + "\n".join(lines))


# ------------------------------------------------------------------
# HANDLERS — admin side
# ------------------------------------------------------------------

async def admin_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query.from_user.id != ADMIN_CHAT_ID:
        await query.answer("Not authorized.", show_alert=True)
        return
    await query.answer()

    action, order_id_str = query.data.split(":", 1)
    order_id = int(order_id_str)
    order = db_get_order(order_id)
    if not order:
        await context.bot.send_message(chat_id=ADMIN_CHAT_ID, text="Order not found.")
        return

    _, user_id, username, items_json, total, status, created_at = order

    # The admin message here is a photo (caption), so edit the caption, not the text.
    if action == "admin_confirm":
        db_update_status(order_id, "paid")

        # Cosmetic only — a failure here (double tap, "message is not
        # modified", non-photo message) must never stop the order being
        # fulfilled, so it's isolated.
        try:
            await query.edit_message_caption(
                caption=f"✅ Order #{order_id} confirmed as paid."
            )
        except Exception:
            logger.info("Could not edit confirmation caption for order #%s", order_id)

        await context.bot.send_message(
            chat_id=user_id,
            text=(
                f"✅ Payment confirmed for order #{order_id}! We're preparing it now.\n"
                f"Your Telegram ID: {user_id} (keep this for any support requests)."
            ),
        )

        items = json.loads(items_json)
        await start_order_fulfilment(context, order_id, user_id, items)

    elif action == "admin_reject":
        db_update_status(order_id, "rejected")
        await query.edit_message_caption(caption=f"❌ Order #{order_id} rejected.")
        await context.bot.send_message(
            chat_id=user_id,
            text=(
                f"We couldn't verify the receipt for order #{order_id}. "
                "Please double check and send a clearer screenshot, "
                "or contact us directly."
            ),
        )


async def admin_menu_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles taps on the admin's persistent button panel."""
    if update.effective_user.id != ADMIN_CHAT_ID:
        return

    text = update.message.text

    if text == A_VIEW_SERIALS:
        await show_serials(update, context)

    elif text == A_ADD_SERIALS:
        await update.message.reply_text(
            "Which pool are these serials for?",
            reply_markup=InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton("6 Months", callback_data="addser:6m")],
                    [InlineKeyboardButton("1 Year", callback_data="addser:1y")],
                ]
            ),
        )

    elif text == A_REMOVE_SERIAL:
        context.user_data["awaiting_admin_input"] = "remove_serial"
        await update.message.reply_text("Send the serial code to remove:")

    elif text == A_RECENT_ORDERS:
        await customer_history(update, context)

    elif text == A_PENDING:
        await admin_pending_list(update, context)

    elif text == A_FIND_ORDER:
        context.user_data["awaiting_admin_input"] = "find_order"
        await update.message.reply_text("Send the order number:")

    elif text == A_FIND_CUSTOMER:
        context.user_data["awaiting_admin_input"] = "find_customer"
        await update.message.reply_text("Send the customer's Telegram ID or @username:")

    elif text == A_CUSTOMER_VIEW:
        await update.message.reply_text(
            "Switched to the customer menu. Send /start to return to the admin panel.",
            reply_markup=main_menu_keyboard(),
        )


def format_fulfilment_info(item_id: str, info_json: str) -> str:
    """Renders the details a customer submitted for one unit."""
    if not info_json:
        return "(no details collected)"
    try:
        info = json.loads(info_json)
    except (TypeError, ValueError):
        return "(details unreadable)"
    if not info:
        return "(no details collected)"

    labels = {
        "first_name": "First name", "last_name": "Last name", "email": "Email",
        "username": "Username", "password": "Password",
        "prev_username": "Previous username",
    }
    return "\n".join(f"{labels.get(k, k)}: {v}" for k, v in info.items())


async def admin_pending_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Every undelivered item across all customers, one button each."""
    if update.effective_user.id != ADMIN_CHAT_ID:
        return

    rows = db_all_pending_items()
    if not rows:
        await update.message.reply_text("No pending orders — everything is delivered.")
        return

    buttons = []
    for fid, order_id, user_id, username, item_id, unit_no, state in rows:
        name = MENU.get(item_id, (item_id,))[0]
        suffix = f" #{unit_no}" if unit_no > 1 else ""
        flag = "📝" if state == "awaiting_delivery" else "⌛"
        who = f"@{username}" if username else str(user_id)
        label = f"{flag} {name}{suffix} — {who} (#{order_id})"
        buttons.append([InlineKeyboardButton(label[:60], callback_data=f"apend:{fid}")])

    await update.message.reply_text(
        "Pending orders:\n📝 = details ready to deliver   ⌛ = waiting on customer",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def admin_pending_detail(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Shows the details a customer submitted, with a deliver button."""
    query = update.callback_query
    if query.from_user.id != ADMIN_CHAT_ID:
        await query.answer("Not authorized.", show_alert=True)
        return
    await query.answer()

    fulfilment_id = int(query.data.split(":", 1)[1])
    row = db_get_fulfilment(fulfilment_id)
    back = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="apend_back")]])

    if not row:
        await query.edit_message_text("That item no longer exists.", reply_markup=back)
        return

    _, order_id, user_id, item_id, unit_no, state, info_json = row
    name = MENU.get(item_id, (item_id,))[0]
    order = db_get_order(order_id)
    username = order[2] if order else None

    text = (
        f"{name}{f' #{unit_no}' if unit_no > 1 else ''}\n"
        f"Order #{order_id} — {f'@{username}' if username else ''} (ID: {user_id})\n"
        f"Status: {state}\n\n"
        f"{format_fulfilment_info(item_id, info_json)}"
    )

    buttons = []
    if state == "awaiting_delivery":
        buttons.append([InlineKeyboardButton("📤 Deliver to Customer", callback_data=f"deliver:{fulfilment_id}")])
    buttons.append([InlineKeyboardButton("⬅️ Back", callback_data="apend_back")])

    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(buttons))


async def admin_pending_back(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    rows = db_all_pending_items()
    if not rows:
        await query.edit_message_text("No pending orders — everything is delivered.")
        return

    buttons = []
    for fid, order_id, user_id, username, item_id, unit_no, state in rows:
        name = MENU.get(item_id, (item_id,))[0]
        suffix = f" #{unit_no}" if unit_no > 1 else ""
        flag = "📝" if state == "awaiting_delivery" else "⌛"
        who = f"@{username}" if username else str(user_id)
        buttons.append(
            [InlineKeyboardButton(f"{flag} {name}{suffix} — {who} (#{order_id})"[:60], callback_data=f"apend:{fid}")]
        )

    await query.edit_message_text(
        "Pending orders:\n📝 = details ready to deliver   ⌛ = waiting on customer",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def show_serials(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Lists the actual available serial codes in each pool, not just counts."""
    rows = db_list_serials(status="available")
    if not rows:
        await update.message.reply_text("No available serials in either pool.")
        return

    grouped = {"6m": [], "1y": []}
    for duration, code, status, used_for_order in rows:
        grouped.setdefault(duration, []).append(code)

    lines = []
    for duration, label in (("6m", "6 Months"), ("1y", "1 Year")):
        codes = grouped.get(duration, [])
        lines.append(f"\n🔑 {label} — {len(codes)} available")
        if codes:
            lines.extend(f"  {code}" for code in codes)
        else:
            lines.append("  (none)")

    text = "\n".join(lines).strip()
    for i in range(0, len(text), 3500):
        await update.message.reply_text(text[i:i + 3500])


async def add_serials_pick_duration(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin picked which pool to add serials to — now wait for the codes."""
    query = update.callback_query
    if query.from_user.id != ADMIN_CHAT_ID:
        await query.answer("Not authorized.", show_alert=True)
        return
    await query.answer()

    duration = query.data.split(":", 1)[1]
    context.user_data["awaiting_admin_input"] = "add_serials"
    context.user_data["add_serials_duration"] = duration
    label = "1 Year" if duration == "1y" else "6 Months"
    await query.edit_message_text(
        f"Send the {label} serial codes now — one per line, or separated by spaces. "
        "You can paste many at once."
    )


async def admin_input_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the admin's typed answer after tapping a panel button that
    needed input (add/remove serial, find order, find customer)."""
    mode = context.user_data.pop("awaiting_admin_input", None)
    if not mode:
        return
    text = update.message.text.strip()

    if mode == "add_serials":
        duration = context.user_data.pop("add_serials_duration", None)
        codes = text.split()
        added = db_add_serials(duration, codes)
        total = db_count_serials(duration)
        label = "1 Year" if duration == "1y" else "6 Months"
        await update.message.reply_text(
            f"Added {added} new {label} serial(s) (duplicates skipped).\nTotal available now: {total}"
        )

    elif mode == "remove_serial":
        conn = sqlite3.connect(DB_PATH)
        cur = conn.execute("DELETE FROM serials WHERE code = ?", (text,))
        conn.commit()
        removed = cur.rowcount
        conn.close()
        await update.message.reply_text(
            f"Removed serial {text} from the pool." if removed else f"No serial matching {text} found."
        )

    elif mode == "find_order":
        context.args = [text]
        await order_lookup(update, context)

    elif mode == "find_customer":
        context.args = [text]
        await customer_lookup(update, context)


async def text_state_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Single entry point for all free-text messages that aren't the main
    menu buttons. Dispatches based on what state the sender is in, so only
    one handler ever needs to be registered for generic text — registering
    two separate broad text handlers would cause them to silently steal
    each other's messages."""
    if context.user_data.get("awaiting_registration_field"):
        await registration_field_reply(update, context)
        return

    if context.user_data.get("awaiting_generic_field"):
        await generic_field_reply(update, context)
        return

    if update.effective_user.id == ADMIN_CHAT_ID:
        # Panel-button follow-ups take priority — they're the most recent
        # thing the admin explicitly asked to do.
        if context.user_data.get("awaiting_admin_input"):
            await admin_input_reply(update, context)
            return
        if context.user_data.get("awaiting_credentials_fulfilment"):
            await credentials_reply(update, context)
            return


async def start_order_fulfilment(context: ContextTypes.DEFAULT_TYPE, order_id: int, user_id: int, items: dict):
    """Records every product in the paid order in the database, then starts
    collecting details for the first one."""
    db_add_fulfilment_items(order_id, user_id, items)
    await process_next_in_queue(context, user_id, order_id)


async def process_next_in_queue(context: ContextTypes.DEFAULT_TYPE, user_id: int, order_id: int = None):
    """Starts collection for the next product still needing details.

    Reads the queue from the database rather than memory, so a restart or
    redeploy can't lose track of a half-finished order. If no order_id is
    given, picks up whichever of this customer's orders still needs
    something."""
    if order_id is None:
        pending = db_user_pending_items(user_id)
        needs = [row for row in pending if row[4] == "needs_info"]
        if not needs:
            return
        order_id = needs[0][1]

    nxt = db_next_needs_info(order_id)
    if not nxt:
        return
    fulfilment_id, item_id = nxt

    item_name = MENU.get(item_id, (item_id, 0))[0]
    customer_data = context.application.user_data[user_id]

    if item_id in IMD_TRIGGER_ITEMS:
        await start_imd_collection(context, order_id, user_id, item_id, fulfilment_id)
    else:
        customer_data["generic_fulfilment_id"] = fulfilment_id
        customer_data["generic_item_id"] = item_id
        customer_data["generic_order_id"] = order_id
        customer_data["generic_data"] = {}
        customer_data["awaiting_generic_field"] = GENERIC_FIELDS[0][0]
        await context.bot.send_message(
            chat_id=user_id,
            text=(
                f"📝 Now let's set up your *{item_name}*.\n\n"
                f"Please provide the following:\n{GENERIC_RULES_TEXT}\n\n"
                f"{GENERIC_FIELDS[0][1]}"
            ),
            parse_mode=ParseMode.MARKDOWN,
        )


async def generic_field_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Collects first name, last name, email, username and password for a
    non-iMD subscription, validating the username and password rules and
    re-asking until they're satisfied."""
    field = context.user_data.get("awaiting_generic_field")
    if not field:
        return

    value = update.message.text.strip()

    if field == "username":
        error = validate_generic_username(value)
        if error:
            await update.message.reply_text(error)
            return
    elif field == "password":
        error = validate_generic_password(value)
        if error:
            await update.message.reply_text(error)
            return

    data = context.user_data.setdefault("generic_data", {})
    data[field] = value

    field_names = [f[0] for f in GENERIC_FIELDS]
    idx = field_names.index(field)

    if idx + 1 < len(GENERIC_FIELDS):
        next_field, next_prompt = GENERIC_FIELDS[idx + 1]
        context.user_data["awaiting_generic_field"] = next_field
        await update.message.reply_text(next_prompt)
        return

    # All fields collected — hand off to the admin for manual fulfilment.
    order_id = context.user_data.pop("generic_order_id", None)
    item_id = context.user_data.pop("generic_item_id", None)
    fulfilment_id = context.user_data.pop("generic_fulfilment_id", None)
    context.user_data.pop("awaiting_generic_field", None)
    context.user_data.pop("generic_data", None)

    await update.message.reply_text(DELIVERY_48H_MESSAGE)

    if fulfilment_id:
        db_set_fulfilment_info(fulfilment_id, data)
        db_set_fulfilment_state(fulfilment_id, "awaiting_delivery")

    if ADMIN_CHAT_ID and order_id:
        item_name = MENU.get(item_id, (item_id, 0))[0]
        await context.bot.send_message(
            chat_id=ADMIN_CHAT_ID,
            text=(
                f"📝 Manual fulfilment needed — Order #{order_id}\n"
                f"Product: {item_name}\n\n"
                f"First name: {data.get('first_name')}\n"
                f"Last name: {data.get('last_name')}\n"
                f"Email: {data.get('email')}\n"
                f"Username: {data.get('username')}\n"
                f"Password: {data.get('password')}\n\n"
                "Deliver within 48 hours using the 📤 Send Credentials button on this order."
            ),
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("📤 Send Credentials", callback_data=f"deliver:{fulfilment_id}")]]
            ),
        )

    # Move on to the next item in this order, if any.
    await process_next_in_queue(context, update.effective_user.id, order_id)


async def start_imd_collection(context: ContextTypes.DEFAULT_TYPE, order_id: int, user_id: int, imd_item: str, fulfilment_id: int):
    """Kicks off the customer-facing form collection for an iMD item —
    email/username/password for a new account, or just the previous
    username for a renewal."""
    is_renew = imd_item in IMD_RENEW_ITEMS
    duration = IMD_DURATION_MAP[imd_item]

    # context.application.user_data lets us reach into the CUSTOMER's data
    # store from here, even though this often runs in the admin's context.
    customer_data = context.application.user_data[user_id]
    customer_data["registration_order"] = order_id
    customer_data["registration_data"] = {}
    customer_data["registration_is_renew"] = is_renew
    customer_data["registration_duration"] = duration
    customer_data["registration_item_id"] = imd_item
    customer_data["registration_fulfilment_id"] = fulfilment_id

    if is_renew:
        customer_data["awaiting_registration_field"] = "prev_username"
        text = "🎓 Let's renew your iMD account.\n\nReply with your previous username:"
    else:
        customer_data["awaiting_registration_field"] = "email"
        text = "🎓 Let's set up your iMD account. Please fill this form to register:\n\nReply with your email address:"

    await context.bot.send_message(chat_id=user_id, text=text)


async def registration_field_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Collects the iMD form fields from the customer, one message at a
    time — either email -> username -> password (new account) or just
    previous username (renewal)."""
    field = context.user_data.get("awaiting_registration_field")
    if not field:
        return  # not in the middle of registration

    is_renew = context.user_data.get("registration_is_renew", False)
    value = update.message.text.strip()
    data = context.user_data.setdefault("registration_data", {})
    data[field] = value

    # Retry mode: iMD rejected one specific field, so we only re-collect
    # that field and go straight back to the admin — no need to make the
    # customer re-enter everything.
    if context.user_data.get("registration_retry_field") == field:
        context.user_data.pop("registration_retry_field", None)
        await finish_imd_collection(update, context, data)
        return

    if is_renew:
        # Only one field to collect for a renewal.
        if field == "prev_username":
            await finish_imd_collection(update, context, data)
        return

    # New account: three fields in sequence.
    if field == "email":
        context.user_data["awaiting_registration_field"] = "username"
        await update.message.reply_text("Desired username:")
        return

    if field == "username":
        context.user_data["awaiting_registration_field"] = "password"
        await update.message.reply_text("Desired password:")
        return

    if field == "password":
        await finish_imd_collection(update, context, data)


async def finish_imd_collection(update: Update, context: ContextTypes.DEFAULT_TYPE, data: dict):
    """Common finish step for both the new-account and renewal collection
    flows. Runs the registration/renewal immediately — no admin tap
    needed — and just reports the outcome to the admin afterward."""
    order_id = context.user_data.pop("registration_order", None)
    is_renew = context.user_data.pop("registration_is_renew", False)
    duration = context.user_data.pop("registration_duration", None)
    item_id = context.user_data.pop("registration_item_id", None)
    fulfilment_id = context.user_data.pop("registration_fulfilment_id", None)
    context.user_data.pop("awaiting_registration_field", None)
    context.user_data.pop("registration_data", None)

    await update.message.reply_text(
        "Thanks! We're setting up your account now — you'll get your login details here shortly."
    )

    if not order_id:
        return

    context.application.bot_data.setdefault("pending_registrations", {})[order_id] = {
        **data,
        "is_renew": is_renew,
        "duration": duration,
        "item_id": item_id,
        "fulfilment_id": fulfilment_id,
    }

    if ADMIN_CHAT_ID:
        duration_label = "1 Year" if duration == "1y" else "6 Months"
        if is_renew:
            summary = f"Previous username: {data.get('prev_username')}"
        else:
            summary = (
                f"Email: {data.get('email')}\n"
                f"Username: {data.get('username')}\n"
                f"Password: {data.get('password')}"
            )
        await context.bot.send_message(
            chat_id=ADMIN_CHAT_ID,
            text=(
                f"🎓 iMD {'renewal' if is_renew else 'registration'} — Order #{order_id}\n"
                f"Duration: {duration_label}\n\n"
                f"{summary}\n\nRunning automatically..."
            ),
        )

    await run_imd_registration(context, order_id)


async def reg_go(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin tapped the retry button on a previous failure."""
    query = update.callback_query
    if query.from_user.id != ADMIN_CHAT_ID:
        await query.answer("Not authorized.", show_alert=True)
        return
    await query.answer()

    order_id = int(query.data.split(":", 1)[1])
    await run_imd_registration(context, order_id)


async def run_imd_registration(context: ContextTypes.DEFAULT_TYPE, order_id: int):
    """Pulls a serial from the pool matching this order's duration and
    performs the registration or renewal, then handles the outcome."""
    pending = context.application.bot_data.get("pending_registrations", {})
    data = pending.get(order_id)

    if not data:
        if ADMIN_CHAT_ID:
            await context.bot.send_message(
                chat_id=ADMIN_CHAT_ID, text=f"No pending registration data found for order #{order_id}."
            )
        return

    duration = data.get("duration")
    is_renew = data.get("is_renew")

    popped = db_pop_serial(duration)
    if not popped:
        duration_label = "1 Year" if duration == "1y" else "6 Months"
        if ADMIN_CHAT_ID:
            await context.bot.send_message(
                chat_id=ADMIN_CHAT_ID,
                text=(
                    f"⚠️ No available {duration_label} serials in stock for order #{order_id}.\n"
                    f"Add some via ➕ Add Serials, then tap Retry below."
                ),
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("🔁 Retry", callback_data=f"reg_go:{order_id}")]]
                ),
            )
        return

    serial_id, serial_code = popped

    if is_renew:
        status, detail, page_text = await attempt_imd_action(
            IMD_RENEW_URL, IMD_RENEW_FIELD_MAP, {"username": data.get("prev_username"), "serial": serial_code}
        )
    else:
        status, detail, page_text = await attempt_imd_action(
            IMD_REGISTER_URL,
            IMD_REGISTER_FIELD_MAP,
            {
                "username": data.get("username"),
                "password": data.get("password"),
                "verify_password": data.get("password"),
                "email": data.get("email"),
                "serial": serial_code,
            },
        )

    order = db_get_order(order_id)
    customer_user_id = order[1] if order else None
    action_label = "Renewal" if is_renew else "Registration"
    retry_button = InlineKeyboardMarkup(
        [[InlineKeyboardButton(f"🔁 Retry {action_label}", callback_data=f"reg_go:{order_id}")]]
    )

    # ---- Confirmed success: deliver to the customer, consume the serial.
    if status == "success":
        pending.pop(order_id, None)
        db_finalize_serial(serial_id, order_id, success=True)

        if is_renew:
            # iMD's extension confirmation includes the new expiry date —
            # show that to the customer rather than today's date.
            valid_until = extract_valid_until(page_text)
            delivery_message = build_imd_delivery_message(
                data.get("prev_username"), password=None, date_str=valid_until, duration=duration
            )
        else:
            delivery_message = build_imd_delivery_message(
                data.get("username"), data.get("password"), duration=duration
            )

        db_save_delivery(order_id, delivery_message)
        db_add_delivery(order_id, customer_user_id, data.get("item_id"), delivery_message)
        await context.bot.send_message(
            chat_id=ADMIN_CHAT_ID,
            text=f"✅ {action_label} confirmed for order #{order_id}. Serial used: {serial_code}\n\n{detail}",
        )
        if customer_user_id:
            await context.bot.send_message(chat_id=customer_user_id, text=delivery_message)
            if data.get("fulfilment_id"):
                db_set_fulfilment_state(data["fulfilment_id"], "delivered")
            # This order may contain more items — move on to the next one.
            await process_next_in_queue(context, customer_user_id, order_id)
        return

    # Everything below is a non-success: the serial goes back to the pool
    # so it isn't lost, and the customer is NOT sent any credentials.
    db_finalize_serial(serial_id, order_id, success=False)

    # ---- Serial problem: admin's to fix, customer isn't involved.
    if status == "serial_error":
        await context.bot.send_message(
            chat_id=ADMIN_CHAT_ID,
            text=(
                f"🔴 SERIAL PROBLEM — Order #{order_id} ({action_label})\n\n"
                f"Serial `{serial_code}` was rejected by iMD and has been returned to the pool.\n"
                f"Remove it with /removeserial {serial_code}, add a working one with "
                f"/addserials {duration} <code>, then retry.\n\n{detail}"
            ),
            reply_markup=retry_button,
        )
        return

    # ---- Username problem: ask the customer to supply a different one.
    if status == "username_error":
        if is_renew:
            admin_note = (
                f"🟠 USERNAME PROBLEM — Order #{order_id} (Renewal)\n\n"
                f"iMD didn't accept the username '{data.get('prev_username')}'.\n"
                f"The customer has been asked to send their correct username. "
                f"Serial {serial_code} returned to the pool.\n\n{detail}"
            )
            customer_note = (
                "⚠️ We couldn't find an iMD account with that username.\n\n"
                "Please reply with your correct previous username:"
            )
            retry_field = "prev_username"
        else:
            admin_note = (
                f"🟠 USERNAME PROBLEM — Order #{order_id} (Registration)\n\n"
                f"iMD didn't accept the username '{data.get('username')}' (likely already taken).\n"
                f"The customer has been asked to choose another. "
                f"Serial {serial_code} returned to the pool.\n\n{detail}"
            )
            customer_note = (
                "⚠️ That username isn't available on iMD.\n\n"
                "Please reply with a different desired username:"
            )
            retry_field = "username"

        await context.bot.send_message(chat_id=ADMIN_CHAT_ID, text=admin_note)
        if customer_user_id:
            customer_data = context.application.user_data[customer_user_id]
            customer_data["registration_order"] = order_id
            customer_data["registration_is_renew"] = is_renew
            customer_data["registration_duration"] = duration
            customer_data["registration_data"] = {k: v for k, v in data.items() if k not in ("is_renew", "duration")}
            customer_data["registration_retry_field"] = retry_field
            customer_data["awaiting_registration_field"] = retry_field
            await context.bot.send_message(chat_id=customer_user_id, text=customer_note)
        return

    # ---- Email problem: ask the customer for a different address.
    if status == "email_error":
        await context.bot.send_message(
            chat_id=ADMIN_CHAT_ID,
            text=(
                f"🟠 EMAIL PROBLEM — Order #{order_id} (Registration)\n\n"
                f"iMD didn't accept the email '{data.get('email')}'.\n"
                f"The customer has been asked for another. "
                f"Serial {serial_code} returned to the pool.\n\n{detail}"
            ),
        )
        if customer_user_id:
            customer_data = context.application.user_data[customer_user_id]
            customer_data["registration_order"] = order_id
            customer_data["registration_is_renew"] = is_renew
            customer_data["registration_duration"] = duration
            customer_data["registration_data"] = {k: v for k, v in data.items() if k not in ("is_renew", "duration")}
            customer_data["registration_retry_field"] = "email"
            customer_data["awaiting_registration_field"] = "email"
            await context.bot.send_message(
                chat_id=customer_user_id,
                text="⚠️ That email address wasn't accepted by iMD.\n\nPlease reply with a different email address:",
            )
        return

    # ---- Unknown / automation error: never assume success.
    await context.bot.send_message(
        chat_id=ADMIN_CHAT_ID,
        text=(
            f"⚠️ COULD NOT CONFIRM — Order #{order_id} ({action_label})\n\n"
            f"The result page didn't clearly say whether this worked, so nothing was sent to the customer "
            f"and serial {serial_code} was returned to the pool.\n\n"
            f"Please check iMD manually. If it DID go through, deliver with the 📤 Send Credentials button "
            f"on the order's payment message.\n\n{detail}"
        ),
        reply_markup=retry_button,
    )


def classify_imd_result(page_text: str) -> str:
    """Reads the post-submit page text and decides what happened.
    Returns one of: 'success', 'serial_error', 'username_error',
    'email_error', 'unknown'.

    'unknown' is the safe default — the caller must NOT treat it as
    success, because reporting an unconfirmed result as success is
    exactly what let a bad serial through as a completed renewal."""
    lowered = page_text.lower()

    # Check errors before success: an error page can still contain
    # incidental words like "register" that would false-positive.
    for keyword in IMD_SERIAL_ERROR_KEYWORDS:
        if keyword in lowered:
            return "serial_error"
    for keyword in IMD_USERNAME_ERROR_KEYWORDS:
        if keyword in lowered:
            return "username_error"
    for keyword in IMD_EMAIL_ERROR_KEYWORDS:
        if keyword in lowered:
            return "email_error"
    for keyword in IMD_SUCCESS_KEYWORDS:
        if keyword in lowered:
            return "success"
    return "unknown"


async def attempt_imd_action(url: str, field_map: dict, values: dict):
    """Automatic form submission using a real headless Chromium browser
    (via Playwright), since plain HTTP requests are blocked by Cloudflare.
    Shared by both registration (register/index.php) and renewal
    (ess.php) — the only difference is the URL, field map, and values.

    Waits for any Cloudflare interstitial to clear AFTER submitting so we
    can read the real result page, then classifies the outcome. Returns
    (status: str, detail: str, page_text: str) where status is one of
    'success', 'serial_error', 'username_error', 'email_error',
    'unknown', or 'error' (the automation itself failed). page_text is
    the visible text of the result page, used to pull details like the
    new expiry date out of a successful extension."""
    from playwright.async_api import async_playwright

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
            )
            context = await browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
                ),
                viewport={"width": 1280, "height": 800},
            )
            page = await context.new_page()

            await page.goto(url, wait_until="networkidle", timeout=30000)

            for _ in range(6):
                if "Just a moment" not in await page.title():
                    break
                await page.wait_for_timeout(3000)

            title = await page.title()
            if "Just a moment" in title:
                content = await page.content()
                await browser.close()
                return "error", f"Still blocked by Cloudflare before the form loaded. Title: {title}\n{content[:500]}", ""

            for key, field_name in field_map.items():
                if key in values and values[key] is not None:
                    await page.fill(f'input[name="{field_name}"]', values[key])

            await page.click("#submit")
            await page.wait_for_load_state("networkidle", timeout=30000)

            # Wait out the post-submit Cloudflare interstitial — up to ~45s.
            # We must reach the real result page; classifying the challenge
            # page itself tells us nothing about whether it worked.
            for _ in range(15):
                if "Just a moment" not in await page.title():
                    break
                await page.wait_for_timeout(3000)

            result_title = await page.title()
            if "Just a moment" in result_title:
                content = await page.content()
                await browser.close()
                return "unknown", (
                    "Could not read the result — still on a Cloudflare challenge page after waiting 45s. "
                    "The action may or may not have gone through; please verify manually.\n"
                    f"Title: {result_title}\n{content[:400]}"
                ), ""

            body_text = await page.inner_text("body")
            content = await page.content()
            await browser.close()

            status = classify_imd_result(body_text)
            snippet = body_text[:600].replace("\n", " ").strip() or content[:600]
            return status, f"Page title: {result_title}\nPage said:\n{snippet}", body_text

    except Exception as exc:
        return "error", f"Browser automation failed: {exc}", ""


def extract_valid_until(page_text: str):
    """Pulls the expiry date out of iMD's extension success message
    ('Account is extended successfully and valid for: <date>').
    Returns the date string, or None if it isn't found."""
    match = re.search(r"valid\s+for\s*:?\s*(.+)", page_text, re.IGNORECASE)
    if not match:
        return None
    # Take just the first line and trim trailing punctuation/whitespace.
    value = match.group(1).splitlines()[0].strip().rstrip(".").strip()
    return value or None


def build_imd_delivery_message(username: str, password: str = None, date_str: str = None, duration: str = None) -> str:
    """The exact account-delivery message format used for iMD orders.
    Omits the password line for renewals (password left as None).
    `date_str` overrides the date shown — used for extensions, where iMD
    tells us the actual new expiry date, which is more useful to the
    customer than today's date."""
    if not date_str:
        date_str = datetime.now().strftime("%Y-%m-%d")
    duration_label = "6 months" if duration == "6m" else "1 year"
    password_line = f"Password: {password}\n" if password else ""
    return (
        f"✅ IMD {duration_label} Full Access \n\n"
        f"Date: {date_str}\n\n"
        f"Username: {username}\n"
        f"{password_line}\n"
        "⭕️ This account is restricted to simultaneous use on a single device. "
        "Device changes are permitted; however, logging in on a new device will "
        "automatically terminate the session on the previous one.\n\n"
        "♦️ This account is designated for exclusive use by one individual. "
        "Please ensure your password is not shared with others. Unauthorized "
        "sharing or misuse may result in account suspension.\n\n"
        "🛑 Installation Guide:\n\n"
        "🌐 Web Version for all platforms: \n"
        "www.imdweb.org\n\n\n"
        "📱App version for Android:\n"
        "sg.imedicaldoctor.net/imd200.apk"
    )


async def deliver_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin tapped '📤 Send Credentials'. The callback carries the specific
    fulfilment row (one purchased unit), so two identical products in the
    same order stay independent."""
    query = update.callback_query
    if query.from_user.id != ADMIN_CHAT_ID:
        await query.answer("Not authorized.", show_alert=True)
        return
    await query.answer()

    fulfilment_id = int(query.data.split(":", 1)[1])
    row = db_get_fulfilment(fulfilment_id)
    if not row:
        await context.bot.send_message(chat_id=ADMIN_CHAT_ID, text="That item no longer exists.")
        return

    _, order_id, user_id, item_id, unit_no, state, info_json = row
    context.user_data["awaiting_credentials_fulfilment"] = fulfilment_id

    name = MENU.get(item_id, (item_id,))[0]
    await context.bot.send_message(
        chat_id=ADMIN_CHAT_ID,
        text=f"Reply with the account details for {name} (Order #{order_id}).",
    )


async def credentials_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Catches the admin's next text message after tapping 'Send Credentials'
    and forwards it to the customer as their account details."""
    if update.effective_user.id != ADMIN_CHAT_ID:
        return

    fulfilment_id = context.user_data.pop("awaiting_credentials_fulfilment", None)
    if not fulfilment_id:
        return  # admin isn't in the middle of delivering anything

    row = db_get_fulfilment(fulfilment_id)
    if not row:
        await update.message.reply_text("That item no longer exists.")
        return

    _, order_id, user_id, item_id, unit_no, state, info_json = row
    credentials_text = update.message.text
    name = MENU.get(item_id, (item_id,))[0]

    await context.bot.send_message(
        chat_id=user_id,
        text=f"🔑 Here are your account details for {name} (Order #{order_id}):\n\n{credentials_text}",
    )
    db_save_delivery(order_id, credentials_text)
    db_add_delivery(order_id, user_id, item_id, credentials_text)
    db_set_fulfilment_state(fulfilment_id, "delivered")
    await update.message.reply_text(f"✅ {name} delivered to the customer (Order #{order_id}).")

    # A manual delivery ends whatever the customer was mid-way through for
    # this unit, so clear any stale collection state and move the order on —
    # otherwise the remaining items are never asked for.
    customer_data = context.application.user_data[user_id]
    for key in (
        "awaiting_registration_field", "registration_order", "registration_data",
        "registration_is_renew", "registration_duration", "registration_item_id",
        "registration_fulfilment_id", "registration_retry_field",
        "awaiting_generic_field", "generic_data", "generic_order_id",
        "generic_item_id", "generic_fulfilment_id",
    ):
        customer_data.pop(key, None)
    await process_next_in_queue(context, user_id, order_id)


async def add_serials_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin-only: /addserials <6m|1y> <code1> <code2> ... — bulk-adds
    serials to the auto-assignment pool. Codes can be separated by spaces
    or pasted on separate lines in the same message."""
    if update.effective_user.id != ADMIN_CHAT_ID:
        return

    if len(context.args) < 2:
        await update.message.reply_text(
            "Usage: /addserials <6m|1y> <code1> <code2> ...\n"
            "You can paste many codes at once, one per line or space-separated."
        )
        return

    duration_arg = context.args[0].lower()
    if duration_arg in ("6m", "6month", "6months"):
        duration = "6m"
    elif duration_arg in ("1y", "1year", "12m"):
        duration = "1y"
    else:
        await update.message.reply_text("Duration must be '6m' or '1y'.")
        return

    codes = context.args[1:]
    added = db_add_serials(duration, codes)
    total_available = db_count_serials(duration)
    duration_label = "1 Year" if duration == "1y" else "6 Months"
    await update.message.reply_text(
        f"Added {added} new {duration_label} serial(s) (duplicates skipped).\n"
        f"Total available now: {total_available}"
    )


async def remove_serial_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin-only: /removeserial <code> — deletes a bad serial from the
    pool so it never gets auto-assigned again."""
    if update.effective_user.id != ADMIN_CHAT_ID:
        return

    if not context.args:
        await update.message.reply_text("Usage: /removeserial <code>")
        return

    code = context.args[0]
    conn = sqlite3.connect(DB_PATH)
    cur = conn.execute("DELETE FROM serials WHERE code = ?", (code,))
    conn.commit()
    removed = cur.rowcount
    conn.close()

    if removed:
        await update.message.reply_text(f"Removed serial {code} from the pool.")
    else:
        await update.message.reply_text(f"No serial matching {code} found.")


async def serials_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin-only: /serials — shows how many serials are left in each pool."""
    if update.effective_user.id != ADMIN_CHAT_ID:
        return

    six_month = db_count_serials("6m")
    one_year = db_count_serials("1y")
    await update.message.reply_text(
        f"Available serials:\n6 Months: {six_month}\n1 Year: {one_year}"
    )


async def customer_lookup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin-only: full picture for one customer — delivered and pending
    shown separately, each with the details they submitted."""
    if update.effective_user.id != ADMIN_CHAT_ID:
        return

    if not context.args:
        await update.message.reply_text("Usage: /customer <user_id or @username>")
        return

    query_arg = context.args[0]
    conn = sqlite3.connect(DB_PATH)
    if query_arg.startswith("@"):
        row = conn.execute(
            "SELECT user_id FROM orders WHERE username = ? ORDER BY id DESC LIMIT 1",
            (query_arg[1:],),
        ).fetchone()
        conn.close()
        if not row:
            await update.message.reply_text(f"No orders found for {query_arg}.")
            return
        user_id = row[0]
    else:
        conn.close()
        try:
            user_id = int(query_arg)
        except ValueError:
            await update.message.reply_text("Provide a numeric Telegram user ID or @username.")
            return

    delivered = db_user_delivered_units(user_id)
    pending = db_user_pending_items(user_id)

    if not delivered and not pending:
        await update.message.reply_text(f"No orders found for {query_arg}.")
        return

    parts = [f"Customer {query_arg} (ID: {user_id})"]

    parts.append("\n✅ DELIVERED")
    if delivered:
        for fid, order_id, item_id, unit_no, info_json in delivered:
            name = MENU.get(item_id, (item_id,))[0]
            suffix = f" #{unit_no}" if unit_no > 1 else ""
            parts.append(f"\n• {name}{suffix} — Order #{order_id}")
            parts.append(format_fulfilment_info(item_id, info_json))
    else:
        parts.append("(none)")

    parts.append("\n⏳ PENDING")
    if pending:
        for fid, order_id, item_id, unit_no, state in pending:
            name = MENU.get(item_id, (item_id,))[0]
            suffix = f" #{unit_no}" if unit_no > 1 else ""
            label = "details ready to deliver" if state == "awaiting_delivery" else "waiting on customer"
            parts.append(f"\n• {name}{suffix} — Order #{order_id} ({label})")
            row = db_get_fulfilment(fid)
            parts.append(format_fulfilment_info(item_id, row[6] if row else None))
    else:
        parts.append("(none)")

    text = "\n".join(parts)
    for i in range(0, len(text), 3500):
        await update.message.reply_text(text[i:i + 3500])


async def customer_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin-only: overview of recent orders and their delivery status."""
    if update.effective_user.id != ADMIN_CHAT_ID:
        return

    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT id, user_id, username, items_json, total, status, delivered_at "
        "FROM orders ORDER BY id DESC LIMIT 30"
    ).fetchall()
    conn.close()

    if not rows:
        await update.message.reply_text("No orders yet.")
        return

    lines = []
    for order_id, user_id, username, items_json, total, status, delivered_at in rows:
        items = json.loads(items_json)
        item_names = ", ".join(MENU[i][0] for i in items if i in MENU)
        line = f"#{order_id} {username or 'unknown'} (ID: {user_id}) — {item_names} — {status}"
        if delivered_at:
            line += f" (delivered {delivered_at[:10]})"
        lines.append(line)

    # Telegram caps messages at 4096 chars — chunk if the list gets long.
    text = "\n".join(lines)
    for i in range(0, len(text), 3500):
        await update.message.reply_text(text[i:i + 3500])


async def order_lookup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin-only: full detail for one order, including delivered credentials."""
    if update.effective_user.id != ADMIN_CHAT_ID:
        return

    if not context.args:
        await update.message.reply_text("Usage: /order <order_id>")
        return

    try:
        order_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Order id must be a number.")
        return

    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT id, user_id, username, items_json, total, status, created_at, "
        "credentials, delivered_at FROM orders WHERE id = ?",
        (order_id,),
    ).fetchone()
    conn.close()

    if not row:
        await update.message.reply_text(f"Order #{order_id} not found.")
        return

    oid, user_id, username, items_json, total, status, created_at, credentials, delivered_at = row
    items = json.loads(items_json)
    item_names = ", ".join(MENU[i][0] for i in items if i in MENU)

    text = (
        f"Order #{oid}\n"
        f"Customer: {username or 'unknown'} (ID: {user_id})\n"
        f"Items: {item_names}\n"
        f"Total: {CURRENCY}{total:.2f}\n"
        f"Status: {status}\n"
        f"Created: {created_at[:19]}\n"
    )
    if delivered_at:
        text += f"Delivered: {delivered_at[:19]}\n\nCredentials sent:\n{credentials}"
    else:
        text += "\nNo credentials delivered yet."

    await update.message.reply_text(text)


# ------------------------------------------------------------------
# MAIN
# ------------------------------------------------------------------

def main():
    db_init()

    if BOT_TOKEN == "PUT_YOUR_BOT_TOKEN_HERE":
        raise SystemExit("Set BOT_TOKEN (env var) before running.")

    # Persist conversation state to disk. Without this, an in-progress
    # collection (and anything else in user_data) is wiped by every
    # restart or redeploy, silently stranding half-finished orders.
    persistence = PicklePersistence(
        filepath=os.path.join(os.path.dirname(DB_PATH) or ".", "bot_state.pickle"),
        store_data=PersistenceInput(bot_data=True, chat_data=True, user_data=True, callback_data=False),
    )

    app = Application.builder().token(BOT_TOKEN).persistence(persistence).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("myorders", my_orders))
    app.add_handler(CommandHandler("customers", customer_history))
    app.add_handler(CommandHandler("customer", customer_lookup))
    app.add_handler(CommandHandler("order", order_lookup))
    app.add_handler(CommandHandler("addserials", add_serials_command))
    app.add_handler(CommandHandler("serials", serials_status))
    app.add_handler(CommandHandler("listserials", show_serials))
    app.add_handler(CommandHandler("removeserial", remove_serial_command))
    app.add_handler(CallbackQueryHandler(order_action, pattern=r"^(paid|cancel):"))
    app.add_handler(CallbackQueryHandler(admin_action, pattern=r"^admin_(confirm|reject):"))
    app.add_handler(CallbackQueryHandler(deliver_start, pattern=r"^deliver:"))
    app.add_handler(CallbackQueryHandler(reg_go, pattern=r"^reg_go:"))
    app.add_handler(CallbackQueryHandler(add_serials_pick_duration, pattern=r"^addser:"))
    app.add_handler(CallbackQueryHandler(imd_menu_start, pattern=r"^imd_menu$"))
    app.add_handler(CallbackQueryHandler(imd_type_selected, pattern=r"^imd_type:"))
    app.add_handler(CallbackQueryHandler(pay_with_stars, pattern=r"^pay_stars:"))
    app.add_handler(CallbackQueryHandler(local_pay_start, pattern=r"^local_pay:"))
    app.add_handler(CallbackQueryHandler(local_country_selected, pattern=r"^local_country:"))
    app.add_handler(CallbackQueryHandler(pay_crypto, pattern=r"^pay_crypto:"))
    app.add_handler(CallbackQueryHandler(back_to_checkout, pattern=r"^back_to_checkout:"))
    app.add_handler(CallbackQueryHandler(my_subscription_detail, pattern=r"^mysub:"))
    app.add_handler(CallbackQueryHandler(subs_menu, pattern=r"^subs_menu$"))
    app.add_handler(CallbackQueryHandler(subs_registered, pattern=r"^subs_registered$"))
    app.add_handler(CallbackQueryHandler(subs_pending, pattern=r"^subs_pending$"))
    app.add_handler(CallbackQueryHandler(pending_detail, pattern=r"^pend:"))
    app.add_handler(CallbackQueryHandler(admin_pending_detail, pattern=r"^apend:"))
    app.add_handler(CallbackQueryHandler(admin_pending_back, pattern=r"^apend_back$"))
    app.add_handler(PreCheckoutQueryHandler(precheckout_callback))
    app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment_callback))
    app.add_handler(MessageHandler(filters.PHOTO, receipt_photo))
    app.add_handler(MessageHandler(filters.Text([BUY_LABEL, MY_SUBS_LABEL, BASKET_LABEL, SUPPORT_LABEL]), main_menu_text))
    app.add_handler(MessageHandler(filters.Text(ADMIN_LABELS), admin_menu_text))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_state_router))
    app.add_handler(CallbackQueryHandler(menu_button))  # catch-all for menu/cart callbacks

    logger.info("Bot starting...")
    app.run_polling()


if __name__ == "__main__":
    main()
