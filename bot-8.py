"""
P2P Telegram Escrow Bot — Full Implementation
Single file | No database | LOG GROUP = storage | Telethon auto group creation
"""

import asyncio
import urllib.request
import json as _json
import uuid
import io
import logging
import qrcode
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputFile
from telegram.ext import ApplicationBuilder, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ContextTypes
from telethon import TelegramClient
from telethon.tl.functions.channels import (
    CreateChannelRequest, InviteToChannelRequest,
    EditAdminRequest
)
from telethon.tl.functions.messages import ExportChatInviteRequest
from telethon.tl.types import ChatAdminRights
from config import BOT_TOKEN, MAIN_ADMIN_ID, API_ID, API_HASH, PHONE, state

logging.basicConfig(format="%(asctime)s | %(levelname)s | %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# Tracks which admin is waiting for text input after pressing a Set button
# Maps user_id -> field_name (e.g. "fee", "bio", "addadmin", "api_id", etc.)
_admin_waiting: dict[int, str] = {}

# ══════════════════════════════════════════════════════════
# TELETHON — Start & Auto Group Creation
# ══════════════════════════════════════════════════════════

async def start_telethon():
    if not API_ID or not API_HASH or not PHONE:
        logger.warning("Telethon credentials missing. Auto group creation disabled.")
        return
    client = TelegramClient("escrow_session", API_ID, API_HASH)
    await client.start(phone=PHONE)
    state.telethon_client = client
    logger.info("✅ Telethon client started.")

async def create_group_telethon(title: str, bot_username: str):
    client = state.telethon_client
    if not client:
        return None, None
    try:
        result = await client(CreateChannelRequest(
            title=title,
            about="P2P Escrow Deal Group",
            megagroup=True
        ))
        channel = result.chats[0]
        group_id = int(f"-100{channel.id}")

        bot_entity = await client.get_entity(bot_username)
        await client(InviteToChannelRequest(channel=channel, users=[bot_entity]))

        rights = ChatAdminRights(
            post_messages=True, edit_messages=True, delete_messages=True,
            ban_users=True, invite_users=True, pin_messages=True,
            add_admins=False, manage_call=True, other=True
        )
        await client(EditAdminRequest(
            channel=channel, user_id=bot_entity,
            admin_rights=rights, rank="Escrow Bot"
        ))

        invite = await client(ExportChatInviteRequest(peer=channel))
        return group_id, invite.link

    except Exception as e:
        logger.error(f"Telethon group creation failed: {e}")
        return None, None

# ══════════════════════════════════════════════════════════
# HELPERS
# ══════════════════════════════════════════════════════════

def is_main_admin(uid): return uid == MAIN_ADMIN_ID
def is_admin(uid): return uid == MAIN_ADMIN_ID or uid in state.sub_admins
def trade_id(): return "TRD-" + str(uuid.uuid4()).upper()[:8]
def deal_by_group(cid):
    did = state.group_to_deal.get(cid)
    return (did, state.deals.get(did)) if did else (None, None)
def deal_by_id(did): return state.deals.get(did)

async def log(ctx, msg):
    if state.log_group_id:
        try:
            await ctx.bot.send_message(chat_id=state.log_group_id, text=f"📋 LOG\n\n{msg}", parse_mode="HTML")
        except Exception as e:
            logger.error(f"Log error: {e}")

async def alert_admins(ctx, msg, deal_id=None):
    for uid in [MAIN_ADMIN_ID] + list(state.sub_admins):
        try:
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("🚨 Handle Dispute", callback_data=f"dispute_handle:{deal_id}")]]) if deal_id else None
            await ctx.bot.send_message(chat_id=uid, text=msg, reply_markup=kb, parse_mode="HTML")
        except Exception:
            pass

def qr_bytes(data):
    img = qrcode.make(data)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf.read()

async def send_qr(ctx, chat_id, address, caption):
    try:
        await ctx.bot.send_photo(
            chat_id=chat_id,
            photo=InputFile(io.BytesIO(qr_bytes(address)), filename="qr.png"),
            caption=caption, parse_mode="HTML"
        )
    except Exception:
        await ctx.bot.send_message(chat_id=chat_id, text=caption, parse_mode="HTML")

def new_deal(tid, group_id, creator_id):
    return {
        "trade_id": tid, "group_id": group_id, "status": "SETUP",
        "creator_id": creator_id,
        "buyer_id": None, "buyer_username": None, "buyer_address": None,
        "seller_id": None, "seller_username": None, "seller_address": None,
        "quantity": None, "rate": None, "condition": None, "token": None,
        "token_buyer_confirmed": False, "token_seller_confirmed": False,
        "deposit_address": None,
        "buyer_confirmed": False, "seller_confirmed": False,
        "funded": False, "created_at": datetime.utcnow().isoformat()
    }

# ══════════════════════════════════════════════════════════
# /start
# ══════════════════════════════════════════════════════════

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        return
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🤝 Start Deal", callback_data="start_deal")],
        [InlineKeyboardButton("📖 Instructions", callback_data="show_instructions")]
    ])
    await update.message.reply_text(
        "👋 <b>Welcome to P2P Escrow Bot</b>\n\nSecure peer-to-peer trading with automatic escrow.\n\nChoose an option below:",
        reply_markup=kb, parse_mode="HTML"
    )

# ══════════════════════════════════════════════════════════
# /instructions
# ══════════════════════════════════════════════════════════

async def cmd_instructions(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = (
        "📖 <b>HOW TO USE ESCROW BOT</b>\n\n"
        "<b>1️⃣</b> /start → <b>Start Deal</b> — bot creates private group\n\n"
        "<b>2️⃣</b> Both join → use <b>/dd</b> [qty] [rate] [condition]\n\n"
        "<b>3️⃣</b> <b>/buyer</b> [address] and <b>/seller</b> [address]\n\n"
        "<b>4️⃣</b> <b>/token</b> → select token → both confirm\n\n"
        "<b>5️⃣</b> <b>/deposit</b> → seller gets OxaPay address and pays crypto to escrow\n\n"
        "<b>6️⃣</b> <b>/verify</b> → OxaPay payment confirmed → buyer now pays seller off-platform\n\n"
        "<b>7️⃣</b> <b>/release</b> → buyer or seller runs this after buyer has paid\n\n"
        "<b>8️⃣</b> Both press <b>Confirm</b> → crypto releases to buyer's address\n\n"
        "<b>9️⃣</b> <b>/dispute</b> → call admin if any issue — admin joins the group\n\n"
        "⚠️ <i>All steps must be done inside your deal group</i>"
    )
    if update.callback_query:
        await update.callback_query.edit_message_text(text, parse_mode="HTML")
    else:
        await update.message.reply_text(text, parse_mode="HTML")

# ══════════════════════════════════════════════════════════
# ADMIN PANEL
# ══════════════════════════════════════════════════════════

def admin_panel_kb():
    tc = "✅ ON" if state.telethon_client else "❌ OFF"
    ox = "✅ SET" if state.oxapay_key else "❌ NOT SET"
    lg = "✅ SET" if state.log_group_id else "❌ NOT SET"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"📋 Log Group {lg}", callback_data="adm:setloggroup"),
         InlineKeyboardButton("📊 Status", callback_data="adm:status")],
        [InlineKeyboardButton("➕ Add Admin", callback_data="adm:addadmin"),
         InlineKeyboardButton("➖ Remove Admin", callback_data="adm:removeadmin")],
        [InlineKeyboardButton("💸 Set Fee %", callback_data="adm:setfee"),
         InlineKeyboardButton("🏷 Set Bio Tag", callback_data="adm:setbio")],
        [InlineKeyboardButton(f"🔑 OxaPay {ox}", callback_data="adm:setoxapay"),
         InlineKeyboardButton("✅ Check OxaPay", callback_data="adm:checkoxapay")],
        [InlineKeyboardButton("🗑 Reset OxaPay", callback_data="adm:resetoxapay"),
         InlineKeyboardButton(f"📡 Telethon {tc}", callback_data="adm:telethon")],
        [InlineKeyboardButton("👥 List Admins", callback_data="adm:listadmins"),
         InlineKeyboardButton("🔄 Refresh", callback_data="adm:status")]
    ])

async def cmd_adminpanel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_main_admin(update.effective_user.id):
        return
    await update.message.reply_text("👑 <b>ADMIN CONTROL PANEL</b>\n\nSelect an action:", reply_markup=admin_panel_kb(), parse_mode="HTML")

# ══════════════════════════════════════════════════════════
# CALLBACK ROUTER
# ══════════════════════════════════════════════════════════

async def callback_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    d = q.data
    if d == "start_deal":                   await handle_start_deal(update, ctx)
    elif d == "show_instructions":          await cmd_instructions(update, ctx)
    elif d.startswith("token_select:"):     await handle_token_pick(update, ctx, d)
    elif d.startswith("token_confirm:"):    await handle_token_confirm(update, ctx, d)
    elif d.startswith("token_reselect:"):   await handle_token_reselect(update, ctx, d)
    elif d.startswith("confirm:"):          await handle_confirmation(update, ctx, d)
    elif d.startswith("dispute_handle:"):   await handle_dispute_admin(update, ctx, d)
    elif d.startswith("dispute_call"):      await handle_dispute_call(update, ctx)
    elif d.startswith("adm:"):              await handle_admin_panel_cb(update, ctx, d)

# ══════════════════════════════════════════════════════════
# ADMIN PANEL CALLBACKS
# ══════════════════════════════════════════════════════════

