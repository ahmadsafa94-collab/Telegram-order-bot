# Telegram Order Bot (manual payment confirmation)

A Telegram bot that takes orders through an inline menu, builds a cart,
checks out, and handles payment the way it realistically works in markets
(like Lebanon) where Telegram's native Payments API isn't available:

1. Customer builds an order via inline buttons.
2. At checkout, the bot shows your OMT/Whish transfer details (and
   optionally a Tap Payments / MyFatoorah payment link, if you set one up).
3. Customer sends the transfer, then taps **"I've Paid"**.
4. You (the admin) get a message with the order details and **Confirm /
   Reject** buttons.
5. Tapping Confirm automatically messages the customer that their order
   is being prepared. Tapping Reject tells them to double check and retry.

No card processor integration required to get started — you're
verifying transfers manually, which is what most Lebanese online
businesses do anyway (see: OMT/Whish dominance, COD still 60-70% of
transactions).

## Setup

```bash
pip install -r requirements.txt
```

1. Talk to **@BotFather** on Telegram → `/newbot` → get your token.
2. Talk to **@userinfobot** → get your numeric user ID (this makes you
   the admin who approves payments).
3. Set environment variables:

```bash
export BOT_TOKEN="123456:ABC-your-token"
export ADMIN_CHAT_ID="123456789"
```

   (Or just edit the constants directly at the top of `bot.py`.)

4. Edit `MENU` in `bot.py` with your real items and prices.
5. Edit `PAYMENT_INSTRUCTIONS` with your real OMT/Whish name and number.
6. Run it:

```bash
python bot.py
```

Message your bot on Telegram and hit `/start`.

## Commands

- `/start` — shows the menu
- `/myorders` — shows a customer's last 5 orders and their status

## Data

Orders are stored in a local SQLite file, `orders.db`, created
automatically next to `bot.py`. No external database needed to start.
Order statuses: `awaiting_payment` → `awaiting_confirmation` → `paid` /
`rejected` / `cancelled`.

## Upgrading later

- **Photo proof of payment**: have the "I've Paid" step also prompt for
  a screenshot, forwarded to you alongside the order.
- **Real payment link**: if you set up Tap Payments or MyFatoorah with a
  hosted checkout/payment-link product, set `PAYMENT_LINK_BASE_URL` and
  a "Pay by card" button will appear automatically.
- **Multiple admins**: swap `ADMIN_CHAT_ID` for a list and notify/allow
  all of them.
- **Persistent cart across restarts**: currently the cart lives in
  `context.user_data` (in-memory unless you configure PTB's persistence).
  Add `persistence=PicklePersistence(...)` to `Application.builder()` if
  you want carts to survive a bot restart.
