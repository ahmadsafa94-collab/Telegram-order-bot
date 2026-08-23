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

import asyncio
import json
import logging
import os
import math
import re
import sqlite3
from datetime import datetime, timedelta

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InlineQueryResultArticle,
    InputTextMessageContent,
    KeyboardButton,
    LabeledPrice,
    ReplyKeyboardMarkup,
    Update,
    WebAppInfo,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    InlineQueryHandler,
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

# Private "Payments" channel — every approved receipt is posted here.
# Numeric channel IDs look like -100xxxxxxxxxx. Leave unset until you've
# created the channel and added this bot as an admin; see
# channel_post_detector below for how to discover the ID.
PAYMENTS_CHANNEL_ID_RAW = os.environ.get("PAYMENTS_CHANNEL_ID", "-1004374833714")
PAYMENTS_CHANNEL_ID = int(PAYMENTS_CHANNEL_ID_RAW) if PAYMENTS_CHANNEL_ID_RAW.strip() else None

# Mini App URL — set this to your GitHub Pages URL once deployed.
# Format: https://<github-username>.github.io/<repo-name>/
# Example: https://ahmadsafa94-collab.github.io/Telegram-order-bot/
MINI_APP_URL = os.environ.get("MINI_APP_URL", "")

# HTTP API server for the Mini App to call.
# Railway sets RAILWAY_PUBLIC_DOMAIN automatically — no manual config needed.
# If running locally or on another host, set BOT_API_URL manually.

# Private "Subscriptions" channel — every subscription actually delivered
# to a customer is posted here (product, login, date, customer).
SUBSCRIPTIONS_CHANNEL_ID_RAW = os.environ.get("SUBSCRIPTIONS_CHANNEL_ID", "-1004471406420")
SUBSCRIPTIONS_CHANNEL_ID = int(SUBSCRIPTIONS_CHANNEL_ID_RAW) if SUBSCRIPTIONS_CHANNEL_ID_RAW.strip() else None

DB_PATH = os.environ.get("DB_PATH", os.path.join(os.path.dirname(__file__), "orders.db"))

CURRENCY = "$"

# Hours ahead of UTC for sales-report day boundaries. Order timestamps are
# stored in UTC, so without this "Today" would end at UTC midnight rather
# than your local one. Set via the REPORT_UTC_OFFSET variable in Railway.
REPORT_UTC_OFFSET = float(os.environ.get("REPORT_UTC_OFFSET", "3"))

# Telegram Stars pricing: ~50 Stars per $1 (based on the in-app purchase
# packages, e.g. 100 Stars = $2.00). Adjust if Telegram's pricing changes.
STAR_RATE = 50

# Your menu. Keys are short item IDs, values are (display name, price).
MENU = {
    # Legacy — no longer shown in any menu, kept only so a cart/order that
    # was persisted (PicklePersistence survives restarts) before this
    # catalog restructure doesn't crash on lookup.
    "item1": ("Uptodate Online", 20.00),
    "item3": ("Uptodate Online + Offline", 30.00),
    "item4": ("Amboss Full Access - 1 year", 85.00),

    # iMD VIP — unchanged.
    "imd_new_6m": ("iMD VIP New Account - 6 Months", 50.00),
    "imd_new_1y": ("iMD VIP New Account - 1 Year", 75.00),
    "imd_renew_6m": ("iMD VIP Renewal - 6 Months", 50.00),
    "imd_renew_1y": ("iMD VIP Renewal - 1 Year", 75.00),

    # Uptodate — three access tiers, each with a Mobile App / Mobile App +
    # Browser choice.
    "up_online_app": ("Uptodate Online 1 Year - Mobile App", 20.00),
    "up_online_web": ("Uptodate Online 1 Year - Mobile App + Browser", 35.00),
    "up_offline_app": ("Uptodate Online + Offline Access 1 Year - Mobile App", 30.00),
    "up_offline_web": ("Uptodate Online + Offline Access 1 Year - Mobile App + Browser", 45.00),
    "up_pathways_app": ("Uptodate Online + Pathways Access 1 Year - Mobile App", 30.00),
    "up_pathways_web": ("Uptodate Online + Pathways Access 1 Year - Mobile App + Browser", 45.00),

    # Amboss — Premium+Library (1 year, or a fixed-date promo), or
    # Library-only.
    "amboss_premium_1y": ("Amboss Premium (Unlimited Qbanks) + Library - 1 Year", 85.00),
    "amboss_premium_promo": ("Amboss Premium (Unlimited Qbanks) + Library - Valid until 18/9/2026", 35.00),
    "amboss_library": ("Amboss Library Access Only (Limited Qbanks) - 1 Year", 65.00),

    # Single-choice subscriptions — one price each, no sub-menu needed.
    "dynamed": ("Dynamed - 1 Year", 20.00),
    "dynamedex": ("DynamedEx - 1 Year", 30.00),
    "bmj_best_practice": ("BMJ Best Practice - 1 Year", 20.00),
    "bmj_learning": ("BMJ Learning - 1 Year", 30.00),
    "visualdx": ("VisualDx - 1 Year", 20.00),
    "lexicomp": ("Lexicomp - Mobile App - 1 Year", 20.00),
    "accessmedicine": ("AccessMedicine - 1 Year", 20.00),
    "boardvitals": ("Boardvitals VIP - 1 Year", 55.00),
    "clinicalkey": ("ClinicalKey - 1 Year", 35.00),
    "sciencedirect": ("ScienceDirect - 1 Year", 35.00),
    "statdx": ("StatDx - 1 Year", 35.00),
    "nejm": ("NEJM - 1 Year", 20.00),
    "jama_evidence": ("JAMA Evidence - 1 Year", 20.00),
    "scopus": ("Scopus - 1 Year", 35.00),
    "springerlink": ("SpringerLink - 1 Year", 35.00),
}

# Order the single-choice items appear in the main menu.
SINGLE_MAIN_ITEMS = [
    "dynamed", "dynamedex", "bmj_best_practice", "bmj_learning", "visualdx",
    "lexicomp", "accessmedicine", "boardvitals", "clinicalkey", "sciencedirect",
    "statdx", "nejm", "jama_evidence", "scopus", "springerlink",
]

# Navigation-only tree for the two multi-level products (Uptodate, Amboss).
# Prices/names for the actual leaves live in MENU (single source of truth);
# this only describes how to browse down to them. A node is a category if
# it has "children", or a leaf if it has "item" (pointing at a MENU key) —
# the two can be mixed at the same level (see Amboss below).
CATALOG = {
    "uptodate": {
        "label": "Uptodate",
        "children": {
            "up_online": {
                "label": "Online 1 Year",
                "children": {
                    "up_online_app": {"label": "Mobile App", "item": "up_online_app"},
                    "up_online_web": {"label": "Mobile App + Browser", "item": "up_online_web"},
                },
            },
            "up_offline": {
                "label": "Online + Offline Access 1 Year",
                "children": {
                    "up_offline_app": {"label": "Mobile App", "item": "up_offline_app"},
                    "up_offline_web": {"label": "Mobile App + Browser", "item": "up_offline_web"},
                },
            },
            "up_pathways": {
                "label": "Online + Pathways Access 1 Year",
                "children": {
                    "up_pathways_app": {"label": "Mobile App", "item": "up_pathways_app"},
                    "up_pathways_web": {"label": "Mobile App + Browser", "item": "up_pathways_web"},
                },
            },
        },
    },
    "amboss": {
        "label": "Amboss",
        "children": {
            "amboss_premium": {
                "label": "Premium (Unlimited Qbanks) + Library",
                "children": {
                    "amboss_premium_1y": {"label": "1 Year", "item": "amboss_premium_1y"},
                    "amboss_premium_promo": {"label": "Valid until 18/9/2026", "item": "amboss_premium_promo"},
                },
            },
            "amboss_library": {"label": "Library Access Only (Limited Qbanks)", "item": "amboss_library"},
        },
    },
}

# ------------------------------------------------------------------
# iMD FORM COLLECTION
# ------------------------------------------------------------------

# All four iMD variants trigger the guided collection flow.
IMD_TRIGGER_ITEMS = {"imd_new_6m", "imd_new_1y", "imd_renew_6m", "imd_renew_1y"}
IMD_NEW_ITEMS = {"imd_new_6m", "imd_new_1y"}
IMD_RENEW_ITEMS = {"imd_renew_6m", "imd_renew_1y"}

# ------------------------------------------------------------------
# SUPPORT TICKETS
# ------------------------------------------------------------------

# Which purchased items route to the Uptodate-specific ticket choices
# (vs. iMD's, vs. the generic fallback for everything else).
UPTODATE_TICKET_ITEMS = {
    "item1", "item3",  # legacy
    "up_online_app", "up_online_web", "up_offline_app", "up_offline_web",
    "up_pathways_app", "up_pathways_web",
}

IMD_FORGOT_PASSWORD_URL = "https://en.imedicaldoctor.net/forgot.php"
IMD_CHANGE_PASSWORD_URL = "https://en.imedicaldoctor.net/changepass.php"
IMD_APK_URL = "sg.imedicaldoctor.net/imd200.apk"
IMD_WEB_URL = "www.imdweb.org"

# Maps each item id to which serial pool it should draw from.
IMD_DURATION_MAP = {
    "imd_new_6m": "6m",
    "imd_new_1y": "1y",
    "imd_renew_6m": "6m",
    "imd_renew_1y": "1y",
}

# Overridable via Railway variables, so a URL change (or trying http://
# instead of https://) doesn't need a code edit.
IMD_REGISTER_URL = os.environ.get(
    "IMD_REGISTER_URL", "https://imedicaldoctor.net/register/index.php"
)
IMD_RENEW_URL = os.environ.get("IMD_RENEW_URL", "https://imedicaldoctor.net/ess.php")

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

GENERIC_PASSWORD_SYMBOLS = "-_.#@"

DELIVERY_48H_MESSAGE = (
    "✅ Thanks! Your subscription will be delivered here within the next 48 hours.\n\n"
    "If you haven't heard from us after 48 hours, please contact support from the menu."
)

# The fields collected for non-iMD subscriptions, in order.
GENERIC_FIELDS = [
    ("first_name", "First name:"),
    ("last_name", "Last name:"),
    (
        "email",
        "Email address:\n\n(This must be an email you have never used to register on "
        "this website before.)",
    ),
    (
        "username",
        "Desired username (at least 6 characters):\n\n(This must be a username you have "
        "never used to register on this website before.)",
    ),
    (
        "password",
        "Desired password (at least 8 characters, with at least one number, "
        f"one capital letter, and one symbol from {GENERIC_PASSWORD_SYMBOLS}):",
    ),
]

# Renewal is much simpler — just the existing login, no uniqueness rules
# (they're relaying credentials that already exist, not creating new ones).
RENEWAL_FIELDS = [
    ("login_username", "Username or email used to login to the account:"),
    ("login_password", "Password used to login to the account:"),
]

# Used when re-prompting a customer who has an unfinished order.
IMD_FIELD_PROMPTS = {
    "prev_username": "Reply with your previous iMD username:",
    "email": "Reply with your email address:",
    "username": "Reply with your desired username:",
    "password": "Reply with your desired password:",
}

# Combined lookup for re-prompting a non-iMD field, covering both the New
# Account and Renewal field sets.
GENERIC_FIELD_PROMPTS = dict(GENERIC_FIELDS)
GENERIC_FIELD_PROMPTS.update(dict(RENEWAL_FIELDS))


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

COUNTRY_FLAGS = {
    "Lebanon": "🇱🇧", "Jordan": "🇯🇴", "India": "🇮🇳", "Ghana": "🇬🇭",
    "Pakistan": "🇵🇰", "Europe": "🇪🇺", "USA": "🇺🇸", "KSA": "🇸🇦", "Russia": "🇷🇺",
}

# 1 USD in the local currency, and the currency code to display it in.
# "USD" as the code means no conversion is shown (paid in USD directly).
CURRENCY_RATES = {
    "Lebanon": ("USD", 1),
    "Jordan": ("JOD", 0.7066),
    "India": ("INR", 94.6666),
    "Ghana": ("GHS", 11.8),
    "Pakistan": ("PKR", 280),
    "Europe": ("USD", 1),
    "USA": ("USD", 1),
    "KSA": ("SAR", 3.8),
    "Russia": ("RUB", 85.5),
}