async def handle_admin_panel_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE, d: str):
    q = update.callback_query
    if not is_main_admin(q.from_user.id):
        await q.answer("❌ Access denied.", show_alert=True)
        return
    action = d.split(":", 1)[1]

    if action == "status":
        all_d = list(state.deals.values())
        total = len(all_d)
        done = sum(1 for x in all_d if x["status"] == "COMPLETED")
        dis  = sum(1 for x in all_d if x["status"] == "DISPUTED")
        fund = sum(1 for x in all_d if x["status"] == "FUNDED")
        ox = f"✅ {state.oxapay_key[:4]}...{state.oxapay_key[-4:]}" if state.oxapay_key else "❌ Not Set (Demo)"
        lg = f"✅ <code>{state.log_group_id}</code>" if state.log_group_id else "❌ Not Set"
        tc = "✅ Connected" if state.telethon_client else "❌ Not Connected"
        await q.edit_message_text(
            f"📊 <b>BOT STATUS</b>\n\n"
            f"📋 Log Group: {lg}\n🔑 OxaPay: {ox}\n📡 Telethon: {tc}\n"
            f"💸 Fee: <b>{state.fee_percent}%</b>\n🏷 Bio Tag: <b>{state.required_bio or 'Not Set'}</b>\n"
            f"👥 Sub Admins: <b>{len(state.sub_admins)}</b>\n\n"
            f"📦 Total: {total}  🟢 Active: {total-done}  ✅ Done: {done}\n"
            f"💰 Funded: {fund}  🚨 Disputed: {dis}\n\n"
            f"🤖 Mode: {'LIVE' if state.oxapay_key else 'DEMO'}",
            parse_mode="HTML", reply_markup=admin_panel_kb()
        )

    elif action == "listadmins":
        txt = f"👑 Main: <code>{MAIN_ADMIN_ID}</code>\n\n"
        txt += ("👨‍💼 Sub Admins:\n" + "".join(f"{i}. <code>{a}</code>\n" for i, a in enumerate(state.sub_admins, 1))) if state.sub_admins else "👨‍💼 Sub Admins: None"
        await q.edit_message_text(f"📋 <b>ADMIN LIST</b>\n\n{txt}", parse_mode="HTML", reply_markup=admin_panel_kb())

    elif action == "checkoxapay":
        if not state.oxapay_key:
            await q.edit_message_text("❌ OxaPay key not set.", parse_mode="HTML", reply_markup=admin_panel_kb())
            return
        await q.edit_message_text("⏳ Checking OxaPay…", parse_mode="HTML")
        try:
            loop = asyncio.get_event_loop()
            def _check_ox_cb():
                req = urllib.request.Request(
                    "https://api.oxapay.com/merchants/balance",
                    data=_json.dumps({"merchant": state.oxapay_key}).encode(),
                    headers={"Content-Type": "application/json"}
                )
                with urllib.request.urlopen(req, timeout=15) as r:
                    return _json.loads(r.read().decode())
            data = await loop.run_in_executor(None, _check_ox_cb)
            if data.get("result") == 100:
                bal = data.get("balance", {})
                bal_txt = "\n".join(f"  • {k}: {v}" for k, v in bal.items()) if bal else "N/A"
                txt = f"✅ <b>OxaPay Connected!</b>\n\n💰 Balances:\n{bal_txt}"
            else:
                txt = f"⚠️ Error: {data.get('message', 'Unknown')}"
        except Exception as e:
            txt = f"❌ Connection failed: {e}"
        await q.edit_message_text(txt, parse_mode="HTML", reply_markup=admin_panel_kb())

    elif action == "resetoxapay":
        state.oxapay_key = None
        await q.edit_message_text("✅ <b>OxaPay key removed.</b> Bot is in DEMO mode.", parse_mode="HTML", reply_markup=admin_panel_kb())

    elif action == "setloggroup":
        await q.edit_message_text(
            "📋 <b>Set Log Group</b>\n\n1. Create a private group\n2. Add bot as admin\n3. Send <code>/setloggroup</code> inside that group\n\n⬅️ /adminpanel",
            parse_mode="HTML"
        )

    elif action == "telethon":
        tc = "✅ Connected" if state.telethon_client else "❌ Not Connected"
        api_status = "✅ Set" if state.api_id else "❌ Not Set"
        hash_status = "✅ Set" if state.api_hash else "❌ Not Set"
        phone_status = f"✅ {state.phone}" if state.phone else "❌ Not Set"
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔢 Set API ID", callback_data="adm:inp:api_id"),
             InlineKeyboardButton("🔑 Set API Hash", callback_data="adm:inp:api_hash")],
            [InlineKeyboardButton("📱 Set Phone", callback_data="adm:inp:phone")],
            [InlineKeyboardButton("🚀 Connect Telethon", callback_data="adm:starttelethon")],
            [InlineKeyboardButton("⬅️ Back", callback_data="adm:status")]
        ])
        await q.edit_message_text(
            f"📡 <b>TELETHON SETUP</b>\n\nStatus: {tc}\n\n"
            f"• API ID: <b>{api_status}</b>\n"
            f"• API Hash: <b>{hash_status}</b>\n"
            f"• Phone: <b>{phone_status}</b>\n\n"
            f"Get API ID & Hash from: my.telegram.org\n\n"
            f"Set each one then press 🚀 Connect",
            parse_mode="HTML", reply_markup=kb
        )

    elif action == "starttelethon":
        if not state.api_id or not state.api_hash or not state.phone:
            await q.answer("❌ Set API ID, Hash and Phone first!", show_alert=True)
            return
        await q.edit_message_text("⏳ <b>Connecting Telethon…</b>\n\nCheck your Telegram app for OTP.", parse_mode="HTML")
        try:
            from telethon import TelegramClient
            client = TelegramClient("escrow_session", int(state.api_id), state.api_hash)
            state._pending_telethon = client
            await client.connect()
            if not await client.is_user_authorized():
                await client.send_code_request(state.phone)
                state._waiting_otp = True
                kb = InlineKeyboardMarkup([[InlineKeyboardButton("✏️ Enter OTP", callback_data="adm:inp:otp")]])
                await q.edit_message_text(
                    "📲 <b>OTP Sent!</b>\n\nPress below to enter the code from your Telegram app.",
                    parse_mode="HTML", reply_markup=kb
                )
            else:
                state.telethon_client = client
                await q.edit_message_text("✅ <b>Telethon Connected!</b>\n\nAuto group creation is now ACTIVE! 🎉", parse_mode="HTML", reply_markup=admin_panel_kb())
        except Exception as e:
            await q.edit_message_text(f"❌ <b>Telethon Error:</b> {e}", parse_mode="HTML", reply_markup=admin_panel_kb())

    elif action.startswith("inp:"):
        # User pressed a Set button — ask for input
        field = action.split(":")[1]
        labels = {
            "api_id":    ("🔢 <b>Enter API ID</b>", "Numbers only. Get from my.telegram.org"),
            "api_hash":  ("🔑 <b>Enter API Hash</b>", "Long string. Get from my.telegram.org"),
            "phone":     ("📱 <b>Enter Phone Number</b>", "Include country code. Example: +1234567890"),
            "otp":       ("📲 <b>Enter OTP Code</b>", "Check your Telegram app for the code"),
            "oxapay":    ("🔑 <b>Enter OxaPay API Key</b>", "From your OxaPay merchant dashboard"),
            "fee":       ("💸 <b>Enter Fee Percentage</b>", "Numbers only. Example: 1.5 (for 1.5%)"),
            "bio":       ("🏷 <b>Enter Bio Tag</b>", "Users with this in bio get 0% fee"),
            "addadmin":  ("➕ <b>Enter User ID</b>", "Telegram user ID to add as sub admin"),
            "removeadmin": ("➖ <b>Enter User ID</b>", "Telegram user ID to remove from sub admins"),
        }
        title, hint = labels.get(field, ("✏️ <b>Enter Value</b>", "Type and send"))
        _admin_waiting[q.from_user.id] = field
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="adm:cancel_input")]])
        await q.edit_message_text(
            f"{title}\n\n💡 {hint}\n\nType and send your message now:",
            parse_mode="HTML", reply_markup=kb
        )

    elif action == "cancel_input":
        _admin_waiting.pop(q.from_user.id, None)
        await q.edit_message_text("❌ <b>Cancelled.</b>", parse_mode="HTML", reply_markup=admin_panel_kb())

    elif action in ("addadmin", "removeadmin", "setfee", "setbio", "setoxapay"):
        field_map = {
            "addadmin": "addadmin", "removeadmin": "removeadmin",
            "setfee": "fee", "setbio": "bio", "setoxapay": "oxapay"
        }
        field = field_map[action]
        labels = {
            "addadmin":    ("➕ <b>Add Sub Admin</b>",    "Enter Telegram User ID"),
            "removeadmin": ("➖ <b>Remove Sub Admin</b>", "Enter Telegram User ID to remove"),
            "fee":         ("💸 <b>Set Fee %</b>",        f"Current: {state.fee_percent}% — Enter new value (0-50)"),
            "bio":         ("🏷 <b>Set Bio Tag</b>",      f"Current: {state.required_bio or 'Not set'} — Enter new tag"),
            "oxapay":      ("🔑 <b>Set OxaPay Key</b>",  "Enter your OxaPay API key"),
        }
        title, hint = labels[field]
        _admin_waiting[q.from_user.id] = field
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="adm:cancel_input")]])
        await q.edit_message_text(
            f"{title}\n\n💡 {hint}\n\nType and send your message now:",
            parse_mode="HTML", reply_markup=kb
        )

