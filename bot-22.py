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

# ── waiting state dicts ──────────────────────────────────
# admin panel text input:  user_id -> field_name
_admin_waiting: dict[int, str] = {}
# buyer/seller address collection:  user_id -> {"deal_id": str, "role": str, "chat_id": int}
_address_waiting: dict[int, dict] = {}
# dd form collection:  chat_id -> True (waiting for form reply)
_dd_waiting: dict[int, str] = {}   # chat_id -> deal_id

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

async def create_group_telethon(title: str, bot_username: str, ctx_bot=None):
    client = state.telethon_client
    if not client:
        return None, None, None
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

        # ── User invite link: max 2 members (buyer + seller only) ──
        from telethon.tl.functions.messages import ExportChatInviteRequest as _ExpInv
        user_invite = await client(_ExpInv(peer=channel, usage_limit=2))
        invite_link = user_invite.link  # this link allows only 2 joins

        # ── Telethon account (creator/owner) leaves the group ──
        # Must leave so admin's personal account is not visible as creator.
        # Strategy: try LeaveChannelRequest first; if it fails or account is still
        # in the group, demote self to member then leave (required for creators).
        try:
            from telethon.tl.functions.channels import LeaveChannelRequest
            from telethon.tl.functions.channels import GetParticipantRequest
            from telethon.tl.functions.channels import EditAdminRequest as _EditAdm
            from telethon.tl.types import ChatAdminRights as _CAR

            # Step 1: Demote self (creator → plain member) so leave is possible
            me = await client.get_me()
            try:
                empty_rights = _CAR(
                    post_messages=False, edit_messages=False, delete_messages=False,
                    ban_users=False, invite_users=False, pin_messages=False,
                    add_admins=False, manage_call=False, other=False
                )
                await client(_EditAdm(channel=channel, user_id=me.id,
                                      admin_rights=empty_rights, rank=""))
            except Exception as demote_err:
                logger.warning(f"Could not demote self before leave: {demote_err}")

            # Step 2: Leave the channel
            await client(LeaveChannelRequest(channel))
            logger.info("Telethon account successfully left the group.")

        except Exception as e:
            logger.warning(f"Could not leave group as creator: {e}")
            # Step 3 fallback — try kicking self via bot if leave failed
            try:
                me = await client.get_me()
                await ctx_bot.ban_chat_member(chat_id=group_id, user_id=me.id)
                await ctx_bot.unban_chat_member(chat_id=group_id, user_id=me.id)
                logger.info("Telethon account kicked+unbanned (fallback leave).")
            except Exception as kick_err:
                logger.warning(f"Fallback kick also failed: {kick_err}")

        return group_id, invite_link

    except Exception as e:
        logger.error(f"Telethon group creation failed: {e}")
        return None, None

# ══════════════════════════════════════════════════════════
# HELPERS
# ══════════════════════════════════════════════════════════

def is_main_admin(uid): return uid == MAIN_ADMIN_ID
def is_admin(uid):      return uid == MAIN_ADMIN_ID or uid in state.sub_admins
def trade_id():         return "TRD-" + str(uuid.uuid4()).upper()[:8]
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
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("➕ Add Me to Group", callback_data=f"dispute_handle:{deal_id}")
    ]]) if deal_id else None
    # DM every admin
    for uid in [MAIN_ADMIN_ID] + list(state.sub_admins):
        try:
            await ctx.bot.send_message(chat_id=uid, text=msg, reply_markup=kb, parse_mode="HTML")
        except Exception:
            pass
    # Also post to dispute group if configured
    if state.dispute_group_id:
        try:
            await ctx.bot.send_message(
                chat_id=state.dispute_group_id,
                text=msg, reply_markup=kb, parse_mode="HTML"
            )
        except Exception as e:
            logger.warning(f"Could not post to dispute group: {e}")

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
        "deposit_address": None, "oxapay_track_id": None,
        "buyer_confirmed": False, "seller_confirmed": False,
        "funded": False, "created_at": datetime.utcnow().isoformat()
    }

# ══════════════════════════════════════════════════════════
# /start
# ══════════════════════════════════════════════════════════

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        return
    user = update.effective_user
    if is_admin(user.id):
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🤝 Start Deal", callback_data="start_deal")],
            [InlineKeyboardButton("📖 Instructions", callback_data="show_instructions")],
            [InlineKeyboardButton("👑 Admin Panel", callback_data="adm:status")]
        ])
    else:
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🤝 Start Deal", callback_data="start_deal")],
            [InlineKeyboardButton("📖 Instructions", callback_data="show_instructions")]
        ])
    text = (
        "🔐 <b>P2P Escrow Bot</b>\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "✅ Secure • Fast • Trustless\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "Trade safely with crypto escrow.\n"
        "Your funds are protected until both parties confirm.\n\n"
        "👇 Choose an option to begin:"
    )
    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=kb, parse_mode="HTML")
    else:
        await update.message.reply_text(text, reply_markup=kb, parse_mode="HTML")

# ══════════════════════════════════════════════════════════
# /instructions
# ══════════════════════════════════════════════════════════

async def cmd_instructions(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = (
        "📖 <b>HOW TO USE ESCROW BOT</b>\n\n"
        "<b>1️⃣</b> /start → <b>Start Deal</b> — bot creates private group\n\n"
        "<b>2️⃣</b> Both join the group\n\n"
        "<b>3️⃣</b> /dd — bot sends a blank deal form, you fill & send it back\n\n"
        "<b>4️⃣</b> /buyer — sets YOU as buyer (bot asks for your wallet)\n"
        "      /seller — sets YOU as seller (bot asks for your wallet)\n\n"
        "<b>5️⃣</b> /token → select token → both confirm\n\n"
        "<b>6️⃣</b> /deposit → seller pays crypto to escrow address\n\n"
        "<b>7️⃣</b> /verify → OxaPay confirms payment → buyer pays seller off-platform\n\n"
        "<b>8️⃣</b> /release → buyer or seller runs after buyer pays → both confirm\n\n"
        "<b>9️⃣</b> Crypto releases to buyer's wallet automatically\n\n"
        "<b>🚨</b> /dispute — call admin if any issue\n\n"
        "⚠️ <i>All steps must be done inside your deal group</i>"
    )
    if update.callback_query:
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back_to_start")]])
        await update.callback_query.edit_message_text(text, parse_mode="HTML", reply_markup=kb)
    else:
        await update.message.reply_text(text, parse_mode="HTML")

# ══════════════════════════════════════════════════════════
# ADMIN PANEL
# ══════════════════════════════════════════════════════════

def admin_panel_kb():
    tc = "✅ ON" if state.telethon_client else "❌ OFF"
    ox = "✅ SET" if state.oxapay_key else "❌ NOT SET"
    lg = "✅ SET" if state.log_group_id else "❌ NOT SET"
    dg = "✅ SET" if state.dispute_group_id else "❌ NOT SET"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"📋 Log Group {lg}", callback_data="adm:setloggroup"),
         InlineKeyboardButton(f"🚨 Dispute Group {dg}", callback_data="adm:setdisputegroup")],
        [InlineKeyboardButton("📊 Status", callback_data="adm:status"),
         InlineKeyboardButton("👥 List Admins", callback_data="adm:listadmins")],
        [InlineKeyboardButton("➕ Add Admin", callback_data="adm:addadmin"),
         InlineKeyboardButton("➖ Remove Admin", callback_data="adm:removeadmin")],
        [InlineKeyboardButton("💸 Set Fee %", callback_data="adm:setfee"),
         InlineKeyboardButton("🏷 Set Bio Tag", callback_data="adm:setbio")],
        [InlineKeyboardButton("🎟 Set Bio Discount %", callback_data="adm:setbiodiscount"),
         InlineKeyboardButton(f"🔑 OxaPay {ox}", callback_data="adm:setoxapay")],
        [InlineKeyboardButton("✅ Check OxaPay", callback_data="adm:checkoxapay"),
         InlineKeyboardButton("🗑 Reset OxaPay", callback_data="adm:resetoxapay")],
        [InlineKeyboardButton(f"📡 Telethon {tc}", callback_data="adm:telethon"),
         InlineKeyboardButton("🔄 Refresh", callback_data="adm:status")]
    ])

async def cmd_adminpanel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_main_admin(update.effective_user.id):
        return
    await update.message.reply_text(
        "👑 <b>ADMIN CONTROL PANEL</b>\n\nSelect an action:",
        reply_markup=admin_panel_kb(), parse_mode="HTML"
    )

# ══════════════════════════════════════════════════════════
# CALLBACK ROUTER
# ══════════════════════════════════════════════════════════

