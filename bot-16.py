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
# address edit waiting:  user_id -> {"deal_id": str, "role": str, "chat_id": int}
_address_edit_waiting: dict[int, dict] = {}
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
    fee_pct  = state.fee_percent
    bio_tag  = state.required_bio or "not set"
    bio_disc = state.bio_discount_percent if hasattr(state, "bio_discount_percent") else 0

    text = (
        "📖 <b>HOW TO USE ESCROW BOT</b>\n\n"

        "━━━━━━━━━━━━━━━━━━━━\n"
        "🔢 <b>DEAL SETUP STEPS</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"

        "<b>1️⃣ Start Deal</b>\n"
        "/start → tap <b>Start Deal</b> → bot creates private group\n\n"

        "<b>2️⃣ Both Join</b>\n"
        "Buyer and Seller both join the group\n\n"

        "<b>3️⃣ Fill Deal Form</b>\n"
        "/dd → bot sends blank form → fill & send it back\n\n"

        "<b>4️⃣ Set Roles & Wallets</b>\n"
        "/buyer — sets you as buyer (provide your wallet)\n"
        "/seller — sets you as seller (provide your wallet)\n\n"

        "<b>5️⃣ Select Token</b>\n"
        "/token → choose USDT/BTC/LTC → both confirm\n\n"

        "<b>6️⃣ Deposit</b>\n"
        "/deposit → OxaPay generates payment link\n"
        "Seller pays crypto to escrow via payment link\n\n"

        "<b>7️⃣ Verify Payment</b>\n"
        "/verify → OxaPay confirms payment received\n"
        "/balance → check how much is deposited anytime\n\n"

        "<b>8️⃣ Buyer Pays Seller (off-platform)</b>\n"
        "Buyer sends fiat/payment to seller outside the bot\n\n"

        "<b>9️⃣ Release Funds</b>\n"
        "Both must confirm — then crypto auto-releases to buyer\n\n"

        "━━━━━━━━━━━━━━━━━━━━\n"
        "💸 <b>RELEASE OPTIONS</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"

        "📤 <b>/release all</b>\n"
        "Release full escrow amount to buyer.\n"
        "Both must confirm → auto payout → deal done.\n\n"

        "📤 <b>/release X</b>  (partial release)\n"
        "Release X amount to buyer, rest stays in escrow.\n"
        "Example: <code>/release 50</code>\n"
        "• Both confirm → X released to buyer\n"
        "• Remaining stays locked in escrow\n"
        "• Group stays active\n"
        "• Seller can do more /release or /refund later\n\n"

        "━━━━━━━━━━━━━━━━━━━━\n"
        "↩️ <b>REFUND OPTIONS</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"

        "↩️ <b>/refund all</b>\n"
        "Refund full remaining escrow back to seller.\n"
        f"Fee {fee_pct}% is deducted. Both must confirm.\n\n"

        "↩️ <b>/refund Y</b>  (partial refund)\n"
        "Refund Y amount back to seller, rest stays in escrow.\n"
        "Example: <code>/refund 30</code>\n"
        "• Both confirm → Y refunded to seller\n"
        "• Remaining stays locked\n"
        "• Group stays active\n"
        "• Can keep doing /release or /refund as needed\n\n"

        "⚠️ <i>Group auto-closes in 7 days if balance remains. Daily reminders sent.</i>\n\n"

        "━━━━━━━━━━━━━━━━━━━━\n"
        "🎟 <b>BIO DISCOUNT</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"

        f"If buyer or seller has <b>{bio_tag}</b> in their Telegram bio,\n"
        f"they get a discounted fee of <b>{bio_disc}%</b> instead of {fee_pct}%.\n\n"
        "How to get discount:\n"
        f"• Add <code>{bio_tag}</code> to your Telegram bio\n"
        "• Bot auto-checks at deal deposit & release time\n"
        "• No need to do anything extra\n\n"

        "━━━━━━━━━━━━━━━━━━━━\n"
        "✏️ <b>WALLET COMMANDS</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"

        "📬 <b>/buyer [address]</b> — register as buyer\n"
        "📬 <b>/seller [address]</b> — register as seller\n"
        "↳ Inline: <code>/buyer TXxyz123</code>  OR  /buyer → then send address\n\n"

        "✏️ <b>/editaddress</b> — change your wallet\n"
        "• Buyer: editable until deposit confirmed\n"
        "• Seller: editable until refund processed\n"
        "↳ Inline: <code>/editaddress NEW_ADDRESS</code>\n\n"

        "━━━━━━━━━━━━━━━━━━━━\n"
        "🚨 <b>DISPUTE / PROBLEMS</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"

        "/dispute — call admin to resolve issue\n"
        "/balance — check deposit status anytime\n\n"

        "👨‍💼 <b>Admin Commands:</b>\n"
        "/admindeposit DEAL_ID — manually confirm deposit\n"
        "/adminrelease DEAL_ID buyer/seller — force release\n"
        "/adminforcedeposit ADDRESS AMOUNT TOKEN — send to any wallet\n"
        "/refund DEAL_ID — initiate refund\n"
        "/disputeend DEAL_ID — close dispute, resume deal\n\n"

        "⭐ <b>VOUCH SYSTEM</b>\n"
        "After deal completes, both parties get a DM to leave a review.\n"
        "Reviews are posted in the vouch channel.\n"
        "You can skip — group closes after 2 minutes anyway.\n\n"

        "⚠️ <i>All deal commands must be used inside your deal group</i>"
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
    vg = "✅ SET" if getattr(state, "vouch_group_id", None) else "❌ NOT SET"
    ve = "✅ ON" if getattr(state, "vouch_enabled", True) else "❌ OFF"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"📋 Log Group {lg}", callback_data="adm:setloggroup"),
         InlineKeyboardButton(f"🚨 Dispute Group {dg}", callback_data="adm:setdisputegroup")],
        [InlineKeyboardButton(f"⭐ Vouch Group {vg}", callback_data="adm:setvouchgroup"),
         InlineKeyboardButton(f"⭐ Vouch System {ve}", callback_data="adm:togglevouch")],
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
    elif d.startswith("vouch:"):             await handle_vouch_callback(update, ctx, d)
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
        vg = f"✅ <code>{state.vouch_group_id}</code>" if getattr(state, "vouch_group_id", None) else "❌ Not Set"
        ve = "✅ ON" if getattr(state, "vouch_enabled", True) else "❌ OFF"
        await q.edit_message_text(
            f"📊 <b>BOT STATUS</b>\n\n"
            f"📋 Log Group: {lg}\n🚨 Dispute Group: {dg}\n⭐ Vouch Group: {vg}\n⭐ Vouch System: {ve}\n🔑 OxaPay: {ox}\n📡 Telethon: {tc}\n"
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
                        "trackId":  1
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

    elif action == "setvouchgroup":
        vg_status = f"✅ Currently set: <code>{state.vouch_group_id}</code>" if getattr(state, "vouch_group_id", None) else "❌ Not set yet"
        await q.edit_message_text(
            f"⭐ <b>Set Vouch Group</b>\n\n{vg_status}\n\n"
            "Option 1 — Run inside the vouch group:\n"
            "<code>/setvouchgroup</code>\n\n"
            "Option 2 — From any chat with group ID:\n"
            "<code>/setvouchgroup -100xxxxxxxxxx</code>\n\n"
            "⚠️ Bot must be admin in that group.\n"
            "All deal vouches will be forwarded there.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="adm:status")]])
        )

    elif action == "togglevouch":
        state.vouch_enabled = not getattr(state, "vouch_enabled", True)
        status_txt = "✅ <b>Vouch System ENABLED</b>\n\nAfter each deal, bot will ask both parties for a vouch/review." if state.vouch_enabled else "❌ <b>Vouch System DISABLED</b>\n\nNo vouch requests will be sent after deals."
        await q.edit_message_text(status_txt, parse_mode="HTML", reply_markup=admin_panel_kb())

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

    # ── Edit address capture ───────────────────────────────
    if user.id in _address_edit_waiting:
        info = _address_edit_waiting[user.id]
        if info.get("chat_id") == chat.id:
            did   = info["deal_id"]
            role  = info["role"]
            deal  = deal_by_id(did)
            if deal:
                new_addr = text.strip()
                other_role = "seller" if role == "buyer" else "buyer"
                other_addr = deal.get(f"{other_role}_address", "")
                if other_addr and other_addr.strip().lower() == new_addr.lower():
                    await msg.reply_text(
                        f"❌ <b>Same address as {other_role}!</b> Please send a different wallet address.",
                        parse_mode="HTML"
                    )
                    return
                old_addr = deal.get(f"{role}_address", "N/A")
                deal[f"{role}_address"] = new_addr
                _address_edit_waiting.pop(user.id, None)
                label = "🛒 Buyer" if role == "buyer" else "🏪 Seller"
                await msg.reply_text(
                    f"✅ <b>{label} Address Updated!</b>\n\n"
                    f"📤 Old: <code>{old_addr}</code>\n"
                    f"📥 New: <code>{new_addr}</code>\n\n"
                    f"🔒 Address locked until {'release' if role == 'buyer' else 'refund'}.",
                    parse_mode="HTML"
                )
                return
            else:
                _address_edit_waiting.pop(user.id, None)

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

    # If address provided inline: /buyer 0xABCDEF or /seller 0xABCDEF
    inline_address = " ".join(ctx.args).strip() if ctx.args else None
    if inline_address:
        # Validate not same as other party
        other_role = "seller" if role == "buyer" else "buyer"
        other_addr = deal.get(f"{other_role}_address")
        if other_addr and other_addr.strip().lower() == inline_address.strip().lower():
            await update.message.reply_text(
                f"❌ <b>Same Address Not Allowed!</b>\n\n"
                f"Your wallet is the same as the <b>{other_role}'s</b>.\n"
                f"Both parties must use <b>different wallets</b>.",
                parse_mode="HTML"
            )
            return
        # Register directly
        deal[f"{role}_id"]       = user.id
        deal[f"{role}_username"] = user.username or user.first_name
        deal[f"{role}_address"]  = inline_address
        deal[f"{role}_locked"]   = True
        _address_waiting.pop(user.id, None)
        b = deal.get("buyer_id") is not None
        s = deal.get("seller_id") is not None
        if b and s:
            deal["status"] = "ROLES_SET"
            next_step = "🔒 <b>Both roles locked!</b>\n\n➡️ <b>Next step:</b> Use <b>/token</b> to select payment token"
        elif b:
            next_step = "⏳ Waiting for <b>Seller</b> to send <b>/seller</b> or <b>/seller [address]</b>"
        else:
            next_step = "⏳ Waiting for <b>Buyer</b> to send <b>/buyer</b> or <b>/buyer [address]</b>"
        await update.message.reply_text(
            f"{'🛒' if role == 'buyer' else '🏪'} <b>{label} Registered!</b>\n\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"👤 @{deal[f'{role}_username']}\n"
            f"💳 Wallet: <code>{inline_address}</code>\n"
            f"🔒 Role permanently locked\n"
            f"━━━━━━━━━━━━━━━━━━━━\n\n"
            f"{next_step}",
            parse_mode="HTML"
        )
        return

    # No inline address — Register this user as waiting to provide their address
    _address_waiting[user.id] = {"deal_id": did, "role": role, "chat_id": chat.id}

    await update.message.reply_text(
        f"👋 <b>{label} — {user.first_name}</b>\n\n"
        f"📬 Please send your <b>wallet address</b> in this group now.\n\n"
        f"💡 <i>Tip: You can also use <code>/{role} YOUR_ADDRESS</code> directly next time.</i>\n\n"
        f"<i>Your next message will be captured as your address. Role is permanently locked after this.</i>",
        parse_mode="HTML"
    )

# ══════════════════════════════════════════════════════════
# /editaddress — update wallet before deposit (buyer) or refund (seller)
# ══════════════════════════════════════════════════════════

async def cmd_editaddress(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """
    Buyer can edit address before deposit is confirmed.
    Seller can edit address before refund is processed.
    Usage: /editaddress  OR  /editaddress NEW_ADDRESS
    """
    chat = update.effective_chat
    user = update.effective_user
    if chat.type == "private":
        await update.message.reply_text("❌ Use inside your deal group.")
        return
    did, deal = deal_by_group(chat.id)
    if not deal:
        await update.message.reply_text("❌ No active deal here.")
        return

    # Determine role
    role = None
    if deal.get("buyer_id") == user.id:
        role = "buyer"
    elif deal.get("seller_id") == user.id:
        role = "seller"
    else:
        await update.message.reply_text("❌ You are not a party in this deal.")
        return

    # Lock rules: buyer locked after deposit, seller locked after refund triggered
    status = deal.get("status", "")
    if role == "buyer":
        locked_statuses = ("FUNDED", "AWAITING_CONFIRMATION", "COMPLETED", "REFUNDED", "CANCELLED", "DISPUTED")
        if status in locked_statuses:
            await update.message.reply_text(
                f"🔒 <b>Buyer address is locked.</b>\n\n"
                f"Address can only be changed before deposit is confirmed.\n"
                f"Current status: <b>{status}</b>",
                parse_mode="HTML"
            )
            return
    else:  # seller
        locked_statuses = ("AWAITING_REFUND_CONFIRM", "REFUNDED", "COMPLETED", "CANCELLED")
        if status in locked_statuses:
            await update.message.reply_text(
                f"🔒 <b>Seller address is locked.</b>\n\n"
                f"Address can only be changed before a refund is processed.\n"
                f"Current status: <b>{status}</b>",
                parse_mode="HTML"
            )
            return

    label = "🛒 Buyer" if role == "buyer" else "🏪 Seller"
    current_addr = deal.get(f"{role}_address", "Not set")

    # Inline: /editaddress NEW_ADDRESS
    inline_address = " ".join(ctx.args).strip() if ctx.args else None
    if inline_address:
        other_role = "seller" if role == "buyer" else "buyer"
        other_addr = deal.get(f"{other_role}_address", "")
        if other_addr and other_addr.strip().lower() == inline_address.lower():
            await update.message.reply_text(
                f"❌ Same address as {other_role}! Use a different wallet.", parse_mode="HTML"
            )
            return
        deal[f"{role}_address"] = inline_address
        await update.message.reply_text(
            f"✅ <b>{label} Address Updated!</b>\n\n"
            f"📤 Old: <code>{current_addr}</code>\n"
            f"📥 New: <code>{inline_address}</code>",
            parse_mode="HTML"
        )
        return

    # No inline — ask them to send new address
    _address_edit_waiting[user.id] = {"deal_id": did, "role": role, "chat_id": chat.id}
    await update.message.reply_text(
        f"✏️ <b>Edit {label} Address</b>\n\n"
        f"📋 Current: <code>{current_addr}</code>\n\n"
        f"📬 Send your <b>new wallet address</b> now.\n"
        f"💡 Or use: <code>/editaddress NEW_ADDRESS</code>",
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
    bio_tag_hint = ""
    if state.required_bio:
        bio_disc_val = getattr(state, "bio_discount_percent", 0.0)
        bio_tag_hint = (
            f"\n🏷 <b>Bio Discount Available!</b> Add <code>{state.required_bio}</code> to your Telegram bio "
            f"to get <b>{bio_disc_val}%</b> fee instead of {state.fee_percent}%\n"
        )
    if dep_discount_applied:
        saved_amt = qty_float * (state.fee_percent / 100) - qty_float * (dep_effective_fee / 100)
        fee_info = (
            f"💸 Fee: {state.fee_percent}% → <b>{dep_effective_fee}% (Bio Discount ✅)</b>\n"
            f"🏷 {dep_discount_reason}\n"
            f"💰 You save: <b>{saved_amt:.6f} {sym_dep}</b>\n"
        )
    else:
        fee_info = f"💸 Fee on release: <b>{state.fee_percent}%</b>{bio_tag_hint}\n"

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
        import uuid as _uuid
        order_id = f"ESC-{did}-{_uuid.uuid4().hex[:8].upper()}"
        loop = asyncio.get_event_loop()
        def _req():
            req = urllib.request.Request(
                "https://api.oxapay.com/merchants/request",
                data=_json.dumps({
                    "merchant":    state.oxapay_key,
                    "amount":      qty_float,
                    "currency":    currency,
                    "network":     network,
                    "orderId":     order_id,
                    "description": f"Escrow {did}",
                    "lifeTime":    60,
                    "feePaidByPayer": 0,
                    "underPaidCover": 0,
                    "type":        2
                }).encode(),
                headers={"Content-Type": "application/json"}
            )
            with urllib.request.urlopen(req, timeout=15) as r:
                return _json.loads(r.read().decode())
        data = await loop.run_in_executor(None, _req)
        logger.info(f"OxaPay request response for {did}: {data}")
        if data.get("result") != 100:
            raise Exception(f"OxaPay error {data.get('result')}: {data.get('message', 'Validation problem — check API key and parameters')}")
        track_id = str(data.get("trackId") or data.get("track_id") or "")
        pay_link  = data.get("payLink") or ""
        address   = data.get("address") or data.get("payAddress") or ""
        # OxaPay now returns payLink instead of direct address
        deposit_ref = address or pay_link
        if not deposit_ref:
            raise Exception(f"No address or payLink in OxaPay response: {data}")
        deal["deposit_address"]  = deposit_ref
        deal["oxapay_track_id"]  = track_id
        deal["oxapay_order_id"]  = order_id
        deal["oxapay_pay_link"]  = pay_link
        deal["status"] = "AWAITING_DEPOSIT"
        # Build message depending on whether we have address or payLink
        if address:
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
        else:
            await update.message.reply_text(
                f"✅ <b>PAYMENT LINK READY</b>\n\n"
                f"🪙 Token: {TOKEN_LABELS.get(deal['token'], deal['token'])}\n"
                f"💰 Amount: <b>{qty_float} {sym_dep}</b>\n\n"
                f"🔗 <b>Seller Payment Link:</b>\n{pay_link}\n\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"🛒 Buyer: @{deal.get('buyer_username', 'N/A')}\n"
                f"📥 Buyer Wallet: <code>{deal.get('buyer_address', 'N/A')}</code>\n\n"
                f"🏪 Seller: @{deal.get('seller_username', 'N/A')}\n"
                f"📤 Seller Wallet: <code>{deal.get('seller_address', 'N/A')}</code>\n\n"
                f"{fee_info}"
                f"⚠️ <b>SELLER</b>: Open the link above and pay EXACT amount.\n\n"
                f"➡️ After paying, use <b>/verify</b> to confirm payment.",
                parse_mode="HTML"
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
            import uuid as _uuid
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
                        "callbackUrl": "",
                        "description": f"Escrow Release {did}",
                    }).encode(),
                    headers={"Content-Type": "application/json"}
                )
                with urllib.request.urlopen(req, timeout=30) as r:
                    return _json.loads(r.read().decode())
            pdata = await loop.run_in_executor(None, _payout)
            logger.info(f"OxaPay payout response for {did}: {pdata}")

            if pdata.get("result") == 100:
                payout_success = True
                payout_txid    = pdata.get("trackId") or pdata.get("txID") or "N/A"
            else:
                payout_err = f"OxaPay error {pdata.get('result')}: {pdata.get('message', 'Unknown error')}"
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
            f"💸 Fee: <b>{fee_pct}% → {effective_fee_pct}% (Bio Discount ✅)</b>\n"
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
        f"<i>Group will be deleted in 1 minute.</i>"
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

    # ── Vouch system or direct close ──
    if getattr(state, "vouch_enabled", True) and getattr(state, "vouch_group_id", None):
        await asyncio.sleep(10)
        try:
            await ctx.bot.send_message(
                chat_id=group_id,
                text=(
                    "⭐ <b>Deal Done!</b> Both parties will receive a vouch request in DM.\n"
                    "<i>Group closes in ~2 minutes after vouch window.</i>"
                ),
                parse_mode="HTML"
            )
        except Exception:
            pass
        asyncio.create_task(send_vouch_request(ctx, did, deal))
    else:
        await asyncio.sleep(10)
        try:
            await ctx.bot.send_message(chat_id=group_id, text="🗑 <b>Group closing in 50 seconds. Thank you!</b>", parse_mode="HTML")
            await asyncio.sleep(50)
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
# ══════════════════════════════════════════════════════════
# /balance — show deposit status from OxaPay
# ══════════════════════════════════════════════════════════

async def cmd_balance(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Show how much has been deposited for current deal via OxaPay inquiry."""
    chat = update.effective_chat
    if chat.type == "private":
        await update.message.reply_text("❌ Use inside your deal group.")
        return
    did, deal = deal_by_group(chat.id)
    if not deal:
        await update.message.reply_text("❌ No active deal here.")
        return

    qty       = float(deal.get("quantity", 0))
    partial_released = float(deal.get("partial_released", 0))
    remaining_balance = round(qty - partial_released, 8)
    token     = deal.get("token", "")
    sym       = TOKEN_SYMBOL.get(token, token)
    token_lbl = TOKEN_LABELS.get(token, token)
    addr      = deal.get("deposit_address", "N/A")
    track_id  = deal.get("oxapay_track_id", "")

    # DEMO mode
    if not state.oxapay_key or addr.startswith("DEMO_"):
        funded_txt = "✅ Funded (DEMO)" if deal.get("funded") else "⏳ Not yet funded"
        partial_line = (
            f"\n📤 Released So Far: <b>{partial_released} {sym}</b>\n"
            f"🔒 Remaining in Escrow: <b>{remaining_balance} {sym}</b>"
        ) if partial_released > 0 else ""
        await update.message.reply_text(
            f"💰 <b>Balance / Deposit Status</b>\n\n"
            f"🆔 Deal: <code>{did}</code>\n"
            f"🪙 Token: {token_lbl}\n"
            f"💵 Total Deposited: <b>{qty} {sym}</b>"
            f"{partial_line}\n\n"
            f"📬 Escrow Address: <code>{addr}</code>\n"
            f"📊 Status: {funded_txt}\n\n"
            f"🧪 <i>Running in DEMO mode — no real balance check</i>",
            parse_mode="HTML"
        )
        return

    if not track_id:
        await update.message.reply_text(
            "⚠️ No deposit address generated yet.\nUse <b>/deposit</b> first.",
            parse_mode="HTML"
        )
        return

    await update.message.reply_text("⏳ Fetching deposit status from OxaPay…")
    try:
        loop = asyncio.get_event_loop()
        def _balance_check():
            req = urllib.request.Request(
                "https://api.oxapay.com/merchants/inquiry",
                data=_json.dumps({
                    "merchant": state.oxapay_key,
                    "trackId":  track_id
                }).encode(),
                headers={"Content-Type": "application/json"}
            )
            with urllib.request.urlopen(req, timeout=15) as r:
                return _json.loads(r.read().decode())
        data = await loop.run_in_executor(None, _balance_check)
        logger.info(f"OxaPay balance check for {did}: {data}")

        if data.get("result") != 100:
            await update.message.reply_text(
                f"⚠️ OxaPay error {data.get('result')}: {data.get('message', 'Unknown')}\n"
                f"Try again or use <b>/verify</b>.",
                parse_mode="HTML"
            )
            return

        pay_status   = (data.get("status") or data.get("paymentStatus") or "").lower()
        received_amt = data.get("receivedAmount") or data.get("receivedAmountCrypto") or 0
        expected_amt = data.get("payAmount") or qty

        status_label = {
            "paid":    "✅ Paid — Confirmed",
            "waiting": "⏳ Waiting — Not received yet",
            "expired": "⌛ Expired — Run /deposit again",
            "failed":  "❌ Failed",
        }.get(pay_status, f"❓ {pay_status}")

        next_step = "➡️ Use /verify to confirm funding." if pay_status == "paid" else "⏳ Waiting for blockchain confirmation."

        partial_rel = float(deal.get("partial_released", 0))
        remaining_bal = round(qty - partial_rel, 8)
        escrow_line = ""
        if partial_rel > 0:
            escrow_line = (
                f"📤 Released So Far: <b>{partial_rel} {sym}</b>\n"
                f"🔒 Remaining in Escrow: <b>{remaining_bal} {sym}</b>\n"
            )

        await update.message.reply_text(
            f"💰 <b>Deposit Balance</b>\n\n"
            f"🆔 Deal: <code>{did}</code>\n"
            f"🪙 Token: {token_lbl}\n\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"📥 Total Deposited: <b>{expected_amt} {sym}</b>\n"
            f"✅ Received:        <b>{received_amt} {sym}</b>\n"
            f"{escrow_line}"
            f"━━━━━━━━━━━━━━━━━━━━\n\n"
            f"📊 Payment Status: {status_label}\n"
            f"📬 Address: <code>{addr}</code>\n"
            f"🔎 Track ID: <code>{track_id}</code>\n\n"
            f"{next_step}",
            parse_mode="HTML"
        )
    except Exception as e:
        await update.message.reply_text(
            f"❌ Could not fetch balance: <code>{e}</code>",
            parse_mode="HTML"
        )


# ══════════════════════════════════════════════════════════
# /admindeposit — admin manually marks deposit as confirmed
# (used when OxaPay cannot verify but admin confirms payment)
# ══════════════════════════════════════════════════════════

async def cmd_admindeposit(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Admin manually confirms deposit — for dispute cases where OxaPay fails to verify.
    Usage: /admindeposit DEAL_ID [amount]
    """
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("❌ Admin only command.")
        return

    if not ctx.args:
        await update.message.reply_text(
            "Usage: <code>/admindeposit DEAL_ID [amount]</code>\n\n"
            "Example: <code>/admindeposit ABC123XY 100</code>\n\n"
            "Use when OxaPay cannot verify but you have manually confirmed payment.",
            parse_mode="HTML"
        )
        return

    admin_user = update.effective_user
    did = ctx.args[0].upper()
    manual_amount = None
    if len(ctx.args) >= 2:
        try:
            manual_amount = float(ctx.args[1])
        except ValueError:
            pass

    deal = deal_by_id(did)
    if not deal:
        await update.message.reply_text(f"❌ Deal not found: <code>{did}</code>", parse_mode="HTML")
        return

    if deal.get("funded"):
        await update.message.reply_text("⚠️ Deal is already marked as funded.")
        return

    if deal.get("status") == "COMPLETED":
        await update.message.reply_text("⚠️ Deal already completed.")
        return

    qty       = manual_amount or float(deal.get("quantity", 0))
    token     = deal.get("token", "")
    sym       = TOKEN_SYMBOL.get(token, token)
    token_lbl = TOKEN_LABELS.get(token, token)
    group_id  = deal.get("group_id")

    from datetime import timezone, timedelta
    IST = timezone(timedelta(hours=5, minutes=30))
    now_ist = datetime.now(IST).strftime("%d %b %Y, %I:%M %p IST")

    deal["funded"]           = True
    deal["status"]           = "FUNDED"
    deal["funded_by"]        = f"ADMIN:{admin_user.username or admin_user.id}"
    deal["funded_at"]        = datetime.utcnow().isoformat()
    deal["admin_deposit"]    = True
    deal["admin_deposit_by"] = admin_user.username or str(admin_user.id)
    if manual_amount:
        deal["admin_confirmed_amount"] = manual_amount

    # Notify the deal group
    try:
        await ctx.bot.send_message(
            chat_id=group_id,
            text=(
                f"✅ <b>ADMIN DEPOSIT CONFIRMED</b>\n\n"
                f"👨‍💼 Admin: @{admin_user.username or admin_user.id}\n"
                f"🆔 Deal: <code>{did}</code>\n"
                f"🪙 Token: {token_lbl}\n"
                f"💰 Amount: <b>{qty} {sym}</b>\n\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"🛒 Buyer: @{deal.get('buyer_username', 'N/A')}\n"
                f"📥 Buyer Wallet: <code>{deal.get('buyer_address', 'N/A')}</code>\n\n"
                f"🏪 Seller: @{deal.get('seller_username', 'N/A')}\n"
                f"📤 Seller Wallet: <code>{deal.get('seller_address', 'N/A')}</code>\n\n"
                f"⚠️ <i>Deposit manually confirmed by admin (OxaPay bypassed)</i>\n"
                f"📌 Buyer: Now send fiat payment to seller off-platform.\n"
                f"Once done, use <b>/release</b> to complete the deal.\n\n"
                f"⏰ {now_ist}"
            ),
            parse_mode="HTML"
        )
    except Exception as e:
        logger.warning(f"Could not notify group {group_id}: {e}")

    # Confirm to admin
    await update.message.reply_text(
        f"✅ <b>Deposit manually confirmed!</b>\n\n"
        f"🆔 Deal: <code>{did}</code>\n"
        f"💰 Amount: {qty} {sym}\n"
        f"🛒 Buyer: @{deal.get('buyer_username', 'N/A')}\n"
        f"🏪 Seller: @{deal.get('seller_username', 'N/A')}\n\n"
        f"📊 Status set to: <b>FUNDED</b>\n"
        f"➡️ Parties can now use /release to complete.",
        parse_mode="HTML"
    )

    await log(ctx,
        f"✅ <b>ADMIN DEPOSIT CONFIRMED</b>\n\n"
        f"🆔 Deal: <code>{did}</code>\n"
        f"🪙 Token: {token_lbl}  💰 Amount: {qty} {sym}\n\n"
        f"🛒 Buyer: @{deal.get('buyer_username')} ({deal.get('buyer_id')})\n"
        f"📥 Buyer Wallet: <code>{deal.get('buyer_address', 'N/A')}</code>\n\n"
        f"🏪 Seller: @{deal.get('seller_username')} ({deal.get('seller_id')})\n"
        f"📤 Seller Wallet: <code>{deal.get('seller_address', 'N/A')}</code>\n\n"
        f"👨‍💼 Confirmed by: @{admin_user.username or admin_user.id}\n"
        f"📊 Status: FUNDED (Admin Manual)\n⏰ {now_ist}"
    )


# ══════════════════════════════════════════════════════════
# /adminforcedeposit — admin sends payout to ANY address
# ══════════════════════════════════════════════════════════

async def cmd_adminforcedeposit(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """
    Admin force-sends any amount to any wallet address via OxaPay.
    Usage: /adminforcedeposit ADDRESS AMOUNT TOKEN
    TOKEN options: USDT_TRC20 | USDT_BEP20 | BTC | LTC
    Example: /adminforcedeposit TXxyz... 50 USDT_TRC20
    """
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("❌ Admin only.")
        return

    if len(ctx.args) < 3:
        await update.message.reply_text(
            "📤 <b>Admin Force Deposit</b>\n\n"
            "Usage: <code>/adminforcedeposit ADDRESS AMOUNT TOKEN</code>\n\n"
            "Tokens: <code>USDT_TRC20</code> | <code>USDT_BEP20</code> | <code>BTC</code> | <code>LTC</code>\n\n"
            "Example:\n"
            "<code>/adminforcedeposit TXxyz123 50 USDT_TRC20</code>",
            parse_mode="HTML"
        )
        return

    admin_user  = update.effective_user
    to_address  = ctx.args[0].strip()
    try:
        amount = float(ctx.args[1])
    except ValueError:
        await update.message.reply_text("❌ Invalid amount. Must be a number.")
        return
    token = ctx.args[2].upper().strip()

    TOKEN_NET_MAP = {
        "USDT_TRC20": ("USDT", "TRX"),
        "USDT_BEP20": ("USDT", "BSC"),
        "BTC":        ("BTC",  "BTC"),
        "LTC":        ("LTC",  "LTC"),
    }
    if token not in TOKEN_NET_MAP:
        await update.message.reply_text(
            f"❌ Unknown token: <code>{token}</code>\n\n"
            f"Valid options: USDT_TRC20, USDT_BEP20, BTC, LTC",
            parse_mode="HTML"
        )
        return

    currency, network = TOKEN_NET_MAP[token]
    sym = TOKEN_SYMBOL.get(token, token)

    if not state.oxapay_key:
        await update.message.reply_text("❌ OxaPay key not set. Use /setoxapay first.")
        return

    await update.message.reply_text(
        f"⏳ Sending <b>{amount} {sym}</b> to <code>{to_address}</code>…",
        parse_mode="HTML"
    )

    from datetime import timezone, timedelta
    IST = timezone(timedelta(hours=5, minutes=30))
    now_ist = datetime.now(IST).strftime("%d %b %Y, %I:%M %p IST")

    try:
        loop = asyncio.get_event_loop()
        def _force_payout():
            req = urllib.request.Request(
                "https://api.oxapay.com/merchants/payout",
                data=_json.dumps({
                    "merchant":    state.oxapay_key,
                    "address":     to_address,
                    "amount":      round(amount, 8),
                    "currency":    currency,
                    "network":     network,
                    "callbackUrl": "",
                    "description": f"Admin Force Deposit by @{admin_user.username or admin_user.id}",
                }).encode(),
                headers={"Content-Type": "application/json"}
            )
            with urllib.request.urlopen(req, timeout=30) as r:
                return _json.loads(r.read().decode())
        pdata = await loop.run_in_executor(None, _force_payout)
        logger.info(f"Admin force deposit response: {pdata}")

        if pdata.get("result") == 100:
            txid = pdata.get("trackId") or pdata.get("txID") or "N/A"
            await update.message.reply_text(
                f"✅ <b>Force Deposit Sent!</b>\n\n"
                f"📬 To: <code>{to_address}</code>\n"
                f"💰 Amount: <b>{amount} {sym}</b>\n"
                f"🔗 Track ID: <code>{txid}</code>\n"
                f"⏰ {now_ist}",
                parse_mode="HTML"
            )
            await log(ctx,
                f"📤 <b>ADMIN FORCE DEPOSIT</b>\n\n"
                f"👨‍💼 Admin: @{admin_user.username or admin_user.id}\n"
                f"📬 To: <code>{to_address}</code>\n"
                f"💰 {amount} {sym} ({token})\n"
                f"🔗 TX: <code>{txid}</code>\n"
                f"⏰ {now_ist}"
            )
        else:
            err_msg = f"OxaPay error {pdata.get('result')}: {pdata.get('message', 'Unknown')}"
            await update.message.reply_text(
                f"❌ <b>Payout Failed</b>\n\n<code>{err_msg}</code>",
                parse_mode="HTML"
            )
            await log(ctx, f"❌ <b>ADMIN FORCE DEPOSIT FAILED</b>\n\n{err_msg}\n👨‍💼 @{admin_user.username}")
    except Exception as e:
        await update.message.reply_text(f"❌ Error: <code>{e}</code>", parse_mode="HTML")
        await log(ctx, f"❌ <b>ADMIN FORCE DEPOSIT ERROR</b>\n\n{e}")


# ══════════════════════════════════════════════════════════
# PARTIAL RELEASE: /release X  — release X amount to buyer, hold rest
# RELEASE ALL:    /release all — release full amount to buyer
# ══════════════════════════════════════════════════════════

async def cmd_release_partial(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """
    /release all  -> release full remaining amount to buyer
    /release X    -> release X amount to buyer, rest stays in escrow
    """
    chat = update.effective_chat
    user = update.effective_user

    if chat.type == "private":
        await update.message.reply_text("\u274c Use inside your deal group.")
        return

    did, deal = deal_by_group(chat.id)
    if not deal:
        await update.message.reply_text("\u274c No active deal here.")
        return

    if not deal.get("funded"):
        await update.message.reply_text("\u26a0\ufe0f Deal not funded yet. Use /verify first.", parse_mode="HTML")
        return

    blocked_statuses = {
        "COMPLETED": "\u26a0\ufe0f Deal already completed.",
        "CANCELLED": "\u26a0\ufe0f Deal has been cancelled.",
        "DISPUTED": "\u26a0\ufe0f Deal is under dispute. Wait for admin resolution.",
        "AWAITING_CONFIRMATION": "\u26a0\ufe0f Confirmation already pending. Both parties must confirm.",
    }
    if deal["status"] in blocked_statuses:
        await update.message.reply_text(blocked_statuses[deal["status"]])
        return

    if user.id not in (deal.get("buyer_id"), deal.get("seller_id")):
        await update.message.reply_text("\u274c Only buyer or seller can trigger release.")
        return

    token     = deal.get("token", "")
    sym       = TOKEN_SYMBOL.get(token, token)
    tok_lbl   = TOKEN_LABELS.get(token, token)
    total_qty = float(deal.get("quantity", 0))
    released_so_far = float(deal.get("partial_released", 0))
    remaining = round(total_qty - released_so_far, 8)

    if remaining <= 0:
        await update.message.reply_text("\u2705 Full amount already released.")
        return

    arg = ctx.args[0].strip().lower() if ctx.args else "all"

    if arg == "all":
        release_amt = remaining
        is_full = True
    else:
        try:
            release_amt = float(arg)
        except ValueError:
            await update.message.reply_text(
                "\u274c Invalid amount.\n\nUsage:\n"
                "<code>/release all</code> \u2014 release everything\n"
                "<code>/release 50</code> \u2014 release 50 to buyer",
                parse_mode="HTML"
            )
            return

        if release_amt <= 0:
            await update.message.reply_text("\u274c Amount must be greater than 0.")
            return

        if release_amt > remaining:
            await update.message.reply_text(
                f"\u274c Cannot release {release_amt} {sym}.\n"
                f"Only {remaining} {sym} remaining in escrow.",
                parse_mode="HTML"
            )
            return

        is_full = (round(release_amt, 8) >= round(remaining, 8))

    fee_pct        = state.fee_percent
    fee_amt        = round(release_amt * fee_pct / 100, 8)
    final_to_buyer = round(release_amt - fee_amt, 8)
    after_release  = round(remaining - release_amt, 8)

    who = "Buyer" if user.id == deal.get("buyer_id") else "Seller"

    deal["pending_partial_release"] = {
        "amount":         release_amt,
        "fee_amt":        fee_amt,
        "final_to_buyer": final_to_buyer,
        "after_release":  after_release,
        "is_full":        is_full,
        "initiated_by":   user.username or str(user.id),
    }
    deal["status"]           = "AWAITING_CONFIRMATION"
    deal["buyer_confirmed"]  = (user.id == deal.get("buyer_id"))
    deal["seller_confirmed"] = (user.id == deal.get("seller_id"))

    b = deal["buyer_confirmed"]
    s = deal["seller_confirmed"]
    release_label = "FULL RELEASE" if is_full else f"PARTIAL RELEASE"

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(f"{'\u2705' if b else '\u23f3'} Buyer Confirm",  callback_data=f"partial_confirm:buyer:{did}"),
         InlineKeyboardButton(f"{'\u2705' if s else '\u23f3'} Seller Confirm", callback_data=f"partial_confirm:seller:{did}")],
        [InlineKeyboardButton("\U0001f6a8 Dispute / Call Admin", callback_data=f"dispute_call:{did}")]
    ])

    msg = (
        f"\U0001f513 <b>{release_label} INITIATED</b>\n\n"
        f"\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
        f"Triggered by: <b>{who}</b> (@{user.username or user.first_name})\n"
        f"\U0001f194 Deal: <code>{did}</code>\n"
        f"\U0001fa99 Token: {tok_lbl}\n\n"
        f"\U0001f4b0 Total in Escrow:    <b>{remaining} {sym}</b>\n"
        f"\U0001f4e4 Releasing to Buyer: <b>{release_amt} {sym}</b>\n"
        f"\U0001f4b8 Fee ({fee_pct}%):         <b>{fee_amt} {sym}</b>\n"
        f"\u2705 Buyer Receives:     <b>{final_to_buyer} {sym}</b>\n"
        f"\U0001f512 Remaining in Escrow: <b>{after_release} {sym}</b>\n\n"
        f"\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
        f"\U0001f6d2 Buyer: @{deal.get('buyer_username','N/A')} \u2014 {'\u2705 Confirmed' if b else '\u23f3 Waiting'}\n"
        f"\U0001f3ea Seller: @{deal.get('seller_username','N/A')} \u2014 {'\u2705 Confirmed' if s else '\u23f3 Waiting'}\n\n"
        f"\u26a0\ufe0f Waiting for both parties to confirm."
    )
    await update.message.reply_text(msg, reply_markup=kb, parse_mode="HTML")


async def partial_confirm_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Handle partial_confirm:buyer/seller:DID button presses."""
    query = update.callback_query
    await query.answer()
    parts = query.data.split(":")
    if len(parts) < 3:
        return
    _, role, did = parts[0], parts[1], parts[2]

    deal = deal_by_id(did)
    if not deal:
        await query.edit_message_text("\u274c Deal not found.")
        return

    pending = deal.get("pending_partial_release")
    if not pending:
        await query.edit_message_text("\u274c No pending release found.")
        return

    user = query.from_user
    if role == "buyer" and user.id == deal.get("buyer_id"):
        deal["buyer_confirmed"] = True
    elif role == "seller" and user.id == deal.get("seller_id"):
        deal["seller_confirmed"] = True
    else:
        await query.answer("\u274c This button is not for you.", show_alert=True)
        return

    b = deal["buyer_confirmed"]
    s = deal["seller_confirmed"]
    token  = deal.get("token", "")
    sym    = TOKEN_SYMBOL.get(token, token)
    tok_lbl = TOKEN_LABELS.get(token, token)
    release_amt    = pending["amount"]
    fee_amt        = pending["fee_amt"]
    final_to_buyer = pending["final_to_buyer"]
    after_release  = pending["after_release"]
    is_full        = pending["is_full"]

    if not (b and s):
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton(f"{'\u2705' if b else '\u23f3'} Buyer Confirm",  callback_data=f"partial_confirm:buyer:{did}"),
             InlineKeyboardButton(f"{'\u2705' if s else '\u23f3'} Seller Confirm", callback_data=f"partial_confirm:seller:{did}")],
            [InlineKeyboardButton("\U0001f6a8 Dispute", callback_data=f"dispute_call:{did}")]
        ])
        await query.edit_message_text(
            f"\U0001f513 <b>{'FULL' if is_full else 'PARTIAL'} RELEASE</b>\n\n"
            f"\U0001f6d2 Buyer:  {'\u2705 Confirmed' if b else '\u23f3 Waiting'}\n"
            f"\U0001f3ea Seller: {'\u2705 Confirmed' if s else '\u23f3 Waiting'}\n\n"
            f"\U0001f4e4 Releasing: <b>{release_amt} {sym}</b>\n"
            f"\u2705 Buyer gets: <b>{final_to_buyer} {sym}</b>\n"
            f"\U0001f512 Remaining: <b>{after_release} {sym}</b>\n\n"
            f"\u26a0\ufe0f Waiting for both to confirm\u2026",
            reply_markup=kb, parse_mode="HTML"
        )
        return

    # Both confirmed — execute payout
    await query.edit_message_text(
        f"\U0001f389 <b>BOTH CONFIRMED!</b>\n\n"
        f"\u23f3 Processing payout of <b>{final_to_buyer} {sym}</b> to buyer\u2026",
        parse_mode="HTML"
    )

    TOKEN_NET_MAP = {
        "USDT_TRC20": ("USDT", "TRX"),
        "USDT_BEP20": ("USDT", "BSC"),
        "BTC":        ("BTC",  "BTC"),
        "LTC":        ("LTC",  "LTC"),
    }
    currency, network = TOKEN_NET_MAP.get(token, ("USDT", "TRX"))
    buyer_addr = deal.get("buyer_address", "N/A")

    payout_success = False
    payout_txid    = None
    payout_err     = None

    if state.oxapay_key and buyer_addr and buyer_addr != "N/A" and final_to_buyer > 0:
        try:
            loop = asyncio.get_event_loop()
            def _payout():
                req = urllib.request.Request(
                    "https://api.oxapay.com/merchants/payout",
                    data=_json.dumps({
                        "merchant":    state.oxapay_key,
                        "address":     buyer_addr,
                        "amount":      round(final_to_buyer, 8),
                        "currency":    currency,
                        "network":     network,
                        "callbackUrl": "",
                        "description": f"Partial Release {did}",
                    }).encode(),
                    headers={"Content-Type": "application/json"}
                )
                with urllib.request.urlopen(req, timeout=30) as r:
                    return _json.loads(r.read().decode())
            pdata = await loop.run_in_executor(None, _payout)
            logger.info(f"OxaPay partial payout for {did}: {pdata}")
            if pdata.get("result") == 100:
                payout_success = True
                payout_txid = pdata.get("trackId") or "N/A"
            else:
                payout_err = f"OxaPay error {pdata.get('result')}: {pdata.get('message','Unknown')}"
        except Exception as e:
            payout_err = str(e)
    else:
        payout_success = True
        payout_txid = "DEMO_MODE"

    if not payout_success:
        deal["status"] = "FUNDED"
        deal.pop("pending_partial_release", None)
        deal["buyer_confirmed"]  = False
        deal["seller_confirmed"] = False
        await ctx.bot.send_message(
            chat_id=deal["group_id"],
            text=f"\u274c <b>PAYOUT FAILED</b>\n\nError: <code>{payout_err}</code>\n\nUse /release again.",
            parse_mode="HTML"
        )
        await log(ctx, f"\u274c PARTIAL PAYOUT FAILED\n\U0001f194 {did}\n{payout_err}")
        return

    # Update deal state
    deal["partial_released"] = round(float(deal.get("partial_released", 0)) + release_amt, 8)
    deal.pop("pending_partial_release", None)
    deal["buyer_confirmed"]  = False
    deal["seller_confirmed"] = False
    deal["last_action_at"]   = datetime.utcnow().isoformat()

    from datetime import timezone, timedelta
    IST = timezone(timedelta(hours=5, minutes=30))
    now_ist = datetime.now(IST).strftime("%d %b %Y, %I:%M %p IST")
    tx_line = f"\U0001f517 TX: <code>{payout_txid}</code>\n" if payout_txid and payout_txid != "DEMO_MODE" else "\U0001f9ea DEMO MODE\n"
    total_released = deal["partial_released"]

    if is_full or after_release <= 0:
        deal["status"] = "COMPLETED"
        deal["completed_at"] = datetime.utcnow().isoformat()
        msg = (
            f"\u2705 <b>DEAL COMPLETED</b>\n\n"
            f"\U0001f194 Deal: <code>{did}</code>\n"
            f"\U0001fa99 Token: {tok_lbl}\n\n"
            f"\U0001f4b0 Released: <b>{release_amt} {sym}</b>\n"
            f"\u2705 Buyer Received: <b>{final_to_buyer} {sym}</b>\n"
            f"\U0001f4b8 Fee: <b>{fee_amt} {sym}</b>\n\n"
            f"{tx_line}"
            f"\u23f0 {now_ist}\n\n"
            f"\U0001f389 Escrow completed! Group closes in <b>1 minute</b>."
        )
        async def _delete_group_after_1min():
            await asyncio.sleep(60)
            try:
                if getattr(state, "vouch_enabled", True) and getattr(state, "vouch_group_id", None):
                    await ctx.bot.send_message(
                        chat_id=deal["group_id"],
                        text="⭐ <b>Deal Done!</b> Both parties will receive a vouch request in DM.\n<i>Group closes in ~2 minutes after vouch window.</i>",
                        parse_mode="HTML"
                    )
                    asyncio.create_task(send_vouch_request(ctx, did, deal))
                    return
                await ctx.bot.send_message(chat_id=deal["group_id"], text="\U0001f5d1 <b>Group closing now. Goodbye!</b>", parse_mode="HTML")
                for p in ("buyer", "seller"):
                    pid2 = deal.get(f"{p}_id")
                    if pid2:
                        try:
                            await ctx.bot.ban_chat_member(chat_id=deal["group_id"], user_id=pid2)
                            await ctx.bot.unban_chat_member(chat_id=deal["group_id"], user_id=pid2)
                        except Exception:
                            pass
                await ctx.bot.leave_chat(deal["group_id"])
                if state.telethon_client:
                    from telethon.tl.functions.channels import DeleteChannelRequest
                    try:
                        entity = await state.telethon_client.get_entity(deal["group_id"])
                        await state.telethon_client(DeleteChannelRequest(entity))
                    except Exception:
                        pass
            except Exception as ex:
                logger.warning(f"Could not close group {deal['group_id']}: {ex}")
        asyncio.create_task(_delete_group_after_1min())
    else:
        deal["status"] = "FUNDED"
        msg = (
            f"\u2705 <b>PARTIAL RELEASE DONE</b>\n\n"
            f"\U0001f194 Deal: <code>{did}</code>\n"
            f"\U0001fa99 Token: {tok_lbl}\n\n"
            f"\U0001f4e4 Released Now: <b>{release_amt} {sym}</b>\n"
            f"\u2705 Buyer Received: <b>{final_to_buyer} {sym}</b>\n"
            f"\U0001f4b8 Fee: <b>{fee_amt} {sym}</b>\n\n"
            f"\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
            f"\U0001f4ca Total Released: <b>{total_released} {sym}</b>\n"
            f"\U0001f512 Still in Escrow: <b>{after_release} {sym}</b>\n\n"
            f"{tx_line}"
            f"\u23f0 {now_ist}\n\n"
            f"\u27a1\ufe0f Seller can now use:\n"
            f"\u2022 <b>/release X</b> \u2014 release more to buyer\n"
            f"\u2022 <b>/release all</b> \u2014 release remaining\n"
            f"\u2022 <b>/refund Y</b> \u2014 refund Y back to seller\n\n"
            f"\u26a0\ufe0f Group auto-closes in <b>7 days</b> if no action. Daily reminders will be sent."
        )
        _partial_group_id = deal["group_id"]
        async def _wait_7days_partial():
            day_seconds = 86400
            days_waited = 0
            while days_waited < 7:
                await asyncio.sleep(day_seconds)
                days_waited += 1
                days_left = 7 - days_waited
                cur_remaining = round(float(deal.get("quantity", 0)) - float(deal.get("partial_released", 0)), 8)
                if deal.get("status") in ("COMPLETED", "REFUNDED", "CANCELLED"):
                    return
                if cur_remaining <= 0:
                    # Balance cleared — close in 1 min
                    await asyncio.sleep(60)
                    try:
                        await ctx.bot.send_message(chat_id=_partial_group_id, text="✅ <b>Escrow balance cleared. Group closing now.</b>", parse_mode="HTML")
                        await ctx.bot.leave_chat(_partial_group_id)
                        if state.telethon_client:
                            from telethon.tl.functions.channels import DeleteChannelRequest
                            try:
                                entity = await state.telethon_client.get_entity(_partial_group_id)
                                await state.telethon_client(DeleteChannelRequest(entity))
                            except Exception:
                                pass
                    except Exception:
                        pass
                    return
                # Daily notification
                notif_sym = TOKEN_SYMBOL.get(deal.get("token", ""), deal.get("token", ""))
                try:
                    await ctx.bot.send_message(
                        chat_id=_partial_group_id,
                        text=(
                            f"⏰ <b>Daily Escrow Reminder — Day {days_waited}/7</b>\n\n"
                            f"🔒 Remaining in Escrow: <b>{cur_remaining} {notif_sym}</b>\n"
                            f"📅 {days_left} day(s) until group auto-closes.\n\n"
                            f"➡️ Use /release or /refund to settle."
                        ),
                        parse_mode="HTML"
                    )
                except Exception:
                    pass
            # 7 days done
            try:
                await ctx.bot.send_message(chat_id=_partial_group_id, text="⌛ <b>7-day period ended. Group closing now.</b>", parse_mode="HTML")
                await ctx.bot.leave_chat(_partial_group_id)
                if state.telethon_client:
                    from telethon.tl.functions.channels import DeleteChannelRequest
                    try:
                        entity = await state.telethon_client.get_entity(_partial_group_id)
                        await state.telethon_client(DeleteChannelRequest(entity))
                    except Exception:
                        pass
            except Exception as ex:
                logger.warning(f"Could not close group {_partial_group_id} after 7 days: {ex}")
        asyncio.create_task(_wait_7days_partial())

    await ctx.bot.send_message(chat_id=deal["group_id"], text=msg, parse_mode="HTML")
    await log(ctx,
        f"{'\u2705 DEAL COMPLETED' if deal['status'] == 'COMPLETED' else '\U0001f4e4 PARTIAL RELEASE'}\n\n"
        f"\U0001f194 {did}  \U0001fa99 {tok_lbl}\n"
        f"Released: {release_amt} {sym} -> Buyer gets: {final_to_buyer} {sym}\n"
        f"Remaining: {after_release} {sym}\nTX: {payout_txid}\n\u23f0 {now_ist}"
    )



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
    if deal.get("status") == "COMPLETED" and float(deal.get("quantity", 0)) - float(deal.get("partial_released", 0)) <= 0:
        await update.message.reply_text("⚠️ Deal already completed with no remaining balance.")
        return

    user = update.effective_user
    assigned = state.dispute_admins.get(did)
    if assigned and assigned != user.id and not is_main_admin(user.id):
        await update.message.reply_text("❌ Another admin is handling this dispute.")
        return

    group_id  = deal.get("group_id")
    total_qty = float(deal.get("quantity", 0))
    released_so_far = float(deal.get("partial_released", 0))
    remaining = round(total_qty - released_so_far, 8)
    fee_pct   = state.fee_percent
    fee_amt   = round(remaining * fee_pct / 100, 8)
    final     = round(remaining - fee_amt, 8)
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
    deal["partial_released"]  = float(deal.get("quantity", 0))  # mark all as released

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
        if getattr(state, "vouch_enabled", True) and getattr(state, "vouch_group_id", None):
            await ctx.bot.send_message(
                chat_id=group_id,
                text="⭐ <b>Deal Done!</b> Both parties will receive a vouch request in DM.\n<i>Group closes in ~2 minutes.</i>",
                parse_mode="HTML"
            )
            asyncio.create_task(send_vouch_request(ctx, did, deal))
        else:
            await ctx.bot.send_message(chat_id=group_id, text="🗑 <b>Group closing in 50 seconds.</b>", parse_mode="HTML")
            await asyncio.sleep(50)
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
    """
    /refund all  → refund full remaining escrow to seller (fee deducted)
    /refund Y    → refund Y amount to seller, rest stays in escrow
    Both buyer and seller must confirm.
    """
    user = update.effective_user
    chat = update.effective_chat

    # Determine deal — first arg could be deal_id or amount
    did  = None
    deal = None
    arg  = None

    if ctx.args:
        first = ctx.args[0].upper()
        # If it looks like a deal ID (not a number), treat it as deal ID
        try:
            float(ctx.args[0])
            # it's a number — deal from group
            arg = ctx.args[0].lower()
            if chat.type != "private":
                did, deal = deal_by_group(chat.id)
        except ValueError:
            if first == "ALL":
                arg = "all"
                if chat.type != "private":
                    did, deal = deal_by_group(chat.id)
            else:
                did  = first
                deal = deal_by_id(did)
                arg  = ctx.args[1].lower() if len(ctx.args) > 1 else "all"
    elif chat.type != "private":
        did, deal = deal_by_group(chat.id)
        arg = "all"

    if not deal:
        await update.message.reply_text(
            "Usage:\n"
            "<code>/refund all</code> — refund full amount to seller\n"
            "<code>/refund 50</code> — refund 50 to seller, rest stays\n",
            parse_mode="HTML"
        )
        return

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
    if deal.get("status") == "AWAITING_CONFIRMATION":
        await update.message.reply_text("⚠️ A release confirmation is already pending.")
        return

    # Not funded — only admin can cancel
    if not deal.get("funded"):
        if not is_admin(user.id):
            await update.message.reply_text("❌ Deal not funded yet. Only admin can cancel it.")
            return
        deal["status"]      = "REFUNDED"
        deal["refunded_by"] = user.username or str(user.id)
        deal["refunded_at"] = datetime.utcnow().isoformat()
        try:
            await ctx.bot.send_message(
                chat_id=deal["group_id"],
                text=(
                    f"🚫 <b>DEAL CANCELLED (No Payment Made)</b>\n\n"
                    f"👨‍💼 Admin: @{user.username or user.id}\n"
                    f"🆔 <code>{did}</code>\n\n"
                    f"No funds deposited. Deal cancelled.\n📊 Status: CANCELLED"
                ),
                parse_mode="HTML"
            )
        except Exception:
            pass
        await update.message.reply_text(f"✅ Deal <code>{did}</code> cancelled (no deposit).", parse_mode="HTML")
        await log(ctx, f"🚫 <b>DEAL CANCELLED (no deposit)</b>\n\n🆔 <code>{did}</code>\n👨‍💼 @{user.username}")
        return

    # Calculate remaining in escrow
    token     = deal.get("token", "")
    sym       = TOKEN_SYMBOL.get(token, token)
    tok_lbl   = TOKEN_LABELS.get(token, token)
    total_qty = float(deal.get("quantity", 0))
    released_so_far = float(deal.get("partial_released", 0))
    remaining = round(total_qty - released_so_far, 8)

    if remaining <= 0:
        await update.message.reply_text("⚠️ No funds remaining in escrow.")
        return

    # Parse refund amount
    if arg is None or arg == "all":
        refund_amt = remaining
        is_full = True
    else:
        try:
            refund_amt = float(arg)
        except ValueError:
            await update.message.reply_text(
                "❌ Invalid amount.\n\n"
                "Usage:\n"
                "<code>/refund all</code> — refund everything\n"
                "<code>/refund 50</code> — refund 50 to seller",
                parse_mode="HTML"
            )
            return
        if refund_amt <= 0:
            await update.message.reply_text("❌ Amount must be greater than 0.")
            return
        if refund_amt > remaining:
            await update.message.reply_text(
                f"❌ Cannot refund {refund_amt} {sym}.\n"
                f"Only {remaining} {sym} remaining in escrow.",
                parse_mode="HTML"
            )
            return
        is_full = (round(refund_amt, 8) >= round(remaining, 8))

    fee_pct    = state.fee_percent
    fee_amt    = round(refund_amt * fee_pct / 100, 8)
    seller_gets = round(refund_amt - fee_amt, 8)
    after_refund = round(remaining - refund_amt, 8)

    who_requested = "Admin" if is_admin(user.id) else ("Buyer" if user.id == deal.get("buyer_id") else "Seller")
    refund_label  = "FULL REFUND" if is_full else "PARTIAL REFUND"

    deal["status"]              = "AWAITING_REFUND_CONFIRM"
    deal["refund_buyer_ok"]     = False
    deal["refund_seller_ok"]    = False
    deal["refunded_by"]         = user.username or str(user.id)
    deal["pending_refund"] = {
        "amount":       refund_amt,
        "fee_amt":      fee_amt,
        "seller_gets":  seller_gets,
        "after_refund": after_refund,
        "is_full":      is_full,
    }

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Buyer Agrees Refund",  callback_data=f"refund_confirm:buyer:{did}"),
         InlineKeyboardButton("✅ Seller Agrees Refund", callback_data=f"refund_confirm:seller:{did}")],
        [InlineKeyboardButton("❌ Cancel Refund Request", callback_data=f"refund_cancel:{did}")]
    ])

    try:
        await ctx.bot.send_message(
            chat_id=deal["group_id"],
            text=(
                f"💸 <b>{refund_label} REQUESTED</b>\n\n"
                f"Requested by: <b>{who_requested}</b> (@{user.username or user.id})\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"🆔 Deal: <code>{did}</code>\n"
                f"🪙 Token: {tok_lbl}\n\n"
                f"🔒 Total in Escrow:   <b>{remaining} {sym}</b>\n"
                f"↩️ Refunding:          <b>{refund_amt} {sym}</b>\n"
                f"💸 Fee ({fee_pct}%):       <b>{fee_amt} {sym}</b>\n"
                f"🏪 Seller Receives:    <b>{seller_gets} {sym}</b>\n"
                f"🔒 Remaining After:    <b>{after_refund} {sym}</b>\n\n"
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
        f"✅ {refund_label} request sent.\nWaiting for both parties to confirm.",
        parse_mode="HTML"
    )
    await log(ctx,
        f"💸 <b>{refund_label} INITIATED</b>\n\n🆔 <code>{did}</code>\n"
        f"👨‍💼 @{deal['refunded_by']}\n"
        f"💰 Remaining: {remaining} {sym} → Refund: {refund_amt} {sym} → Seller gets: {seller_gets} {sym}\n"
        f"🔒 After: {after_refund} {sym}\n📊 Awaiting confirmation"
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
        sym     = TOKEN_SYMBOL.get(deal.get("token", ""), deal.get("token", ""))
        pending = deal.get("pending_refund", {})
        refund_amt  = pending.get("amount", 0)
        fee_amt     = pending.get("fee_amt", 0)
        seller_gets = pending.get("seller_gets", 0)
        after_refund= pending.get("after_refund", 0)
        is_full     = pending.get("is_full", True)
        total_qty   = float(deal.get("quantity", 0))
        released_so_far = float(deal.get("partial_released", 0))
        remaining   = round(total_qty - released_so_far, 8)
        await q.edit_message_text(
            f"💸 <b>REFUND CONFIRMATION</b>\n\n"
            f"🆔 <code>{did}</code>\n"
            f"🔒 Total in Escrow: {remaining:.6f} {sym}\n"
            f"↩️ Refunding:       <b>{refund_amt:.6f} {sym}</b>\n"
            f"💸 Fee ({state.fee_percent}%): {fee_amt:.6f} {sym}\n"
            f"🏪 Seller Receives: <b>{seller_gets:.6f} {sym}</b>\n"
            f"🔒 Remaining After: <b>{after_refund:.6f} {sym}</b>\n\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"🛒 Buyer:  {'✅ Confirmed' if b_ok else '⏳ Waiting'}\n"
            f"🏪 Seller: {'✅ Confirmed' if s_ok else '⏳ Waiting'}",
            reply_markup=kb, parse_mode="HTML"
        )
        return

    # BOTH confirmed — process refund
    sym     = TOKEN_SYMBOL.get(deal.get("token", ""), deal.get("token", ""))
    token   = deal.get("token", "")
    token_label = TOKEN_LABELS.get(token, token)
    pending = deal.get("pending_refund", {})
    refund_amt  = pending.get("amount", 0) if pending else float(deal.get("quantity", 0))
    fee_amt     = pending.get("fee_amt", 0) if pending else round(refund_amt * state.fee_percent / 100, 8)
    seller_gets = pending.get("seller_gets", 0) if pending else round(refund_amt - fee_amt, 8)
    after_refund= pending.get("after_refund", 0) if pending else 0
    is_full     = pending.get("is_full", True) if pending else True
    qty         = float(deal.get("quantity", 0))
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
                        "callbackUrl": "",
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
                payout_err = f"OxaPay error {pdata.get('result')}: {pdata.get('message', 'Unknown error')}"
        except Exception as e:
            payout_err = str(e)
    else:
        payout_success = True
        payout_txid = "DEMO_MODE"

    # Update partial_released to reflect refunded amount (reduces escrow balance)
    released_so_far = float(deal.get("partial_released", 0))
    deal["partial_released"] = round(released_so_far + refund_amt, 8)
    deal.pop("pending_refund", None)
    deal["refund_txid"]      = payout_txid
    deal["fee_deducted"]     = fee_amt
    deal["refunded_at"]      = datetime.utcnow().isoformat()
    deal["last_action_at"]   = datetime.utcnow().isoformat()

    # Determine if fully settled
    total_qty      = float(deal.get("quantity", 0))
    new_partial    = deal["partial_released"]
    new_remaining  = round(total_qty - new_partial, 8)
    fully_settled  = is_full or new_remaining <= 0

    if fully_settled:
        deal["status"] = "REFUNDED"
    else:
        deal["status"] = "FUNDED"  # Partial refund — balance still remaining

    if not payout_success:
        # Restore state on failure
        deal["partial_released"] = released_so_far
        deal["status"] = "FUNDED" if deal.get("funded") else "TOKEN_SELECTED"
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

    if fully_settled:
        group_close_note = "<i>Group closes in 1 minute.</i>"
    else:
        group_close_note = (
            f"🔒 <b>Remaining in Escrow: {new_remaining} {sym}</b>\n"
            f"➡️ Use /release or /refund to settle remaining.\n"
            f"<i>Group closes in 7 days if no further action.</i>"
        )

    # Notify group
    try:
        await ctx.bot.send_message(
            chat_id=deal["group_id"],
            text=(
                f"↩️ <b>{'DEAL REFUNDED' if fully_settled else 'PARTIAL REFUND DONE'}</b>\n\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"🆔 Trade ID: <code>{did}</code>\n"
                f"🪙 Token: {token_label}\n\n"
                f"🔒 In Escrow:    {round(total_qty - released_so_far, 8):.6f} {sym}\n"
                f"↩️ Refunded Now: <b>{refund_amt:.6f} {sym}</b>\n"
                f"💸 Fee ({state.fee_percent}%): {fee_amt:.6f} {sym}\n"
                f"🏪 Seller Gets:  <b>{seller_gets:.6f} {sym}</b>\n"
                f"{tx_line}"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"🛒 Buyer: @{deal.get('buyer_username','N/A')} — ✅ Confirmed\n"
                f"🏪 Seller: @{deal.get('seller_username','N/A')} — ✅ Confirmed\n"
                f"📤 Refunded to Seller Wallet:\n<code>{seller_addr}</code>\n\n"
                f"⏰ {refunded_ist}\n\n"
                f"{group_close_note}"
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
                        f"↩️ <b>{'Deal Refunded' if fully_settled else 'Partial Refund'}: {did}</b>\n\n"
                        f"🪙 {token_label}\n"
                        f"↩️ Refunded: {refund_amt:.6f} {sym}\n"
                        f"💸 Fee: {fee_amt:.6f} {sym}\n"
                        f"🏪 Seller Gets: {seller_gets:.6f} {sym}\n"
                        f"🔒 Still in Escrow: {new_remaining:.6f} {sym}\n"
                        f"📤 Seller Wallet: <code>{seller_addr}</code>\n\n"
                        f"⏰ {refunded_ist}"
                    ),
                    parse_mode="HTML"
                )
            except Exception:
                pass

    await log(ctx,
        f"↩️ <b>{'DEAL REFUNDED' if fully_settled else 'PARTIAL REFUND'}</b>\n\n🆔 <code>{did}</code>\n"
        f"🪙 {token_label}\n"
        f"↩️ Refunded: {refund_amt:.6f} → Seller gets: {seller_gets:.6f} {sym}\n"
        f"🔒 Remaining: {new_remaining:.6f} {sym}\n"
        f"📤 Seller: <code>{seller_addr}</code>\n"
        f"🔗 TX: {payout_txid}\n"
        f"⏰ {refunded_ist}"
    )

    # ── Group close logic ──
    group_id_for_close = deal.get("group_id")
    if fully_settled:
        # Balance = 0 → vouch then close
        async def _close_group_refunded():
            await asyncio.sleep(10)
            try:
                if getattr(state, "vouch_enabled", True) and getattr(state, "vouch_group_id", None):
                    await ctx.bot.send_message(
                        chat_id=group_id_for_close,
                        text="⭐ <b>Deal Done!</b> Both parties will receive a vouch request in DM.\n<i>Group closes in ~2 minutes.</i>",
                        parse_mode="HTML"
                    )
                    asyncio.create_task(send_vouch_request(ctx, did, deal))
                    return
                await ctx.bot.send_message(chat_id=group_id_for_close, text="🗑 <b>Group closing now. Thank you!</b>", parse_mode="HTML")
                for p in ("buyer", "seller"):
                    pid2 = deal.get(f"{p}_id")
                    if pid2:
                        try:
                            await ctx.bot.ban_chat_member(chat_id=group_id_for_close, user_id=pid2)
                            await ctx.bot.unban_chat_member(chat_id=group_id_for_close, user_id=pid2)
                        except Exception:
                            pass
                await ctx.bot.leave_chat(group_id_for_close)
                if state.telethon_client:
                    from telethon.tl.functions.channels import DeleteChannelRequest
                    try:
                        entity = await state.telethon_client.get_entity(group_id_for_close)
                        await state.telethon_client(DeleteChannelRequest(entity))
                    except Exception:
                        pass
            except Exception as ex:
                logger.warning(f"Could not close group {group_id_for_close}: {ex}")
        asyncio.create_task(_close_group_refunded())
    else:
        # Balance remaining → wait 7 days, send daily notifications
        async def _wait_7days_then_close_refund():
            seven_days = 7 * 86400
            day_seconds = 86400
            days_waited = 0
            while days_waited < 7:
                await asyncio.sleep(day_seconds)
                days_waited += 1
                days_left = 7 - days_waited
                cur_remaining = round(float(deal.get("quantity", 0)) - float(deal.get("partial_released", 0)), 8)
                if deal.get("status") in ("COMPLETED", "REFUNDED", "CANCELLED"):
                    return
                if cur_remaining <= 0:
                    # Balance cleared mid-wait — close in 1 min
                    await asyncio.sleep(60)
                    try:
                        await ctx.bot.send_message(chat_id=group_id_for_close, text="✅ <b>Escrow balance cleared. Group closing now.</b>", parse_mode="HTML")
                        await ctx.bot.leave_chat(group_id_for_close)
                        if state.telethon_client:
                            from telethon.tl.functions.channels import DeleteChannelRequest
                            try:
                                entity = await state.telethon_client.get_entity(group_id_for_close)
                                await state.telethon_client(DeleteChannelRequest(entity))
                            except Exception:
                                pass
                    except Exception:
                        pass
                    return
                # Send daily notification
                notif_sym = TOKEN_SYMBOL.get(deal.get("token", ""), deal.get("token", ""))
                try:
                    await ctx.bot.send_message(
                        chat_id=group_id_for_close,
                        text=(
                            f"⏰ <b>Daily Escrow Reminder — Day {days_waited}/7</b>\n\n"
                            f"🔒 Remaining in Escrow: <b>{cur_remaining} {notif_sym}</b>\n"
                            f"📅 {days_left} day(s) until group auto-closes.\n\n"
                            f"➡️ Use /release or /refund to settle."
                        ),
                        parse_mode="HTML"
                    )
                except Exception:
                    pass
                if days_waited >= 7:
                    break
            # 7 days done — close group
            try:
                await ctx.bot.send_message(
                    chat_id=group_id_for_close,
                    text="⌛ <b>7-day period ended. Group closing now.</b>",
                    parse_mode="HTML"
                )
                await ctx.bot.leave_chat(group_id_for_close)
                if state.telethon_client:
                    from telethon.tl.functions.channels import DeleteChannelRequest
                    try:
                        entity = await state.telethon_client.get_entity(group_id_for_close)
                        await state.telethon_client(DeleteChannelRequest(entity))
                    except Exception:
                        pass
            except Exception as ex:
                logger.warning(f"Could not close group {group_id_for_close} after 7 days: {ex}")
        asyncio.create_task(_wait_7days_then_close_refund())


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
    _resolvable = ("DISPUTED", "AWAITING_CONFIRMATION", "AWAITING_REFUND_CONFIRM", "FUNDED", "TOKEN_SELECTED", "ROLES_SET")
    if deal.get("status") not in _resolvable:
        await update.message.reply_text(
            f"⚠️ Cannot use /disputeend on status: <b>{deal.get('status')}</b>\n\n"
            f"Resolvable statuses: {', '.join(_resolvable)}",
            parse_mode="HTML"
        )
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

async def cmd_setvouchgroup(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Set the vouch group where deal reviews are forwarded.
    Usage: /setvouchgroup  (inside the group)
    OR:    /setvouchgroup -100XXXXXXXXXX  (from anywhere)
    """
    chat = update.effective_chat
    user = update.effective_user
    if not is_main_admin(user.id):
        return

    if ctx.args:
        arg = ctx.args[0].strip()
        try:
            gid = int(arg)
            state.vouch_group_id = gid
            await update.message.reply_text(
                f"✅ <b>VOUCH GROUP SET!</b>\n\n🆔 <code>{gid}</code>\n\nAll deal vouches will be forwarded there.",
                parse_mode="HTML"
            )
        except ValueError:
            try:
                chat_obj = await ctx.bot.get_chat(arg)
                state.vouch_group_id = chat_obj.id
                await update.message.reply_text(
                    f"✅ <b>VOUCH GROUP SET!</b>\n\n📋 {chat_obj.title}\n🆔 <code>{chat_obj.id}</code>",
                    parse_mode="HTML"
                )
            except Exception as e:
                await update.message.reply_text(
                    f"❌ Could not resolve group: {e}\n\nMake sure the bot is a member, then try:\n<code>/setvouchgroup GROUP_ID</code>",
                    parse_mode="HTML"
                )
        return

    if chat.type == "private":
        await update.message.reply_text(
            "❌ Run inside the vouch group, or provide group ID:\n<code>/setvouchgroup -100XXXXXXXXXX</code>",
            parse_mode="HTML"
        )
        return

    state.vouch_group_id = chat.id
    await update.message.reply_text(
        f"✅ <b>VOUCH GROUP SET!</b>\n\n📋 {chat.title}\n🆔 <code>{chat.id}</code>\n\nAll deal vouches will be forwarded here.",
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
                    "trackId":  1
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
    vg = f"✅ <code>{state.vouch_group_id}</code>" if getattr(state, "vouch_group_id", None) else "❌ Not Set"
    ve = "✅ ON" if getattr(state, "vouch_enabled", True) else "❌ OFF"
    await update.message.reply_text(
        f"📊 <b>BOT STATUS</b>\n\n📋 Log Group: {lg}\n🚨 Dispute Group: {dg}\n⭐ Vouch Group: {vg}\n⭐ Vouch System: {ve}\n🔑 OxaPay: {ox}\n📡 Telethon: {tc}\n"
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
    if update.effective_chat.type != "private":
        return

    # Check vouch text waiting first (any user, not just admin)
    if user.id in _vouch_text_waiting:
        await process_vouch_text(update, ctx)
        return

    if not is_main_admin(user.id):
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
# VOUCH SYSTEM
# ══════════════════════════════════════════════════════════

async def send_vouch_request(ctx, did: str, deal: dict):
    """
    After deal completion, DM both buyer and seller asking for a vouch.
    Each gets: vouch button + skip button.
    If no response in 2 minutes → treat as skip → delete group.
    If vouch given → forward to vouch group → then delete group.
    """
    if not getattr(state, "vouch_enabled", True):
        return
    if not getattr(state, "vouch_group_id", None):
        return

    group_id   = deal.get("group_id")
    token_lbl  = TOKEN_LABELS.get(deal.get("token", ""), deal.get("token", ""))
    sym        = TOKEN_SYMBOL.get(deal.get("token", ""), deal.get("token", ""))
    qty        = float(deal.get("quantity", 0))

    vouch_responses = {}  # user_id -> "vouched" | "skipped"

    async def _ask_party(pid, role, other_username):
        if not pid:
            vouch_responses[role] = "skipped"
            return
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("⭐ Leave a Vouch", callback_data=f"vouch:start:{did}:{role}"),
             InlineKeyboardButton("⏭ Skip", callback_data=f"vouch:skip:{did}:{role}")]
        ])
        try:
            await ctx.bot.send_message(
                chat_id=pid,
                text=(
                    f"⭐ <b>Deal Completed! Leave a Vouch?</b>\n\n"
                    f"🆔 Deal: <code>{did}</code>\n"
                    f"🪙 Token: {token_lbl} | 💰 {qty} {sym}\n"
                    f"🤝 Your counterparty: @{other_username or 'N/A'}\n\n"
                    f"Your review will be posted in the vouch channel.\n"
                    f"<i>You have 2 minutes to respond — or it auto-skips.</i>"
                ),
                reply_markup=kb,
                parse_mode="HTML"
            )
        except Exception:
            vouch_responses[role] = "skipped"

    buyer_id       = deal.get("buyer_id")
    seller_id      = deal.get("seller_id")
    buyer_uname    = deal.get("buyer_username", "N/A")
    seller_uname   = deal.get("seller_username", "N/A")

    await _ask_party(buyer_id,  "buyer",  seller_uname)
    await _ask_party(seller_id, "seller", buyer_uname)

    # Wait up to 2 minutes; vouch callbacks will populate vouch_responses
    # Store pending state so handle_vouch_callback can find it
    _vouch_pending[did] = {
        "deal":        deal,
        "responses":   vouch_responses,
        "buyer_id":    buyer_id,
        "seller_id":   seller_id,
        "group_id":    group_id,
        "ctx":         ctx,
    }

    # After 2 minutes — close group regardless
    await asyncio.sleep(120)
    _vouch_pending.pop(did, None)
    try:
        await ctx.bot.send_message(
            chat_id=group_id,
            text="🗑 <b>Group closing now. Thank you for using P2P Escrow!</b>",
            parse_mode="HTML"
        )
        for p in ("buyer", "seller"):
            pid2 = deal.get(f"{p}_id")
            if pid2:
                try:
                    await ctx.bot.ban_chat_member(chat_id=group_id, user_id=pid2)
                    await ctx.bot.unban_chat_member(chat_id=group_id, user_id=pid2)
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
    except Exception as ex:
        logger.warning(f"Vouch timeout group close error {group_id}: {ex}")


