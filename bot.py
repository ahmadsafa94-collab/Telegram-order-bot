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
    conn.commit()
    conn.close()


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
SUPPORT_LABEL = "🆘 Support"


def main_menu_keyboard() -> ReplyKeyboardMarkup:
    """A persistent keyboard pinned to the bottom of the chat — stays visible
    across every message, not just the one it was attached to."""
    return ReplyKeyboardMarkup(
        [[BUY_LABEL, MY_SUBS_LABEL], [SUPPORT_LABEL]],
        resize_keyboard=True,
    )


def cart_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("➕ Add more items", callback_data="back_to_menu")],
            [InlineKeyboardButton("🗑 Clear cart", callback_data="clear_cart")],
            [InlineKeyboardButton("✅ Checkout", callback_data="checkout")],
        ]
    )


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
        rows = db_user_orders(update.effective_user.id)
        if not rows:
            await update.message.reply_text("You have no orders yet.")
            return
        lines = [
            f"#{order_id} — {CURRENCY}{total:.2f} — {status}"
            for order_id, items_json, total, status, created_at in rows
        ]
        await update.message.reply_text("Your recent orders:\n" + "\n".join(lines))

    elif text == SUPPORT_LABEL:
        await update.message.reply_text(
            "Tap below to contact support:",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("Open Support Chat", url="https://t.me/uptodate_admin")]]
            ),
        )


async def menu_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    cart = context.user_data.setdefault("cart", {})

    if data.startswith("add:"):
        item_id = data.split(":", 1)[1]
        cart[item_id] = cart.get(item_id, 0) + 1
        name = MENU[item_id][0]
        await query.answer(f"Added {name}", show_alert=False)

    elif data == "view_cart":
        await query.edit_message_text(format_cart(cart), parse_mode=ParseMode.MARKDOWN, reply_markup=cart_keyboard())
        return

    elif data == "back_to_menu":
        await query.edit_message_text("Tap an item below to add it to your order.", reply_markup=menu_keyboard())
        return

    elif data == "clear_cart":
        cart.clear()
        await query.edit_message_text("Cart cleared. Tap an item to start again.", reply_markup=menu_keyboard())
        return

    elif data == "checkout":
        await start_checkout(update, context)
        return

    # after adding an item, refresh the menu view with a small confirmation
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

        await start_imd_collection(context, order_id, user_id, items)


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
        await query.edit_message_caption(
            caption=f"✅ Order #{order_id} confirmed as paid.\n\nTap below when ready to send login details.",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("📤 Send Credentials", callback_data=f"deliver:{order_id}")]]
            ),
        )
        await context.bot.send_message(
            chat_id=user_id,
            text=(
                f"✅ Payment confirmed for order #{order_id}! We're preparing it now.\n"
                f"Your Telegram ID: {user_id} (keep this for any support requests)."
            ),
        )

        # If this order includes an iMD item, start the guided form collection.
        items = json.loads(items_json)
        await start_imd_collection(context, order_id, user_id, items)

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


async def text_state_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Single entry point for all free-text messages that aren't the main
    menu buttons. Dispatches based on what state the sender is in, so only
    one handler ever needs to be registered for generic text — registering
    two separate broad text handlers would cause them to silently steal
    each other's messages."""
    if context.user_data.get("awaiting_registration_field"):
        await registration_field_reply(update, context)
        return

    if update.effective_user.id == ADMIN_CHAT_ID:
        if context.user_data.get("awaiting_credentials_for_order"):
            await credentials_reply(update, context)
            return