def convert_for_country(total_usd: float, country: str) -> str:
    """The order total converted into that country's currency, with the
    USD amount shown alongside for reference."""
    code, rate = CURRENCY_RATES.get(country, ("USD", 1))
    if code == "USD":
        return f"{CURRENCY}{total_usd:.2f}"
    converted = total_usd * rate
    return f"{converted:,.2f} {code} (≈{CURRENCY}{total_usd:.2f})"


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
        "CLIQ ALIAS: `WKS555`\n(Zain Cash)\nMohammad shamalti"
    ),
    "India": (
        "UPI ID (tap to copy)\n`s4005194160889795@slc`\n\n"
        "Name: Shilpaben karetiya\n\n"
        "‼️ Important Remarks:\n\n"
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
    "Pakistan": (
        "Tap on the account/IBAN number to copy.\n\n"
        "Bank: HBL\n"
        "Name: MUHAMMAD BILAL\n"
        "Account Number: `03577900871203`\n"
        "IBAN: `PK08HABB0003577900871203`\n"
        "Branch: TOTALAI"
    ),
    "Europe": (
        "Revolut (visa to visa)\n\n"
        "1) Open your Revolut app/website\n"
        "2) Click \"Transfer\" > \"+ New\" > \"Card recipient\"\n"
        "3) Enter the details\n\n"
        "Card Number: `5413525250267271`\n"
        "Name: Alijon Karimov\n"
        "Country: Tajikistan"
    ),
    "KSA": (
        "(Tap to copy)\n\n"
        "`SA8710000006857309000101`\n\n"
        "`SA0510000062300187719603`\n\n"
        "Bank: Ahli Bank\n"
        "Name: Jamil Hajji\n\n"
        "*Please make sure the purpose of the payment be Friends and family or "
        "personal NOT goods or services.*"
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

# USA offers a choice of remittance apps instead of one fixed method.
USA_PAYMENT_APPS = ["TapTap Send", "Ria", "Paysend", "Remitly", "Revolut"]

_RIA_PAYSEND_REMITLY_TEXT = (
    "1- Enter the payment app/website\n"
    "2- Send to Country: Lebanon\n"
    "3- Choose \"Purpl\"\n"
    "4- Enter the Mobile Number `0096181666579`\n"
    "5- First Name: Ahmad\n"
    "6- Last Name: Safa\n"
    "7- Purpose: Educational/Personal\n"
    "8- Date of Birth: 17/09/1994"
)

USA_APP_INSTRUCTIONS = {
    "TapTap Send": (
        "1- Open the Taptap Send app\n"
        "2- Choose Country: Lebanon\n"
        "3- Choose Whish Money Wallet\n"
        "Make sure you choose Whish Money wallet not Cash pickup\n"
        "4: Enter the details: (tap to copy)\n\n"
        "Phone Number: `0096181666579`\n"
        "First Name: Ahmad\n"
        "Last Name: Safa\n"
        "Year of Birth: 1994"
    ),
    "Ria": _RIA_PAYSEND_REMITLY_TEXT,
    "Paysend": _RIA_PAYSEND_REMITLY_TEXT,
    "Remitly": _RIA_PAYSEND_REMITLY_TEXT,
    "Revolut": LOCAL_PAYMENT_INSTRUCTIONS["Europe"],
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

# Card payment tutorial screenshots, shown when a customer taps
# "Pay using Visa/Mastercard".
CARD_TUTORIAL_IMAGES = [
    os.path.join(os.path.dirname(__file__), "assets", "card_step1.jpg"),
    os.path.join(os.path.dirname(__file__), "assets", "card_step2.jpg"),
    os.path.join(os.path.dirname(__file__), "assets", "card_step3.jpg"),
]

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
    for column, coltype in [
        ("credentials", "TEXT"), ("delivered_at", "TEXT"),
        ("payment_method", "TEXT"), ("payer_name", "TEXT"), ("receipt_photo_file_id", "TEXT"),
    ]:
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
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            direction TEXT NOT NULL,
            body TEXT NOT NULL,
            is_read INTEGER NOT NULL DEFAULT 0,
            delivered INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            first_seen TEXT NOT NULL,
            last_seen TEXT NOT NULL
        )
        """
    )
    # Referral/credits system — added later, ALTER TABLE skipped if the
    # column already exists.
    for column, coltype in [
        ("credits", "INTEGER NOT NULL DEFAULT 0"),
        ("referred_by", "INTEGER"),
        ("referral_intro_shown", "INTEGER NOT NULL DEFAULT 0"),
    ]:
        try:
            conn.execute(f"ALTER TABLE users ADD COLUMN {column} {coltype}")
        except sqlite3.OperationalError:
            pass  # column already exists
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS announcements (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            body TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS seen_announcements (
            user_id INTEGER NOT NULL,
            announcement_id INTEGER NOT NULL,
            PRIMARY KEY (user_id, announcement_id)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS stock_status (
            item_id TEXT PRIMARY KEY,
            status TEXT NOT NULL DEFAULT 'available'
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS custom_items (
            item_id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            price REAL NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS used_credentials (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            item_id TEXT NOT NULL,
            email TEXT,
            username TEXT,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS book_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            username TEXT,
            link TEXT NOT NULL,
            price REAL,
            status TEXT NOT NULL DEFAULT 'pending',
            order_id INTEGER,
            created_at TEXT NOT NULL,
            priced_at TEXT,
            resolved_at TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS book_menu_items (
            item_id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            price REAL NOT NULL,
            request_id INTEGER NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS imd_catalog (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            category TEXT,
            extracted_at TEXT NOT NULL
        )
        """
    )
    try:
        conn.execute("CREATE INDEX IF NOT EXISTS idx_imd_name ON imd_catalog(name)")
    except Exception:
        pass
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS tickets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            username TEXT,
            category TEXT NOT NULL,
            subscription_item_id TEXT,
            subscription_label TEXT,
            self_help TEXT,
            message TEXT,
            photo_file_id TEXT,
            status TEXT NOT NULL DEFAULT 'unresolved',
            resolution_message TEXT,
            created_at TEXT NOT NULL,
            resolved_at TEXT
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

    # Migration: an earlier version created `fulfilment` without unit_no /
    # info_json and with UNIQUE(order_id, item_id). CREATE TABLE IF NOT
    # EXISTS silently leaves that old table in place, so every insert then
    # fails with OperationalError and order fulfilment dies right after the
    # payment-confirmed message. Rebuild it when the old shape is detected.
    columns = [r[1] for r in conn.execute("PRAGMA table_info(fulfilment)").fetchall()]
    if columns and "unit_no" not in columns:
        conn.execute("ALTER TABLE fulfilment RENAME TO fulfilment_old")
        conn.execute(
            """
            CREATE TABLE fulfilment (
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
        conn.execute(
            "INSERT INTO fulfilment (order_id, user_id, item_id, unit_no, state, created_at) "
            "SELECT order_id, user_id, item_id, 1, state, created_at FROM fulfilment_old"
        )
        conn.execute("DROP TABLE fulfilment_old")
        logger.info("Migrated fulfilment table to per-unit schema")

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


def db_delete_fulfilment(fulfilment_id: int):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.execute("DELETE FROM fulfilment WHERE id = ?", (fulfilment_id,))
    conn.commit()
    deleted = cur.rowcount
    conn.close()
    return bool(deleted)


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
        # NOT an `o.status = 'paid'` check: delivering the first item of a
        # multi-item order flips the whole order's status to 'delivered',
        # which would then hide its remaining items from this list.
        "WHERE f.state != 'delivered' "
        "AND o.status NOT IN ('cancelled', 'rejected', 'awaiting_payment', "
        "'awaiting_receipt', 'awaiting_confirmation') "
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


def db_sales_since(since_iso: str):
    """Units sold per product since a timestamp, counting only orders that
    were actually paid for. Returns (list of (item_id, qty), revenue)."""
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT items_json, total FROM orders "
        "WHERE created_at >= ? AND status NOT IN ('awaiting_payment', 'awaiting_receipt', "
        "'awaiting_confirmation', 'cancelled', 'rejected')",
        (since_iso,),
    ).fetchall()
    conn.close()

    counts = {}
    revenue = 0.0
    for items_json, total in rows:
        try:
            items = json.loads(items_json)
        except (TypeError, ValueError):
            continue
        for item_id, qty in items.items():
            counts[item_id] = counts.get(item_id, 0) + int(qty)
        revenue += float(total or 0)
    return sorted(counts.items(), key=lambda kv: -kv[1]), revenue


def db_add_message(order_id: int, user_id: int, direction: str, body: str, delivered: bool = True) -> int:
    """Records one message in either direction. `delivered` tracks whether
    the live push to Telegram actually succeeded — if not, the Inbox is
    still where the admin can find it, so a failed live send is never a
    dead end."""
    conn = sqlite3.connect(DB_PATH)
    # Outgoing (admin -> customer) messages are authored by the admin, so
    # there's nothing to "read" — only incoming ones start unread.
    is_read = 1 if direction == "to_customer" else 0
    cur = conn.execute(
        "INSERT INTO messages (order_id, user_id, direction, body, is_read, delivered, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (order_id, user_id, direction, body, is_read, int(delivered), datetime.utcnow().isoformat()),
    )
    conn.commit()
    message_id = cur.lastrowid
    conn.close()
    return message_id


def db_inbox_messages(read: bool):
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT m.id, m.order_id, o.username, m.body, m.created_at, m.delivered "
        "FROM messages m LEFT JOIN orders o ON o.id = m.order_id "
        "WHERE m.direction = 'from_customer' AND m.is_read = ? "
        "ORDER BY m.id DESC",
        (0 if not read else 1,),
    ).fetchall()
    conn.close()
    return rows


def db_get_message(message_id: int):
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT id, order_id, user_id, direction, body, created_at, delivered FROM messages WHERE id = ?",
        (message_id,),
    ).fetchone()
    conn.close()
    return row


def db_mark_message_read(message_id: int):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("UPDATE messages SET is_read = 1 WHERE id = ?", (message_id,))
    conn.commit()
    conn.close()


def db_user_exists(user_id: int) -> bool:
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute("SELECT 1 FROM users WHERE user_id = ?", (user_id,)).fetchone()
    conn.close()
    return row is not None


def db_upsert_user(user_id: int, username: str):
    """Records that this person has used the bot — the recipient list for
    broadcasts. Called on every /start, so it stays current."""
    now = datetime.utcnow().isoformat()
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT INTO users (user_id, username, first_seen, last_seen) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(user_id) DO UPDATE SET username = excluded.username, last_seen = excluded.last_seen",
        (user_id, username, now, now),
    )
    conn.commit()
    conn.close()


def db_set_referred_by(user_id: int, referrer_id: int):
    """Records who referred this user — only takes effect if they don't
    already have a referrer (first-touch attribution) and they aren't
    referring themselves."""
    if referrer_id == user_id:
        return
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "UPDATE users SET referred_by = ? WHERE user_id = ? AND referred_by IS NULL",
        (referrer_id, user_id),
    )
    conn.commit()
    conn.close()


def db_get_referred_by(user_id: int):
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute("SELECT referred_by FROM users WHERE user_id = ?", (user_id,)).fetchone()
    conn.close()
    return row[0] if row else None


def db_get_credits(user_id: int) -> int:
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute("SELECT credits FROM users WHERE user_id = ?", (user_id,)).fetchone()
    conn.close()
    return row[0] if row else 0


def db_add_credits(user_id: int, amount: int) -> int:
    """Adds credits and returns the new balance."""
    conn = sqlite3.connect(DB_PATH)
    conn.execute("UPDATE users SET credits = credits + ? WHERE user_id = ?", (amount, user_id))
    conn.commit()
    row = conn.execute("SELECT credits FROM users WHERE user_id = ?", (user_id,)).fetchone()
    conn.close()
    return row[0] if row else amount


def db_deduct_credits(user_id: int, amount: int) -> bool:
    """Deducts credits only if the balance actually covers it — the WHERE
    clause makes this safe even if called concurrently. Returns whether
    the deduction happened."""
    conn = sqlite3.connect(DB_PATH)
    cur = conn.execute(
        "UPDATE users SET credits = credits - ? WHERE user_id = ? AND credits >= ?",
        (amount, user_id, amount),
    )
    conn.commit()
    success = cur.rowcount > 0
    conn.close()
    return success


def db_mark_referral_intro_shown(user_id: int):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("UPDATE users SET referral_intro_shown = 1 WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()


def db_referral_intro_shown(user_id: int) -> bool:
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute("SELECT referral_intro_shown FROM users WHERE user_id = ?", (user_id,)).fetchone()
    conn.close()
    return bool(row and row[0])


def db_customers_with_credits():
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT user_id, username, credits FROM users WHERE credits > 0 ORDER BY credits DESC"
    ).fetchall()
    conn.close()
    return rows


def db_all_user_ids(exclude: int = None):
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute("SELECT user_id FROM users").fetchall()
    conn.close()
    return [r[0] for r in rows if r[0] != exclude]


def db_create_ticket(user_id: int, username: str, category: str, subscription_item_id: str = None,
                      subscription_label: str = None, self_help: str = None) -> int:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.execute(
        "INSERT INTO tickets (user_id, username, category, subscription_item_id, "
        "subscription_label, self_help, status, created_at) VALUES (?, ?, ?, ?, ?, ?, 'unresolved', ?)",
        (user_id, username, category, subscription_item_id, subscription_label, self_help,
         datetime.utcnow().isoformat()),
    )
    conn.commit()
    ticket_id = cur.lastrowid
    conn.close()
    return ticket_id


def db_set_ticket_message(ticket_id: int, message: str = None, photo_file_id: str = None):
    conn = sqlite3.connect(DB_PATH)
    if message is not None:
        conn.execute("UPDATE tickets SET message = ? WHERE id = ?", (message, ticket_id))
    if photo_file_id is not None:
        conn.execute("UPDATE tickets SET photo_file_id = ? WHERE id = ?", (photo_file_id, ticket_id))
    conn.commit()
    conn.close()


def db_get_ticket(ticket_id: int):
    """Returns (id, user_id, username, category, subscription_item_id,
    subscription_label, self_help, message, photo_file_id, status,
    resolution_message, created_at, resolved_at)."""
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT id, user_id, username, category, subscription_item_id, subscription_label, "
        "self_help, message, photo_file_id, status, resolution_message, created_at, resolved_at "
        "FROM tickets WHERE id = ?",
        (ticket_id,),
    ).fetchone()
    conn.close()
    return row


def db_list_tickets(status: str):
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT id, user_id, username, category, subscription_label, created_at "
        "FROM tickets WHERE status = ? ORDER BY id DESC",
        (status,),
    ).fetchall()
    conn.close()
    return rows


def db_resolve_ticket(ticket_id: int, resolution_message: str):
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "UPDATE tickets SET status = 'resolved', resolution_message = ?, resolved_at = ? WHERE id = ?",
        (resolution_message, datetime.utcnow().isoformat(), ticket_id),
    )
    conn.commit()
    conn.close()


def db_add_custom_item(item_id: str, name: str, price: float):
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT INTO custom_items (item_id, name, price, created_at) VALUES (?, ?, ?, ?)",
        (item_id, name, price, datetime.utcnow().isoformat()),
    )
    conn.commit()
    conn.close()


def db_load_custom_items():
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute("SELECT item_id, name, price FROM custom_items").fetchall()
    conn.close()
    return rows


def db_credential_already_used(user_id: int, item_id: str, field: str, value: str) -> bool:
    """Checks whether this customer has already used this email or
    username in a previous New Account registration for this same
    subscription. Case-insensitive, since emails/usernames are.
    field must be 'email' or 'username'."""
    assert field in ("email", "username"), "field must be 'email' or 'username' — never user input"
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        f"SELECT 1 FROM used_credentials WHERE user_id = ? AND item_id = ? AND LOWER({field}) = LOWER(?)",
        (user_id, item_id, value),
    ).fetchone()
    conn.close()
    return row is not None


def db_record_used_credentials(user_id: int, item_id: str, email: str, username: str):
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT INTO used_credentials (user_id, item_id, email, username, created_at) VALUES (?, ?, ?, ?, ?)",
        (user_id, item_id, email, username, datetime.utcnow().isoformat()),
    )
    conn.commit()
    conn.close()


def db_create_book_request(user_id: int, username: str, link: str) -> int:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.execute(
        "INSERT INTO book_requests (user_id, username, link, status, created_at) VALUES (?, ?, ?, 'pending', ?)",
        (user_id, username, link, datetime.utcnow().isoformat()),
    )
    conn.commit()
    request_id = cur.lastrowid
    conn.close()
    return request_id


def db_get_book_request(request_id: int):
    """Returns (id, user_id, username, link, price, status, order_id,
    created_at, priced_at, resolved_at)."""
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT id, user_id, username, link, price, status, order_id, created_at, priced_at, resolved_at "
        "FROM book_requests WHERE id = ?",
        (request_id,),
    ).fetchone()
    conn.close()
    return row


def db_set_book_price(request_id: int, price: float):
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "UPDATE book_requests SET price = ?, status = 'priced', priced_at = ? WHERE id = ?",
        (price, datetime.utcnow().isoformat(), request_id),
    )
    conn.commit()
    conn.close()


def db_set_book_status(request_id: int, status: str, order_id: int = None):
    conn = sqlite3.connect(DB_PATH)
    if order_id is not None:
        conn.execute(
            "UPDATE book_requests SET status = ?, order_id = ?, resolved_at = ? WHERE id = ?",
            (status, order_id, datetime.utcnow().isoformat(), request_id),
        )
    else:
        conn.execute(
            "UPDATE book_requests SET status = ?, resolved_at = ? WHERE id = ?",
            (status, datetime.utcnow().isoformat(), request_id),
        )
    conn.commit()
    conn.close()


def db_list_book_requests(status: str):
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT id, user_id, username, link, price, created_at FROM book_requests "
        "WHERE status = ? ORDER BY id DESC",
        (status,),
    ).fetchall()
    conn.close()
    return rows


def db_add_book_menu_item(item_id: str, name: str, price: float, request_id: int):
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT INTO book_menu_items (item_id, name, price, request_id, created_at) VALUES (?, ?, ?, ?, ?)",
        (item_id, name, price, request_id, datetime.utcnow().isoformat()),
    )
    conn.commit()
    conn.close()


def db_load_book_menu_items():
    """Only loaded into MENU (for lookups to work), never into
    SINGLE_MAIN_ITEMS — these are one-off private items for whichever
    customer requested them, not part of the general catalog."""
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute("SELECT item_id, name, price FROM book_menu_items").fetchall()
    conn.close()
    return rows


# ------------------------------------------------------------------
# MINI APP HTTP API  (aiohttp, runs alongside the bot in the same loop)
# ------------------------------------------------------------------

def generate_item_id(name: str) -> str:
    """Slugifies a subscription name into an item_id, guaranteed unique
    against whatever's currently in MENU (adds a numeric suffix on
    collision)."""
    base = re.sub(r'[^a-z0-9]+', '_', name.lower()).strip('_') or "item"
    item_id = base
    counter = 2
    while item_id in MENU:
        item_id = f"{base}_{counter}"
        counter += 1
    return item_id


def db_is_out_of_stock(item_id: str) -> bool:
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute("SELECT status FROM stock_status WHERE item_id = ?", (item_id,)).fetchone()
    conn.close()
    return bool(row and row[0] == "out_of_stock")


def db_set_stock_status(item_id: str, out_of_stock: bool):
    status = "out_of_stock" if out_of_stock else "available"
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT INTO stock_status (item_id, status) VALUES (?, ?) "
        "ON CONFLICT(item_id) DO UPDATE SET status = excluded.status",
        (item_id, status),
    )
    conn.commit()
    conn.close()


def db_out_of_stock_items() -> set:
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute("SELECT item_id FROM stock_status WHERE status = 'out_of_stock'").fetchall()
    conn.close()
    return {r[0] for r in rows}


def db_mark_announcement_seen(user_id: int, announcement_id: int):
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT OR IGNORE INTO seen_announcements (user_id, announcement_id) VALUES (?, ?)",
        (user_id, announcement_id),
    )
    conn.commit()
    conn.close()


def db_unseen_announcement_count(user_id: int) -> int:
    """How many announcements this customer has never opened."""
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT COUNT(*) FROM announcements a "
        "WHERE NOT EXISTS (SELECT 1 FROM seen_announcements s "
        "WHERE s.user_id = ? AND s.announcement_id = a.id)",
        (user_id,),
    ).fetchone()
    conn.close()
    return row[0] if row else 0


def db_pending_order_count() -> int:
    """Units that still need admin action — used for the admin badge."""
    return len(db_all_pending_items())


def db_unread_inbox_count() -> int:
    return len(db_inbox_messages(read=False))


def db_unresolved_ticket_count() -> int:
    return len(db_list_tickets("unresolved"))


def db_add_announcement(body: str) -> int:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.execute(
        "INSERT INTO announcements (body, created_at) VALUES (?, ?)",
        (body, datetime.utcnow().isoformat()),
    )
    conn.commit()
    announcement_id = cur.lastrowid
    conn.close()
    return announcement_id


def db_list_announcements(limit: int = 20):
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT id, body, created_at FROM announcements ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    conn.close()
    return rows


def db_get_announcement(announcement_id: int):
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT id, body, created_at FROM announcements WHERE id = ?", (announcement_id,)
    ).fetchone()
    conn.close()
    return row


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


def db_finalize_serial(serial_id: int, order_id: int, success: bool, mark_not_working: bool = False):
    conn = sqlite3.connect(DB_PATH)
    if success:
        conn.execute(
            "UPDATE serials SET status = 'used', used_for_order = ?, used_at = ? WHERE id = ?",
            (order_id, datetime.utcnow().isoformat(), serial_id),
        )
    elif mark_not_working:
        # Serial was rejected by iMD — mark for admin review rather than
        # releasing it back to the pool where it would fail again.
        conn.execute(
            "UPDATE serials SET status = 'not_working' WHERE id = ?",
            (serial_id,),
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


def db_set_payment_method(order_id: int, method: str):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("UPDATE orders SET payment_method = ? WHERE id = ?", (method, order_id))
    conn.commit()
    conn.close()


def db_set_payer_name(order_id: int, name: str):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("UPDATE orders SET payer_name = ? WHERE id = ?", (name, order_id))
    conn.commit()
    conn.close()


def db_set_receipt_photo(order_id: int, photo_file_id: str):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("UPDATE orders SET receipt_photo_file_id = ? WHERE id = ?", (photo_file_id, order_id))
    conn.commit()
    conn.close()


def db_get_payment_record(order_id: int):
    """Returns (payment_method, payer_name, receipt_photo_file_id) for the
    Payments channel post."""
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT payment_method, payer_name, receipt_photo_file_id FROM orders WHERE id = ?",
        (order_id,),
    ).fetchone()
    conn.close()
    return row or (None, None, None)


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

def md_escape(text: str) -> str:
    """Escapes Telegram Markdown control characters. Product names or
    customer-supplied values containing _ * ` [ would otherwise make
    Telegram reject the entire message with a parse error."""
    if text is None:
        return ""
    for ch in ("\\", "_", "*", "`", "["):
        text = text.replace(ch, "\\" + ch)
    return text


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
    out_of_stock = db_out_of_stock_items()
    rows = []

    for cat_key, cat in CATALOG.items():
        label = cat["label"]
        if _category_fully_out_of_stock(cat, out_of_stock):
            label = f"🚫 {label} (out of stock)"
        rows.append([InlineKeyboardButton(label, callback_data=f"cat:{cat_key}")])

    imd_label = "🎓 iMD VIP"
    if IMD_TRIGGER_ITEMS <= out_of_stock:
        imd_label = "🚫 iMD VIP (out of stock)"
    rows.append([InlineKeyboardButton(imd_label, callback_data="imd_menu")])

    for item_id in SINGLE_MAIN_ITEMS:
        name, price = MENU[item_id]
        if item_id in out_of_stock:
            label = f"🚫 {name} (out of stock)"
        else:
            label = f"{name} — {CURRENCY}{price:.2f}"
        rows.append([InlineKeyboardButton(label, callback_data=f"add:{item_id}")])

    rows.append([InlineKeyboardButton("🛒 View Cart / Checkout", callback_data="view_cart")])
    return InlineKeyboardMarkup(rows)


def _category_fully_out_of_stock(node: dict, out_of_stock: set) -> bool:
    """True if every leaf under this catalog node is out of stock — used
    to flag a whole top-level category (e.g. all of Amboss) rather than
    making the customer drill in to discover nothing's available."""
    if "item" in node:
        return node["item"] in out_of_stock
    return all(_category_fully_out_of_stock(child, out_of_stock) for child in node.get("children", {}).values())


def _catalog_get_node(path: str) -> dict:
    keys = path.split(".")
    node = {"children": CATALOG}
    for k in keys:
        node = node["children"][k]
    return node


async def catalog_navigate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Generic browser for the CATALOG tree — handles Uptodate, Amboss, and
    any future multi-level product without needing a dedicated handler
    per product, the way the iMD sub-menu currently does."""
    query = update.callback_query
    await query.answer()
    path = query.data.split(":", 1)[1]
    node = _catalog_get_node(path)
    out_of_stock = db_out_of_stock_items()

    buttons = []
    for child_key, child in node.get("children", {}).items():
        if "item" in child:
            item_id = child["item"]
            name, price = MENU[item_id]
            if item_id in out_of_stock:
                label = f"🚫 {child['label']} (out of stock)"
            else:
                label = f"{child['label']} — {CURRENCY}{price:.2f}"
            buttons.append([InlineKeyboardButton(label, callback_data=f"add:{item_id}")])
        else:
            child_path = f"{path}.{child_key}"
            label = child["label"]
            if _category_fully_out_of_stock(child, out_of_stock):
                label = f"🚫 {label} (out of stock)"
            buttons.append([InlineKeyboardButton(label, callback_data=f"cat:{child_path}")])

    parent = f"cat:{path.rsplit('.', 1)[0]}" if "." in path else "back_to_menu"
    buttons.append([InlineKeyboardButton("⬅️ Back", callback_data=parent)])

    await query.edit_message_text(
        f"{node['label']} — choose an option:", reply_markup=InlineKeyboardMarkup(buttons)
    )


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
    out_of_stock = db_out_of_stock_items()

    def label_for(item_id, base_label, price):
        if item_id in out_of_stock:
            return f"🚫 {base_label} (out of stock)"
        return f"{base_label} — {CURRENCY}{price:.2f}"

    buttons = [
        [InlineKeyboardButton(label_for(six_month_id, "6 Months", six_month_price), callback_data=f"add:{six_month_id}")],
        [InlineKeyboardButton(label_for(one_year_id, "1 Year", one_year_price), callback_data=f"add:{one_year_id}")],
        [InlineKeyboardButton("⬅️ Back", callback_data="imd_menu")],
    ]
    label = "New Account" if imd_type == "new" else "Renew Previous Account"
    await query.edit_message_text(f"iMD VIP — {label}. Choose a duration:", reply_markup=InlineKeyboardMarkup(buttons))


# Persistent bottom keyboard labels (must match exactly between the keyboard
# and the handler that checks incoming text against them).
BUY_LABEL = "🛒 Buy New Subscription"
MY_SUBS_LABEL = "📋 My Subscriptions"
BASKET_LABEL = "🧺 Check the Basket and Pay"
ANNOUNCEMENTS_LABEL = "📢 Announcements"
JOIN_CHANNEL_LABEL = "📡 Join Channel"
TICKET_LABEL = "🎫 Send a Ticket"
IMD_SEARCH_LABEL = "🔬 Search iMD Resources"
GET_FREE_LABEL = "🎁 Get Free Accounts"
MY_CREDITS_LABEL = "💳 My Credits"
BOOK_REQUEST_LABEL = "📚 Request a Book"
SUPPORT_LABEL = "🆘 Support"

# Admin-only panel labels.
A_VIEW_SERIALS = "🔑 View Serials"
A_ADD_SERIALS = "➕ Add Serials"
A_REMOVE_SERIAL = "🗑 Remove Serial"
A_RECENT_ORDERS = "📊 Recent Orders"
A_PENDING = "⏳ Pending Orders"
A_INPUT = "📈 Input"
A_INBOX = "📥 Inbox"
A_BROADCAST = "📢 Broadcast"
A_FIND_ORDER = "🔎 Find Order"
A_FIND_CUSTOMER = "👤 Find Customer"
A_STOCK = "📦 Manage Stock"
A_TICKETS = "🎫 Tickets"
A_CREDITS = "💳 Customer Credits"
A_ADD_SUBSCRIPTION = "➕ Add New Subscription"
A_BOOK_REQUESTS = "📚 Book Requests"
A_DELIVERED = "📦 Delivered Subscriptions"
A_IMD_CATALOG = "🔬 Update iMD Catalog"
A_CUSTOMER_VIEW = "🛍 Customer Menu"

ADMIN_LABELS = [
    A_VIEW_SERIALS, A_ADD_SERIALS, A_REMOVE_SERIAL,
    A_RECENT_ORDERS, A_PENDING, A_INPUT, A_INBOX, A_BROADCAST,
    A_FIND_ORDER, A_FIND_CUSTOMER, A_STOCK, A_TICKETS, A_CREDITS,
    A_ADD_SUBSCRIPTION, A_BOOK_REQUESTS, A_DELIVERED, A_IMD_CATALOG, A_CUSTOMER_VIEW,
]

# Every key the admin's text_state_router uses. clear_admin_flow_state()
# must be called at the start of every new admin interactive flow so a
# stale earlier state never intercepts input meant for the new one.
ADMIN_FLOW_KEYS = [
    "awaiting_admin_input",
    "awaiting_comment_for_order",
    "awaiting_ticket_resolution",
    "awaiting_credentials_fulfilment",
    "awaiting_admin_message_for_order",
    "awaiting_new_sub_field", "new_sub_data",
    "awaiting_book_price_for",
    "awaiting_imd_catalog_username", "awaiting_imd_catalog_password",
    "awaiting_imd_session_token", "imd_extract_username",
    "awaiting_imd_api_url", "imd_saved_token",
    "awaiting_imd_diag_username", "awaiting_imd_diag_password", "imd_diag_username",
]

def clear_admin_flow_state(user_data: dict):
    for key in ADMIN_FLOW_KEYS:
        user_data.pop(key, None)


def _badge(label: str, count: int) -> str:
    """Appends a 🔴 and the count to a button label when there's something
    new to see — the simplest possible notification that works inside a
    ReplyKeyboardMarkup, which doesn't support inline markup or colours."""
    return f"{label} 🔴{count}" if count > 0 else label


def _shop_url(user_id: int = 0) -> str:
    """Builds the Mini App URL. When user_id is known, subscription data and
    custom admin-added items are encoded as a base64 query param (?subs=...)
    so the Mini App can display them without any HTTP API call."""
    if not MINI_APP_URL:
        return ""

    def _encode(data: dict) -> str:
        import base64 as _b64
        return _b64.urlsafe_b64encode(
            json.dumps(data, separators=(",", ":")).encode()
        ).decode().rstrip("=")

    # Always include custom catalog items so the Mini App catalog stays in sync
    try:
        custom = [[item_id, name, float(price)] for item_id, name, price in db_load_custom_items()]
    except Exception:
        custom = []

    if not user_id:
        if custom:
            try:
                return f"{MINI_APP_URL}?subs={_encode({'d':[],'p':[],'c':custom})}"
            except Exception:
                pass
        return MINI_APP_URL

    try:
        conn = sqlite3.connect(DB_PATH)
        # Deliveries with credentials message for the "tap to see details" feature
        del_rows = conn.execute(
            "SELECT id, item_id, delivered_at, message FROM deliveries "
            "WHERE user_id = ? ORDER BY id DESC",
            (user_id,),
        ).fetchall()
        # Pending: include fulfilment_id and order_id so the Mini App can
        # send the right resume action back to the bot
        pend_rows = conn.execute(
            "SELECT f.id, f.order_id, f.item_id, f.state, o.status FROM fulfilment f "
            "JOIN orders o ON o.id = f.order_id "
            "WHERE f.user_id = ? AND f.state != 'delivered' "
            "AND o.status NOT IN ('cancelled','rejected')",
            (user_id,),
        ).fetchall()
        # Items marked out-of-stock by the admin
        oos_rows = conn.execute(
            "SELECT item_id FROM stock_status WHERE status = 'out_of_stock'"
        ).fetchall()
        conn.close()

        oos_ids = [r[0] for r in oos_rows]

        delivered = [
            [MENU.get(iid, (iid,))[0], dat[:10] if dat else "", msg or ""]
            for _, iid, dat, msg in del_rows
        ]
        pending = [
            # If the ORDER is awaiting a receipt, show that regardless of
            # what the fulfilment row says (Mini App pre-stores credentials
            # and sets awaiting_delivery, but receipt still hasn't arrived).
            [MENU.get(iid, (iid,))[0],
             "awaiting_receipt" if ostatus == "awaiting_receipt" else fstate,
             fid, oid]
            for fid, oid, iid, fstate, ostatus in pend_rows
        ]
        # Custom items — exclude out-of-stock ones so the Mini App catalog
        # automatically stays in sync with the admin's stock toggles
        custom_filtered = [
            [item_id, name, float(price)]
            for item_id, name, price in db_load_custom_items()
            if item_id not in oos_ids
        ]
        data = {
            "d": delivered,
            "p": pending,
            "c": custom_filtered,
            "oos": oos_ids,
        }
        return f"{MINI_APP_URL}?subs={_encode(data)}"
    except Exception:
        logger.exception("_shop_url: failed to encode subscription data for user %s", user_id)
        if custom:
            try:
                return f"{MINI_APP_URL}?subs={_encode({'d':[],'p':[],'c':custom})}"
            except Exception:
                pass
        return MINI_APP_URL


def main_menu_keyboard(user_id: int = 0) -> ReplyKeyboardMarkup:
    """Persistent customer keyboard. When the Mini App is configured the
    🏪 Shop button is full-width and first; the catalog-only buttons
    (Buy, Basket) are removed since they're inside the app. My Subscriptions
    stays on its own row in both modes so it's always easy to find."""
    ann_count = db_unseen_announcement_count(user_id) if user_id else 0
    shop_url  = _shop_url(user_id)

    if shop_url:
        return ReplyKeyboardMarkup(
            [
                [KeyboardButton("🏪 Shop", web_app=WebAppInfo(url=shop_url))],
                [MY_SUBS_LABEL],
                [_badge(ANNOUNCEMENTS_LABEL, ann_count), JOIN_CHANNEL_LABEL],
                [GET_FREE_LABEL, MY_CREDITS_LABEL],
                [BOOK_REQUEST_LABEL, TICKET_LABEL],
                [IMD_SEARCH_LABEL],
                [SUPPORT_LABEL],
            ],
            resize_keyboard=True,
        )
    else:
        return ReplyKeyboardMarkup(
            [
                [BUY_LABEL],
                [MY_SUBS_LABEL],
                [BASKET_LABEL],
                [_badge(ANNOUNCEMENTS_LABEL, ann_count), JOIN_CHANNEL_LABEL],
                [GET_FREE_LABEL, MY_CREDITS_LABEL],
                [BOOK_REQUEST_LABEL, TICKET_LABEL],
                [IMD_SEARCH_LABEL],
                [SUPPORT_LABEL],
            ],
            resize_keyboard=True,
        )


def admin_menu_keyboard() -> ReplyKeyboardMarkup:
    """The admin's persistent panel. Badges Pending Orders, Inbox, and
    Tickets with live counts whenever there's something waiting — the
    keyboard is rebuilt on demand so the count is always current."""
    pending = db_pending_order_count()
    inbox   = db_unread_inbox_count()
    tickets = db_unresolved_ticket_count()
    return ReplyKeyboardMarkup(
        [
            [A_VIEW_SERIALS, A_ADD_SERIALS],
            [A_REMOVE_SERIAL, A_RECENT_ORDERS],
            [_badge(A_PENDING, pending), A_INPUT],
            [_badge(A_INBOX, inbox), A_BROADCAST],
            [A_FIND_ORDER, A_FIND_CUSTOMER],
            [A_STOCK, _badge(A_TICKETS, tickets)],
            [A_CREDITS, A_ADD_SUBSCRIPTION],
            [A_BOOK_REQUESTS],
            [A_DELIVERED],
            [A_IMD_CATALOG],
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
            ],
            [InlineKeyboardButton("💬 Send Comment", callback_data=f"admin_comment:{order_id}")],
        ]
    )


# ------------------------------------------------------------------
# HANDLERS — customer side
# ------------------------------------------------------------------

async def get_bot_username(context: ContextTypes.DEFAULT_TYPE) -> str:
    """Cached lookup of the bot's own @username, needed to build referral
    deep links (https://t.me/<username>?start=ref_<id>)."""
    cached = context.application.bot_data.get("bot_username")
    if cached:
        return cached
    me = await context.bot.get_me()
    context.application.bot_data["bot_username"] = me.username
    return me.username


def referral_link(bot_username: str, user_id: int) -> str:
    return f"https://t.me/{bot_username}?start=ref_{user_id}"


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.setdefault("cart", {})
    user_id = update.effective_user.id
    is_new = not db_user_exists(user_id)
    db_upsert_user(user_id, update.effective_user.username)

    # Deep-link referral: /start ref_<referrer_id>. Only attributed for a
    # genuinely new user — an existing user tapping someone else's link
    # later doesn't retroactively change who referred them.
    if is_new and context.args:
        arg = context.args[0]
        if arg.startswith("ref_"):
            try:
                referrer_id = int(arg[4:])
                db_set_referred_by(user_id, referrer_id)
            except ValueError:
                pass

    if update.effective_user.id == ADMIN_CHAT_ID:
        await update.message.reply_text(
            "Admin panel — use the buttons below any time.",
            reply_markup=admin_menu_keyboard(),
        )
        return
    await update.message.reply_text(
        "Welcome! Use the buttons below any time.",
        reply_markup=main_menu_keyboard(update.effective_user.id),
    )


async def main_menu_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles taps on the persistent bottom keyboard: Buy, My Subscriptions,
    Support. Strips the live badge suffix before routing so a badged
    Announcements button still routes correctly."""
    text = (update.message.text or "").split(" 🔴")[0].strip()

    if text == BUY_LABEL:
        context.user_data.setdefault("cart", {})
        await update.message.reply_text(
            "Tap an item below to add it to your order.", reply_markup=menu_keyboard()
        )

    elif text == MY_SUBS_LABEL:
        await update.message.reply_text("What would you like to see?", reply_markup=subs_menu_keyboard())

    elif text == BASKET_LABEL:
        cart = context.user_data.setdefault("cart", {})
        await update.message.reply_text(
            format_cart(cart), parse_mode=ParseMode.MARKDOWN, reply_markup=cart_keyboard(cart)
        )

    elif text == ANNOUNCEMENTS_LABEL:
        await show_announcements(update, context)

    elif text == JOIN_CHANNEL_LABEL:
        await update.message.reply_text(
            "Tap below to join our channel:",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("Open Channel", url="https://t.me/uptodate_accounts")]]
            ),
        )

    elif text == TICKET_LABEL:
        await show_ticket_menu(update, context)

    elif text == GET_FREE_LABEL:
        await show_get_free_accounts(update, context)

    elif text == MY_CREDITS_LABEL:
        await show_my_credits(update, context)

    elif text == BOOK_REQUEST_LABEL:
        context.user_data["awaiting_book_link"] = True
        await update.message.reply_text("Please send the link of the book you're looking for:")

    elif text == IMD_SEARCH_LABEL:
        await imd_search_start(update, context)

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


def announcements_keyboard():
    rows = db_list_announcements(limit=20)
    if not rows:
        return None
    buttons = []
    for announcement_id, body, created_at in rows:
        preview = body.replace("\n", " ")[:40]
        buttons.append(
            [InlineKeyboardButton(f"{created_at[:10]} — {preview}"[:60], callback_data=f"annview:{announcement_id}")]
        )
    return InlineKeyboardMarkup(buttons)


async def show_announcements(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Customer-facing list of past broadcasts. Sends an updated keyboard
    alongside so the badge clears immediately as they open the list."""
    user_id = update.effective_user.id
    keyboard = announcements_keyboard()
    if not keyboard:
        await update.message.reply_text(
            "No announcements yet.",
            reply_markup=main_menu_keyboard(user_id),
        )
        return
    await update.message.reply_text(
        "📢 Announcements — tap one to read it:",
        reply_markup=keyboard,
    )
    # Push a refreshed keyboard so the badge disappears at the moment the
    # customer opens the list — even before they read individual items.
    # We mark all existing announcements seen at the moment they tap the
    # tab, since by opening the list they've at least been informed.
    conn = __import__('sqlite3').connect(DB_PATH)
    rows = conn.execute("SELECT id FROM announcements").fetchall()
    conn.close()
    for (ann_id,) in rows:
        db_mark_announcement_seen(user_id, ann_id)
    # Send the refreshed keyboard (badge now 0) as a silent update.
    await update.message.reply_text(
        "👆 Tap any announcement above to read it in full.",
        reply_markup=main_menu_keyboard(user_id),
    )


async def announcement_detail(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    announcement_id = int(query.data.split(":", 1)[1])
    db_mark_announcement_seen(query.from_user.id, announcement_id)

    row = db_get_announcement(announcement_id)
    back = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="ann_back")]])

    if not row:
        await query.edit_message_text("That announcement no longer exists.", reply_markup=back)
        return

    _, body, created_at = row
    await query.edit_message_text(f"📢 {created_at[:10]}\n\n{body}", reply_markup=back)


