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
    "item2": ("iMD VIP - 1 year", 75.00),
    "item3": ("Uptodate Online + Offline", 30.00),
    "item4": ("Amboss Full Access - 1 year", 85.00),
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
        rows.append(
            [InlineKeyboardButton(f"{name} — {CURRENCY}{price:.2f}", callback_data=f"add:{item_id}")]
        )
    rows.append([InlineKeyboardButton("🛒 View Cart / Checkout", callback_data="view_cart")])
    rows.append([InlineKeyboardButton("⬅️ Main Menu", callback_data="main_menu")])
    return InlineKeyboardMarkup(rows)


def main_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🛒 Buy New Subscription", callback_data="show_menu")],
            [InlineKeyboardButton("📋 My Subscriptions", callback_data="view_my_subs")],
            [InlineKeyboardButton("🆘 Support", url="https://t.me/uptodate_admin")],
        ]
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
        "Welcome! What would you like to do?",
        reply_markup=main_menu_keyboard(),
    )


async def main_menu_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the three top-level buttons: Buy, My Subscriptions, and
    navigating back to this menu from elsewhere."""
    query = update.callback_query
    await query.answer()

    if query.data == "show_menu":
        context.user_data.setdefault("cart", {})
        await query.edit_message_text(
            "Tap an item below to add it to your order.", reply_markup=menu_keyboard()
        )

    elif query.data == "view_my_subs":
        rows = db_user_orders(query.from_user.id)
        back_button = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Main Menu", callback_data="main_menu")]])
        if not rows:
            await query.edit_message_text("You have no orders yet.", reply_markup=back_button)
            return
        lines = [f"#{order_id} — {CURRENCY}{total:.2f} — {status}" for order_id, items_json, total, status, created_at in rows]
        text = "Your recent orders:\n" + "\n".join(lines)
        await query.edit_message_text(text, reply_markup=back_button)

    elif query.data == "main_menu":
        await query.edit_message_text("Welcome! What would you like to do?", reply_markup=main_menu_keyboard())


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
    """Catches the admin's next text message after a confirmation and
    forwards it to the customer as their account credentials."""
    if update.effective_user.id != ADMIN_CHAT_ID:
        return

    order_id = context.user_data.get("awaiting_credentials_for_order")
    if not order_id:
        return  # admin isn't in the middle of delivering credentials

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
    app.add_handler(CallbackQueryHandler(order_action, pattern=r"^(paid|cancel):"))
    app.add_handler(CallbackQueryHandler(admin_action, pattern=r"^admin_(confirm|reject):"))
    app.add_handler(CallbackQueryHandler(deliver_start, pattern=r"^deliver:"))
    app.add_handler(CallbackQueryHandler(pay_with_stars, pattern=r"^pay_stars:"))
    app.add_handler(CallbackQueryHandler(local_pay_start, pattern=r"^local_pay:"))
    app.add_handler(CallbackQueryHandler(local_country_selected, pattern=r"^local_country:"))
    app.add_handler(CallbackQueryHandler(pay_crypto, pattern=r"^pay_crypto:"))
    app.add_handler(CallbackQueryHandler(back_to_checkout, pattern=r"^back_to_checkout:"))
    app.add_handler(CallbackQueryHandler(main_menu_button, pattern=r"^(show_menu|view_my_subs|main_menu)$"))
    app.add_handler(PreCheckoutQueryHandler(precheckout_callback))
    app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment_callback))
    app.add_handler(MessageHandler(filters.PHOTO, receipt_photo))
    app.add_handler(MessageHandler(filters.TEXT & filters.User(user_id=ADMIN_CHAT_ID) & ~filters.COMMAND, credentials_reply))
    app.add_handler(CallbackQueryHandler(menu_button))  # catch-all for menu/cart callbacks

    logger.info("Bot starting...")
    app.run_polling()


if __name__ == "__main__":
    main()
