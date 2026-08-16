"""
Telegram Order Bot — cart + checkout + manual payment confirmation.

Built for markets like Lebanon where Telegram's native Payments API
(Stripe-backed) isn't available. Instead of in-chat card payment, the
bot collects the order, shows payment instructions (OMT/Whish transfer,
or an optional Tap Payments / MyFatoorah link you paste in), and lets
you (the admin) confirm payment with a tap — which then notifies the
customer automatically.

-------------------------------------------------------------------------
SETUP
-------------------------------------------------------------------------
1. pip install -r requirements.txt
2. Create a bot with @BotFather, get the token.
3. Get your own numeric Telegram user ID (message @userinfobot).
4. Set environment variables (or edit the constants below):
     BOT_TOKEN=5324079406:AAGhZWPrpMol2gG_UKzc7pOoTWD-Yg_O-uY
     ADMIN_CHAT_ID=5018373506
5. Edit the MENU dict below with your real items/prices.
6. Edit PAYMENT_INSTRUCTIONS with your real OMT/Whish details.
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
    Update,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ------------------------------------------------------------------
# CONFIG — edit these
# ------------------------------------------------------------------

BOT_TOKEN = os.environ.get("BOT_TOKEN", "PUT_YOUR_BOT_TOKEN_HERE")
ADMIN_CHAT_ID = int(os.environ.get("ADMIN_CHAT_ID", "0"))  # your Telegram user ID

DB_PATH = os.path.join(os.path.dirname(__file__), "orders.db")

CURRENCY = "$"

# Your menu. Keys are short item IDs, values are (display name, price).
MENU = {
    "item1": ("Classic Burger", 8.00),
    "item2": ("Chicken Wrap", 7.50),
    "item3": ("Fries", 3.00),
    "item4": ("Soft Drink", 1.50),
}

# Payment instructions shown to the customer at checkout.
PAYMENT_INSTRUCTIONS = (
    "*Pay via Payment link using visa or master card or Pay using local payment methods*\n\n"
    "Name: `Your Name Here`\n"
    "Phone: `+961 XX XXX XXX`\n\n"
    "After sending the transfer, tap *I've Paid* below. "
    "We'll confirm and start on your order right away."
)

# Optional: if you set up a Tap Payments / MyFatoorah *payment link* product,
# you can paste a link-generation URL or leave this blank to skip it.
PAYMENT_LINK_BASE_URL = "https://payments.suyool.com/pay/g401_MD"  # e.g. "https://pay.tap.company/..." or leave ""

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
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
    conn.close()
    return row


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
    return InlineKeyboardMarkup(rows)


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
        "Welcome! Tap an item below to add it to your order.",
        reply_markup=menu_keyboard(),
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

    text = (
        f"*Order #{order_id} created*\n\n"
        f"{format_cart(cart)}\n\n"
        f"{PAYMENT_INSTRUCTIONS}"
    )

    buttons = []
    if PAYMENT_LINK_BASE_URL:
        buttons.append([InlineKeyboardButton("💳 Pay by card", url=PAYMENT_LINK_BASE_URL)])
    buttons.append([InlineKeyboardButton("✅ I've Paid", callback_data=f"paid:{order_id}")])
    buttons.append([InlineKeyboardButton("✖️ Cancel order", callback_data=f"cancel:{order_id}")])

    await query.edit_message_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(buttons))

    # clear the cart now that the order has been placed
    context.user_data["cart"] = {}


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
        db_update_status(order_id, "awaiting_confirmation")
        await query.edit_message_text(
            f"Thanks! We've marked order #{order_id} as *awaiting confirmation*.\n"
            "You'll get a message the moment it's confirmed.",
            parse_mode=ParseMode.MARKDOWN,
        )
        await notify_admin_new_payment_claim(context, order)

    elif action == "cancel":
        db_update_status(order_id, "cancelled")
        await query.edit_message_text(f"Order #{order_id} cancelled.")


async def notify_admin_new_payment_claim(context: ContextTypes.DEFAULT_TYPE, order_row):
    order_id, user_id, username, items_json, total, status, created_at = order_row
    items = json.loads(items_json)
    lines = [f"{qty}x {MENU[i][0]}" for i, qty in items.items()]
    text = (
        f"🔔 *Payment claimed — Order #{order_id}*\n"
        f"From: @{username or user_id}\n\n"
        + "\n".join(lines)
        + f"\n\n*Total: {CURRENCY}{total:.2f}*\n\n"
        "Verify the transfer, then confirm or reject below."
    )
    if ADMIN_CHAT_ID:
        await context.bot.send_message(
            chat_id=ADMIN_CHAT_ID,
            text=text,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=admin_review_keyboard(order_id),
        )
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
        await query.edit_message_text("Order not found.")
        return

    _, user_id, username, items_json, total, status, created_at = order

    if action == "admin_confirm":
        db_update_status(order_id, "paid")
        await query.edit_message_text(f"✅ Order #{order_id} confirmed as paid.")
        await context.bot.send_message(
            chat_id=user_id,
            text=f"✅ Payment confirmed for order #{order_id}! We're preparing it now.",
        )

    elif action == "admin_reject":
        db_update_status(order_id, "rejected")
        await query.edit_message_text(f"❌ Order #{order_id} rejected.")
        await context.bot.send_message(
            chat_id=user_id,
            text=(
                f"We couldn't verify payment for order #{order_id}. "
                "Please double check the transfer details and try again, "
                "or contact us directly."
            ),
        )


# ------------------------------------------------------------------
# MAIN
# ------------------------------------------------------------------

def main():
    db_init()

    if BOT_TOKEN == "PUT_YOUR_BOT_TOKEN_HERE":
        raise SystemExit("Set BOT_TOKEN (env var or in bot.py) before running.")

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("myorders", my_orders))
    app.add_handler(CallbackQueryHandler(order_action, pattern=r"^(paid|cancel):"))
    app.add_handler(CallbackQueryHandler(admin_action, pattern=r"^admin_(confirm|reject):"))
    app.add_handler(CallbackQueryHandler(menu_button))  # catch-all for menu/cart callbacks

    logger.info("Bot starting...")
    app.run_polling()


if __name__ == "__main__":
    main()