async def announcement_back(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    keyboard = announcements_keyboard()
    if not keyboard:
        await query.edit_message_text("No announcements yet.")
        return
    await query.edit_message_text("📢 Announcements — tap one to read it:", reply_markup=keyboard)


GET_FREE_ACCOUNTS_INTRO = (
    "By sharing the bot with your friends, you can earn credits and use them to get free accounts.\n\n"
    "It's so Easy, proceed to get a personalised link of the bot, share it with your friends. "
    "Whenever your friends do a purchase through the bot link you sent to them you will earn 1 Credit.\n\n"
    "Each Credit = 4 USD\n\n"
    "Whenever your credits are equal to the price of any of the subscriptions we sell, you can get it for free."
)


async def show_get_free_accounts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """First visit ever: shows the full explanation + 'Want to proceed?'.
    Every visit after that: shows the link directly, per spec."""
    user_id = update.effective_user.id
    if db_referral_intro_shown(user_id):
        await send_referral_link(context, user_id, update.effective_chat.id)
        return

    await update.message.reply_text(
        GET_FREE_ACCOUNTS_INTRO,
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("Want to proceed?", callback_data="getfree_proceed")]]
        ),
    )


async def getfree_proceed(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Customer tapped 'Want to proceed?' — mark the intro as seen (so
    future visits skip straight to the link) and show it now."""
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    db_mark_referral_intro_shown(user_id)
    await send_referral_link(context, user_id, query.message.chat_id, edit_query=query)


async def send_referral_link(context: ContextTypes.DEFAULT_TYPE, user_id: int, chat_id: int, edit_query=None):
    """The actual link + caption, reused by both the first-time 'proceed'
    flow and every subsequent tap of the Get Free Accounts tab."""
    bot_username = await get_bot_username(context)
    link = referral_link(bot_username, user_id)
    text = (
        f"{link}\n\n"
        "This is your personalised link, share it and earn credits.\n\n"
        "You can check your credits from the tab"
    )
    buttons = InlineKeyboardMarkup([[InlineKeyboardButton("💳 My Credits", callback_data="goto_my_credits")]])

    if edit_query:
        await edit_query.edit_message_text(text, reply_markup=buttons)
    else:
        await context.bot.send_message(chat_id=chat_id, text=text, reply_markup=buttons)


async def goto_my_credits(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """The 'My Credits' button shown inside Get Free Accounts (and the
    insufficient-credits message) — takes the customer to that page."""
    query = update.callback_query
    await query.answer()
    await send_my_credits(context, query.from_user.id, query.message.chat_id, edit_query=query)


async def show_my_credits(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_my_credits(context, update.effective_user.id, update.effective_chat.id)


async def send_my_credits(context: ContextTypes.DEFAULT_TYPE, user_id: int, chat_id: int, edit_query=None):
    credits = db_get_credits(user_id)
    usd = credits * 4
    bot_username = await get_bot_username(context)
    link = referral_link(bot_username, user_id)

    text = (
        f"💳 Your Credits: {credits}\n"
        f"Equivalent: {CURRENCY}{usd}\n\n"
        f"Your personalised link:\n`{link}`\n\n"
        "Share more to earn more. The credits will be added whenever someone starts the bot "
        "using your personalised link and buys any subscription — you'll earn 1 credit every "
        "time they purchase, and you'll be notified when it happens."
    )

    if edit_query:
        await edit_query.edit_message_text(text, parse_mode=ParseMode.MARKDOWN)
    else:
        await context.bot.send_message(chat_id=chat_id, text=text, parse_mode=ParseMode.MARKDOWN)


async def book_link_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Catches the link the customer sends after tapping Request a Book."""
    if not context.user_data.get("awaiting_book_link"):
        return
    context.user_data.pop("awaiting_book_link", None)

    link = update.message.text.strip()
    user_id = update.effective_user.id
    username = update.effective_user.username

    request_id = db_create_book_request(user_id, username, link)
    await update.message.reply_text("Thanks! Please wait for an update from us regarding pricing.")

    if ADMIN_CHAT_ID:
        who = f"@{username}" if username else str(user_id)
        await context.bot.send_message(
            chat_id=ADMIN_CHAT_ID,
            text=f"📚 New book request #{request_id} — {who}\n\n{link}",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("💰 Set Price", callback_data=f"bookprice:{request_id}")]]
            ),
        )


async def book_price_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin tapped '💰 Set Price' — wait for the price."""
    query = update.callback_query
    if query.from_user.id != ADMIN_CHAT_ID:
        await query.answer("Not authorized.", show_alert=True)
        return
    await query.answer()

    request_id = int(query.data.split(":", 1)[1])
    clear_admin_flow_state(context.user_data)
    context.user_data["awaiting_book_price_for"] = request_id
    await context.bot.send_message(
        chat_id=ADMIN_CHAT_ID, text=f"Enter the price in USD for book request #{request_id}:"
    )


async def book_price_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin's typed price — sends the customer the price with Proceed/Cancel."""
    request_id = context.user_data.pop("awaiting_book_price_for", None)
    if not request_id:
        return

    try:
        price = float(update.message.text.strip())
        if price <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("Please send a valid positive number for the price:")
        context.user_data["awaiting_book_price_for"] = request_id
        return

    request = db_get_book_request(request_id)
    if not request:
        await update.message.reply_text("That book request no longer exists.")
        return
    _, user_id, username, link, _, status, order_id, created_at, priced_at, resolved_at = request

    db_set_book_price(request_id, price)
    await update.message.reply_text(f"✅ Price set — the customer has been notified.")

    await context.bot.send_message(
        chat_id=user_id,
        text=(
            f"📚 Your book request has a price!\n\n"
            f"Link: {link}\n"
            f"Price: {CURRENCY}{price:.2f}\n\n"
            "Would you like to proceed?"
        ),
        reply_markup=InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("✅ Proceed with Payment", callback_data=f"bookproceed:{request_id}")],
                [InlineKeyboardButton("❌ Cancel Request", callback_data=f"bookcancel:{request_id}")],
            ]
        ),
    )


async def book_proceed(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Customer tapped 'Proceed with Payment' — creates a one-off order for
    this exact price and hands off to the normal checkout screen, reusing
    every existing payment method (Stars/Card/local/crypto/Credits)."""
    query = update.callback_query
    await query.answer()
    request_id = int(query.data.split(":", 1)[1])

    request = db_get_book_request(request_id)
    if not request:
        await query.edit_message_text("This book request no longer exists.")
        return
    _, user_id, username, link, price, status, order_id, created_at, priced_at, resolved_at = request

    if status != "priced":
        await query.edit_message_text("This request isn't awaiting payment anymore.")
        return

    item_id = f"book_{request_id}"
    # Deliberately NOT embedding the raw link here — checkout_view renders
    # this with parse_mode=MARKDOWN, and a link containing underscores,
    # asterisks, or brackets would break Telegram's parser (this bot has
    # hit that exact failure mode before). The customer already knows
    # which book they asked for; the reference number is enough context.
    item_name = f"Requested Book #{request_id}"

    db_add_book_menu_item(item_id, item_name, price, request_id)
    MENU[item_id] = (item_name, price)  # in-memory now; NOT added to SINGLE_MAIN_ITEMS —
    # private to this customer's order, never shown in the general catalog.

    new_order_id = db_create_order(user_id, username, {item_id: 1}, price)
    db_set_book_status(request_id, "ordered", order_id=new_order_id)

    text, keyboard = checkout_view(new_order_id)
    await query.edit_message_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard)


async def book_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Customer tapped 'Cancel Request'."""
    query = update.callback_query
    await query.answer()
    request_id = int(query.data.split(":", 1)[1])
    db_set_book_status(request_id, "cancelled")
    await query.edit_message_text("Your book request has been cancelled.")


async def show_ticket_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Top of the ticket flow: the customer's bought subscriptions (each
    routes to a tailored set of choices) plus three generic options."""
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT id, item_id, delivered_at FROM deliveries WHERE user_id = ? ORDER BY id DESC",
        (update.effective_user.id,),
    ).fetchall()
    conn.close()

    buttons = []
    for delivery_id, item_id, delivered_at in rows:
        name = MENU.get(item_id, (item_id,))[0] if item_id else f"Delivery #{delivery_id}"
        label = name + (f" ({delivered_at[:10]})" if delivered_at else "")
        buttons.append([InlineKeyboardButton(label[:60], callback_data=f"ticketsub:{delivery_id}")])

    buttons.append([InlineKeyboardButton("💳 My payment has not been approved yet", callback_data="ticketgen:payment_not_approved")])
    buttons.append([InlineKeyboardButton("📦 My subscription has not been delivered yet", callback_data="ticketgen:not_delivered")])
    buttons.append([InlineKeyboardButton("❓ Other", callback_data="ticketgen:other")])

    await update.message.reply_text(
        "🎫 What's this about?", reply_markup=InlineKeyboardMarkup(buttons)
    )


async def ticket_generic_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """One of the three top-level generic options — straight to the
    message + screenshot collection."""
    query = update.callback_query
    await query.answer()
    category = query.data.split(":", 1)[1]

    category_labels = {
        "payment_not_approved": "My payment has not been approved yet",
        "not_delivered": "My subscription has not been delivered yet",
        "other": "Other",
    }
    ticket_id = db_create_ticket(
        update.effective_user.id, query.from_user.username, category,
        subscription_label=category_labels.get(category),
    )
    await start_ticket_message_collection(context, query.from_user.id, ticket_id, query.message.chat_id)


async def ticket_subscription_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Customer picked one of their bought subscriptions — routes to
    Uptodate-specific, iMD-specific, or generic default choices."""
    query = update.callback_query
    await query.answer()
    delivery_id = int(query.data.split(":", 1)[1])

    conn = sqlite3.connect(DB_PATH)
    row = conn.execute("SELECT item_id FROM deliveries WHERE id = ?", (delivery_id,)).fetchone()
    conn.close()
    item_id = row[0] if row else None
    name = MENU.get(item_id, (item_id or "this subscription",))[0]

    context.user_data["ticket_sub_item_id"] = item_id
    context.user_data["ticket_sub_label"] = name
    context.user_data["ticket_sub_delivery_id"] = delivery_id

    if item_id in IMD_TRIGGER_ITEMS:
        buttons = [
            [InlineKeyboardButton("🔑 I can't login (username/password error)", callback_data="ticketimd:login")],
            [InlineKeyboardButton("⬇️ I can't download resources inside the app", callback_data="ticketimd:download")],
        ]
    elif item_id in UPTODATE_TICKET_ITEMS:
        buttons = [
            [InlineKeyboardButton("🔄 My Account needs Renewal", callback_data="ticketup:renewal")],
            [InlineKeyboardButton("🔑 I can't login", callback_data="ticketup:login")],
            [InlineKeyboardButton("❓ Other", callback_data="ticketup:other")],
        ]
    else:
        buttons = [
            [InlineKeyboardButton("🔑 I can't login", callback_data="ticketother:login")],
            [InlineKeyboardButton("❓ Other", callback_data="ticketother:other")],
        ]

    await query.edit_message_text(f"🎫 {name} — what's the issue?", reply_markup=InlineKeyboardMarkup(buttons))


async def ticket_uptodate_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Uptodate sub-choice picked — straight to message + screenshot."""
    query = update.callback_query
    await query.answer()
    choice = query.data.split(":", 1)[1]
    labels = {"renewal": "My Account needs Renewal", "login": "I can't login", "other": "Other"}
    category = f"uptodate_{choice}"

    item_id = context.user_data.pop("ticket_sub_item_id", None)
    label = context.user_data.pop("ticket_sub_label", None)
    context.user_data.pop("ticket_sub_delivery_id", None)

    ticket_id = db_create_ticket(
        query.from_user.id, query.from_user.username, category,
        subscription_item_id=item_id, subscription_label=label,
    )
    await query.edit_message_text(f"🎫 {labels[choice]} — {label}")
    await start_ticket_message_collection(context, query.from_user.id, ticket_id, query.message.chat_id)


async def ticket_other_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Generic-product sub-choice (anything that isn't Uptodate or iMD)."""
    query = update.callback_query
    await query.answer()
    choice = query.data.split(":", 1)[1]
    labels = {"login": "I can't login", "other": "Other"}
    category = f"generic_{choice}"

    item_id = context.user_data.pop("ticket_sub_item_id", None)
    label = context.user_data.pop("ticket_sub_label", None)
    context.user_data.pop("ticket_sub_delivery_id", None)

    ticket_id = db_create_ticket(
        query.from_user.id, query.from_user.username, category,
        subscription_item_id=item_id, subscription_label=label,
    )
    await query.edit_message_text(f"🎫 {labels[choice]} — {label}")
    await start_ticket_message_collection(context, query.from_user.id, ticket_id, query.message.chat_id)


async def ticket_imd_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """iMD sub-choice — shows the relevant self-help steps, then asks if
    that resolved it."""
    query = update.callback_query
    await query.answer()
    choice = query.data.split(":", 1)[1]

    item_id = context.user_data.pop("ticket_sub_item_id", None)
    label = context.user_data.pop("ticket_sub_label", None)
    context.user_data.pop("ticket_sub_delivery_id", None)

    if choice == "login":
        category = "imd_login"
        self_help = (
            "Retrieve your password from here\n"
            f"{IMD_FORGOT_PASSWORD_URL}\n\n"
            "Then if you like, change it from here\n"
            f"{IMD_CHANGE_PASSWORD_URL}"
        )
    else:
        category = "imd_download"
        self_help = (
            "Make sure you have the latest version of the app.\n\n"
            f"Android: {IMD_APK_URL}\n\n"
            "If you're using iOS/macOS (no app available), use the web version instead: "
            f"{IMD_WEB_URL}"
        )

    ticket_id = db_create_ticket(
        query.from_user.id, query.from_user.username, category,
        subscription_item_id=item_id, subscription_label=label, self_help=self_help,
    )
    context.user_data["ticket_awaiting_resolved_check"] = ticket_id

    await query.edit_message_text(
        self_help,
        reply_markup=InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("✅ Yes, resolved", callback_data=f"ticketresolved:{ticket_id}:yes")],
                [InlineKeyboardButton("❌ No, still an issue", callback_data=f"ticketresolved:{ticket_id}:no")],
            ]
        ),
    )


async def ticket_resolved_check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Customer answered whether the iMD self-help steps worked."""
    query = update.callback_query
    await query.answer()
    _, ticket_id_str, answer = query.data.split(":", 2)
    ticket_id = int(ticket_id_str)
    context.user_data.pop("ticket_awaiting_resolved_check", None)

    if answer == "yes":
        db_resolve_ticket(ticket_id, "Resolved via self-service — customer confirmed the issue is fixed.")
        await query.edit_message_text("✅ Great, glad that fixed it! Your ticket has been closed.")
        if ADMIN_CHAT_ID:
            ticket = db_get_ticket(ticket_id)
            who = f"@{ticket[2]}" if ticket[2] else str(ticket[1])
            await context.bot.send_message(
                chat_id=ADMIN_CHAT_ID,
                text=f"✅ Ticket #{ticket_id} ({who}) — self-resolved via the automated steps. No action needed.",
            )
        return

    await query.edit_message_text("Sorry that didn't fix it — let's get you help.")
    await start_ticket_message_collection(context, query.from_user.id, ticket_id, query.message.chat_id)


# Every key that belongs to a customer-side "collection" flow — registration
# (iMD), generic (non-iMD subscriptions), and payment receipt follow-up.
# When a ticket starts, any of these still active get stashed and cleared
# so the ticket's own "message" then "screenshot" steps can't be confused
# with them; they're restored once the ticket is fully submitted.
COLLECTION_STATE_KEYS = [
    "awaiting_registration_field", "registration_order", "registration_data",
    "registration_is_renew", "registration_duration", "registration_item_id",
    "registration_fulfilment_id", "registration_retry_field",
    "awaiting_generic_field", "generic_data", "generic_order_id",
    "generic_item_id", "generic_fulfilment_id", "generic_account_type",
    "awaiting_payer_name_for_order", "pending_receipt_photo", "awaiting_receipt_for_order",
    "awaiting_book_link",
]


def stash_collection_state(user_data: dict) -> bool:
    """Saves and clears any in-progress registration/generic/receipt flow.
    Returns True if anything was actually paused."""
    stash = {}
    for key in COLLECTION_STATE_KEYS:
        if key in user_data:
            stash[key] = user_data.pop(key)
    if stash:
        user_data["stashed_collection_state"] = stash
        return True
    return False


async def restore_collection_state(context: ContextTypes.DEFAULT_TYPE, user_data: dict, chat_id: int):
    """Restores a previously stashed flow (if any) and re-sends the prompt
    for whatever field the customer was on, so they can pick up exactly
    where they left off after finishing a ticket."""
    stash = user_data.pop("stashed_collection_state", None)
    if not stash:
        return
    user_data.update(stash)

    reg_field = stash.get("awaiting_registration_field")
    if reg_field:
        prompt = IMD_FIELD_PROMPTS.get(reg_field, "Please send the requested information:")
        await context.bot.send_message(chat_id=chat_id, text=f"Let's continue where we left off.\n\n{prompt}")
        return

    gen_field = stash.get("awaiting_generic_field")
    if gen_field:
        prompt = GENERIC_FIELD_PROMPTS.get(gen_field, "Please send the requested information:")
        await context.bot.send_message(chat_id=chat_id, text=f"Let's continue where we left off.\n\n{prompt}")
        return

    # Was stuck at the New Account / Renewal question itself (before
    # answering, so no awaiting_generic_field was set yet).
    if stash.get("generic_fulfilment_id") and not stash.get("awaiting_generic_field"):
        item_id = stash.get("generic_item_id")
        item_name = MENU.get(item_id, (item_id, 0))[0]
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"Let's continue where we left off. 📝 {item_name}. Is this a:",
            reply_markup=InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton("🆕 New Account", callback_data="gentype:new")],
                    [InlineKeyboardButton("🔄 Renewal of a previous account", callback_data="gentype:renew")],
                ]
            ),
        )
        return

    if stash.get("awaiting_receipt_for_order"):
        await context.bot.send_message(
            chat_id=chat_id,
            text="Let's continue where we left off — please upload the photo of your payment receipt:",
        )
    elif stash.get("awaiting_payer_name_for_order"):
        await context.bot.send_message(
            chat_id=chat_id,
            text="Let's continue where we left off — please send the Full Name of the person who did the payment:",
        )


async def start_ticket_message_collection(context: ContextTypes.DEFAULT_TYPE, user_id: int, ticket_id: int, chat_id: int):
    customer_data = context.application.user_data[user_id]
    stash_collection_state(customer_data)
    customer_data["awaiting_ticket_field"] = "message"
    customer_data["ticket_id"] = ticket_id
    await context.bot.send_message(
        chat_id=chat_id,
        text="Please send a message describing the problem:",
    )


async def ticket_field_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Collects the ticket's text description, then asks for a screenshot."""
    field = context.user_data.get("awaiting_ticket_field")
    if field != "message":
        return

    ticket_id = context.user_data.get("ticket_id")
    db_set_ticket_message(ticket_id, message=update.message.text)
    context.user_data["awaiting_ticket_field"] = "screenshot"
    await update.message.reply_text("Thanks — now please send a screenshot:")


async def ticket_screenshot_reply(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Collects the ticket's screenshot and notifies the admin. Returns
    True if it handled the photo (so the generic receipt-photo handler
    knows not to also try)."""
    if context.user_data.get("awaiting_ticket_field") != "screenshot":
        return False

    ticket_id = context.user_data.pop("ticket_id", None)
    context.user_data.pop("awaiting_ticket_field", None)
    photo_file_id = update.message.photo[-1].file_id
    db_set_ticket_message(ticket_id, photo_file_id=photo_file_id)

    await update.message.reply_text(
        f"✅ Ticket #{ticket_id} submitted. We'll get back to you as soon as possible."
    )

    if ADMIN_CHAT_ID:
        ticket = db_get_ticket(ticket_id)
        (_, user_id, username, category, sub_item_id, sub_label, self_help,
         message, photo, status, resolution, created_at, resolved_at) = ticket
        who = f"@{username}" if username else str(user_id)
        caption = (
            f"🎫 New ticket #{ticket_id} — {who}\n"
            f"Category: {category}\n"
            + (f"Subscription: {sub_label}\n" if sub_label else "")
            + (f"\nSelf-help shown:\n{self_help}\n" if self_help else "")
            + f"\nMessage:\n{message}"
        )
        await context.bot.send_photo(
            chat_id=ADMIN_CHAT_ID,
            photo=photo_file_id,
            caption=caption,
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("✅ Mark Resolved", callback_data=f"ticketresolve:{ticket_id}")]]
            ),
        )
        # Push updated keyboard so the Tickets badge appears immediately.
        await refresh_admin_keyboard(context)

    await restore_collection_state(context, context.user_data, update.effective_chat.id)
    return True


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
                f"⏳ *{md_escape(name)}* (Order #{order_id})\n\n"
                "We've received your receipt and are verifying your payment. "
                "You'll hear from us as soon as it's confirmed."
            )
        else:
            text = (
                f"⏳ *{md_escape(name)}* (Order #{order_id})\n\n"
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
            prompt = GENERIC_FIELD_PROMPTS.get(gen_field, "Please send the requested information:")
        else:
            # Nothing in progress for this unit — restart its collection.
            await query.edit_message_text(
                f"📝 *{md_escape(name)}* (Order #{order_id})\n\nWe still need some details from you.",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=back,
            )
            await process_next_in_queue(context, query.from_user.id, order_id)
            return

        await query.edit_message_text(
            f"📝 *{md_escape(name)}* (Order #{order_id})\n\nWe still need some information from you.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=back,
        )
        await context.bot.send_message(chat_id=query.from_user.id, text=prompt)
        return

    await query.edit_message_text(
        f"⏳ *{md_escape(name)}* (Order #{order_id})\n\n"
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
        item_id = data.split(":", 1)[1]

        if db_is_out_of_stock(item_id):
            await query.answer("🚫 Out of stock — try again later.", show_alert=True)
            return

        cart[item_id] = cart.get(item_id, 0) + 1
        name = MENU.get(item_id, (item_id,))[0]
        await query.answer()

        # Put the confirmation in the message body rather than a toast — a
        # toast flashes briefly at the top of the screen and is easy to
        # miss, while this sits right above the buttons the customer is
        # already looking at.
        total_units = sum(cart.values())
        summary = (
            "Tap an item below to add it to your order.\n\n"
            f"✅ Added to the basket: {name}\n"
            f"🧺 Basket: {total_units} item{'s' if total_units != 1 else ''} — "
            f"{CURRENCY}{cart_total(cart):.2f}"
        )
        try:
            await query.edit_message_text(summary, reply_markup=menu_keyboard())
        except Exception:
            # Same text as before (e.g. re-tapping in an odd order) — the
            # cart still updated, so this is safe to ignore.
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
        [InlineKeyboardButton("💳 Pay using Visa/Mastercard", callback_data=f"pay_card:{order_id}")],
        [InlineKeyboardButton("🌍 Pay using local payment methods", callback_data=f"local_pay:{order_id}")],
        [InlineKeyboardButton("₿ Pay with Cryptocurrency", callback_data=f"pay_crypto:{order_id}")],
        [InlineKeyboardButton("🎁 Pay using my own Credits", callback_data=f"pay_credits:{order_id}")],
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
            InlineKeyboardButton(
                f"{COUNTRY_FLAGS.get(country, '')} {country}", callback_data=f"local_country:{order_id}:{country}"
            )
            for country in pair
        ])
    rows.append([InlineKeyboardButton("⬅️ Back", callback_data=f"back_to_checkout:{order_id}")])

    await query.edit_message_text(
        "Which country are you paying from?",
        reply_markup=InlineKeyboardMarkup(rows),
    )


def _country_instructions_view(order_id: int, country: str):
    """Builds the (text, keyboard) for a country's payment instructions,
    including the converted amount. Shared by the direct-country path and
    the USA app sub-menu."""
    order = db_get_order(order_id)
    total = order[4] if order else 0

    instructions = LOCAL_PAYMENT_INSTRUCTIONS.get(
        country, "Contact us directly for payment instructions in your country."
    )
    flag = COUNTRY_FLAGS.get(country, "")
    text = (
        f"*Payment instructions — {flag} {country}*\n\n"
        f"*Amount to pay: {convert_for_country(total, country)}*\n\n"
        f"{instructions}\n\n"
        "After paying, tap *I've Paid* below."
    )
    buttons = [
        [InlineKeyboardButton("✅ I've Paid", callback_data=f"paid:{order_id}")],
        [InlineKeyboardButton("⬅️ Back", callback_data=f"local_pay:{order_id}")],
    ]
    return text, InlineKeyboardMarkup(buttons)


async def local_country_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Shows local payment instructions for the chosen country — or, for
    the USA, a choice of remittance apps first."""
    query = update.callback_query
    await query.answer()
    _, order_id_str, country = query.data.split(":", 2)
    order_id = int(order_id_str)

    if country == "USA":
        rows = [
            [InlineKeyboardButton(app, callback_data=f"usa_app:{order_id}:{app}")]
            for app in USA_PAYMENT_APPS
        ]
        rows.append([InlineKeyboardButton("⬅️ Back", callback_data=f"local_pay:{order_id}")])
        await query.edit_message_text(
            f"{COUNTRY_FLAGS['USA']} USA — choose how you'd like to pay:",
            reply_markup=InlineKeyboardMarkup(rows),
        )
        return

    context.user_data["selected_country"] = country
    db_set_payment_method(order_id, country)
    text, keyboard = _country_instructions_view(order_id, country)
    await query.edit_message_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard)

    # India includes a QR code image — send it as a follow-up photo if present.
    if country == "India" and os.path.exists(INDIA_QR_PATH):
        try:
            with open(INDIA_QR_PATH, "rb") as qr_file:
                await context.bot.send_photo(chat_id=query.message.chat_id, photo=qr_file)
        except Exception:
            logger.exception("Failed to send India QR code image")