async def start_imd_collection(context: ContextTypes.DEFAULT_TYPE, order_id: int, user_id: int, items: dict):
    """Kicks off the customer-facing form collection for an iMD order —
    email/username/password for a new account, or just the previous
    username for a renewal."""
    imd_item = next((i for i in items if i in IMD_TRIGGER_ITEMS), None)
    if not imd_item:
        return

    is_renew = imd_item in IMD_RENEW_ITEMS
    duration = IMD_DURATION_MAP[imd_item]

    # context.application.user_data lets us reach into the CUSTOMER's data
    # store from here, even though this often runs in the admin's context.
    customer_data = context.application.user_data[user_id]
    customer_data["registration_order"] = order_id
    customer_data["registration_data"] = {}
    customer_data["registration_is_renew"] = is_renew
    customer_data["registration_duration"] = duration

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
    flows: notify the admin with a one-tap 'complete this' button. The
    serial is pulled automatically from the pool when that button is
    tapped — the admin doesn't need to type one in."""
    order_id = context.user_data.pop("registration_order", None)
    is_renew = context.user_data.pop("registration_is_renew", False)
    duration = context.user_data.pop("registration_duration", None)
    context.user_data.pop("awaiting_registration_field", None)
    context.user_data.pop("registration_data", None)

    await update.message.reply_text(
        "Thanks! We're setting up your account now — you'll get your login details here shortly."
    )

    if not order_id or not ADMIN_CHAT_ID:
        return

    context.application.bot_data.setdefault("pending_registrations", {})[order_id] = {
        **data,
        "is_renew": is_renew,
        "duration": duration,
    }

    available = db_count_serials(duration)
    duration_label = "1 Year" if duration == "1y" else "6 Months"

    if is_renew:
        summary = f"Previous username: {data.get('prev_username')}"
        action_label = "✅ Complete Renewal"
    else:
        summary = (
            f"Email: {data.get('email')}\n"
            f"Username: {data.get('username')}\n"
            f"Password: {data.get('password')}"
        )
        action_label = "✅ Complete Registration"

    await context.bot.send_message(
        chat_id=ADMIN_CHAT_ID,
        text=(
            f"🎓 iMD {'renewal' if is_renew else 'registration'} request — Order #{order_id}\n"
            f"Duration: {duration_label} ({available} serials available)\n\n"
            f"{summary}"
        ),
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton(action_label, callback_data=f"reg_go:{order_id}")]]
        ),
    )


