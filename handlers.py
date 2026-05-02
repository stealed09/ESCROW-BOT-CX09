import asyncio
import urllib.request
import json as _json
import uuid
import logging
from datetime import datetime
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    ChatPermissions
)
from telegram.ext import ContextTypes
from telegram.error import TelegramError
from config import state, MAIN_ADMIN_ID

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────

def is_main_admin(user_id: int) -> bool:
    return user_id == MAIN_ADMIN_ID

def is_admin(user_id: int) -> bool:
    return user_id == MAIN_ADMIN_ID or user_id in state.sub_admins

def generate_trade_id() -> str:
    return "TRD-" + str(uuid.uuid4()).upper()[:8]

def get_deal_by_group(chat_id: int):
    deal_id = state.group_to_deal.get(chat_id)
    if deal_id:
        return deal_id, state.deals.get(deal_id)
    return None, None

def get_deal_by_id(deal_id: str):
    return state.deals.get(deal_id)

async def send_log(context: ContextTypes.DEFAULT_TYPE, message: str):
    """Send a message to the LOG GROUP."""
    if state.log_group_id:
        try:
            await context.bot.send_message(
                chat_id=state.log_group_id,
                text=f"📋 LOG\n\n{message}",
                parse_mode="HTML"
            )
        except TelegramError as e:
            logger.error(f"Failed to send log: {e}")

async def notify_all_admins(context: ContextTypes.DEFAULT_TYPE, message: str, deal_id: str = None):
    """Notify main admin and all sub admins."""
    admin_ids = [MAIN_ADMIN_ID] + list(state.sub_admins)
    for admin_id in admin_ids:
        try:
            kb = None
            if deal_id:
                kb = InlineKeyboardMarkup([[
                    InlineKeyboardButton("🚨 Handle Dispute", callback_data=f"handle_dispute:{deal_id}")
                ]])
            await context.bot.send_message(
                chat_id=admin_id,
                text=message,
                reply_markup=kb,
                parse_mode="HTML"
            )
        except TelegramError:
            pass

def deal_summary_text(deal: dict) -> str:
    return (
        f"🆔 <b>Trade ID:</b> {deal.get('trade_id', 'N/A')}\n"
        f"👤 <b>Buyer:</b> @{deal.get('buyer_username', 'Not set')} | <code>{deal.get('buyer_address', 'N/A')}</code>\n"
        f"👤 <b>Seller:</b> @{deal.get('seller_username', 'Not set')} | <code>{deal.get('seller_address', 'N/A')}</code>\n"
        f"💰 <b>Quantity:</b> {deal.get('quantity', 'N/A')}\n"
        f"📈 <b>Rate:</b> {deal.get('rate', 'N/A')}\n"
        f"📝 <b>Condition:</b> {deal.get('condition', 'None')}\n"
        f"🪙 <b>Token:</b> {deal.get('token', 'Not selected')}\n"
        f"📊 <b>Status:</b> {deal.get('status', 'N/A')}"
    )

# ─────────────────────────────────────────────
# /start COMMAND
# ─────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        return
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🤝 Start Deal", callback_data="start_deal")],
        [InlineKeyboardButton("📖 Instructions", callback_data="show_instructions")]
    ])
    await update.message.reply_text(
        "👋 <b>Welcome to P2P Escrow Bot</b>\n\n"
        "This bot helps you trade safely using a private escrow group.\n\n"
        "Choose an option below to get started:",
        reply_markup=kb,
        parse_mode="HTML"
    )

# ─────────────────────────────────────────────
# /instructions COMMAND
# ─────────────────────────────────────────────

async def cmd_instructions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "📖 <b>HOW TO USE ESCROW BOT</b>\n\n"
        "Follow these steps in order:\n\n"
        "<b>1️⃣ /dd</b> — Fill in deal details (quantity, rate, condition)\n"
        "<b>2️⃣ /buyer</b> {address} — Set buyer wallet address\n"
        "   <b>/seller</b> {address} — Set seller wallet address\n"
        "<b>3️⃣ /token</b> — Select payment token (USDT, BTC, LTC)\n"
        "<b>4️⃣ /deposit</b> — Get deposit address & QR code\n"
        "<b>5️⃣ /verify</b> — Verify payment (marks as FUNDED)\n"
        "<b>6️⃣ Confirm buttons</b> — Both buyer & seller must confirm\n"
        "<b>7️⃣ /dispute</b> — Trigger dispute if there's an issue\n\n"
        "⚠️ <i>All steps must be done inside your deal group</i>"
    )
    if update.callback_query:
        await update.callback_query.edit_message_text(text, parse_mode="HTML")
    else:
        await update.message.reply_text(text, parse_mode="HTML")

# ─────────────────────────────────────────────
# CALLBACK QUERY HANDLER (central dispatcher)
# ─────────────────────────────────────────────

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    user = query.from_user

    # ── Start Deal ──
    if data == "start_deal":
        await handle_start_deal(update, context)

    # ── Instructions ──
    elif data == "show_instructions":
        await cmd_instructions(update, context)

    # ── Token Selection ──
    elif data.startswith("token:"):
        await handle_token_selection(update, context, data)

    # ── Buyer/Seller Confirm ──
    elif data.startswith("confirm:"):
        await handle_confirmation(update, context, data)

    # ── Handle Dispute (admin) ──
    elif data.startswith("handle_dispute:"):
        await handle_dispute_admin(update, context, data)

# ─────────────────────────────────────────────
# START DEAL — Create private group
# ─────────────────────────────────────────────

async def handle_start_deal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = query.from_user

    if not state.log_group_id:
        await query.edit_message_text(
            "❌ <b>Cannot create deal.</b>\n\n"
            "The admin has not set up the LOG GROUP yet.\n"
            "Please contact the administrator.",
            parse_mode="HTML"
        )
        return

    await query.edit_message_text(
        "⏳ <b>Creating your private deal group...</b>\n\n"
        "Please wait a moment.",
        parse_mode="HTML"
    )

    try:
        # Create private group
        new_group = await context.bot.create_group(
            title=f"🔒 Escrow Deal — {user.username or user.first_name}"
        )
        group_id = new_group.id
    except Exception as e:
        # Fallback: use supergroup creation via forward
        await context.bot.send_message(
            chat_id=user.id,
            text=(
                "⚠️ <b>Note:</b> Bot could not auto-create group (API limitation in demo).\n\n"
                "Please:\n"
                "1. Create a Telegram group manually\n"
                "2. Add the bot as admin\n"
                "3. Send <code>/initdeal</code> inside that group\n\n"
                "Then come back here after setup."
            ),
            parse_mode="HTML"
        )
        return

    # Generate deal
    trade_id = generate_trade_id()
    deal = {
        "trade_id": trade_id,
        "group_id": group_id,
        "status": "SETUP",
        "creator_id": user.id,
        "buyer_id": None,
        "buyer_username": None,
        "buyer_address": None,
        "seller_id": None,
        "seller_username": None,
        "seller_address": None,
        "quantity": None,
        "rate": None,
        "condition": None,
        "token": None,
        "deposit_address": None,
        "buyer_confirmed": False,
        "seller_confirmed": False,
        "funded": False,
        "created_at": datetime.utcnow().isoformat()
    }

    state.deals[trade_id] = deal
    state.group_to_deal[group_id] = trade_id

    # Invite link
    try:
        link = await context.bot.create_chat_invite_link(group_id, creates_join_request=False)
        invite_url = link.invite_link
    except:
        invite_url = "Check group"

    await context.bot.send_message(
        chat_id=group_id,
        text=(
            f"🔒 <b>Private Escrow Group Created</b>\n\n"
            f"🆔 <b>Trade ID:</b> <code>{trade_id}</code>\n\n"
            f"✅ Group is ready. Both parties must join this group.\n\n"
            f"➡️ <b>Next Step:</b> Use <b>/dd</b> to fill in deal details."
        ),
        parse_mode="HTML"
    )

    await context.bot.send_message(
        chat_id=user.id,
        text=(
            f"✅ <b>Deal Group Created!</b>\n\n"
            f"🆔 Trade ID: <code>{trade_id}</code>\n"
            f"🔗 Invite Link: {invite_url}\n\n"
            f"Share this link with the other party.\n"
            f"➡️ <b>Next:</b> Use <b>/dd</b> inside the group."
        ),
        parse_mode="HTML"
    )

    # Log to LOG GROUP
    await send_log(context,
        f"🆕 <b>DEAL CREATED</b>\n\n"
        f"🆔 Trade ID: <code>{trade_id}</code>\n"
        f"👤 Creator: @{user.username} (ID: {user.id})\n"
        f"📦 Group ID: <code>{group_id}</code>\n"
        f"⏰ Time: {deal['created_at']}"
    )