async def usa_app_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Shows instructions for the chosen USA remittance app."""
    query = update.callback_query
    await query.answer()
    _, order_id_str, app = query.data.split(":", 2)
    order_id = int(order_id_str)

    context.user_data["selected_country"] = "USA"
    db_set_payment_method(order_id, f"USA — {app}")

    order = db_get_order(order_id)
    total = order[4] if order else 0
    instructions = USA_APP_INSTRUCTIONS.get(app, "Contact us directly for payment instructions.")

    text = (
        f"*Payment instructions — {COUNTRY_FLAGS['USA']} USA ({app})*\n\n"
        f"*Amount to pay: {convert_for_country(total, 'USA')}*\n\n"
        f"{instructions}\n\n"
        "After paying, tap *I've Paid* below."
    )
    buttons = [
        [InlineKeyboardButton("✅ I've Paid", callback_data=f"paid:{order_id}")],
        [InlineKeyboardButton("⬅️ Back", callback_data=f"local_country:{order_id}:USA")],
    ]
    await query.edit_message_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(buttons))


async def pay_card_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Shows the step-by-step tutorial for paying by Visa/Mastercard,
    with real screenshots, before sending the customer to the payment link."""
    query = update.callback_query
    await query.answer()
    order_id = int(query.data.split(":", 1)[1])

    order = db_get_order(order_id)
    total = order[4] if order else 0
    db_set_payment_method(order_id, "Visa/Mastercard")

    text = (
        "*💳 Pay using Visa/Mastercard*\n\n"
        f"Amount to pay: *{CURRENCY}{total:.2f}*\n\n"
        "1️⃣ First click on the \"Open Payment Link\" button\n\n"
        "2️⃣ Choose \"Pay online with your card\"\n\n"
        "3️⃣ Fill in the details:\n"
        "• From whom is the gift? — type your first name\n"
        f"• How much do you want to gift? — enter *{total:.2f}* (exact amount in USD)\n"
        "• Message (optional) — leave this empty\n\n"
        "4️⃣ Enter your Visa/Mastercard details and proceed\n\n"
        "✅ Done!\n\n"
        "5️⃣ Click *I've Paid* below after the payment is done."
    )
    buttons = [
        [InlineKeyboardButton("💳 Open Payment Link", url=PAYMENT_LINK_BASE_URL)],
        [InlineKeyboardButton("✅ I've Paid", callback_data=f"paid:{order_id}")],
        [InlineKeyboardButton("⬅️ Back", callback_data=f"back_to_checkout:{order_id}")],
    ]
    await query.edit_message_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(buttons))

    for path in CARD_TUTORIAL_IMAGES:
        if os.path.exists(path):
            try:
                with open(path, "rb") as img_file:
                    await context.bot.send_photo(chat_id=query.message.chat_id, photo=img_file)
            except Exception:
                logger.exception("Failed to send card tutorial image: %s", path)


async def pay_credits_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """'Pay using my own Credits' — instant like Stars, no 'I've Paid' step.
    1 credit = $4, and most prices aren't clean multiples of that, so the
    required credit count is rounded UP (math.ceil) — the customer always
    pays with credits worth at least the price, never less. Any leftover
    value from that rounding just stays in their balance untouched."""
    query = update.callback_query
    await query.answer()
    order_id = int(query.data.split(":", 1)[1])
    user_id = query.from_user.id

    order = db_get_order(order_id)
    if not order:
        await query.edit_message_text("Order not found.")
        return
    _, order_user_id, username, items_json, total, status, created_at = order

    required_credits = math.ceil(total / 4)
    balance = db_get_credits(user_id)

    if balance < required_credits:
        await query.edit_message_text(
            "I am sorry, you don't have enough credits to buy this subscription.\n\n"
            "To earn more credits, click the button below.",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("🎁 Get Free Accounts", callback_data="goto_getfree")]]
            ),
        )
        return

    deducted = db_deduct_credits(user_id, required_credits)
    if not deducted:
        # Balance changed between the check above and now (e.g. another
        # order paid with credits in the same moment) — fail safe rather
        # than let the customer go negative.
        await query.edit_message_text(
            "I am sorry, you don't have enough credits to buy this subscription.\n\n"
            "To earn more credits, click the button below.",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("🎁 Get Free Accounts", callback_data="goto_getfree")]]
            ),
        )
        return

    db_update_status(order_id, "paid")
    db_set_payment_method(order_id, "Credits")

    remaining = db_get_credits(user_id)
    await query.edit_message_text(
        f"✅ Paid with {required_credits} credits! We're preparing your order now.\n"
        f"Remaining credits: {remaining} ({CURRENCY}{remaining * 5})"
    )

    items = json.loads(items_json)
    await start_order_fulfilment(context, order_id, order_user_id, items)
    await award_referral_credit(context, order_user_id)
    await post_receipt_to_payments_channel(context, order_id)


async def goto_getfree(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """The 'Get Free Accounts' button shown from the insufficient-credits
    message — takes the customer straight to that tab's content."""
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    if db_referral_intro_shown(user_id):
        await send_referral_link(context, user_id, query.message.chat_id, edit_query=query)
        return
    await query.edit_message_text(
        GET_FREE_ACCOUNTS_INTRO,
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("Want to proceed?", callback_data="getfree_proceed")]]
        ),
    )


async def pay_crypto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Shows crypto wallet addresses for payment."""
    query = update.callback_query
    await query.answer()
    order_id = int(query.data.split(":", 1)[1])
    db_set_payment_method(order_id, "Cryptocurrency")

    buttons = [
        [InlineKeyboardButton("✅ I've Paid", callback_data=f"paid:{order_id}")],
        [InlineKeyboardButton("⬅️ Back", callback_data=f"back_to_checkout:{order_id}")],
    ]
    text = CRYPTO_INSTRUCTIONS + "\n\nClick *I've Paid* below after the payment is done."
    await query.edit_message_text(
        text, parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(buttons)
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
    """Fires automatically once a Stars payment completes — no manual
    confirmation needed. Structured to match the manual-confirm path:
    order fulfilment (iMD/generic collection, delivery, Pending Orders
    tracking) must start regardless of whether the informational admin
    notification below succeeds — a failed cosmetic step must never
    silently strand the order."""
    payload = update.message.successful_payment.invoice_payload
    order_id = int(payload.split("_", 1)[1])

    db_update_status(order_id, "paid")
    db_set_payment_method(order_id, "Telegram Stars")

    await update.message.reply_text(
        f"✅ Payment received via Telegram Stars for order #{order_id}! We're preparing it now.\n"
        f"Your Telegram ID: {update.effective_user.id} (keep this for any support requests)."
    )

    order = db_get_order(order_id)
    if not order:
        logger.error("Stars payment succeeded for order #%s but no matching order record was found", order_id)
        if ADMIN_CHAT_ID:
            await context.bot.send_message(
                chat_id=ADMIN_CHAT_ID,
                text=(
                    f"⚠️ Stars payment received for order #{order_id}, but no matching order "
                    "record exists — please check this manually."
                ),
            )
        return

    _, user_id, username, items_json, total, status, created_at = order
    items = json.loads(items_json)

    if ADMIN_CHAT_ID:
        lines = [f"{qty}x {MENU[i][0]}" for i, qty in items.items()]
        text = (
            f"⭐ Stars payment received — Order #{order_id}\n"
            f"From: @{username or user_id}\n\n"
            + "\n".join(lines)
            + f"\n\nTotal: {CURRENCY}{total:.2f}\n"
            "Paid automatically via Telegram Stars — no action needed."
        )
        try:
            await context.bot.send_message(chat_id=ADMIN_CHAT_ID, text=text)
        except Exception:
            # Non-fatal — this is informational only. Fulfilment proceeds
            # below no matter what happens here, same as how the manual
            # path isolates its cosmetic caption edit.
            logger.exception("Failed to notify admin of Stars payment for order #%s", order_id)

    await start_order_fulfilment(context, order_id, user_id, items)
    await award_referral_credit(context, user_id)
    await post_receipt_to_payments_channel(context, order_id)


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
        # Use a list so multiple pending receipts don't overwrite each other
        pending = context.user_data.setdefault("pending_receipt_orders", [])
        if order_id not in pending:
            pending.append(order_id)
        context.user_data["awaiting_receipt_for_order"] = pending[0]  # always process oldest first

        if context.user_data.get("selected_country") == "India":
            await query.edit_message_text(
                "Please send the full receipt showing clearly the amount paid and "
                "transaction ID or UTR.\n\n"
                "*Receipts that are missing the paid amount or the transaction ID "
                "won't be approved.*",
                parse_mode=ParseMode.MARKDOWN,
            )
        else:
            await query.edit_message_text(
                f"Order #{order_id}: please upload a *photo* of your payment receipt "
                "(screenshot is fine) as your next message here.",
                parse_mode=ParseMode.MARKDOWN,
            )

    elif action == "cancel":
        db_update_status(order_id, "cancelled")
        await query.edit_message_text(f"Order #{order_id} cancelled.")


async def photo_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Single entry point for incoming photos — dispatches to the ticket
    screenshot flow or the payment receipt flow, whichever the sender is
    actually in the middle of. Mirrors text_state_router: one registered
    handler avoids two photo handlers silently stealing each other's
    updates."""
    if await ticket_screenshot_reply(update, context):
        return
    await receipt_photo(update, context)


async def receipt_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles a photo sent by the customer after tapping 'I've Paid'. Asks
    for the payer's full name next, rather than notifying the admin
    immediately — that name goes into the admin's review message."""
    # Use the queue — always process the oldest pending receipt order
    pending_orders = context.user_data.get("pending_receipt_orders", [])
    order_id = pending_orders[0] if pending_orders else context.user_data.get("awaiting_receipt_for_order")

    if not order_id:
        # Not expecting a receipt right now — ignore stray photos.
        return

    order = db_get_order(order_id)
    if not order:
        await update.message.reply_text("Order not found — please start over with /start.")
        # Remove from queue
        pending_orders = context.user_data.get("pending_receipt_orders", [])
        if order_id in pending_orders:
            pending_orders.remove(order_id)
        context.user_data["awaiting_receipt_for_order"] = pending_orders[0] if pending_orders else None
        return

    # Remove processed order from queue; advance to next if any
    pending_orders = context.user_data.get("pending_receipt_orders", [])
    if order_id in pending_orders:
        pending_orders.remove(order_id)
    context.user_data["awaiting_receipt_for_order"] = pending_orders[0] if pending_orders else None
    context.user_data["awaiting_payer_name_for_order"] = order_id
    photo_file_id = update.message.photo[-1].file_id
    context.user_data["pending_receipt_photo"] = photo_file_id
    db_set_receipt_photo(order_id, photo_file_id)

    await update.message.reply_text(
        "Please send the Full Name of the person who did the payment, as mentioned on the receipt:"
    )


async def payer_name_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Catches the payer's name after the receipt photo, then notifies the
    admin with both."""
    order_id = context.user_data.pop("awaiting_payer_name_for_order", None)
    if not order_id:
        return

    photo_file_id = context.user_data.pop("pending_receipt_photo", None)
    payer_name = update.message.text.strip()

    order = db_get_order(order_id)
    if not order:
        await update.message.reply_text("Order not found — please start over with /start.")
        return

    db_update_status(order_id, "awaiting_confirmation")
    db_set_payer_name(order_id, payer_name)

    await update.message.reply_text(
        f"Thanks! Receipt received for order #{order_id}. "
        "We're verifying it and will confirm shortly."
    )

    if photo_file_id:
        await notify_admin_receipt(context, order, photo_file_id, payer_name=payer_name)


async def notify_admin_receipt(
    context: ContextTypes.DEFAULT_TYPE, order_row, photo_file_id: str, payer_name: str = None
):
    order_id, user_id, username, items_json, total, status, created_at = order_row
    items = json.loads(items_json)
    lines = [f"{qty}x {MENU[i][0]}" for i, qty in items.items()]
    # Plain text on purpose — usernames or item names can contain characters
    # (like _ or *) that break Telegram's Markdown parser and silently fail
    # to send. No parse_mode here avoids that entirely.
    caption = (
        f"🧾 Receipt received — Order #{order_id}\n"
        f"From: {username or 'unknown'} (ID: {user_id})\n"
        + (f"Payer name on receipt: {payer_name}\n" if payer_name else "")
        + "\n"
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
            # A new receipt = a new pending order. Push the updated keyboard
            # so the Pending Orders badge appears immediately.
            await refresh_admin_keyboard(context)
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

def extract_login_from_delivery_message(text: str):
    """Pulls Username/Password out of a delivery message. Every delivery
    path (iMD's template, and the prompt admins are given for manual
    deliveries) follows the "Username: ...\\nPassword: ..." convention,
    so this works uniformly across all of them without needing separate
    structured data threaded through each call site."""
    username_match = re.search(r'Username:\s*(.+)', text)
    password_match = re.search(r'Password:\s*(.+)', text)
    username = username_match.group(1).strip() if username_match else None
    password = password_match.group(1).strip() if password_match else None
    return username, password


async def post_subscription_to_channel(context: ContextTypes.DEFAULT_TYPE, order_id: int, item_id: str, delivery_message: str):
    """Posts a delivered subscription to the private Subscriptions channel:
    the product, its login, the date, and the customer. Isolated with its
    own error handling — a failure here must never affect the actual
    delivery to the customer, which has already happened by the time
    this runs."""
    if not SUBSCRIPTIONS_CHANNEL_ID:
        logger.warning(
            "SUBSCRIPTIONS_CHANNEL_ID not set — order #%s's delivery was not posted to the Subscriptions channel",
            order_id,
        )
        return

    order = db_get_order(order_id)
    if not order:
        return
    _, user_id, username_tg, items_json, total, status, created_at = order

    name = MENU.get(item_id, (item_id or "Subscription",))[0]
    who = f"@{username_tg}" if username_tg else str(user_id)
    date_str = datetime.now().strftime("%Y-%m-%d")
    login_username, login_password = extract_login_from_delivery_message(delivery_message)

    lines = [f"📦 Subscription Delivered", "", f"Subscription: {name}"]
    if login_username:
        lines.append(f"Username: {login_username}")
    if login_password:
        lines.append(f"Password: {login_password}")
    if not login_username and not login_password:
        # Couldn't find the convention in this particular message — fall
        # back to showing exactly what was sent rather than nothing.
        lines.append(f"Login details: {delivery_message}")
    lines.append(f"Date: {date_str}")
    lines.append(f"Customer: {who}")

    try:
        await context.bot.send_message(chat_id=SUBSCRIPTIONS_CHANNEL_ID, text="\n".join(lines))
    except Exception:
        logger.exception("Failed to post delivery for order #%s to the Subscriptions channel", order_id)
        if ADMIN_CHAT_ID:
            await context.bot.send_message(
                chat_id=ADMIN_CHAT_ID,
                text=f"⚠️ Could not post order #{order_id}'s delivery to the Subscriptions channel — check SUBSCRIPTIONS_CHANNEL_ID and that the bot is still an admin there.",
            )


async def award_referral_credit(context: ContextTypes.DEFAULT_TYPE, paying_user_id: int):
    """Called once per successfully paid order. If the paying customer was
    referred, their referrer gets 1 credit — every single time that
    customer completes an order, not just their first."""
    referrer_id = db_get_referred_by(paying_user_id)
    if not referrer_id:
        return

    new_balance = db_add_credits(referrer_id, 1)
    try:
        await context.bot.send_message(
            chat_id=referrer_id,
            text=(
                f"🎉 You just earned 1 credit — someone you referred made a purchase!\n\n"
                f"Your credits: {new_balance} ({CURRENCY}{new_balance * 5})"
            ),
        )
    except Exception:
        logger.exception("Failed to notify referrer %s of new credits", referrer_id)


async def post_receipt_to_payments_channel(context: ContextTypes.DEFAULT_TYPE, order_id: int):
    """Posts the approved payment to the private Payments channel, captioned
    with the country/method, amount, customer name, and subscription.
    Sends the receipt image when there is one (manual payment methods);
    Stars payments have no receipt screenshot, so those post as a text
    message with the same details instead of being skipped.
    Isolated with its own error handling — a failure here must never
    affect the actual order confirmation."""
    if not PAYMENTS_CHANNEL_ID:
        # This previously failed silently — nothing told anyone the post
        # never happened. Now it's logged and surfaced once per order.
        logger.warning("PAYMENTS_CHANNEL_ID not set — order #%s was not posted to the Payments channel", order_id)
        if ADMIN_CHAT_ID:
            await context.bot.send_message(
                chat_id=ADMIN_CHAT_ID,
                text=f"ℹ️ PAYMENTS_CHANNEL_ID isn't set, so order #{order_id} wasn't posted to the Payments channel.",
            )
        return

    order = db_get_order(order_id)
    if not order:
        return
    _, user_id, username, items_json, total, status, created_at = order

    method, payer_name, photo_file_id = db_get_payment_record(order_id)

    items = json.loads(items_json)
    item_names = ", ".join(MENU.get(i, (i,))[0] for i in items)
    who = payer_name or (f"@{username}" if username else str(user_id))

    caption = (
        f"🧾 Payment Approved — Order #{order_id}\n\n"
        f"Country/Method: {method or 'unknown'}\n"
        f"Amount: {CURRENCY}{total:.2f}\n"
        f"Customer: {who}\n"
        f"Subscription: {item_names}"
    )

    try:
        if photo_file_id:
            await context.bot.send_photo(chat_id=PAYMENTS_CHANNEL_ID, photo=photo_file_id, caption=caption)
        else:
            await context.bot.send_message(chat_id=PAYMENTS_CHANNEL_ID, text=caption)
    except Exception:
        logger.exception("Failed to post receipt for order #%s to the Payments channel", order_id)
        if ADMIN_CHAT_ID:
            await context.bot.send_message(
                chat_id=ADMIN_CHAT_ID,
                text=f"⚠️ Could not post order #{order_id}'s receipt to the Payments channel — check PAYMENTS_CHANNEL_ID and that the bot is still an admin there.",
            )


async def channel_post_detector(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """One-time helper: if PAYMENTS_CHANNEL_ID isn't configured yet, any
    post in a channel the bot can see gets its chat_id reported to the
    admin — the easiest way to discover a private channel's numeric ID
    without a third-party tool."""
    if PAYMENTS_CHANNEL_ID:
        return  # already configured, nothing to do
    if not ADMIN_CHAT_ID:
        return
    chat = update.effective_chat
    await context.bot.send_message(
        chat_id=ADMIN_CHAT_ID,
        text=(
            f"📡 Detected a post in \"{chat.title or 'a channel'}\" — its ID is:\n\n{chat.id}\n\n"
            "Set PAYMENTS_CHANNEL_ID to this value in Railway's Variables tab, then redeploy."
        ),
    )


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
        await award_referral_credit(context, user_id)
        await post_receipt_to_payments_channel(context, order_id)

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

    # Strip the live badge suffix (e.g. " 🔴3") before routing, so the
    # comparison against the static A_* labels always works regardless of
    # what count is currently shown on the button.
    raw = update.message.text or ""
    text = raw.split(" 🔴")[0].strip()

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
        clear_admin_flow_state(context.user_data)
        context.user_data["awaiting_admin_input"] = "remove_serial"
        await update.message.reply_text("Send the serial code to remove:")

    elif text == A_RECENT_ORDERS:
        await customer_history(update, context)

    elif text == A_PENDING:
        await admin_pending_list(update, context)

    elif text == A_INPUT:
        await admin_input_menu(update, context)

    elif text == A_INBOX:
        await inbox_menu(update, context)

    elif text == A_BROADCAST:
        clear_admin_flow_state(context.user_data)
        context.user_data["awaiting_admin_input"] = "broadcast"
        recipient_count = len(db_all_user_ids(exclude=ADMIN_CHAT_ID))
        await update.message.reply_text(
            f"Type the announcement to send to all {recipient_count} customer(s):"
        )

    elif text == A_FIND_ORDER:
        clear_admin_flow_state(context.user_data)
        context.user_data["awaiting_admin_input"] = "find_order"
        await update.message.reply_text("Send the order number:")

    elif text == A_FIND_CUSTOMER:
        clear_admin_flow_state(context.user_data)
        context.user_data["awaiting_admin_input"] = "find_customer"
        await update.message.reply_text("Send the customer's Telegram ID or @username:")

    elif text == A_STOCK:
        await stock_menu(update, context)

    elif text == A_TICKETS:
        await tickets_menu(update, context)

    elif text == A_CREDITS:
        await credits_menu(update, context)

    elif text == A_ADD_SUBSCRIPTION:
        clear_admin_flow_state(context.user_data)
        context.user_data["awaiting_new_sub_field"] = "name"
        context.user_data["new_sub_data"] = {}
        await update.message.reply_text("Enter the name of the new subscription:")

    elif text == A_BOOK_REQUESTS:
        await book_requests_menu(update, context)

    elif text == A_DELIVERED:
        await delivered_subscriptions_menu(update, context)

    elif text == A_IMD_CATALOG:
        await imd_catalog_extract_start(update, context)

    elif text == A_CUSTOMER_VIEW:
        await update.message.reply_text(
            "Switched to the customer menu. Send /start to return to the admin panel.",
            reply_markup=main_menu_keyboard(0),  # no user context, so no badge
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
        "login_username": "Username/Email", "login_password": "Password",
    }
    return "\n".join(f"{labels.get(k, k)}: {v}" for k, v in info.items())


async def admin_input_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Sales summary — pick a period."""
    if update.effective_user.id != ADMIN_CHAT_ID:
        return
    await update.message.reply_text(
        "Show sales for:",
        reply_markup=InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("Today", callback_data="sales:today")],
                [InlineKeyboardButton("This Week", callback_data="sales:week")],
                [InlineKeyboardButton("This Month", callback_data="sales:month")],
            ]
        ),
    )


async def admin_sales_report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Totals sold per product for the chosen period."""
    query = update.callback_query
    if query.from_user.id != ADMIN_CHAT_ID:
        await query.answer("Not authorized.", show_alert=True)
        return
    await query.answer()

    period = query.data.split(":", 1)[1]
    offset = timedelta(hours=REPORT_UTC_OFFSET)
    local_now = datetime.utcnow() + offset

    if period == "today":
        local_since = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
        label = "Today"
    elif period == "week":
        local_since = (local_now - timedelta(days=local_now.weekday())).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        label = "This week"
    else:
        local_since = local_now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        label = "This month"

    # Convert the local boundary back to UTC, since that's how orders are stored.
    counts, revenue = db_sales_since((local_since - offset).isoformat())

    back = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Today", callback_data="sales:today"),
                InlineKeyboardButton("Week", callback_data="sales:week"),
                InlineKeyboardButton("Month", callback_data="sales:month"),
            ]
        ]
    )

    if not counts:
        await query.edit_message_text(f"📈 {label}\n\nNo sales in this period.", reply_markup=back)
        return

    lines = [f"📈 {label}", ""]
    total_units = 0
    for item_id, qty in counts:
        name = MENU.get(item_id, (item_id,))[0]
        lines.append(f"{qty} × {name}")
        total_units += qty
    lines.append("")
    lines.append(f"Total accounts: {total_units}")
    lines.append(f"Revenue: {CURRENCY}{revenue:.2f}")

    await query.edit_message_text("\n".join(lines), reply_markup=back)


async def admin_comment_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin tapped 'Send Comment' on a receipt — wait for the message text
    and pass it on to the customer. Leaves the order's status untouched so
    it can still be confirmed or rejected afterwards."""
    query = update.callback_query
    if query.from_user.id != ADMIN_CHAT_ID:
        await query.answer("Not authorized.", show_alert=True)
        return
    await query.answer()

    order_id = int(query.data.split(":", 1)[1])
    clear_admin_flow_state(context.user_data)
    context.user_data["awaiting_comment_for_order"] = order_id
    await context.bot.send_message(
        chat_id=ADMIN_CHAT_ID,
        text=f"Type the message to send to the customer about order #{order_id}:",
    )