# ══════════════════════════════════════════════════════════
# STEP 2: START DEAL — Telethon auto group creation
# ══════════════════════════════════════════════════════════

async def handle_start_deal(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    user = q.from_user

    if not state.log_group_id:
        await q.edit_message_text("❌ <b>Cannot create deal.</b>\n\nAdmin has not set the LOG GROUP yet.", parse_mode="HTML")
        return

    await q.edit_message_text("⏳ <b>Creating your private deal group…</b>\nPlease wait.", parse_mode="HTML")

    tid = trade_id()
    group_id, invite_url = None, None

    if state.telethon_client:
        bot_me = await ctx.bot.get_me()
        group_id, invite_url = await create_group_telethon(f"🔒 Escrow {tid}", bot_me.username)

    if not group_id:
        await ctx.bot.send_message(
            chat_id=user.id,
            text=(
                "⚠️ <b>Auto Group Creation Failed</b>\n\n"
                "Please do this instead:\n"
                "1️⃣ Create a Telegram group manually\n"
                "2️⃣ Add this bot as <b>Admin</b>\n"
                "3️⃣ Run <code>/initdeal</code> inside the group\n\n"
                "<i>Make sure API_ID, API_HASH and PHONE are correct in your .env</i>"
            ),
            parse_mode="HTML"
        )
        return

    deal = new_deal(tid, group_id, user.id)
    state.deals[tid] = deal
    state.group_to_deal[group_id] = tid

    await ctx.bot.send_message(
        chat_id=user.id,
        text=(
            f"✅ <b>Deal Group Created!</b>\n\n"
            f"🆔 Trade ID: <code>{tid}</code>\n"
            f"🔗 Invite Link: {invite_url}\n\n"
            f"Share this link with the other party.\n\n"
            f"➡️ <b>Next step:</b> Both join the group, then use <b>/dd</b> inside it."
        ),
        parse_mode="HTML"
    )

    try:
        await ctx.bot.send_message(
            chat_id=group_id,
            text=(
                f"🔒 <b>Escrow Deal Group Ready</b>\n\n"
                f"🆔 Trade ID: <code>{tid}</code>\n\n"
                f"Both buyer and seller must join this group.\n\n"
                f"➡️ <b>Next step:</b> Use <b>/dd</b> to fill deal details."
            ),
            parse_mode="HTML"
        )
    except Exception as e:
        logger.warning(f"Could not send welcome msg to group: {e}")

    await log(ctx,
        f"🆕 <b>DEAL CREATED</b>\n\n"
        f"🆔 Trade ID: <code>{tid}</code>\n"
        f"👤 Creator: @{user.username} ({user.id})\n"
        f"📦 Group ID: <code>{group_id}</code>\n"
        f"🔗 Invite: {invite_url}\n"
        f"⏰ Time: {deal['created_at']}"
    )

# ══════════════════════════════════════════════════════════
# /initdeal — manual fallback
# ══════════════════════════════════════════════════════════

async def cmd_initdeal(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    user = update.effective_user
    if chat.type == "private":
        await update.message.reply_text("❌ Use inside a group.")
        return
    if not state.log_group_id:
        await update.message.reply_text("❌ <b>LOG GROUP not set.</b> Admin must run /setloggroup first.", parse_mode="HTML")
        return
    if chat.id in state.group_to_deal:
        await update.message.reply_text("⚠️ This group already has an active deal.")
        return

    tid = trade_id()
    deal = new_deal(tid, chat.id, user.id)
    state.deals[tid] = deal
    state.group_to_deal[chat.id] = tid

    await update.message.reply_text(
        f"🔒 <b>Escrow Deal Initialized</b>\n\n"
        f"🆔 Trade ID: <code>{tid}</code>\n\n"
        f"➡️ <b>Next step:</b> Use <b>/dd</b> to fill deal details.",
        parse_mode="HTML"
    )
    await log(ctx, f"🆕 <b>DEAL CREATED</b>\n\n🆔 <code>{tid}</code>\n👤 @{user.username} ({user.id})\n📦 <code>{chat.id}</code>\n⏰ {deal['created_at']}")

# ══════════════════════════════════════════════════════════
# STEP 3: /dd
# ══════════════════════════════════════════════════════════

async def cmd_dd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if chat.type == "private":
        await update.message.reply_text("❌ Use /dd inside your deal group.")
        return
    did, deal = deal_by_group(chat.id)
    if not deal:
        await update.message.reply_text("❌ No active deal. Use /initdeal first.")
        return
    if deal["status"] != "SETUP":
        await update.message.reply_text(f"⚠️ Deal in <b>{deal['status']}</b> — cannot edit form.", parse_mode="HTML")
        return
    if not ctx.args or len(ctx.args) < 2:
        await update.message.reply_text(
            "📋 <b>DEAL FORM</b>\n\nFormat: <code>/dd [quantity] [rate] [condition]</code>\n\nExample:\n<code>/dd 500 1.02 Payment within 30 minutes</code>",
            parse_mode="HTML"
        )
        return

    deal["quantity"]  = ctx.args[0]
    deal["rate"]      = ctx.args[1]
    deal["condition"] = " ".join(ctx.args[2:]) if len(ctx.args) > 2 else "None"

    await update.message.reply_text(
        f"✅ <b>Deal Form Saved!</b>\n\n"
        f"💰 Quantity: {deal['quantity']}\n📈 Rate: {deal['rate']}\n📝 Condition: {deal['condition']}\n\n"
        f"➡️ <b>Next step:</b>\n<code>/buyer [wallet_address]</code>\n<code>/seller [wallet_address]</code>",
        parse_mode="HTML"
    )

# ══════════════════════════════════════════════════════════
# STEP 4: /buyer & /seller
# ══════════════════════════════════════════════════════════

async def cmd_buyer(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await set_role(update, ctx, "buyer")

async def cmd_seller(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await set_role(update, ctx, "seller")

async def set_role(update: Update, ctx: ContextTypes.DEFAULT_TYPE, role: str):
    chat = update.effective_chat
    user = update.effective_user
    if chat.type == "private":
        await update.message.reply_text("❌ Use inside your deal group.")
        return
    did, deal = deal_by_group(chat.id)
    if not deal:
        await update.message.reply_text("❌ No active deal here.")
        return
    if deal["status"] != "SETUP":
        await update.message.reply_text(f"⚠️ Cannot change roles. Status: <b>{deal['status']}</b>", parse_mode="HTML")
        return
    if not ctx.args:
        await update.message.reply_text(f"❌ Provide wallet address.\nExample: <code>/{role} YourAddress</code>", parse_mode="HTML")
        return

    deal[f"{role}_id"]       = user.id
    deal[f"{role}_username"] = user.username or user.first_name
    deal[f"{role}_address"]  = ctx.args[0]
    label = "🛒 Buyer" if role == "buyer" else "🏪 Seller"

    b = deal.get("buyer_id") is not None
    s = deal.get("seller_id") is not None
    if b and s:
        deal["status"] = "ROLES_SET"
        next_step = "✅ Both roles set!\n\n➡️ <b>Next step:</b> Use <b>/token</b>"
    elif b:
        next_step = "⏳ Waiting for seller: <code>/seller [address]</code>"
    else:
        next_step = "⏳ Waiting for buyer: <code>/buyer [address]</code>"

    await update.message.reply_text(
        f"✅ <b>{label} Set!</b>\n\n👤 @{deal[f'{role}_username']}\n💳 <code>{ctx.args[0]}</code>\n\n{next_step}",
        parse_mode="HTML"
    )

# ══════════════════════════════════════════════════════════
# STEP 5: /token — select & both confirm
# ══════════════════════════════════════════════════════════

TOKEN_LABELS = {
    "USDT_TRC20": "💵 USDT TRC20",
    "USDT_BEP20": "💵 USDT BEP20",
    "BTC": "₿ BTC",
    "LTC": "Ł LTC"
}

def token_select_kb(did):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💵 USDT TRC20", callback_data=f"token_select:USDT_TRC20:{did}"),
         InlineKeyboardButton("💵 USDT BEP20", callback_data=f"token_select:USDT_BEP20:{did}")],
        [InlineKeyboardButton("₿ BTC", callback_data=f"token_select:BTC:{did}"),
         InlineKeyboardButton("Ł LTC", callback_data=f"token_select:LTC:{did}")]
    ])

async def cmd_token(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if chat.type == "private":
        await update.message.reply_text("❌ Use inside your deal group.")
        return
    did, deal = deal_by_group(chat.id)
    if not deal:
        await update.message.reply_text("❌ No active deal here.")
        return
    if deal.get("funded"):
        await update.message.reply_text("❌ Token locked — payment already made.")
        return
    if deal["status"] not in ("ROLES_SET", "TOKEN_SELECTED"):
        await update.message.reply_text(f"⚠️ Complete previous steps first. Status: <b>{deal['status']}</b>", parse_mode="HTML")
        return
    if not deal.get("buyer_id") or not deal.get("seller_id"):
        await update.message.reply_text("❌ Set buyer and seller roles first.")
        return
    await update.message.reply_text(
        "🪙 <b>SELECT PAYMENT TOKEN</b>\n\nChoose the token for this deal.\n⚠️ <i>Both buyer AND seller must confirm.</i>",
        reply_markup=token_select_kb(did), parse_mode="HTML"
    )

async def handle_token_pick(update: Update, ctx: ContextTypes.DEFAULT_TYPE, d: str):
    q = update.callback_query
    user = q.from_user
    _, token, did = d.split(":")
    deal = deal_by_id(did)
    if not deal:
        await q.edit_message_text("❌ Deal not found.")
        return
    if user.id not in (deal.get("buyer_id"), deal.get("seller_id")):
        await q.answer("❌ Only deal participants can select token.", show_alert=True)
        return
    if deal.get("funded"):
        await q.answer("❌ Token locked after payment.", show_alert=True)
        return

    deal["token"] = token
    deal["token_buyer_confirmed"]  = False
    deal["token_seller_confirmed"] = False

    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Confirm Token", callback_data=f"token_confirm:{did}"),
        InlineKeyboardButton("🔄 Re-select", callback_data=f"token_reselect:{did}")
    ]])
    await q.edit_message_text(
        f"🪙 <b>Token Proposed: {TOKEN_LABELS.get(token, token)}</b>\n\n"
        f"Selected by: @{user.username or user.first_name}\n\n"
        f"⚠️ <b>BOTH buyer and seller must confirm.</b>\n\n"
        f"Press ✅ <b>Confirm Token</b> or 🔄 <b>Re-select</b>",
        reply_markup=kb, parse_mode="HTML"
    )