async def callback_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    d = q.data
    if   d == "start_deal":                  await handle_start_deal(update, ctx)
    elif d == "show_instructions":           await cmd_instructions(update, ctx)
    elif d == "back_to_start":               await cmd_start(update, ctx)
    elif d.startswith("token_select:"):      await handle_token_pick(update, ctx, d)
    elif d.startswith("token_confirm:"):     await handle_token_confirm(update, ctx, d)
    elif d.startswith("token_reselect:"):    await handle_token_reselect(update, ctx, d)
    elif d.startswith("confirm:"):           await handle_confirmation(update, ctx, d)
    elif d.startswith("refund_confirm:"):    await handle_refund_confirm(update, ctx, d)
    elif d.startswith("refund_cancel:"):     await handle_refund_cancel(update, ctx, d)
    elif d.startswith("dispute_handle:"):    await handle_dispute_admin(update, ctx, d)
    elif d.startswith("dispute_call"):       await handle_dispute_call(update, ctx)
    elif d.startswith("adm:"):               await handle_admin_panel_cb(update, ctx, d)

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
        done  = sum(1 for x in all_d if x["status"] == "COMPLETED")
        dis   = sum(1 for x in all_d if x["status"] == "DISPUTED")
        fund  = sum(1 for x in all_d if x["status"] == "FUNDED")
        ox = f"✅ {state.oxapay_key[:4]}...{state.oxapay_key[-4:]}" if state.oxapay_key else "❌ Not Set (Demo)"
        lg = f"✅ <code>{state.log_group_id}</code>" if state.log_group_id else "❌ Not Set"
        dg = f"✅ <code>{state.dispute_group_id}</code>" if state.dispute_group_id else "❌ Not Set"
        tc = "✅ Connected" if state.telethon_client else "❌ Not Connected"
        await q.edit_message_text(
            f"📊 <b>BOT STATUS</b>\n\n"
            f"📋 Log Group: {lg}\n🚨 Dispute Group: {dg}\n🔑 OxaPay: {ox}\n📡 Telethon: {tc}\n"
            f"💸 Fee: <b>{state.fee_percent}%</b>  🎟 Bio Discount: <b>{getattr(state,'bio_discount_percent',0.0)}%</b>\n🏷 Bio Tag: <b>{state.required_bio or 'Not Set'}</b>\n"
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
            def _check():
                # OxaPay has no /balance endpoint — validate key via /merchants/inquiry
                # with a dummy trackId. Result 200 = not found (key valid), 203 = invalid key
                req = urllib.request.Request(
                    "https://api.oxapay.com/merchants/inquiry",
                    data=_json.dumps({
                        "merchant": state.oxapay_key,
                        "trackId":  0
                    }).encode(),
                    headers={"Content-Type": "application/json"}
                )
                with urllib.request.urlopen(req, timeout=15) as r:
                    return _json.loads(r.read().decode())
            data = await loop.run_in_executor(None, _check)
            logger.info(f"OxaPay key check response: {data}")
            result = data.get("result")
            # result 200 = invoice not found (key IS valid, just no such trackId)
            # result 100 = success
            # result 203 = invalid/unauthorized merchant key
            if result in (100, 200):
                txt = (
                    f"✅ <b>OxaPay Key Valid!</b>\n\n"
                    f"🔑 Key: <code>{state.oxapay_key[:4]}{'*'*(len(state.oxapay_key)-8)}{state.oxapay_key[-4:]}</code>\n"
                    f"🌐 API: Connected\n\n"
                    f"Bot is in <b>LIVE mode</b> — real crypto will be processed."
                )
            elif result == 203:
                txt = (
                    f"❌ <b>Invalid OxaPay Key!</b>\n\n"
                    f"The API key was rejected by OxaPay (error 203).\n"
                    f"Please check your merchant key and update it."
                )
            else:
                txt = f"⚠️ <b>OxaPay responded:</b> result={result}\nMessage: {data.get('message', 'Unknown')}"
        except Exception as e:
            txt = f"❌ Connection failed: {e}"
        await q.edit_message_text(txt, parse_mode="HTML", reply_markup=admin_panel_kb())

    elif action == "resetoxapay":
        state.oxapay_key = None
        await q.edit_message_text("✅ <b>OxaPay key removed.</b> Bot in DEMO mode.", parse_mode="HTML", reply_markup=admin_panel_kb())

    elif action == "setloggroup":
        await q.edit_message_text(
            "📋 <b>Set Log Group</b>\n\n1. Create a private group\n2. Add bot as admin\n3. Send <code>/setloggroup</code> inside that group",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="adm:status")]])
        )

    elif action == "setdisputegroup":
        await q.edit_message_text(
            "🚨 <b>Set Dispute Group</b>\n\n"
            "Option 1 — Run inside the group:\n"
            "<code>/setdisputegroup</code>\n\n"
            "Option 2 — From any chat with group ID:\n"
            "<code>/setdisputegroup -100xxxxxxxxxx</code>\n\n"
            "Option 3 — With invite link (bot must be member):\n"
            "<code>/setdisputegroup https://t.me/+xxxxx</code>\n\n"
            "⚠️ Bot must be admin/member in that group.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="adm:status")]])
        )

    elif action == "telethon":
        tc = "✅ Connected" if state.telethon_client else "❌ Not Connected"
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔢 Set API ID",   callback_data="adm:inp:api_id"),
             InlineKeyboardButton("🔑 Set API Hash", callback_data="adm:inp:api_hash")],
            [InlineKeyboardButton("📱 Set Phone",    callback_data="adm:inp:phone")],
            [InlineKeyboardButton("🚀 Connect Telethon", callback_data="adm:starttelethon")],
            [InlineKeyboardButton("⬅️ Back",         callback_data="adm:status")]
        ])
        await q.edit_message_text(
            f"📡 <b>TELETHON SETUP</b>\n\nStatus: {tc}\n\n"
            f"• API ID: <b>{'✅ Set' if state.api_id else '❌ Not Set'}</b>\n"
            f"• API Hash: <b>{'✅ Set' if state.api_hash else '❌ Not Set'}</b>\n"
            f"• Phone: <b>{'✅ ' + str(state.phone) if state.phone else '❌ Not Set'}</b>\n\n"
            f"Get API ID & Hash from: my.telegram.org\n\nSet each one then press 🚀 Connect",
            parse_mode="HTML", reply_markup=kb
        )

    elif action == "starttelethon":
        if not state.api_id or not state.api_hash or not state.phone:
            await q.answer("❌ Set API ID, Hash and Phone first!", show_alert=True)
            return
        await q.edit_message_text("⏳ <b>Connecting Telethon…</b>\n\nCheck your Telegram app for OTP.", parse_mode="HTML")
        try:
            from telethon import TelegramClient as _TC
            client = _TC("escrow_session", int(state.api_id), state.api_hash)
            state._pending_telethon = client
            await client.connect()
            if not await client.is_user_authorized():
                await client.send_code_request(state.phone)
                state._waiting_otp = True
                kb = InlineKeyboardMarkup([
                    [InlineKeyboardButton("✏️ Enter OTP", callback_data="adm:inp:otp")],
                    [InlineKeyboardButton("⬅️ Back", callback_data="adm:telethon")]
                ])
                await q.edit_message_text("📲 <b>OTP Sent!</b>\n\nPress below to enter the code from your Telegram app.", parse_mode="HTML", reply_markup=kb)
            else:
                state.telethon_client = client
                await q.edit_message_text("✅ <b>Telethon Connected!</b>\n\nAuto group creation is now ACTIVE! 🎉", parse_mode="HTML", reply_markup=admin_panel_kb())
        except Exception as e:
            await q.edit_message_text(f"❌ <b>Telethon Error:</b> {e}", parse_mode="HTML", reply_markup=admin_panel_kb())

    elif action.startswith("inp:"):
        field = action.split(":")[1]
        labels = {
            "api_id":      ("🔢 <b>Enter API ID</b>",         "Numbers only. Get from my.telegram.org"),
            "api_hash":    ("🔑 <b>Enter API Hash</b>",        "Long string. Get from my.telegram.org"),
            "phone":       ("📱 <b>Enter Phone Number</b>",    "Include country code. Example: +91XXXXXXXXXX"),
            "otp":         ("📲 <b>Enter OTP Code</b>",        "Check your Telegram app for the code"),
            "oxapay":      ("🔑 <b>Enter OxaPay API Key</b>",  "From your OxaPay merchant dashboard"),
            "fee":         ("💸 <b>Enter Fee Percentage</b>",  "Numbers only. Example: 1.5"),
            "bio":         ("🏷 <b>Enter Bio Tag</b>",         "Users with this in bio get 0% fee"),
            "addadmin":    ("➕ <b>Enter User ID</b>",          "Telegram user ID to add as sub admin"),
            "removeadmin": ("➖ <b>Enter User ID</b>",          "Telegram user ID to remove from sub admins"),
        }
        title, hint = labels.get(field, ("✏️ <b>Enter Value</b>", "Type and send"))
        _admin_waiting[q.from_user.id] = field
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("❌ Cancel", callback_data="adm:cancel_input"),
             InlineKeyboardButton("⬅️ Back", callback_data="adm:status")]
        ])
        await q.edit_message_text(f"{title}\n\n💡 {hint}\n\nType and send your message now:", parse_mode="HTML", reply_markup=kb)

    elif action == "cancel_input":
        _admin_waiting.pop(q.from_user.id, None)
        await q.edit_message_text("❌ <b>Cancelled.</b>", parse_mode="HTML", reply_markup=admin_panel_kb())

    elif action in ("addadmin", "removeadmin", "setfee", "setbio", "setbiodiscount", "setoxapay"):
        field_map = {
            "addadmin": "addadmin", "removeadmin": "removeadmin",
            "setfee": "fee", "setbio": "bio", "setbiodiscount": "bio_discount", "setoxapay": "oxapay"
        }
        field = field_map[action]
        cur_discount = getattr(state, "bio_discount_percent", 0.0)
        labels = {
            "addadmin":    ("➕ <b>Add Sub Admin</b>",    "Enter Telegram User ID"),
            "removeadmin": ("➖ <b>Remove Sub Admin</b>", "Enter Telegram User ID to remove"),
            "fee":         ("💸 <b>Set Fee %</b>",        f"Current: {state.fee_percent}% — Enter new value (0-50)"),
            "bio":         ("🏷 <b>Set Bio Tag</b>",      f"Current: {state.required_bio or 'Not set'} — Enter new tag"),
            "bio_discount":("🎟 <b>Set Bio Discount %</b>", f"Current: {cur_discount}% — Fee for bio-matched users (0 = free)"),
            "oxapay":      ("🔑 <b>Set OxaPay Key</b>",  "Enter your OxaPay API key"),
        }
        title, hint = labels[field]
        _admin_waiting[q.from_user.id] = field
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("❌ Cancel", callback_data="adm:cancel_input"),
             InlineKeyboardButton("⬅️ Back", callback_data="adm:status")]
        ])
        await q.edit_message_text(f"{title}\n\n💡 {hint}\n\nType and send your message now:", parse_mode="HTML", reply_markup=kb)

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
        group_id, invite_url = await create_group_telethon(f"🔒 Escrow {tid}", bot_me.username, ctx.bot)

    if not group_id:
        await ctx.bot.send_message(
            chat_id=user.id,
            text=(
                "⚠️ <b>Auto Group Creation Failed</b>\n\n"
                "Please do this manually:\n"
                "1️⃣ Create a Telegram group\n"
                "2️⃣ Add this bot as <b>Admin</b>\n"
                "3️⃣ Run <code>/initdeal</code> inside the group\n\n"
                "<i>Make sure API_ID, API_HASH and PHONE are set correctly</i>"
            ),
            parse_mode="HTML"
        )
        return

    deal = new_deal(tid, group_id, user.id)
    state.deals[tid] = deal
    state.group_to_deal[group_id] = tid

    # ── Send user-facing invite (2-person limit: buyer + seller only) ──
    await ctx.bot.send_message(
        chat_id=user.id,
        text=(
            f"✅ <b>Deal Group Created!</b>\n\n"
            f"🆔 Trade ID: <code>{tid}</code>\n"
            f"🔗 <b>Deal Invite Link (max 2 joins):</b>\n{invite_url}\n\n"
            f"⚠️ <b>Share this link ONLY with the other party.</b>\n"
            f"This link allows exactly <b>2 people</b> to join (buyer + seller).\n\n"
            f"➡️ <b>Next step:</b> Both join the group, then run <b>/dd</b> to fill the deal form."
        ),
        parse_mode="HTML"
    )

    # ── Notify admins (info only — no join link at this stage) ──
    for admin_id in [MAIN_ADMIN_ID] + list(state.sub_admins):
        try:
            await ctx.bot.send_message(
                chat_id=admin_id,
                text=(
                    f"🆕 <b>NEW DEAL STARTED</b>\n\n"
                    f"🆔 Trade ID: <code>{tid}</code>\n"
                    f"👤 Creator: @{user.username or user.first_name} ({user.id})\n"
                    f"📦 Group: <code>{group_id}</code>\n"
                    f"⏰ {deal['created_at']}\n\n"
                    f"<i>Admin join link will be provided only if /dispute is called.</i>"
                ),
                parse_mode="HTML"
            )
        except Exception:
            pass

    # ── Full instructions in group welcome message ──
    try:
        await ctx.bot.send_message(
            chat_id=group_id,
            text=(
                f"🔒 <b>ESCROW DEAL GROUP — {tid}</b>\n\n"
                f"Welcome! This is a secure P2P escrow group.\n\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"📋 <b>RULES (IMPORTANT):</b>\n"
                f"• Only <b>Buyer</b> and <b>Seller</b> are in this group\n"
                f"• Each person sets their role <b>once only</b> — no changes allowed\n"
                f"• Admin joins <b>only if called</b> via /dispute\n\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"📖 <b>STEPS TO FOLLOW:</b>\n\n"
                f"<b>1️⃣</b> Send <b>/dd</b> → fill the deal form (qty, rate, condition)\n\n"
                f"<b>2️⃣</b> Buyer sends <b>/buyer</b> → sends wallet address (locked permanently)\n"
                f"    Seller sends <b>/seller</b> → sends wallet address (locked permanently)\n\n"
                f"<b>3️⃣</b> Send <b>/token</b> → select crypto token → both confirm\n\n"
                f"<b>4️⃣</b> Seller uses <b>/deposit</b> → gets escrow address → sends crypto\n\n"
                f"<b>5️⃣</b> Send <b>/verify</b> → bot confirms payment received\n\n"
                f"<b>6️⃣</b> Buyer pays seller off-platform (fiat/goods)\n\n"
                f"<b>7️⃣</b> Send <b>/release</b> → both confirm → crypto releases to buyer\n\n"
                f"<b>🚨 /dispute</b> → calls admin if any problem\n\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"⚠️ <i>After deal completes, both parties will be removed and group deleted automatically.</i>"
            ),
            parse_mode="HTML"
        )
    except Exception as e:
        logger.warning(f"Could not send welcome msg to group: {e}")

    await log(ctx,
        f"🆕 <b>DEAL CREATED</b>\n\n"
        f"🆔 Trade ID: <code>{tid}</code>\n"
        f"👤 Creator: @{user.username or user.first_name} ({user.id})\n"
        f"📦 Group ID: <code>{group_id}</code>\n"
        f"🔗 User Invite (limit 2): {invite_url}\n"
        f"⏰ {deal['created_at']}\n"
        f"📊 Status: SETUP"
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
        f"➡️ <b>Next step:</b> Either party sends <b>/dd</b> to fill the deal form.",
        parse_mode="HTML"
    )
    await log(ctx, f"🆕 <b>DEAL CREATED</b>\n\n🆔 <code>{tid}</code>\n👤 @{user.username} ({user.id})\n📦 <code>{chat.id}</code>\n⏰ {deal['created_at']}")

# ══════════════════════════════════════════════════════════
# STEP 3: /dd — sends blank copyable form
# ══════════════════════════════════════════════════════════

BLANK_FORM = (
    "QUANTITY-\n"
    "RATE-\n"
    "CONDITION-"
)

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
        await update.message.reply_text(
            f"⚠️ Deal form already filled. Status: <b>{deal['status']}</b>\n\n"
            f"💰 Quantity: {deal.get('quantity','—')}\n"
            f"📈 Rate: {deal.get('rate','—')}\n"
            f"📝 Condition: {deal.get('condition','—')}",
            parse_mode="HTML"
        )
        return

    # Mark this group as waiting for a form reply
    _dd_waiting[chat.id] = did

    await update.message.reply_text(
        "📋 <b>DEAL FORM</b>\n\n"
        "Copy the form below, fill in the values, and <b>send it back in this group</b>:\n\n"
        f"<code>{BLANK_FORM}</code>\n\n"
        "Example:\n"
        "<code>QUANTITY-500\n"
        "RATE-1.02\n"
        "CONDITION-Payment within 30 minutes</code>\n\n"
        "⚠️ Keep the format exactly as shown.",
        parse_mode="HTML"
    )

# ══════════════════════════════════════════════════════════
# GROUP MESSAGE HANDLER — captures deal form + address replies
# ══════════════════════════════════════════════════════════

async def group_message_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    user = update.effective_user
    msg  = update.message
    if not msg or not msg.text:
        return
    text = msg.text.strip()

    # ── Deal form capture ────────────────────────────────
    if chat.id in _dd_waiting:
        did = _dd_waiting[chat.id]
        deal = deal_by_id(did)
        if deal and deal["status"] == "SETUP":
            lines = {l.split("-", 1)[0].strip().upper(): l.split("-", 1)[1].strip()
                     for l in text.splitlines() if "-" in l}
            qty  = lines.get("QUANTITY", "").strip()
            rate = lines.get("RATE",     "").strip()
            cond = lines.get("CONDITION","").strip()

            if not qty or not rate:
                await msg.reply_text(
                    "❌ Invalid format. Please use:\n\n"
                    "<code>QUANTITY-500\nRATE-1.02\nCONDITION-Pay within 30 mins</code>",
                    parse_mode="HTML"
                )
                return

            deal["quantity"]  = qty
            deal["rate"]      = rate
            deal["condition"] = cond or "None"
            deal["status"]    = "FORM_FILLED"   # ← strict flow: buyer/seller only after this
            _dd_waiting.pop(chat.id, None)

            await msg.reply_text(
                f"✅ <b>Deal Form Confirmed!</b>\n\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"💰 QUANTITY — <b>{qty}</b>\n"
                f"📈 RATE — <b>{rate}</b>\n"
                f"📝 CONDITION — <b>{deal['condition']}</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━\n\n"
                f"🔒 Form is now locked.\n\n"
                f"➡️ <b>Next step:</b>\n"
                f"• Buyer sends <b>/buyer</b> → then send wallet address\n"
                f"• Seller sends <b>/seller</b> → then send wallet address",
                parse_mode="HTML"
            )
            return

    # ── Wallet address capture (from buyer/seller) ───────
    if user.id in _address_waiting:
        info = _address_waiting[user.id]
        if info.get("chat_id") != chat.id:
            return  # wrong group
        did   = info["deal_id"]
        role  = info["role"]
        deal  = deal_by_id(did)
        if not deal:
            _address_waiting.pop(user.id, None)
            return

        address = text
        deal[f"{role}_id"]       = user.id
        deal[f"{role}_username"] = user.username or user.first_name
        deal[f"{role}_address"]  = address
        # ── Same-address BLOCK ──
        other_role = "seller" if role == "buyer" else "buyer"
        other_addr = deal.get(f"{other_role}_address")
        if other_addr and other_addr.strip().lower() == address.strip().lower():
            # Rollback — don't allow same address
            deal[f"{role}_id"]       = None
            deal[f"{role}_username"] = None
            deal[f"{role}_address"]  = None
            deal[f"{role}_locked"]   = None
            _address_waiting[user.id] = {"deal_id": did, "role": role, "chat_id": chat.id}
            await msg.reply_text(
                f"❌ <b>Same Address Not Allowed!</b>\n\n"
                f"Your wallet address is the same as the <b>{other_role}'s</b>.\n"
                f"Both parties must use <b>different wallets</b>.\n\n"
                f"Please send a <b>different wallet address</b> now.",
                parse_mode="HTML"
            )
            return

        deal[f"{role}_locked"] = True
        _address_waiting.pop(user.id, None)

        b = deal.get("buyer_id") is not None
        s = deal.get("seller_id") is not None
        if b and s:
            deal["status"] = "ROLES_SET"
            next_step = (
                "🔒 <b>Both roles locked!</b>\n\n"
                "➡️ <b>Next step:</b> Use <b>/token</b> to select payment token"
            )
        elif b:
            next_step = "⏳ Waiting for <b>Seller</b> to send <b>/seller</b>"
        else:
            next_step = "⏳ Waiting for <b>Buyer</b> to send <b>/buyer</b>"

        label = "🛒 Buyer" if role == "buyer" else "🏪 Seller"
        await msg.reply_text(
            f"{'🛒' if role == 'buyer' else '🏪'} <b>{label} Registered!</b>\n\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"👤 @{deal[f'{role}_username']}\n"
            f"💳 Wallet: <code>{address}</code>\n"
            f"🔒 Role permanently locked\n"
            f"━━━━━━━━━━━━━━━━━━━━\n\n"
            f"{next_step}",
            parse_mode="HTML"
        )