async def admin_comment_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Sends the admin's typed comment to the customer, with a Reply button
    so the customer can respond directly — same pattern as the Inbox messages."""
    order_id = context.user_data.pop("awaiting_comment_for_order", None)
    if not order_id:
        return

    order = db_get_order(order_id)
    if not order:
        await update.message.reply_text("Order not found.")
        return

    _, user_id, username, items_json, total, status, created_at = order
    await context.bot.send_message(
        chat_id=user_id,
        text=f"💬 Message about your order #{order_id}:\n\n{update.message.text}",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("↩️ Reply", callback_data=f"cust_reply:{order_id}")]]
        ),
    )
    await update.message.reply_text(f"✅ Comment sent to the customer for order #{order_id}.")


def all_toggleable_items() -> list:
    """Every product that can be individually marked out of stock: the
    catalog leaves (Uptodate/Amboss variants), the four iMD variants, and
    the single-choice mains — in a sensible display order."""
    items = []

    def walk(node):
        if "item" in node:
            items.append(node["item"])
        else:
            for child in node.get("children", {}).values():
                walk(child)

    for cat in CATALOG.values():
        walk(cat)
    items.extend(["imd_new_6m", "imd_new_1y", "imd_renew_6m", "imd_renew_1y"])
    items.extend(SINGLE_MAIN_ITEMS)
    return items


def stock_keyboard() -> InlineKeyboardMarkup:
    out_of_stock = db_out_of_stock_items()
    buttons = []
    for item_id in all_toggleable_items():
        name = MENU.get(item_id, (item_id,))[0]
        flag = "🚫" if item_id in out_of_stock else "✅"
        buttons.append([InlineKeyboardButton(f"{flag} {name}", callback_data=f"stocktoggle:{item_id}")])
    return InlineKeyboardMarkup(buttons)


async def stock_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin-only: lists every product with a one-tap available/out-of-stock
    toggle."""
    if update.effective_user.id != ADMIN_CHAT_ID:
        return
    await update.message.reply_text(
        "📦 Manage Stock — tap an item to toggle:\n✅ available · 🚫 out of stock",
        reply_markup=stock_keyboard(),
    )


async def stock_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Flips one item's stock status and refreshes the list in place."""
    query = update.callback_query
    if query.from_user.id != ADMIN_CHAT_ID:
        await query.answer("Not authorized.", show_alert=True)
        return

    item_id = query.data.split(":", 1)[1]
    currently_out = db_is_out_of_stock(item_id)
    db_set_stock_status(item_id, out_of_stock=not currently_out)

    await query.answer("Marked out of stock" if not currently_out else "Marked available")
    await query.edit_message_reply_markup(reply_markup=stock_keyboard())


async def new_subscription_field_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Collects name -> price -> duration for a new subscription, then adds
    it to MENU (in memory, immediately purchasable) and saves it to the
    database so it survives the next redeploy."""
    if update.effective_user.id != ADMIN_CHAT_ID:
        return

    field = context.user_data.get("awaiting_new_sub_field")
    if not field:
        return

    value = update.message.text.strip()
    data = context.user_data.setdefault("new_sub_data", {})

    if field == "name":
        if not value:
            await update.message.reply_text("Please send a non-empty name:")
            return
        data["name"] = value
        context.user_data["awaiting_new_sub_field"] = "price"
        await update.message.reply_text("Enter the price in USD (numbers only, e.g. 25):")
        return

    if field == "price":
        try:
            price = float(value)
            if price <= 0:
                raise ValueError
        except ValueError:
            await update.message.reply_text("Please send a valid positive number for the price:")
            return
        data["price"] = price
        context.user_data["awaiting_new_sub_field"] = "duration"
        await update.message.reply_text("Enter the duration (e.g. \"1 Year\", \"6 Months\"):")
        return

    if field == "duration":
        if not value:
            await update.message.reply_text("Please send a non-empty duration:")
            return
        data["duration"] = value
        context.user_data.pop("awaiting_new_sub_field", None)
        context.user_data.pop("new_sub_data", None)

        full_name = f"{data['name']} - {data['duration']}"
        item_id = generate_item_id(full_name)

        db_add_custom_item(item_id, full_name, data["price"])
        MENU[item_id] = (full_name, data["price"])
        SINGLE_MAIN_ITEMS.append(item_id)

        await update.message.reply_text(
            f"✅ Added to the menu:\n\n{full_name} — {CURRENCY}{data['price']:.2f}\n\n"
            "It's now available for customers to purchase, and listed under 📦 Manage Stock."
        )


def _book_requests_summary_keyboard() -> InlineKeyboardMarkup:
    pending = len(db_list_book_requests("pending"))
    priced = len(db_list_book_requests("priced"))
    ordered = len(db_list_book_requests("ordered"))
    cancelled = len(db_list_book_requests("cancelled"))
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(f"🆕 Pending ({pending})", callback_data="bookreqs:pending")],
            [InlineKeyboardButton(f"💰 Awaiting Customer ({priced})", callback_data="bookreqs:priced")],
            [InlineKeyboardButton(f"✅ Ordered ({ordered})", callback_data="bookreqs:ordered")],
            [InlineKeyboardButton(f"❌ Cancelled ({cancelled})", callback_data="bookreqs:cancelled")],
        ]
    )


async def book_requests_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin-only: top of the Book Requests tab — one bucket per status,
    so a request the admin didn't act on immediately isn't lost."""
    if update.effective_user.id != ADMIN_CHAT_ID:
        return
    await update.message.reply_text("📚 Book Requests:", reply_markup=_book_requests_summary_keyboard())


async def book_requests_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query.from_user.id != ADMIN_CHAT_ID:
        await query.answer("Not authorized.", show_alert=True)
        return
    await query.answer()

    status = query.data.split(":", 1)[1]
    rows = db_list_book_requests(status)
    back = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="bookreqs_back")]])

    labels = {"pending": "Pending", "priced": "Awaiting Customer", "ordered": "Ordered", "cancelled": "Cancelled"}
    label = labels.get(status, status)

    if not rows:
        await query.edit_message_text(f"No {label.lower()} book requests.", reply_markup=back)
        return

    buttons = []
    for request_id, user_id, username, link, price, created_at in rows:
        who = f"@{username}" if username else str(user_id)
        price_str = f" — {CURRENCY}{price:.2f}" if price else ""
        buttons.append(
            [InlineKeyboardButton(f"#{request_id} — {who}{price_str}", callback_data=f"bookreqview:{request_id}")]
        )
    buttons.append([InlineKeyboardButton("⬅️ Back", callback_data="bookreqs_back")])

    await query.edit_message_text(f"{label} book requests:", reply_markup=InlineKeyboardMarkup(buttons))