async def handle_token_confirm(update: Update, ctx: ContextTypes.DEFAULT_TYPE, d: str):
    q = update.callback_query
    user = q.from_user
    _, did = d.split(":", 1)
    deal = deal_by_id(did)
    if not deal:
        await q.answer("❌ Deal not found.", show_alert=True)
        return

    if user.id == deal.get("buyer_id"):      role = "buyer"
    elif user.id == deal.get("seller_id"):   role = "seller"
    else:
        await q.answer("❌ Not a deal participant.", show_alert=True)
        return

    deal[f"token_{role}_confirmed"] = True
    await q.answer(f"✅ {role.capitalize()} confirmed!")

    b_ok  = deal.get("token_buyer_confirmed")
    s_ok  = deal.get("token_seller_confirmed")
    label = TOKEN_LABELS.get(deal["token"], deal["token"])

    if b_ok and s_ok:
        deal["status"] = "TOKEN_SELECTED"
        await q.edit_message_text(
            f"🔒 <b>Token Locked: {label}</b>\n\n✅ Buyer: Confirmed\n✅ Seller: Confirmed\n\n"
            f"➡️ <b>Next step:</b> Seller uses <b>/deposit</b>",
            parse_mode="HTML"
        )
    else:
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Confirm Token", callback_data=f"token_confirm:{did}"),
            InlineKeyboardButton("🔄 Re-select", callback_data=f"token_reselect:{did}")
        ]])
        await q.edit_message_text(
            f"🪙 <b>Token: {label}</b>\n\n"
            f"🛒 Buyer: {'✅ Confirmed' if b_ok else '⏳ Waiting'}\n"
            f"🏪 Seller: {'✅ Confirmed' if s_ok else '⏳ Waiting'}\n\n"
            f"⚠️ Both must confirm before proceeding.",
            reply_markup=kb, parse_mode="HTML"
        )

async def handle_token_reselect(update: Update, ctx: ContextTypes.DEFAULT_TYPE, d: str):
    q = update.callback_query
    user = q.from_user
    _, did = d.split(":", 1)
    deal = deal_by_id(did)
    if not deal:
        await q.answer("❌ Deal not found.", show_alert=True)
        return
    if user.id not in (deal.get("buyer_id"), deal.get("seller_id")):
        await q.answer("❌ Not a deal participant.", show_alert=True)
        return
    deal["token"] = None
    deal["token_buyer_confirmed"]  = False
    deal["token_seller_confirmed"] = False
    await q.edit_message_text("🪙 <b>Re-select Payment Token</b>\n\nChoose:", reply_markup=token_select_kb(did), parse_mode="HTML")

# ══════════════════════════════════════════════════════════
# STEP 6: /deposit
# ══════════════════════════════════════════════════════════

async def cmd_deposit(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if chat.type == "private":
        await update.message.reply_text("❌ Use inside your deal group.")
        return
    did, deal = deal_by_group(chat.id)
    if not deal:
        await update.message.reply_text("❌ No active deal here.")
        return
    if deal["status"] not in ("TOKEN_SELECTED", "AWAITING_DEPOSIT"):
        await update.message.reply_text("❌ Select and confirm token first using <b>/token</b>", parse_mode="HTML")
        return

    if not state.oxapay_key:
        demo_addr = f"DEMO_{did[:8]}"
        deal["deposit_address"] = demo_addr
        deal["status"] = "AWAITING_DEPOSIT"
        await send_qr(ctx, chat.id, demo_addr,
            f"🔧 <b>DEMO DEPOSIT ADDRESS</b>\n\n🪙 Token: {deal.get('token')}\n"
            f"📬 Address:\n<code>{demo_addr}</code>\n💰 Amount: {deal.get('quantity')}\n\n"
            f"⚠️ DEMO mode — no real payment needed.\n\n➡️ <b>Next step:</b> Use <b>/verify</b>"
        )
        return

    await update.message.reply_text("⏳ Generating deposit address via OxaPay…")
    token_map = {
        "USDT_TRC20": ("USDT", "TRX"), "USDT_BEP20": ("USDT", "BSC"),
        "BTC": ("BTC", "BTC"), "LTC": ("LTC", "LTC")
    }
    currency, network = token_map.get(deal["token"], ("USDT", "TRX"))
    try:
        loop = asyncio.get_event_loop()
        def _oxapay_req():
            req = urllib.request.Request(
                "https://api.oxapay.com/merchants/request",
                data=_json.dumps({"merchant": state.oxapay_key, "amount": float(deal.get("quantity", 1)),
                                  "currency": currency, "network": network,
                                  "description": f"Escrow {did}", "lifeTime": 60}).encode(),
                headers={"Content-Type": "application/json"}
            )
            with urllib.request.urlopen(req, timeout=15) as r:
                return _json.loads(r.read().decode())
        data = await loop.run_in_executor(None, _oxapay_req)
        if data.get("result") != 100:
            raise Exception(data.get("message", "Unknown error"))
        address  = data.get("payAddress", "N/A")
        track_id = data.get("trackId", "")
        deal["deposit_address"]   = address
        deal["oxapay_track_id"]   = track_id
        deal["status"] = "AWAITING_DEPOSIT"
        await send_qr(ctx, chat.id, address,
            f"✅ <b>DEPOSIT ADDRESS READY</b>\n\n"
            f"🪙 Token: {deal['token']}\n"
            f"📬 Address:\n<code>{address}</code>\n"
            f"💰 Amount: {deal.get('quantity')}\n\n"
            f"⚠️ <b>SELLER:</b> Send EXACT amount to this address.\n\n"
            f"➡️ After sending, use <b>/verify</b> to confirm OxaPay payment."
        )
    except Exception as e:
        fallback = f"DEMO_{did[:8]}"
        deal["deposit_address"] = fallback
        deal["status"] = "AWAITING_DEPOSIT"
        await update.message.reply_text(
            f"❌ OxaPay Error: {e}\n\n🔧 Fallback: <code>{fallback}</code>\n\n➡️ Use <b>/verify</b>",
            parse_mode="HTML"
        )

# ══════════════════════════════════════════════════════════
# STEP 7: /verify — confirm OxaPay payment received
# ══════════════════════════════════════════════════════════

async def cmd_verify(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    user = update.effective_user
    if chat.type == "private":
        await update.message.reply_text("❌ Use inside your deal group.")
        return
    did, deal = deal_by_group(chat.id)
    if not deal:
        await update.message.reply_text("❌ No active deal here.")
        return
    if deal.get("funded"):
        await update.message.reply_text("⚠️ Deal already verified and funded.")
        return
    if deal["status"] != "AWAITING_DEPOSIT":
        await update.message.reply_text("❌ Use /deposit first to generate an address.", parse_mode="HTML")
        return

    # DEMO mode — no OxaPay key set, skip verification
    if not state.oxapay_key or deal.get("deposit_address", "").startswith("DEMO_"):
        deal["funded"]    = True
        deal["status"]    = "FUNDED"
        deal["funded_by"] = user.username or user.first_name
        deal["funded_at"] = datetime.utcnow().isoformat()
        await update.message.reply_text(
            f"✅ <b>[DEMO] Payment Marked as Funded</b>\n\n"
            f"🆔 <code>{did}</code>\n🪙 {deal.get('token')}\n💰 {deal.get('quantity')}\n\n"
            f"📌 <b>Buyer:</b> Now send the agreed fiat/payment to the seller off-platform.\n"
            f"Once done, either party runs <b>/release</b> to proceed to confirmation.",
            parse_mode="HTML"
        )
        await log(ctx,
            f"💰 <b>DEAL FUNDED (DEMO)</b>\n\n🆔 <code>{did}</code>\n"
            f"🪙 {deal.get('token')}  💵 {deal.get('quantity')}\n"
            f"👤 @{deal['funded_by']}\n⏰ {deal['funded_at']}\n📊 FUNDED"
        )
        return

    # Live mode — query OxaPay inquiry API using trackId
    track_id = deal.get("oxapay_track_id")
    if not track_id:
        await update.message.reply_text(
            "❌ No OxaPay tracking ID found.\n\nRun <b>/deposit</b> again to get a new address.",
            parse_mode="HTML"
        )
        return

    await update.message.reply_text("⏳ Checking OxaPay payment status…")
    try:
        loop = asyncio.get_event_loop()
        def _oxapay_inquiry():
            req = urllib.request.Request(
                "https://api.oxapay.com/merchants/inquiry",
                data=_json.dumps({"merchant": state.oxapay_key, "trackId": track_id}).encode(),
                headers={"Content-Type": "application/json"}
            )
            with urllib.request.urlopen(req, timeout=15) as r:
                return _json.loads(r.read().decode())
        data = await loop.run_in_executor(None, _oxapay_inquiry)

        if data.get("result") != 100:
            await update.message.reply_text(
                f"⚠️ OxaPay error: {data.get('message', 'Unknown error')}\n\nTry again shortly.",
                parse_mode="HTML"
            )
            return

        pay_status = data.get("status", "").lower()  # e.g. "paid", "waiting", "expired"

        if pay_status != "paid":
            status_labels = {
                "waiting": "⏳ Waiting — payment not received yet",
                "expired": "⌛ Expired — address has expired, run /deposit again",
                "failed":  "❌ Failed",
            }
            label = status_labels.get(pay_status, f"⚠️ Status: {pay_status}")
            await update.message.reply_text(
                f"🔍 <b>Payment Status: {label}</b>\n\n"
                f"📬 Address: <code>{deal.get('deposit_address')}</code>\n"
                f"💰 Expected: {deal.get('quantity')} {deal.get('token')}\n\n"
                f"Please wait for the blockchain to confirm, then try <b>/verify</b> again.",
                parse_mode="HTML"
            )
            return

        # Payment confirmed ✅
        deal["funded"]    = True
        deal["status"]    = "FUNDED"
        deal["funded_by"] = user.username or user.first_name
        deal["funded_at"] = datetime.utcnow().isoformat()

        await update.message.reply_text(
            f"✅ <b>OxaPay Payment Confirmed!</b>\n\n"
            f"🆔 Trade ID: <code>{did}</code>\n"
            f"🪙 Token: {deal.get('token')}\n"
            f"💰 Amount: {deal.get('quantity')}\n\n"
            f"────────────────────\n"
            f"📌 <b>Buyer:</b> Now send the agreed fiat/payment to the seller off-platform.\n\n"
            f"Once buyer has paid, either party runs <b>/release</b> to start final confirmation.",
            parse_mode="HTML"
        )
        await log(ctx,
            f"💰 <b>DEAL FUNDED</b>\n\n🆔 <code>{did}</code>\n"
            f"🪙 {deal.get('token')}  💵 {deal.get('quantity')}\n"
            f"👤 @{deal['funded_by']}\n⏰ {deal['funded_at']}\n📊 FUNDED"
        )

    except Exception as e:
        await update.message.reply_text(f"❌ OxaPay check failed: {e}\n\nTry again.", parse_mode="HTML")

# ══════════════════════════════════════════════════════════
# STEP 8: /release — buyer or seller triggers confirmation stage
# ══════════════════════════════════════════════════════════

async def cmd_release(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    user = update.effective_user
    if chat.type == "private":
        await update.message.reply_text("❌ Use inside your deal group.")
        return
    did, deal = deal_by_group(chat.id)
    if not deal:
        await update.message.reply_text("❌ No active deal here.")
        return
    if not deal.get("funded"):
        await update.message.reply_text("❌ Payment not verified yet. Use /verify first.")
        return
    if deal["status"] == "COMPLETED":
        await update.message.reply_text("⚠️ Deal is already completed.")
        return
    if deal["status"] == "CANCELLED":
        await update.message.reply_text("⚠️ Deal has been cancelled.")
        return
    if deal["status"] == "DISPUTED":
        await update.message.reply_text("⚠️ Deal is under dispute. Wait for admin resolution.")
        return
    if deal["status"] == "AWAITING_CONFIRMATION":
        await update.message.reply_text("⚠️ Confirmation already started. Both parties must press Confirm above.")
        return
    if deal["status"] != "FUNDED":
        await update.message.reply_text(f"⚠️ Cannot release at this stage. Status: <b>{deal['status']}</b>", parse_mode="HTML")
        return

    # Only buyer or seller can trigger release
    if user.id not in (deal.get("buyer_id"), deal.get("seller_id")):
        await update.message.reply_text("❌ Only the buyer or seller can run /release.")
        return

    who = "Buyer" if user.id == deal.get("buyer_id") else "Seller"

    # Reset confirmations fresh for this release round
    deal["buyer_confirmed"]  = False
    deal["seller_confirmed"] = False
    deal["status"]           = "AWAITING_CONFIRMATION"
    deal["release_by"]       = user.username or user.first_name
    deal["release_at"]       = datetime.utcnow().isoformat()

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Buyer Confirm", callback_data=f"confirm:buyer:{did}"),
         InlineKeyboardButton("✅ Seller Confirm", callback_data=f"confirm:seller:{did}")],
        [InlineKeyboardButton("🚨 Dispute / Call Admin", callback_data=f"dispute_call:{did}")]
    ])
    await update.message.reply_text(
        f"🔓 <b>RELEASE INITIATED by {who}</b>\n\n"
        f"🆔 Trade ID: <code>{did}</code>\n"
        f"🪙 Token: {deal.get('token')}  💰 Amount: {deal.get('quantity')}\n\n"
        f"────────────────────\n"
        f"<b>Both buyer and seller must press Confirm below.</b>\n"
        f"Funds release to buyer's address once both confirm.\n\n"
        f"🛒 Buyer:  <code>{deal.get('buyer_address', 'N/A')}</code>\n\n"
        f"⚠️ If there is any issue, press 🚨 Dispute to call admin.",
        reply_markup=kb, parse_mode="HTML"
    )
    await log(ctx,
        f"🔓 <b>RELEASE INITIATED</b>\n\n🆔 <code>{did}</code>\n"
        f"👤 By: @{deal['release_by']} ({who})\n⏰ {deal['release_at']}\n📊 AWAITING_CONFIRMATION"
    )