# ══════════════════════════════════════════════════════════
# STEP 4: /buyer & /seller — self-assignment only
# ══════════════════════════════════════════════════════════

async def cmd_buyer(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await _set_role_prompt(update, ctx, "buyer")

async def cmd_seller(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await _set_role_prompt(update, ctx, "seller")

async def _set_role_prompt(update: Update, ctx: ContextTypes.DEFAULT_TYPE, role: str):
    chat = update.effective_chat
    user = update.effective_user
    if chat.type == "private":
        await update.message.reply_text("❌ Use inside your deal group.")
        return
    did, deal = deal_by_group(chat.id)
    if not deal:
        await update.message.reply_text("❌ No active deal here.")
        return

    # ── STRICT FLOW: /dd form must be filled first ──
    if deal["status"] == "SETUP":
        await update.message.reply_text(
            "⚠️ <b>Fill the deal form first!</b>\n\n"
            "Send <b>/dd</b> to get the deal form, fill it and send it back.\n"
            "Only then can you register as buyer or seller.",
            parse_mode="HTML"
        )
        return

    # ── PERMANENT LOCK: roles can only be set in FORM_FILLED or ROLES_SET ──
    if deal["status"] not in ("FORM_FILLED", "ROLES_SET"):
        await update.message.reply_text(
            f"🔒 <b>Roles are locked.</b> Deal status: <b>{deal['status']}</b>\n\n"
            f"Role changes are not allowed after deal has progressed.",
            parse_mode="HTML"
        )
        return

    # ── LOCK: if THIS role is already taken (confirmed) ──
    existing_id = deal.get(f"{role}_id")
    if existing_id is not None:
        existing_username = deal.get(f"{role}_username", "unknown")
        if existing_id == user.id:
            await update.message.reply_text(
                f"✅ You are already registered as the <b>{'🛒 Buyer' if role == 'buyer' else '🏪 Seller'}</b>.\n\n"
                f"<i>Roles cannot be changed after setting.</i>",
                parse_mode="HTML"
            )
        else:
            await update.message.reply_text(
                f"🔒 <b>{'Buyer' if role == 'buyer' else 'Seller'} role is already taken</b> by @{existing_username}.\n\n"
                f"<i>This message has been ignored.</i>",
                parse_mode="HTML"
            )
        return

    # ── Prevent one person from being both buyer AND seller ──
    # Check 1: already confirmed in the other role
    other_role = "seller" if role == "buyer" else "buyer"
    if deal.get(f"{other_role}_id") == user.id:
        await update.message.reply_text(
            f"❌ You are already the <b>{other_role}</b>. You cannot also be the {role}.",
            parse_mode="HTML"
        )
        return

    # Check 2: currently waiting to submit address for the other role
    pending = _address_waiting.get(user.id)
    if pending and pending.get("deal_id") == did and pending.get("role") != role:
        await update.message.reply_text(
            f"❌ You already claimed the <b>{pending['role']}</b> role. "
            f"Please send your wallet address first, or you cannot take both roles.",
            parse_mode="HTML"
        )
        return

    # Check 3: already waiting for THIS role (re-sent command) — just remind them
    if pending and pending.get("deal_id") == did and pending.get("role") == role:
        label = "🛒 Buyer" if role == "buyer" else "🏪 Seller"
        await update.message.reply_text(
            f"⏳ <b>{label}</b> — You already claimed this role.\n\n"
            f"📬 Please send your <b>wallet address</b> now to complete registration.",
            parse_mode="HTML"
        )
        return

    label = "🛒 Buyer" if role == "buyer" else "🏪 Seller"

    # Register this user as waiting to provide their address
    _address_waiting[user.id] = {"deal_id": did, "role": role, "chat_id": chat.id}

    await update.message.reply_text(
        f"👋 <b>{label} — {user.first_name}</b>\n\n"
        f"📬 Please send your <b>wallet address</b> in this group now.\n\n"
        f"<i>Your next message will be captured as your address. Role is permanently locked after this.</i>",
        parse_mode="HTML"
    )

# ══════════════════════════════════════════════════════════
# STEP 5: /token — select & both confirm
# ══════════════════════════════════════════════════════════

TOKEN_LABELS = {
    "USDT_TRC20": "💵 USDT TRC20",
    "USDT_BEP20": "💵 USDT BEP20",
    "BTC":        "₿ BTC",
    "LTC":        "Ł LTC"
}

# Short currency symbol for amounts (e.g. "2.5 USDT", "0.001 BTC")
TOKEN_SYMBOL = {
    "USDT_TRC20": "USDT",
    "USDT_BEP20": "USDT",
    "BTC":        "BTC",
    "LTC":        "LTC",
}

def token_select_kb(did):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💵 USDT TRC20", callback_data=f"token_select:USDT_TRC20:{did}"),
         InlineKeyboardButton("💵 USDT BEP20", callback_data=f"token_select:USDT_BEP20:{did}")],
        [InlineKeyboardButton("₿ BTC",         callback_data=f"token_select:BTC:{did}"),
         InlineKeyboardButton("Ł LTC",         callback_data=f"token_select:LTC:{did}")]
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
        if deal["status"] in ("SETUP", "FORM_FILLED"):
            await update.message.reply_text(
                "⚠️ <b>Register buyer & seller first!</b>\n\n"
                "Both buyer and seller must register with <b>/buyer</b> and <b>/seller</b> before selecting a token.",
                parse_mode="HTML"
            )
        else:
            await update.message.reply_text(
                f"⚠️ Cannot select token at this stage. Status: <b>{deal['status']}</b>",
                parse_mode="HTML"
            )
        return
    if not deal.get("buyer_id") or not deal.get("seller_id"):
        await update.message.reply_text("❌ Both buyer and seller must be set first.")
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
        InlineKeyboardButton("🔄 Re-select",    callback_data=f"token_reselect:{did}")
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
    if user.id == deal.get("buyer_id"):    role = "buyer"
    elif user.id == deal.get("seller_id"): role = "seller"
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
            f"🔒 <b>Token Locked!</b>\n\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"🪙 Token: <b>{label}</b>\n"
            f"✅ Buyer: Confirmed\n"
            f"✅ Seller: Confirmed\n"
            f"━━━━━━━━━━━━━━━━━━━━\n\n"
            f"➡️ <b>Next step:</b> Seller uses <b>/deposit</b>",
            parse_mode="HTML"
        )
    else:
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Confirm Token", callback_data=f"token_confirm:{did}"),
            InlineKeyboardButton("🔄 Re-select",    callback_data=f"token_reselect:{did}")
        ]])
        await q.edit_message_text(
            f"🪙 <b>Token: {label}</b>\n\n"
            f"🛒 Buyer:  {'✅ Confirmed' if b_ok else '⏳ Waiting'}\n"
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
# STEP 6: /deposit — seller sends to OxaPay address
# ══════════════════════════════════════════════════════════

async def cmd_deposit(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    user = update.effective_user
    if chat.type == "private":
        await update.message.reply_text("❌ Use inside your deal group.")
        return
    did, deal = deal_by_group(chat.id)
    if not deal:
        await update.message.reply_text("❌ No active deal here.")
        return
    if deal["status"] not in ("TOKEN_SELECTED", "AWAITING_DEPOSIT"):
        if deal["status"] in ("SETUP", "FORM_FILLED"):
            await update.message.reply_text(
                "⚠️ <b>Complete earlier steps first!</b>\n\n"
                "1️⃣ Fill form with <b>/dd</b>\n"
                "2️⃣ Register roles with <b>/buyer</b> and <b>/seller</b>\n"
                "3️⃣ Select token with <b>/token</b>\n\n"
                "Then seller can use <b>/deposit</b>.",
                parse_mode="HTML"
            )
        elif deal["status"] == "ROLES_SET":
            await update.message.reply_text(
                "⚠️ <b>Select token first!</b>\n\nUse <b>/token</b> and both confirm it.",
                parse_mode="HTML"
            )
        else:
            await update.message.reply_text(
                f"⚠️ Cannot deposit at this stage. Status: <b>{deal['status']}</b>",
                parse_mode="HTML"
            )
        return
    # Only seller should deposit
    if user.id != deal.get("seller_id"):
        await update.message.reply_text("❌ Only the <b>seller</b> can initiate the deposit.", parse_mode="HTML")
        return

    # ── Bio discount preview ──
    dep_discount_applied = False
    dep_discount_reason  = ""
    dep_effective_fee    = state.fee_percent
    if state.required_bio:
        for chk_role, chk_id in (("buyer", deal.get("buyer_id")), ("seller", deal.get("seller_id"))):
            if not chk_id:
                continue
            try:
                chk_chat = await ctx.bot.get_chat(chk_id)
                bio = getattr(chk_chat, "bio", "") or ""
                if state.required_bio.lower() in bio.lower():
                    dep_discount_applied = True
                    dep_discount_reason  = f"{chk_role.capitalize()}'s bio contains «{state.required_bio}»"
                    dep_effective_fee    = getattr(state, "bio_discount_percent", 0.0)
                    break
            except Exception:
                pass

    qty_float = float(deal.get("quantity", 1))
    sym_dep   = TOKEN_SYMBOL.get(deal.get("token", ""), deal.get("token", ""))
    if dep_discount_applied:
        saved_amt = qty_float * (state.fee_percent / 100) - qty_float * (dep_effective_fee / 100)
        fee_info = (
            f"💸 Fee: ~~{state.fee_percent}%~~ → <b>{dep_effective_fee}% (Bio Discount ✅)</b>\n"
            f"🏷 {dep_discount_reason}\n"
            f"💰 You save: <b>{saved_amt:.6f} {sym_dep}</b>\n"
        )
    else:
        fee_info = f"💸 Fee on release: <b>{state.fee_percent}%</b>\n"

    if not state.oxapay_key:
        demo_addr = f"DEMO_{did[:8]}"
        deal["deposit_address"] = demo_addr
        deal["status"] = "AWAITING_DEPOSIT"
        await send_qr(ctx, chat.id, demo_addr,
            f"🔧 <b>DEMO DEPOSIT ADDRESS</b>\n\n"
            f"🪙 Token: {TOKEN_LABELS.get(deal.get('token',''), deal.get('token',''))}\n"
            f"💰 Amount: {qty_float} {sym_dep}\n\n"
            f"📬 <b>Escrow Deposit Address:</b>\n<code>{demo_addr}</code>\n\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"🛒 <b>Buyer:</b> @{deal.get('buyer_username', 'N/A')}\n"
            f"📥 Buyer Wallet: <code>{deal.get('buyer_address', 'N/A')}</code>\n\n"
            f"🏪 <b>Seller:</b> @{deal.get('seller_username', 'N/A')}\n"
            f"📤 Seller Wallet: <code>{deal.get('seller_address', 'N/A')}</code>\n\n"
            f"{fee_info}"
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
        def _req():
            req = urllib.request.Request(
                "https://api.oxapay.com/merchants/request",
                data=_json.dumps({
                    "merchant":    state.oxapay_key,
                    "amount":      qty_float,
                    "currency":    currency,
                    "network":     network,
                    "description": f"Escrow {did}",
                    "lifeTime":    60
                }).encode(),
                headers={"Content-Type": "application/json"}
            )
            with urllib.request.urlopen(req, timeout=15) as r:
                return _json.loads(r.read().decode())
        data = await loop.run_in_executor(None, _req)
        logger.info(f"OxaPay request response for {did}: {data}")
        if data.get("result") != 100:
            raise Exception(data.get("message", f"OxaPay error code: {data.get('result')}"))
        address  = data.get("address") or data.get("payAddress") or ""
        track_id = data.get("trackId") or data.get("track_id") or ""
        if not address:
            raise Exception(f"No address in OxaPay response: {data}")
        deal["deposit_address"]  = address
        deal["oxapay_track_id"]  = track_id
        deal["status"] = "AWAITING_DEPOSIT"
        await send_qr(ctx, chat.id, address,
            f"✅ <b>DEPOSIT ADDRESS READY</b>\n\n"
            f"🪙 Token: {TOKEN_LABELS.get(deal['token'], deal['token'])}\n"
            f"💰 Amount: <b>{qty_float} {sym_dep}</b>\n\n"
            f"📬 <b>Escrow Deposit Address:</b>\n<code>{address}</code>\n\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"🛒 Buyer: @{deal.get('buyer_username', 'N/A')}\n"
            f"📥 Buyer Wallet: <code>{deal.get('buyer_address', 'N/A')}</code>\n\n"
            f"🏪 Seller: @{deal.get('seller_username', 'N/A')}\n"
            f"📤 Seller Wallet: <code>{deal.get('seller_address', 'N/A')}</code>\n\n"
            f"{fee_info}"
            f"⚠️ <b>SELLER</b>: Send EXACT amount to the escrow address above.\n\n"
            f"➡️ After sending, use <b>/verify</b> to confirm OxaPay payment."
        )
    except Exception as e:
        logger.error(f"OxaPay deposit failed for {did}: {e}")
        await update.message.reply_text(
            f"❌ <b>OxaPay Error:</b> <code>{e}</code>\n\n"
            f"Check your OxaPay API key and try again.\n"
            f"Use <b>/checkoxapay</b> to test the connection.",
            parse_mode="HTML"
        )
        def _req():
            req = urllib.request.Request(
                "https://api.oxapay.com/merchants/request",
                data=_json.dumps({
                    "merchant":    state.oxapay_key,
                    "amount":      qty_float,
                    "currency":    currency,
                    "network":     network,
                    "description": f"Escrow {did}",
                    "lifeTime":    60
                }).encode(),
                headers={"Content-Type": "application/json"}
            )
            with urllib.request.urlopen(req, timeout=15) as r:
                return _json.loads(r.read().decode())
        data = await loop.run_in_executor(None, _req)
        logger.info(f"OxaPay request response for {did}: {data}")
        if data.get("result") != 100:
            raise Exception(data.get("message", f"OxaPay error code: {data.get('result')}"))
        # OxaPay returns field "address" (not "payAddress")
        address  = data.get("address") or data.get("payAddress") or data.get("payAddress") or ""
        track_id = data.get("trackId") or data.get("track_id") or ""
        if not address:
            raise Exception(f"No address in OxaPay response: {data}")
        deal["deposit_address"]  = address
        deal["oxapay_track_id"]  = track_id
        deal["status"] = "AWAITING_DEPOSIT"
        sym = TOKEN_SYMBOL.get(deal["token"], deal["token"])
        await send_qr(ctx, chat.id, address,
            f"✅ <b>DEPOSIT ADDRESS READY</b>\n\n"
            f"🪙 Token: {TOKEN_LABELS.get(deal['token'], deal['token'])}\n"
            f"💰 Amount: <b>{qty_float} {sym}</b>\n\n"
            f"📬 <b>Escrow Deposit Address:</b>\n<code>{address}</code>\n\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"🛒 Buyer: @{deal.get('buyer_username', 'N/A')}\n"
            f"📥 Buyer Wallet: <code>{deal.get('buyer_address', 'N/A')}</code>\n\n"
            f"🏪 Seller: @{deal.get('seller_username', 'N/A')}\n"
            f"📤 Seller Wallet: <code>{deal.get('seller_address', 'N/A')}</code>\n\n"
            f"⚠️ <b>SELLER</b>: Send EXACT amount to the escrow address above.\n\n"
            f"➡️ After sending, use <b>/verify</b> to confirm OxaPay payment."
        )
    except Exception as e:
        logger.error(f"OxaPay deposit failed for {did}: {e}")
        await update.message.reply_text(
            f"❌ <b>OxaPay Error:</b> <code>{e}</code>\n\n"
            f"Check your OxaPay API key and try again.\n"
            f"Use <b>/checkoxapay</b> to test the connection.",
            parse_mode="HTML"
        )

# ══════════════════════════════════════════════════════════
# STEP 7: /verify — OxaPay payment check
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
        if deal["status"] in ("SETUP", "FORM_FILLED", "ROLES_SET", "TOKEN_SELECTED"):
            await update.message.reply_text(
                "⚠️ <b>Seller must deposit first!</b>\n\nUse <b>/deposit</b> to generate an escrow address, then seller sends crypto to it.",
                parse_mode="HTML"
            )
        else:
            await update.message.reply_text(
                f"⚠️ Cannot verify at this stage. Status: <b>{deal['status']}</b>",
                parse_mode="HTML"
            )
        return

    # DEMO mode
    if not state.oxapay_key or deal.get("deposit_address", "").startswith("DEMO_"):
        deal["funded"]    = True
        deal["status"]    = "FUNDED"
        deal["funded_by"] = user.username or user.first_name
        deal["funded_at"] = datetime.utcnow().isoformat()
        await update.message.reply_text(
            f"✅ <b>[DEMO] Payment Marked as Funded</b>\n\n"
            f"🆔 Trade ID: <code>{did}</code>\n"
            f"🪙 Token: {deal.get('token')}\n"
            f"💰 Amount: {deal.get('quantity')}\n\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"🛒 <b>Buyer:</b> @{deal.get('buyer_username', 'N/A')}\n"
            f"📥 Buyer Wallet: <code>{deal.get('buyer_address', 'N/A')}</code>\n\n"
            f"🏪 <b>Seller:</b> @{deal.get('seller_username', 'N/A')}\n"
            f"📤 Seller Wallet: <code>{deal.get('seller_address', 'N/A')}</code>\n\n"
            f"📌 <b>Buyer:</b> Now send the agreed fiat/payment to the seller off-platform.\n"
            f"Once done, either party runs <b>/release</b> to proceed.",
            parse_mode="HTML"
        )
        await log(ctx,
            f"💰 <b>DEAL FUNDED (DEMO)</b>\n\n"
            f"🆔 Trade ID: <code>{did}</code>\n"
            f"🪙 Token: {deal.get('token')}  💵 Amount: {deal.get('quantity')}\n"
            f"📈 Rate: {deal.get('rate')}  📝 Condition: {deal.get('condition')}\n\n"
            f"🛒 Buyer: @{deal.get('buyer_username')} ({deal.get('buyer_id')})\n"
            f"📥 Buyer Wallet: <code>{deal.get('buyer_address', 'N/A')}</code>\n\n"
            f"🏪 Seller: @{deal.get('seller_username')} ({deal.get('seller_id')})\n"
            f"📤 Seller Wallet: <code>{deal.get('seller_address', 'N/A')}</code>\n\n"
            f"📬 Escrow Address: <code>{deal.get('deposit_address', 'N/A')}</code>\n"
            f"👤 Verified by: @{deal['funded_by']}\n"
            f"⏰ {deal['funded_at']}\n📊 Status: FUNDED"
        )
        return

    # Live — OxaPay inquiry
    track_id = deal.get("oxapay_track_id")
    if not track_id:
        await update.message.reply_text("❌ No OxaPay tracking ID. Run <b>/deposit</b> again.", parse_mode="HTML")
        return

    await update.message.reply_text("⏳ Checking OxaPay payment status…")
    try:
        loop = asyncio.get_event_loop()
        def _inquiry():
            req = urllib.request.Request(
                "https://api.oxapay.com/merchants/inquiry",
                data=_json.dumps({"merchant": state.oxapay_key, "trackId": track_id}).encode(),
                headers={"Content-Type": "application/json"}
            )
            with urllib.request.urlopen(req, timeout=15) as r:
                return _json.loads(r.read().decode())
        data = await loop.run_in_executor(None, _inquiry)
        logger.info(f"OxaPay inquiry response for {did}: {data}")

        if data.get("result") != 100:
            await update.message.reply_text(f"⚠️ OxaPay error: {data.get('message', 'Unknown')}. Try again.", parse_mode="HTML")
            return

        # OxaPay may return "status" or "paymentStatus"
        pay_status = (data.get("status") or data.get("paymentStatus") or "").lower()
        if pay_status != "paid":
            labels = {
                "waiting": "⏳ Waiting — payment not received yet",
                "expired": "⌛ Expired — run /deposit again for a new address",
                "failed":  "❌ Failed",
            }
            await update.message.reply_text(
                f"🔍 <b>Payment Status: {labels.get(pay_status, pay_status)}</b>\n\n"
                f"📬 <code>{deal.get('deposit_address')}</code>\n"
                f"💰 Expected: {deal.get('quantity')} {deal.get('token')}\n\n"
                f"Wait for blockchain confirmation, then try <b>/verify</b> again.",
                parse_mode="HTML"
            )
            return

        deal["funded"]    = True
        deal["status"]    = "FUNDED"
        deal["funded_by"] = user.username or user.first_name
        deal["funded_at"] = datetime.utcnow().isoformat()
        await update.message.reply_text(
            f"✅ <b>OxaPay Payment Confirmed!</b>\n\n"
            f"🆔 Trade ID: <code>{did}</code>\n"
            f"🪙 Token: {deal.get('token')}\n"
            f"💰 Amount: {deal.get('quantity')}\n\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"🛒 <b>Buyer:</b> @{deal.get('buyer_username', 'N/A')}\n"
            f"📥 Buyer Wallet: <code>{deal.get('buyer_address', 'N/A')}</code>\n\n"
            f"🏪 <b>Seller:</b> @{deal.get('seller_username', 'N/A')}\n"
            f"📤 Seller Wallet: <code>{deal.get('seller_address', 'N/A')}</code>\n\n"
            f"📌 <b>Buyer:</b> Now send the agreed fiat/payment to the seller off-platform.\n\n"
            f"Once buyer has paid, either party runs <b>/release</b> to start final confirmation.",
            parse_mode="HTML"
        )
        await log(ctx,
            f"💰 <b>DEAL FUNDED</b>\n\n"
            f"🆔 Trade ID: <code>{did}</code>\n"
            f"🪙 Token: {deal.get('token')}  💵 Amount: {deal.get('quantity')}\n"
            f"📈 Rate: {deal.get('rate')}  📝 Condition: {deal.get('condition')}\n\n"
            f"🛒 Buyer: @{deal.get('buyer_username')} ({deal.get('buyer_id')})\n"
            f"📥 Buyer Wallet: <code>{deal.get('buyer_address', 'N/A')}</code>\n\n"
            f"🏪 Seller: @{deal.get('seller_username')} ({deal.get('seller_id')})\n"
            f"📤 Seller Wallet: <code>{deal.get('seller_address', 'N/A')}</code>\n\n"
            f"📬 Escrow Address: <code>{deal.get('deposit_address', 'N/A')}</code>\n"
            f"🔎 OxaPay Track ID: {deal.get('oxapay_track_id', 'N/A')}\n"
            f"👤 Verified by: @{deal['funded_by']}\n"
            f"⏰ {deal['funded_at']}\n📊 Status: FUNDED"
        )
    except Exception as e:
        await update.message.reply_text(f"❌ OxaPay check failed: {e}\n\nTry again.", parse_mode="HTML")

# ══════════════════════════════════════════════════════════
# STEP 8: /release — buyer or seller triggers confirmation
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
        await update.message.reply_text(
            "⚠️ <b>Payment not verified yet!</b>\n\n"
            "Use <b>/verify</b> to confirm OxaPay payment first.",
            parse_mode="HTML"
        )
        return
    if deal["status"] == "COMPLETED":
        await update.message.reply_text("⚠️ Deal already completed.")
        return
    if deal["status"] == "CANCELLED":
        await update.message.reply_text("⚠️ Deal has been cancelled.")
        return
    if deal["status"] == "DISPUTED":
        await update.message.reply_text("⚠️ Deal is under dispute. Wait for admin resolution.")
        return
    if deal["status"] == "AWAITING_CONFIRMATION":
        await update.message.reply_text("⚠️ Confirmation already started. Both parties must press Confirm.")
        return
    if deal["status"] != "FUNDED":
        await update.message.reply_text(f"⚠️ Cannot release at this stage. Status: <b>{deal['status']}</b>", parse_mode="HTML")
        return
    if user.id not in (deal.get("buyer_id"), deal.get("seller_id")):
        await update.message.reply_text("❌ Only the buyer or seller can run /release.")
        return

    who = "Buyer" if user.id == deal.get("buyer_id") else "Seller"
    deal["buyer_confirmed"]  = False
    deal["seller_confirmed"] = False
    deal["status"]           = "AWAITING_CONFIRMATION"
    deal["release_by"]       = user.username or user.first_name
    deal["release_at"]       = datetime.utcnow().isoformat()

    # Auto-confirm the party who triggered /release
    if user.id == deal.get("buyer_id"):
        deal["buyer_confirmed"] = True
    elif user.id == deal.get("seller_id"):
        deal["seller_confirmed"] = True

    b = deal["buyer_confirmed"]
    s = deal["seller_confirmed"]

    # If somehow both are already confirmed (shouldn't happen normally), release immediately
    if b and s:
        await update.message.reply_text(
            "🎉 <b>BOTH CONFIRMED!</b>\n\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "✅ Buyer — Confirmed\n"
            "✅ Seller — Confirmed\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            "⏳ Processing release to buyer's wallet…",
            parse_mode="HTML"
        )
        await release_deal(ctx, did, deal, chat.id)
        return

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(f"{'✅' if b else '⏳'} Buyer Confirm",  callback_data=f"confirm:buyer:{did}"),
         InlineKeyboardButton(f"{'✅' if s else '⏳'} Seller Confirm", callback_data=f"confirm:seller:{did}")],
        [InlineKeyboardButton("🚨 Dispute / Call Admin", callback_data=f"dispute_call:{did}")]
    ])
    await update.message.reply_text(
        f"🔓 <b>RELEASE INITIATED</b>\n\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"Triggered by: <b>{who}</b> (@{deal['release_by']})\n"
        f"🆔 Trade ID: <code>{did}</code>\n"
        f"🪙 Token: {deal.get('token')}  💰 Amount: {deal.get('quantity')} {TOKEN_SYMBOL.get(deal.get('token',''), '')}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n\n"
        f"🛒 <b>Buyer:</b> @{deal.get('buyer_username', 'N/A')} — {'✅ Confirmed' if b else '⏳ Waiting'}\n"
        f"📥 Buyer Wallet: <code>{deal.get('buyer_address', 'N/A')}</code>\n\n"
        f"🏪 <b>Seller:</b> @{deal.get('seller_username', 'N/A')} — {'✅ Confirmed' if s else '⏳ Waiting'}\n"
        f"📤 Seller Wallet: <code>{deal.get('seller_address', 'N/A')}</code>\n\n"
        f"⚠️ <b>Waiting for the other party to confirm.</b>\n\n"
        f"Issue? Press 🚨 Dispute to call admin.",
        reply_markup=kb, parse_mode="HTML"
    )
    await log(ctx,
        f"🔓 <b>RELEASE INITIATED</b>\n\n"
        f"🆔 Trade ID: <code>{did}</code>\n"
        f"👤 By: @{deal['release_by']} ({who}) — ✅ Auto-confirmed\n\n"
        f"🛒 Buyer: @{deal.get('buyer_username')} ({deal.get('buyer_id')}) — {'✅' if b else '⏳'}\n"
        f"📥 Buyer Wallet: <code>{deal.get('buyer_address', 'N/A')}</code>\n\n"
        f"🏪 Seller: @{deal.get('seller_username')} ({deal.get('seller_id')}) — {'✅' if s else '⏳'}\n"
        f"📤 Seller Wallet: <code>{deal.get('seller_address', 'N/A')}</code>\n\n"
        f"🪙 Token: {deal.get('token')}  💰 Amount: {deal.get('quantity')} {TOKEN_SYMBOL.get(deal.get('token',''), '')}\n"
        f"⏰ {deal['release_at']}\n📊 Status: AWAITING_CONFIRMATION"
    )