async def book_requests_back(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("📚 Book Requests:", reply_markup=_book_requests_summary_keyboard())


async def book_request_view(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Full detail for one book request. Sent as a new message (not an
    edit) so the link is always plain text, never at risk of Telegram
    trying to parse it as Markdown."""
    query = update.callback_query
    if query.from_user.id != ADMIN_CHAT_ID:
        await query.answer("Not authorized.", show_alert=True)
        return
    await query.answer()

    request_id = int(query.data.split(":", 1)[1])
    request = db_get_book_request(request_id)
    if not request:
        await context.bot.send_message(chat_id=ADMIN_CHAT_ID, text="That book request no longer exists.")
        return

    _, user_id, username, link, price, status, order_id, created_at, priced_at, resolved_at = request
    who = f"@{username}" if username else str(user_id)

    text = (
        f"📚 Book Request #{request_id} — {who} (ID: {user_id})\n"
        f"Status: {status}\n"
        f"Requested: {created_at[:19]}\n"
        + (f"Price: {CURRENCY}{price:.2f}\n" if price else "")
        + (f"Order: #{order_id}\n" if order_id else "")
        + f"\nLink:\n{link}"
    )

    buttons = []
    if status == "pending":
        buttons.append([InlineKeyboardButton("💰 Set Price", callback_data=f"bookprice:{request_id}")])
    buttons.append([InlineKeyboardButton("⬅️ Back", callback_data=f"bookreqs:{status}")])

    await context.bot.send_message(chat_id=ADMIN_CHAT_ID, text=text, reply_markup=InlineKeyboardMarkup(buttons))


# ── iMD catalog extraction & search ──────────────────────────────────

def db_imd_catalog_count() -> int:
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute("SELECT COUNT(*) FROM imd_catalog").fetchone()
    conn.close()
    return row[0] if row else 0


def db_imd_search(query: str, limit: int = 10, offset: int = 0):
    """Search the iMD catalog with progressive fallback:
    1. Case-insensitive full-phrase match (LOWER LIKE)
    2. All-words match (every word must appear, case-insensitive)
    3. Any-word match (at least one word, for showing 'similars')
    Results from earlier strategies always come first."""
    conn = sqlite3.connect(DB_PATH)
    q = query.strip().lower()

    def run(where_clause, params):
        rows  = conn.execute(
            f"SELECT name, category FROM imd_catalog WHERE {where_clause} ORDER BY name LIMIT ? OFFSET ?",
            params + [limit, offset],
        ).fetchall()
        count = conn.execute(
            f"SELECT COUNT(*) FROM imd_catalog WHERE {where_clause}",
            params,
        ).fetchone()[0]
        return rows, count

    # Strategy 1: full phrase, case-insensitive
    rows, count = run("LOWER(name) LIKE ?", [f"%{q}%"])
    if rows:
        conn.close()
        return rows, count

    # Strategy 2: ALL words must appear (order-independent)
    words = [w for w in q.split() if len(w) > 1]
    if len(words) > 1:
        clause = " AND ".join(["LOWER(name) LIKE ?" for _ in words])
        rows, count = run(clause, [f"%{w}%" for w in words])
        if rows:
            conn.close()
            return rows, count

    # Strategy 3: ANY word matches (similar / partial)
    if words:
        clause = " OR ".join(["LOWER(name) LIKE ?" for _ in words])
        rows, count = run(clause, [f"%{w}%" for w in words])

    conn.close()
    return rows, count


def db_imd_save_catalog(names: list):
    """Replaces the entire catalog with a fresh extraction."""
    conn = sqlite3.connect(DB_PATH)
    conn.execute("DELETE FROM imd_catalog")
    now = datetime.utcnow().isoformat()
    conn.executemany(
        "INSERT INTO imd_catalog (name, category, extracted_at) VALUES (?, ?, ?)",
        [(name, category, now) for name, category in names],
    )
    conn.commit()
    conn.close()


async def _imd_login(page, username: str, password: str, status_cb, bot=None, admin_id=None):
    """Shared login flow: logs into imdweb.org, dismisses the User
    Agreement, and lands on the downloads page. Raises ValueError on
    failure. Used by both the full extraction and the raw-sample
    diagnostic so the two never drift out of sync."""
    await status_cb("🔑 Logging in to imdweb.org...")
    await page.goto("https://imdweb.org/login", wait_until="load", timeout=60000)
    await page.wait_for_timeout(4000)

    # Fill by position — works regardless of generated class names
    text_inputs = await page.locator('input[type="text"], input[type="email"], input:not([type])').all()
    pwd_inputs  = await page.locator('input[type="password"]').all()
    if text_inputs:
        await text_inputs[0].fill(username)
    if pwd_inputs:
        await pwd_inputs[0].fill(password)

    clicked = False
    for sel in ['button[type="submit"]', 'button:has-text("Login")', 'button:has-text("Sign in")']:
        if await page.locator(sel).count():
            await page.locator(sel).first.click()
            clicked = True
            break
    if not clicked and pwd_inputs:
        await pwd_inputs[0].press("Enter")

    await page.wait_for_timeout(6000)

    # ── Dismiss User Agreement (appears before the redirect completes) ──
    # imdweb.org shows this modal WHILE the URL is still /login —
    # we must click Agree before checking the URL, otherwise we
    # incorrectly conclude that login failed.
    for attempt in range(8):
        dismissed = await page.evaluate("""() => {
            function triggerClick(el) {
                const rk = Object.keys(el).find(k =>
                    k.startsWith("__reactFiber") ||
                    k.startsWith("__reactProps") ||
                    k.startsWith("__reactInternalInstance"));
                if (rk) {
                    const props = el[rk]?.memoizedProps || el[rk] || {};
                    if (props.onClick) {
                        props.onClick({type:"click",target:el,currentTarget:el,
                            stopPropagation:()=>{},preventDefault:()=>{}});
                        return true;
                    }
                    let fiber = el[rk];
                    for (let i = 0; i < 5; i++) {
                        fiber = fiber?.return;
                        if (fiber?.memoizedProps?.onClick) {
                            fiber.memoizedProps.onClick({});
                            return true;
                        }
                    }
                }
                ["pointerdown","mousedown","pointerup","mouseup","click"].forEach(t =>
                    el.dispatchEvent(new MouseEvent(t, {
                        view:window, bubbles:true, cancelable:true, buttons:1}))
                );
                return true;
            }
            for (const el of document.querySelectorAll("button,[role=button]")) {
                const txt = (el.innerText || el.textContent || "").trim();
                if (txt === "Agree" || txt === "I Agree" || txt === "Accept") {
                    return triggerClick(el);
                }
            }
            return false;
        }""")
        if dismissed:
            logger.info("iMD: User Agreement dismissed (attempt %s)", attempt + 1)
            await page.wait_for_timeout(3000)  # wait for redirect after agree
            break
        await page.wait_for_timeout(1000)

    # Now check if we actually made it past the login page
    if "login" in page.url.lower():
        if bot and admin_id:
            try:
                sc = "/tmp/imd_login_fail.png"
                await page.screenshot(path=sc)
                await bot.send_photo(chat_id=admin_id, photo=open(sc, "rb"),
                                     caption=f"Login failed. URL: {page.url}")
            except Exception:
                pass
        raise ValueError("Login failed — please check your iMD username and password.")

    await status_cb("✅ Logged in and Agreement dismissed. Navigating to databases...")

    # ── Navigate to databases page ────────────────────────────────
    await status_cb("📂 Navigating to databases...")
    # Direct URL — avoids triggering the modal again on the home page
    await page.goto("https://imdweb.org/downloads", wait_until="load", timeout=30000)
    await page.wait_for_timeout(3000)

    # Dismiss modal again if it reappears
    await page.evaluate("""() => {
        document.querySelectorAll("button").forEach(b => {
            if ((b.innerText||"").trim()==="Agree") b.click();
        });
    }""")
    await page.wait_for_timeout(2000)


async def imd_raw_sample_playwright(username: str, password: str, status_cb,
                                     bot=None, admin_id=None) -> str:
    """Diagnostic only: logs in, fetches ONE page from the downloads API,
    and returns the raw, unparsed JSON as a pretty-printed string. Use
    this to see the actual field names the API returns (name/title/
    display_name/etc.) before deciding how to parse them — no catalog
    is touched."""
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
        )
        ctx = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 900},
        )
        page = await ctx.new_page()
        await _imd_login(page, username, password, status_cb, bot=bot, admin_id=admin_id)

        await status_cb("📥 Fetching one raw page for inspection...")
        raw_json = await page.evaluate("""async () => {
            const resp = await fetch("/api/labrange/downloads?limit=5&offset=0");
            const text = await resp.text();
            return text;
        }""")
        await browser.close()

    try:
        parsed = json.loads(raw_json)
        return json.dumps(parsed, indent=2)[:15000]
    except (json.JSONDecodeError, TypeError):
        return raw_json[:15000]


async def extract_imd_catalog_playwright(username: str, password: str,
                                          status_cb, bot=None, admin_id=None) -> list:
    """Logs into imdweb.org with Playwright, dismisses the User Agreement,
    then calls the known API endpoint FROM INSIDE the browser using fetch().
    Because we use the browser's own authenticated session, no tokens need
    to be extracted and the 1-session limit is not triggered."""
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
        )
        ctx = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 900},
        )
        page = await ctx.new_page()
        await _imd_login(page, username, password, status_cb, bot=bot, admin_id=admin_id)

        # ── Download ALL databases via in-browser fetch() ─────────────
        await status_cb("📥 Downloading database catalog (this takes a few minutes)...")

        # Increase page timeout so the long-running JS doesn't get cut off
        page.set_default_timeout(600000)  # 10 minutes

        raw = await page.evaluate("""async () => {
            const ENDPOINT = "/api/labrange/downloads";
            const LIMIT    = 100;
            const all      = [];
            let   offset   = 0;
            let   empty    = 0;

            while (empty < 3 && all.length < 200000) {
                let items = null;
                // Try without scope first (all databases), then with scope=all
                for (const scope of [null, "all", "recent", "top"]) {
                    const params = new URLSearchParams({limit: LIMIT, offset});
                    if (scope) params.set("scope", scope);
                    try {
                        const resp = await fetch(ENDPOINT + "?" + params);
                        if (!resp.ok) continue;
                        const data = await resp.json();
                        // Extract the array — API may wrap it in a key
                        if (Array.isArray(data)) {
                            items = data;
                        } else {
                            for (const key of ["data","items","results","databases","books","list"]) {
                                if (Array.isArray(data[key])) { items = data[key]; break; }
                            }
                        }
                        if (items && items.length > 0) break;
                    } catch(e) {}
                }

                if (!items || items.length === 0) {
                    empty++;
                    offset += LIMIT;
                    continue;
                }
                empty = 0;

                for (const item of items) {
                    // Prefer a human-readable display name over the raw
                    // filename — some entries have BOTH (e.g. name:
                    // "00134097.db", title: "Comprehensive Vision
                    // Rehabilitation"), and the filename must not win.
                    const displayName = item.title || item.display_name || item.label
                        || item.book_title || item.full_name || "";
                    const fallbackName = item.name || item.db_name || item.filename || "";
                    const name = (displayName && displayName.trim()) ? displayName : fallbackName;
                    const cat  = item.section || item.category || item.type || item.specialty || "";
                    if (name.length > 2) all.push([name, cat]);
                }

                if (items.length < LIMIT) break;  // last page
                offset += LIMIT;
            }

            return all;
        }""")

        await browser.close()

    if not raw:
        raise ValueError(
            "No databases found.\n\n"
            "The login may have succeeded but the API returned empty results. "
            "This can happen if the User Agreement modal was not dismissed. "
            "Try running again."
        )

    # De-duplicate by name
    seen = set()
    unique = []
    for name, cat in raw:
        if name and name not in seen:
            seen.add(name)
            unique.append((name, cat))

    return unique


def _extract_names_from_json(data, depth=0) -> list:
    """Recursively digs through a JSON response to find database name strings."""
    if depth > 6:
        return []
    results = []
    if isinstance(data, list):
        for item in data:
            if isinstance(item, dict):
                name = (item.get("name") or item.get("title") or item.get("db_name")
                        or item.get("database_name") or item.get("label") or "")
                category = (item.get("category") or item.get("type")
                            or item.get("specialty") or "")
                if name and 3 < len(name) < 300:
                    results.append((name.strip(), category.strip() if category else None))
                else:
                    results.extend(_extract_names_from_json(item, depth + 1))
            elif isinstance(item, str) and 5 < len(item) < 200:
                results.append((item.strip(), None))
    elif isinstance(data, dict):
        for key in ("data", "results", "items", "databases", "books", "list", "records"):
            if key in data:
                results.extend(_extract_names_from_json(data[key], depth + 1))
        if not results:
            for v in data.values():
                results.extend(_extract_names_from_json(v, depth + 1))
    return results


# ── Admin: start extraction flow ──────────────────────────────────────

async def imd_catalog_extract_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_CHAT_ID:
        return
    clear_admin_flow_state(context.user_data)
    count = db_imd_catalog_count()
    msg = ""
    if count:
        msg = f"Current catalog has {count:,} databases.\n\n"
    context.user_data["awaiting_imd_catalog_username"] = True
    await update.message.reply_text(
        f"{msg}🔬 Extract iMD Catalog\n\n"
        "This will log into imdweb.org, extract all database names, and save them "
        "so customers can search before having credentials.\n\n"
        "Send your iMD username:"
    )


async def imd_diag_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin-only /imddiag: fetches ONE raw page from the downloads API
    and sends it back unparsed, so we can see the real field names
    (title vs. name vs. display_name etc.) before touching the catalog."""
    if update.effective_user.id != ADMIN_CHAT_ID:
        return
    clear_admin_flow_state(context.user_data)
    context.user_data["awaiting_imd_diag_username"] = True
    await update.message.reply_text(
        "🔍 Diagnostic — fetches one raw page of the downloads API, no catalog changes.\n\n"
        "Send your iMD username:"
    )


async def imd_diag_username_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("awaiting_imd_diag_username", None)
    context.user_data["imd_diag_username"] = update.message.text.strip()
    context.user_data["awaiting_imd_diag_password"] = True
    await update.message.reply_text(
        "Send your iMD password:\n_(it won't be stored — used once for this check)_",
        parse_mode=ParseMode.MARKDOWN,
    )


async def imd_diag_password_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("awaiting_imd_diag_password", None)
    username = context.user_data.pop("imd_diag_username", "")
    password = update.message.text.strip()

    try:
        await update.message.delete()
    except Exception:
        pass

    status_msg = await context.bot.send_message(
        chat_id=ADMIN_CHAT_ID,
        text="🔄 Logging in and fetching one raw page..."
    )

    async def update_status(text: str):
        try:
            await status_msg.edit_text(text)
        except Exception:
            pass

    try:
        raw_text = await imd_raw_sample_playwright(
            username, password, update_status,
            bot=context.bot, admin_id=ADMIN_CHAT_ID,
        )
        out_path = os.path.join(os.path.dirname(DB_PATH) or ".", "imd_raw_sample.json")
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(raw_text)
        await update_status("✅ Got a raw sample page — sending it now.")
        with open(out_path, "rb") as f:
            await context.bot.send_document(
                chat_id=ADMIN_CHAT_ID,
                document=f,
                filename="imd_raw_sample.json",
                caption="Raw, unparsed API response — check the field names here.",
            )
    except ValueError as e:
        await update_status(f"❌ {e}")
    except Exception as e:
        logger.exception("iMD diagnostic fetch failed")
        await update_status(f"❌ Diagnostic failed: {e}")


async def imd_catalog_username_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("awaiting_imd_catalog_username", None)
    context.user_data["imd_extract_username"] = update.message.text.strip()
    context.user_data["awaiting_imd_catalog_password"] = True
    await update.message.reply_text(
        "Send your iMD password:\n_(it won't be stored — used once for extraction)_",
        parse_mode=ParseMode.MARKDOWN,
    )


async def imd_catalog_password_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("awaiting_imd_catalog_password", None)
    username = context.user_data.pop("imd_extract_username", "")
    password = update.message.text.strip()

    try:
        await update.message.delete()
    except Exception:
        pass

    status_msg = await context.bot.send_message(
        chat_id=ADMIN_CHAT_ID,
        text="🔄 Logging in and downloading the catalog from inside the browser..."
    )

    async def update_status(text: str):
        try:
            await status_msg.edit_text(text)
        except Exception:
            pass

    try:
        databases = await extract_imd_catalog_playwright(
            username, password, update_status,
            bot=context.bot, admin_id=ADMIN_CHAT_ID,
        )
        await update_status(f"💾 Saving {len(databases):,} databases...")
        db_imd_save_catalog(databases)
        await update_status(
            f"✅ iMD Catalog updated!\n\n"
            f"📚 {len(databases):,} databases saved.\n\n"
            "Customers can now use 🔬 Search iMD Resources."
        )
    except ValueError as e:
        await update_status(f"❌ {e}")
    except Exception as e:
        logger.exception("iMD catalog extraction failed")
        await update_status(f"❌ Extraction failed: {e}")


async def _ask_for_session_token(context):
    context.user_data["awaiting_imd_session_token"] = True
    await context.bot.send_message(
        chat_id=ADMIN_CHAT_ID,
        text=(
            "📱 *Step 1 — Get your session token*\n\n"
            "In Firefox, go to `imdweb.org`, log in, click *Agree*, "
            "then paste this in the address bar:\n\n"
            "`javascript:alert(JSON.stringify(localStorage))`\n\n"
            "Copy the full popup text and send it here.\n\n"
            "_The token looks like: {\"imd\\_web\\_resume\":\"...\",\"imd\\_web\\_device\\_id\":\"...\"}_"
        ),
        parse_mode=ParseMode.MARKDOWN,
    )


async def imd_api_url_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin sent the real API URL discovered via the fetch interceptor.
    Use it directly with the previously saved token."""
    context.user_data.pop("awaiting_imd_api_url", None)
    api_url = update.message.text.strip()

    saved_token = context.user_data.pop("imd_saved_token", "")
    if not saved_token:
        await update.message.reply_text(
            "No saved token. Please run 🔬 Update iMD Catalog again from the start."
        )
        return

    if not api_url.startswith("http"):
        await update.message.reply_text("That doesn't look like a URL. Please send just the API URL starting with https://")
        context.user_data["awaiting_imd_api_url"] = True
        context.user_data["imd_saved_token"] = saved_token
        return

    # Strip any query parameters to get the base endpoint
    base_url = api_url.split("?")[0]

    status_msg = await context.bot.send_message(
        chat_id=ADMIN_CHAT_ID,
        text=f"🔄 Using discovered API: {base_url}\nExtracting databases..."
    )

    async def update_status(text: str):
        try:
            await status_msg.edit_text(text)
        except Exception:
            pass

    # Inject the real URL into the extraction
    import json as _json
    import aiohttp

    try:
        parsed = _json.loads(saved_token)
        resume_token = parsed.get("imd_web_resume", "")
        device_id = parsed.get("imd_web_device_id", "")
    except Exception:
        resume_token = saved_token
        device_id = ""

    cookie_str = f"imd_web_resume={resume_token}"
    if device_id:
        cookie_str += f"; imd_web_device_id={device_id}"

    headers = {
        "User-Agent": "Mozilla/5.0 (Linux; Android 10; Mobile) AppleWebKit/537.36",
        "Accept": "application/json, */*",
        "Referer": "https://imdweb.org/",
        "Origin": "https://imdweb.org",
        "Cookie": cookie_str,
        "Authorization": f"Bearer {resume_token}",
    }

    databases = []
    try:
        async with aiohttp.ClientSession() as session:
            # First call to see what params work
            for param_set in [
                {"page": 1, "limit": 100},
                {"page": 1, "per_page": 100},
                {"offset": 0, "limit": 100},
                {"q": "", "page": 1, "limit": 100},
                {},
            ]:
                async with session.get(base_url, params=param_set, headers=headers,
                                       timeout=aiohttp.ClientTimeout(total=20)) as resp:
                    if resp.status == 200:
                        ct = resp.headers.get("content-type", "")
                        if "json" in ct or resp.status == 200:
                            data = await resp.json(content_type=None)
                            names = _extract_names_from_json(data)
                            if names:
                                databases.extend(names)
                                working_params = param_set
                                await update_status(
                                    f"✅ API works! Got {len(names)} on first page. Downloading all..."
                                )
                                break

            if not databases:
                await update_status(
                    "❌ Could not get data from that URL.\n\n"
                    "Please send the FULL URL shown in the popup including any ?parameters"
                )
                context.user_data["awaiting_imd_api_url"] = True
                context.user_data["imd_saved_token"] = saved_token
                return

            # Paginate
            page_num = 2
            empty = 0
            while empty < 3 and page_num < 2000:
                params = {**working_params, "page": page_num}
                try:
                    async with session.get(base_url, params=params, headers=headers,
                                           timeout=aiohttp.ClientTimeout(total=15)) as resp:
                        if resp.status == 200:
                            data = await resp.json(content_type=None)
                            names = _extract_names_from_json(data)
                            if names:
                                databases.extend(names)
                                empty = 0
                                if page_num % 20 == 0:
                                    await update_status(f"📥 {len(databases):,} databases so far...")
                            else:
                                empty += 1
                except Exception:
                    empty += 1
                page_num += 1

        # De-duplicate
        seen = set()
        unique = [(n, c) for n, c in databases if n.strip() and n.strip() not in seen and not seen.add(n.strip())]

        db_imd_save_catalog(unique)
        await update_status(
            f"✅ iMD Catalog updated!\n\n"
            f"📚 {len(unique):,} databases saved.\n"
            "Customers can now use 🔬 Search iMD Resources."
        )
    except Exception as e:
        logger.exception("iMD API URL extraction failed")
        await update_status(f"❌ Failed: {e}")


async def imd_session_token_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin pasted their session token — use it directly for API calls."""
    context.user_data.pop("awaiting_imd_session_token", None)
    raw = update.message.text.strip()

    try:
        await update.message.delete()
    except Exception:
        pass

    # Save the token so imd_api_url_reply can use it if endpoint probing fails
    context.user_data["imd_saved_token"] = raw

    status_msg = await context.bot.send_message(
        chat_id=ADMIN_CHAT_ID,
        text="🔄 Using your session to extract databases..."
    )

    async def update_status(text: str):
        try:
            await status_msg.edit_text(text)
        except Exception:
            pass

    try:
        databases = await extract_imd_with_token(raw, update_status)
        if not databases:
            await update_status(
                "❌ No databases found.\n\n"
                "The token might have expired. Please get a fresh one from Firefox."
            )
            return

        await update_status(f"💾 Saving {len(databases):,} databases...")
        db_imd_save_catalog(databases)
        await update_status(
            f"✅ iMD Catalog updated!\n\n"
            f"📚 {len(databases):,} databases saved.\n\n"
            f"Customers can now use 🔬 Search iMD Resources."
        )
    except Exception as e:
        logger.exception("iMD catalog extraction with token failed")
        await update_status(f"❌ Extraction failed: {e}")


async def extract_imd_with_token(token_raw: str, status_cb) -> list:
    """Calls imdweb.org's internal API using the session data the admin
    extracted from their browser's localStorage."""
    import aiohttp
    import json as _json

    token_raw = token_raw.strip()

    # Accept three formats:
    # 1. "Bearer eyJ..." — the actual JWT from the fetch interceptor (best)
    # 2. JSON localStorage dump — {"imd_web_resume":"...","imd_web_device_id":"..."}
    # 3. Raw token string
    bearer_token = None
    if token_raw.startswith("Bearer "):
        bearer_token = token_raw  # already complete
    elif token_raw.startswith("eyJ"):
        bearer_token = f"Bearer {token_raw}"
    else:
        # Parse localStorage JSON
        try:
            parsed = _json.loads(token_raw)
            if isinstance(parsed, dict):
                resume = (parsed.get("imd_web_resume") or
                          parsed.get("token") or parsed.get("auth"))
                if resume:
                    bearer_token = f"Bearer {resume}"
            elif isinstance(parsed, list):
                for pair in parsed:
                    if isinstance(pair, list) and len(pair) == 2:
                        if "resume" in str(pair[0]).lower():
                            bearer_token = f"Bearer {pair[1]}"
                            break
        except (_json.JSONDecodeError, TypeError):
            bearer_token = f"Bearer {token_raw.strip(chr(34)+chr(39))}"

    if not bearer_token:
        raise ValueError("Could not extract auth token. Send the full popup text.")

    await status_cb("🔑 Token ready. Calling iMD API...")

    # Use Bearer auth only — no new login, no cookie session creation.
    # iMD allows 1 login at a time; the Bearer JWT rides on the existing
    # Firefox session without creating a second one.
    base_headers = {
        "User-Agent": (
            "Mozilla/5.0 (Linux; Android 10; Mobile) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/128.0.0.0 Mobile Safari/537.36"
        ),
        "Accept": "application/json, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://imdweb.org/",
        "Origin": "https://imdweb.org",
        "Authorization": bearer_token,
    }

    databases = []

    # First: try to resume the session (the app likely calls an auth/resume
    # endpoint to exchange imd_web_resume for a full session token)
    await status_cb("🔄 Resuming iMD session...")
    session_token = None

    async with aiohttp.ClientSession() as session:
        # Try common session resume endpoints
        for resume_url in [
            "https://imdweb.org/api/auth/resume",
            "https://imdweb.org/api/session",
            "https://imdweb.org/api/user/session",
            "https://imdweb.org/api/v1/auth/resume",
            "https://imdweb.org/api/auth",
        ]:
            try:
                async with session.post(
                    resume_url,
                    json={"token": resume_token, "device_id": device_id},
                    headers=base_headers,
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json(content_type=None)
                        new_token = (
                            data.get("token") or data.get("access_token")
                            or data.get("auth_token") or data.get("jwt")
                        )
                        if new_token:
                            session_token = new_token
                            base_headers["Authorization"] = f"Bearer {session_token}"
                            await status_cb(f"✅ Session resumed. Searching for databases API...")
                            break
            except Exception:
                pass

        # Try all candidate database list endpoints
        candidate_endpoints = [
            # CONFIRMED working endpoint discovered via fetch interceptor
            "https://imdweb.org/api/labrange/downloads",
            # Fallbacks in case the above changes
            "https://imdweb.org/downloads",
            "https://imdweb.org/databases",
            "https://imdweb.org/api/databases",
            "https://imdweb.org/api/downloads",
        ]

        working_endpoint = None
        await status_cb("🔍 Probing iMD database API endpoints...")

        for url in candidate_endpoints:
            for param_set in [
                # Try without scope first to get ALL databases
                {"limit": 100, "offset": 0},
                {"limit": 100, "offset": 0, "scope": "all"},
                {"limit": 40,  "offset": 0},
                # Page-based fallback
                {"page": 1, "limit": 100},
                {},
            ]:
                try:
                    async with session.get(
                        url,
                        params=param_set,
                        headers=base_headers,
                        timeout=aiohttp.ClientTimeout(total=15),
                    ) as resp:
                        if resp.status in (200, 206):
                            ct = resp.headers.get("content-type", "")
                            if "json" in ct or "text" in ct:
                                data = await resp.json(content_type=None)
                                names = _extract_names_from_json(data)
                                if names:
                                    working_endpoint = url
                                    working_params = param_set
                                    databases.extend(names)
                                    logger.info(
                                        "Found iMD API: %s with params %s (%s items)",
                                        url, param_set, len(names),
                                    )
                                    break
                except Exception:
                    pass
            if working_endpoint:
                break

        if not working_endpoint:
            raise ValueError(
                "Could not reach the iMD API with this token.\n\n"
                "The token may have expired. Please:\n"
                "1. Open imdweb.org in Firefox\n"
                "2. Log in and click Agree\n"
                "3. Run javascript:alert(JSON.stringify(localStorage)) in the address bar\n"
                "4. Send the fresh token here"
            )

        # Paginate using offset-based pattern confirmed from iMD API:
        # /api/labrange/downloads?limit=40&offset=0 → offset=40 → offset=80 ...
        await status_cb(
            f"✅ API found! Downloading all databases "
            f"(found {len(databases):,} on first page, paginating...)..."
        )

        limit = working_params.get("limit", 100)
        offset = limit  # first page already fetched at offset=0
        consecutive_empty = 0

        while consecutive_empty < 3:
            fetched = False
            # Build next-page params — offset is primary, page is fallback
            base = {k: v for k, v in working_params.items()
                    if k not in ("offset", "page")}
            for params in [
                {**base, "offset": offset},
                {**base, "page": (offset // limit) + 1},
            ]:
                try:
                    async with session.get(
                        working_endpoint,
                        params=params,
                        headers=base_headers,
                        timeout=aiohttp.ClientTimeout(total=15),
                    ) as resp:
                        if resp.status == 200:
                            data = await resp.json(content_type=None)
                            names = _extract_names_from_json(data)
                            if names:
                                databases.extend(names)
                                consecutive_empty = 0
                                fetched = True
                                if len(databases) % 1000 < limit:
                                    await status_cb(
                                        f"📥 {len(databases):,} databases downloaded..."
                                    )
                                break
                except Exception:
                    pass
            if not fetched:
                consecutive_empty += 1
            offset += limit
            if offset > 200000:
                break

    # De-duplicate by name
    seen = set()
    unique = []
    for entry in databases:
        name = entry[0].strip() if entry and entry[0] else ""
        if name and name not in seen and len(name) > 2:
            seen.add(name)
            unique.append(entry)

    return unique


# ── Customer: search the local catalog ───────────────────────────────

IMD_SEARCH_PAGE_SIZE = 20


def render_imd_search_page(query: str, offset: int):
    """Builds the text + pagination keyboard for one page of iMD search
    results. Shared by the chat search flow so Prev/Next re-renders
    consistently with the initial search."""
    rows, total = db_imd_search(query, limit=IMD_SEARCH_PAGE_SIZE, offset=offset)
    if not rows:
        return None, None

    start = offset + 1
    end = offset + len(rows)
    lines = [f"🔬 *iMD Search: '{md_escape(query)}'*\n{total:,} result{'s' if total != 1 else ''} found\n"]
    for name, category in rows:
        cat = f" · _{md_escape(category)}_" if category else ""
        lines.append(f"📖 {md_escape(name)}{cat}")
    lines.append(f"\n_Showing {start}-{end} of {total:,}._")
    text = "\n".join(lines)

    nav = []
    if offset > 0:
        prev_offset = max(0, offset - IMD_SEARCH_PAGE_SIZE)
        nav.append(InlineKeyboardButton("◀️ Prev", callback_data=f"imdpage:{prev_offset}"))
    if end < total:
        nav.append(InlineKeyboardButton("Next ▶️", callback_data=f"imdpage:{offset + IMD_SEARCH_PAGE_SIZE}"))
    keyboard = InlineKeyboardMarkup([nav]) if nav else None

    return text, keyboard


async def imd_search_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    count = db_imd_catalog_count()
    if count == 0:
        await update.message.reply_text(
            "🔬 The iMD resource catalog hasn't been loaded yet.\n"
            "Please contact support — the admin needs to sync the catalog first."
        )
        return
    context.user_data["awaiting_imd_search"] = True
    live_search_button = InlineKeyboardMarkup([[
        InlineKeyboardButton("⚡ Live Search", switch_inline_query_current_chat="")
    ]])
    await update.message.reply_text(
        f"🔬 Search iMD Resources\n\n"
        f"Our catalog has {count:,} medical databases and textbooks.\n"
        "Type the name of what you're looking for below, "
        "or tap ⚡ *Live Search* for results that update as you type:",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=live_search_button,
    )


async def imd_search_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("awaiting_imd_search", None)
    query = update.message.text.strip()
    if not query or len(query) < 2:
        await update.message.reply_text("Please send at least 2 characters to search.")
        return

    context.user_data["last_imd_search_query"] = query
    text, keyboard = render_imd_search_page(query, offset=0)
    if not text:
        await update.message.reply_text(
            f"No results found for '{query}'.\n\n"
            "Try fewer words or different spelling."
        )
        return

    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard)


async def imd_search_page_nav(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Prev/Next button on a search results page."""
    query_cb = update.callback_query
    await query_cb.answer()
    offset = int(query_cb.data.split(":", 1)[1])
    query = context.user_data.get("last_imd_search_query")
    if not query:
        await query_cb.edit_message_text("This search has expired — please search again.")
        return
    text, keyboard = render_imd_search_page(query, offset)
    if not text:
        await query_cb.edit_message_text(f"No results found for '{query}'.")
        return
    await query_cb.edit_message_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard)


async def imd_inline_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Live, as-you-type search via Telegram inline mode — the only
    mechanism Telegram offers for showing results while the user is
    still typing (regular messages only arrive after Send is pressed).
    Triggered by typing '@<bot_username> query' in any chat, including
    this bot's own chat."""
    inline_query = update.inline_query
    text = (inline_query.query or "").strip()
    if len(text) < 2:
        await inline_query.answer([], cache_time=1, is_personal=True)
        return

    rows, total = db_imd_search(text, limit=25, offset=0)
    results = []
    for i, (name, category) in enumerate(rows):
        subtitle = category or ""
        results.append(
            InlineQueryResultArticle(
                id=str(i),
                title=name,
                description=subtitle,
                input_message_content=InputTextMessageContent(f"📖 {name}"),
            )
        )
    await inline_query.answer(results, cache_time=1, is_personal=True)


async def delivered_subscriptions_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin-only: all delivered subscriptions across all customers, most
    recent first. Shows customer, subscription, and delivery date so the
    admin has a quick overview of everything that's been sent out."""
    if update.effective_user.id != ADMIN_CHAT_ID:
        return

    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT d.item_id, d.delivered_at, d.user_id, u.username "
        "FROM deliveries d "
        "LEFT JOIN users u ON u.user_id = d.user_id "
        "ORDER BY d.delivered_at DESC "
        "LIMIT 100"
    ).fetchall()
    conn.close()

    if not rows:
        await update.message.reply_text("No subscriptions have been delivered yet.")
        return

    lines = [f"📦 Delivered Subscriptions ({len(rows)} most recent)\n"]
    for item_id, delivered_at, uid, username in rows:
        name = MENU.get(item_id, (item_id,))[0]
        who  = f"@{username}" if username else str(uid)
        date = delivered_at[:10] if delivered_at else "?"
        lines.append(f"✅ {name}\n   → {who}  ·  {date}")

    text = "\n".join(lines)
    for i in range(0, len(text), 3500):
        await update.message.reply_text(text[i:i + 3500])


async def credits_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin-only: every customer holding credits, highest balance first."""
    if update.effective_user.id != ADMIN_CHAT_ID:
        return

    rows = db_customers_with_credits()
    if not rows:
        await update.message.reply_text("No customers have any credits yet.")
        return

    lines = ["💳 Customer Credits:", ""]
    for user_id, username, credits in rows:
        who = f"@{username}" if username else str(user_id)
        lines.append(f"{who} (ID: {user_id}) — {credits} credits ({CURRENCY}{credits * 4})")

    text = "\n".join(lines)
    for i in range(0, len(text), 3500):
        await update.message.reply_text(text[i:i + 3500])


async def refresh_admin_keyboard(context: ContextTypes.DEFAULT_TYPE):
    """Pushes an updated admin keyboard so badge counts are current.
    Uses a zero-width space as text so the message is as unobtrusive as
    possible — it's just a carrier for the updated ReplyKeyboardMarkup."""
    if not ADMIN_CHAT_ID:
        return
    try:
        await context.bot.send_message(
            chat_id=ADMIN_CHAT_ID,
            text="​",  # zero-width space — invisible but satisfies Telegram's non-empty requirement
            reply_markup=admin_menu_keyboard(),
        )
    except Exception:
        logger.exception("Failed to refresh admin keyboard")


async def tickets_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin-only: top of the Tickets tab — unresolved vs resolved."""
    if update.effective_user.id != ADMIN_CHAT_ID:
        return
    unresolved = len(db_list_tickets("unresolved"))
    resolved = len(db_list_tickets("resolved"))
    await update.message.reply_text(
        "🎫 Tickets:",
        reply_markup=InlineKeyboardMarkup(
            [
                [InlineKeyboardButton(f"🔴 Unresolved ({unresolved})", callback_data="tickets:unresolved")],
                [InlineKeyboardButton(f"✅ Resolved ({resolved})", callback_data="tickets:resolved")],
            ]
        ),
    )


def _tickets_list_keyboard(status: str) -> InlineKeyboardMarkup:
    rows = db_list_tickets(status)
    buttons = []
    for ticket_id, user_id, username, category, sub_label, created_at in rows:
        who = f"@{username}" if username else str(user_id)
        what = sub_label or category
        buttons.append(
            [InlineKeyboardButton(f"#{ticket_id} — {who} — {what}"[:60], callback_data=f"ticketview:{ticket_id}")]
        )
    buttons.append([InlineKeyboardButton("⬅️ Back", callback_data="tickets_back")])
    return InlineKeyboardMarkup(buttons)


async def tickets_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query.from_user.id != ADMIN_CHAT_ID:
        await query.answer("Not authorized.", show_alert=True)
        return
    await query.answer()
    status = query.data.split(":", 1)[1]

    keyboard = _tickets_list_keyboard(status)
    label = "Resolved" if status == "resolved" else "Unresolved"
    if len(keyboard.inline_keyboard) == 1:  # only the Back button — list is empty
        await query.edit_message_text(f"No {label.lower()} tickets.", reply_markup=keyboard)
        return
    await query.edit_message_text(f"{label} tickets:", reply_markup=keyboard)


async def tickets_back(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    unresolved = len(db_list_tickets("unresolved"))
    resolved = len(db_list_tickets("resolved"))
    await query.edit_message_text(
        "🎫 Tickets:",
        reply_markup=InlineKeyboardMarkup(
            [
                [InlineKeyboardButton(f"🔴 Unresolved ({unresolved})", callback_data="tickets:unresolved")],
                [InlineKeyboardButton(f"✅ Resolved ({resolved})", callback_data="tickets:resolved")],
            ]
        ),
    )


async def ticket_view(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Shows one ticket's full detail. Sends a fresh message (rather than
    editing the list) since a screenshot may need to be attached, which a
    text message can't be edited into."""
    query = update.callback_query
    if query.from_user.id != ADMIN_CHAT_ID:
        await query.answer("Not authorized.", show_alert=True)
        return
    await query.answer()

    ticket_id = int(query.data.split(":", 1)[1])
    ticket = db_get_ticket(ticket_id)
    if not ticket:
        await context.bot.send_message(chat_id=ADMIN_CHAT_ID, text="That ticket no longer exists.")
        return

    (_, user_id, username, category, sub_item_id, sub_label, self_help,
     message, photo, status, resolution, created_at, resolved_at) = ticket
    who = f"@{username}" if username else str(user_id)

    text = (
        f"🎫 Ticket #{ticket_id} — {who} (ID: {user_id})\n"
        f"Category: {category}\n"
        + (f"Subscription: {sub_label}\n" if sub_label else "")
        + f"Status: {status}\n"
        + f"Opened: {created_at[:19]}\n"
        + (f"\nSelf-help shown:\n{self_help}\n" if self_help else "")
        + (f"\nMessage:\n{message}" if message else "\n(no message provided)")
        + (f"\n\nResolution:\n{resolution}" if resolution else "")
    )

    buttons = []
    if status == "unresolved":
        buttons.append([InlineKeyboardButton("✅ Mark Resolved", callback_data=f"ticketresolve:{ticket_id}")])
    buttons.append([InlineKeyboardButton("⬅️ Back", callback_data=f"tickets:{status}")])

    if photo:
        await context.bot.send_photo(
            chat_id=ADMIN_CHAT_ID, photo=photo, caption=text, reply_markup=InlineKeyboardMarkup(buttons)
        )
    else:
        await context.bot.send_message(
            chat_id=ADMIN_CHAT_ID, text=text, reply_markup=InlineKeyboardMarkup(buttons)
        )


async def ticket_resolve_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin tapped 'Mark Resolved' — wait for the closing message to send
    the customer."""
    query = update.callback_query
    if query.from_user.id != ADMIN_CHAT_ID:
        await query.answer("Not authorized.", show_alert=True)
        return
    await query.answer()

    ticket_id = int(query.data.split(":", 1)[1])
    clear_admin_flow_state(context.user_data)
    context.user_data["awaiting_ticket_resolution"] = ticket_id
    await context.bot.send_message(
        chat_id=ADMIN_CHAT_ID,
        text=f"Type the message to send the customer when closing ticket #{ticket_id}:",
    )


async def ticket_resolution_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Sends the admin's closing message to the customer and marks the
    ticket resolved."""
    ticket_id = context.user_data.pop("awaiting_ticket_resolution", None)
    if not ticket_id:
        return

    ticket = db_get_ticket(ticket_id)
    if not ticket:
        await update.message.reply_text("That ticket no longer exists.")
        return

    user_id = ticket[1]
    resolution_text = update.message.text
    db_resolve_ticket(ticket_id, resolution_text)

    await context.bot.send_message(
        chat_id=user_id,
        text=f"✅ Your ticket #{ticket_id} has been resolved:\n\n{resolution_text}",
    )
    await update.message.reply_text(f"✅ Ticket #{ticket_id} marked resolved and the customer notified.")
    await refresh_admin_keyboard(context)


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


def _inbox_summary_keyboard() -> InlineKeyboardMarkup:
    unread = len(db_inbox_messages(read=False))
    read = len(db_inbox_messages(read=True))
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(f"📩 Unread ({unread})", callback_data="inbox:unread")],
            [InlineKeyboardButton(f"📖 Read ({read})", callback_data="inbox:read")],
        ]
    )


async def inbox_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin-only: top of the Inbox — unread vs read customer messages.
    Sends an updated keyboard so the badge clears immediately as the admin
    opens the Inbox, even before reading individual messages."""
    if update.effective_user.id != ADMIN_CHAT_ID:
        return
    await update.message.reply_text("Inbox:", reply_markup=_inbox_summary_keyboard())
    await refresh_admin_keyboard(context)


async def inbox_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Lists messages in the chosen Unread/Read bucket."""
    query = update.callback_query
    if query.from_user.id != ADMIN_CHAT_ID:
        await query.answer("Not authorized.", show_alert=True)
        return
    await query.answer()

    which = query.data.split(":", 1)[1]
    rows = db_inbox_messages(read=(which == "read"))
    back = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="inbox_back")]])

    if not rows:
        label = "read" if which == "read" else "unread"
        await query.edit_message_text(f"No {label} messages.", reply_markup=back)
        return

    buttons = []
    for message_id, order_id, username, body, created_at, delivered in rows:
        who = f"@{username}" if username else f"Order #{order_id}"
        preview = body.replace("\n", " ")[:30]
        flag = "" if delivered else "⚠️ "
        buttons.append(
            [InlineKeyboardButton(f"{flag}{who}: {preview}"[:60], callback_data=f"inboxmsg:{message_id}")]
        )
    buttons.append([InlineKeyboardButton("⬅️ Back", callback_data="inbox_back")])

    await query.edit_message_text(
        f"{'📖 Read' if which == 'read' else '📩 Unread'} messages:",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def inbox_message_detail(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Shows one message in full, marks it read, and offers a Reply button."""
    query = update.callback_query
    if query.from_user.id != ADMIN_CHAT_ID:
        await query.answer("Not authorized.", show_alert=True)
        return
    await query.answer()

    message_id = int(query.data.split(":", 1)[1])
    row = db_get_message(message_id)
    back = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="inbox_back")]])

    if not row:
        await query.edit_message_text("That message no longer exists.", reply_markup=back)
        return

    _, order_id, user_id, direction, body, created_at, delivered = row
    db_mark_message_read(message_id)

    order = db_get_order(order_id)
    username = order[2] if order else None
    who = f"@{username}" if username else str(user_id)

    text = (
        f"From: {who} (Order #{order_id})\n"
        f"Sent: {created_at[:19]}\n"
        + ("⚠️ Live delivery to admin failed — found via Inbox only.\n" if not delivered else "")
        + f"\n{body}"
    )
    buttons = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("💬 Reply", callback_data=f"admin_msg:{order_id}")],
            [InlineKeyboardButton("⬅️ Back", callback_data="inbox_back")],
        ]
    )
    await query.edit_message_text(text, reply_markup=buttons)


async def inbox_back(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("Inbox:", reply_markup=_inbox_summary_keyboard())


async def admin_msg_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin tapped '💬 Message Customer' — wait for their next text and
    forward it, tagged with which order it's about."""
    query = update.callback_query
    if query.from_user.id != ADMIN_CHAT_ID:
        await query.answer("Not authorized.", show_alert=True)
        return
    await query.answer()

    order_id = int(query.data.split(":", 1)[1])
    clear_admin_flow_state(context.user_data)
    context.user_data["awaiting_admin_message_for_order"] = order_id
    await context.bot.send_message(
        chat_id=ADMIN_CHAT_ID, text=f"Type the message to send the customer about order #{order_id}:"
    )


async def admin_message_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Sends the admin's typed message to the customer, with a Reply button
    so they can answer directly from within the message. Recorded in the
    message log regardless of whether the live send succeeds."""
    order_id = context.user_data.pop("awaiting_admin_message_for_order", None)
    if not order_id:
        return

    order = db_get_order(order_id)
    if not order:
        await update.message.reply_text("Order not found.")
        return

    user_id = order[1]
    body = update.message.text

    try:
        await context.bot.send_message(
            chat_id=user_id,
            text=f"💬 Message about your order #{order_id}:\n\n{body}",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("↩️ Reply", callback_data=f"cust_reply:{order_id}")]]
            ),
        )
        db_add_message(order_id, user_id, "to_customer", body, delivered=True)
        await update.message.reply_text(f"✅ Message sent to the customer for order #{order_id}.")
    except Exception:
        logger.exception("Failed to deliver admin message for order #%s", order_id)
        db_add_message(order_id, user_id, "to_customer", body, delivered=False)
        await update.message.reply_text(
            f"⚠️ Could not deliver this to the customer for order #{order_id} — it's saved, "
            "but they may not have received it live. They may have blocked the bot, or it "
            "couldn't reach them."
        )


async def customer_reply_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Customer tapped '↩️ Reply' — wait for their next text and forward it
    to the admin, tagged with the same order."""
    query = update.callback_query
    await query.answer()
    order_id = int(query.data.split(":", 1)[1])
    context.user_data["awaiting_customer_reply_for_order"] = order_id
    await context.bot.send_message(chat_id=query.from_user.id, text="Type your reply:")


async def customer_reply_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Forwards the customer's reply to the admin, with a Reply button of
    its own so the conversation can continue. Always recorded in the
    message log — including when the live push fails — so 📥 Inbox is a
    reliable fallback the admin can check even if the chat notification
    never arrived."""
    order_id = context.user_data.pop("awaiting_customer_reply_for_order", None)
    if not order_id:
        return

    order = db_get_order(order_id)
    username = order[2] if order else None
    who = f"@{username}" if username else str(update.effective_user.id)
    body = update.message.text

    message_id = db_add_message(order_id, update.effective_user.id, "from_customer", body, delivered=bool(ADMIN_CHAT_ID))

    if not ADMIN_CHAT_ID:
        return

    try:
        await context.bot.send_message(
            chat_id=ADMIN_CHAT_ID,
            text=f"💬 Reply from {who} — Order #{order_id}:\n\n{body}",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("💬 Reply", callback_data=f"admin_msg:{order_id}")]]
            ),
        )
    except Exception:
        # The live push failed, but the message is already in the Inbox
        # (recorded above) — nothing is lost, it just needs to be checked
        # there instead of appearing directly in chat.
        logger.exception("Failed to deliver customer reply for order #%s to admin", order_id)

    # Push updated keyboard so the Inbox badge appears immediately.
    await refresh_admin_keyboard(context)


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
    if item_id in IMD_TRIGGER_ITEMS:
        # The customer already supplied the username/password, so manual
        # delivery just needs to send the standard iMD template — no
        # retyping. Offered regardless of state so a stuck item can always
        # be pushed through.
        if info_json:
            buttons.append(
                [InlineKeyboardButton("📤 Send iMD Details Now", callback_data=f"imddeliver:{fulfilment_id}")]
            )
        buttons.append(
            [InlineKeyboardButton("🔁 Retry Auto Registration", callback_data=f"reg_go:{order_id}")]
        )
        buttons.append(
            [InlineKeyboardButton("✍️ Type Details Instead", callback_data=f"deliver:{fulfilment_id}")]
        )
    elif state == "awaiting_delivery":
        buttons.append([InlineKeyboardButton("📤 Deliver Manually", callback_data=f"deliver:{fulfilment_id}")])
    buttons.append([InlineKeyboardButton("💬 Message Customer", callback_data=f"admin_msg:{order_id}")])
    buttons.append([InlineKeyboardButton("🗑 Delete", callback_data=f"pdel:{fulfilment_id}")])
    buttons.append([InlineKeyboardButton("⬅️ Back", callback_data="apend_back")])

    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(buttons))


async def pending_delete_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin tapped '🗑 Delete' — ask for confirmation before actually
    removing it, since this can't be undone."""
    query = update.callback_query
    if query.from_user.id != ADMIN_CHAT_ID:
        await query.answer("Not authorized.", show_alert=True)
        return
    await query.answer()

    fulfilment_id = int(query.data.split(":", 1)[1])
    row = db_get_fulfilment(fulfilment_id)
    if not row:
        await query.edit_message_text("That item no longer exists.")
        return

    _, order_id, user_id, item_id, unit_no, state, info_json = row
    name = MENU.get(item_id, (item_id,))[0]

    await query.edit_message_text(
        f"Delete {name}{f' #{unit_no}' if unit_no > 1 else ''} from Order #{order_id}?\n\n"
        "This removes it from Pending Orders permanently — the customer will NOT be notified "
        "and this can't be undone.",
        reply_markup=InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("✅ Yes, delete it", callback_data=f"pdel_yes:{fulfilment_id}")],
                [InlineKeyboardButton("❌ Cancel", callback_data=f"apend:{fulfilment_id}")],
            ]
        ),
    )