# ══════════════════════════════════════════════════════════
# STEP 9: CONFIRMATION
# ══════════════════════════════════════════════════════════

async def handle_confirmation(update: Update, ctx: ContextTypes.DEFAULT_TYPE, d: str):
    q = update.callback_query
    user = q.from_user
    _, role, did = d.split(":")
    deal = deal_by_id(did)
    if not deal:
        await q.answer("❌ Deal not found.", show_alert=True)
        return
    if deal["status"] != "AWAITING_CONFIRMATION":
        await q.answer("❌ Release not started yet. Run /release first.", show_alert=True)
        return
    if deal.get("status") == "COMPLETED":
        await q.answer("✅ Deal already completed.", show_alert=True)
        return

    if role == "buyer":
        if user.id != deal.get("buyer_id"):
            await q.answer("❌ You are not the buyer.", show_alert=True)
            return
        if deal.get("buyer_confirmed"):
            await q.answer("✅ Already confirmed.", show_alert=True)
            return
        deal["buyer_confirmed"] = True
    elif role == "seller":
        if user.id != deal.get("seller_id"):
            await q.answer("❌ You are not the seller.", show_alert=True)
            return
        if deal.get("seller_confirmed"):
            await q.answer("✅ Already confirmed.", show_alert=True)
            return
        deal["seller_confirmed"] = True

    await q.answer(f"✅ {role.capitalize()} confirmed!")
    b = deal["buyer_confirmed"]
    s = deal["seller_confirmed"]

    if b and s:
        await q.edit_message_text(
            "🎉 <b>BOTH CONFIRMED!</b>\n\n✅ Buyer\n✅ Seller\n\n⏳ Processing release to buyer's address…",
            parse_mode="HTML"
        )
        await release_deal(ctx, did, deal, q.message.chat_id)
    else:
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton(f"{'✅' if b else '⏳'} Buyer Confirm", callback_data=f"confirm:buyer:{did}"),
             InlineKeyboardButton(f"{'✅' if s else '⏳'} Seller Confirm", callback_data=f"confirm:seller:{did}")],
            [InlineKeyboardButton("🚨 Dispute / Call Admin", callback_data=f"dispute_call:{did}")]
        ])
        await q.edit_message_text(
            f"📊 <b>CONFIRMATION STATUS</b>\n\n"
            f"🛒 Buyer:  {'✅ Confirmed' if b else '⏳ Waiting'}\n"
            f"🏪 Seller: {'✅ Confirmed' if s else '⏳ Waiting'}\n\n"
            f"⚠️ Both must confirm for release to buyer's address.",
            reply_markup=kb, parse_mode="HTML"
        )

# ══════════════════════════════════════════════════════════
# STEP 10: RELEASE — funds go to buyer's address
# ══════════════════════════════════════════════════════════