# ══════════════════════════════════════════════════════════
# STEP 9: CONFIRMATION BUTTONS
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
        await q.answer("❌ Run /release first to start confirmation.", show_alert=True)
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
            "🎉 <b>BOTH CONFIRMED!</b>\n\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "✅ Buyer — Confirmed\n"
            "✅ Seller — Confirmed\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            "⏳ Processing release to buyer's wallet…",
            parse_mode="HTML"
        )
        await release_deal(ctx, did, deal, q.message.chat_id)
    else:
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton(f"{'✅' if b else '⏳'} Buyer Confirm",  callback_data=f"confirm:buyer:{did}"),
             InlineKeyboardButton(f"{'✅' if s else '⏳'} Seller Confirm", callback_data=f"confirm:seller:{did}")],
            [InlineKeyboardButton("🚨 Dispute / Call Admin", callback_data=f"dispute_call:{did}")]
        ])
        await q.edit_message_text(
            f"📊 <b>CONFIRMATION STATUS</b>\n\n"
            f"🆔 <code>{did}</code>\n"
            f"🪙 Token: {deal.get('token')}  💰 Amount: {deal.get('quantity')}\n\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"🛒 Buyer: @{deal.get('buyer_username', 'N/A')} — {'✅ Confirmed' if b else '⏳ Waiting'}\n"
            f"📥 Buyer Wallet: <code>{deal.get('buyer_address', 'N/A')}</code>\n\n"
            f"🏪 Seller: @{deal.get('seller_username', 'N/A')} — {'✅ Confirmed' if s else '⏳ Waiting'}\n"
            f"📤 Seller Wallet: <code>{deal.get('seller_address', 'N/A')}</code>\n\n"
            f"⚠️ Both must confirm for crypto to release to buyer's wallet.",
            reply_markup=kb, parse_mode="HTML"
        )