# Pending vouch state:  did -> {...}
_vouch_pending: dict[str, dict] = {}
# Waiting for vouch text:  user_id -> {"did": str, "role": str}
_vouch_text_waiting: dict[int, dict] = {}


async def handle_vouch_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE, d: str):
    """Handle vouch:start/skip/submit:DID:role callbacks."""
    q    = update.callback_query
    user = q.from_user
    parts = d.split(":")
    # vouch:action:did:role
    if len(parts) < 4:
        await q.answer("❌ Invalid vouch data.", show_alert=True)
        return
    _, action, did, role = parts[0], parts[1], parts[2], parts[3]

    pending = _vouch_pending.get(did)

    if action == "skip":
        if pending:
            pending["responses"][role] = "skipped"
        await q.edit_message_text(
            "⏭ <b>Vouch skipped.</b>\n\nThank you for using P2P Escrow!",
            parse_mode="HTML"
        )
        return

    if action == "start":
        # Ask them to type their vouch
        _vouch_text_waiting[user.id] = {"did": did, "role": role}
        await q.edit_message_text(
            "⭐ <b>Write Your Vouch</b>\n\n"
            "Send your review as a message now.\n"
            "Example: <i>Smooth deal, fast payment, trusted seller! ⭐⭐⭐⭐⭐</i>\n\n"
            "<i>Your message will be forwarded to the vouch channel.</i>",
            parse_mode="HTML"
        )
        return

    await q.answer()