async def release_deal(ctx, did, deal, group_id):
    apply_fee = True
    if state.required_bio:
        try:
            buyer_chat = await ctx.bot.get_chat(deal.get("buyer_id"))
            bio = getattr(buyer_chat, "bio", "") or ""
            if state.required_bio.lower() in bio.lower():
                apply_fee = False
        except Exception:
            pass

    qty      = float(deal.get("quantity", 0))
    fee_amt  = qty * (state.fee_percent / 100) if apply_fee else 0.0
    final    = qty - fee_amt
    buyer_addr = deal.get("buyer_address", "N/A")

    deal["status"]       = "COMPLETED"
    deal["final_amount"] = final
    deal["fee_deducted"] = fee_amt
    deal["completed_at"] = datetime.utcnow().isoformat()

    try:
        await ctx.bot.send_message(
            chat_id=group_id,
            text=(
                f"🎉 <b>DEAL COMPLETED!</b>\n\n"
                f"🆔 Trade ID: <code>{did}</code>\n"
                f"🪙 Token: {deal.get('token')}\n"
                f"💰 Original: {qty}\n"
                f"💸 Fee ({state.fee_percent}%): {fee_amt:.4f}\n"
                f"✅ Released Amount: {final:.4f}\n\n"
                f"📬 <b>Release Address (Buyer):</b>\n<code>{buyer_addr}</code>\n\n"
                f"🛒 Buyer: @{deal.get('buyer_username')}\n"
                f"🏪 Seller: @{deal.get('seller_username')}\n\n"
                f"📊 Status: <b>COMPLETED</b>\n"
                f"⏰ {deal['completed_at']}\n\n"
                f"Thank you for using P2P Escrow! 🙏"
            ),
            parse_mode="HTML"
        )
    except Exception:
        pass

    await log(ctx,
        f"✅ <b>DEAL COMPLETED</b>\n\n🆔 <code>{did}</code>\n"
        f"🛒 @{deal.get('buyer_username')}  🏪 @{deal.get('seller_username')}\n"
        f"🪙 {deal.get('token')}  💰 {qty}  💸 Fee: {fee_amt:.4f}  ✅ Final: {final:.4f}\n"
        f"📬 Buyer Address: <code>{buyer_addr}</code>\n"
        f"📦 <code>{group_id}</code>\n📊 COMPLETED\n⏰ {deal['completed_at']}"
    )

    for p in ("buyer", "seller"):
        pid = deal.get(f"{p}_id")
        if pid:
            try:
                msg = (
                    f"✅ <b>Deal Completed: {did}</b>\n\n"
                    f"Final: <b>{final:.4f} {deal.get('token')}</b>\n"
                    f"📬 Released to buyer: <code>{buyer_addr}</code>\n\n"
                    f"Group closes shortly."
                )
                await ctx.bot.send_message(chat_id=pid, text=msg, parse_mode="HTML")
            except Exception:
                pass

    await asyncio.sleep(10)
    try:
        await ctx.bot.send_message(chat_id=group_id, text="🗑 <b>Group closing in 10 seconds. Thank you!</b>", parse_mode="HTML")
        await asyncio.sleep(10)
        await ctx.bot.leave_chat(group_id)
        if state.telethon_client:
            from telethon.tl.functions.channels import DeleteChannelRequest
            try:
                entity = await state.telethon_client.get_entity(group_id)
                await state.telethon_client(DeleteChannelRequest(entity))
            except Exception:
                pass
    except Exception:
        pass

# ══════════════════════════════════════════════════════════
# STEP 11: /dispute
# ══════════════════════════════════════════════════════════