# ══════════════════════════════════════════════════════════
# STEP 10: RELEASE — funds go to buyer's address
# ══════════════════════════════════════════════════════════

async def release_deal(ctx, did, deal, group_id):
    token       = deal.get("token", "")
    sym         = TOKEN_SYMBOL.get(token, token)
    token_label = TOKEN_LABELS.get(token, token)

    # ── Bio discount check (buyer OR seller) ──
    discount_applied = False
    discount_reason  = ""
    discount_pct     = state.bio_discount_percent if hasattr(state, "bio_discount_percent") else state.fee_percent  # default: full discount
    if state.required_bio:
        for chk_role, chk_id in (("buyer", deal.get("buyer_id")), ("seller", deal.get("seller_id"))):
            if not chk_id:
                continue
            try:
                chk_chat = await ctx.bot.get_chat(chk_id)
                bio = getattr(chk_chat, "bio", "") or ""
                if state.required_bio.lower() in bio.lower():
                    discount_applied = True
                    discount_reason  = f"{chk_role.capitalize()}'s bio contains «{state.required_bio}»"
                    break
            except Exception:
                pass

    qty        = float(deal.get("quantity", 0))
    fee_pct    = state.fee_percent
    if discount_applied:
        effective_fee_pct = state.bio_discount_percent  # admin-set discounted rate
        fee_amt = qty * (effective_fee_pct / 100)
    else:
        effective_fee_pct = fee_pct
        fee_amt = qty * (fee_pct / 100)
    final      = qty - fee_amt

    buyer_addr  = deal.get("buyer_address", "N/A")
    seller_addr = deal.get("seller_address", "N/A")

    # IST time
    from datetime import timezone, timedelta
    IST = timezone(timedelta(hours=5, minutes=30))
    completed_ist = datetime.now(IST).strftime("%d %b %Y, %I:%M %p IST")

    # ── OxaPay Payout: transfer final amount to buyer's wallet ──
    payout_success = False
    payout_txid    = None
    payout_err     = None

    TOKEN_NET_MAP = {
        "USDT_TRC20": ("USDT", "TRX"),
        "USDT_BEP20": ("USDT", "BSC"),
        "BTC":        ("BTC",  "BTC"),
        "LTC":        ("LTC",  "LTC"),
    }
    currency, network = TOKEN_NET_MAP.get(token, ("USDT", "TRX"))

    if state.oxapay_key and buyer_addr and buyer_addr != "N/A" and final > 0:
        try:
            loop = asyncio.get_event_loop()
            def _payout():
                req = urllib.request.Request(
                    "https://api.oxapay.com/merchants/payout",
                    data=_json.dumps({
                        "merchant":    state.oxapay_key,
                        "address":     buyer_addr,
                        "amount":      round(final, 8),
                        "currency":    currency,
                        "network":     network,
                        "description": f"Escrow Release {did}",
                    }).encode(),
                    headers={"Content-Type": "application/json"}
                )
                with urllib.request.urlopen(req, timeout=30) as r:
                    return _json.loads(r.read().decode())
            pdata = await loop.run_in_executor(None, _payout)

            if pdata.get("result") == 100:
                payout_success = True
                payout_txid    = pdata.get("trackId") or pdata.get("txID") or "N/A"
            else:
                payout_err = pdata.get("message", "Unknown OxaPay error")
                logger.error(f"OxaPay payout failed for {did}: {payout_err}")
        except Exception as e:
            payout_err = str(e)
            logger.error(f"OxaPay payout exception for {did}: {e}")
    else:
        # DEMO mode — no real payout
        payout_success = True
        payout_txid    = "DEMO_MODE"

    # If payout failed, notify admin and pause — do NOT delete group
    if not payout_success:
        try:
            await ctx.bot.send_message(
                chat_id=group_id,
                text=(
                    f"⚠️ <b>PAYOUT FAILED — Admin Notified</b>\n\n"
                    f"OxaPay could not send funds to buyer.\n"
                    f"Error: <code>{payout_err}</code>\n\n"
                    f"Admin will resolve this manually.\n"
                    f"🆔 Deal: <code>{did}</code>"
                ),
                parse_mode="HTML"
            )
        except Exception:
            pass
        await log(ctx,
            f"🚨 <b>PAYOUT FAILED</b>\n\n"
            f"🆔 Deal: <code>{did}</code>\n"
            f"📥 Buyer Wallet: <code>{buyer_addr}</code>\n"
            f"💰 Amount: {final:.6f} {sym}\n"
            f"❌ Error: {payout_err}\n\n"
            f"⚠️ Manual intervention required!"
        )
        return  # Stop here — group stays alive for admin to fix

    deal["status"]       = "COMPLETED"
    deal["final_amount"] = final
    deal["fee_deducted"] = fee_amt
    deal["completed_at"] = datetime.utcnow().isoformat()
    deal["payout_txid"]  = payout_txid

    # ── Fee breakdown lines ──
    if discount_applied:
        saved = qty * (fee_pct / 100) - fee_amt
        fee_line = (
            f"💸 Fee: ~~{fee_pct}%~~ → <b>{effective_fee_pct}% (Bio Discount ✅)</b>\n"
            f"🏷 Reason: {discount_reason}\n"
            f"💰 Fee Saved: <b>{saved:.6f} {sym}</b>"
        )
    else:
        fee_line = (
            f"💸 Fee: <b>{fee_pct}%</b> = <b>{fee_amt:.6f} {sym}</b>"
        )

    tx_line = f"🔗 Payout TX: <code>{payout_txid}</code>\n" if payout_txid and payout_txid != "DEMO_MODE" else "🧪 Mode: DEMO (no real transfer)\n"

    completion_msg = (
        f"🎉 <b>DEAL COMPLETED!</b>\n\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🆔 Trade ID: <code>{did}</code>\n"
        f"🪙 Token: {token_label}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n\n"
        f"💰 <b>Amount Breakdown:</b>\n"
        f"   Original:  <b>{qty:.6f} {sym}</b>\n"
        f"   {fee_line}\n"
        f"   Released:  <b>{final:.6f} {sym}</b>\n\n"
        f"✅ <b>Funds Sent to Buyer Wallet!</b>\n"
        f"{tx_line}"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🛒 <b>Buyer:</b> @{deal.get('buyer_username', 'N/A')}\n"
        f"📥 Wallet: <code>{buyer_addr}</code>\n\n"
        f"🏪 <b>Seller:</b> @{deal.get('seller_username', 'N/A')}\n"
        f"📤 Wallet: <code>{seller_addr}</code>\n\n"
        f"⏰ {completed_ist}\n\n"
        f"✨ Thank you for using P2P Escrow!\n"
        f"<i>Group will be deleted in 20 seconds.</i>"
    )

    try:
        await ctx.bot.send_message(chat_id=group_id, text=completion_msg, parse_mode="HTML")
    except Exception:
        pass

    # ── DM both parties ──
    dm_msg = (
        f"✅ <b>Deal Completed!</b>\n\n"
        f"🆔 Trade ID: <code>{did}</code>\n"
        f"🪙 Token: {token_label}\n\n"
        f"💰 <b>Amount Breakdown:</b>\n"
        f"   Original:  {qty:.6f} {sym}\n"
        f"   Fee ({fee_pct}%{'→0% Discount✅' if discount_applied else ''}):  {fee_amt:.6f} {sym}\n"
        f"   Released:  <b>{final:.6f} {sym}</b>\n\n"
        f"🛒 Buyer: @{deal.get('buyer_username', 'N/A')}\n"
        f"📥 Buyer Wallet: <code>{buyer_addr}</code>\n\n"
        f"🏪 Seller: @{deal.get('seller_username', 'N/A')}\n"
        f"📤 Seller Wallet: <code>{seller_addr}</code>\n\n"
        f"⏰ {completed_ist}"
    )
    for p in ("buyer", "seller"):
        pid = deal.get(f"{p}_id")
        if pid:
            try:
                await ctx.bot.send_message(chat_id=pid, text=dm_msg, parse_mode="HTML")
            except Exception:
                pass

    # ── Admin log ──
    await log(ctx,
        f"✅ <b>DEAL COMPLETED</b>\n\n"
        f"🆔 Trade ID: <code>{did}</code>\n"
        f"📈 Rate: {deal.get('rate')}  📝 Condition: {deal.get('condition')}\n\n"
        f"🛒 Buyer: @{deal.get('buyer_username')} ({deal.get('buyer_id')})\n"
        f"📥 Buyer Wallet: <code>{buyer_addr}</code>\n\n"
        f"🏪 Seller: @{deal.get('seller_username')} ({deal.get('seller_id')})\n"
        f"📤 Seller Wallet: <code>{seller_addr}</code>\n\n"
        f"🪙 Token: {token_label}\n"
        f"💰 Original: {qty:.6f} {sym}\n"
        f"💸 Fee ({fee_pct}%{' — DISCOUNTED→0%' if discount_applied else ''}): {fee_amt:.6f} {sym}\n"
        f"✅ Final Released: {final:.6f} {sym}\n"
        f"🔗 Payout TX: <code>{payout_txid}</code>\n"
        f"📬 Escrow Address: <code>{deal.get('deposit_address', 'N/A')}</code>\n"
        f"📦 Group: <code>{group_id}</code>\n"
        f"📊 Status: COMPLETED\n"
        f"⏰ {completed_ist}"
    )

    await asyncio.sleep(10)
    try:
        await ctx.bot.send_message(chat_id=group_id, text="🗑 <b>Group closing in 10 seconds. Thank you!</b>", parse_mode="HTML")
        await asyncio.sleep(10)

        # ── Kick buyer and seller before bot leaves ──
        for p in ("buyer", "seller"):
            pid = deal.get(f"{p}_id")
            if pid:
                try:
                    await ctx.bot.ban_chat_member(chat_id=group_id, user_id=pid)
                    await ctx.bot.unban_chat_member(chat_id=group_id, user_id=pid)
                except Exception as e:
                    logger.warning(f"Could not kick {p} ({pid}) from group: {e}")

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
# STEP 11: DISPUTE
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
        f"⏳ Admin notified and will join shortly.",
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
    """Inline dispute button in the group (dispute_call:{did})."""
    q = update.callback_query
    parts = q.data.split(":", 1)
    did = parts[1] if len(parts) > 1 else None
    chat_id = q.message.chat_id

    deal = deal_by_id(did) if did else None
    if not deal:
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
        f"Admin notified and will join shortly. Please remain in the group.",
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
    """Admin clicks 'Add Me to Group' — generates 1-use invite link for that admin only."""
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

    group_id = deal.get("group_id")

    # ── Generate 1-use invite link only for this admin ──
    invite_link = None
    try:
        link_obj = await ctx.bot.create_chat_invite_link(
            chat_id=group_id,
            member_limit=1,
            name=f"Admin {user.username or user.id} — {did}"
        )
        invite_link = link_obj.invite_link
    except Exception as e:
        logger.warning(f"Could not generate admin dispute invite: {e}")

    link_line = f"🔗 <b>Join Link (1-use, only for you):</b>\n{invite_link}" if invite_link else \
                f"⚠️ Could not generate link. Group ID: <code>{group_id}</code>"

    # ── Deal summary for admin ──
    buyer_addr  = deal.get("buyer_address", "N/A")
    seller_addr = deal.get("seller_address", "N/A")
    qty         = deal.get("quantity", "N/A")
    token       = deal.get("token", "N/A")

    await q.edit_message_text(
        f"🚨 <b>DISPUTE — You are handling this</b>\n\n"
        f"🆔 Trade ID: <code>{did}</code>\n"
        f"📊 Status: <b>{deal.get('status')}</b>\n"
        f"🪙 Token: {token}  💰 Amount: {qty}\n"
        f"📈 Rate: {deal.get('rate', 'N/A')}  📝 Condition: {deal.get('condition', 'N/A')}\n\n"
        f"🛒 Buyer: @{deal.get('buyer_username', 'N/A')} ({deal.get('buyer_id', 'N/A')})\n"
        f"📥 Buyer Wallet: <code>{buyer_addr}</code>\n\n"
        f"🏪 Seller: @{deal.get('seller_username', 'N/A')} ({deal.get('seller_id', 'N/A')})\n"
        f"📤 Seller Wallet: <code>{seller_addr}</code>\n\n"
        f"⚠️ Dispute by: @{deal.get('dispute_by', 'N/A')}\n"
        f"📝 Reason: {deal.get('dispute_reason', 'N/A')}\n\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"{link_line}\n\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📋 <b>Commands (copy-paste ready):</b>\n\n"
        f"Release to buyer:\n<code>/adminrelease buyer {did}</code>\n\n"
        f"Release to seller:\n<code>/adminrelease seller {did}</code>\n\n"
        f"Refund (cancel deal):\n<code>/refund {did}</code>\n\n"
        f"Close dispute (resume deal):\n<code>/disputeend {did}</code>",
        parse_mode="HTML"
    )

    # ── Notify group: admin is joining ──
    try:
        await ctx.bot.send_message(
            chat_id=group_id,
            text=(
                f"👨‍💼 <b>Admin @{user.username or 'Admin'} is joining to handle the dispute.</b>\n\n"
                f"🆔 Trade ID: <code>{did}</code>\n"
                f"🪙 Token: {token}  💰 Amount: {qty}\n\n"
                f"🛒 Buyer: @{deal.get('buyer_username', 'N/A')}\n"
                f"📥 Buyer Wallet: <code>{buyer_addr}</code>\n\n"
                f"🏪 Seller: @{deal.get('seller_username', 'N/A')}\n"
                f"📤 Seller Wallet: <code>{seller_addr}</code>\n\n"
                f"Please remain in the group. Admin will resolve the dispute.\n\n"
                f"<i>Admin commands available in this group:</i>\n"
                f"<code>/adminrelease buyer {did}</code> — release to buyer\n"
                f"<code>/adminrelease seller {did}</code> — release to seller\n"
                f"<code>/refund {did}</code> — cancel & refund\n"
                f"<code>/disputeend {did}</code> — close dispute, resume deal"
            ),
            parse_mode="HTML"
        )
    except Exception:
        pass