# ─────────────────────────────────────────────
# /initdeal — Alternative: User adds bot to group
# ─────────────────────────────────────────────

async def cmd_initdeal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Called when someone adds bot to an existing group and runs /initdeal"""
    chat = update.effective_chat
    user = update.effective_user

    if chat.type == "private":
        await update.message.reply_text("❌ Use this command inside a group.")
        return

    if not state.log_group_id:
        await update.message.reply_text(
            "❌ <b>LOG GROUP not set.</b>\nAdmin must run /setloggroup first.",
            parse_mode="HTML"
        )
        return

    # Check if group already has a deal
    if chat.id in state.group_to_deal:
        await update.message.reply_text("⚠️ This group already has an active deal.")
        return

    trade_id = generate_trade_id()
    deal = {
        "trade_id": trade_id,
        "group_id": chat.id,
        "status": "SETUP",
        "creator_id": user.id,
        "buyer_id": None,
        "buyer_username": None,
        "buyer_address": None,
        "seller_id": None,
        "seller_username": None,
        "seller_address": None,
        "quantity": None,
        "rate": None,
        "condition": None,
        "token": None,
        "deposit_address": None,
        "buyer_confirmed": False,
        "seller_confirmed": False,
        "funded": False,
        "created_at": datetime.utcnow().isoformat()
    }

    state.deals[trade_id] = deal
    state.group_to_deal[chat.id] = trade_id

    await update.message.reply_text(
        f"🔒 <b>Escrow Deal Initialized</b>\n\n"
        f"🆔 <b>Trade ID:</b> <code>{trade_id}</code>\n\n"
        f"Both buyer and seller must be in this group.\n\n"
        f"➡️ <b>Next Step:</b> Use <b>/dd</b> to fill deal details.",
        parse_mode="HTML"
    )

    await send_log(context,
        f"🆕 <b>DEAL CREATED</b>\n\n"
        f"🆔 Trade ID: <code>{trade_id}</code>\n"
        f"👤 Creator: @{user.username} (ID: {user.id})\n"
        f"📦 Group ID: <code>{chat.id}</code>\n"
        f"⏰ Time: {deal['created_at']}"
    )

# ─────────────────────────────────────────────
# /dd — Deal Form
# ─────────────────────────────────────────────

async def cmd_dd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    user = update.effective_user

    if chat.type == "private":
        await update.message.reply_text("❌ Use /dd inside your deal group.")
        return

    deal_id, deal = get_deal_by_group(chat.id)
    if not deal:
        await update.message.reply_text("❌ No active deal in this group. Use /initdeal first.")
        return

    if deal["status"] not in ["SETUP"]:
        await update.message.reply_text(
            f"⚠️ Deal is in <b>{deal['status']}</b> status. Cannot edit form now.",
            parse_mode="HTML"
        )
        return

    # Expect: /dd Quantity - X | Rate - Y | Condition - Z
    args = context.args
    if not args:
        await update.message.reply_text(
            "📋 <b>DEAL FORM</b>\n\n"
            "Fill in deal details using this format:\n\n"
            "<code>/dd [quantity] [rate] [condition]</code>\n\n"
            "<b>Example:</b>\n"
            "<code>/dd 500 1.02 Payment within 30 minutes</code>\n\n"
            "Fields:\n"
            "• <b>Quantity</b> — Amount of crypto\n"
            "• <b>Rate</b> — Exchange rate or price\n"
            "• <b>Condition</b> — Any special terms (optional)\n\n"
            "➡️ Or use: <code>/dd quantity rate condition</code>",
            parse_mode="HTML"
        )
        return

    if len(args) < 2:
        await update.message.reply_text(
            "❌ Provide at least quantity and rate.\n"
            "Example: <code>/dd 500 1.02 Pay in 30min</code>",
            parse_mode="HTML"
        )
        return

    deal["quantity"] = args[0]
    deal["rate"] = args[1]
    deal["condition"] = " ".join(args[2:]) if len(args) > 2 else "None"
    deal["form_filled_by"] = user.id

    await update.message.reply_text(
        f"✅ <b>Deal Form Saved!</b>\n\n"
        f"💰 <b>Quantity:</b> {deal['quantity']}\n"
        f"📈 <b>Rate:</b> {deal['rate']}\n"
        f"📝 <b>Condition:</b> {deal['condition']}\n\n"
        f"➡️ <b>Next Step:</b> Set roles using:\n"
        f"<code>/buyer [wallet_address]</code>\n"
        f"<code>/seller [wallet_address]</code>",
        parse_mode="HTML"
    )

# ─────────────────────────────────────────────
# /buyer & /seller — Role Setup
# ─────────────────────────────────────────────

async def cmd_buyer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    user = update.effective_user

    if chat.type == "private":
        await update.message.reply_text("❌ Use inside your deal group.")
        return

    deal_id, deal = get_deal_by_group(chat.id)
    if not deal:
        await update.message.reply_text("❌ No active deal here.")
        return

    if deal["status"] not in ["SETUP"]:
        await update.message.reply_text(
            f"⚠️ Cannot change roles. Deal status: <b>{deal['status']}</b>",
            parse_mode="HTML"
        )
        return

    if not context.args:
        await update.message.reply_text(
            "❌ Provide wallet address.\nExample: <code>/buyer TRX123abc...</code>",
            parse_mode="HTML"
        )
        return

    address = context.args[0]
    deal["buyer_id"] = user.id
    deal["buyer_username"] = user.username or user.first_name
    deal["buyer_address"] = address

    await update.message.reply_text(
        f"✅ <b>Buyer Set!</b>\n\n"
        f"👤 User: @{deal['buyer_username']}\n"
        f"💳 Address: <code>{address}</code>\n\n"
        + check_roles_complete(deal),
        parse_mode="HTML"
    )

async def cmd_seller(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    user = update.effective_user

    if chat.type == "private":
        await update.message.reply_text("❌ Use inside your deal group.")
        return

    deal_id, deal = get_deal_by_group(chat.id)
    if not deal:
        await update.message.reply_text("❌ No active deal here.")
        return

    if deal["status"] not in ["SETUP"]:
        await update.message.reply_text(
            f"⚠️ Cannot change roles. Deal status: <b>{deal['status']}</b>",
            parse_mode="HTML"
        )
        return

    if not context.args:
        await update.message.reply_text(
            "❌ Provide wallet address.\nExample: <code>/seller TRX456xyz...</code>",
            parse_mode="HTML"
        )
        return

    address = context.args[0]
    deal["seller_id"] = user.id
    deal["seller_username"] = user.username or user.first_name
    deal["seller_address"] = address

    await update.message.reply_text(
        f"✅ <b>Seller Set!</b>\n\n"
        f"👤 User: @{deal['seller_username']}\n"
        f"💳 Address: <code>{address}</code>\n\n"
        + check_roles_complete(deal),
        parse_mode="HTML"
    )

def check_roles_complete(deal: dict) -> str:
    buyer_ok = deal.get("buyer_id") is not None
    seller_ok = deal.get("seller_id") is not None

    if buyer_ok and seller_ok:
        deal["status"] = "ROLES_SET"
        return "✅ Both roles are set!\n\n➡️ <b>Next Step:</b> Use <b>/token</b> to select payment token."
    elif buyer_ok:
        return "⏳ Waiting for seller to use <code>/seller [address]</code>"
    else:
        return "⏳ Waiting for buyer to use <code>/buyer [address]</code>"

# ─────────────────────────────────────────────
# /token — Select Token
# ─────────────────────────────────────────────

async def cmd_token(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat

    if chat.type == "private":
        await update.message.reply_text("❌ Use inside your deal group.")
        return

    deal_id, deal = get_deal_by_group(chat.id)
    if not deal:
        await update.message.reply_text("❌ No active deal here.")
        return

    if deal.get("funded"):
        await update.message.reply_text(
            "❌ <b>Cannot change token.</b>\n"
            "Payment already made. Contact admin if needed.",
            parse_mode="HTML"
        )
        return

    if deal["status"] not in ["ROLES_SET", "SETUP"]:
        await update.message.reply_text(
            f"⚠️ Please complete previous steps first.\n"
            f"Current status: <b>{deal['status']}</b>",
            parse_mode="HTML"
        )
        return

    if not deal.get("buyer_id") or not deal.get("seller_id"):
        await update.message.reply_text(
            "❌ Set buyer and seller roles first using /buyer and /seller.",
            parse_mode="HTML"
        )
        return

    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("💵 USDT TRC20", callback_data=f"token:USDT_TRC20:{deal_id}"),
            InlineKeyboardButton("💵 USDT BEP20", callback_data=f"token:USDT_BEP20:{deal_id}")
        ],
        [
            InlineKeyboardButton("₿ BTC", callback_data=f"token:BTC:{deal_id}"),
            InlineKeyboardButton("Ł LTC", callback_data=f"token:LTC:{deal_id}")
        ]
    ])

    await update.message.reply_text(
        "🪙 <b>SELECT PAYMENT TOKEN</b>\n\n"
        "Choose the token for this escrow deal:\n\n"
        "⚠️ <i>Token cannot be changed after payment is made.</i>",
        reply_markup=kb,
        parse_mode="HTML"
    )

async def handle_token_selection(update: Update, context: ContextTypes.DEFAULT_TYPE, data: str):
    query = update.callback_query
    user = query.from_user
    parts = data.split(":")
    token = parts[1]
    deal_id = parts[2]

    deal = get_deal_by_id(deal_id)
    if not deal:
        await query.edit_message_text("❌ Deal not found.")
        return

    # Only buyer or seller can select
    if user.id not in [deal.get("buyer_id"), deal.get("seller_id")]:
        await query.answer("❌ Only deal participants can select token.", show_alert=True)
        return

    if deal.get("funded"):
        await query.answer("❌ Cannot change after payment. Contact admin.", show_alert=True)
        return

    deal["token"] = token
    deal["status"] = "TOKEN_SELECTED"
    deal["token_selected_by"] = user.username or user.first_name

    token_display = {
        "USDT_TRC20": "💵 USDT (TRC20)",
        "USDT_BEP20": "💵 USDT (BEP20)",
        "BTC": "₿ Bitcoin (BTC)",
        "LTC": "Ł Litecoin (LTC)"
    }.get(token, token)

    await query.edit_message_text(
        f"✅ <b>Token Selected & Locked!</b>\n\n"
        f"🪙 <b>Token:</b> {token_display}\n"
        f"👤 <b>Selected by:</b> @{deal['token_selected_by']}\n\n"
        f"🔒 Token is now locked for this deal.\n\n"
        f"➡️ <b>Next Step:</b> Use <b>/deposit</b> to get payment address.",
        parse_mode="HTML"
    )

# ─────────────────────────────────────────────
# /deposit — Get Deposit Address
# ─────────────────────────────────────────────

async def cmd_deposit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat

    if chat.type == "private":
        await update.message.reply_text("❌ Use inside your deal group.")
        return

    deal_id, deal = get_deal_by_group(chat.id)
    if not deal:
        await update.message.reply_text("❌ No active deal here.")
        return

    if deal["status"] not in ["TOKEN_SELECTED", "AWAITING_DEPOSIT"]:
        await update.message.reply_text(
            "❌ Please select a token first using <b>/token</b>",
            parse_mode="HTML"
        )
        return

    if not state.oxapay_key:
        await update.message.reply_text(
            "❌ <b>OxaPay not configured.</b>\n\n"
            "Admin must set API key using /setoxapay\n\n"
            "⚠️ <i>DEMO MODE: Use /verify to simulate funding.</i>",
            parse_mode="HTML"
        )
        deal["status"] = "AWAITING_DEPOSIT"
        deal["deposit_address"] = f"DEMO_ADDRESS_{deal_id[:6]}"
        await update.message.reply_text(
            f"🔧 <b>DEMO DEPOSIT ADDRESS</b>\n\n"
            f"🪙 Token: {deal.get('token', 'N/A')}\n"
            f"📬 Address: <code>{deal['deposit_address']}</code>\n"
            f"💰 Amount: {deal.get('quantity', 'N/A')}\n\n"
            f"⚠️ This is a DEMO address — no real payment needed.\n\n"
            f"➡️ <b>Next Step:</b> Use <b>/verify</b> to simulate payment.",
            parse_mode="HTML"
        )
        return

    # OxaPay integration
    await update.message.reply_text("⏳ Generating deposit address via OxaPay...")

    token_map = {
        "USDT_TRC20": ("USDT", "TRX"),
        "USDT_BEP20": ("USDT", "BSC"),
        "BTC": ("BTC", "BTC"),
        "LTC": ("LTC", "LTC")
    }
    currency, network = token_map.get(deal["token"], ("USDT", "TRX"))

    try:
        address = await create_oxapay_invoice(
            api_key=state.oxapay_key,
            amount=float(deal.get("quantity", 1)),
            currency=currency,
            network=network,
            trade_id=deal_id
        )
        deal["deposit_address"] = address
        deal["status"] = "AWAITING_DEPOSIT"

        await update.message.reply_text(
            f"✅ <b>DEPOSIT ADDRESS READY</b>\n\n"
            f"🪙 <b>Token:</b> {deal['token']}\n"
            f"📬 <b>Address:</b>\n<code>{address}</code>\n"
            f"💰 <b>Amount:</b> {deal.get('quantity', 'N/A')}\n\n"
            f"📸 QR Code: <i>Send exact amount to above address</i>\n\n"
            f"⚠️ Send <b>EXACT</b> amount. Different amounts may cause delays.\n\n"
            f"➡️ <b>Next Step:</b> After sending, use <b>/verify</b>",
            parse_mode="HTML"
        )
    except Exception as e:
        await update.message.reply_text(
            f"❌ OxaPay Error: {str(e)}\n\n"
            f"🔧 <b>Falling back to DEMO mode.</b>\n"
            f"📬 Demo Address: <code>DEMO_{deal_id[:8]}</code>\n\n"
            f"➡️ Use <b>/verify</b> to simulate payment.",
            parse_mode="HTML"
        )
        deal["deposit_address"] = f"DEMO_{deal_id[:8]}"
        deal["status"] = "AWAITING_DEPOSIT"

async def create_oxapay_invoice(api_key: str, amount: float, currency: str, network: str, trade_id: str) -> str:
    """Create OxaPay invoice and return address."""
    url = "https://api.oxapay.com/merchants/request"
    payload = {
        "merchant": api_key,
        "amount": amount,
        "currency": currency,
        "network": network,
        "description": f"Escrow Deal {trade_id}",
        "callbackUrl": "https://yoursite.com/callback",
        "returnUrl": "https://yoursite.com/return",
        "lifeTime": 60
    }
    loop = asyncio.get_event_loop()
    def _oxapay_addr():
        req = urllib.request.Request(url, data=_json.dumps(payload).encode(), headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=15) as r:
            return _json.loads(r.read().decode())
    data = await loop.run_in_executor(None, _oxapay_addr)
    if data.get("result") == 100:
        return data.get("payAddress", "N/A")
    else:
        raise Exception(f"OxaPay API error: {data.get('message', 'Unknown')}")

# ─────────────────────────────────────────────
# /verify — Simulate Payment (DEMO)
# ─────────────────────────────────────────────

async def cmd_verify(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    user = update.effective_user

    if chat.type == "private":
        await update.message.reply_text("❌ Use inside your deal group.")
        return

    deal_id, deal = get_deal_by_group(chat.id)
    if not deal:
        await update.message.reply_text("❌ No active deal here.")
        return

    if deal.get("funded"):
        await update.message.reply_text("⚠️ Deal already marked as FUNDED.")
        return

    if deal["status"] != "AWAITING_DEPOSIT":
        await update.message.reply_text(
            "❌ Cannot verify now.\n"
            "Use /deposit first to get the deposit address.",
            parse_mode="HTML"
        )
        return

    # Mark funded
    deal["funded"] = True
    deal["status"] = "FUNDED"
    deal["funded_by"] = user.username or user.first_name
    deal["funded_at"] = datetime.utcnow().isoformat()

    # Confirmation buttons
    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Buyer Confirm", callback_data=f"confirm:buyer:{deal_id}"),
            InlineKeyboardButton("✅ Seller Confirm", callback_data=f"confirm:seller:{deal_id}")
        ]
    ])

    await update.message.reply_text(
        f"✅ <b>PAYMENT FUNDED!</b>\n\n"
        f"🆔 Trade ID: <code>{deal_id}</code>\n"
        f"🪙 Token: {deal.get('token')}\n"
        f"💰 Amount: {deal.get('quantity')}\n"
        f"📊 Status: <b>FUNDED</b>\n\n"
        f"─────────────────────\n"
        f"<b>BOTH PARTIES MUST CONFIRM</b>\n\n"
        f"👇 Press your confirm button below:\n"
        f"• Buyer confirms they sent payment\n"
        f"• Seller confirms they received it\n\n"
        f"⚠️ Deal releases ONLY when BOTH confirm.",
        reply_markup=kb,
        parse_mode="HTML"
    )

    # Log FUNDED
    await send_log(context,
        f"💰 <b>DEAL FUNDED</b>\n\n"
        f"🆔 Trade ID: <code>{deal_id}</code>\n"
        f"🪙 Token: {deal.get('token')}\n"
        f"💵 Amount: {deal.get('quantity')}\n"
        f"👤 Verified by: @{deal['funded_by']}\n"
        f"⏰ Time: {deal['funded_at']}\n"
        f"📊 Status: FUNDED"
    )

# ─────────────────────────────────────────────
# CONFIRMATION HANDLER
# ─────────────────────────────────────────────

async def handle_confirmation(update: Update, context: ContextTypes.DEFAULT_TYPE, data: str):
    query = update.callback_query
    user = query.from_user
    parts = data.split(":")
    role = parts[1]       # buyer or seller
    deal_id = parts[2]

    deal = get_deal_by_id(deal_id)
    if not deal:
        await query.answer("❌ Deal not found.", show_alert=True)
        return

    if not deal.get("funded"):
        await query.answer("❌ Deal not funded yet.", show_alert=True)
        return

    if deal.get("status") == "COMPLETED":
        await query.answer("✅ Deal already completed.", show_alert=True)
        return

    # Validate role vs user
    if role == "buyer":
        if user.id != deal.get("buyer_id"):
            await query.answer("❌ You are not the buyer.", show_alert=True)
            return
        if deal.get("buyer_confirmed"):
            await query.answer("✅ You already confirmed.", show_alert=True)
            return
        deal["buyer_confirmed"] = True
        await query.answer("✅ Buyer confirmed!")

    elif role == "seller":
        if user.id != deal.get("seller_id"):
            await query.answer("❌ You are not the seller.", show_alert=True)
            return
        if deal.get("seller_confirmed"):
            await query.answer("✅ You already confirmed.", show_alert=True)
            return
        deal["seller_confirmed"] = True
        await query.answer("✅ Seller confirmed!")

    # Update message
    buyer_status = "✅" if deal.get("buyer_confirmed") else "⏳"
    seller_status = "✅" if deal.get("seller_confirmed") else "⏳"

    if deal["buyer_confirmed"] and deal["seller_confirmed"]:
        # RELEASE DEAL
        await query.edit_message_text(
            f"🎉 <b>BOTH PARTIES CONFIRMED!</b>\n\n"
            f"{buyer_status} Buyer: Confirmed\n"
            f"{seller_status} Seller: Confirmed\n\n"
            f"⏳ Processing release...",
            parse_mode="HTML"
        )
        await release_deal(context, deal_id, deal, query.message.chat_id)
    else:
        kb = InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    f"{buyer_status} Buyer Confirm",
                    callback_data=f"confirm:buyer:{deal_id}"
                ),
                InlineKeyboardButton(
                    f"{seller_status} Seller Confirm",
                    callback_data=f"confirm:seller:{deal_id}"
                )
            ]
        ])
        await query.edit_message_text(
            f"📊 <b>CONFIRMATION STATUS</b>\n\n"
            f"{buyer_status} Buyer: {'Confirmed ✅' if deal.get('buyer_confirmed') else 'Waiting...'}\n"
            f"{seller_status} Seller: {'Confirmed ✅' if deal.get('seller_confirmed') else 'Waiting...'}\n\n"
            f"⚠️ Both must confirm for release.",
            reply_markup=kb,
            parse_mode="HTML"
        )

# ─────────────────────────────────────────────
# RELEASE DEAL
# ─────────────────────────────────────────────


async def release_deal(context: ContextTypes.DEFAULT_TYPE, deal_id: str, deal: dict, group_id: int):
    """Process deal release with fee calculation."""

    # Fee logic
    apply_fee = True

    # Check bio condition
    if state.required_bio:
        try:
            buyer_info = await context.bot.get_chat(deal.get("buyer_id"))
            bio = getattr(buyer_info, 'bio', '') or ''
            if state.required_bio.lower() in bio.lower():
                apply_fee = False
        except:
            pass

    quantity = float(deal.get("quantity", 0))
    fee_amount = 0.0
    final_amount = quantity

    if apply_fee and state.fee_percent > 0:
        fee_amount = quantity * (state.fee_percent / 100)
        final_amount = quantity - fee_amount

    deal["status"] = "COMPLETED"
    deal["final_amount"] = final_amount
    deal["fee_deducted"] = fee_amount
    deal["completed_at"] = datetime.utcnow().isoformat()

    # Send completion message in group
    summary = (
        f"🎉 <b>DEAL COMPLETED!</b>\n\n"
        f"🆔 Trade ID: <code>{deal_id}</code>\n"
        f"🪙 Token: {deal.get('token')}\n"
        f"💰 Original Amount: {quantity}\n"
        f"💸 Fee ({state.fee_percent}%): {fee_amount:.4f}\n"
        f"✅ Final Amount: {final_amount:.4f}\n\n"
        f"👤 Buyer: @{deal.get('buyer_username')}\n"
        f"👤 Seller: @{deal.get('seller_username')}\n\n"
        f"📊 Status: <b>COMPLETED</b>\n"
        f"⏰ Time: {deal['completed_at']}\n\n"
        f"Thank you for using P2P Escrow! 🙏"
    )

    try:
        await context.bot.send_message(chat_id=group_id, text=summary, parse_mode="HTML")
    except:
        pass

    # Log to LOG GROUP
    await send_log(context,
        f"✅ <b>DEAL COMPLETED</b>\n\n"
        f"🆔 Trade ID: <code>{deal_id}</code>\n"
        f"👤 Buyer: @{deal.get('buyer_username')}\n"
        f"👤 Seller: @{deal.get('seller_username')}\n"
        f"🪙 Token: {deal.get('token')}\n"
        f"💰 Quantity: {quantity}\n"
        f"💸 Fee: {fee_amount:.4f} ({state.fee_percent}%)\n"
        f"✅ Final Amount: {final_amount:.4f}\n"
        f"📦 Group ID: <code>{group_id}</code>\n"
        f"📊 Status: COMPLETED\n"
        f"⏰ Timestamp: {deal['completed_at']}"
    )

    # Auto delete group after delay
    await asyncio.sleep(15)
    try:
        await context.bot.send_message(
            chat_id=group_id,
            text="🗑 <b>This group will be deleted in 5 seconds.</b>\n\nThank you!",
            parse_mode="HTML"
        )
        await asyncio.sleep(5)
        # Note: Bot can only delete group if it's admin with delete rights
        # Telegram doesn't allow bots to delete groups directly
        # We notify and the group owner should delete it
        members = [deal.get("buyer_id"), deal.get("seller_id")]
        for member_id in members:
            if member_id:
                try:
                    await context.bot.send_message(
                        chat_id=member_id,
                        text=(
                            f"✅ <b>Deal {deal_id} Completed!</b>\n\n"
                            f"Your escrow deal is done.\n"
                            f"Final Amount: <b>{final_amount:.4f} {deal.get('token')}</b>\n\n"
                            f"The deal group is now closed."
                        ),
                        parse_mode="HTML"
                    )
                except:
                    pass
    except:
        pass

# ─────────────────────────────────────────────
# /dispute — Trigger Dispute
# ─────────────────────────────────────────────

async def cmd_dispute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    user = update.effective_user

    if chat.type == "private":
        await update.message.reply_text("❌ Use inside your deal group.")
        return

    deal_id, deal = get_deal_by_group(chat.id)
    if not deal:
        await update.message.reply_text("❌ No active deal here.")
        return

    if deal.get("status") == "COMPLETED":
        await update.message.reply_text("❌ Cannot dispute a completed deal.")
        return

    if deal.get("status") == "DISPUTED":
        await update.message.reply_text("⚠️ Dispute already open. Admin will assist shortly.")
        return

    reason = " ".join(context.args) if context.args else "No reason provided"

    deal["status"] = "DISPUTED"
    deal["dispute_by"] = user.username or user.first_name
    deal["dispute_reason"] = reason
    deal["dispute_at"] = datetime.utcnow().isoformat()

    await update.message.reply_text(
        f"🚨 <b>DISPUTE TRIGGERED!</b>\n\n"
        f"👤 By: @{user.username or user.first_name}\n"
        f"📝 Reason: {reason}\n\n"
        f"⏳ An admin will join shortly to assist.\n"
        f"Please remain in the group.",
        parse_mode="HTML"
    )

    # Notify all admins
    group_link = f"https://t.me/c/{str(chat.id).replace('-100', '')}/1"
    await notify_all_admins(
        context,
        f"🚨 <b>DISPUTE ALERT!</b>\n\n"
        f"🆔 Trade ID: <code>{deal_id}</code>\n"
        f"👤 Buyer: @{deal.get('buyer_username', 'N/A')}\n"
        f"👤 Seller: @{deal.get('seller_username', 'N/A')}\n"
        f"⚠️ Disputed by: @{deal['dispute_by']}\n"
        f"📝 Reason: {reason}\n"
        f"🔗 Group: {group_link}\n"
        f"⏰ Time: {deal['dispute_at']}",
        deal_id=deal_id
    )

    # Log dispute
    await send_log(context,
        f"⚠️ <b>DISPUTE OPENED</b>\n\n"
        f"🆔 Trade ID: <code>{deal_id}</code>\n"
        f"👤 Buyer: @{deal.get('buyer_username')}\n"
        f"👤 Seller: @{deal.get('seller_username')}\n"
        f"⚠️ Triggered by: @{deal['dispute_by']}\n"
        f"📝 Reason: {reason}\n"
        f"📦 Group ID: <code>{chat.id}</code>\n"
        f"📊 Status: DISPUTED\n"
        f"⏰ Time: {deal['dispute_at']}"
    )

async def handle_dispute_admin(update: Update, context: ContextTypes.DEFAULT_TYPE, data: str):
    """Admin clicks 'Handle Dispute' button."""
    query = update.callback_query
    user = query.from_user
    deal_id = data.split(":")[1]

    if not is_admin(user.id):
        await query.answer("❌ Not authorized.", show_alert=True)
        return

    deal = get_deal_by_id(deal_id)
    if not deal:
        await query.answer("❌ Deal not found.", show_alert=True)
        return

    # Check if admin already assigned
    if deal_id in state.dispute_admins:
        assigned = state.dispute_admins[deal_id]
        if assigned != user.id:
            await query.answer(
                f"❌ Another admin is already handling this dispute.",
                show_alert=True
            )
            return

    # Assign admin to dispute
    state.dispute_admins[deal_id] = user.id
    deal["dispute_admin"] = user.username or user.first_name

    await query.edit_message_text(
        f"✅ <b>You are now handling this dispute.</b>\n\n"
        f"🆔 Trade ID: <code>{deal_id}</code>\n"
        f"👤 Buyer: @{deal.get('buyer_username')}\n"
        f"👤 Seller: @{deal.get('seller_username')}\n\n"
        f"Please join the deal group and resolve the issue.\n\n"
        f"Commands available:\n"
        f"<code>/releaseto buyer {deal_id}</code> — Release to buyer\n"
        f"<code>/releaseto seller {deal_id}</code> — Release to seller",
        parse_mode="HTML"
    )

    # Notify deal group
    try:
        await context.bot.send_message(
            chat_id=deal["group_id"],
            text=(
                f"👨‍💼 <b>Admin Joined Dispute</b>\n\n"
                f"Admin @{user.username or user.first_name} is now handling your dispute.\n"
                f"Please cooperate and provide evidence if asked."
            ),
            parse_mode="HTML"
        )
    except:
        pass

# ─────────────────────────────────────────────
# ─────────────────────────────────────────────
# ADMIN COMMANDS
# ─────────────────────────────────────────────

async def cmd_setloggroup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    user = update.effective_user

    if not is_main_admin(user.id):
        await update.message.reply_text(
            "❌ <b>Access Denied.</b>\nOnly main admin can set log group.",
            parse_mode="HTML"
        )
        return

    if chat.type == "private":
        await update.message.reply_text(
            "❌ <b>Wrong place!</b>\n\n"
            "Run this command <b>inside</b> the private group\n"
            "you want to use as LOG GROUP.\n\n"
            "Steps:\n"
            "1. Create a private group\n"
            "2. Add this bot as admin\n"
            "3. Run /setloggroup inside that group",
            parse_mode="HTML"
        )
        return

    state.log_group_id = chat.id

    await update.message.reply_text(
        f"✅ <b>LOG GROUP SET SUCCESSFULLY!</b>\n\n"
        f"📋 Group: <b>{chat.title}</b>\n"
        f"🆔 Group ID: <code>{chat.id}</code>\n\n"
        f"All deal events will now be logged here:\n"
        f"• Deal Created\n"
        f"• Deal Funded\n"
        f"• Deal Completed\n"
        f"• Disputes\n\n"
        f"✅ Bot is ready for deals!",
        parse_mode="HTML"
    )

    # Confirm log in the log group itself
    await context.bot.send_message(
        chat_id=state.log_group_id,
        text=(
            f"📋 <b>LOG GROUP ACTIVATED</b>\n\n"
            f"This group is now the official log destination.\n"
            f"🆔 Group ID: <code>{chat.id}</code>\n"
            f"👨‍💼 Set by: @{user.username or user.first_name}\n"
            f"⏰ Time: {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC"
        ),
        parse_mode="HTML"
    )


async def cmd_addadmin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user

    if not is_main_admin(user.id):
        await update.message.reply_text(
            "❌ <b>Access Denied.</b>\nOnly main admin can add sub admins.",
            parse_mode="HTML"
        )
        return

    if not context.args:
        await update.message.reply_text(
            "❌ <b>Missing user ID.</b>\n\n"
            "Usage: <code>/addadmin {user_id}</code>\n\n"
            "Example: <code>/addadmin 123456789</code>\n\n"
            "📌 Sub admins can only handle disputes.",
            parse_mode="HTML"
        )
        return

    try:
        new_admin_id = int(context.args[0])

        if new_admin_id == MAIN_ADMIN_ID:
            await update.message.reply_text(
                "⚠️ That is already the main admin.",
                parse_mode="HTML"
            )
            return

        if new_admin_id in state.sub_admins:
            await update.message.reply_text(
                f"⚠️ User <code>{new_admin_id}</code> is already a sub admin.",
                parse_mode="HTML"
            )
            return

        state.sub_admins.add(new_admin_id)

        await update.message.reply_text(
            f"✅ <b>Sub Admin Added!</b>\n\n"
            f"👤 User ID: <code>{new_admin_id}</code>\n"
            f"🔐 Role: Sub Admin (Dispute Handler)\n\n"
            f"Total Sub Admins: {len(state.sub_admins)}",
            parse_mode="HTML"
        )

        # Notify new admin
        try:
            await context.bot.send_message(
                chat_id=new_admin_id,
                text=(
                    f"👨‍💼 <b>You have been added as Sub Admin!</b>\n\n"
                    f"You can now handle disputes in escrow deals.\n\n"
                    f"When a dispute is triggered, you will receive\n"
                    f"an alert with a button to handle it.\n\n"
                    f"Available command:\n"
                    f"<code>/releaseto buyer|seller DEAL_ID</code>"
                ),
                parse_mode="HTML"
            )
        except TelegramError:
            await update.message.reply_text(
                f"⚠️ Could not notify user {new_admin_id} (they may not have started the bot).",
                parse_mode="HTML"
            )

    except ValueError:
        await update.message.reply_text(
            "❌ Invalid user ID. Must be a number.\n"
            "Example: <code>/addadmin 123456789</code>",
            parse_mode="HTML"
        )


async def cmd_removeadmin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user

    if not is_main_admin(user.id):
        await update.message.reply_text(
            "❌ <b>Access Denied.</b>\nOnly main admin can remove sub admins.",
            parse_mode="HTML"
        )
        return

    if not context.args:
        await update.message.reply_text(
            "❌ <b>Missing user ID.</b>\n\n"
            "Usage: <code>/removeadmin {user_id}</code>\n\n"
            "Example: <code>/removeadmin 123456789</code>",
            parse_mode="HTML"
        )
        return

    try:
        rem_id = int(context.args[0])

        if rem_id == MAIN_ADMIN_ID:
            await update.message.reply_text(
                "❌ Cannot remove the main admin.",
                parse_mode="HTML"
            )
            return

        if rem_id not in state.sub_admins:
            await update.message.reply_text(
                f"⚠️ User <code>{rem_id}</code> is not a sub admin.",
                parse_mode="HTML"
            )
            return

        state.sub_admins.discard(rem_id)

        await update.message.reply_text(
            f"✅ <b>Sub Admin Removed!</b>\n\n"
            f"👤 User ID: <code>{rem_id}</code>\n\n"
            f"Remaining Sub Admins: {len(state.sub_admins)}",
            parse_mode="HTML"
        )

        # Notify removed admin
        try:
            await context.bot.send_message(
                chat_id=rem_id,
                text=(
                    "⚠️ <b>Admin Access Removed</b>\n\n"
                    "You have been removed as sub admin.\n"
                    "You no longer have dispute handling access."
                ),
                parse_mode="HTML"
            )
        except TelegramError:
            pass

    except ValueError:
        await update.message.reply_text(
            "❌ Invalid user ID. Must be a number.\n"
            "Example: <code>/removeadmin 123456789</code>",
            parse_mode="HTML"
        )


async def cmd_setfee(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user

    if not is_main_admin(user.id):
        await update.message.reply_text(
            "❌ <b>Access Denied.</b>\nOnly main admin can set fee.",
            parse_mode="HTML"
        )
        return

    if not context.args:
        await update.message.reply_text(
            f"❌ <b>Missing percentage.</b>\n\n"
            f"Usage: <code>/setfee {{percentage}}</code>\n\n"
            f"Examples:\n"
            f"<code>/setfee 1</code>   → 1% fee\n"
            f"<code>/setfee 1.5</code> → 1.5% fee\n"
            f"<code>/setfee 0</code>   → No fee\n\n"
            f"Current fee: <b>{state.fee_percent}%</b>",
            parse_mode="HTML"
        )
        return

    try:
        fee = float(context.args[0])

        if fee < 0:
            await update.message.reply_text("❌ Fee cannot be negative.")
            return

        if fee > 50:
            await update.message.reply_text("❌ Fee cannot exceed 50%.")
            return

        old_fee = state.fee_percent
        state.fee_percent = fee

        await update.message.reply_text(
            f"✅ <b>Fee Updated!</b>\n\n"
            f"Old Fee: <s>{old_fee}%</s>\n"
            f"New Fee: <b>{fee}%</b>\n\n"
            f"📌 Bio exemption: {f'@{state.required_bio} → 0%' if state.required_bio else 'Not set'}",
            parse_mode="HTML"
        )

    except ValueError:
        await update.message.reply_text(
            "❌ Invalid number.\n"
            "Example: <code>/setfee 1.5</code>",
            parse_mode="HTML"
        )


async def cmd_setbio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user

    if not is_main_admin(user.id):
        await update.message.reply_text(
            "❌ <b>Access Denied.</b>\nOnly main admin can set bio requirement.",
            parse_mode="HTML"
        )
        return

    if not context.args:
        await update.message.reply_text(
            f"❌ <b>Missing text.</b>\n\n"
            f"Usage: <code>/setbio {{required_text}}</code>\n\n"
            f"How it works:\n"
            f"• If buyer's bio contains this text → <b>0% fee</b>\n"
            f"• Otherwise → normal fee applies\n\n"
            f"Example: <code>/setbio @EscrowVIP</code>\n\n"
            f"Current: <b>{state.required_bio or 'Not set'}</b>",
            parse_mode="HTML"
        )
        return

    old_bio = state.required_bio
    state.required_bio = context.args[0]

    await update.message.reply_text(
        f"✅ <b>Bio Requirement Set!</b>\n\n"
        f"Required text: <b>{state.required_bio}</b>\n\n"
        f"Users with <code>{state.required_bio}</code> in their bio\n"
        f"will get <b>0% fee</b> on deal completion.\n\n"
        f"Previous setting: <i>{old_bio or 'None'}</i>",
        parse_mode="HTML"
    )


async def cmd_setoxapay(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user

    if not is_main_admin(user.id):
        await update.message.reply_text(
            "❌ <b>Access Denied.</b>\nOnly main admin can set OxaPay key.",
            parse_mode="HTML"
        )
        return

    if not context.args:
        await update.message.reply_text(
            f"❌ <b>Missing API key.</b>\n\n"
            f"Usage: <code>/setoxapay {{api_key}}</code>\n\n"
            f"Get your key from: oxapay.com\n\n"
            f"Current status: <b>{'✅ Set' if state.oxapay_key else '❌ Not Set (Demo Mode)'}</b>",
            parse_mode="HTML"
        )
        return

    old_status = "✅ Was Set" if state.oxapay_key else "❌ Was Not Set"
    state.oxapay_key = context.args[0]

    masked = f"{state.oxapay_key[:4]}{'*' * (len(state.oxapay_key) - 8)}{state.oxapay_key[-4:]}" \
        if len(state.oxapay_key) > 8 else "****"

    await update.message.reply_text(
        f"✅ <b>OxaPay API Key Set!</b>\n\n"
        f"🔑 Key: <code>{masked}</code>\n"
        f"Previous: <i>{old_status}</i>\n\n"
        f"Use /checkoxapay to verify connection.",
        parse_mode="HTML"
    )


async def cmd_checkoxapay(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user

    if not is_main_admin(user.id):
        await update.message.reply_text(
            "❌ <b>Access Denied.</b>",
            parse_mode="HTML"
        )
        return

    if not state.oxapay_key:
        await update.message.reply_text(
            "❌ <b>OxaPay key not set.</b>\n\n"
            "Use /setoxapay to add your API key.\n"
            "Bot is currently running in DEMO mode.",
            parse_mode="HTML"
        )
        return

    await update.message.reply_text(
        "⏳ <b>Checking OxaPay connection...</b>",
        parse_mode="HTML"
    )

    try:
        loop = asyncio.get_event_loop()
        def _check_oxapay():
            req = urllib.request.Request(
                "https://api.oxapay.com/merchants/balance",
                data=_json.dumps({"merchant": state.oxapay_key}).encode(),
                headers={"Content-Type": "application/json"}
            )
            with urllib.request.urlopen(req, timeout=15) as r:
                return _json.loads(r.read().decode())
        data = await loop.run_in_executor(None, _check_oxapay)

        if data.get("result") == 100:
            balances = data.get("balance", {})
            balance_text = "\n".join(
                [f"  • {k}: {v}" for k, v in balances.items()]
            ) if isinstance(balances, dict) else str(balances)

            await update.message.reply_text(
                f"✅ <b>OxaPay Connected!</b>\n\n"
                f"🔑 API Status: Active\n"
                f"💰 Balances:\n{balance_text or '  N/A'}\n\n"
                f"Everything is working correctly.",
                parse_mode="HTML"
            )
        else:
            await update.message.reply_text(
                f"⚠️ <b>OxaPay Error!</b>\n\n"
                f"Response: {data.get('message', 'Unknown error')}\n"
                f"Code: {data.get('result', 'N/A')}\n\n"
                f"Check your API key and try again.",
                parse_mode="HTML"
            )

    except asyncio.TimeoutError:
        await update.message.reply_text(
            "❌ <b>Connection Timeout!</b>\n\n"
            "OxaPay did not respond in time.\n"
            "Check your internet or try again.",
            parse_mode="HTML"
        )
    except Exception as e:
        await update.message.reply_text(
            f"❌ <b>Connection Failed!</b>\n\n"
            f"Error: <code>{str(e)}</code>",
            parse_mode="HTML"
        )


async def cmd_resetoxapay(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user

    if not is_main_admin(user.id):
        await update.message.reply_text(
            "❌ <b>Access Denied.</b>",
            parse_mode="HTML"
        )
        return

    if not state.oxapay_key:
        await update.message.reply_text(
            "⚠️ OxaPay key was not set. Nothing to reset.",
            parse_mode="HTML"
        )
        return

    state.oxapay_key = None

    await update.message.reply_text(
        "✅ <b>OxaPay API Key Removed!</b>\n\n"
        "Bot is now running in <b>DEMO mode</b>.\n\n"
        "• /deposit will show demo addresses\n"
        "• /verify will simulate payment\n"
        "• No real transactions will occur\n\n"
        "Use /setoxapay to add a new key.",
        parse_mode="HTML"
    )


async def cmd_releaseto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user

    if not is_admin(user.id):
        await update.message.reply_text(
            "❌ <b>Access Denied.</b>\nAdmin only command.",
            parse_mode="HTML"
        )
        return

    if len(context.args) < 2:
        await update.message.reply_text(
            "❌ <b>Missing arguments.</b>\n\n"
            "Usage: <code>/releaseto buyer|seller DEAL_ID</code>\n\n"
            "Examples:\n"
            "<code>/releaseto buyer TRD-ABC12345</code>\n"
            "<code>/releaseto seller TRD-ABC12345</code>\n\n"
            "⚠️ Use only during active dispute.",
            parse_mode="HTML"
        )
        return

    party = context.args[0].lower()
    deal_id = context.args[1].upper()

    if party not in ["buyer", "seller"]:
        await update.message.reply_text(
            "❌ Invalid party.\n"
            "Must be <b>buyer</b> or <b>seller</b>.",
            parse_mode="HTML"
        )
        return

    deal = get_deal_by_id(deal_id)
    if not deal:
        await update.message.reply_text(
            f"❌ Deal not found: <code>{deal_id}</code>",
            parse_mode="HTML"
        )
        return

    if deal.get("status") == "COMPLETED":
        await update.message.reply_text(
            f"⚠️ Deal <code>{deal_id}</code> is already COMPLETED.",
            parse_mode="HTML"
        )
        return

    if deal.get("status") not in ["DISPUTED", "FUNDED", "AWAITING_DEPOSIT"]:
        await update.message.reply_text(
            f"⚠️ Deal status is <b>{deal.get('status')}</b>.\n"
            f"Can only force release on DISPUTED or FUNDED deals.",
            parse_mode="HTML"
        )
        return

    # Check if this admin is assigned to this dispute
    if deal.get("status") == "DISPUTED":
        assigned = state.dispute_admins.get(deal_id)
        if assigned and assigned != user.id and not is_main_admin(user.id):
            await update.message.reply_text(
                "❌ Another admin is already handling this dispute.",
                parse_mode="HTML"
            )
            return

    release_to_username = deal.get(f"{party}_username", "N/A")
    release_to_address = deal.get(f"{party}_address", "N/A")
    quantity = float(deal.get("quantity", 0))

    # Apply fee calculation
    fee_amount = quantity * (state.fee_percent / 100)
    final_amount = quantity - fee_amount

    # Update deal state
    deal["status"] = "COMPLETED"
    deal["force_released_to"] = party
    deal["force_release_admin"] = user.username or str(user.id)
    deal["fee_deducted"] = fee_amount
    deal["final_amount"] = final_amount
    deal["completed_at"] = datetime.utcnow().isoformat()

    # Notify deal group
    try:
        await context.bot.send_message(
            chat_id=deal["group_id"],
            text=(
                f"⚖️ <b>ADMIN DECISION — DEAL RESOLVED</b>\n\n"
                f"👨‍💼 Admin: @{user.username or 'Admin'}\n"
                f"⚖️ Decision: Release to <b>{party.upper()}</b>\n\n"
                f"─────────────────────\n"
                f"🆔 Trade ID: <code>{deal_id}</code>\n"
                f"🪙 Token: {deal.get('token', 'N/A')}\n"
                f"💰 Original: {quantity}\n"
                f"💸 Fee ({state.fee_percent}%): {fee_amount:.4f}\n"
                f"✅ Released: {final_amount:.4f}\n"
                f"👤 To: @{release_to_username}\n"
                f"📬 Address: <code>{release_to_address}</code>\n"
                f"─────────────────────\n\n"
                f"📊 Status: <b>COMPLETED</b>\n"
                f"⏰ {deal['completed_at']}\n\n"
                f"This group will close shortly."
            ),
            parse_mode="HTML"
        )
    except TelegramError as e:
        logger.error(f"Could not notify deal group: {e}")

    # Confirm to admin
    await update.message.reply_text(
        f"✅ <b>Force Release Done!</b>\n\n"
        f"🆔 Deal: <code>{deal_id}</code>\n"
        f"⚖️ Released to: <b>{party.upper()}</b> (@{release_to_username})\n"
        f"💰 Amount: {final_amount:.4f} {deal.get('token', '')}\n"
        f"📬 Address: <code>{release_to_address}</code>",
        parse_mode="HTML"
    )

    # Full log to LOG GROUP
    await send_log(
        context,
        f"⚖️ <b>ADMIN FORCE RELEASE</b>\n\n"
        f"🆔 Trade ID: <code>{deal_id}</code>\n"
        f"👤 Buyer: @{deal.get('buyer_username', 'N/A')}\n"
        f"👤 Seller: @{deal.get('seller_username', 'N/A')}\n"
        f"⚖️ Released to: {party.upper()} (@{release_to_username})\n"
        f"📬 Address: <code>{release_to_address}</code>\n"
        f"🪙 Token: {deal.get('token', 'N/A')}\n"
        f"💰 Original Amount: {quantity}\n"
        f"💸 Fee Deducted: {fee_amount:.4f} ({state.fee_percent}%)\n"
        f"✅ Final Amount: {final_amount:.4f}\n"
        f"👨‍💼 Admin: @{user.username or str(user.id)}\n"
        f"📦 Group ID: <code>{deal.get('group_id')}</code>\n"
        f"📊 Status: COMPLETED (Force Release)\n"
        f"⏰ Timestamp: {deal['completed_at']}"
    )

    # Auto cleanup after delay
    await asyncio.sleep(15)
    try:
        await context.bot.send_message(
            chat_id=deal["group_id"],
            text=(
                "🗑 <b>This deal group is now closed.</b>\n"
                "Thank you for using P2P Escrow Bot."
            ),
            parse_mode="HTML"
        )
        # Notify both parties privately
        for p in ["buyer", "seller"]:
            pid = deal.get(f"{p}_id")
            if pid:
                try:
                    await context.bot.send_message(
                        chat_id=pid,
                        text=(
                            f"📋 <b>Deal Closed: {deal_id}</b>\n\n"
                            f"Your deal has been resolved by admin.\n"
                            f"Outcome: Funds released to <b>{party.upper()}</b>\n\n"
                            f"Contact admin if you have questions."
                        ),
                        parse_mode="HTML"
                    )
                except TelegramError:
                    pass
    except TelegramError:
        pass


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user

    if not is_admin(user.id):
        await update.message.reply_text(
            "❌ <b>Access Denied.</b>\nAdmin only command.",
            parse_mode="HTML"
        )
        return

    # Deal statistics
    all_deals = list(state.deals.values())
    total = len(all_deals)
    active = len([d for d in all_deals if d["status"] not in ["COMPLETED"]])
    completed = len([d for d in all_deals if d["status"] == "COMPLETED"])
    disputed = len([d for d in all_deals if d["status"] == "DISPUTED"])
    funded = len([d for d in all_deals if d["status"] == "FUNDED"])

    oxapay_status = (
        f"✅ Active (<code>{state.oxapay_key[:4]}...{state.oxapay_key[-4:]}</code>)"
        if state.oxapay_key else "❌ Not Set (Demo Mode)"
    )

    log_group_status = (
        f"✅ Set (<code>{state.log_group_id}</code>)"
        if state.log_group_id else "❌ Not Set"
    )

    await update.message.reply_text(
        f"📊 <b>BOT STATUS DASHBOARD</b>\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"⚙️ <b>CONFIGURATION</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"📋 Log Group: {log_group_status}\n"
        f"🔑 OxaPay: {oxapay_status}\n"
        f"💸 Fee: <b>{state.fee_percent}%</b>\n"
        f"👤 Bio Exemption: <b>{state.required_bio or 'Not Set'}</b>\n"
        f"👨‍💼 Sub Admins: <b>{len(state.sub_admins)}</b>\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"📦 <b>DEAL STATISTICS</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"📁 Total Deals: <b>{total}</b>\n"
        f"🟢 Active: <b>{active}</b>\n"
        f"✅ Completed: <b>{completed}</b>\n"
        f"💰 Funded: <b>{funded}</b>\n"
        f"🚨 Disputed: <b>{disputed}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"⏰ Uptime: Since bot start\n"
        f"🤖 Mode: {'LIVE' if state.oxapay_key else 'DEMO'}",
        parse_mode="HTML"
    )

async def cmd_dealinfo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat = update.effective_chat

    if not context.args:
        await update.message.reply_text(
            "❌ <b>Missing deal ID.</b>\n\n"
            "Usage: <code>/dealinfo {TRADE_ID}</code>\n\n"
            "Example: <code>/dealinfo TRD-ABC12345</code>",
            parse_mode="HTML"
        )
        return

    deal_id = context.args[0].upper()
    deal = get_deal_by_id(deal_id)

    if not deal:
        await update.message.reply_text(
            f"❌ Deal not found: <code>{deal_id}</code>\n\n"
            f"Make sure the Trade ID is correct.",
            parse_mode="HTML"
        )
        return

    # Authorization check
    # Allow if: admin, or user is part of deal, or in the deal group
    is_participant = user.id in [deal.get("buyer_id"), deal.get("seller_id")]
    group_deal_id = state.group_to_deal.get(chat.id)
    is_in_deal_group = group_deal_id == deal_id

    if not is_admin(user.id) and not is_participant and not is_in_deal_group:
        await update.message.reply_text(
            "❌ <b>Not authorized.</b>\n"
            "You are not part of this deal.",
            parse_mode="HTML"
        )
        return

    # Status emoji map
    status_emoji = {
        "SETUP": "🔧",
        "ROLES_SET": "👥",
        "TOKEN_SELECTED": "🪙",
        "AWAITING_DEPOSIT": "⏳",
        "FUNDED": "💰",
        "COMPLETED": "✅",
        "DISPUTED": "🚨"
    }
    s_emoji = status_emoji.get(deal.get("status", ""), "📋")

    buyer_confirm = "✅" if deal.get("buyer_confirmed") else "⏳"
    seller_confirm = "✅" if deal.get("seller_confirmed") else "⏳"

    await update.message.reply_text(
        f"📋 <b>DEAL INFORMATION</b>\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"🆔 Trade ID: <code>{deal_id}</code>\n"
        f"{s_emoji} Status: <b>{deal.get('status', 'N/A')}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"👥 <b>PARTIES</b>\n"
        f"🛒 Buyer: @{deal.get('buyer_username', 'Not Set')}\n"
        f"   Address: <code>{deal.get('buyer_address', 'N/A')}</code>\n"
        f"🏪 Seller: @{deal.get('seller_username', 'Not Set')}\n"
        f"   Address: <code>{deal.get('seller_address', 'N/A')}</code>\n\n"
        f"💰 <b>DEAL DETAILS</b>\n"
        f"📦 Quantity: <b>{deal.get('quantity', 'N/A')}</b>\n"
        f"📈 Rate: <b>{deal.get('rate', 'N/A')}</b>\n"
        f"📝 Condition: {deal.get('condition', 'None')}\n"
        f"🪙 Token: <b>{deal.get('token', 'Not Selected')}</b>\n\n"
        f"📬 Deposit Address:\n"
        f"<code>{deal.get('deposit_address', 'Not Generated')}</code>\n\n"
        f"✅ <b>CONFIRMATIONS</b>\n"
        f"{buyer_confirm} Buyer: {'Confirmed' if deal.get('buyer_confirmed') else 'Pending'}\n"
        f"{seller_confirm} Seller: {'Confirmed' if deal.get('seller_confirmed') else 'Pending'}\n\n"
        f"⏰ Created: {deal.get('created_at', 'N/A')[:19].replace('T', ' ')} UTC\n"
        f"⏰ Completed: {deal.get('completed_at', 'N/A')[:19].replace('T', ' ') if deal.get('completed_at') else 'N/A'} UTC",
        parse_mode="HTML"
    )

async def cmd_listadmins(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user

    if not is_main_admin(user.id):
        await update.message.reply_text(
            "❌ <b>Access Denied.</b>",
            parse_mode="HTML"
        )
        return

    admin_list = f"👑 Main Admin: <code>{MAIN_ADMIN_ID}</code>\n\n"

    if state.sub_admins:
        admin_list += "👨‍💼 <b>Sub Admins:</b>\n"
        for i, admin_id in enumerate(state.sub_admins, 1):
            admin_list += f"{i}. <code>{admin_id}</code>\n"
    else:
        admin_list += "👨‍💼 <b>Sub Admins:</b> None"

    await update.message.reply_text(
        f"📋 <b>ADMIN LIST</b>\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"{admin_list}\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"Total: {1 + len(state.sub_admins)} admins",
        parse_mode="HTML"
    )


async def cmd_canceldeal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin can cancel a deal forcefully."""
    user = update.effective_user

    if not is_main_admin(user.id):
        await update.message.reply_text(
            "❌ <b>Access Denied.</b>\nMain admin only.",
            parse_mode="HTML"
        )
        return

    if not context.args:
        await update.message.reply_text(
            "Usage: <code>/canceldeal {TRADE_ID}</code>",
            parse_mode="HTML"
        )
        return

    deal_id = context.args[0].upper()
    deal = get_deal_by_id(deal_id)

    if not deal:
        await update.message.reply_text(f"❌ Deal not found: <code>{deal_id}</code>", parse_mode="HTML")
        return

    if deal.get("status") == "COMPLETED":
        await update.message.reply_text("⚠️ Cannot cancel a completed deal.")
        return

    old_status = deal.get("status")
    deal["status"] = "CANCELLED"
    deal["cancelled_by"] = user.username
    deal["cancelled_at"] = datetime.utcnow().isoformat()

    try:
        await context.bot.send_message(
            chat_id=deal["group_id"],
            text=(
                f"🚫 <b>DEAL CANCELLED BY ADMIN</b>\n\n"
                f"🆔 Trade ID: <code>{deal_id}</code>\n"
                f"👨‍💼 Cancelled by: @{user.username}\n\n"
                f"This deal has been cancelled.\n"
                f"No funds have been transferred."
            ),
            parse_mode="HTML"
        )
    except TelegramError:
        pass

    await update.message.reply_text(
        f"✅ Deal <code>{deal_id}</code> cancelled.\n"
        f"Previous status: {old_status}",
        parse_mode="HTML"
    )

    await send_log(
        context,
        f"🚫 <b>DEAL CANCELLED</b>\n\n"
        f"🆔 Trade ID: <code>{deal_id}</code>\n"
        f"👨‍💼 Cancelled by: @{user.username}\n"
        f"📊 Previous Status: {old_status}\n"
        f"⏰ Time: {deal['cancelled_at']}"
    )
  