async def cmd_dispute(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    user = update.effective_user
    if chat.type == "private":
        await update.message.reply_text("❌ Use inside your deal group.")
        return
    did, deal = deal_by_group(chat.id)
    if not deal:
        await update.message.reply_text("❌ No active deal here.")
        return
    if deal.get("status") == "COMPLETED":
        await update.message.reply_text("❌ Cannot dispute a completed deal.")
        return
    if deal.get("status") == "DISPUTED":
        await update.message.reply_text("⚠️ Dispute already open. Admin will assist shortly.")
        return

    reason = " ".join(ctx.args) if ctx.args else "No reason provided"
    deal["status"]         = "DISPUTED"
    deal["dispute_by"]     = user.username or user.first_name
    deal["dispute_reason"] = reason
    deal["dispute_at"]     = datetime.utcnow().isoformat()

    await update.message.reply_text(
        f"🚨 <b>DISPUTE TRIGGERED!</b>\n\n"
        f"👤 By: @{deal['dispute_by']}\n📝 Reason: {reason}\n\n"
        f"⏳ An admin has been notified and will join shortly.",
        parse_mode="HTML"
    )

    group_link = f"https://t.me/c/{str(chat.id).replace('-100','')}/1"
    await alert_admins(ctx,
        f"🚨 <b>DISPUTE ALERT!</b>\n\n"
        f"🆔 <code>{did}</code>\n"
        f"🛒 @{deal.get('buyer_username','N/A')}  🏪 @{deal.get('seller_username','N/A')}\n"
        f"⚠️ By: @{deal['dispute_by']}\n📝 {reason}\n"
        f"🔗 {group_link}\n⏰ {deal['dispute_at']}",
        deal_id=did
    )
    await log(ctx,
        f"⚠️ <b>DISPUTE OPENED</b>\n\n🆔 <code>{did}</code>\n"
        f"🛒 @{deal.get('buyer_username')}  🏪 @{deal.get('seller_username')}\n"
        f"⚠️ By: @{deal['dispute_by']}\n📝 {reason}\n"
        f"📦 <code>{chat.id}</code>\n📊 DISPUTED\n⏰ {deal['dispute_at']}"
    )

async def handle_dispute_call(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Dispute button pressed inline from deal group (callback_data = dispute_call:{did})."""
    q = update.callback_query
    parts = q.data.split(":", 1)
    did = parts[1] if len(parts) > 1 else None
    chat_id = q.message.chat_id

    if did:
        deal = deal_by_id(did)
    else:
        did, deal = deal_by_group(chat_id)

    if not deal:
        await q.answer("❌ No active deal.", show_alert=True)
        return
    if deal.get("status") == "DISPUTED":
        await q.answer("⚠️ Dispute already open. Admin is on the way.", show_alert=True)
        return
    if deal.get("status") == "COMPLETED":
        await q.answer("⚠️ Deal already completed.", show_alert=True)
        return

    user = q.from_user
    deal["status"]         = "DISPUTED"
    deal["dispute_by"]     = user.username or user.first_name
    deal["dispute_reason"] = "Triggered via inline button"
    deal["dispute_at"]     = datetime.utcnow().isoformat()

    await q.edit_message_text(
        f"🚨 <b>DISPUTE TRIGGERED!</b>\n\n"
        f"👤 By: @{user.username or user.first_name}\n\n"
        f"Admin has been notified and will join shortly. Please remain in the group.",
        parse_mode="HTML"
    )

    group_link = f"https://t.me/c/{str(chat_id).replace('-100','')}/1"
    await alert_admins(ctx,
        f"🚨 <b>DISPUTE ALERT!</b>\n\n"
        f"🆔 <code>{did}</code>\n"
        f"🛒 @{deal.get('buyer_username','N/A')}  🏪 @{deal.get('seller_username','N/A')}\n"
        f"⚠️ By: @{deal['dispute_by']}\n🔗 {group_link}",
        deal_id=did
    )
    await log(ctx, f"⚠️ <b>DISPUTE OPENED</b>\n\n🆔 <code>{did}</code>\n📊 DISPUTED\n⏰ {deal['dispute_at']}")

async def handle_dispute_admin(update: Update, ctx: ContextTypes.DEFAULT_TYPE, d: str):
    """Admin clicks '➕ Add Me to Group' — generates single-use invite link for admin."""
    q = update.callback_query
    user = q.from_user
    did = d.split(":", 1)[1]
    if not is_admin(user.id):
        await q.answer("❌ Not authorized.", show_alert=True)
        return
    deal = deal_by_id(did)
    if not deal:
        await q.answer("❌ Deal not found.", show_alert=True)
        return
    if did in state.dispute_admins and state.dispute_admins[did] != user.id:
        await q.answer("❌ Another admin is already handling this dispute.", show_alert=True)
        return

    state.dispute_admins[did] = user.id
    deal["dispute_admin"] = user.username or str(user.id)

    # Generate a single-use invite link to the deal group
    group_id = deal.get("group_id")
    invite_link = None
    try:
        link_obj = await ctx.bot.create_chat_invite_link(
            chat_id=group_id,
            member_limit=1,
            name=f"Admin {user.username or user.id}"
        )
        invite_link = link_obj.invite_link
    except Exception as e:
        logger.warning(f"Could not generate invite for admin: {e}")

    link_line = f"🔗 <b>Join link (1-use):</b> {invite_link}" if invite_link else \
                f"⚠️ Could not auto-generate link. Group ID: <code>{group_id}</code>"

    await q.edit_message_text(
        f"✅ <b>You are handling dispute</b> — <code>{did}</code>\n"
        f"🛒 @{deal.get('buyer_username')}  🏪 @{deal.get('seller_username')}\n\n"
        f"{link_line}\n\n"
        f"<b>Commands once inside:</b>\n"
        f"<code>/releaseto buyer {did}</code>\n"
        f"<code>/releaseto seller {did}</code>\n"
        f"<code>/canceldeal {did}</code>",
        parse_mode="HTML"
    )

    try:
        await ctx.bot.send_message(
            chat_id=group_id,
            text=f"👨‍💼 <b>Admin @{user.username or 'Admin'} is joining to handle the dispute.</b>\n"
                 f"Please remain in the group.",
            parse_mode="HTML"
        )
    except Exception:
        pass
# ══════════════════════════════════════════════════════════
# ADMIN COMMANDS
# ══════════════════════════════════════════════════════════

async def cmd_setloggroup(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    user = update.effective_user
    if not is_main_admin(user.id):
        return
    if chat.type == "private":
        await update.message.reply_text("❌ Run this inside the group you want as LOG GROUP.", parse_mode="HTML")
        return
    state.log_group_id = chat.id
    await update.message.reply_text(f"✅ <b>LOG GROUP SET!</b>\n\n📋 {chat.title}\n🆔 <code>{chat.id}</code>\n\nBot ready for deals!", parse_mode="HTML")

async def cmd_addadmin(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_main_admin(update.effective_user.id):
        return
    if not ctx.args:
        await update.message.reply_text("Usage: <code>/addadmin {user_id}</code>", parse_mode="HTML")
        return
    try:
        uid = int(ctx.args[0])
        if uid in state.sub_admins:
            await update.message.reply_text(f"⚠️ Already sub admin: <code>{uid}</code>", parse_mode="HTML")
            return
        state.sub_admins.add(uid)
        await update.message.reply_text(f"✅ Sub Admin Added: <code>{uid}</code>", parse_mode="HTML")
        try:
            await ctx.bot.send_message(chat_id=uid, text="👨‍💼 <b>You've been added as Sub Admin!</b>", parse_mode="HTML")
        except Exception:
            pass
    except ValueError:
        await update.message.reply_text("❌ Invalid ID.")

async def cmd_removeadmin(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_main_admin(update.effective_user.id):
        return
    if not ctx.args:
        await update.message.reply_text("Usage: <code>/removeadmin {user_id}</code>", parse_mode="HTML")
        return
    try:
        uid = int(ctx.args[0])
        state.sub_admins.discard(uid)
        await update.message.reply_text(f"✅ Removed <code>{uid}</code>", parse_mode="HTML")
    except ValueError:
        await update.message.reply_text("❌ Invalid ID.")

async def cmd_setfee(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_main_admin(update.effective_user.id):
        return
    if not ctx.args:
        await update.message.reply_text(f"Usage: <code>/setfee {{percent}}</code>\nCurrent: <b>{state.fee_percent}%</b>", parse_mode="HTML")
        return
    try:
        fee = float(ctx.args[0])
        if not (0 <= fee <= 50):
            await update.message.reply_text("❌ Fee must be 0–50%.")
            return
        old = state.fee_percent
        state.fee_percent = fee
        await update.message.reply_text(f"✅ Fee: <s>{old}%</s> → <b>{fee}%</b>", parse_mode="HTML")
    except ValueError:
        await update.message.reply_text("❌ Invalid number.")

async def cmd_setbio(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_main_admin(update.effective_user.id):
        return
    if not ctx.args:
        await update.message.reply_text(f"Usage: <code>/setbio {{tag}}</code>\nCurrent: <b>{state.required_bio or 'Not set'}</b>", parse_mode="HTML")
        return
    state.required_bio = ctx.args[0]
    await update.message.reply_text(f"✅ Bio tag: <b>{state.required_bio}</b>\nUsers with this in bio → 0% fee.", parse_mode="HTML")

async def cmd_setoxapay(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_main_admin(update.effective_user.id):
        return
    if not ctx.args:
        await update.message.reply_text("Usage: <code>/setoxapay {api_key}</code>", parse_mode="HTML")
        return
    state.oxapay_key = ctx.args[0]
    key = state.oxapay_key
    masked = f"{key[:4]}{'*'*(len(key)-8)}{key[-4:]}" if len(key) > 8 else "****"
    await update.message.reply_text(f"✅ <b>OxaPay Key Set!</b>\n🔑 <code>{masked}</code>\n\nUse /checkoxapay to verify.", parse_mode="HTML")

async def cmd_checkoxapay(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_main_admin(update.effective_user.id):
        return
    if not state.oxapay_key:
        await update.message.reply_text("❌ OxaPay key not set.")
        return
    await update.message.reply_text("⏳ Checking OxaPay…")
    try:
        loop = asyncio.get_event_loop()
        def _check_ox_cmd():
            req = urllib.request.Request(
                "https://api.oxapay.com/merchants/balance",
                data=_json.dumps({"merchant": state.oxapay_key}).encode(),
                headers={"Content-Type": "application/json"}
            )
            with urllib.request.urlopen(req, timeout=15) as r:
                return _json.loads(r.read().decode())
        data = await loop.run_in_executor(None, _check_ox_cmd)
        if data.get("result") == 100:
            bal = data.get("balance", {})
            bal_txt = "\n".join(f"  • {k}: {v}" for k, v in bal.items()) if bal else "N/A"
            await update.message.reply_text(f"✅ <b>OxaPay Connected!</b>\n\n💰 Balances:\n{bal_txt}", parse_mode="HTML")
        else:
            await update.message.reply_text(f"⚠️ Error: {data.get('message')}")
    except Exception as e:
        await update.message.reply_text(f"❌ Failed: {e}")

async def cmd_resetoxapay(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_main_admin(update.effective_user.id):
        return
    state.oxapay_key = None
    await update.message.reply_text("✅ OxaPay key removed. Bot is in DEMO mode.")

async def cmd_releaseto(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    if len(ctx.args) < 2:
        await update.message.reply_text("Usage: <code>/releaseto buyer|seller DEAL_ID</code>", parse_mode="HTML")
        return
    party = ctx.args[0].lower()
    did   = ctx.args[1].upper()
    if party not in ("buyer", "seller"):
        await update.message.reply_text("❌ Must be buyer or seller.")
        return
    deal = deal_by_id(did)
    if not deal:
        await update.message.reply_text(f"❌ Not found: <code>{did}</code>", parse_mode="HTML")
        return
    if deal.get("status") == "COMPLETED":
        await update.message.reply_text("⚠️ Already completed.")
        return

    user = update.effective_user
    if deal.get("status") == "DISPUTED":
        assigned = state.dispute_admins.get(did)
        if assigned and assigned != user.id and not is_main_admin(user.id):
            await update.message.reply_text("❌ Another admin is handling this dispute.")
            return

    qty     = float(deal.get("quantity", 0))
    fee_amt = qty * (state.fee_percent / 100)
    final   = qty - fee_amt
    to_user = deal.get(f"{party}_username", "N/A")
    to_addr = deal.get(f"{party}_address", "N/A")

    deal["status"]            = "COMPLETED"
    deal["force_released_to"] = party
    deal["fee_deducted"]      = fee_amt
    deal["final_amount"]      = final
    deal["completed_at"]      = datetime.utcnow().isoformat()

    try:
        await ctx.bot.send_message(
            chat_id=deal["group_id"],
            text=(
                f"⚖️ <b>ADMIN DECISION — DEAL RESOLVED</b>\n\n"
                f"👨‍💼 Admin: @{user.username}\n⚖️ Released to: <b>{party.upper()}</b>\n\n"
                f"🆔 <code>{did}</code>\n🪙 {deal.get('token')}\n"
                f"💰 Original: {qty}\n💸 Fee: {fee_amt:.4f}\n✅ Released: {final:.4f}\n"
                f"👤 To: @{to_user}\n📬 <code>{to_addr}</code>\n\n"
                f"📊 COMPLETED — Group closes shortly."
            ),
            parse_mode="HTML"
        )
    except Exception:
        pass

    await update.message.reply_text(f"✅ Force Released to <b>{party.upper()}</b> (@{to_user}) — {final:.4f}", parse_mode="HTML")
    await log(ctx,
        f"⚖️ <b>ADMIN FORCE RELEASE</b>\n\n🆔 <code>{did}</code>\n⚖️ {party.upper()} (@{to_user})\n"
        f"🪙 {deal.get('token')}  💰 {qty}  💸 {fee_amt:.4f}  ✅ {final:.4f}\n"
        f"👨‍💼 @{user.username}\n📊 COMPLETED (Force)\n⏰ {deal['completed_at']}"
    )
    await asyncio.sleep(15)
    try:
        await ctx.bot.send_message(chat_id=deal["group_id"], text="🗑 <b>Group closed.</b>", parse_mode="HTML")
        await ctx.bot.leave_chat(deal["group_id"])
        if state.telethon_client:
            from telethon.tl.functions.channels import DeleteChannelRequest
            try:
                entity = await state.telethon_client.get_entity(deal["group_id"])
                await state.telethon_client(DeleteChannelRequest(entity))
            except Exception:
                pass
    except Exception:
        pass

async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    all_d = list(state.deals.values())
    total = len(all_d)
    done  = sum(1 for x in all_d if x["status"] == "COMPLETED")
    dis   = sum(1 for x in all_d if x["status"] == "DISPUTED")
    fund  = sum(1 for x in all_d if x["status"] == "FUNDED")
    ox = f"✅ {state.oxapay_key[:4]}...{state.oxapay_key[-4:]}" if state.oxapay_key else "❌ Not Set (Demo)"
    lg = f"✅ <code>{state.log_group_id}</code>" if state.log_group_id else "❌ Not Set"
    tc = "✅ Connected" if state.telethon_client else "❌ Not Connected"
    await update.message.reply_text(
        f"📊 <b>BOT STATUS</b>\n\n📋 Log Group: {lg}\n🔑 OxaPay: {ox}\n📡 Telethon: {tc}\n"
        f"💸 Fee: <b>{state.fee_percent}%</b>\n🏷 Bio: <b>{state.required_bio or 'Not Set'}</b>\n"
        f"👥 Sub Admins: <b>{len(state.sub_admins)}</b>\n\n"
        f"📦 Total: {total}  🟢 Active: {total-done}  ✅ Done: {done}\n"
        f"💰 Funded: {fund}  🚨 Disputed: {dis}\n\n"
        f"🤖 Mode: {'LIVE' if state.oxapay_key else 'DEMO'}",
        parse_mode="HTML"
    )

async def cmd_dealinfo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat = update.effective_chat
    if not ctx.args:
        await update.message.reply_text("Usage: <code>/dealinfo {TRADE_ID}</code>", parse_mode="HTML")
        return
    did  = ctx.args[0].upper()
    deal = deal_by_id(did)
    if not deal:
        await update.message.reply_text(f"❌ Not found: <code>{did}</code>", parse_mode="HTML")
        return
    is_part = user.id in (deal.get("buyer_id"), deal.get("seller_id"))
    in_grp  = state.group_to_deal.get(chat.id) == did
    if not is_admin(user.id) and not is_part and not in_grp:
        await update.message.reply_text("❌ Not authorized.")
        return
    b = "✅" if deal.get("buyer_confirmed") else "⏳"
    s = "✅" if deal.get("seller_confirmed") else "⏳"
    await update.message.reply_text(
        f"📋 <b>DEAL INFO</b>\n\n🆔 <code>{did}</code>  📊 <b>{deal.get('status')}</b>\n\n"
        f"🛒 @{deal.get('buyer_username','Not Set')}  <code>{deal.get('buyer_address','N/A')}</code>\n"
        f"🏪 @{deal.get('seller_username','Not Set')}  <code>{deal.get('seller_address','N/A')}</code>\n\n"
        f"💰 {deal.get('quantity','N/A')}  📈 {deal.get('rate','N/A')}\n"
        f"📝 {deal.get('condition','None')}\n🪙 {deal.get('token','Not Selected')}\n"
        f"📬 <code>{deal.get('deposit_address','Not Generated')}</code>\n\n"
        f"{b} Buyer  |  {s} Seller\n"
        f"⏰ {deal.get('created_at','N/A')[:19].replace('T',' ')} UTC",
        parse_mode="HTML"
    )

async def cmd_listadmins(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_main_admin(update.effective_user.id):
        return
    txt = f"👑 Main: <code>{MAIN_ADMIN_ID}</code>\n\n"
    txt += ("👨‍💼 Sub Admins:\n" + "".join(f"{i}. <code>{a}</code>\n" for i, a in enumerate(state.sub_admins, 1))) if state.sub_admins else "👨‍💼 Sub Admins: None"
    await update.message.reply_text(f"📋 <b>ADMIN LIST</b>\n\n{txt}", parse_mode="HTML")

async def cmd_canceldeal(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_main_admin(update.effective_user.id):
        return
    if not ctx.args:
        await update.message.reply_text("Usage: <code>/canceldeal {TRADE_ID}</code>", parse_mode="HTML")
        return
    did  = ctx.args[0].upper()
    deal = deal_by_id(did)
    if not deal:
        await update.message.reply_text(f"❌ Not found: <code>{did}</code>", parse_mode="HTML")
        return
    if deal.get("status") == "COMPLETED":
        await update.message.reply_text("⚠️ Cannot cancel completed deal.")
        return
    old = deal["status"]
    user = update.effective_user
    deal["status"]       = "CANCELLED"
    deal["cancelled_by"] = user.username
    deal["cancelled_at"] = datetime.utcnow().isoformat()
    try:
        await ctx.bot.send_message(chat_id=deal["group_id"], text=f"🚫 <b>DEAL CANCELLED BY ADMIN</b>\n\n🆔 <code>{did}</code>\nNo funds transferred.", parse_mode="HTML")
    except Exception:
        pass
    await update.message.reply_text(f"✅ Deal <code>{did}</code> cancelled. Was: {old}", parse_mode="HTML")
    await log(ctx, f"🚫 <b>DEAL CANCELLED</b>\n\n🆔 <code>{did}</code>\n👨‍💼 @{user.username}\n📊 Was: {old}\n⏰ {deal['cancelled_at']}")

# ══════════════════════════════════════════════════════════
# ADMIN INPUT HANDLER — captures text after button press
# ══════════════════════════════════════════════════════════

async def admin_input_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Handles text input from admin after pressing a Set button."""
    user = update.effective_user
    if not is_main_admin(user.id):
        return
    if update.effective_chat.type != "private":
        return

    field = _admin_waiting.get(user.id)
    if not field:
        return  # not waiting for input, ignore

    value = update.message.text.strip()
    _admin_waiting.pop(user.id, None)

    kb = admin_panel_kb()

    if field == "addadmin":
        try:
            uid = int(value)
            if uid == MAIN_ADMIN_ID:
                await update.message.reply_text("⚠️ That's the main admin already.", reply_markup=kb)
                return
            state.sub_admins.add(uid)
            await update.message.reply_text(f"✅ <b>Sub Admin Added!</b>\nID: <code>{uid}</code>\nTotal: {len(state.sub_admins)}", parse_mode="HTML", reply_markup=kb)
            try:
                await ctx.bot.send_message(chat_id=uid, text="👨‍💼 <b>You have been added as Sub Admin!</b>", parse_mode="HTML")
            except Exception:
                pass
        except ValueError:
            await update.message.reply_text("❌ Invalid ID. Must be a number.", reply_markup=kb)

    elif field == "removeadmin":
        try:
            uid = int(value)
            if uid not in state.sub_admins:
                await update.message.reply_text(f"⚠️ <code>{uid}</code> is not a sub admin.", parse_mode="HTML", reply_markup=kb)
                return
            state.sub_admins.discard(uid)
            await update.message.reply_text(f"✅ Removed <code>{uid}</code> from sub admins.", parse_mode="HTML", reply_markup=kb)
        except ValueError:
            await update.message.reply_text("❌ Invalid ID. Must be a number.", reply_markup=kb)

    elif field == "fee":
        try:
            fee = float(value)
            if not (0 <= fee <= 50):
                await update.message.reply_text("❌ Fee must be between 0 and 50.", reply_markup=kb)
                return
            old = state.fee_percent
            state.fee_percent = fee
            await update.message.reply_text(f"✅ Fee updated: <s>{old}%</s> → <b>{fee}%</b>", parse_mode="HTML", reply_markup=kb)
        except ValueError:
            await update.message.reply_text("❌ Invalid number.", reply_markup=kb)

    elif field == "bio":
        state.required_bio = value
        await update.message.reply_text(f"✅ Bio tag set: <b>{value}</b>\nUsers with this in bio → 0% fee.", parse_mode="HTML", reply_markup=kb)

    elif field == "oxapay":
        state.oxapay_key = value
        masked = f"{value[:4]}{'*'*(len(value)-8)}{value[-4:]}" if len(value) > 8 else "****"
        await update.message.reply_text(f"✅ <b>OxaPay Key Set!</b>\n🔑 <code>{masked}</code>\n\nUse Check OxaPay to verify.", parse_mode="HTML", reply_markup=kb)

    elif field == "api_id":
        try:
            state.api_id = int(value)
            await update.message.reply_text(f"✅ API ID set: <code>{state.api_id}</code>\n\nNow set API Hash.", parse_mode="HTML", reply_markup=kb)
        except ValueError:
            await update.message.reply_text("❌ API ID must be a number.", reply_markup=kb)

    elif field == "api_hash":
        state.api_hash = value
        await update.message.reply_text("✅ <b>API Hash set!</b>\n\nNow set Phone number.", parse_mode="HTML", reply_markup=kb)

    elif field == "phone":
        state.phone = value
        await update.message.reply_text(f"✅ Phone set: <code>{value}</code>\n\nNow press 📡 Telethon → 🚀 Connect.", parse_mode="HTML", reply_markup=kb)

    elif field == "otp":
        if not getattr(state, "_pending_telethon", None):
            await update.message.reply_text("❌ Session expired. Press 🚀 Connect again.", reply_markup=kb)
            return
        try:
            client = state._pending_telethon
            await client.sign_in(state.phone, value)
            state.telethon_client = client
            state._waiting_otp = False
            state._pending_telethon = None
            await update.message.reply_text("✅ <b>Telethon Connected!</b>\n\nAuto group creation is ACTIVE! 🎉", parse_mode="HTML", reply_markup=kb)
        except Exception as e:
            await update.message.reply_text(f"❌ OTP failed: {e}\n\nTry connecting again.", reply_markup=kb)

    else:
        await update.message.reply_text("⚠️ Unknown input field.", reply_markup=kb)


# ══════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════

async def post_init(app):
    await start_telethon()

def main():
    logger.info("Starting P2P Escrow Bot…")
    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("instructions", cmd_instructions))
    app.add_handler(CommandHandler("adminpanel", cmd_adminpanel))
    app.add_handler(CommandHandler("initdeal", cmd_initdeal))
    app.add_handler(CommandHandler("dd", cmd_dd))
    app.add_handler(CommandHandler("buyer", cmd_buyer))
    app.add_handler(CommandHandler("seller", cmd_seller))
    app.add_handler(CommandHandler("token", cmd_token))
    app.add_handler(CommandHandler("deposit", cmd_deposit))
    app.add_handler(CommandHandler("verify", cmd_verify))
    app.add_handler(CommandHandler("release", cmd_release))
    app.add_handler(CommandHandler("dispute", cmd_dispute))
    app.add_handler(CommandHandler("dealinfo", cmd_dealinfo))
    app.add_handler(CommandHandler("setloggroup", cmd_setloggroup))
    app.add_handler(CommandHandler("addadmin", cmd_addadmin))
    app.add_handler(CommandHandler("removeadmin", cmd_removeadmin))
    app.add_handler(CommandHandler("setfee", cmd_setfee))
    app.add_handler(CommandHandler("setbio", cmd_setbio))
    app.add_handler(CommandHandler("setoxapay", cmd_setoxapay))
    app.add_handler(CommandHandler("checkoxapay", cmd_checkoxapay))
    app.add_handler(CommandHandler("resetoxapay", cmd_resetoxapay))
    app.add_handler(CommandHandler("releaseto", cmd_releaseto))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("listadmins", cmd_listadmins))
    app.add_handler(CommandHandler("canceldeal", cmd_canceldeal))
    app.add_handler(MessageHandler(filters.TEXT & filters.ChatType.PRIVATE & ~filters.COMMAND, admin_input_handler))
    app.add_handler(CallbackQueryHandler(callback_handler))

    logger.info("All handlers registered. Bot running…")
    app.run_polling(allowed_updates=["message", "callback_query", "chat_member"])

if __name__ == "__main__":
    main()