# ══════════════════════════════════════════════════════════
# ADMIN-ONLY COMMANDS
# ══════════════════════════════════════════════════════════

async def cmd_adminrelease(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Admin force-releases funds to buyer or seller."""
    if not is_admin(update.effective_user.id):
        return
    if len(ctx.args) < 2:
        await update.message.reply_text(
            "Usage: <code>/adminrelease buyer|seller DEAL_ID</code>", parse_mode="HTML"
        )
        return
    party = ctx.args[0].lower()
    did   = ctx.args[1].upper()
    if party not in ("buyer", "seller"):
        await update.message.reply_text("❌ Must be <b>buyer</b> or <b>seller</b>.", parse_mode="HTML")
        return
    deal = deal_by_id(did)
    if not deal:
        await update.message.reply_text(f"❌ Deal not found: <code>{did}</code>", parse_mode="HTML")
        return
    if deal.get("status") == "COMPLETED":
        await update.message.reply_text("⚠️ Deal already completed.")
        return

    user = update.effective_user
    assigned = state.dispute_admins.get(did)
    if assigned and assigned != user.id and not is_main_admin(user.id):
        await update.message.reply_text("❌ Another admin is handling this dispute.")
        return

    group_id    = deal.get("group_id")
    qty         = float(deal.get("quantity", 0))
    fee_amt     = qty * (state.fee_percent / 100)
    final       = qty - fee_amt
    to_user     = deal.get(f"{party}_username", "N/A")
    to_addr     = deal.get(f"{party}_address", "N/A")
    other_party = "seller" if party == "buyer" else "buyer"
    other_addr  = deal.get(f"{other_party}_address", "N/A")
    other_user  = deal.get(f"{other_party}_username", "N/A")
    sym         = TOKEN_SYMBOL.get(deal.get("token", ""), deal.get("token", ""))
    token_label = TOKEN_LABELS.get(deal.get("token", ""), deal.get("token", ""))

    from datetime import timezone, timedelta
    IST = timezone(timedelta(hours=5, minutes=30))
    completed_ist = datetime.now(IST).strftime("%d %b %Y, %I:%M %p IST")

    deal["status"]            = "COMPLETED"
    deal["force_released_to"] = party
    deal["fee_deducted"]      = fee_amt
    deal["final_amount"]      = final
    deal["completed_at"]      = datetime.utcnow().isoformat()

    # ── Notify the group ──
    try:
        await ctx.bot.send_message(
            chat_id=group_id,
            text=(
                f"⚖️ <b>ADMIN DECISION — DEAL RESOLVED</b>\n\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"👨‍💼 Admin: @{user.username or user.id}\n"
                f"⚖️ Released to: <b>{party.upper()}</b>\n\n"
                f"🆔 Trade ID: <code>{did}</code>\n"
                f"🪙 Token: {token_label}\n\n"
                f"💰 Original:  {qty:.6f} {sym}\n"
                f"💸 Fee ({state.fee_percent}%): {fee_amt:.6f} {sym}\n"
                f"✅ Released:  <b>{final:.6f} {sym}</b>\n\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"🛒 Buyer: @{deal.get('buyer_username', 'N/A')}\n"
                f"📥 Buyer Wallet: <code>{deal.get('buyer_address', 'N/A')}</code>\n\n"
                f"🏪 Seller: @{deal.get('seller_username', 'N/A')}\n"
                f"📤 Seller Wallet: <code>{deal.get('seller_address', 'N/A')}</code>\n\n"
                f"⏰ {completed_ist}\n"
                f"📊 COMPLETED — Group closes shortly."
            ),
            parse_mode="HTML"
        )
    except Exception:
        pass  # bot may not be in group yet if admin hasn't joined

    # ── Confirm to admin ──
    await update.message.reply_text(
        f"✅ <b>Force Released to {party.upper()}</b>\n\n"
        f"👤 @{to_user}\n"
        f"📬 Wallet: <code>{to_addr}</code>\n"
        f"💰 Released: {final:.6f} {sym} ({token_label})",
        parse_mode="HTML"
    )

    await log(ctx,
        f"⚖️ <b>ADMIN FORCE RELEASE</b>\n\n"
        f"🆔 Trade ID: <code>{did}</code>\n"
        f"⚖️ Released to: {party.upper()} (@{to_user})\n"
        f"📬 Wallet: <code>{to_addr}</code>\n\n"
        f"🛒 Buyer: @{deal.get('buyer_username')} — <code>{deal.get('buyer_address', 'N/A')}</code>\n"
        f"🏪 Seller: @{deal.get('seller_username')} — <code>{deal.get('seller_address', 'N/A')}</code>\n\n"
        f"🪙 Token: {token_label}\n"
        f"💰 Original: {qty:.6f} {sym}  💸 Fee: {fee_amt:.6f} {sym}  ✅ Final: {final:.6f} {sym}\n"
        f"👨‍💼 Admin: @{user.username or user.id}\n"
        f"📊 COMPLETED (Admin Force)\n⏰ {completed_ist}"
    )

    # ── DM both parties ──
    for p in ("buyer", "seller"):
        pid = deal.get(f"{p}_id")
        if pid:
            try:
                await ctx.bot.send_message(
                    chat_id=pid,
                    text=(
                        f"⚖️ <b>Admin resolved your deal: {did}</b>\n\n"
                        f"Released to: <b>{party.upper()}</b>\n"
                        f"Amount: {final:.4f} {deal.get('token', '')}\n"
                        f"📬 To wallet: <code>{to_addr}</code>"
                    ),
                    parse_mode="HTML"
                )
            except Exception:
                pass

    await asyncio.sleep(10)
    try:
        await ctx.bot.send_message(chat_id=group_id, text="🗑 <b>Group closing in 10 seconds.</b>", parse_mode="HTML")
        await asyncio.sleep(10)
        for p in ("buyer", "seller"):
            pid = deal.get(f"{p}_id")
            if pid:
                try:
                    await ctx.bot.ban_chat_member(chat_id=group_id, user_id=pid)
                    await ctx.bot.unban_chat_member(chat_id=group_id, user_id=pid)
                except Exception:
                    pass
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

async def cmd_refund(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Anyone in the deal (buyer, seller, or admin) can request a refund.
    If funded: requires BOTH parties to confirm. Fee deducted, remainder → seller.
    If not funded: admin-only cancel.
    """
    user = update.effective_user
    chat = update.effective_chat

    # Determine deal
    did  = None
    deal = None
    if ctx.args:
        did  = ctx.args[0].upper()
        deal = deal_by_id(did)
    elif chat.type != "private":
        did, deal = deal_by_group(chat.id)

    if not deal:
        await update.message.reply_text("Usage: <code>/refund DEAL_ID</code>  or run inside deal group.", parse_mode="HTML")
        return

    # Auth: admin OR buyer/seller of this deal
    is_participant = user.id in (deal.get("buyer_id"), deal.get("seller_id"))
    if not is_admin(user.id) and not is_participant:
        return

    if deal.get("status") == "COMPLETED":
        await update.message.reply_text("⚠️ Cannot refund a completed deal.")
        return
    if deal.get("status") == "REFUNDED":
        await update.message.reply_text("⚠️ Already refunded.")
        return
    if deal.get("status") == "AWAITING_REFUND_CONFIRM":
        await update.message.reply_text("⚠️ Refund already pending — check the deal group.")
        return

    # Must be funded to have a real refund; if not funded, only admin can cancel
    if not deal.get("funded"):
        if not is_admin(user.id):
            await update.message.reply_text("❌ Deal has not been funded yet. Only admin can cancel it.")
            return
        deal["status"]       = "REFUNDED"
        deal["refunded_by"]  = user.username or str(user.id)
        deal["refunded_at"]  = datetime.utcnow().isoformat()
        try:
            await ctx.bot.send_message(
                chat_id=deal["group_id"],
                text=(
                    f"🚫 <b>DEAL CANCELLED (No Payment Made)</b>\n\n"
                    f"👨‍💼 Admin: @{user.username or user.id}\n"
                    f"🆔 <code>{did}</code>\n\n"
                    f"No funds were deposited. Deal cancelled.\n📊 Status: CANCELLED"
                ),
                parse_mode="HTML"
            )
        except Exception:
            pass
        await update.message.reply_text(f"✅ Deal <code>{did}</code> cancelled (no deposit).", parse_mode="HTML")
        await log(ctx, f"🚫 <b>DEAL CANCELLED (no deposit)</b>\n\n🆔 <code>{did}</code>\n👨‍💼 @{user.username}")
        return

    # Deal IS funded — need both parties to confirm refund
    qty     = float(deal.get("quantity", 0))
    sym     = TOKEN_SYMBOL.get(deal.get("token", ""), deal.get("token", ""))
    fee_amt = qty * (state.fee_percent / 100)
    refund_amt = qty - fee_amt

    deal["status"]           = "AWAITING_REFUND_CONFIRM"
    deal["refund_buyer_ok"]  = False
    deal["refund_seller_ok"] = False
    deal["refunded_by"]      = user.username or str(user.id)

    who_requested = "Admin" if is_admin(user.id) else ("Buyer" if user.id == deal.get("buyer_id") else "Seller")

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Buyer Agrees Refund",  callback_data=f"refund_confirm:buyer:{did}"),
         InlineKeyboardButton("✅ Seller Agrees Refund", callback_data=f"refund_confirm:seller:{did}")],
        [InlineKeyboardButton("❌ Cancel Refund Request", callback_data=f"refund_cancel:{did}")]
    ])

    try:
        await ctx.bot.send_message(
            chat_id=deal["group_id"],
            text=(
                f"💸 <b>REFUND REQUESTED</b>\n\n"
                f"Requested by: <b>{who_requested}</b> (@{user.username or user.id})\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"🆔 Trade ID: <code>{did}</code>\n"
                f"🪙 Token: {TOKEN_LABELS.get(deal.get('token',''), deal.get('token',''))}\n\n"
                f"💰 Original:   {qty:.6f} {sym}\n"
                f"💸 Fee ({state.fee_percent}%): {fee_amt:.6f} {sym}\n"
                f"↩️ Refund to Seller: <b>{refund_amt:.6f} {sym}</b>\n\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"⚠️ <b>Both buyer and seller must confirm</b> to proceed.\n\n"
                f"🛒 Buyer: @{deal.get('buyer_username','N/A')} — ⏳ Waiting\n"
                f"🏪 Seller: @{deal.get('seller_username','N/A')} — ⏳ Waiting"
            ),
            reply_markup=kb,
            parse_mode="HTML"
        )
    except Exception as e:
        logger.error(f"Could not send refund request to group: {e}")

    await update.message.reply_text(
        f"✅ Refund request sent to group <code>{did}</code>.\nWaiting for both parties to confirm.",
        parse_mode="HTML"
    )
    await log(ctx,
        f"💸 <b>REFUND INITIATED</b>\n\n🆔 <code>{did}</code>\n"
        f"👨‍💼 @{deal['refunded_by']}\n"
        f"💰 {qty:.6f} {sym} → Refund: {refund_amt:.6f} {sym} (fee: {fee_amt:.6f})\n"
        f"📊 Awaiting confirmation from both parties"
    )