async def pending_delete_execute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Actually deletes the pending item after confirmation."""
    query = update.callback_query
    if query.from_user.id != ADMIN_CHAT_ID:
        await query.answer("Not authorized.", show_alert=True)
        return
    await query.answer()

    fulfilment_id = int(query.data.split(":", 1)[1])
    row = db_get_fulfilment(fulfilment_id)

    if not row:
        await query.edit_message_text("That item no longer exists.")
        return

    _, order_id, user_id, item_id, unit_no, state, info_json = row
    name = MENU.get(item_id, (item_id,))[0]

    db_delete_fulfilment(fulfilment_id)

    back = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back to Pending Orders", callback_data="apend_back")]])
    await query.edit_message_text(
        f"🗑 Deleted: {name}{f' #{unit_no}' if unit_no > 1 else ''} — Order #{order_id}",
        reply_markup=back,
    )


async def imd_manual_deliver(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Sends the standard iMD delivery message using the details the
    customer already submitted — used when automatic registration failed
    but the admin has registered the account by hand."""
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

    try:
        info = json.loads(info_json) if info_json else {}
    except (TypeError, ValueError):
        info = {}

    if not info:
        await query.edit_message_text(
            "No details on file for this item — use ✍️ Type Details Instead.", reply_markup=back
        )
        return

    is_renew = item_id in IMD_RENEW_ITEMS
    duration = IMD_DURATION_MAP.get(item_id)

    if is_renew:
        username = info.get("prev_username")
        password = None
    else:
        username = info.get("username")
        password = info.get("password")

    if not username:
        await query.edit_message_text(
            "No username on file for this item — use ✍️ Type Details Instead.", reply_markup=back
        )
        return

    message = build_imd_delivery_message(username, password, duration=duration)

    await context.bot.send_message(chat_id=user_id, text=message)
    db_save_delivery(order_id, message)
    db_add_delivery(order_id, user_id, item_id, message)
    await post_subscription_to_channel(context, order_id, item_id, message)
    db_set_fulfilment_state(fulfilment_id, "delivered")
    context.application.bot_data.get("pending_registrations", {}).pop(order_id, None)

    await query.edit_message_text(
        f"✅ iMD details sent to the customer (Order #{order_id}).", reply_markup=back
    )

    # Continue with any remaining items in the same order.
    await process_next_in_queue(context, user_id, order_id)


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
    """Lists available serials and any not_working ones so the admin can
    review and delete failed serials."""
    available = db_list_serials(status="available")
    not_working = db_list_serials(status="not_working")

    lines = []

    grouped = {"6m": [], "1y": []}
    for duration, code, status, used_for_order in available:
        grouped.setdefault(duration, []).append(code)

    lines.append("🔑 Available Serials")
    for duration, label in (("6m", "6 Months"), ("1y", "1 Year")):
        codes = grouped.get(duration, [])
        lines.append(f"\n  {label} — {len(codes)} available")
        if codes:
            lines.extend(f"    {code}" for code in codes)
        else:
            lines.append("    (none)")

    if not_working:
        lines.append("\n\n❌ Not Working Serials (rejected by iMD — review and delete)")
        nw_grouped = {"6m": [], "1y": []}
        for duration, code, status, used_for_order in not_working:
            nw_grouped.setdefault(duration, []).append(code)
        for duration, label in (("6m", "6 Months"), ("1y", "1 Year")):
            codes = nw_grouped.get(duration, [])
            if codes:
                lines.append(f"\n  {label}:")
                lines.extend(f"    ✕ {code}" for code in codes)

    if not available and not not_working:
        await update.message.reply_text("No serials in either pool.")
        return

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
    clear_admin_flow_state(context.user_data)
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

    elif mode == "broadcast":
        body = update.message.text  # unstripped — preserve the admin's formatting
        db_add_announcement(body)

        recipients = db_all_user_ids(exclude=ADMIN_CHAT_ID)
        sent, failed = 0, 0
        for user_id in recipients:
            try:
                # Send the announcement text AND a refreshed keyboard in
                # the same message so the 📢 badge appears immediately for
                # each recipient without a separate follow-up message.
                await context.bot.send_message(
                    chat_id=user_id,
                    text=f"📢 Announcement:\n\n{body}",
                    reply_markup=main_menu_keyboard(user_id),
                )
                sent += 1
            except Exception:
                failed += 1
            await asyncio.sleep(0.05)  # gentle on Telegram's rate limits

        await update.message.reply_text(
            f"✅ Broadcast sent.\nDelivered: {sent}\nFailed: {failed}\n\n"
            "It's also saved under customers' 📢 Announcements tab."
        )


async def text_state_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Single entry point for all free-text messages. Priority rules:
    1. Explicit Reply-button taps (most recent intent, never swallowed)
    2. All admin interactive states — ALL in one block so clear_admin_flow_state()
       guarantees only ONE can be active at a time (prevents the bug where
       awaiting_book_price_for intercepts text meant for awaiting_comment_for_order)
    3. Customer collection flows (registration, receipt, book link, ticket)
    4. Generic text (not a command or button tap)"""

    # ── 1. Explicit reply-button taps ────────────────────────────────
    if context.user_data.get("awaiting_customer_reply_for_order"):
        await customer_reply_text(update, context)
        return

    # ── 2. Admin interactive states (all here, in a single block) ────
    if update.effective_user.id == ADMIN_CHAT_ID:
        if context.user_data.get("awaiting_admin_message_for_order"):
            await admin_message_reply(update, context)
            return
        if context.user_data.get("awaiting_comment_for_order"):
            await admin_comment_reply(update, context)
            return
        if context.user_data.get("awaiting_book_price_for"):
            await book_price_reply(update, context)
            return
        if context.user_data.get("awaiting_imd_catalog_username"):
            await imd_catalog_username_reply(update, context)
            return
        if context.user_data.get("awaiting_imd_catalog_password"):
            await imd_catalog_password_reply(update, context)
            return
        if context.user_data.get("awaiting_imd_diag_username"):
            await imd_diag_username_reply(update, context)
            return
        if context.user_data.get("awaiting_imd_diag_password"):
            await imd_diag_password_reply(update, context)
            return
        if context.user_data.get("awaiting_imd_session_token"):
            await imd_session_token_reply(update, context)
            return
        if context.user_data.get("awaiting_imd_api_url"):
            await imd_api_url_reply(update, context)
            return
        if context.user_data.get("awaiting_new_sub_field"):
            await new_subscription_field_reply(update, context)
            return
        if context.user_data.get("awaiting_admin_input"):
            await admin_input_reply(update, context)
            return
        if context.user_data.get("awaiting_ticket_resolution"):
            await ticket_resolution_reply(update, context)
            return
        if context.user_data.get("awaiting_credentials_fulfilment"):
            await credentials_reply(update, context)
            return

    # ── 3. Customer collection flows ─────────────────────────────────
    if context.user_data.get("awaiting_ticket_field") == "message":
        await ticket_field_reply(update, context)
        return

    if context.user_data.get("awaiting_book_link"):
        await book_link_reply(update, context)
        return

    if context.user_data.get("awaiting_imd_search"):
        await imd_search_query(update, context)
        return

    if context.user_data.get("awaiting_registration_field"):
        await registration_field_reply(update, context)
        return

    if context.user_data.get("awaiting_generic_field"):
        await generic_field_reply(update, context)
        return

    if context.user_data.get("awaiting_payer_name_for_order"):
        await payer_name_reply(update, context)
        return


async def start_order_fulfilment(context: ContextTypes.DEFAULT_TYPE, order_id: int, user_id: int, items: dict):
    """Records every purchased unit of the paid order, then starts
    collecting details for the first one.

    Wrapped in error handling because a failure here means the customer is
    told their payment succeeded and then never asked for anything — a
    silent dead end. If it breaks, the admin gets told instead."""
    try:
        db_add_fulfilment_items(order_id, user_id, items)
        await process_next_in_queue(context, user_id, order_id)
    except Exception as exc:
        logger.exception("Failed to start fulfilment for order #%s", order_id)
        if ADMIN_CHAT_ID:
            await context.bot.send_message(
                chat_id=ADMIN_CHAT_ID,
                text=(
                    f"⚠️ Could not start fulfilment for order #{order_id} — the customer has NOT "
                    f"been asked for their details.\n\nError: {exc}"
                ),
            )


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
        # Nothing left for the customer to fill in.
        # If there are non-iMD items still waiting for admin delivery, send
        # the 48-hour notice here — this is the single canonical place so
        # it fires exactly once whether the customer used the Mini App (where
        # generic_field_reply never runs) or the bot's own registration flow.
        conn = sqlite3.connect(DB_PATH)
        waiting = conn.execute(
            "SELECT item_id FROM fulfilment WHERE order_id = ? AND state = 'awaiting_delivery'",
            (order_id,),
        ).fetchall() if order_id else []
        conn.close()
        manual_items = [
            iid for (iid,) in waiting
            if iid not in IMD_TRIGGER_ITEMS and not iid.startswith("book_")
        ]
        if manual_items and user_id:
            try:
                await context.bot.send_message(
                    chat_id=user_id,
                    text=DELIVERY_48H_MESSAGE,
                )
            except Exception:
                logger.exception("Failed to send 48h notice to user %s", user_id)
        return
    fulfilment_id, item_id = nxt

    item_name = MENU.get(item_id, (item_id, 0))[0]
    customer_data = context.application.user_data[user_id]

    # Before asking the customer anything, check if credentials were already
    # collected (Mini App Step 2 or any pre-payment collection). If info_json
    # is already set, use it directly — never ask twice.
    existing_row = db_get_fulfilment(fulfilment_id)
    existing_info_json = existing_row[6] if existing_row else None
    if existing_info_json:
        existing_info = json.loads(existing_info_json)

        if item_id in IMD_TRIGGER_ITEMS:
            # iMD with pre-stored credentials: trigger the Playwright automation
            # DIRECTLY using the stored data. Do NOT call start_imd_collection —
            # that asks the customer for credentials interactively, causing the
            # "33 messages" infinite-recursion bug seen in production.
            #
            # CRITICAL: change state BEFORE the recursive call below, otherwise
            # the next call finds the same needs_info item and loops forever.
            db_set_fulfilment_state(fulfilment_id, "awaiting_delivery")

            is_renew  = item_id.startswith("imd_renew")
            duration  = "6m" if "6m" in item_id else "1y"

            reg_data = {
                "duration":      duration,
                "is_renew":      is_renew,
                "order_id":      order_id,
                "item_id":       item_id,
                "fulfilment_id": fulfilment_id,
                "user_id":       user_id,
            }
            if is_renew:
                reg_data["prev_username"] = existing_info.get("prev_username", "")
            else:
                reg_data.update({
                    "email":    existing_info.get("email", ""),
                    "username": existing_info.get("username", ""),
                    "password": existing_info.get("password", ""),
                })

            pending = context.application.bot_data.setdefault("pending_registrations", {})
            pending[order_id] = reg_data

            # Run automation in background so it doesn't block the queue loop
            context.application.create_task(run_imd_registration(context, order_id))

        elif item_id.startswith("book_"):
            db_set_fulfilment_state(fulfilment_id, "awaiting_delivery")
            if ADMIN_CHAT_ID:
                await context.bot.send_message(
                    chat_id=ADMIN_CHAT_ID,
                    text=(
                        f"📚 Book paid — Order #{order_id}\n{item_name}\n\n"
                        "Deliver using the 📤 Send Book button on this order."
                    ),
                    reply_markup=InlineKeyboardMarkup(
                        [[InlineKeyboardButton("📤 Send Book", callback_data=f"deliver:{fulfilment_id}")]]
                    ),
                )
        else:
            # Non-iMD: state MUST be changed before the recursive call below
            db_set_fulfilment_state(fulfilment_id, "awaiting_delivery")
            atype = existing_info.get("account_type", "new")
            if atype == "new":
                cred_lines = (
                    f"First name: {existing_info.get('first_name','')}\n"
                    f"Last name: {existing_info.get('last_name','')}\n"
                    f"Email: {existing_info.get('email','')}\n"
                    f"Username: {existing_info.get('username','')}\n"
                    f"Password: {existing_info.get('password','')}"
                )
                tag = "🆕 NEW"
            else:
                cred_lines = (
                    f"Username/Email: {existing_info.get('login_username','')}\n"
                    f"Password: {existing_info.get('login_password','')}"
                )
                tag = "🔄 RENEWAL"
            if ADMIN_CHAT_ID:
                await context.bot.send_message(
                    chat_id=ADMIN_CHAT_ID,
                    text=(
                        f"📝 Ready to deliver ({tag}) — Order #{order_id}\n"
                        f"Product: {item_name}\n\n{cred_lines}\n\n"
                        "Deliver using the 📤 Send Credentials button."
                    ),
                    reply_markup=InlineKeyboardMarkup(
                        [[InlineKeyboardButton("📤 Send Credentials",
                                               callback_data=f"deliver:{fulfilment_id}")]]
                    ),
                )

        # Advance the queue — safe because state was already changed above
        # so the next call won't re-process this item
        await process_next_in_queue(context, user_id, order_id)
        return

    if item_id in IMD_TRIGGER_ITEMS:
        await start_imd_collection(context, order_id, user_id, item_id, fulfilment_id)
    elif item_id.startswith("book_"):
        # A book request already has everything it needs (the link was
        # collected up front, before payment) — skip straight to the
        # admin delivering it, same as any other manual delivery.
        db_set_fulfilment_state(fulfilment_id, "awaiting_delivery")
        if ADMIN_CHAT_ID:
            await context.bot.send_message(
                chat_id=ADMIN_CHAT_ID,
                text=(
                    f"📚 Book paid — Order #{order_id}\n"
                    f"{item_name}\n\n"
                    "Deliver it using the 📤 Send Book button on this order."
                ),
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("📤 Send Book", callback_data=f"deliver:{fulfilment_id}")]]
                ),
            )
    else:
        # Store which unit this is for, but wait for New/Renewal before
        # starting either field list.
        customer_data["generic_fulfilment_id"] = fulfilment_id
        customer_data["generic_item_id"] = item_id
        customer_data["generic_order_id"] = order_id
        await context.bot.send_message(
            chat_id=user_id,
            text=f"📝 Let's set up your {item_name}. Is this a:",
            reply_markup=InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton("🆕 New Account", callback_data="gentype:new")],
                    [InlineKeyboardButton("🔄 Renewal of a previous account", callback_data="gentype:renew")],
                ]
            ),
        )


async def generic_type_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Customer answered New Account vs Renewal — starts whichever field
    list applies. Not used for iMD, which already has its own separate
    New/Renew purchase choice made before checkout."""
    query = update.callback_query
    await query.answer()
    account_type = query.data.split(":", 1)[1]  # "new" or "renew"

    fulfilment_id = context.user_data.get("generic_fulfilment_id")
    item_id = context.user_data.get("generic_item_id")
    if not fulfilment_id or not item_id:
        await query.edit_message_text("This step has expired — please check 📋 My Subscriptions to continue.")
        return

    item_name = MENU.get(item_id, (item_id, 0))[0]
    context.user_data["generic_account_type"] = account_type
    context.user_data["generic_data"] = {}

    fields = GENERIC_FIELDS if account_type == "new" else RENEWAL_FIELDS
    context.user_data["awaiting_generic_field"] = fields[0][0]

    label = "New Account" if account_type == "new" else "Renewal"
    # Plain text on purpose: the password rules mention symbols like _ and
    # #, and Telegram's Markdown parser breaks on unescaped underscores.
    await query.edit_message_text(f"📝 {item_name} — {label}\n\n{fields[0][1]}")