async def process_vouch_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Called from admin_input_handler when user is in vouch text waiting state."""
    user = update.effective_user
    text = update.message.text.strip()
    info = _vouch_text_waiting.pop(user.id, None)
    if not info:
        return False

    did  = info["did"]
    role = info["role"]
    pending = _vouch_pending.get(did)
    deal = pending["deal"] if pending else None
    if not deal:
        deal = state.deals.get(did)

    if not deal:
        await update.message.reply_text("❌ Deal not found. Vouch could not be saved.")
        return True

    token_lbl  = TOKEN_LABELS.get(deal.get("token", ""), deal.get("token", ""))
    sym        = TOKEN_SYMBOL.get(deal.get("token", ""), deal.get("token", ""))
    qty        = float(deal.get("quantity", 0))
    buyer_u    = deal.get("buyer_username", "N/A")
    seller_u   = deal.get("seller_username", "N/A")
    from_u     = user.username or user.first_name

    # Forward to vouch group
    if getattr(state, "vouch_group_id", None):
        try:
            from datetime import timezone, timedelta
            IST = timezone(timedelta(hours=5, minutes=30))
            now_ist = datetime.now(IST).strftime("%d %b %Y, %I:%M %p IST")
            vouch_msg = (
                f"⭐ <b>NEW VOUCH</b>\n\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"🆔 Deal: <code>{did}</code>\n"
                f"🪙 Token: {token_lbl} | 💰 {qty} {sym}\n"
                f"🛒 Buyer: @{buyer_u}  |  🏪 Seller: @{seller_u}\n"
                f"👤 Review by: @{from_u} ({role.capitalize()})\n"
                f"━━━━━━━━━━━━━━━━━━━━\n\n"
                f"💬 <b>Review:</b>\n{text}\n\n"
                f"⏰ {now_ist}"
            )
            await ctx.bot.send_message(
                chat_id=state.vouch_group_id,
                text=vouch_msg,
                parse_mode="HTML"
            )
        except Exception as e:
            logger.warning(f"Could not forward vouch to vouch group: {e}")

    # Confirm to user
    await update.message.reply_text(
        "✅ <b>Vouch submitted! Thank you.</b>\n\nYour review has been posted to the vouch channel. 🙏",
        parse_mode="HTML"
    )

    # Mark this role as vouched
    if pending:
        pending["responses"][role] = "vouched"

    return True


# ══════════════════════════════════════════════════════════
# MEMBER JOIN HANDLER — welcome message with instructions
# ══════════════════════════════════════════════════════════

async def handle_new_member(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Send welcome + instructions when buyer/seller joins the deal group."""
    chat    = update.effective_chat
    members = update.message.new_chat_members
    if not members:
        return

    did, deal = deal_by_group(chat.id)
    if not deal:
        return  # Not a deal group

    fee_pct  = state.fee_percent
    bio_tag  = state.required_bio or "not set"
    bio_disc = getattr(state, "bio_discount_percent", 0.0)

    for member in members:
        if member.is_bot:
            continue
        uname = member.username or member.first_name
        welcome = (
            f"👋 <b>Welcome @{uname}!</b>\n\n"
            f"🔐 This is your <b>P2P Escrow Deal Group</b>.\n\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"📋 <b>QUICK STEPS:</b>\n\n"
            f"<b>1.</b> Fill deal form → <b>/dd</b>\n"
            f"<b>2.</b> Set role + wallet:\n"
            f"   • <code>/buyer YOUR_WALLET</code>\n"
            f"   • <code>/seller YOUR_WALLET</code>\n"
            f"   ↳ Or just <code>/buyer</code> then send address\n"
            f"<b>3.</b> Select token → <b>/token</b>\n"
            f"<b>4.</b> Seller deposits → <b>/deposit</b>\n"
            f"<b>5.</b> Verify payment → <b>/verify</b>\n"
            f"<b>6.</b> Buyer pays off-platform\n"
            f"<b>7.</b> Release funds → <b>/release</b> or <b>/release all</b>\n\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"💸 Fee: <b>{fee_pct}%</b>"
        )
        if state.required_bio and bio_tag != "not set":
            welcome += (
                f"\n🏷 <b>Bio Discount:</b> Add <code>{bio_tag}</code> to your Telegram bio → pay only <b>{bio_disc}%</b> fee!"
            )
        welcome += (
            f"\n\n📊 <b>/balance</b> — check deposit\n"
            f"❓ <b>/dispute</b> — call admin if issue\n"
            f"✏️ <b>/editaddress</b> — change wallet (before deposit)\n\n"
            f"<i>All commands must be used in this group.</i>"
        )
        try:
            await ctx.bot.send_message(chat_id=chat.id, text=welcome, parse_mode="HTML")
        except Exception:
            pass

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
    app.add_handler(CommandHandler("editaddress",  cmd_editaddress))
    app.add_handler(CommandHandler("token",        cmd_token))
    app.add_handler(CommandHandler("deposit",      cmd_deposit))
    app.add_handler(CommandHandler("verify",       cmd_verify))
    app.add_handler(CommandHandler("release",      cmd_release_partial))
    app.add_handler(CallbackQueryHandler(partial_confirm_callback, pattern=r"^partial_confirm:"))
    app.add_handler(CommandHandler("dispute",      cmd_dispute))
    app.add_handler(CommandHandler("balance",      cmd_balance))
    app.add_handler(CommandHandler("dealinfo",     cmd_dealinfo))

    # Admin-only commands
    app.add_handler(CommandHandler("adminpanel",         cmd_adminpanel))
    app.add_handler(CommandHandler("adminrelease",       cmd_adminrelease))
    app.add_handler(CommandHandler("admindeposit",       cmd_admindeposit))
    app.add_handler(CommandHandler("adminforcedeposit",  cmd_adminforcedeposit))
    app.add_handler(CommandHandler("refund",             cmd_refund))
    app.add_handler(CommandHandler("disputeend",         cmd_disputeend))
    app.add_handler(CommandHandler("canceldeal",         cmd_canceldeal))
    app.add_handler(CommandHandler("setloggroup",        cmd_setloggroup))
    app.add_handler(CommandHandler("setdisputegroup",    cmd_setdisputegroup))
    app.add_handler(CommandHandler("setvouchgroup",      cmd_setvouchgroup))
    app.add_handler(CommandHandler("addadmin",           cmd_addadmin))
    app.add_handler(CommandHandler("removeadmin",        cmd_removeadmin))
    app.add_handler(CommandHandler("setfee",             cmd_setfee))
    app.add_handler(CommandHandler("setbio",             cmd_setbio))
    app.add_handler(CommandHandler("setbiodiscount",     cmd_setbiodiscount))
    app.add_handler(CommandHandler("setoxapay",          cmd_setoxapay))
    app.add_handler(CommandHandler("checkoxapay",        cmd_checkoxapay))
    app.add_handler(CommandHandler("resetoxapay",        cmd_resetoxapay))
    app.add_handler(CommandHandler("status",             cmd_status))
    app.add_handler(CommandHandler("listadmins",         cmd_listadmins))

    # New member join handler — welcome message
    app.add_handler(MessageHandler(
        filters.StatusUpdate.NEW_CHAT_MEMBERS,
        handle_new_member
    ))

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