async def handle_refund_confirm(update: Update, ctx: ContextTypes.DEFAULT_TYPE, d: str):
    """Both buyer and seller must confirm refund."""
    q    = update.callback_query
    user = q.from_user
    parts = d.split(":")
    _, role, did = parts[0], parts[1], parts[2]
    deal = deal_by_id(did)
    if not deal:
        await q.answer("❌ Deal not found.", show_alert=True)
        return
    if deal.get("status") != "AWAITING_REFUND_CONFIRM":
        await q.answer("⚠️ Refund already processed or cancelled.", show_alert=True)
        return

    # Role check
    if role == "buyer" and user.id != deal.get("buyer_id"):
        await q.answer("❌ You are not the buyer.", show_alert=True)
        return
    if role == "seller" and user.id != deal.get("seller_id"):
        await q.answer("❌ You are not the seller.", show_alert=True)
        return

    deal[f"refund_{role}_ok"] = True
    await q.answer(f"✅ {role.capitalize()} confirmed refund!")

    b_ok = deal.get("refund_buyer_ok")
    s_ok = deal.get("refund_seller_ok")

    if not (b_ok and s_ok):
        # Update message to show partial confirmation
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton(f"{'✅' if b_ok else '⏳'} Buyer Confirms",  callback_data=f"refund_confirm:buyer:{did}"),
             InlineKeyboardButton(f"{'✅' if s_ok else '⏳'} Seller Confirms", callback_data=f"refund_confirm:seller:{did}")],
            [InlineKeyboardButton("❌ Cancel Refund Request", callback_data=f"refund_cancel:{did}")]
        ])
        qty     = float(deal.get("quantity", 0))
        sym     = TOKEN_SYMBOL.get(deal.get("token", ""), deal.get("token", ""))
        fee_amt = qty * (state.fee_percent / 100)
        refund_amt = qty - fee_amt
        await q.edit_message_text(
            f"💸 <b>REFUND CONFIRMATION</b>\n\n"
            f"🆔 <code>{did}</code>\n"
            f"💰 Original: {qty:.6f} {sym}\n"
            f"💸 Fee ({state.fee_percent}%): {fee_amt:.6f} {sym}\n"
            f"↩️ Refund to Seller: <b>{refund_amt:.6f} {sym}</b>\n\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"🛒 Buyer:  {'✅ Confirmed' if b_ok else '⏳ Waiting'}\n"
            f"🏪 Seller: {'✅ Confirmed' if s_ok else '⏳ Waiting'}",
            reply_markup=kb, parse_mode="HTML"
        )
        return

    # BOTH confirmed — process refund
    qty     = float(deal.get("quantity", 0))
    sym     = TOKEN_SYMBOL.get(deal.get("token", ""), deal.get("token", ""))
    token   = deal.get("token", "")
    token_label = TOKEN_LABELS.get(token, token)
    fee_amt = qty * (state.fee_percent / 100)
    refund_amt = qty - fee_amt
    seller_addr = deal.get("seller_address", "N/A")

    from datetime import timezone, timedelta
    IST = timezone(timedelta(hours=5, minutes=30))
    refunded_ist = datetime.now(IST).strftime("%d %b %Y, %I:%M %p IST")

    await q.edit_message_text(
        f"✅ <b>BOTH CONFIRMED — Processing Refund…</b>\n\n"
        f"🆔 <code>{did}</code>\n"
        f"↩️ Sending <b>{refund_amt:.6f} {sym}</b> to Seller…",
        parse_mode="HTML"
    )

    # OxaPay payout to seller
    TOKEN_NET_MAP = {
        "USDT_TRC20": ("USDT", "TRX"), "USDT_BEP20": ("USDT", "BSC"),
        "BTC": ("BTC", "BTC"), "LTC": ("LTC", "LTC"),
    }
    currency, network = TOKEN_NET_MAP.get(token, ("USDT", "TRX"))
    payout_success = False
    payout_txid    = None
    payout_err     = None

    if state.oxapay_key and seller_addr and seller_addr != "N/A" and refund_amt > 0:
        try:
            loop = asyncio.get_event_loop()
            def _refund_payout():
                req = urllib.request.Request(
                    "https://api.oxapay.com/merchants/payout",
                    data=_json.dumps({
                        "merchant":    state.oxapay_key,
                        "address":     seller_addr,
                        "amount":      round(refund_amt, 8),
                        "currency":    currency,
                        "network":     network,
                        "description": f"Escrow Refund {did}",
                    }).encode(),
                    headers={"Content-Type": "application/json"}
                )
                with urllib.request.urlopen(req, timeout=30) as r:
                    return _json.loads(r.read().decode())
            pdata = await loop.run_in_executor(None, _refund_payout)
            logger.info(f"OxaPay refund payout for {did}: {pdata}")
            if pdata.get("result") == 100:
                payout_success = True
                payout_txid = pdata.get("trackId") or pdata.get("txID") or "N/A"
            else:
                payout_err = pdata.get("message", "Unknown error")
        except Exception as e:
            payout_err = str(e)
    else:
        payout_success = True
        payout_txid = "DEMO_MODE"

    deal["status"]       = "REFUNDED"
    deal["refund_txid"]  = payout_txid
    deal["fee_deducted"] = fee_amt
    deal["refunded_at"]  = datetime.utcnow().isoformat()

    if not payout_success:
        try:
            await ctx.bot.send_message(
                chat_id=deal["group_id"],
                text=(
                    f"⚠️ <b>REFUND PAYOUT FAILED — Admin Notified</b>\n\n"
                    f"Error: <code>{payout_err}</code>\n"
                    f"🆔 Deal: <code>{did}</code>\n"
                    f"Admin will resolve manually."
                ),
                parse_mode="HTML"
            )
        except Exception:
            pass
        await log(ctx,
            f"🚨 <b>REFUND PAYOUT FAILED</b>\n\n🆔 <code>{did}</code>\n"
            f"📤 Seller Wallet: <code>{seller_addr}</code>\n"
            f"💰 Amount: {refund_amt:.6f} {sym}\n❌ Error: {payout_err}"
        )
        return

    tx_line = f"🔗 TX: <code>{payout_txid}</code>\n" if payout_txid and payout_txid != "DEMO_MODE" else "🧪 DEMO mode\n"

    # Notify group
    try:
        await ctx.bot.send_message(
            chat_id=deal["group_id"],
            text=(
                f"↩️ <b>DEAL REFUNDED</b>\n\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"🆔 Trade ID: <code>{did}</code>\n"
                f"🪙 Token: {token_label}\n\n"
                f"💰 Original:   {qty:.6f} {sym}\n"
                f"💸 Fee ({state.fee_percent}%): {fee_amt:.6f} {sym}\n"
                f"↩️ Returned:   <b>{refund_amt:.6f} {sym}</b>\n"
                f"{tx_line}"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"🛒 Buyer: @{deal.get('buyer_username','N/A')} — ✅ Confirmed\n"
                f"🏪 Seller: @{deal.get('seller_username','N/A')} — ✅ Confirmed\n"
                f"📤 Refunded to Seller Wallet:\n<code>{seller_addr}</code>\n\n"
                f"⏰ {refunded_ist}\n"
                f"<i>Group closes in 20 seconds.</i>"
            ),
            parse_mode="HTML"
        )
    except Exception:
        pass

    # DM both parties
    for p in ("buyer", "seller"):
        pid = deal.get(f"{p}_id")
        if pid:
            try:
                await ctx.bot.send_message(
                    chat_id=pid,
                    text=(
                        f"↩️ <b>Deal Refunded: {did}</b>\n\n"
                        f"🪙 {token_label}\n"
                        f"💰 Original: {qty:.6f} {sym}\n"
                        f"💸 Fee: {fee_amt:.6f} {sym}\n"
                        f"↩️ Returned to Seller: {refund_amt:.6f} {sym}\n"
                        f"📤 Seller Wallet: <code>{seller_addr}</code>\n\n"
                        f"⏰ {refunded_ist}"
                    ),
                    parse_mode="HTML"
                )
            except Exception:
                pass

    await log(ctx,
        f"↩️ <b>DEAL REFUNDED</b>\n\n🆔 <code>{did}</code>\n"
        f"🪙 {token_label}\n"
        f"💰 {qty:.6f} → Fee: {fee_amt:.6f} → Refunded: {refund_amt:.6f} {sym}\n"
        f"📤 Seller: <code>{seller_addr}</code>\n"
        f"🔗 TX: {payout_txid}\n"
        f"⏰ {refunded_ist}"
    )

    # Delete group
    group_id = deal.get("group_id")
    await asyncio.sleep(10)
    try:
        await ctx.bot.send_message(chat_id=group_id, text="🗑 <b>Group closing in 10 seconds.</b>", parse_mode="HTML")
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


async def handle_refund_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE, d: str):
    """Admin or participant cancels the refund request."""
    q    = update.callback_query
    user = q.from_user
    did  = d.split(":", 1)[1]
    deal = deal_by_id(did)
    if not deal:
        await q.answer("❌ Deal not found.", show_alert=True)
        return
    if not (is_admin(user.id) or user.id in (deal.get("buyer_id"), deal.get("seller_id"))):
        await q.answer("❌ Not authorized.", show_alert=True)
        return
    if deal.get("status") != "AWAITING_REFUND_CONFIRM":
        await q.answer("⚠️ Refund not pending.", show_alert=True)
        return

    deal["status"] = "FUNDED" if deal.get("funded") else "TOKEN_SELECTED"
    deal.pop("refund_buyer_ok", None)
    deal.pop("refund_seller_ok", None)
    await q.edit_message_text(
        f"❌ <b>Refund Cancelled</b>\n\n"
        f"🆔 <code>{did}</code>\n"
        f"Deal resumed. Status: <b>{deal['status']}</b>",
        parse_mode="HTML"
    )
    await log(ctx, f"❌ <b>REFUND CANCELLED</b>\n\n🆔 <code>{did}</code>\n👤 @{user.username or user.id}")