async def generic_field_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Collects either the New Account fields (first/last name, email,
    username, password — with uniqueness checks on email/username) or the
    Renewal fields (existing login + password, no uniqueness checks) for
    a non-iMD subscription, depending on which the customer chose."""
    field = context.user_data.get("awaiting_generic_field")
    if not field:
        return

    account_type = context.user_data.get("generic_account_type", "new")
    fields = GENERIC_FIELDS if account_type == "new" else RENEWAL_FIELDS
    item_id = context.user_data.get("generic_item_id")

    value = update.message.text.strip()

    if account_type == "new":
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

        # Uniqueness — only meaningful for a brand-new registration, and
        # only checkable against what this customer has submitted to us
        # before for this exact subscription.
        if field in ("email", "username") and item_id:
            if db_credential_already_used(update.effective_user.id, item_id, field, value):
                await update.message.reply_text(
                    f"You've already used this {field} to register for this subscription before. "
                    "Please provide a different one:"
                )
                return

    data = context.user_data.setdefault("generic_data", {})
    data[field] = value

    field_names = [f[0] for f in fields]
    idx = field_names.index(field)

    if idx + 1 < len(fields):
        next_field, next_prompt = fields[idx + 1]
        context.user_data["awaiting_generic_field"] = next_field
        await update.message.reply_text(next_prompt)
        return

    # All fields collected — hand off to the admin for manual fulfilment.
    order_id = context.user_data.pop("generic_order_id", None)
    item_id = context.user_data.pop("generic_item_id", None)
    fulfilment_id = context.user_data.pop("generic_fulfilment_id", None)
    context.user_data.pop("awaiting_generic_field", None)
    context.user_data.pop("generic_data", None)
    context.user_data.pop("generic_account_type", None)

    await update.message.reply_text(
        "✅ Details received — we'll get your subscription set up and notify you here.",
    )

    if fulfilment_id:
        db_set_fulfilment_info(fulfilment_id, data)
        db_set_fulfilment_state(fulfilment_id, "awaiting_delivery")

    # Record the credentials so future New Account attempts for this same
    # subscription can be checked against them — only applies to genuinely
    # new registrations, not renewals (which reuse an existing account).
    if account_type == "new" and item_id:
        db_record_used_credentials(
            update.effective_user.id, item_id, data.get("email"), data.get("username")
        )

    if ADMIN_CHAT_ID and order_id:
        item_name = MENU.get(item_id, (item_id, 0))[0]
        if account_type == "new":
            detail = (
                f"First name: {data.get('first_name')}\n"
                f"Last name: {data.get('last_name')}\n"
                f"Email: {data.get('email')}\n"
                f"Username: {data.get('username')}\n"
                f"Password: {data.get('password')}\n\n"
            )
            tag = "🆕 NEW ACCOUNT"
        else:
            detail = (
                f"Username/Email: {data.get('login_username')}\n"
                f"Password: {data.get('login_password')}\n\n"
            )
            tag = "🔄 RENEWAL"

        await context.bot.send_message(
            chat_id=ADMIN_CHAT_ID,
            text=(
                f"📝 Manual fulfilment needed — Order #{order_id} ({tag})\n"
                f"Product: {item_name}\n\n"
                f"{detail}"
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

    # Persist the customer's details on the fulfilment row too, so they show
    # in Pending Orders and survive a restart — bot_data alone would leave
    # the admin with nothing to work from if auto-registration fails.
    if fulfilment_id:
        db_set_fulfilment_info(fulfilment_id, data)
        db_set_fulfilment_state(fulfilment_id, "awaiting_delivery")

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
        # The warm-up-via-register-page trick was only needed while ess.php
        # was being Cloudflare-challenged harder than the register page.
        # That's been fixed on iMD's side, so this goes back to a direct
        # call, same as registration.
        status, detail, page_text = await attempt_imd_action(
            IMD_RENEW_URL,
            IMD_RENEW_FIELD_MAP,
            {"username": data.get("prev_username"), "serial": serial_code},
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
            await post_subscription_to_channel(context, order_id, data.get("item_id"), delivery_message)
            if data.get("fulfilment_id"):
                db_set_fulfilment_state(data["fulfilment_id"], "delivered")
            # This order may contain more items — move on to the next one.
            await process_next_in_queue(context, customer_user_id, order_id)
        return

    # Everything below is a non-success. For serial errors, mark the serial
    # as not_working (not back to available — it would fail again) and retry
    # automatically with the next available serial before bothering the admin.
    if status == "serial_error":
        db_finalize_serial(serial_id, order_id, success=False, mark_not_working=True)
        logger.warning(
            "iMD serial %s rejected for order #%s — marked not_working, trying next serial",
            serial_code, order_id,
        )
        # Try next available serial of the same duration
        next_serial = db_pop_serial(duration)
        if next_serial:
            next_serial_id, next_serial_code = next_serial
            pending[order_id] = (next_serial_id, next_serial_code)
            await context.bot.send_message(
                chat_id=ADMIN_CHAT_ID,
                text=(
                    f"🔄 Serial `{serial_code}` failed for order #{order_id} — "
                    f"marked as not working, trying next serial `{next_serial_code}`..."
                ),
            )
            # Re-run the registration with the new serial (same data/context)
            context.application.create_task(
                run_imd_registration(context, order_id)
            )
        else:
            # Pool exhausted — notify admin to add more serials
            await context.bot.send_message(
                chat_id=ADMIN_CHAT_ID,
                text=(
                    f"🔴 ALL SERIALS EXHAUSTED — Order #{order_id} ({action_label})\n\n"
                    f"Serial `{serial_code}` and all others in the pool have been tried and failed.\n"
                    "Add a working serial and then retry the order.\n\n"
                    f"{detail}"
                ),
                reply_markup=retry_button,
            )
        return

    # Everything else (username_error, email_error, etc.) — serial goes back
    # to the pool (it's not the serial's fault).
    db_finalize_serial(serial_id, order_id, success=False)

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
    # Renewals in particular are often blocked by Cloudflare before the form
    # even loads, so put the one-tap manual delivery right here rather than
    # making the admin go hunting for it in Pending Orders.
    fulfilment_id = data.get("fulfilment_id")
    fallback_buttons = []
    if fulfilment_id:
        fallback_buttons.append(
            [InlineKeyboardButton("📤 Send iMD Details Now", callback_data=f"imddeliver:{fulfilment_id}")]
        )
    fallback_buttons.append(
        [InlineKeyboardButton(f"🔁 Retry {action_label}", callback_data=f"reg_go:{order_id}")]
    )

    who = data.get("prev_username") if is_renew else data.get("username")
    await context.bot.send_message(
        chat_id=ADMIN_CHAT_ID,
        text=(
            f"⚠️ COULD NOT CONFIRM — Order #{order_id} ({action_label})\n\n"
            f"Nothing was sent to the customer and serial {serial_code} went back to the pool.\n\n"
            f"Username: {who}\n"
            f"Serial to use: {serial_code} (still available in the pool)\n\n"
            f"Register this manually on iMD, then tap 📤 Send iMD Details Now to deliver it.\n\n{detail}"
        ),
        reply_markup=InlineKeyboardMarkup(fallback_buttons),
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


async def wait_for_challenge(page, attempts: int = 15, interval_ms: int = 3000) -> str:
    """Waits out a Cloudflare interstitial. Returns the final page title."""
    for _ in range(attempts):
        title = await page.title()
        if "Just a moment" not in title:
            return title
        await page.wait_for_timeout(interval_ms)
    return await page.title()


async def attempt_imd_action(url: str, field_map: dict, values: dict, warmup_url: str = None):
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

            # Each run starts a fresh browser with no Cloudflare clearance
            # cookie, and some pages (ess.php) are challenged harder than
            # others (register/index.php). Loading the easier page first lets
            # the challenge clear; the resulting cookie is domain-wide, so the
            # target page then loads normally in the same session.
            if warmup_url and warmup_url != url:
                try:
                    await page.goto(warmup_url, wait_until="networkidle", timeout=30000)
                    await wait_for_challenge(page, attempts=10)
                except Exception:
                    pass  # warm-up is best effort

            await page.goto(url, wait_until="networkidle", timeout=30000)
            title = await wait_for_challenge(page, attempts=15)

            if "Just a moment" in title:
                # Reloading after the challenge script has run often
                # succeeds where the first load didn't.
                try:
                    await page.reload(wait_until="networkidle", timeout=30000)
                    title = await wait_for_challenge(page, attempts=10)
                except Exception:
                    pass

            if "Just a moment" in title:
                content = await page.content()
                await browser.close()
                return "error", f"Still blocked by Cloudflare before the form loaded. Title: {title}\n{content[:500]}", ""

            for key, field_name in field_map.items():
                if key in values and values[key] is not None:
                    await page.fill(f'input[name="{field_name}"]', values[key])

            await page.click("#submit")
            await page.wait_for_load_state("networkidle", timeout=30000)

            # Wait out the post-submit Cloudflare interstitial — up to ~60s.
            # We must reach the real result page; classifying the challenge
            # page itself tells us nothing about whether it worked.
            result_title = await wait_for_challenge(page, attempts=20)
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
    Omits the password line for renewals (password left as None), and
    labels the date line "Valid until:" for renewals vs "Date:" for new
    registrations — password being None is what distinguishes the two.
    `date_str` overrides the date shown — used for extensions, where iMD
    tells us the actual new expiry date, which is more useful to the
    customer than today's date."""
    is_renewal = password is None
    if not date_str:
        date_str = datetime.now().strftime("%Y-%m-%d")
    date_label = "Valid until" if is_renewal else "Date"
    duration_label = "6 months" if duration == "6m" else "1 year"
    password_line = f"Password: {password}\n" if password else ""
    return (
        f"✅ IMD {duration_label} Full Access \n\n"
        f"{date_label}: {date_str}\n\n"
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
    await post_subscription_to_channel(context, order_id, item_id, credentials_text)
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
    credits = db_get_credits(user_id)

    if not delivered and not pending and not credits:
        await update.message.reply_text(f"No orders found for {query_arg}.")
        return

    parts = [f"Customer {query_arg} (ID: {user_id})"]
    parts.append(f"💳 Credits: {credits} ({CURRENCY}{credits * 4})")

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

async def web_app_data_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Receives the full order from the Mini App: items with account details
    and the chosen payment method. Creates the order, stores all the
    registration details in fulfilment rows, and routes to the right next
    step — Stars/Credits go straight to fulfilment, manual payment methods
    ask for one receipt photo."""
    if not update.message or not update.message.web_app_data:
        return
    try:
        data = json.loads(update.message.web_app_data.data)
    except (json.JSONDecodeError, TypeError):
        await update.message.reply_text("Something went wrong reading your order — please try again.")
        return

    action = data.get("action")
    user_id = update.effective_user.id
    if action == "view_subscriptions":
        delivered = db_user_delivered_units(user_id)
        if not delivered:
            await update.message.reply_text(
                "You don't have any delivered subscriptions yet.\n\n"
                "Once your orders are processed, they'll appear here.",
                reply_markup=main_menu_keyboard(user_id),
            )
            return
        lines = []
        for delivery_id, item_id, delivered_at in delivered:
            name = MENU.get(item_id, (item_id,))[0]
            date = delivered_at[:10] if delivered_at else ""
            lines.append(f"✅ {name}" + (f" — {date}" if date else ""))
        await update.message.reply_text(
            "📋 Your delivered subscriptions:\n\n" + "\n".join(lines),
            reply_markup=main_menu_keyboard(user_id),
        )
        return

    if action == "complete_registration":
        fid = data.get("fulfilment_id")
        oid = data.get("order_id")
        if not fid or not oid:
            await update.message.reply_text("Could not find that pending order. Please contact support.")
            return
        # Verify it belongs to this user and still needs action
        conn = sqlite3.connect(DB_PATH)
        row = conn.execute(
            "SELECT item_id, state FROM fulfilment WHERE id = ? AND user_id = ? AND order_id = ?",
            (fid, user_id, oid),
        ).fetchone()
        conn.close()
        if not row:
            await update.message.reply_text("Order not found or already processed.")
            return
        item_id, state = row
        if state == "awaiting_delivery":
            await update.message.reply_text("✅ Your subscription is being prepared. We'll deliver it shortly.")
            return
        await update.message.reply_text(
            f"Let's complete your registration for {MENU.get(item_id,(item_id,))[0]}:",
            reply_markup=main_menu_keyboard(user_id),
        )
        await process_next_in_queue(context, user_id, oid)
        return

    if action == "send_receipt":
        oid = data.get("order_id")
        if not oid:
            await update.message.reply_text("Could not find that order. Please contact support.")
            return
        pending = context.user_data.setdefault("pending_receipt_orders", [])
        if oid not in pending:
            pending.append(oid)
        context.user_data["awaiting_receipt_for_order"] = pending[0]
        await update.message.reply_text(
            "Please send a photo of your payment receipt:",
            reply_markup=main_menu_keyboard(user_id),
        )
        return

    if action == "view_pending":
        pending = db_user_pending_items(user_id)
        if not pending:
            await update.message.reply_text(
                "You have no pending orders right now.",
                reply_markup=main_menu_keyboard(user_id),
            )
            return
        lines = []
        for fid, oid, item_id, unit_no, state in pending:
            name = MENU.get(item_id, (item_id,))[0]
            state_label = {
                "needs_info": "⌛ Waiting for your details",
                "awaiting_delivery": "🔄 Being prepared",
            }.get(state, "⏳ In progress")
            lines.append(f"{state_label}: {name}")
        await update.message.reply_text(
            "⏳ Your pending orders:\n\n" + "\n".join(lines),
            reply_markup=main_menu_keyboard(user_id),
        )
        return

    if action not in ("cart_checkout", "full_order"):
        return

    items_raw    = data.get("items", [])
    pay_method   = data.get("payment_method", "")
    total_sent   = float(data.get("total", 0))
    user_id      = update.effective_user.id
    username     = update.effective_user.username

    if not items_raw:
        await update.message.reply_text("Your cart was empty.")
        return

    # Build the items dict for the order, checking stock
    order_items = {}
    skipped     = []
    for entry in items_raw:
        item_id = entry.get("id")
        if not item_id or item_id not in MENU:
            skipped.append(entry.get("name") or item_id or "unknown")
            continue
        if db_is_out_of_stock(item_id):
            skipped.append(MENU[item_id][0] + " (out of stock)")
            continue
        order_items[item_id] = order_items.get(item_id, 0) + 1

    if not order_items:
        await update.message.reply_text("None of your items are available right now.\n" + "\n".join(skipped))
        return

    total = sum(MENU[i][1] * q for i, q in order_items.items())
    order_id = db_create_order(user_id, username, order_items, total)
    db_set_payment_method(order_id, pay_method or "Mini App")

    # Create a fulfilment row for each unit and pre-populate it with the
    # registration details the customer already filled in the Mini App.
    db_add_fulfilment_items(order_id, user_id, order_items)

    # Store credentials from the Mini App directly into fulfilment rows —
    # using a targeted query for THIS order so we get the exact rows we just
    # inserted (db_all_pending_items queries for admin view and returns a
    # different set of rows than what we need here).
    detail_map = {}
    for entry in items_raw:
        iid = entry.get("id")
        if iid and iid in order_items:
            detail_map.setdefault(iid, []).append(entry)

    conn = sqlite3.connect(DB_PATH)
    order_fuls = conn.execute(
        "SELECT id, item_id, unit_no FROM fulfilment WHERE order_id = ? ORDER BY id",
        (order_id,),
    ).fetchall()
    conn.close()

    for fid, iid, unit_no in order_fuls:
        details_list = detail_map.get(iid, [])
        detail_entry = details_list[unit_no - 1] if unit_no - 1 < len(details_list) else {}
        if not detail_entry:
            continue
        info = {k: v for k, v in detail_entry.items() if k not in ("id", "account_type")}
        info["account_type"] = detail_entry.get("account_type", "new")
        db_set_fulfilment_info(fid, info)
        # Mark awaiting_delivery so process_next_in_queue skips re-asking
        # for credentials — they've already been collected in the Mini App.
        if iid not in IMD_TRIGGER_ITEMS:
            db_set_fulfilment_state(fid, "awaiting_delivery")
            # Notify admin immediately with the stored credentials so they
            # can register as soon as the payment is confirmed.
            item_name = MENU.get(iid, (iid,))[0]
            atype = detail_entry.get("account_type", "new")
            if atype == "new":
                cred_lines = (
                    f"First name: {info.get('first_name','')}\n"
                    f"Last name: {info.get('last_name','')}\n"
                    f"Email: {info.get('email','')}\n"
                    f"Username: {info.get('username','')}\n"
                    f"Password: {info.get('password','')}"
                )
                tag = "🆕 NEW"
            else:
                cred_lines = (
                    f"Username/Email: {info.get('login_username','')}\n"
                    f"Password: {info.get('login_password','')}"
                )
                tag = "🔄 RENEWAL"
            if ADMIN_CHAT_ID:
                try:
                    await context.bot.send_message(
                        chat_id=ADMIN_CHAT_ID,
                        text=(
                            f"📝 Credentials received ({tag}) — Order #{order_id} (awaiting payment)\n"
                            f"Product: {item_name}\n\n{cred_lines}\n\n"
                            "Deliver once payment is confirmed using the 📤 Send Credentials button."
                        ),
                        reply_markup=InlineKeyboardMarkup(
                            [[InlineKeyboardButton("📤 Send Credentials",
                                                   callback_data=f"deliver:{fid}")]]
                        ),
                    )
                except Exception:
                    logger.exception("Failed to send pre-payment credentials to admin")


    # Summary line for the customer confirmation message
    lines = [f"{q}× {MENU[i][0]}" for i, q in order_items.items()]
    summary = "\n".join(lines)
    if skipped:
        summary += "\n\n⚠️ Skipped (out of stock):\n" + "\n".join(skipped)

    # ── Route based on payment method ───────────────────
    pay_lower = pay_method.lower()

    if pay_lower == "credits":
        required = math.ceil(total / 4)
        if not db_deduct_credits(user_id, required):
            await update.message.reply_text(
                f"❌ Not enough credits. You need {required} credits (${total:.2f}) — "
                "earn more via the 🎁 Get Free Accounts tab."
            )
            db_update_status(order_id, "cancelled")
            return
        db_update_status(order_id, "paid")
        db_set_payment_method(order_id, "Credits — Mini App")
        await update.message.reply_text(
            f"✅ Paid with credits!\n\n{summary}\n\nTotal: {CURRENCY}{total:.2f}\n\n"
            "We're preparing your subscription(s) now."
        )
        await start_order_fulfilment(context, order_id, user_id, order_items)
        await award_referral_credit(context, user_id)
        await post_receipt_to_payments_channel(context, order_id)
        return

    if pay_lower == "stars":
        # Create a Stars invoice — the customer pays in Telegram, the
        # successful_payment_callback handles fulfilment.
        db_update_status(order_id, "awaiting_payment")
        amount_stars = max(1, round(total))
        try:
            await update.message.reply_invoice(
                title      = "Medic SalesBot Order",
                description= summary[:255],
                payload    = f"order_{order_id}",
                currency   = "XTR",
                prices     = [{"label": "Total", "amount": amount_stars}],
            )
        except Exception:
            logger.exception("Failed to send Stars invoice for order #%s", order_id)
            await update.message.reply_text(
                f"Order #{order_id} created. Total: {CURRENCY}{total:.2f}\n\n"
                "Stars invoice failed — please use another payment method or contact support."
            )
        return

    # Manual payment methods (local, card, crypto) — confirm the order and
    # ask for a receipt photo. Everything else is already stored.
    db_update_status(order_id, "awaiting_receipt")
    # Queue-based: don't overwrite existing pending receipt orders
    _pending = context.user_data.setdefault("pending_receipt_orders", [])
    if order_id not in _pending:
        _pending.append(order_id)
    context.user_data["awaiting_receipt_for_order"] = _pending[0]

    await update.message.reply_text(
        f"✅ Order #{order_id} confirmed!\n\n{summary}\n\nTotal: {CURRENCY}{total:.2f}\n"
        f"Payment: {pay_method}\n\n"
        "Please upload a screenshot of your payment receipt:"
    )

    # Notify admin
    if ADMIN_CHAT_ID:
        item_names = ", ".join(MENU[i][0] for i in order_items)
        who = f"@{username}" if username else str(user_id)
        detail_lines = []
        for entry in items_raw:
            iid = entry.get("id", "")
            atype = entry.get("account_type", "new")
            creds = {k: v for k, v in entry.items() if k not in ("id","account_type")}
            cred_str = " | ".join(f"{k}: {v}" for k, v in creds.items())
            detail_lines.append(f"  [{atype.upper()}] {MENU.get(iid,(iid,))[0]}: {cred_str}")
        details_block = "\n".join(detail_lines)
        try:
            await context.bot.send_message(
                chat_id=ADMIN_CHAT_ID,
                text=(
                    f"📱 Mini App Order #{order_id} — {who}\n"
                    f"Payment: {pay_method}\nTotal: {CURRENCY}{total:.2f}\n\n"
                    f"{details_block}\n\n"
                    "Awaiting receipt from customer."
                ),
            )
        except Exception:
            logger.exception("Failed to notify admin of Mini App order #%s", order_id)


async def backup_db(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin-only: sends the raw SQLite database file as a Telegram document.
    Use this BEFORE a redeploy when no Railway Volume is mounted, to ensure
    no data is lost. The file can be re-uploaded to /data/orders.db later
    via the Railway shell."""
    if update.effective_user.id != ADMIN_CHAT_ID:
        return
    if not os.path.exists(DB_PATH):
        await update.message.reply_text("No database file found at the configured path.")
        return
    await update.message.reply_text(
        f"📦 Sending database backup from `{DB_PATH}` — save this file before redeploying.",
        parse_mode=ParseMode.MARKDOWN,
    )
    with open(DB_PATH, "rb") as f:
        await update.message.reply_document(
            document=f,
            filename="orders_backup.db",
            caption=f"Database backup — {datetime.now().strftime('%Y-%m-%d %H:%M UTC')}",
        )


async def export_imd_catalog(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin-only: dumps every row in imd_catalog (name + category) to a
    plain text file and sends it as a document, so we can eyeball what
    actually got scraped vs. what's missing."""
    if update.effective_user.id != ADMIN_CHAT_ID:
        return
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT name, category FROM imd_catalog ORDER BY category, name"
    ).fetchall()
    conn.close()

    if not rows:
        await update.message.reply_text("The iMD catalog is empty.")
        return

    lines = [f"{name}\t{category or ''}" for name, category in rows]
    out_path = os.path.join(os.path.dirname(DB_PATH) or ".", "imd_catalog_export.txt")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    await update.message.reply_text(
        f"📚 {len(rows):,} entries in the iMD catalog — sending the full export.",
    )
    with open(out_path, "rb") as f:
        await update.message.reply_document(
            document=f,
            filename="imd_catalog_export.txt",
            caption=f"iMD catalog export — {datetime.now().strftime('%Y-%m-%d %H:%M UTC')} — {len(rows):,} entries",
        )


def main():
    db_init()

    # Restore any subscriptions the admin added via ➕ Add New Subscription
    # in a previous run — MENU/SINGLE_MAIN_ITEMS are otherwise reset to
    # just the built-in catalog on every restart.
    for item_id, name, price in db_load_custom_items():
        MENU[item_id] = (name, price)
        if item_id not in SINGLE_MAIN_ITEMS:
            SINGLE_MAIN_ITEMS.append(item_id)

    # Restore any priced/ordered book requests — MENU-only, deliberately
    # never added to SINGLE_MAIN_ITEMS, since these are one-off items
    # private to whichever customer requested them, not general catalog.
    for item_id, name, price in db_load_book_menu_items():
        MENU[item_id] = (name, price)

    async def post_init(application):
        """Resets the bottom-left menu button so customers use the
        🏪 Shop keyboard button (the only one that supports sendData)."""
        try:
            from telegram import MenuButtonDefault
            await application.bot.set_chat_menu_button(menu_button=MenuButtonDefault())
        except Exception:
            logger.exception("Failed to reset menu button")

    if BOT_TOKEN == "PUT_YOUR_BOT_TOKEN_HERE":
        raise SystemExit("Set BOT_TOKEN (env var) before running.")

    # Persist conversation state to disk. Without this, an in-progress
    # collection (and anything else in user_data) is wiped by every
    # restart or redeploy, silently stranding half-finished orders.
    persistence = PicklePersistence(
        filepath=os.path.join(os.path.dirname(DB_PATH) or ".", "bot_state.pickle"),
        store_data=PersistenceInput(bot_data=True, chat_data=True, user_data=True, callback_data=False),
    )

    app = Application.builder().token(BOT_TOKEN).persistence(persistence).post_init(post_init).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("myorders", my_orders))
    app.add_handler(CommandHandler("backupdb", backup_db))
    app.add_handler(CommandHandler("exportimd", export_imd_catalog))
    app.add_handler(CommandHandler("imddiag", imd_diag_start))
    app.add_handler(MessageHandler(filters.StatusUpdate.WEB_APP_DATA, web_app_data_handler))
    app.add_handler(CommandHandler("customers", customer_history))
    app.add_handler(CommandHandler("customer", customer_lookup))
    app.add_handler(CommandHandler("order", order_lookup))
    app.add_handler(CommandHandler("addserials", add_serials_command))
    app.add_handler(CommandHandler("serials", serials_status))
    app.add_handler(CommandHandler("listserials", show_serials))
    app.add_handler(CommandHandler("removeserial", remove_serial_command))
    app.add_handler(CallbackQueryHandler(order_action, pattern=r"^(paid|cancel):"))
    app.add_handler(CallbackQueryHandler(admin_action, pattern=r"^admin_(confirm|reject):"))
    app.add_handler(CallbackQueryHandler(admin_comment_start, pattern=r"^admin_comment:"))
    app.add_handler(CallbackQueryHandler(deliver_start, pattern=r"^deliver:"))
    app.add_handler(CallbackQueryHandler(reg_go, pattern=r"^reg_go:"))
    app.add_handler(CallbackQueryHandler(add_serials_pick_duration, pattern=r"^addser:"))
    app.add_handler(CallbackQueryHandler(imd_menu_start, pattern=r"^imd_menu$"))
    app.add_handler(CallbackQueryHandler(imd_search_page_nav, pattern=r"^imdpage:"))
    app.add_handler(InlineQueryHandler(imd_inline_query))
    app.add_handler(CallbackQueryHandler(catalog_navigate, pattern=r"^cat:"))
    app.add_handler(CallbackQueryHandler(stock_toggle, pattern=r"^stocktoggle:"))
    app.add_handler(CallbackQueryHandler(getfree_proceed, pattern=r"^getfree_proceed$"))
    app.add_handler(CallbackQueryHandler(goto_my_credits, pattern=r"^goto_my_credits$"))
    app.add_handler(CallbackQueryHandler(ticket_generic_start, pattern=r"^ticketgen:"))
    app.add_handler(CallbackQueryHandler(ticket_subscription_selected, pattern=r"^ticketsub:"))
    app.add_handler(CallbackQueryHandler(ticket_uptodate_choice, pattern=r"^ticketup:"))
    app.add_handler(CallbackQueryHandler(ticket_other_choice, pattern=r"^ticketother:"))
    app.add_handler(CallbackQueryHandler(ticket_imd_choice, pattern=r"^ticketimd:"))
    app.add_handler(CallbackQueryHandler(ticket_resolved_check, pattern=r"^ticketresolved:"))
    app.add_handler(CallbackQueryHandler(tickets_list, pattern=r"^tickets:"))
    app.add_handler(CallbackQueryHandler(tickets_back, pattern=r"^tickets_back$"))
    app.add_handler(CallbackQueryHandler(ticket_view, pattern=r"^ticketview:"))
    app.add_handler(CallbackQueryHandler(ticket_resolve_start, pattern=r"^ticketresolve:"))
    app.add_handler(CallbackQueryHandler(imd_type_selected, pattern=r"^imd_type:"))
    app.add_handler(CallbackQueryHandler(pay_with_stars, pattern=r"^pay_stars:"))
    app.add_handler(CallbackQueryHandler(local_pay_start, pattern=r"^local_pay:"))
    app.add_handler(CallbackQueryHandler(local_country_selected, pattern=r"^local_country:"))
    app.add_handler(CallbackQueryHandler(usa_app_selected, pattern=r"^usa_app:"))
    app.add_handler(CallbackQueryHandler(pay_card_start, pattern=r"^pay_card:"))
    app.add_handler(CallbackQueryHandler(pay_crypto, pattern=r"^pay_crypto:"))
    app.add_handler(CallbackQueryHandler(pay_credits_start, pattern=r"^pay_credits:"))
    app.add_handler(CallbackQueryHandler(generic_type_selected, pattern=r"^gentype:"))
    app.add_handler(CallbackQueryHandler(book_price_start, pattern=r"^bookprice:"))
    app.add_handler(CallbackQueryHandler(book_proceed, pattern=r"^bookproceed:"))
    app.add_handler(CallbackQueryHandler(book_cancel, pattern=r"^bookcancel:"))
    app.add_handler(CallbackQueryHandler(book_requests_list, pattern=r"^bookreqs:"))
    app.add_handler(CallbackQueryHandler(book_requests_back, pattern=r"^bookreqs_back$"))
    app.add_handler(CallbackQueryHandler(book_request_view, pattern=r"^bookreqview:"))
    app.add_handler(CallbackQueryHandler(goto_getfree, pattern=r"^goto_getfree$"))
    app.add_handler(CallbackQueryHandler(back_to_checkout, pattern=r"^back_to_checkout:"))
    app.add_handler(CallbackQueryHandler(my_subscription_detail, pattern=r"^mysub:"))
    app.add_handler(CallbackQueryHandler(subs_menu, pattern=r"^subs_menu$"))
    app.add_handler(CallbackQueryHandler(subs_registered, pattern=r"^subs_registered$"))
    app.add_handler(CallbackQueryHandler(subs_pending, pattern=r"^subs_pending$"))
    app.add_handler(CallbackQueryHandler(pending_detail, pattern=r"^pend:"))
    app.add_handler(CallbackQueryHandler(admin_pending_detail, pattern=r"^apend:"))
    app.add_handler(CallbackQueryHandler(pending_delete_confirm, pattern=r"^pdel:"))
    app.add_handler(CallbackQueryHandler(pending_delete_execute, pattern=r"^pdel_yes:"))
    app.add_handler(CallbackQueryHandler(admin_msg_start, pattern=r"^admin_msg:"))
    app.add_handler(CallbackQueryHandler(customer_reply_start, pattern=r"^cust_reply:"))
    app.add_handler(CallbackQueryHandler(inbox_list, pattern=r"^inbox:"))
    app.add_handler(CallbackQueryHandler(inbox_message_detail, pattern=r"^inboxmsg:"))
    app.add_handler(CallbackQueryHandler(inbox_back, pattern=r"^inbox_back$"))
    app.add_handler(CallbackQueryHandler(announcement_detail, pattern=r"^annview:"))
    app.add_handler(CallbackQueryHandler(announcement_back, pattern=r"^ann_back$"))
    app.add_handler(CallbackQueryHandler(admin_pending_back, pattern=r"^apend_back$"))
    app.add_handler(CallbackQueryHandler(imd_manual_deliver, pattern=r"^imddeliver:"))
    app.add_handler(CallbackQueryHandler(admin_sales_report, pattern=r"^sales:"))
    app.add_handler(PreCheckoutQueryHandler(precheckout_callback))
    app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment_callback))
    app.add_handler(MessageHandler(filters.PHOTO, photo_router))
    app.add_handler(MessageHandler(filters.ChatType.CHANNEL, channel_post_detector))
    # Build regex patterns that match button labels with OR without a badge
    # suffix (e.g. "⏳ Pending Orders 🔴3") — filters.create had the wrong
    # function signature for PTB 21 and broke all keyboard button routing.
    _customer_labels = [BUY_LABEL, MY_SUBS_LABEL, BASKET_LABEL, ANNOUNCEMENTS_LABEL,
                        JOIN_CHANNEL_LABEL, TICKET_LABEL, GET_FREE_LABEL, MY_CREDITS_LABEL,
                        BOOK_REQUEST_LABEL, IMD_SEARCH_LABEL, SUPPORT_LABEL]
    _customer_pat = "^(" + "|".join(re.escape(l) for l in _customer_labels) + r")( 🔴\d+)?$"
    _admin_pat    = "^(" + "|".join(re.escape(l) for l in ADMIN_LABELS)    + r")( 🔴\d+)?$"

    # Route admin buttons — only if ADMIN_CHAT_ID is actually configured,
    # since filters.User(0) raises a ValueError in PTB 21.
    if ADMIN_CHAT_ID:
        app.add_handler(MessageHandler(
            filters.User(ADMIN_CHAT_ID) & filters.Regex(_admin_pat),
            admin_menu_text,
        ))
        app.add_handler(MessageHandler(
            ~filters.User(ADMIN_CHAT_ID) & filters.Regex(_customer_pat),
            main_menu_text,
        ))
    else:
        app.add_handler(MessageHandler(filters.Regex(_customer_pat), main_menu_text))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_state_router))
    app.add_handler(CallbackQueryHandler(menu_button))  # catch-all for menu/cart callbacks

    logger.info("Bot starting...")
    app.run_polling()


if __name__ == "__main__":
    main()