async def reg_go(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin tapped 'Complete Registration'/'Complete Renewal'. Pulls a
    serial automatically from the pool matching this order's duration —
    no manual serial entry needed."""
    query = update.callback_query
    if query.from_user.id != ADMIN_CHAT_ID:
        await query.answer("Not authorized.", show_alert=True)
        return
    await query.answer()

    order_id = int(query.data.split(":", 1)[1])
    pending = context.application.bot_data.get("pending_registrations", {})
    data = pending.get(order_id)

    if not data:
        await context.bot.send_message(chat_id=ADMIN_CHAT_ID, text=f"No pending registration data found for order #{order_id}.")
        return

    duration = data.get("duration")
    is_renew = data.get("is_renew")

    popped = db_pop_serial(duration)
    if not popped:
        duration_label = "1 Year" if duration == "1y" else "6 Months"
        await context.bot.send_message(
            chat_id=ADMIN_CHAT_ID,
            text=(
                f"⚠️ No available {duration_label} serials in stock for order #{order_id}.\n"
                f"Add more with /addserials {duration} <code1> <code2> ... then tap the button again."
            ),
        )
        return

    serial_id, serial_code = popped
    await context.bot.send_message(chat_id=ADMIN_CHAT_ID, text="Attempting registration...")

    if is_renew:
        success, detail = await attempt_imd_action(
            IMD_RENEW_URL, IMD_RENEW_FIELD_MAP, {"username": data.get("prev_username"), "serial": serial_code}
        )
    else:
        success, detail = await attempt_imd_action(
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

    if success:
        pending.pop(order_id, None)
        db_finalize_serial(serial_id, order_id, success=True)

        if is_renew:
            delivery_message = build_imd_delivery_message(data.get("prev_username"), password=None)
        else:
            delivery_message = build_imd_delivery_message(data.get("username"), data.get("password"))

        db_save_delivery(order_id, delivery_message)
        await context.bot.send_message(
            chat_id=ADMIN_CHAT_ID, text=f"✅ {'Renewal' if is_renew else 'Registration'} finished. Serial used: {serial_code}\n\n{detail}"
        )
        if customer_user_id:
            await context.bot.send_message(chat_id=customer_user_id, text=delivery_message)
    else:
        db_finalize_serial(serial_id, order_id, success=False)  # return serial to pool
        await context.bot.send_message(
            chat_id=ADMIN_CHAT_ID,
            text=(
                f"⚠️ Attempt did not clearly succeed for order #{order_id} — serial {serial_code} "
                f"was returned to the pool. Check the response below, or handle manually.\n\n{detail}"
            ),
        )


async def attempt_imd_action(url: str, field_map: dict, values: dict):
    """Automatic form submission using a real headless Chromium browser
    (via Playwright), since plain HTTP requests are blocked by Cloudflare.
    Shared by both registration (register/index.php) and renewal
    (ess.php) — the only difference is the URL, field map, and values.

    Note: the post-submit page can still show a Cloudflare interstitial
    even when the action itself went through server-side (confirmed by
    testing on the registration flow — accounts were created despite
    this page showing). So a lingering 'Just a moment' title after
    submit is NOT treated as a failure; only an actual exception counts.
    Returns (success: bool, detail: str)."""
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

            for _ in range(4):
                title = await page.title()
                if "Just a moment" not in title:
                    break
                await page.wait_for_timeout(3000)

            title = await page.title()
            if "Just a moment" in title:
                content = await page.content()
                await browser.close()
                return False, f"Still blocked by Cloudflare before the form loaded. Title: {title}\n{content[:500]}"

            for key, field_name in field_map.items():
                if key in values and values[key] is not None:
                    await page.fill(f'input[name="{field_name}"]', values[key])

            await page.click("#submit")
            await page.wait_for_load_state("networkidle", timeout=30000)

            for _ in range(3):
                result_title = await page.title()
                if "Just a moment" not in result_title:
                    break
                await page.wait_for_timeout(3000)

            result_title = await page.title()
            content = await page.content()
            await browser.close()

            snippet = content[:800].replace("\n", " ")
            return True, f"Submitted. Page title after: {result_title}\n{snippet}"

    except Exception as exc:
        return False, f"Browser automation failed: {exc}"


def build_imd_delivery_message(username: str, password: str = None) -> str:
    """The exact account-delivery message format used for iMD orders.
    Omits the password line for renewals (password param left as None)."""
    date_str = datetime.now().strftime("%Y-%m-%d")
    password_line = f"Password: {password}\n" if password else ""
    return (
        "✅ IMD 1 year Full Access \n\n"
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
    """Admin tapped '📤 Send Credentials' on a specific order — start waiting
    for their next text message, tied to this exact order."""
    query = update.callback_query
    if query.from_user.id != ADMIN_CHAT_ID:
        await query.answer("Not authorized.", show_alert=True)
        return
    await query.answer()

    order_id = int(query.data.split(":", 1)[1])
    context.user_data["awaiting_credentials_for_order"] = order_id
    await context.bot.send_message(
        chat_id=ADMIN_CHAT_ID,
        text=f"Reply with the username & password for order #{order_id}.",
    )


async def credentials_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Catches the admin's next text message after tapping 'Send Credentials'
    (the generic manual delivery flow for non-iMD products) and forwards
    it to the customer as their account details."""
    if update.effective_user.id != ADMIN_CHAT_ID:
        return

    order_id = context.user_data.get("awaiting_credentials_for_order")
    if not order_id:
        return  # admin isn't in the middle of delivering anything

    order = db_get_order(order_id)
    if not order:
        await update.message.reply_text("Order not found.")
        context.user_data.pop("awaiting_credentials_for_order", None)
        return

    _, user_id, username, items_json, total, status, created_at = order
    credentials_text = update.message.text

    await context.bot.send_message(
        chat_id=user_id,
        text=f"🔑 Here are your account details for order #{order_id}:\n\n{credentials_text}",
    )
    db_save_delivery(order_id, credentials_text)
    context.user_data.pop("awaiting_credentials_for_order", None)
    await update.message.reply_text(f"✅ Credentials sent to the customer for order #{order_id}.")


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
    """Admin-only: every order for one specific customer, by Telegram user
    ID or @username."""
    if update.effective_user.id != ADMIN_CHAT_ID:
        return

    if not context.args:
        await update.message.reply_text("Usage: /customer <user_id or @username>")
        return

    query_arg = context.args[0]
    conn = sqlite3.connect(DB_PATH)

    if query_arg.startswith("@"):
        username = query_arg[1:]
        rows = conn.execute(
            "SELECT id, items_json, total, status, created_at, delivered_at, credentials "
            "FROM orders WHERE username = ? ORDER BY id DESC",
            (username,),
        ).fetchall()
    else:
        try:
            user_id = int(query_arg)
        except ValueError:
            await update.message.reply_text("Provide a numeric Telegram user ID or @username.")
            return
        rows = conn.execute(
            "SELECT id, items_json, total, status, created_at, delivered_at, credentials "
            "FROM orders WHERE user_id = ? ORDER BY id DESC",
            (user_id,),
        ).fetchall()

    conn.close()

    if not rows:
        await update.message.reply_text(f"No orders found for {query_arg}.")
        return

    lines = [f"Orders for {query_arg}:"]
    for order_id, items_json, total, status, created_at, delivered_at, credentials in rows:
        items = json.loads(items_json)
        item_names = ", ".join(MENU[i][0] for i in items if i in MENU)
        line = f"\n#{order_id} — {item_names} — {CURRENCY}{total:.2f} — {status}"
        if delivered_at:
            line += f"\nDelivered: {delivered_at[:19]}\nCredentials: {credentials}"
        lines.append(line)

    text = "".join(lines)
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

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("myorders", my_orders))
    app.add_handler(CommandHandler("customers", customer_history))
    app.add_handler(CommandHandler("customer", customer_lookup))
    app.add_handler(CommandHandler("order", order_lookup))
    app.add_handler(CommandHandler("addserials", add_serials_command))
    app.add_handler(CommandHandler("serials", serials_status))
    app.add_handler(CallbackQueryHandler(order_action, pattern=r"^(paid|cancel):"))
    app.add_handler(CallbackQueryHandler(admin_action, pattern=r"^admin_(confirm|reject):"))
    app.add_handler(CallbackQueryHandler(deliver_start, pattern=r"^deliver:"))
    app.add_handler(CallbackQueryHandler(reg_go, pattern=r"^reg_go:"))
    app.add_handler(CallbackQueryHandler(imd_menu_start, pattern=r"^imd_menu$"))
    app.add_handler(CallbackQueryHandler(imd_type_selected, pattern=r"^imd_type:"))
    app.add_handler(CallbackQueryHandler(pay_with_stars, pattern=r"^pay_stars:"))
    app.add_handler(CallbackQueryHandler(local_pay_start, pattern=r"^local_pay:"))
    app.add_handler(CallbackQueryHandler(local_country_selected, pattern=r"^local_country:"))
    app.add_handler(CallbackQueryHandler(pay_crypto, pattern=r"^pay_crypto:"))
    app.add_handler(CallbackQueryHandler(back_to_checkout, pattern=r"^back_to_checkout:"))
    app.add_handler(PreCheckoutQueryHandler(precheckout_callback))
    app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment_callback))
    app.add_handler(MessageHandler(filters.PHOTO, receipt_photo))
    app.add_handler(MessageHandler(filters.Text([BUY_LABEL, MY_SUBS_LABEL, SUPPORT_LABEL]), main_menu_text))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_state_router))
    app.add_handler(CallbackQueryHandler(menu_button))  # catch-all for menu/cart callbacks

    logger.info("Bot starting...")
    app.run_polling()


if __name__ == "__main__":
    main()