async def cmd_disputeend(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Admin closes a dispute without releasing — restores FUNDED state."""
    if not is_admin(update.effective_user.id):
        return
    if not ctx.args:
        await update.message.reply_text("Usage: <code>/disputeend DEAL_ID</code>", parse_mode="HTML")
        return
    did  = ctx.args[0].upper()
    deal = deal_by_id(did)
    if not deal:
        await update.message.reply_text(f"❌ Not found: <code>{did}</code>", parse_mode="HTML")
        return
    if deal.get("status") != "DISPUTED":
        await update.message.reply_text(f"⚠️ Deal is not in DISPUTED status. Current: <b>{deal.get('status')}</b>", parse_mode="HTML")
        return

    user = update.effective_user
    deal["status"] = "FUNDED" if deal.get("funded") else "TOKEN_SELECTED"
    deal["dispute_ended_by"] = user.username or str(user.id)
    deal["dispute_ended_at"] = datetime.utcnow().isoformat()
    state.dispute_admins.pop(did, None)

    try:
        await ctx.bot.send_message(
            chat_id=deal["group_id"],
            text=(
                f"✅ <b>DISPUTE CLOSED BY ADMIN</b>\n\n"
                f"👨‍💼 Admin: @{user.username or user.id}\n"
                f"🆔 <code>{did}</code>\n\n"
                f"Dispute has been resolved. Deal resumed.\n"
                f"📊 Status: <b>{deal['status']}</b>\n\n"
                f"➡️ Either party can run <b>/release</b> to proceed."
            ),
            parse_mode="HTML"
        )
    except Exception:
        pass

    await update.message.reply_text(f"✅ Dispute ended. Deal <code>{did}</code> status: <b>{deal['status']}</b>", parse_mode="HTML")
    await log(ctx,
        f"🔚 <b>DISPUTE ENDED</b>\n\n🆔 <code>{did}</code>\n"
        f"👨‍💼 @{user.username}\n📊 Status: {deal['status']}\n⏰ {deal['dispute_ended_at']}"
    )

async def cmd_canceldeal(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    if not ctx.args:
        await update.message.reply_text("Usage: <code>/canceldeal DEAL_ID</code>", parse_mode="HTML")
        return
    did  = ctx.args[0].upper()
    deal = deal_by_id(did)
    if not deal:
        await update.message.reply_text(f"❌ Not found: <code>{did}</code>", parse_mode="HTML")
        return
    if deal.get("status") == "COMPLETED":
        await update.message.reply_text("⚠️ Cannot cancel completed deal.")
        return
    old  = deal["status"]
    user = update.effective_user
    deal["status"]       = "CANCELLED"
    deal["cancelled_by"] = user.username
    deal["cancelled_at"] = datetime.utcnow().isoformat()
    try:
        await ctx.bot.send_message(
            chat_id=deal["group_id"],
            text=f"🚫 <b>DEAL CANCELLED BY ADMIN</b>\n\n🆔 <code>{did}</code>\nNo funds transferred.",
            parse_mode="HTML"
        )
    except Exception:
        pass
    await update.message.reply_text(f"✅ Deal <code>{did}</code> cancelled. Was: {old}", parse_mode="HTML")
    await log(ctx, f"🚫 <b>DEAL CANCELLED</b>\n\n🆔 <code>{did}</code>\n👨‍💼 @{user.username}\n📊 Was: {old}\n⏰ {deal['cancelled_at']}")

async def cmd_setloggroup(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    user = update.effective_user
    if not is_main_admin(user.id):
        return
    if chat.type == "private":
        await update.message.reply_text("❌ Run this inside the group you want as LOG GROUP.")
        return
    state.log_group_id = chat.id
    await update.message.reply_text(
        f"✅ <b>LOG GROUP SET!</b>\n\n📋 {chat.title}\n🆔 <code>{chat.id}</code>\n\nBot ready for deals!",
        parse_mode="HTML"
    )


async def cmd_setdisputegroup(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Set the dispute alert group.
    Can be used two ways:
      1. Inside the target group: /setdisputegroup
      2. From private/any chat with an arg: /setdisputegroup GROUP_ID_OR_LINK
    """
    chat = update.effective_chat
    user = update.effective_user
    if not is_main_admin(user.id):
        return

    # If used with an argument — resolve by ID or invite link
    if ctx.args:
        arg = ctx.args[0].strip()
        try:
            # Try numeric group ID first
            gid = int(arg)
            state.dispute_group_id = gid
            await update.message.reply_text(
                f"✅ <b>DISPUTE GROUP SET!</b>\n\n🆔 <code>{gid}</code>\n\n"
                f"All dispute alerts will be forwarded there.",
                parse_mode="HTML"
            )
        except ValueError:
            # It's an invite link — try to get chat info via bot
            try:
                chat_obj = await ctx.bot.get_chat(arg)
                state.dispute_group_id = chat_obj.id
                await update.message.reply_text(
                    f"✅ <b>DISPUTE GROUP SET!</b>\n\n📋 {chat_obj.title}\n🆔 <code>{chat_obj.id}</code>",
                    parse_mode="HTML"
                )
            except Exception as e:
                await update.message.reply_text(
                    f"❌ Could not resolve group: {e}\n\n"
                    f"Make sure the bot is a member of that group, then try:\n"
                    f"<code>/setdisputegroup GROUP_ID</code>",
                    parse_mode="HTML"
                )
        return

    # No arg — must be run inside the target group
    if chat.type == "private":
        await update.message.reply_text(
            "❌ Run inside the dispute group, or pass group ID:\n"
            "<code>/setdisputegroup -100xxxxxxxxxx</code>",
            parse_mode="HTML"
        )
        return

    state.dispute_group_id = chat.id
    await update.message.reply_text(
        f"✅ <b>DISPUTE GROUP SET!</b>\n\n📋 {chat.title}\n🆔 <code>{chat.id}</code>\n\n"
        f"All dispute alerts will be forwarded here.",
        parse_mode="HTML"
    )

async def cmd_addadmin(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_main_admin(update.effective_user.id): return
    if not ctx.args:
        await update.message.reply_text("Usage: <code>/addadmin USER_ID</code>", parse_mode="HTML")
        return
    try:
        uid = int(ctx.args[0])
        state.sub_admins.add(uid)
        await update.message.reply_text(f"✅ Sub Admin Added: <code>{uid}</code>", parse_mode="HTML")
        try:
            await ctx.bot.send_message(chat_id=uid, text="👨‍💼 <b>You've been added as Sub Admin!</b>", parse_mode="HTML")
        except Exception:
            pass
    except ValueError:
        await update.message.reply_text("❌ Invalid ID.")

async def cmd_removeadmin(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_main_admin(update.effective_user.id): return
    if not ctx.args:
        await update.message.reply_text("Usage: <code>/removeadmin USER_ID</code>", parse_mode="HTML")
        return
    try:
        uid = int(ctx.args[0])
        state.sub_admins.discard(uid)
        await update.message.reply_text(f"✅ Removed <code>{uid}</code>", parse_mode="HTML")
    except ValueError:
        await update.message.reply_text("❌ Invalid ID.")

async def cmd_setfee(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_main_admin(update.effective_user.id): return
    if not ctx.args:
        await update.message.reply_text(f"Usage: <code>/setfee PERCENT</code>\nCurrent: <b>{state.fee_percent}%</b>", parse_mode="HTML")
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
    if not is_main_admin(update.effective_user.id): return
    if not ctx.args:
        cur_fee = getattr(state, "bio_discount_percent", 0.0)
        await update.message.reply_text(
            f"Usage: <code>/setbio TAG</code>\n"
            f"Current tag: <b>{state.required_bio or 'Not set'}</b>\n"
            f"Discount fee: <b>{cur_fee}%</b> (set with /setbiodiscount)",
            parse_mode="HTML"
        )
        return
    state.required_bio = ctx.args[0]
    cur_fee = getattr(state, "bio_discount_percent", 0.0)
    await update.message.reply_text(
        f"✅ Bio tag: <b>{state.required_bio}</b>\n"
        f"Users with this in bio get <b>{cur_fee}% fee</b> (instead of {state.fee_percent}%).\n"
        f"Change discount rate: <code>/setbiodiscount PERCENT</code>",
        parse_mode="HTML"
    )


async def cmd_setbiodiscount(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Set the discounted fee % applied when bio tag matches."""
    if not is_main_admin(update.effective_user.id): return
    if not ctx.args:
        cur = getattr(state, "bio_discount_percent", 0.0)
        await update.message.reply_text(
            f"Usage: <code>/setbiodiscount PERCENT</code>\n"
            f"Current: <b>{cur}%</b>\n"
            f"Normal fee: <b>{state.fee_percent}%</b>\n\n"
            f"Example: <code>/setbiodiscount 0</code> = free (0%) for bio-matched users\n"
            f"Example: <code>/setbiodiscount 0.5</code> = 0.5% for bio-matched users",
            parse_mode="HTML"
        )
        return
    try:
        val = float(ctx.args[0])
        if not (0 <= val <= 50):
            await update.message.reply_text("❌ Must be 0–50.")
            return
        state.bio_discount_percent = val
        await update.message.reply_text(
            f"✅ Bio discount fee set: <b>{val}%</b>\n"
            f"Normal fee: {state.fee_percent}%\n"
            f"Bio-matched users pay: <b>{val}%</b>",
            parse_mode="HTML"
        )
    except ValueError:
        await update.message.reply_text("❌ Invalid number.")

async def cmd_setoxapay(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_main_admin(update.effective_user.id): return
    if not ctx.args:
        await update.message.reply_text("Usage: <code>/setoxapay API_KEY</code>", parse_mode="HTML")
        return
    state.oxapay_key = ctx.args[0]
    key = state.oxapay_key
    masked = f"{key[:4]}{'*'*(len(key)-8)}{key[-4:]}" if len(key) > 8 else "****"
    await update.message.reply_text(f"✅ <b>OxaPay Key Set!</b>\n🔑 <code>{masked}</code>", parse_mode="HTML")

async def cmd_checkoxapay(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_main_admin(update.effective_user.id): return
    if not state.oxapay_key:
        await update.message.reply_text("❌ OxaPay key not set.")
        return
    await update.message.reply_text("⏳ Checking OxaPay…")
    try:
        loop = asyncio.get_event_loop()
        def _chk():
            # Validate key via /merchants/inquiry with dummy trackId
            # result 200 = not found (key valid), result 203 = invalid key
            req = urllib.request.Request(
                "https://api.oxapay.com/merchants/inquiry",
                data=_json.dumps({
                    "merchant": state.oxapay_key,
                    "trackId":  0
                }).encode(),
                headers={"Content-Type": "application/json"}
            )
            with urllib.request.urlopen(req, timeout=15) as r:
                return _json.loads(r.read().decode())
        data = await loop.run_in_executor(None, _chk)
        logger.info(f"OxaPay check: {data}")
        result = data.get("result")
        key = state.oxapay_key
        masked = f"{key[:4]}{'*'*(len(key)-8)}{key[-4:]}" if len(key) > 8 else "****"
        if result in (100, 200):
            await update.message.reply_text(
                f"✅ <b>OxaPay Key Valid!</b>\n\n"
                f"🔑 Key: <code>{masked}</code>\n"
                f"🌐 API: Connected\n\n"
                f"Bot is in <b>LIVE mode</b>.",
                parse_mode="HTML"
            )
        elif result == 203:
            await update.message.reply_text(
                f"❌ <b>Invalid OxaPay Key!</b>\n\nKey rejected (error 203). Please update via /setoxapay.",
                parse_mode="HTML"
            )
        else:
            await update.message.reply_text(f"⚠️ OxaPay result={result}: {data.get('message', 'Unknown')}")
    except Exception as e:
        await update.message.reply_text(f"❌ Failed: {e}")

async def cmd_resetoxapay(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_main_admin(update.effective_user.id): return
    state.oxapay_key = None
    await update.message.reply_text("✅ OxaPay key removed. Bot is in DEMO mode.")

async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    all_d = list(state.deals.values())
    total = len(all_d)
    done  = sum(1 for x in all_d if x["status"] == "COMPLETED")
    dis   = sum(1 for x in all_d if x["status"] == "DISPUTED")
    fund  = sum(1 for x in all_d if x["status"] == "FUNDED")
    ox = f"✅ {state.oxapay_key[:4]}...{state.oxapay_key[-4:]}" if state.oxapay_key else "❌ Not Set (Demo)"
    lg = f"✅ <code>{state.log_group_id}</code>" if state.log_group_id else "❌ Not Set"
    dg = f"✅ <code>{state.dispute_group_id}</code>" if state.dispute_group_id else "❌ Not Set"
    tc = "✅ Connected" if state.telethon_client else "❌ Not Connected"
    await update.message.reply_text(
        f"📊 <b>BOT STATUS</b>\n\n📋 Log Group: {lg}\n🚨 Dispute Group: {dg}\n🔑 OxaPay: {ox}\n📡 Telethon: {tc}\n"
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
        await update.message.reply_text("Usage: <code>/dealinfo TRADE_ID</code>", parse_mode="HTML")
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
        f"🛒 @{deal.get('buyer_username','—')}  <code>{deal.get('buyer_address','N/A')}</code>\n"
        f"🏪 @{deal.get('seller_username','—')}  <code>{deal.get('seller_address','N/A')}</code>\n\n"
        f"💰 QUANTITY — {deal.get('quantity','—')}\n"
        f"📈 RATE — {deal.get('rate','—')}\n"
        f"📝 CONDITION — {deal.get('condition','—')}\n"
        f"🪙 Token: {deal.get('token','Not Selected')}\n"
        f"📬 <code>{deal.get('deposit_address','Not Generated')}</code>\n\n"
        f"{b} Buyer  |  {s} Seller\n"
        f"⏰ {deal.get('created_at','N/A')[:19].replace('T',' ')} UTC",
        parse_mode="HTML"
    )

async def cmd_listadmins(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_main_admin(update.effective_user.id): return
    txt = f"👑 Main: <code>{MAIN_ADMIN_ID}</code>\n\n"
    txt += ("👨‍💼 Sub Admins:\n" + "".join(f"{i}. <code>{a}</code>\n" for i, a in enumerate(state.sub_admins, 1))) if state.sub_admins else "👨‍💼 Sub Admins: None"
    await update.message.reply_text(f"📋 <b>ADMIN LIST</b>\n\n{txt}", parse_mode="HTML")

# ══════════════════════════════════════════════════════════
# ADMIN INPUT HANDLER (private chat — admin panel text input)
# ══════════════════════════════════════════════════════════

async def admin_input_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_main_admin(user.id):
        return
    if update.effective_chat.type != "private":
        return
    field = _admin_waiting.get(user.id)
    if not field:
        return

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
            await update.message.reply_text(f"✅ Sub Admin Added: <code>{uid}</code>", parse_mode="HTML", reply_markup=kb)
            try:
                await ctx.bot.send_message(chat_id=uid, text="👨‍💼 <b>You have been added as Sub Admin!</b>", parse_mode="HTML")
            except Exception:
                pass
        except ValueError:
            await update.message.reply_text("❌ Invalid ID. Must be a number.", reply_markup=kb)
    elif field == "removeadmin":
        try:
            uid = int(value)
            state.sub_admins.discard(uid)
            await update.message.reply_text(f"✅ Removed <code>{uid}</code>", parse_mode="HTML", reply_markup=kb)
        except ValueError:
            await update.message.reply_text("❌ Invalid ID.", reply_markup=kb)
    elif field == "fee":
        try:
            fee = float(value)
            if not (0 <= fee <= 50):
                await update.message.reply_text("❌ Fee must be 0–50.", reply_markup=kb)
                return
            old = state.fee_percent
            state.fee_percent = fee
            await update.message.reply_text(f"✅ Fee: <s>{old}%</s> → <b>{fee}%</b>", parse_mode="HTML", reply_markup=kb)
        except ValueError:
            await update.message.reply_text("❌ Invalid number.", reply_markup=kb)
    elif field == "bio":
        state.required_bio = value
        cur_fee = getattr(state, "bio_discount_percent", 0.0)
        await update.message.reply_text(
            f"✅ Bio tag set: <b>{value}</b>\nDiscount fee: <b>{cur_fee}%</b>",
            parse_mode="HTML", reply_markup=kb
        )
    elif field == "bio_discount":
        try:
            val = float(value)
            if not (0 <= val <= 50):
                await update.message.reply_text("❌ Must be 0–50.", reply_markup=kb)
            else:
                state.bio_discount_percent = val
                await update.message.reply_text(
                    f"✅ Bio discount fee: <b>{val}%</b>", parse_mode="HTML", reply_markup=kb
                )
        except ValueError:
            await update.message.reply_text("❌ Invalid number.", reply_markup=kb)
    elif field == "oxapay":
        state.oxapay_key = value
        masked = f"{value[:4]}{'*'*(len(value)-8)}{value[-4:]}" if len(value) > 8 else "****"
        await update.message.reply_text(f"✅ OxaPay Key Set: <code>{masked}</code>", parse_mode="HTML", reply_markup=kb)
    elif field == "api_id":
        try:
            state.api_id = int(value)
            await update.message.reply_text(f"✅ API ID set: <code>{state.api_id}</code>", parse_mode="HTML", reply_markup=kb)
        except ValueError:
            await update.message.reply_text("❌ API ID must be a number.", reply_markup=kb)
    elif field == "api_hash":
        state.api_hash = value
        await update.message.reply_text("✅ <b>API Hash set!</b>", parse_mode="HTML", reply_markup=kb)
    elif field == "phone":
        state.phone = value
        await update.message.reply_text(f"✅ Phone set: <code>{value}</code>", parse_mode="HTML", reply_markup=kb)
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
            await update.message.reply_text(f"❌ OTP failed: {e}", reply_markup=kb)
    else:
        await update.message.reply_text("⚠️ Unknown field.", reply_markup=kb)

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

    # User commands
    app.add_handler(CommandHandler("start",        cmd_start))
    app.add_handler(CommandHandler("instructions", cmd_instructions))
    app.add_handler(CommandHandler("initdeal",     cmd_initdeal))
    app.add_handler(CommandHandler("dd",           cmd_dd))
    app.add_handler(CommandHandler("buyer",        cmd_buyer))
    app.add_handler(CommandHandler("seller",       cmd_seller))
    app.add_handler(CommandHandler("token",        cmd_token))
    app.add_handler(CommandHandler("deposit",      cmd_deposit))
    app.add_handler(CommandHandler("verify",       cmd_verify))
    app.add_handler(CommandHandler("release",      cmd_release))
    app.add_handler(CommandHandler("dispute",      cmd_dispute))
    app.add_handler(CommandHandler("dealinfo",     cmd_dealinfo))

    # Admin-only commands
    app.add_handler(CommandHandler("adminpanel",   cmd_adminpanel))
    app.add_handler(CommandHandler("adminrelease", cmd_adminrelease))
    app.add_handler(CommandHandler("refund",       cmd_refund))
    app.add_handler(CommandHandler("disputeend",   cmd_disputeend))
    app.add_handler(CommandHandler("canceldeal",   cmd_canceldeal))
    app.add_handler(CommandHandler("setloggroup",     cmd_setloggroup))
    app.add_handler(CommandHandler("setdisputegroup", cmd_setdisputegroup))
    app.add_handler(CommandHandler("addadmin",     cmd_addadmin))
    app.add_handler(CommandHandler("removeadmin",  cmd_removeadmin))
    app.add_handler(CommandHandler("setfee",          cmd_setfee))
    app.add_handler(CommandHandler("setbio",          cmd_setbio))
    app.add_handler(CommandHandler("setbiodiscount",  cmd_setbiodiscount))
    app.add_handler(CommandHandler("setoxapay",    cmd_setoxapay))
    app.add_handler(CommandHandler("checkoxapay",  cmd_checkoxapay))
    app.add_handler(CommandHandler("resetoxapay",  cmd_resetoxapay))
    app.add_handler(CommandHandler("status",       cmd_status))
    app.add_handler(CommandHandler("listadmins",   cmd_listadmins))

    # Message handlers — ORDER MATTERS: group first, then private
    app.add_handler(MessageHandler(
        filters.TEXT & filters.ChatType.GROUPS & ~filters.COMMAND,
        group_message_handler
    ))
    app.add_handler(MessageHandler(
        filters.TEXT & filters.ChatType.PRIVATE & ~filters.COMMAND,
        admin_input_handler
    ))

    # Callback buttons
    app.add_handler(CallbackQueryHandler(callback_handler))

    logger.info("All handlers registered. Bot running…")
    app.run_polling(allowed_updates=["message", "callback_query", "chat_member"])

if __name__ == "__main__":
    main()
