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
from datetime import datetime, timedelta
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

# ── Group Escrow forward declarations ─────────────────────
GE_TRADE_PREFIX = "GE-"

# ── Premium Telegram Animated Emojis ──────────────────────
def pe(emoji_id: str, fallback: str) -> str:
    return f'<tg-emoji emoji-id="{emoji_id}">{fallback}</tg-emoji>'

PE_BUTTERFLY  = pe("6001449118000487326",  "🦋")   # Flame Butterfly
PE_SPARK      = pe("6023660820544623088",  "✨")   # Multi Sparkles
PE_LIGHTNING  = pe("6026367225466720832",  "⚡")   # Yellow Lightning
PE_NEON_SKULL = pe("6037182932370590949",  "💀")   # Neon Skull
PE_SHIELD     = pe("5794092207223307346",  "🏆")   # Rank Badge / Shield
PE_TECH       = pe("6086639764251873025",  "🔵")   # Tech Spiral Loader
PE_PARTY      = pe("6023660820544623088",  "🎉")   # Party Spark Lines
PE_RED_DEVIL  = pe("5352545104493031170",  "😈")   # Red Devil
PE_BLACKHEART = pe("5352918496642604333",  "🖤")   # Black Heart Glow
PE_PURPLEFIRE = pe("5999340396432333728",  "🔥")   # Purple Flame Heart
PE_BUTTERFLY2 = pe("5999337402840127790",  "🦋")   # Blue Butterfly Glow
PE_BUTTERFLY3 = pe("5999175482573068600",  "🦋")   # Light Butterfly
PE_WHITEHEART = pe("5084613633418199991",  "🤍")   # White Butterfly
PE_NEONWOLF   = pe("6127636064610818291",  "🐺")   # Red Neon Wolf
PE_NEONBOW    = pe("6066395745139824604",  "🎀")   # Neon Pink Bow
PE_DOTS       = pe("5971944878815317190",  "🌀")   # Floating Color Dots
PE_RINGS      = pe("5971837723676249096",  "🔵")   # Neon Circle Rings
PE_TRIPLER    = pe("5974235702701853774",  "🟡")   # Triple Ring Loader
PE_GOLDMAZE   = pe("4949560993840629085",  "🌟")   # Golden Maze
PE_CONFETTI   = pe("6282977077427702833",  "🎊")   # Color Confetti Sparkle
PE_ARCORE     = pe("6001440193058444284",  "⚙️")   # Arc Reactor

# Shortcuts for common use
PE_CHECK   = PE_SPARK        # ✨ for success
PE_LOCK    = PE_SHIELD       # 🏆 for lock
PE_FIRE    = PE_PURPLEFIRE   # 🔥 for fire
PE_STAR    = PE_GOLDMAZE     # 🌟 for star
PE_CROWN   = PE_NEONBOW      # 🎀 for crown/special
PE_WARN    = PE_RED_DEVIL    # 😈 for warning
PE_DEAL    = PE_BUTTERFLY    # 🦋 for deal active
PE_REJECT  = PE_NEON_SKULL   # 💀 for reject/cancel
PE_RELEASE = PE_LIGHTNING    # ⚡ for release
PE_AGREE   = PE_CONFETTI     # 🎊 for agreement
PE_WELCOME = PE_BUTTERFLY2   # 🦋 for welcome
import re as _re

# ── waiting state dicts ──────────────────────────────────
# admin panel text input:  user_id -> field_name
_admin_waiting: dict[int, str] = {}
# buyer/seller address collection:  user_id -> {"deal_id": str, "role": str, "chat_id": int}
_address_waiting: dict[int, dict] = {}
# address edit waiting:  user_id -> {"deal_id": str, "role": str, "chat_id": int}
_address_edit_waiting: dict[int, dict] = {}
# dd form collection:  chat_id -> True (waiting for form reply)
_dd_waiting: dict[int, str] = {}   # chat_id -> deal_id
_addr_confirm_waiting: dict[int, dict] = {}  # user_id -> {deal_id, role, address, chat_id}

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

def ist_now():
    from datetime import timezone, timedelta
    IST = timezone(timedelta(hours=5, minutes=30))
    return datetime.now(IST).strftime("%d %b %Y, %I:%M %p IST")

# deal_id -> log message_id (for edit-based log)
_deal_log_msg: dict[str, int] = {}

async def log(ctx, msg, deal: dict = None):
    """Send or edit deal log message. If deal provided, edits same message each time."""
    if not state.log_group_id:
        return
    did = deal.get("trade_id") if deal else None
    try:
        if did and did in _deal_log_msg:
            try:
                await ctx.bot.edit_message_text(
                    chat_id=state.log_group_id,
                    message_id=_deal_log_msg[did],
                    text=f"📋 LOG\n\n{msg}",
                    parse_mode="HTML"
                )
                return
            except Exception:
                pass  # fallthrough to send new
        sent = await ctx.bot.send_message(
            chat_id=state.log_group_id,
            text=f"📋 LOG\n\n{msg}",
            parse_mode="HTML"
        )
        if did:
            _deal_log_msg[did] = sent.message_id
    except Exception as e:
        logger.error(f"Log error: {e}")

async def log_deal(ctx, deal: dict, action_note: str = ""):
    """One log message per deal — create on first call, edit on subsequent calls."""
    if not state.log_group_id:
        return
    did    = deal.get("trade_id", "?")
    dtype  = "🛒 PRODUCT" if deal.get("deal_type") == "product" else "🔄 P2P"
    tok    = deal.get("token", "—")
    sym    = TOKEN_SYMBOL.get(tok, tok) if tok else "—"
    qty    = deal.get("quantity", "—")
    rel    = float(deal.get("partial_released", 0))
    try:
        remaining = round(float(qty) - rel, 8) if qty and qty != "—" else "—"
    except Exception:
        remaining = "—"
    lines = [
        f"📋 <b>DEAL LOG</b> [{dtype}]",
        f"🆔 <code>{did}</code>  📊 <b>{deal.get('status','—')}</b>",
        f"🪙 {tok}  💰 {qty} {sym}  📈 {deal.get('rate','—')}",
        f"📝 {deal.get('condition','—')}",
        f"",
        f"🛒 @{deal.get('buyer_username','—')}  <code>{deal.get('buyer_address','—')}</code>",
        f"🏪 @{deal.get('seller_username','—')}  <code>{deal.get('seller_address','—')}</code>",
        f"",
        f"📬 <code>{deal.get('deposit_address','—')}</code>",
        f"📤 Released: {rel} {sym}  🔒 Remaining: {remaining} {sym}",
    ]
    if action_note:
        lines += [f"", f"📌 <b>{action_note}</b>", f"⏰ {ist_now()}"]
    text = "\n".join(lines)
    log_msg_id = deal.get("_log_msg_id")
    try:
        if log_msg_id:
            await ctx.bot.edit_message_text(
                chat_id=state.log_group_id, message_id=log_msg_id,
                text=text, parse_mode="HTML")
        else:
            msg = await ctx.bot.send_message(
                chat_id=state.log_group_id, text=text, parse_mode="HTML")
            deal["_log_msg_id"] = msg.message_id
    except Exception as e:
        logger.warning(f"log_deal error for {did}: {e}")

async def idle_delete_loop(ctx, deal: dict, hours: int = 48):
    """Auto-delete group if deal stays in early stages for X hours."""
    await asyncio.sleep(hours * 3600)
    if deal.get("status") not in ("SETUP", "FORM_FILLED", "ROLES_SET", "TOKEN_SELECTED"):
        return
    group_id = deal.get("group_id")
    deal["status"] = "CANCELLED"
    await log_deal(ctx, deal, f"Auto-cancelled: {hours}hr idle")
    try:
        await ctx.bot.send_message(chat_id=group_id,
            text=f"⌛ <b>No activity for {hours} hours. Group closing automatically.</b>",
            parse_mode="HTML")
        await asyncio.sleep(30)
        await ctx.bot.leave_chat(group_id)
        if state.telethon_client:
            from telethon.tl.functions.channels import DeleteChannelRequest
            try:
                entity = await state.telethon_client.get_entity(group_id)
                await state.telethon_client(DeleteChannelRequest(entity))
            except Exception:
                pass
    except Exception as ex:
        logger.warning(f"Idle close error {group_id}: {ex}")

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

def new_deal(tid, group_id, creator_id, deal_type="p2p"):
    return {
        "trade_id": tid, "group_id": group_id, "status": "SETUP",
        "creator_id": creator_id,
        "buyer_id": None, "buyer_username": None, "buyer_address": None,
        "seller_id": None, "seller_username": None, "seller_address": None,
        "quantity": None, "rate": None, "condition": None, "token": None,
        "token_buyer_confirmed": False, "token_seller_confirmed": False,
        "deposit_address": None, "oxapay_track_id": None,
        "buyer_confirmed": False, "seller_confirmed": False,
        "funded": False, "partial_released": 0.0,
        "created_at": datetime.utcnow().isoformat(),
        "deal_type": deal_type, "_log_msg_id": None,
    }

# ══════════════════════════════════════════════════════════
# /start
# ══════════════════════════════════════════════════════════

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """User-friendly start message with profile info and buttons"""
    if update.effective_chat.type != "private":
        return
    
    user = update.effective_user
    uname = (user.username or "").lower()
    
    # Get user stats
    stats = _user_stats.get(uname, {"total_volume": 0, "deals": 0, "highest_deal": 0, "rank": "Unranked"})
    
    # Welcome message with profile
    welcome_text = (
        f"<b>Welcome to Premium ESCROW Bot</b>\n\n"
        f"Hello, {user.first_name}!\n\n"
        f"<b>Your Profile</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"Username: @{user.username or 'Not Set'}\n"
        f"Global Rank: {stats.get('rank', 'Unranked')}\n"
        f"Deals: {stats.get('deals', 0)}\n"
        f"Volume: ₹{stats.get('total_volume', 0):,.2f}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n\n"
        f"<b>Secure • Fast • Trusted</b>\n\n"
        f"Use the buttons below to navigate:"
    )
    
    # User buttons
    kb_rows = [
        [InlineKeyboardButton("⚙️ ── START A DEAL ──", callback_data="user:noop")],
        [
            InlineKeyboardButton("🔄 P2P Escrow", callback_data="deal_type:p2p"),
            InlineKeyboardButton("🛒 Product Escrow", callback_data="deal_type:product"),
        ],
        [InlineKeyboardButton("⚙️ ── MY ACCOUNT ──", callback_data="user:noop")],
        [
            InlineKeyboardButton("📊 My Stats", callback_data="user:stats"),
            InlineKeyboardButton("📝 Pending Deals", callback_data="user:pending"),
        ],
        [
            InlineKeyboardButton("📜 History", callback_data="user:history"),
            InlineKeyboardButton("🌍 Global Ranks", callback_data="user:ranks"),
        ],
        [InlineKeyboardButton("📖 Help & Guide", callback_data="user:help")],
    ]

    # Admin button if admin
    if is_admin(user.id):
        kb_rows.append([
            InlineKeyboardButton("👑 Admin Panel", callback_data="adm:status")
        ])

    kb = InlineKeyboardMarkup(kb_rows)

    await update.message.reply_text(welcome_text, parse_mode="HTML", reply_markup=kb)


# ══════════════════════════════════════════════════════════
# /instructions
# ══════════════════════════════════════════════════════════

async def cmd_instructions(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    fee_pct  = get_deal_fee("p2p", False)  # show default p2p fee
    bio_tag  = state.required_bio or "not set"
    bio_disc = get_deal_fee("p2p", True)  # bio rate

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

async def _show_fee_panel(q):
    """Show interactive fee configuration panel with edit buttons."""
    fc = getattr(state, "fee_config", {})
    p2p_b   = fc.get("p2p_bio",        1.0)
    p2p_n   = fc.get("p2p_normal",     2.0)
    prod_b  = fc.get("product_bio",    1.5)
    prod_n  = fc.get("product_normal", 3.0)
    ge_b    = fc.get("ge_bio",         1.0)
    ge_n    = fc.get("ge_normal",      2.0)

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("━━ 🔄 P2P ESCROW ━━", callback_data="adm:noop")],
        [
            InlineKeyboardButton(f"🏷 Bio: {p2p_b}%",    callback_data="adm:editfee:p2p_bio"),
            InlineKeyboardButton(f"👤 Normal: {p2p_n}%", callback_data="adm:editfee:p2p_normal"),
        ],
        [InlineKeyboardButton("━━ 🛒 PRODUCT ESCROW ━━", callback_data="adm:noop")],
        [
            InlineKeyboardButton(f"🏷 Bio: {prod_b}%",    callback_data="adm:editfee:product_bio"),
            InlineKeyboardButton(f"👤 Normal: {prod_n}%", callback_data="adm:editfee:product_normal"),
        ],
        [InlineKeyboardButton("━━ 🏪 GROUP ESCROW ━━", callback_data="adm:noop")],
        [
            InlineKeyboardButton(f"🏷 Bio: {ge_b}%",    callback_data="adm:editfee:ge_bio"),
            InlineKeyboardButton(f"👤 Normal: {ge_n}%", callback_data="adm:editfee:ge_normal"),
        ],
        [InlineKeyboardButton("⬅️ Back to Panel", callback_data="adm:status")],
    ])
    await q.edit_message_text(
        f"💸 <b>Fee Configuration</b>\n\n"
        f"🏷 = With bio tag discount\n"
        f"👤 = Without bio tag (normal)\n\n"
        f"<b>Tap any button to edit that fee.</b>",
        parse_mode="HTML",
        reply_markup=kb
    )


def admin_panel_kb():
    tc = "✅ ON"  if state.telethon_client else "❌ OFF"
    ox = "✅ SET" if state.oxapay_key      else "❌ NOT SET"
    lg = "✅ SET" if state.log_group_id    else "❌ NOT SET"
    dg = "✅ SET" if state.dispute_group_id else "❌ NOT SET"
    vg = "✅ SET" if getattr(state, "vouch_group_id",  None) else "❌ NOT SET"
    ve = "✅ ON"  if getattr(state, "vouch_enabled", True)   else "❌ OFF"
    eg = "✅ SET" if getattr(state, "escrow_group_id", None) else "❌ NOT SET"
    ch = "✅ SET" if getattr(state, "channel_link",    None) else "❌ NOT SET"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⚙️ ── GROUP SETUP ──", callback_data="adm:noop")],
        [InlineKeyboardButton(f"📋 Log Group {lg}",       callback_data="adm:setloggroup"),
         InlineKeyboardButton(f"🚨 Dispute Group {dg}",   callback_data="adm:setdisputegroup")],
        [InlineKeyboardButton(f"⭐ Vouch Group {vg}",     callback_data="adm:setvouchgroup"),
         InlineKeyboardButton(f"🏷 Escrow Group {eg}",    callback_data="adm:setescrowgroup")],
        [InlineKeyboardButton(f"📢 Channel {ch}",         callback_data="adm:setchannel"),
         InlineKeyboardButton(f"⭐ Vouch {ve}",           callback_data="adm:togglevouch")],
        [InlineKeyboardButton("⚙️ ── BOT SETTINGS ──", callback_data="adm:noop")],
        [InlineKeyboardButton("📊 Fee Types",             callback_data="adm:setfeetypes")],
        [InlineKeyboardButton("💳 UPI Methods",           callback_data="adm:listupi"),
         InlineKeyboardButton("⏱ Set Timeout",            callback_data="adm:settimeout")],
        [InlineKeyboardButton("🏷 Bio Tag",               callback_data="adm:setbio"),
         InlineKeyboardButton("🎟 Bio Discount %",        callback_data="adm:setbiodiscount")],
        [InlineKeyboardButton(f"🔑 OxaPay {ox}",          callback_data="adm:setoxapay"),
         InlineKeyboardButton("✅ Check OxaPay",          callback_data="adm:checkoxapay")],
        [InlineKeyboardButton("🗑 Reset OxaPay",          callback_data="adm:resetoxapay"),
         InlineKeyboardButton(f"📡 Telethon {tc}",        callback_data="adm:telethon")],
        [InlineKeyboardButton("⚙️ ── ADMINS ──", callback_data="adm:noop")],
        [InlineKeyboardButton("➕ Add Admin",              callback_data="adm:addadmin"),
         InlineKeyboardButton("➖ Remove Admin",           callback_data="adm:removeadmin")],
        [InlineKeyboardButton("👥 List Admins",           callback_data="adm:listadmins"),
         InlineKeyboardButton("📊 Stats",                 callback_data="adm:gestats")],
        [InlineKeyboardButton("🔄 Refresh",               callback_data="adm:status")],
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
    if   d.startswith("deal_type:"):          await handle_deal_type(update, ctx, d)
    elif d == "start_deal":                  await handle_start_deal(update, ctx)
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
    elif d.startswith("addr_confirm:"):      await handle_addr_confirm_cb(update, ctx, d)
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

    if action == "noop":
        await q.answer()
        return

    elif action == "status":
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
            f"💸 P2P: <b>{get_deal_fee('p2p',False)}%</b> / Bio: <b>{get_deal_fee('p2p',True)}%</b>\n"
            f"🛒 Product: <b>{get_deal_fee('product',False)}%</b> / Bio: <b>{get_deal_fee('product',True)}%</b>\n"
            f"🏪 GE: <b>{get_deal_fee('ge',False)}%</b> / Bio: <b>{get_deal_fee('ge',True)}%</b>\n"
            f"🏷 Bio Tag: <b>{state.required_bio or 'Not Set'}</b>\n"
            f"👥 Sub Admins: <b>{len(state.sub_admins)}</b>\n\n"
            f"📦 Total: {total}  🟢 Active: {total-done}  ✅ Done: {done}\n"
            f"💰 Funded: {fund}  🚨 Disputed: {dis}\n\n"
            f"🤖 Mode: {'LIVE' if state.oxapay_key else 'DEMO'}",
            parse_mode="HTML", reply_markup=admin_panel_kb()
        )

    elif action == "listadmins":
        txt = f"👑 Main: <code>{MAIN_ADMIN_ID}</code>\n\n"
        if state.sub_admins:
            lines = []
            for i, a in enumerate(state.sub_admins, 1):
                lim = _admin_hold_limits.get(a)
                lim_str = f" | Limit: ₹{lim:,.0f}" if lim else " | No limit"
                lines.append(f"{i}. <code>{a}</code>{lim_str}")
            txt += "👨‍💼 Sub Admins:\n" + "\n".join(lines)
        else:
            txt += "👨‍💼 Sub Admins: None"
        kb_back = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="adm:status")]])
        await q.edit_message_text(f"📋 <b>ADMIN LIST</b>\n\n{txt}", parse_mode="HTML", reply_markup=kb_back)

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
        _admin_waiting[q.from_user.id] = "log_group"
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("❌ Cancel", callback_data="adm:cancel_input"),
            InlineKeyboardButton("⬅️ Back",   callback_data="adm:status")
        ]])
        cur = f"✅ Current: <code>{state.log_group_id}</code>" if state.log_group_id else "❌ Not set"
        await q.edit_message_text(
            f"📋 <b>Set Log Group</b>\n\n{cur}\n\n"
            f"Group ID ya invite link bhejo:\n"
            f"• Group ID: <code>-100xxxxxxxxxx</code>\n"
            f"• Ya bot ko group mein add karo aur group ID bhejo\n\n"
            f"📌 Bot ko us group ka <b>admin</b> banana zaroori hai.",
            parse_mode="HTML", reply_markup=kb
        )

    elif action == "setdisputegroup":
        _admin_waiting[q.from_user.id] = "dispute_group"
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("❌ Cancel", callback_data="adm:cancel_input"),
            InlineKeyboardButton("⬅️ Back",   callback_data="adm:status")
        ]])
        cur = f"✅ Current: <code>{state.dispute_group_id}</code>" if state.dispute_group_id else "❌ Not set"
        await q.edit_message_text(
            f"🚨 <b>Set Dispute Group</b>\n\n{cur}\n\n"
            f"Group ID bhejo:\n"
            f"<code>-100xxxxxxxxxx</code>\n\n"
            f"📌 Bot ko us group ka <b>admin</b> banana zaroori hai.",
            parse_mode="HTML", reply_markup=kb
        )

    elif action == "setvouchgroup":
        _admin_waiting[q.from_user.id] = "vouch_group"
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("❌ Cancel", callback_data="adm:cancel_input"),
            InlineKeyboardButton("⬅️ Back",   callback_data="adm:status")
        ]])
        cur = f"✅ Current: <code>{state.vouch_group_id}</code>" if getattr(state,"vouch_group_id",None) else "❌ Not set"
        await q.edit_message_text(
            f"⭐ <b>Set Vouch Group</b>\n\n{cur}\n\n"
            f"Group ID bhejo:\n"
            f"<code>-100xxxxxxxxxx</code>\n\n"
            f"📌 Deal ke baad vouch yahan forward hoga.",
            parse_mode="HTML", reply_markup=kb
        )

    elif action == "setescrowgroup":
        _admin_waiting[q.from_user.id] = "escrow_group"
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("❌ Cancel", callback_data="adm:cancel_input"),
            InlineKeyboardButton("⬅️ Back",   callback_data="adm:status")
        ]])
        cur = f"✅ Current: <code>{state.escrow_group_id}</code>" if getattr(state,"escrow_group_id",None) else "❌ Not set"
        await q.edit_message_text(
            f"🏷 <b>Set Escrow Group</b>\n\n{cur}\n\n"
            f"Escrow group ka ID bhejo:\n"
            f"<code>-100xxxxxxxxxx</code>\n\n"
            f"📌 Sirf is group mein bot forms accept karega.\n"
            f"Bot ko us group ka <b>admin</b> banana zaroori hai.",
            parse_mode="HTML", reply_markup=kb
        )

    elif action == "setchannel":
        _admin_waiting[q.from_user.id] = "channel_setup"
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("❌ Cancel", callback_data="adm:cancel_input"),
            InlineKeyboardButton("⬅️ Back",   callback_data="adm:status")
        ]])
        cur = f"✅ Current: {state.channel_name} — {state.channel_link}" if getattr(state,"channel_link",None) else "❌ Not set"
        await q.edit_message_text(
            f"📢 <b>Set Channel Button</b>\n\n{cur}\n\n"
            f"Format mein bhejo:\n"
            f"<code>LINK | Channel Name</code>\n\n"
            f"Example:\n"
            f"<code>https://t.me/babaescrow | Baba Escrow Official</code>\n\n"
            f"📌 Ye button /start message pe dikhega.",
            parse_mode="HTML", reply_markup=kb
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

    elif action == "escrowers":
        lines = ["👨‍⚖️ <b>ESCROWERS</b>\n", f"👑 Main: <code>{MAIN_ADMIN_ID}</code>"]
        for uid in state.sub_admins:
            active_c = len([t for t in _ge_admin_holds.get(uid,[])
                            if t in _ge_deals and _ge_deals[t].get("status") not in ("CLOSED","CANCELLED")])
            try:
                co = await ctx.bot.get_chat(uid)
                un = f"@{co.username}" if co.username else str(uid)
            except Exception:
                un = str(uid)
            lines.append(f"👨‍⚖️ {un} — {active_c} active")
        await q.edit_message_text("\n".join(lines), parse_mode="HTML", reply_markup=admin_panel_kb())

    elif action == "gestats":
        total   = len(_ge_deals)
        active  = sum(1 for d in _ge_deals.values() if d.get("status") not in ("CLOSED","CANCELLED"))
        closed  = sum(1 for d in _ge_deals.values() if d.get("status") == "CLOSED")
        total_vol = sum(
            float(_re.sub(r"[^\d.]","", str(d.get("released_amount") or d.get("amount","0"))))
            for d in _ge_deals.values() if d.get("status") == "CLOSED"
            if _re.sub(r"[^\d.]","", str(d.get("released_amount") or d.get("amount","0")))
        )
        await q.edit_message_text(
            f"📊 <b>GE STATS</b>\n\n"
            f"📦 Total: {total}  🟢 Active: {active}  ✅ Closed: {closed}\n"
            f"💰 Total Vol Cleared: ₹{total_vol:.2f}",
            parse_mode="HTML", reply_markup=admin_panel_kb())

    elif action == "listupi":
        methods = getattr(state, "upi_methods", {})
        if not methods:
            txt = "💳 <b>UPI Methods</b>\n\nNo methods saved.\nUse <code>/saveupi NAME UPIID</code>"
        else:
            lines = ["💳 <b>Saved UPI Methods:</b>\n"]
            for name, data in methods.items():
                lines.append(f"• <code>{name}</code> → <code>{data['upi_id']}</code>")
            txt = "\n".join(lines)
        await q.edit_message_text(txt, parse_mode="HTML", reply_markup=admin_panel_kb())

    elif action == "setfeetypes":
        await _show_fee_panel(q)

    elif action.startswith("editfee:"):
        # adm:editfee:p2p_bio  etc.
        fee_key = action.split(":", 1)[1]
        _admin_waiting[q.from_user.id] = f"fee_type:{fee_key}"
        fc = getattr(state, "fee_config", {})
        cur_val = fc.get(fee_key, 0.0)
        labels = {
            "p2p_bio":        "🔄 P2P — With Bio",
            "p2p_normal":     "🔄 P2P — Without Bio",
            "product_bio":    "🛒 Product — With Bio",
            "product_normal": "🛒 Product — Without Bio",
            "ge_bio":         "🏪 Group Escrow — With Bio",
            "ge_normal":      "🏪 Group Escrow — Without Bio",
        }
        label = labels.get(fee_key, fee_key)
        kb_edit = InlineKeyboardMarkup([[
            InlineKeyboardButton("❌ Cancel", callback_data="adm:setfeetypes"),
            InlineKeyboardButton("⬅️ Back",  callback_data="adm:setfeetypes"),
        ]])
        await q.edit_message_text(
            f"💸 <b>Edit Fee — {label}</b>\n\n"
            f"Current: <b>{cur_val}%</b>\n\n"
            f"Enter new percentage (e.g. <code>1.5</code>):",
            parse_mode="HTML", reply_markup=kb_edit
        )

    elif action == "settimeout":
        _admin_waiting[q.from_user.id] = "timeout"
        kb_to = InlineKeyboardMarkup([[
            InlineKeyboardButton("❌ Cancel", callback_data="adm:cancel_input"),
            InlineKeyboardButton("⬅️ Back", callback_data="adm:status")
        ]])
        cur = getattr(state, "deal_timeout_hours", 24)
        await q.edit_message_text(
            f"⏱ <b>Set Deal Timeout</b>\n\nCurrent: <b>{cur} hours</b>\n\nEnter new timeout in hours (e.g. 24):",
            parse_mode="HTML", reply_markup=kb_to
        )

    elif action in ("addadmin", "removeadmin", "setfee", "setbio", "setbiodiscount", "setoxapay", "settimeout"):
        field_map = {
            "addadmin": "addadmin", "removeadmin": "removeadmin",
            "setfee": "fee", "setbio": "bio", "setbiodiscount": "bio_discount",
            "setoxapay": "oxapay", "settimeout": "timeout"
        }
        field = field_map[action]
        cur_discount = getattr(state, "bio_discount_percent", 0.0)
        labels = {
            "addadmin":    ("➕ <b>Add Sub Admin</b>",    "Enter Telegram User ID"),
            "removeadmin": ("➖ <b>Remove Sub Admin</b>", "Enter Telegram User ID to remove"),
            "fee":         ("💸 <b>Set Fee %</b>",        f"Current default: {state.fee_percent}%\nEnter new value (0-50). Applies to all types.\nFor per-type config use /setfees command."),
            "bio":         ("🏷 <b>Set Bio Tag</b>",      f"Current: {state.required_bio or 'Not set'} — Enter new tag"),
            "bio_discount":("🎟 <b>Set Bio Discount %</b>", f"Current: {cur_discount}% — Fee for bio-matched users (0 = free)"),
            "oxapay":      ("🔑 <b>Set OxaPay Key</b>",  "Enter your OxaPay API key"),
            "timeout":     ("⏱ <b>Set Deal Timeout</b>", f"Current: {getattr(state, 'deal_timeout_hours', 24)}h — Enter hours (e.g. 24)"),
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

# ══════════════════════════════════════════════════════════
# DEAL TYPE SELECTION — P2P or PRODUCT
# ══════════════════════════════════════════════════════════

async def handle_deal_type(update: Update, ctx: ContextTypes.DEFAULT_TYPE, d: str):
    """Handle deal_type:p2p or deal_type:product button."""
    q     = update.callback_query
    user  = q.from_user
    dtype = d.split(":")[1]  # "p2p" or "product"

    if not state.log_group_id:
        await q.edit_message_text("❌ <b>Cannot create deal.</b>\n\nAdmin has not set the LOG GROUP yet.", parse_mode="HTML")
        return

    label = "🔄 P2P ESCROW" if dtype == "p2p" else "🛒 PRODUCT DEAL"
    await q.edit_message_text(f"⏳ <b>Creating {label} group…</b>\nPlease wait.", parse_mode="HTML")

    tid = trade_id()
    group_id, invite_url = None, None

    if state.telethon_client:
        bot_me = await ctx.bot.get_me()
        emoji  = "🔄" if dtype == "p2p" else "🛒"
        group_id, invite_url = await create_group_telethon(f"{emoji} Escrow {tid}", bot_me.username, ctx.bot)

    if not group_id:
        await ctx.bot.send_message(chat_id=user.id,
            text=(
                "⚠️ <b>Auto Group Creation Failed</b>\n\n"
                "Please do manually:\n"
                "1️⃣ Create a Telegram group\n"
                "2️⃣ Add bot as <b>Admin</b>\n"
                f"3️⃣ Run <code>/initdeal {dtype}</code> inside\n\n"
                "<i>Check API_ID, API_HASH and PHONE settings</i>"),
            parse_mode="HTML")
        return

    deal = new_deal(tid, group_id, user.id, deal_type=dtype)
    state.deals[tid] = deal
    state.group_to_deal[group_id] = tid

    deposit_who  = "Seller" if dtype == "p2p" else "Buyer"
    release_to   = "Buyer's wallet" if dtype == "p2p" else "Seller's wallet"
    fee_pct      = get_deal_fee(dtype, False)  # normal fee for this deal type
    bio_fee_pct  = get_deal_fee(dtype, True)   # bio-discounted fee
    bio_tag      = state.required_bio
    bio_disc     = bio_fee_pct
    bio_line     = f"\n🏷 Bio Discount: Add <code>{bio_tag}</code> to bio → <b>{bio_fee_pct}%</b> fee!" if bio_tag else ""

    await ctx.bot.send_message(chat_id=user.id,
        text=(
            f"✅ <b>{label} Group Created!</b>\n\n"
            f"🆔 Trade ID: <code>{tid}</code>\n"
            f"🔗 <b>Invite Link (max 2):</b>\n{invite_url}\n\n"
            f"⚠️ Share with the other party only.\n\n"
            f"💡 <b>How this works:</b>\n"
            f"• <b>{deposit_who}</b> deposits to escrow\n"
            f"• On release → <b>{release_to}</b>\n\n"
            f"➡️ Both join → run <b>/dd</b> to start."),
        parse_mode="HTML")

    # Admin notification
    for admin_id in [MAIN_ADMIN_ID] + list(state.sub_admins):
        try:
            await ctx.bot.send_message(chat_id=admin_id,
                text=(f"🆕 <b>NEW {label}</b>\n\n"
                      f"🆔 <code>{tid}</code>\n"
                      f"👤 @{user.username or user.first_name} ({user.id})\n"
                      f"📦 Group: <code>{group_id}</code>\n"
                      f"⏰ {deal['created_at']}"),
                parse_mode="HTML")
        except Exception:
            pass

    # Group welcome message
    steps_extra = "\n<b>6️⃣</b> Buyer pays seller off-platform (fiat/goods)" if dtype == "p2p" else "\n<b>6️⃣</b> Seller delivers product/service"
    try:
        await ctx.bot.send_message(chat_id=group_id,
            text=(
                f"{'🔄' if dtype=='p2p' else '🛒'} <b>ESCROW DEAL GROUP</b>\n"
                f"<b>{label}</b>\n\n"
                f"🆔 Trade ID: <code>{tid}</code>\n\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"📋 <b>STEPS:</b>\n\n"
                f"<b>1️⃣</b> /dd — fill deal form\n\n"
                f"<b>2️⃣</b> Set role + wallet:\n"
                f"   <code>/buyer YOUR_WALLET</code>   or   <code>/buyer</code>\n"
                f"   <code>/seller YOUR_WALLET</code>  or   <code>/seller</code>\n"
                f"   ↳ Bot confirms your address before locking\n"
                f"   ✏️ Change later: <code>/editaddress NEW_ADDRESS</code>\n\n"
                f"<b>3️⃣</b> /token — both confirm\n\n"
                f"<b>4️⃣</b> /deposit — <b>{deposit_who}</b> deposits to escrow\n\n"
                f"<b>5️⃣</b> /verify — confirm payment received{steps_extra}\n\n"
                f"<b>7️⃣</b> /release — funds go to <b>{'buyer' if dtype=='p2p' else 'seller'}</b>\n\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"💸 Fee: <b>{fee_pct}%</b>{bio_line}\n"
                f"📊 /balance  🚨 /dispute  ✏️ /editaddress\n"
                f"⌛ 48hrs no activity → group auto-deleted"),
            parse_mode="HTML")
    except Exception as e:
        logger.warning(f"Welcome msg error: {e}")

    await log_deal(ctx, deal, "Deal created")
    asyncio.create_task(idle_delete_loop(ctx, deal, hours=48))


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

    deal = new_deal(tid, group_id, user.id, deal_type="p2p")
    state.deals[tid] = deal
    state.group_to_deal[group_id] = tid
    asyncio.create_task(idle_delete_loop(ctx, deal, hours=48))

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

    await log_deal(ctx, deal, f"Deal created — {invite_url}")

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

    dtype = (ctx.args[0].lower() if ctx.args and ctx.args[0].lower() in ("p2p","product") else "p2p")
    tid   = trade_id()
    deal  = new_deal(tid, chat.id, user.id, deal_type=dtype)
    state.deals[tid] = deal
    state.group_to_deal[chat.id] = tid
    label = "🔄 P2P ESCROW" if dtype == "p2p" else "🛒 PRODUCT DEAL"

    await update.message.reply_text(
        f"🔒 <b>Escrow Deal Initialized</b>\n\n"
        f"🆔 Trade ID: <code>{tid}</code>\n"
        f"Type: <b>{label}</b>\n\n"
        f"➡️ Run <b>/dd</b> to fill the deal form.",
        parse_mode="HTML"
    )
    await log_deal(ctx, deal, "Deal created (manual)")
    asyncio.create_task(idle_delete_loop(ctx, deal, hours=48))

# ══════════════════════════════════════════════════════════
# STEP 3: /dd — sends blank copyable form
# ══════════════════════════════════════════════════════════

BLANK_FORM = (
    "QUANTITY-\n"
    "RATE-\n"
    "CONDITION-"
)

PRODUCT_FORM = (
    "DEALINFO-\n"
    "AMOUNT-\n"
    "CONDITIONS-"
)

async def cmd_dd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    # Route GE trade IDs to cmd_ge_dd
    if ctx.args and ctx.args[0].upper().startswith(GE_TRADE_PREFIX):
        await cmd_ge_dd(update, ctx)
        return
    chat = update.effective_chat
    if chat.type == "private":
        await update.message.reply_text("❌ Use /dd inside your deal group.")
        return
    did, deal = deal_by_group(chat.id)
    if not deal:
        await update.message.reply_text("❌ No active deal. Use /initdeal first.")
        return
    if deal["status"] != "SETUP":
        dtype = deal.get("deal_type", "p2p")
        if dtype == "product":
            await update.message.reply_text(
                f"⚠️ Form already filled. Status: <b>{deal['status']}</b>\n\n"
                f"📦 Deal Info: {deal.get('rate','—')}\n"
                f"💰 Amount: {deal.get('quantity','—')}\n"
                f"📝 Conditions: {deal.get('condition','—')}",
                parse_mode="HTML")
        else:
            await update.message.reply_text(
                f"⚠️ Form already filled. Status: <b>{deal['status']}</b>\n\n"
                f"💰 Quantity: {deal.get('quantity','—')}\n"
                f"📈 Rate: {deal.get('rate','—')}\n"
                f"📝 Condition: {deal.get('condition','—')}",
                parse_mode="HTML")
        return

    _dd_waiting[chat.id] = did
    dtype = deal.get("deal_type", "p2p")

    if dtype == "product":
        await update.message.reply_text(
            "📋 <b>PRODUCT DEAL — Form</b>\n\n"
            "Copy, fill in values, send back:\n\n"
            f"<code>{PRODUCT_FORM}</code>\n\n"
            "Example:\n"
            "<code>DEALINFO-2x Telegram IDs + Groups\n"
            "AMOUNT-50$\n"
            "CONDITIONS-Delivery within 24 hours</code>",
            parse_mode="HTML")
    else:
        await update.message.reply_text(
            "📋 <b>P2P ESCROW — Deal Form</b>\n\n"
            "Copy, fill in values, send back:\n\n"
            f"<code>{BLANK_FORM}</code>\n\n"
            "Example:\n"
            "<code>QUANTITY-500\n"
            "RATE-1.02\n"
            "CONDITION-Payment within 30 minutes</code>\n\n"
            "⚠️ Keep the format exactly as shown.",
            parse_mode="HTML")

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

    # ── Vouch review capture (in group) ──────────────────
    if user.id in _vouch_text_waiting:
        info = _vouch_text_waiting.get(user.id)
        if info and info.get("group_id") == chat.id:
            await process_vouch_text(update, ctx)
            return

    # ── Deal form capture ────────────────────────────────
    if chat.id in _dd_waiting:
        did = _dd_waiting[chat.id]
        deal = deal_by_id(did)
        if deal and deal["status"] == "SETUP":
            lines = {l.split("-", 1)[0].strip().upper(): l.split("-", 1)[1].strip()
                     for l in text.splitlines() if "-" in l}
            dtype = deal.get("deal_type", "p2p")

            if dtype == "product":
                qty  = lines.get("AMOUNT",    "").strip()
                rate = lines.get("DEALINFO",  "").strip()  # reuse rate field for deal info
                cond = lines.get("CONDITIONS","").strip()
                if not qty or not rate:
                    await msg.reply_text(
                        "❌ Invalid format. Please use:\n\n"
                        "<code>DEALINFO-2x TG IDs\nAMOUNT-50$\nCONDITIONS-24hr delivery</code>",
                        parse_mode="HTML")
                    return
                form_display = (
                    f"📦 DEAL INFO — <b>{rate}</b>\n"
                    f"💰 AMOUNT — <b>{qty}</b>\n"
                    f"📝 CONDITIONS — <b>{cond or 'None'}</b>")
            else:
                qty  = lines.get("QUANTITY", "").strip()
                rate = lines.get("RATE",     "").strip()
                cond = lines.get("CONDITION","").strip()
                if not qty or not rate:
                    await msg.reply_text(
                        "❌ Invalid format. Please use:\n\n"
                        "<code>QUANTITY-500\nRATE-1.02\nCONDITION-Pay within 30 mins</code>",
                        parse_mode="HTML")
                    return
                form_display = (
                    f"💰 QUANTITY — <b>{qty}</b>\n"
                    f"📈 RATE — <b>{rate}</b>\n"
                    f"📝 CONDITION — <b>{cond or 'None'}</b>")

            deal["quantity"]  = qty
            deal["rate"]      = rate
            deal["condition"] = cond or "None"
            deal["status"]    = "FORM_FILLED"
            _dd_waiting.pop(chat.id, None)

            deposit_who = "Seller" if dtype == "p2p" else "Buyer"
            await msg.reply_text(
                f"✅ <b>Deal Form Confirmed!</b>\n\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"{form_display}\n"
                f"━━━━━━━━━━━━━━━━━━━━\n\n"
                f"🔒 Form locked.\n\n"
                f"➡️ <b>Next step:</b>\n"
                f"• Buyer → <code>/buyer YOUR_WALLET</code>\n"
                f"• Seller → <code>/seller YOUR_WALLET</code>\n\n"
                f"<i>Bot will ask you to confirm your address.</i>",
                parse_mode="HTML")
            await log_deal(ctx, deal, "Form filled")
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

        address = text.strip()
        other_role = "seller" if role == "buyer" else "buyer"
        other_addr = deal.get(f"{other_role}_address")
        if other_addr and other_addr.strip().lower() == address.lower():
            await msg.reply_text(
                f"❌ <b>Same address as {other_role}!</b> Use a different wallet.",
                parse_mode="HTML")
            return

        # Ask for confirmation before locking
        _address_waiting.pop(user.id, None)
        _addr_confirm_waiting[user.id] = {
            "deal_id": did, "role": role, "address": address, "chat_id": chat.id}
        label = "🛒 Buyer" if role == "buyer" else "🏪 Seller"
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Yes, Confirm",  callback_data=f"addr_confirm:yes:{did}:{role}"),
             InlineKeyboardButton("✏️ No, Change",    callback_data=f"addr_confirm:no:{did}:{role}")]
        ])
        await msg.reply_text(
            f"{'🛒' if role=='buyer' else '🏪'} <b>{label} — Confirm Address?</b>\n\n"
            f"💳 <code>{address}</code>\n\n"
            f"Is this correct?",
            reply_markup=kb, parse_mode="HTML")

# ══════════════════════════════════════════════════════════
# ── Address confirmation callback ──────────────────────────
async def handle_addr_confirm_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE, d: str):
    """Handle addr_confirm:yes/no:DID:ROLE — confirms or rejects address before locking."""
    q    = update.callback_query
    user = q.from_user
    parts = d.split(":")
    if len(parts) < 4:
        await q.answer("❌ Invalid.", show_alert=True)
        return
    _, action, did, role = parts[0], parts[1], parts[2], parts[3]

    info = _addr_confirm_waiting.get(user.id)
    if not info or info.get("deal_id") != did:
        await q.answer("⏱ Expired. Use /buyer or /seller again.", show_alert=True)
        return

    deal = deal_by_id(did)
    if not deal:
        await q.answer("❌ Deal not found.", show_alert=True)
        return

    if action == "no":
        _addr_confirm_waiting.pop(user.id, None)
        _address_waiting[user.id] = {"deal_id": did, "role": role, "chat_id": info["chat_id"]}
        label = "🛒 Buyer" if role == "buyer" else "🏪 Seller"
        await q.edit_message_text(
            f"✏️ Send your correct <b>{label}</b> wallet address now.",
            parse_mode="HTML")
        return

    # Confirmed — lock the address
    address = info["address"]
    _addr_confirm_waiting.pop(user.id, None)

    deal[f"{role}_id"]       = user.id
    deal[f"{role}_username"] = user.username or user.first_name
    deal[f"{role}_address"]  = address
    deal[f"{role}_locked"]   = True

    b = deal.get("buyer_id") is not None
    s = deal.get("seller_id") is not None
    if b and s:
        deal["status"] = "ROLES_SET"
        next_step = "🔒 Both roles locked!\n\n➡️ Use <b>/token</b> to select token"
    elif b:
        next_step = "⏳ Waiting for Seller → <code>/seller YOUR_WALLET</code>"
    else:
        next_step = "⏳ Waiting for Buyer → <code>/buyer YOUR_WALLET</code>"

    label = "🛒 Buyer" if role == "buyer" else "🏪 Seller"
    bio_hint = ""
    if state.required_bio:
        bio_disc_val = getattr(state, "bio_discount_percent", 0.0)
        bio_hint = (f"\n\n🏷 Add <code>{state.required_bio}</code> to bio → "
                    f"<b>{bio_disc_val}%</b> fee instead of {state.fee_percent}%")

    await q.edit_message_text(
        f"{'🛒' if role=='buyer' else '🏪'} <b>{label} Registered!</b>\n\n"
        f"👤 @{deal[f'{role}_username']}\n"
        f"💳 Wallet: <code>{address}</code>\n"
        f"🔒 Role locked{bio_hint}\n"
        f"✏️ Wrong? → <code>/editaddress NEW_ADDRESS</code>\n\n"
        f"{next_step}",
        parse_mode="HTML")
    await log_deal(ctx, deal, f"{role} registered: {address[:25]}...")

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
        # Show confirmation before locking
        _addr_confirm_waiting[user.id] = {
            "deal_id": did, "role": role, "address": inline_address, "chat_id": chat.id}
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Confirm",   callback_data=f"addr_confirm:yes:{did}:{role}"),
             InlineKeyboardButton("✏️ Change",    callback_data=f"addr_confirm:no:{did}:{role}")]
        ])
        await update.message.reply_text(
            f"{'🛒' if role=='buyer' else '🏪'} <b>{label} — Confirm Address?</b>\n\n"
            f"💳 <code>{inline_address}</code>",
            reply_markup=kb, parse_mode="HTML")
        return

    # No inline address — wait for address in group
    _address_waiting[user.id] = {"deal_id": did, "role": role, "chat_id": chat.id}

    await update.message.reply_text(
        f"{'🛒' if role=='buyer' else '🏪'} <b>{label} — @{user.username or user.first_name}</b>\n\n"
        f"📬 Send your <b>wallet address</b> now.\n"
        f"💡 Or: <code>/{role} YOUR_ADDRESS</code>\n\n"
        f"✏️ Change later: <code>/editaddress NEW_ADDRESS</code>",
        parse_mode="HTML")

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
    # p2p: seller deposits | product: buyer deposits
    dtype = deal.get("deal_type", "p2p")
    deposit_uid  = deal.get("seller_id") if dtype == "p2p" else deal.get("buyer_id")
    deposit_role = "seller" if dtype == "p2p" else "buyer"
    if user.id != deposit_uid:
        await update.message.reply_text(
            f"❌ Only the <b>{deposit_role}</b> can initiate the deposit.", parse_mode="HTML")
        return

    # ── Bio discount preview ──
    dep_discount_applied = False
    dep_discount_reason  = ""
    dep_effective_fee    = get_deal_fee(dtype, False)
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
        , deal=deal)
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
        , deal=deal)
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
    , deal=deal)

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
    _lock_dtype = deal.get("deal_type", "p2p")
    fee_pct    = get_deal_fee(_lock_dtype, False)
    if discount_applied:
        effective_fee_pct = get_deal_fee(_lock_dtype, True)  # bio rate for this deal type
        fee_amt = qty * (effective_fee_pct / 100)
    else:
        effective_fee_pct = fee_pct
        fee_amt = qty * (fee_pct / 100)
    final      = qty - fee_amt

    buyer_addr  = deal.get("buyer_address", "N/A")
    seller_addr = deal.get("seller_address", "N/A")

    # p2p: payout to buyer | product: payout to seller
    _dtype = deal.get("deal_type", "p2p")
    recv_addr  = buyer_addr if _dtype == "p2p" else seller_addr
    recv_label = "Buyer" if _dtype == "p2p" else "Seller"

    # IST time
    from datetime import timezone, timedelta
    IST = timezone(timedelta(hours=5, minutes=30))
    completed_ist = datetime.now(IST).strftime("%d %b %Y, %I:%M %p IST")

    # ── OxaPay Payout: transfer final amount to recipient's wallet ──
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

    if state.oxapay_key and recv_addr and recv_addr != "N/A" and final > 0:
        try:
            import uuid as _uuid
            loop = asyncio.get_event_loop()
            def _payout():
                req = urllib.request.Request(
                    "https://api.oxapay.com/merchants/payout",
                    data=_json.dumps({
                        "merchant":    state.oxapay_key,
                        "address":     recv_addr,
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
        , deal=deal)
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
    , deal=deal)

    # ── Vouch then close ──
    asyncio.create_task(send_vouch_request(ctx, did, deal))
    # (send_vouch_request handles both vouch+close and direct close internally)
    if False:  # placeholder to keep else block structure intact
        await ctx.bot.leave_chat(group_id)
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
    , deal=deal)


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
            , deal=deal)
        else:
            err_msg = f"OxaPay error {pdata.get('result')}: {pdata.get('message', 'Unknown')}"
            await update.message.reply_text(
                f"❌ <b>Payout Failed</b>\n\n<code>{err_msg}</code>",
                parse_mode="HTML"
            )
            await log(ctx, f"❌ <b>ADMIN FORCE DEPOSIT FAILED</b>\n\n{err_msg}\n👨‍💼 @{admin_user.username}", deal=deal)
    except Exception as e:
        await update.message.reply_text(f"❌ Error: <code>{e}</code>", parse_mode="HTML")
        await log(ctx, f"❌ <b>ADMIN FORCE DEPOSIT ERROR</b>\n\n{e}", deal=deal)


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

    _deal_dtype    = deal.get("deal_type", "p2p")
    fee_pct        = get_deal_fee(_deal_dtype, False)
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
    # p2p → buyer gets | product → seller gets
    _pr_dtype    = deal.get("deal_type", "p2p")
    _pr_recv_addr = deal.get("buyer_address", "N/A") if _pr_dtype == "p2p" else deal.get("seller_address", "N/A")
    _pr_recv_lbl  = "buyer" if _pr_dtype == "p2p" else "seller"

    await query.edit_message_text(
        f"\U0001f389 <b>BOTH CONFIRMED!</b>\n\n"
        f"\u23f3 Processing payout of <b>{final_to_buyer} {sym}</b> to {_pr_recv_lbl}\u2026",
        parse_mode="HTML"
    )

    TOKEN_NET_MAP = {
        "USDT_TRC20": ("USDT", "TRX"),
        "USDT_BEP20": ("USDT", "BSC"),
        "BTC":        ("BTC",  "BTC"),
        "LTC":        ("LTC",  "LTC"),
    }
    currency, network = TOKEN_NET_MAP.get(token, ("USDT", "TRX"))
    buyer_addr = _pr_recv_addr  # correct recipient based on deal type

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
        await log(ctx, f"\u274c PARTIAL PAYOUT FAILED\n\U0001f194 {did}\n{payout_err}", deal=deal)
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
        # Deal complete — vouch then close (after 1 min delay)
        async def _complete_after_delay():
            await asyncio.sleep(60)
            await send_vouch_request(ctx, did, deal)
        asyncio.create_task(_complete_after_delay())
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
    , deal=deal)



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
    , deal=deal)

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
    await log(ctx, f"⚠️ <b>DISPUTE OPENED</b>\n\n🆔 <code>{did}</code>\n📊 DISPUTED\n⏰ {deal['dispute_at']}", deal=deal)

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
    _disp_dtype = deal.get("deal_type", "p2p")
    fee_pct   = get_deal_fee(_disp_dtype, False)
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
    , deal=deal)

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
        await log(ctx, f"🚫 <b>DEAL CANCELLED (no deposit)</b>\n\n🆔 <code>{did}</code>\n👨‍💼 @{user.username}", deal=deal)
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

    _ref_dtype  = deal.get("deal_type", "p2p")
    fee_pct    = get_deal_fee(_ref_dtype, False)
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
    , deal=deal)


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
        , deal=deal)
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
    , deal=deal)

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
    await log(ctx, f"❌ <b>REFUND CANCELLED</b>\n\n🆔 <code>{did}</code>\n👤 @{user.username or user.id}", deal=deal)




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
    , deal=deal)

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
    await log(ctx, f"🚫 <b>DEAL CANCELLED</b>\n\n🆔 <code>{did}</code>\n👨‍💼 @{user.username}\n📊 Was: {old}\n⏰ {deal['cancelled_at']}", deal=deal)

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
            # Ask for hold limit immediately
            _admin_waiting[update.effective_user.id] = f"set_limit_for:{uid}"
            limit_kb = InlineKeyboardMarkup([[
                InlineKeyboardButton("⏭ Skip (No Limit)", callback_data="adm:cancel_input"),
                InlineKeyboardButton("⬅️ Back", callback_data="adm:status")
            ]])
            await update.message.reply_text(
                f"✅ Sub Admin Added: <code>{uid}</code>\n\n"
                f"💰 <b>Set Hold Limit for this admin?</b>\n"
                f"Enter amount in ₹ (e.g. <code>50000</code> or <code>50k</code>)\n"
                f"Or press Skip to set no limit.",
                parse_mode="HTML", reply_markup=limit_kb
            )
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
    elif field.startswith("set_limit_for:"):
        try:
            target_uid = int(field.split(":")[1])
            amt_parsed = parse_amount_smart(value)
            if amt_parsed is None:
                await update.message.reply_text("❌ Invalid amount. Try again (e.g. 50000 or 50k)", reply_markup=kb)
                return
            _admin_hold_limits[target_uid] = amt_parsed
            await update.message.reply_text(
                f"✅ Hold limit set!\n👤 <code>{target_uid}</code>\n💰 ₹{amt_parsed:,.0f}\n\nNotify at 80%, block at 100%.",
                parse_mode="HTML", reply_markup=kb
            )
        except Exception as e:
            await update.message.reply_text(f"❌ Error: {e}", reply_markup=kb)

    elif field == "fee":
        try:
            fee = float(value)
            if not (0 <= fee <= 50):
                await update.message.reply_text("❌ Fee must be 0–50.", reply_markup=kb)
                return
            old = state.fee_percent
            state.fee_percent = fee
            # Also sync to fee_config
            if not hasattr(state, "fee_config"):
                state.fee_config = {}
            state.fee_config["p2p_normal"] = fee
            state.fee_config["product_normal"] = fee
            state.fee_config["ge_normal"] = fee
            await update.message.reply_text(f"✅ Fee: <s>{old}%</s> → <b>{fee}%</b>\n\n<i>Applied to all deal types. Use /setfees for per-type config.</i>", parse_mode="HTML", reply_markup=kb)
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
    elif field == "timeout":
        try:
            hrs = float(value)
            if hrs <= 0 or hrs > 720:
                await update.message.reply_text("❌ Must be 1–720 hours.", reply_markup=kb)
                return
            state.deal_timeout_hours = hrs
            await update.message.reply_text(f"✅ Deal timeout set to <b>{hrs} hours</b>.", parse_mode="HTML", reply_markup=kb)
        except ValueError:
            await update.message.reply_text("❌ Invalid number. Enter hours (e.g. 24)", reply_markup=kb)

    elif field.startswith("fee_type:"):
        fee_key = field.split(":", 1)[1]
        valid = ["p2p_bio", "p2p_normal", "product_bio", "product_normal", "ge_bio", "ge_normal"]
        if fee_key not in valid:
            await update.message.reply_text("❌ Invalid fee type.", reply_markup=kb)
            return
        try:
            pct = float(value)
            if not (0 <= pct <= 50):
                await update.message.reply_text("❌ Must be 0–50%.", reply_markup=kb)
                return
            if not hasattr(state, "fee_config") or not isinstance(state.fee_config, dict):
                state.fee_config = {}
            state.fee_config[fee_key] = pct
            # Also keep state.fee_percent in sync with p2p_normal
            if fee_key == "p2p_normal":
                state.fee_percent = pct
            labels = {
                "p2p_bio":        "🔄 P2P — With Bio",
                "p2p_normal":     "🔄 P2P — Without Bio",
                "product_bio":    "🛒 Product — With Bio",
                "product_normal": "🛒 Product — Without Bio",
                "ge_bio":         "🏪 Group Escrow — With Bio",
                "ge_normal":      "🏪 Group Escrow — Without Bio",
            }
            label = labels.get(fee_key, fee_key)
            # Show updated fee panel
            fee_kb = InlineKeyboardMarkup([[
                InlineKeyboardButton("📊 Back to Fees", callback_data="adm:setfeetypes"),
                InlineKeyboardButton("🏠 Panel", callback_data="adm:status"),
            ]])
            await update.message.reply_text(
                f"✅ <b>Fee Updated!</b>\n\n"
                f"{label}: <b>{pct}%</b>",
                parse_mode="HTML", reply_markup=fee_kb
            )
        except ValueError:
            await update.message.reply_text("❌ Invalid percentage. Enter a number (e.g. 1.5)", reply_markup=kb)

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

    elif field == "log_group":
        try:
            gid = int(value.strip())
            state.log_group_id = gid
            await update.message.reply_text(f"✅ <b>Log Group Set!</b>\n🆔 <code>{gid}</code>", parse_mode="HTML", reply_markup=kb)
        except ValueError:
            await update.message.reply_text("❌ Invalid ID. Example: <code>-1001234567890</code>", parse_mode="HTML", reply_markup=kb)

    elif field == "dispute_group":
        try:
            gid = int(value.strip())
            state.dispute_group_id = gid
            await update.message.reply_text(f"✅ <b>Dispute Group Set!</b>\n🆔 <code>{gid}</code>", parse_mode="HTML", reply_markup=kb)
        except ValueError:
            await update.message.reply_text("❌ Invalid ID. Example: <code>-1001234567890</code>", parse_mode="HTML", reply_markup=kb)

    elif field == "vouch_group":
        try:
            gid = int(value.strip())
            state.vouch_group_id = gid
            await update.message.reply_text(f"✅ <b>Vouch Group Set!</b>\n🆔 <code>{gid}</code>", parse_mode="HTML", reply_markup=kb)
        except ValueError:
            await update.message.reply_text("❌ Invalid ID. Example: <code>-1001234567890</code>", parse_mode="HTML", reply_markup=kb)

    elif field == "escrow_group":
        try:
            gid = int(value.strip())
            state.escrow_group_id = gid
            if gid not in _ge_config:
                _ge_config[gid] = {"upi": {}, "log_group_id": state.log_group_id}
            await update.message.reply_text(
                f"✅ <b>Escrow Group Set!</b>\n🆔 <code>{gid}</code>\n\nAb is group mein bot forms accept karega.",
                parse_mode="HTML", reply_markup=kb)
        except ValueError:
            await update.message.reply_text("❌ Invalid ID. Example: <code>-1001234567890</code>", parse_mode="HTML", reply_markup=kb)

    elif field == "channel_setup":
        if "|" in value:
            parts = value.split("|", 1)
            link = parts[0].strip()
            name = parts[1].strip()
        else:
            link = value.strip()
            name = "Our Channel"
        if not link.startswith("http"):
            await update.message.reply_text(
                "❌ Valid link chahiye.\nFormat: <code>https://t.me/channel | Channel Name</code>",
                parse_mode="HTML", reply_markup=kb)
            return
        state.channel_link = link
        state.channel_name = name
        await update.message.reply_text(
            f"✅ <b>Channel Set!</b>\n📢 <b>{name}</b>\n🔗 {link}",
            parse_mode="HTML", reply_markup=kb)

    else:
        await update.message.reply_text("⚠️ Unknown field.", reply_markup=kb)

# ══════════════════════════════════════════════════════════
# VOUCH SYSTEM
# ══════════════════════════════════════════════════════════

# Pending vouch state:  did -> {...}
_vouch_pending: dict[str, dict] = {}
# Waiting for vouch text in GROUP:  user_id -> {"did": str, "role": str, "group_id": int}
_vouch_text_waiting: dict[int, dict] = {}


async def send_vouch_request(ctx, did: str, deal: dict):
    """Ask for vouch IN THE GROUP immediately after deal. 2 min then close."""
    if not getattr(state, "vouch_enabled", True) or not getattr(state, "vouch_group_id", None):
        # No vouch — just close after 60s
        await asyncio.sleep(60)
        await _close_group(ctx, deal)
        return

    group_id  = deal.get("group_id")
    buyer_u   = deal.get("buyer_username", "N/A")
    seller_u  = deal.get("seller_username", "N/A")
    buyer_id  = deal.get("buyer_id")
    seller_id = deal.get("seller_id")

    _vouch_pending[did] = {
        "deal": deal, "responses": {},
        "buyer_id": buyer_id, "seller_id": seller_id, "group_id": group_id,
    }

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(f"⭐ @{buyer_u} — Review",  callback_data=f"vouch:start:{did}:buyer"),
         InlineKeyboardButton("⏭ Skip",                   callback_data=f"vouch:skip:{did}:buyer")],
        [InlineKeyboardButton(f"⭐ @{seller_u} — Review", callback_data=f"vouch:start:{did}:seller"),
         InlineKeyboardButton("⏭ Skip",                   callback_data=f"vouch:skip:{did}:seller")],
    ])
    try:
        await ctx.bot.send_message(chat_id=group_id,
            text=(f"⭐ <b>Leave a Review!</b>\n\n"
                  f"🛒 @{buyer_u}  |  🏪 @{seller_u}\n\n"
                  f"Tap your button, then type your review in the group.\n"
                  f"<i>Group closes in 2 minutes.</i>"),
            reply_markup=kb, parse_mode="HTML")
    except Exception as ex:
        logger.warning(f"Vouch msg error: {ex}")

    await asyncio.sleep(120)
    _vouch_pending.pop(did, None)
    for uid, info in list(_vouch_text_waiting.items()):
        if info.get("did") == did:
            _vouch_text_waiting.pop(uid, None)
    await _close_group(ctx, deal)


async def _close_group(ctx, deal: dict):
    """Close and delete a deal group."""
    group_id = deal.get("group_id")
    if not group_id:
        return
    try:
        await ctx.bot.send_message(chat_id=group_id,
            text="🗑 <b>Group closing now. Thank you!</b>", parse_mode="HTML")
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
        logger.warning(f"Group close error {group_id}: {ex}")


async def handle_vouch_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE, d: str):
    """Handle vouch:start/skip:DID:role — triggered from GROUP buttons."""
    q    = update.callback_query
    user = q.from_user
    parts = d.split(":")
    if len(parts) < 4:
        await q.answer("❌ Invalid.", show_alert=True)
        return
    _, action, did, role = parts[0], parts[1], parts[2], parts[3]
    pending = _vouch_pending.get(did)
    if not pending:
        await q.answer("⏱ Vouch window expired.", show_alert=True)
        return
    expected_id = pending.get(f"{role}_id")
    if expected_id and user.id != expected_id:
        await q.answer("❌ Not your button.", show_alert=True)
        return
    if action == "skip":
        pending["responses"][role] = "skipped"
        await q.answer("⏭ Skipped.")
        return
    if action == "start":
        if user.id in _vouch_text_waiting:
            await q.answer("⏳ Already waiting — type your review in the group!", show_alert=True)
            return
        _vouch_text_waiting[user.id] = {"did": did, "role": role, "group_id": pending.get("group_id")}
        await q.answer("✍️ Type your review as a message in the group now!", show_alert=True)


async def process_vouch_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Called from group_message_handler when user types review in group."""
    user = update.effective_user
    chat = update.effective_chat
    text = update.message.text.strip()
    info = _vouch_text_waiting.get(user.id)
    if not info or info.get("group_id") != chat.id:
        return False
    _vouch_text_waiting.pop(user.id, None)
    did  = info["did"]
    role = info["role"]
    pending = _vouch_pending.get(did)
    deal = pending["deal"] if pending else state.deals.get(did)
    if not deal:
        return True
    from_name = f"@{user.username}" if user.username else user.first_name
    # Forward ONLY name + review to vouch group
    if getattr(state, "vouch_group_id", None):
        try:
            await ctx.bot.send_message(
                chat_id=state.vouch_group_id,
                text=f"⭐ <b>{from_name}</b>\n\n{text}",
                parse_mode="HTML")
        except Exception as e:
            logger.warning(f"Vouch forward error: {e}")
    await update.message.reply_text(
        f"✅ <b>Review posted! Thank you @{user.username or user.first_name} 🙏</b>",
        parse_mode="HTML")
    if pending:
        pending["responses"][role] = "vouched"
    return True

    return True


# ══════════════════════════════════════════════════════════
# MEMBER JOIN HANDLER — welcome message with instructions
# ══════════════════════════════════════════════════════════

async def handle_new_member(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Send welcome + mute if no username — works in any group where bot is active."""
    chat    = update.effective_chat
    members = update.message.new_chat_members
    if not members:
        return

    # P2P deal group check
    is_p2p_group = bool(deal_by_group(chat.id)[1])
    # GE group — set escrow group ya _ge_config mein hai, YA koi bhi group (restart safe)
    is_ge_group = (
        (getattr(state, "escrow_group_id", None) and chat.id == state.escrow_group_id) or
        (chat.id in _ge_config) or
        (not is_p2p_group)  # agar P2P nahi hai to GE treat karo
    )

    for member in members:
        if member.is_bot:
            continue

        # ── Username nahi hai → MUTE + set username ka button ──
        if not member.username:
            name = member.first_name or "User"
            try:
                from telegram import ChatPermissions
                await ctx.bot.restrict_chat_member(
                    chat_id=chat.id,
                    user_id=member.id,
                    permissions=ChatPermissions(
                        can_send_messages=False,
                        can_send_other_messages=False,
                        can_add_web_page_previews=False,
                    )
                )
            except Exception:
                pass

            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton(
                    "✅ Done — Username Set!",
                    callback_data=f"check_username:{member.id}:{chat.id}"
                )
            ]])

            try:
                await ctx.bot.send_message(
                    chat_id=chat.id,
                    text=(
                        f"🔇 <b>{name}</b>, aapko mute kar diya gaya hai!\n\n"
                        f"━━━━━━━━━━━━━━━━━━━\n\n"
                        f"⚠️ Is group mein message karne ke liye\n"
                        f"<b>Telegram Username set karna zaroori hai.</b>\n\n"
                        f"📌 <b>Kaise kare:</b>\n"
                        f"Settings → Edit Profile → Username\n\n"
                        f"Username set karne ke baad neeche button dabao 👇"
                    ),
                    parse_mode="HTML",
                    reply_markup=kb
                )
            except Exception:
                pass
            continue

        # ── Username hai → Welcome message ──
        uname = f"@{member.username}"

        if is_ge_group:
            welcome = (
                f"{PE_WELCOME} <b>Welcome {uname}!</b>\n"
                f"━━━━━━━━━━━━━━\n"
                f"{PE_SHIELD} <b>Baba Escrow</b> — Trusted P2P Platform\n\n"
                f"📝 <b>Forms:</b> <code>form</code> · <code>form2</code> · <code>form3</code> · <code>form4</code>\n"
                f"💰 <b>Calc:</b> <code>calc 5000</code> · 📋 <code>formtype</code>\n\n"
                f"{PE_LIGHTNING} <b>Flow:</b> Form bhejo → Bot check → Admin lock\n"
                f"→ <b>agree</b> likho · Pay karo · Release {PE_SPARK}\n\n"
                f"⚠️ Sabhi fields fill karo · /summary se help lo"
            )
        else:
            fee_pct = get_deal_fee("p2p", False)
            welcome = (
                f"{PE_WELCOME} <b>Welcome {uname}!</b>\n"
                f"━━━━━━━━━━━━━━\n"
                f"🔐 <b>P2P Escrow Deal Group</b>\n\n"
                f"{PE_LIGHTNING} <b>Steps:</b>\n"
                f"<code>/dd</code> → Form fill · <code>/buyer</code>/<code>/seller</code> set\n"
                f"<code>/token</code> → Token · <code>/deposit</code> → Deposit\n"
                f"<code>/verify</code> → Verify · <code>/release</code> → Release\n\n"
                f"💸 Fee: <b>{fee_pct}%</b> · ❓ <code>/dispute</code>"
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
    app.add_handler(CommandHandler("release",      cmd_ge_release_cmd))
    app.add_handler(CallbackQueryHandler(partial_confirm_callback,   pattern=r"^partial_confirm:"))
    app.add_handler(CallbackQueryHandler(handle_ge_confirm_release,  pattern=r"^ge_confirm_release:"))
    app.add_handler(CallbackQueryHandler(handle_check_username,      pattern=r"^check_username:"))
    app.add_handler(CallbackQueryHandler(handle_ge_agree,            pattern=r"^ge_agree:"))
    app.add_handler(CallbackQueryHandler(handle_ge_disagree,         pattern=r"^ge_disagree:"))
    app.add_handler(CallbackQueryHandler(handle_ge_rel_agree,        pattern=r"^ge_rel_agree:"))
    app.add_handler(CallbackQueryHandler(handle_ge_rel_dispute,      pattern=r"^ge_rel_dispute:"))
    app.add_handler(CallbackQueryHandler(handle_user_callbacks,      pattern=r"^user:"))
    app.add_handler(CallbackQueryHandler(handle_ge_rate,             pattern=r"^ge_rate:"))
    app.add_handler(CallbackQueryHandler(handle_ge_stars,            pattern=r"^ge_stars:"))
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

    # ── Group Escrow System Commands ──
    app.add_handler(CommandHandler("setescrowgroup", cmd_ge_setgroup))
    app.add_handler(CommandHandler("setchannel",     cmd_setchannel))
    app.add_handler(CommandHandler("blacklist",      cmd_blacklist))
    app.add_handler(CommandHandler("settimeout",     cmd_settimeout))
    app.add_handler(CommandHandler("calc",           cmd_calc))
    app.add_handler(CommandHandler("setlimit",       cmd_setlimit))
    app.add_handler(CommandHandler("warn",           cmd_warn))
    app.add_handler(CommandHandler("profile",        cmd_profile))
    app.add_handler(CommandHandler("myprofile",      cmd_myprofile))
    app.add_handler(CommandHandler("mystatus",       cmd_mystatus))
    app.add_handler(CommandHandler("stats",          cmd_ge_stats_full))
    app.add_handler(CommandHandler("rep",            cmd_reputation))
    app.add_handler(CommandHandler("available",      cmd_available))
    app.add_handler(CommandHandler("busy",           cmd_busy))
    app.add_handler(CommandHandler("adminstatus",    cmd_admin_status_public))
    app.add_handler(CommandHandler("summary",        cmd_summary))
    app.add_handler(CommandHandler("saveupi",        cmd_saveupi))
    app.add_handler(CommandHandler("listupi",        cmd_listupi))
    app.add_handler(CommandHandler("deleteupi",      cmd_deleteupi))
    app.add_handler(CommandHandler("pay",            cmd_ge_pay))
    app.add_handler(CommandHandler("add",            cmd_ge_add))
    app.add_handler(CommandHandler("close",          cmd_ge_close))
    app.add_handler(CommandHandler("transfer",       cmd_ge_transfer))
    app.add_handler(CommandHandler("myhold",         cmd_myhold))
    app.add_handler(CommandHandler("allhold",        cmd_allhold))
    app.add_handler(CommandHandler("search",         cmd_ge_search))
    app.add_handler(CommandHandler("stats",          cmd_ge_stats))
    app.add_handler(CommandHandler("escrowers",      cmd_ge_escrowers))
    app.add_handler(CommandHandler("help",           cmd_ge_help))
    app.add_handler(CommandHandler("form",           cmd_form1))
    app.add_handler(CommandHandler("form2",          cmd_form2))
    app.add_handler(CommandHandler("form3",          cmd_form3))
    app.add_handler(CommandHandler("form4",          cmd_form4))
    app.add_handler(CommandHandler("formtype",       cmd_formtype))

    # Message handlers — ORDER MATTERS: GE handler first, then existing
    app.add_handler(MessageHandler(
        filters.TEXT & filters.ChatType.GROUPS & ~filters.COMMAND,
        ge_group_message_handler
    ), group=0)
    app.add_handler(MessageHandler(
        filters.TEXT & filters.ChatType.GROUPS & ~filters.COMMAND,
        group_message_handler
    ), group=1)
    app.add_handler(MessageHandler(
        filters.TEXT & filters.ChatType.PRIVATE & ~filters.COMMAND,
        admin_input_handler
    ))

    # Callback buttons
    app.add_handler(CallbackQueryHandler(callback_handler))

    logger.info("All handlers registered. Bot running…")

    async def on_startup(app):
        asyncio.create_task(ge_daily_summary(app.bot))

    app.post_init = on_startup
    app.run_polling(allowed_updates=["message", "callback_query", "chat_member"])

# ══════════════════════════════════════════════════════════
# ██████████████████████████████████████████████████████████
# GROUP ESCROW SYSTEM — Multi-form, Multi-admin, UPI-based
# ██████████████████████████████████████████████████████████
# ══════════════════════════════════════════════════════════

import unicodedata, hashlib

# ── Group Escrow State ────────────────────────────────────
# group_id -> {upi_methods, escrow_group_id, log_group_id}
_ge_config:            dict[int, dict] = {}   # per-group config
_ge_deals:             dict[str, dict] = {}   # tid -> deal dict
_ge_msg_to_deal:       dict[int, dict] = {}   # message_id -> {group_id, tid}
_ge_group_latest_form: dict[int, dict] = {}   # group_id -> {text, message_id}
_ge_admin_holds:       dict[int, list] = {}   # admin_id -> [tid, ...]
_ge_blacklist:         dict[str, dict] = {}   # username.lower() -> {reason, by, at}
_ge_deal_timeout_hrs:  int             = 24   # deal timeout in hours
_ge_daily_stats: dict = {"date": "", "deals": 0, "volume": 0.0, "completed": 0, "cancelled": 0}
_ge_all_stats:   dict = {"total_deals": 0, "total_volume": 0.0, "completed": 0, "cancelled": 0}

# ── NEW FEATURES - Global State ───────────────────────────
_admin_hold_limits: dict[int, float] = {}     # admin_id -> max_hold_amount
_active_warns:      dict[str, dict]  = {}     # tid -> {warned_user, end_time, other_party}
_user_stats:        dict[str, dict]  = {}     # username -> {volume, deals, highest, rank}

# ── FEE STRUCTURE (stored in state config) ────────────────
# state.fee_config = {
#     "p2p_bio": 1.0,        # P2P with bio discount
#     "p2p_normal": 2.0,     # P2P without bio
#     "product_bio": 1.5,    # Product with bio
#     "product_normal": 3.0, # Product without bio
#     "ge_bio": 1.0,         # Group Escrow with bio
#     "ge_normal": 2.0,      # Group Escrow without bio
# }
# Fallback to state.fee_percent if not set

def ge_trade_id():
    import uuid
    return GE_TRADE_PREFIX + str(uuid.uuid4()).upper()[:8]


def parse_amount_smart(text: str) -> float | None:
    """Smart amount parser.
    Supports: 5000, 5k, 50K, 5L, 5 lakh, 1 cr, 1.5cr,
              ₹5000, Rs.5000, 5,000, 50,000, 10000/-, 5000rs, 5000 rs
    Returns None if completely unparseable."""
    import re as _re_amt
    if not text:
        return None
    t = text.strip().lower()

    # Strip currency symbols
    t = _re_amt.sub(r'[₹$€£¥]', '', t)
    # Strip trailing noise: rs, rs., rupee, rupees (with or without space/boundary)
    t = _re_amt.sub(r'\s*rupees?\s*$', '', t)
    t = _re_amt.sub(r'\s*rs\.?\s*$', '', t)
    # Strip trailing /-
    t = _re_amt.sub(r'/-\s*$', '', t)
    # Remove commas, underscores, spaces
    t = t.replace(',', '').replace('_', '').replace(' ', '')

    if not t:
        return None

    # Lakh: 5L, 5l, 5lakh, 2.5L
    m = _re_amt.fullmatch(r'(\d+(?:\.\d+)?)[lL](?:akh)?', t)
    if m:
        try: return float(m.group(1)) * 100_000
        except: return None

    # Crore: 1cr, 1.5cr, 1crore
    m = _re_amt.fullmatch(r'(\d+(?:\.\d+)?)cr(?:ore)?', t)
    if m:
        try: return float(m.group(1)) * 10_000_000
        except: return None

    # K suffix: 5k, 50K, 1.5k
    m = _re_amt.fullmatch(r'(\d+(?:\.\d+)?)k', t)
    if m:
        try: return float(m.group(1)) * 1000
        except: return None

    # Plain number only — reject anything with letters
    m = _re_amt.fullmatch(r'\d+(?:\.\d+)?', t)
    if m:
        try: return float(t)
        except: return None

    return None

def get_deal_fee(deal_type: str = "p2p", has_bio: bool = False) -> float:
    """Get correct fee% based on deal type and bio status.
    deal_type: 'p2p' | 'product' | 'ge'
    Falls back to state.fee_percent if fee_config not set for that type.
    """
    fc = getattr(state, "fee_config", {}) or {}
    if deal_type == "p2p":
        key = "p2p_bio" if has_bio else "p2p_normal"
    elif deal_type == "product":
        key = "product_bio" if has_bio else "product_normal"
    elif deal_type in ("ge", "group"):
        key = "ge_bio" if has_bio else "ge_normal"
    else:
        key = "p2p_normal"
    # Use fee_config value if set, else fallback to global fee_percent
    return float(fc.get(key, state.fee_percent))


def normalize_text(text: str) -> str:
    """Normalize unicode/special fonts to ASCII for parsing."""
    try:
        nfkd = unicodedata.normalize("NFKD", text)
        ascii_str = nfkd.encode("ascii", "ignore").decode("ascii")
        return ascii_str
    except Exception:
        return text

def strip_field_value(val: str) -> str:
    """Clean up a field value — strip spaces, dashes, colons. Take only first meaningful line."""
    if not val:
        return ""
    # Take only first line — ignore example/note lines below
    first_line = val.split("\n")[0].strip()
    first_line = first_line.lstrip(":-").strip()
    # Remove parenthetical examples like "(Example: ...)"
    import re as _re2
    first_line = _re2.sub(r'\(.*?\)', '', first_line).strip()
    if not first_line or first_line.lower() in ("-", "—", "nil", "none", "n/a", ""):
        return ""
    return first_line

def parse_field(text: str, *keys) -> str:
    """Extract value for a field from form text.
    - Keys matched on normalized text (handles unicode/fancy fonts)
    - Value taken from original line (preserves @, emojis)
    - If value is blank/dash, peeks at next non-empty line (multiline forms)
    """
    import re as _re_pf
    orig_lines = text.splitlines()
    norm_lines = normalize_text(text).splitlines()

    for i, norm_line in enumerate(norm_lines):
        nl = norm_line.strip().lower()
        for key in keys:
            kn = normalize_text(key).lower()
            if kn in nl:
                orig_line = orig_lines[i] if i < len(orig_lines) else norm_line
                val = ""
                if ":" in orig_line:
                    val = orig_line.split(":", 1)[1]
                elif " - " in orig_line:
                    val = orig_line.split(" - ", 1)[1]
                elif orig_line.strip().endswith("-"):
                    val = ""

                cleaned = strip_field_value(val)
                if cleaned:
                    return cleaned

                # Value empty on same line — peek next non-empty line
                for j in range(i + 1, min(i + 4, len(orig_lines))):
                    next_line = orig_lines[j].strip()
                    if not next_line:
                        continue
                    # Stop if next line looks like another field key
                    norm_next = normalize_text(next_line).lower()
                    if _re_pf.search(r'[a-z ]{3,}\s*:', norm_next):
                        break
                    cleaned2 = strip_field_value(next_line)
                    if cleaned2:
                        return cleaned2
                break
    return ""

def detect_form_type(text: str) -> str | None:
    """Detect which form type the message contains."""
    norm = normalize_text(text).lower()
    # BET: must have bet-specific fields
    if any(k in norm for k in ["bet type", "bet deal form", "total bet amount", "game name", "party 1 ka loss", "party 2 ka loss"]):
        return "BET"
    # THIRD PARTY: must have third party username or role
    if any(k in norm for k in ["third party username", "third party role", "third party charges", "third party deal form"]):
        return "THIRD_PARTY"
    # SERVICE: must have service-specific fields
    if any(k in norm for k in ["service type", "service deal form", "work completion time", "proof of work"]):
        return "SERVICE"
    # NORMAL: standard deal form
    if any(k in norm for k in ["escrow deal form", "deal of", "buyer bank name", "payment method"]):
        return "NORMAL"
    # Fallback broader check
    if any(k in norm for k in ["buyer username", "seller username", "total amount", "maximum time"]):
        return "NORMAL"
    return None

def parse_form(text: str, form_type: str) -> dict:
    """Parse form text and extract all fields."""
    d = {"form_type": form_type, "raw": text}
    if form_type == "NORMAL":
        d["deal_of"]      = parse_field(text, "Deal Of", "dealof")
        d["amount"]       = parse_field(text, "Total Amount", "amount")
        d["max_time"]     = parse_field(text, "Maximum Time", "max time", "time")
        d["buyer"]        = parse_field(text, "Buyer Username", "buyer")
        d["seller"]       = parse_field(text, "Seller Username", "seller")
        d["payment_method"] = parse_field(text, "Buyer Bank Name", "Payment Method", "bank name")
        d["terms"]        = parse_field(text, "Terms & Conditions", "terms")
    elif form_type == "BET":
        d["bet_type"]     = parse_field(text, "Bet Type", "bet type")
        d["amount"]       = parse_field(text, "Total Bet Amount", "bet amount", "amount")
        d["game_name"]    = parse_field(text, "Game Name", "game")
        d["buyer"]        = parse_field(text, "Party 1 Username", "party 1", "party1")  # treat P1 as buyer
        d["seller"]       = parse_field(text, "Party 2 Username", "party 2", "party2")
        d["p1_loss"]      = parse_field(text, "Party 1 ka loss", "party 1 loss", "p1 loss")
        d["p2_loss"]      = parse_field(text, "Party 2 ka loss", "party 2 loss", "p2 loss")
        d["max_time"]     = parse_field(text, "Maximum Time", "max time", "time")
        d["terms"]        = parse_field(text, "Terms & Conditions", "terms")
    elif form_type == "THIRD_PARTY":
        d["deal_of"]      = parse_field(text, "Deal Of", "dealof")
        d["amount"]       = parse_field(text, "Total Amount", "amount")
        d["buyer"]        = parse_field(text, "Buyer Username", "buyer")
        d["seller"]       = parse_field(text, "Seller Username", "seller")
        d["third_party"]  = parse_field(text, "Third Party Username", "third party")
        d["tp_role"]      = parse_field(text, "Third Party Role", "role")
        d["tp_charges"]   = parse_field(text, "Third Party Charges", "charges")
        d["work_details"] = parse_field(text, "Work Details", "work")
        d["max_time"]     = parse_field(text, "Maximum Time", "max time", "time")
        d["terms"]        = parse_field(text, "Terms & Conditions", "terms")
    elif form_type == "SERVICE":
        d["service_type"] = parse_field(text, "Service Type", "service")
        d["work_details"] = parse_field(text, "Work Details", "work")
        d["amount"]       = parse_field(text, "Total Amount", "amount")
        d["buyer"]        = parse_field(text, "Buyer Username", "buyer")
        d["seller"]       = parse_field(text, "Seller Username", "seller")
        d["completion_time"] = parse_field(text, "Work Completion Time", "completion time", "time")
        d["proof_of_work"] = parse_field(text, "Proof of Work", "proof")
        d["terms"]        = parse_field(text, "Terms & Conditions", "terms")
    return d

def ge_deal_summary(deal: dict) -> str:
    """Generate a clean deal summary string for confirmations and logs."""
    ft    = deal.get("form_type", "NORMAL")
    tid   = deal.get("tid", "—")
    amt   = deal.get("amount", "—")
    buyer = deal.get("buyer", "—")
    seller= deal.get("seller", "—")
    escr  = deal.get("escrower_username", "—")
    status= deal.get("status", "LOCKED")
    ts    = deal.get("locked_at", "—")[:19].replace("T", " ") + " UTC" if deal.get("locked_at") else "—"

    type_emoji = {"NORMAL": "🔹", "BET": "🎯", "THIRD_PARTY": "🤝", "SERVICE": "🛠️"}.get(ft, "🔹")
    type_label = {"NORMAL": "Normal Deal", "BET": "Bet Deal", "THIRD_PARTY": "Third Party Deal", "SERVICE": "Service Deal"}.get(ft, ft)

    lines = [
        f"{type_emoji} <b>{type_label}</b>",
        f"━━━━━━━━━━━━━━━━━━━",
        f"🆔 Trade ID: <code>{tid}</code>",
        f"💰 Amount: <b>{amt}</b>",
        f"🛒 Buyer: <b>{buyer}</b>",
        f"🏪 Seller: <b>{seller}</b>",
        f"👨‍⚖️ Escrower: @{escr}",
        f"📊 Status: <b>{status}</b>",
        f"⏰ {ts}",
    ]
    if ft == "BET":
        lines.insert(3, f"🎮 Game: {deal.get('game_name','—')}")
    elif ft == "THIRD_PARTY":
        lines.insert(5, f"🔸 Third Party: {deal.get('third_party','—')} ({deal.get('tp_role','—')})")
    elif ft == "SERVICE":
        lines.insert(3, f"⚙️ Service: {deal.get('service_type','—')}")

    return "\n".join(lines)

# ── Group Escrow Config Commands ───────────────────────────

async def cmd_ge_setgroup(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Set current group as escrow group. /setescrowgroup"""
    chat = update.effective_chat
    user = update.effective_user
    if not is_admin(user.id):
        return
    if chat.type == "private":
        await update.message.reply_text("❌ Run inside the group to set as escrow group.")
        return
    if chat.id not in _ge_config:
        _ge_config[chat.id] = {"upi": {}, "log_group_id": state.log_group_id}
    else:
        _ge_config[chat.id]["log_group_id"] = state.log_group_id
    # Save in state so ge_group_message_handler can restrict to this group
    state.escrow_group_id = chat.id
    await update.message.reply_text(
        f"✅ <b>Escrow Group Set!</b>\n\n"
        f"📋 {chat.title}\n🆔 <code>{chat.id}</code>\n\n"
        f"📌 Commands available:\n"
        f"<code>/saveupi NAME UPIID</code> — save UPI method\n"
        f"<code>/myhold</code> — your active holdings\n"
        f"<code>/allhold</code> — all escrowers\n"
        f"<code>/stats</code> — group stats\n"
        f"<code>/escrowers</code> — list all admins\n\n"
        f"<i>Admins: Reply 'BOTH AGREE' on any form to lock a deal.</i>",
        parse_mode="HTML"
    )

async def cmd_setchannel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/setchannel LINK NAME — Set channel join link for /start button.
    Example: /setchannel https://t.me/mychannel Baba Escrow Channel"""
    user = update.effective_user
    if not is_admin(user.id):
        return
    args = ctx.args
    if not args or len(args) < 1:
        await update.message.reply_text(
            "❌ <b>Usage:</b>\n"
            "<code>/setchannel LINK</code>\n"
            "<code>/setchannel LINK Channel Name</code>\n\n"
            "<b>Example:</b>\n"
            "<code>/setchannel https://t.me/babaescrow Baba Escrow Official</code>",
            parse_mode="HTML"
        )
        return
    link = args[0]
    name = " ".join(args[1:]) if len(args) > 1 else "Our Channel"
    if not link.startswith("http"):
        await update.message.reply_text("❌ Valid link do — https:// se shuru hona chahiye.")
        return
    state.channel_link = link
    state.channel_name = name
    await update.message.reply_text(
        f"✅ <b>Channel Set!</b>\n\n"
        f"📢 Name: <b>{name}</b>\n"
        f"🔗 Link: {link}\n\n"
        f"Ab /start karne pe ye button dikhega users ko.",
        parse_mode="HTML"
    )

async def cmd_saveupi(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/saveupi NAME UPIID — Save a UPI payment method."""
    chat = update.effective_chat
    user = update.effective_user
    if not is_admin(user.id):
        await update.message.reply_text("❌ Admin only.")
        return
    if len(ctx.args) < 2:
        await update.message.reply_text(
            "Usage: <code>/saveupi NAME UPIID</code>\n"
            "Example: <code>/saveupi axis stealed@axl</code>",
            parse_mode="HTML"
        )
        return
    name   = ctx.args[0].lower().strip()
    upi_id = ctx.args[1].strip()
    gid    = chat.id if chat.type != "private" else None

    # Store globally accessible (any group admin can use)
    if not hasattr(state, "upi_methods"):
        state.upi_methods = {}
    state.upi_methods[name] = {"upi_id": upi_id, "added_by": user.username or str(user.id)}

    await update.message.reply_text(
        f"✅ <b>UPI Saved!</b>\n\n"
        f"🏷 Name: <code>{name}</code>\n"
        f"💳 UPI ID: <code>{upi_id}</code>\n\n"
        f"Use: <code>/pay {name}</code> or <code>/pay 2 {name}</code>",
        parse_mode="HTML"
    )

async def cmd_listupi(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/listupi — List all saved UPI methods."""
    if not is_admin(update.effective_user.id):
        return
    methods = getattr(state, "upi_methods", {})
    if not methods:
        await update.message.reply_text("❌ No UPI methods saved. Use /saveupi NAME UPIID")
        return
    lines = ["💳 <b>Saved UPI Methods:</b>\n"]
    for name, data in methods.items():
        lines.append(f"• <code>{name}</code> → <code>{data['upi_id']}</code>  (by @{data['added_by']})")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")

async def cmd_deleteupi(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/deleteupi NAME — Remove a UPI method."""
    if not is_admin(update.effective_user.id):
        return
    if not ctx.args:
        await update.message.reply_text("Usage: <code>/deleteupi NAME</code>", parse_mode="HTML")
        return
    name = ctx.args[0].lower().strip()
    methods = getattr(state, "upi_methods", {})
    if name not in methods:
        await update.message.reply_text(f"❌ UPI method <code>{name}</code> not found.", parse_mode="HTML")
        return
    del methods[name]
    await update.message.reply_text(f"✅ Removed <code>{name}</code>", parse_mode="HTML")

# ── QR Generation ─────────────────────────────────────────

def generate_upi_qr(upi_id: str, amount: float, name: str = "Escrow") -> bytes:
    """Generate UPI QR code bytes."""
    upi_url = f"upi://pay?pa={upi_id}&pn={name}&am={amount:.2f}&cu=INR"
    img = qrcode.make(upi_url)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf.read()

# ── BOTH AGREE Detection ───────────────────────────────────

async def handle_both_agree(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Detect 'BOTH AGREE' from admin — reply to form, or find latest form in group."""
    chat = update.effective_chat
    user = update.effective_user
    msg  = update.message

    if not is_admin(user.id):
        await msg.reply_text("❌ Sirf admin BOTH AGREE kar sakta hai.")
        return

    norm_text = normalize_text(msg.text or "").strip().upper()
    if "BOTH AGREE" not in norm_text and "BOTHAGREED" not in norm_text and "BOTH AGREED" not in norm_text:
        return

    # ── Get form text — from reply OR latest unprocessed form in group ──
    replied_text = ""
    rmid = None

    if msg.reply_to_message:
        replied_text = msg.reply_to_message.text or msg.reply_to_message.caption or ""
        rmid = msg.reply_to_message.message_id
        # "me" ko form bhejne wale ke username se replace karo
        sender = msg.reply_to_message.from_user
        if sender:
            sender_tag = f"@{sender.username}" if sender.username else sender.first_name
            import re as _re
            replied_text = _re.sub(r'(?<![a-zA-Z@])me(?![a-zA-Z])', sender_tag, replied_text, flags=_re.IGNORECASE)
    else:
        # No reply — find latest form sent in this group (check last tracked form)
        latest = _ge_group_latest_form.get(chat.id)
        if latest:
            replied_text = latest.get("text", "")
            rmid = latest.get("message_id")
        if not replied_text:
            await msg.reply_text(
                "⚠️ <b>BOTH AGREE kaise karo:</b>\n\n"
                "Form message pe <b>reply</b> karo aur likho:\n"
                "<code>BOTH AGREE</code>",
                parse_mode="HTML"
            )
            return

    form_type = detect_form_type(replied_text)

    if not form_type:
        await msg.reply_text("❌ Ye koi valid form nahi lag raha. Form pe reply karke BOTH AGREE karo.")
        return

    # Check if already locked
    if rmid and rmid in _ge_msg_to_deal:
        existing_tid = _ge_msg_to_deal[rmid].get("tid")
        existing_deal = _ge_deals.get(existing_tid, {})
        if existing_deal.get("status") not in ("CANCELLED",):
            await msg.reply_text(
                f"⚠️ Ye deal already lock hai!\n🆔 <code>{existing_tid}</code>\n"
                f"👨‍⚖️ Escrower: @{existing_deal.get('escrower_username','?')}",
                parse_mode="HTML"
            )
            return

    # Parse form
    form_data = parse_form(replied_text, form_type)

    # ── Blank field check — buyer/seller ko tag karo ──
    def is_blank(val):
        return not val or str(val).strip() in ("", "—", "-", "nil", "none", "n/a", "N/A")

    buyer_raw  = form_data.get("buyer", "")
    seller_raw = form_data.get("seller", "")
    amount_raw = form_data.get("amount", "")

    blank_fields = []
    if is_blank(buyer_raw):
        blank_fields.append("● Buyer Username")
    if is_blank(seller_raw):
        blank_fields.append("● Seller Username")
    if is_blank(amount_raw):
        blank_fields.append("● Total Amount")
    if form_type == "NORMAL" and is_blank(form_data.get("deal_of", "")):
        blank_fields.append("● Deal Of")
    if form_type == "BET" and is_blank(form_data.get("game_name", "")):
        blank_fields.append("● Game Name")

    if blank_fields:
        buyer_tag  = f"@{buyer_raw.lstrip('@')}"  if not is_blank(buyer_raw)  else ""
        seller_tag = f"@{seller_raw.lstrip('@')}" if not is_blank(seller_raw) else ""
        tags = " ".join(filter(None, [buyer_tag, seller_tag]))
        fields_text = "\n".join(blank_fields)
        await msg.reply_text(
            f"⚠️ <b>FORM INCOMPLETE!</b>\n\n"
            f"{tags if tags else 'Buyer / Seller'}\n\n"
            f"Ye fields khali hain — pehle fill karo:\n\n"
            f"<b>{fields_text}</b>\n\n"
            f"Form dobara bhejo phir admin BOTH AGREE karega.",
            parse_mode="HTML"
        )
        return

    tid = ge_trade_id()
    now = datetime.utcnow().isoformat()

    # ── Username mandatory check ──
    def is_blank(val):
        return not val or str(val).strip().lower() in ("", "—", "-", "nil", "none", "n/a")

    buyer_raw  = form_data.get("buyer",  "")
    seller_raw = form_data.get("seller", "")

    # Strip @ for username
    buyer_uname  = buyer_raw.lstrip("@").strip()  if not is_blank(buyer_raw)  else ""
    seller_uname = seller_raw.lstrip("@").strip() if not is_blank(seller_raw) else ""

    # Validate usernames look like real usernames (not "me", not blank after ":-")
    invalid_usernames = []
    if not buyer_uname or len(buyer_uname) < 3:
        invalid_usernames.append("Buyer Username (valid @username chahiye)")
    if not seller_uname or len(seller_uname) < 3:
        invalid_usernames.append("Seller Username (valid @username chahiye)")

    if invalid_usernames:
        await msg.reply_text(
            f"⛔️ <b>DEAL REJECT — Username Missing!</b>\n\n"
            f"━━━━━━━━━━━━━━━━━━━\n\n"
            f"❌ Ye fields invalid hain:\n"
            + "\n".join(f"• {f}" for f in invalid_usernames) +
            f"\n\n📌 Dono parties apna Telegram <b>@username</b> form mein likho.\n"
            f"Username ke bina deal lock nahi hogi. ⚠️",
            parse_mode="HTML"
        )
        return

    buyer_tag  = f"@{buyer_uname}"
    seller_tag = f"@{seller_uname}"

    # ── Blacklist check ──
    blacklisted = []
    if buyer_uname.lower() in _ge_blacklist:
        bl = _ge_blacklist[buyer_uname.lower()]
        blacklisted.append(f"🚫 Buyer @{buyer_uname}: {bl.get('reason','Banned')}")
    if seller_uname.lower() in _ge_blacklist:
        bl = _ge_blacklist[seller_uname.lower()]
        blacklisted.append(f"🚫 Seller @{seller_uname}: {bl.get('reason','Banned')}")

    if blacklisted:
        await msg.reply_text(
            f"⛔️ <b>DEAL REJECT — BLACKLISTED USER!</b>\n\n"
            f"━━━━━━━━━━━━━━━━━━━\n\n"
            + "\n".join(blacklisted) +
            f"\n\n❌ Blacklisted users deal nahi kar sakte.\n"
            f"Admin se contact karo agar galti hai.",
            parse_mode="HTML"
        )
        return

    # ── Duplicate deal check ──
    dup = _check_duplicate_deal(buyer_uname, seller_uname)
    if dup:
        await msg.reply_text(
            f"⚠️ <b>DUPLICATE DEAL DETECTED!</b>\n\n"
            f"@{buyer_uname} aur @{seller_uname} ke beech pehle se ek deal active hai:\n\n"
            f"🆔 <code>{dup['tid']}</code>\n"
            f"📊 Status: <b>{dup.get('status','—')}</b>\n"
            f"💰 Amount: {dup.get('amount','—')}\n\n"
            f"⚠️ Kya aap sure ho new deal banana chahte ho?\n"
            f"Pehle wali deal pehle complete karo.",
            parse_mode="HTML"
        )
        return
    bio_tag = getattr(state, "required_bio", None)
    bio_status_buyer  = "❓ Unknown"
    bio_status_seller = "❓ Unknown"
    if bio_tag:
        async def _check_bio(uname_str: str) -> str:
            # Try Telethon first (gets real bio), fallback to Bot API
            if state.telethon_client:
                try:
                    from telethon.tl.functions.users import GetFullUserRequest
                    entity = await state.telethon_client.get_entity(f"@{uname_str}")
                    full   = await state.telethon_client(GetFullUserRequest(entity))
                    bio_text = full.full_user.about or ""
                    return "✅ Set" if bio_tag.lower() in bio_text.lower() else "❌ Not Set"
                except Exception:
                    pass
            try:
                info = await ctx.bot.get_chat(f"@{uname_str}")
                bio_val = getattr(info, "bio", None) or getattr(info, "description", None) or ""
                return "✅ Set" if bio_tag.lower() in bio_val.lower() else "❌ Not Set"
            except Exception:
                return "❓ Can\'t check"
        bio_status_buyer  = await _check_bio(buyer_uname)
        bio_status_seller = await _check_bio(seller_uname)

    # ── Admin hold limit check ──
    amt_float = parse_amount_smart(str(form_data.get("amount", "0"))) or 0
    if amt_float > 0:
        can_proceed = await check_admin_hold_limit(ctx, user.id, amt_float)
        if not can_proceed:
            await msg.reply_text(
                "❌ <b>Admin hold limit reached!</b>\n\n"
                "Kuch deals complete karo pehle.",
                parse_mode="HTML"
            )
            return

    # Parse amount to clean float string before storing
    _raw_amt = form_data.get("amount", "0")
    _parsed_amt = parse_amount_smart(str(_raw_amt))
    if _parsed_amt is not None:
        form_data["amount"] = str(int(_parsed_amt)) if _parsed_amt == int(_parsed_amt) else str(_parsed_amt)

    deal = {
        "tid": tid,
        "form_type": form_type,
        "status": "LOCKED",
        "escrower_id": user.id,
        "escrower_username": user.username or str(user.id),
        "locked_at": now,
        "locked_msg_id": rmid,
        "form_message_id": rmid,  # Store for log links
        "group_id": chat.id,
        "buyer_uname": buyer_uname,
        "seller_uname": seller_uname,
        "buyer_has_bio": bio_status_buyer == "✅ Set",
        "seller_has_bio": bio_status_seller == "✅ Set",
        "added": False,
        "closed": False,
        "received_amount": None,
        "released_amount": None,
        "transfer_history": [],
        "buyer_agreed": False,
        "seller_agreed": False,
        **form_data,
    }

    _ge_deals[tid] = deal
    if rmid:
        _ge_msg_to_deal[rmid] = {"tid": tid, "group_id": chat.id}

    if user.id not in _ge_admin_holds:
        _ge_admin_holds[user.id] = []
    _ge_admin_holds[user.id].append(tid)

    type_emoji = {"NORMAL": "🔹", "BET": "🎯", "THIRD_PARTY": "🤝", "SERVICE": "🛠️"}.get(form_type, "🔹")
    type_label = {"NORMAL": "Normal Deal", "BET": "Bet Deal", "THIRD_PARTY": "Third Party Deal", "SERVICE": "Service Deal"}.get(form_type, form_type)
    amt   = deal.get("amount", "—")
    ist_t = ist_now()

    # ── Bio info line ──
    bio_line = ""
    if bio_tag:
        bio_line = (
            f"\n━━━━━━━━━━━━━━━━━━━\n"
            f"🏷 <b>Bio Tag Check</b> (<code>{bio_tag}</code>):\n"
            f"🛒 Buyer {buyer_tag}: {bio_status_buyer}\n"
            f"🏪 Seller {seller_tag}: {bio_status_seller}\n"
        )

    extra = ""
    if form_type == "BET":
        extra = f"\n🎯 {deal.get('game_name','—')}"
    elif form_type == "THIRD_PARTY":
        extra = f"\n🤝 3rd Party: {deal.get('third_party','—')}"
    elif form_type == "SERVICE":
        extra = f"\n⚙️ Service: {deal.get('service_type','—')}"

    lock_msg = (
        f"{PE_SHIELD} <b>DEAL LOCKED</b>\n"
        f"━━━━━━━━━━━━━━\n"
        f"{type_emoji} <b>{type_label}</b> · 🪪 <code>{tid}</code>\n"
        f"━━━━━━━━━━━━━━\n"
        f"💰 <b>{amt}</b>{extra}\n"
        f"🛒 Buyer: <b>{buyer_tag}</b>\n"
        f"🏪 Seller: <b>{seller_tag}</b>\n"
        f"👨‍⚖️ @{user.username or user.id} · ⏰ {ist_t}"
        + (f"\n\n🏷 Bio · Buyer: {bio_status_buyer} · Seller: {bio_status_seller}" if bio_tag else "")
    )

    # ── Agree + Disagree buttons ──
    agree_kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Agree (Buyer)",     callback_data=f"ge_agree:buyer:{tid}:{buyer_uname}"),
            InlineKeyboardButton("✅ Agree (Seller)",    callback_data=f"ge_agree:seller:{tid}:{seller_uname}"),
        ],
        [
            InlineKeyboardButton("❌ Disagree (Buyer)",  callback_data=f"ge_disagree:buyer:{tid}:{buyer_uname}"),
            InlineKeyboardButton("❌ Disagree (Seller)", callback_data=f"ge_disagree:seller:{tid}:{seller_uname}"),
        ]
    ])

    agree_msg = (
        f"{PE_AGREE} <b>Confirm karo</b> · <code>{tid}</code>\n"
        f"🛒 {buyer_tag} · 🏪 {seller_tag}\n\n"
        f"✅ Agree likhkar ya button dabao\n"
        f"❌ Disagree = 20s mein form delete"
    )

    await ctx.bot.send_message(chat_id=chat.id, text=lock_msg, parse_mode="HTML",
                               reply_to_message_id=rmid if rmid else None)
    agree_sent = await ctx.bot.send_message(chat_id=chat.id, text=agree_msg,
                                             parse_mode="HTML", reply_markup=agree_kb)
    # Store agree message id for deletion on disagree
    deal["agree_msg_id"] = agree_sent.message_id

    await _ge_log(ctx, deal, "🔒 DEAL LOCKED", f"Escrower: @{user.username}\nLocked at: {ist_t}")

    # ── Start timeout watcher + payment reminder ──
    asyncio.create_task(ge_payment_reminder(ctx, tid, chat.id))
    asyncio.create_task(ge_timeout_watcher(ctx, tid, chat.id, _ge_deal_timeout_hrs))

    # ── Update global stats ──
    _ge_all_stats["total_deals"] = _ge_all_stats.get("total_deals", 0) + 1
    today_str = datetime.utcnow().strftime("%Y-%m-%d")
    if _ge_daily_stats.get("date") != today_str:
        _ge_daily_stats.update({"date": today_str, "deals": 0, "volume": 0.0, "completed": 0, "cancelled": 0})
    _ge_daily_stats["deals"] = _ge_daily_stats.get("deals", 0) + 1


async def cmd_ge_release_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/release [TID] — Release deal (GE or P2P). Case insensitive."""
    user = update.effective_user
    chat = update.effective_chat

    if not is_admin(user.id):
        # Non-admin P2P release
        await cmd_release_partial(update, ctx)
        return

    # GE release — find deal
    args = ctx.args or []
    deal = None

    # Check if TID given
    for a in args:
        if a.upper().startswith("GE-"):
            deal = _ge_deals.get(a.upper())
            break

    if not deal:
        deal = _resolve_ge_deal(update)

    if not deal:
        # Find latest in this group
        for d in reversed(list(_ge_deals.values())):
            if d.get("group_id") == chat.id and d.get("status") in ("LOCKED", "ACTIVE"):
                deal = d
                break

    if deal and deal.get("status") in ("LOCKED", "ACTIVE"):
        await _ge_handle_release(update, ctx, deal)
        return

    # Fall back to P2P release
    await cmd_release_partial(update, ctx)


async def handle_ge_disagree(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Buyer/Seller disagreed — notify, countdown, delete form."""
    q    = update.callback_query
    user = q.from_user
    data = q.data  # ge_disagree:buyer/seller:TID:expected_uname

    try:
        _, role, tid, expected_uname = data.split(":", 3)
    except Exception:
        await q.answer("❌ Error.", show_alert=True)
        return

    deal = _ge_deals.get(tid)
    if not deal:
        await q.answer("❌ Deal nahi mili.", show_alert=True)
        return

    actual_uname = (user.username or "").lower()
    if actual_uname != expected_uname.lower():
        await q.answer(
            f"⚠️ Ye button sirf {role.title()} ke liye hai!",
            show_alert=True
        )
        return

    await q.answer("❌ Tumne disagree kiya — form 20 sec mein delete hoga.", show_alert=True)

    buyer_tag  = f"@{deal.get('buyer_uname',  deal.get('buyer',  '')).lstrip('@')}"
    seller_tag = f"@{deal.get('seller_uname', deal.get('seller', '')).lstrip('@')}"
    disagree_tag = f"@{user.username or user.first_name}"

    # Mark deal as cancelled
    deal["status"]        = "CANCELLED"
    deal["cancelled_at"]  = datetime.utcnow().isoformat()
    deal["cancel_reason"] = f"{role.title()} @{user.username} ne disagree kiya"

    _ge_all_stats["cancelled"] = _ge_all_stats.get("cancelled", 0) + 1
    _ge_daily_stats["cancelled"] = _ge_daily_stats.get("cancelled", 0) + 1

    countdown_msg = await ctx.bot.send_message(
        chat_id=q.message.chat.id,
        text=(
            f"{PE_REJECT} <b>Deal Rejected!</b> · <code>{tid}</code>\n"
            f"{disagree_tag} ({role}) ne disagree kiya\n\n"
            f"{buyer_tag} {seller_tag}\n"
            f"⏳ 20s mein delete · Issue ho to admin se baat karo"
        ),
        parse_mode="HTML"
    )

    # Delete agree message buttons
    try:
        await q.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass

    await _ge_log_raw(ctx,
        f"❌ DEAL REJECTED: <code>{tid}</code>\n"
        f"By: @{user.username} ({role})\n"
        f"Buyer: {buyer_tag} | Seller: {seller_tag}")

    # Wait 20 sec then delete messages
    await asyncio.sleep(20)

    # Delete the agree/disagree message
    try:
        await q.message.delete()
    except Exception:
        pass

    # Delete countdown message
    try:
        await countdown_msg.delete()
    except Exception:
        pass

    # Delete original lock message too
    locked_msg_id = deal.get("locked_msg_id")
    if locked_msg_id:
        try:
            await ctx.bot.delete_message(
                chat_id=q.message.chat.id,
                message_id=locked_msg_id
            )
        except Exception:
            pass


async def handle_ge_agree(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Handle buyer/seller agree button press."""
    q    = update.callback_query
    user = q.from_user
    data = q.data  # ge_agree:buyer/seller:TID:expected_uname

    try:
        _, role, tid, expected_uname = data.split(":", 3)
    except Exception:
        await q.answer("❌ Error.", show_alert=True)
        return

    deal = _ge_deals.get(tid)
    if not deal:
        await q.answer("❌ Deal nahi mili.", show_alert=True)
        return

    # Check user is the right party
    actual_uname = (user.username or "").lower()
    if actual_uname != expected_uname.lower():
        await q.answer(
            f"⚠️ Yeh button sirf {role.title()} ke liye hai!\n"
            f"Tumhara username match nahi karta.",
            show_alert=True
        )
        return

    if role == "buyer":
        if deal.get("buyer_agreed"):
            await q.answer("✅ Tumne pehle se agree kar liya hai!", show_alert=True)
            return
        deal["buyer_agreed"] = True
        await q.answer("✅ Buyer ne agree kar liya!", show_alert=True)
    elif role == "seller":
        if deal.get("seller_agreed"):
            await q.answer("✅ Tumne pehle se agree kar liya hai!", show_alert=True)
            return
        deal["seller_agreed"] = True
        await q.answer("✅ Seller ne agree kar liya!", show_alert=True)

    buyer_tag  = f"@{deal['buyer_uname']}"
    seller_tag = f"@{deal['seller_uname']}"
    b_status = "✅ Agreed" if deal.get("buyer_agreed")  else "⏳ Pending"
    s_status = "✅ Agreed" if deal.get("seller_agreed") else "⏳ Pending"

    # Update agree button message
    new_kb = InlineKeyboardMarkup([[
        InlineKeyboardButton(
            f"{'✅' if deal.get('buyer_agreed') else '⏳'} Buyer",
            callback_data=f"ge_agree:buyer:{tid}:{deal['buyer_uname']}"
        ),
        InlineKeyboardButton(
            f"{'✅' if deal.get('seller_agreed') else '⏳'} Seller",
            callback_data=f"ge_agree:seller:{tid}:{deal['seller_uname']}"
        ),
    ]])

    try:
        await q.edit_message_reply_markup(reply_markup=new_kb)
    except Exception:
        pass

    # Dono agree ho gaye
    if deal.get("buyer_agreed") and deal.get("seller_agreed"):
        deal["status"] = "ACTIVE"
        try:
            await q.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        await ctx.bot.send_message(
            chat_id=deal["group_id"],
            text=(
                f"{PE_DEAL} <b>Deal Active!</b> · <code>{tid}</code>\n"
                f"✅ {buyer_tag} · ✅ {seller_tag}\n\n"
                f"{PE_LIGHTNING} Ab admin UPI pe payment karo."
            ),
            parse_mode="HTML"
        )
        # Notify admin
        admin_msg = (
            f"🔔 <b>DEAL ACTIVE — Dono Party Agreed!</b>\n\n"
            f"🆔 <code>{tid}</code>\n"
            f"🛒 Buyer: {buyer_tag}\n"
            f"🏪 Seller: {seller_tag}\n"
            f"💰 Amount: {deal.get('amount','—')}\n\n"
            f"📌 Ab <code>/pay {tid} UPI_NAME</code> se payment QR bhejo."
        )
        try:
            await ctx.bot.send_message(
                chat_id=deal["escrower_id"],
                text=admin_msg,
                parse_mode="HTML"
            )
        except Exception:
            pass
        await _ge_log(ctx, deal, "✅ BOTH PARTIES AGREED — DEAL ACTIVE")
    else:
        # Notify group who agreed, who pending
        await ctx.bot.send_message(
            chat_id=deal["group_id"],
            text=(
                f"📋 <b>Agreement Status</b> — <code>{tid}</code>\n\n"
                f"🛒 Buyer {buyer_tag}: {b_status}\n"
                f"🏪 Seller {seller_tag}: {s_status}\n\n"
                f"⏳ Dono agree karne ke baad deal active hogi."
            ),
            parse_mode="HTML"
        )

async def _ge_log(ctx, deal: dict, action: str, extra: str = ""):
    """Send log to log group for group escrow actions."""
    if not state.log_group_id:
        return
    tid  = deal.get("tid", "—")
    ft   = deal.get("form_type", "—")
    amt  = deal.get("amount", "—")
    buyer= deal.get("buyer", "—")
    seller=deal.get("seller", "—")
    escr = deal.get("escrower_username", "—")
    text = (
        f"📋 <b>GROUP ESCROW LOG</b>\n\n"
        f"🔔 {action}\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"🆔 <code>{tid}</code>  📂 {ft}\n"
        f"💰 Amount: {amt}\n"
        f"🛒 Buyer: {buyer}  🏪 Seller: {seller}\n"
        f"👨‍⚖️ Escrower: @{escr}\n"
        f"⏰ {ist_now()}"
    )
    if extra:
        text += f"\n\n📌 {extra}"
    try:
        await ctx.bot.send_message(chat_id=state.log_group_id, text=text, parse_mode="HTML")
    except Exception as e:
        logger.warning(f"GE log error: {e}")

def _resolve_ge_deal(update: Update) -> dict | None:
    """Get the GE deal from a reply or from context (latest locked deal in group)."""
    msg = update.message
    chat_id = update.effective_chat.id
    if msg and msg.reply_to_message:
        rmid = msg.reply_to_message.message_id
        ref  = _ge_msg_to_deal.get(rmid)
        if ref:
            return _ge_deals.get(ref["tid"])
    # Try to find by args (Trade ID)
    if update.message and update.message.text:
        parts = update.message.text.split()
        for p in parts:
            if p.upper().startswith(GE_TRADE_PREFIX):
                return _ge_deals.get(p.upper())
    return None

def _can_control_deal(user_id: int, deal: dict) -> bool:
    """Check if user is the current escrower of this deal."""
    return deal.get("escrower_id") == user_id or is_main_admin(user_id)

# ── /pay Command ───────────────────────────────────────────

async def cmd_ge_pay(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/pay UPI_NAME  or  /pay FEE% UPI_NAME — Generate payment QR."""
    user = update.effective_user
    chat = update.effective_chat
    if not is_admin(user.id):
        await update.message.reply_text("❌ Admin only.")
        return

    deal = _resolve_ge_deal(update)
    if not deal:
        await update.message.reply_text(
            "❌ No deal found.\n\nReply to a locked deal message, or specify Trade ID.\n"
            "Usage: <code>/pay UPI_NAME</code> or <code>/pay FEE% UPI_NAME</code>",
            parse_mode="HTML"
        )
        return

    if not _can_control_deal(user.id, deal):
        await update.message.reply_text(
            f"❌ Only @{deal.get('escrower_username')} can control this deal.", parse_mode="HTML")
        return

    if deal.get("status") in ("CLOSED", "CANCELLED"):
        await update.message.reply_text("❌ Deal is already closed/cancelled.")
        return

    args = ctx.args
    fee_pct  = None  # Will be auto-determined from bio
    upi_name = None

    if not args:
        await update.message.reply_text(
            "Usage:\n"
            "<code>/pay UPI_NAME</code> — Auto fee (bio check)\n"
            "<code>/pay 2 UPI_NAME</code> — Custom 2% fee",
            parse_mode="HTML"
        )
        return

    if len(args) == 1:
        upi_name = args[0].lower()
    elif len(args) >= 2:
        try:
            fee_pct  = float(args[0].replace("%", ""))
            upi_name = args[1].lower()
        except ValueError:
            upi_name = args[0].lower()

    methods = getattr(state, "upi_methods", {})
    if upi_name not in methods:
        avail = ", ".join(f"<code>{k}</code>" for k in methods) or "none"
        await update.message.reply_text(
            f"❌ UPI <code>{upi_name}</code> not found.\n\nAvailable: {avail}",
            parse_mode="HTML"
        )
        return

    upi_data = methods[upi_name]
    upi_id   = upi_data["upi_id"]

    # ── BIO CHECK + AUTO FEE SELECTION ──
    buyer_uname  = deal.get("buyer_uname",  deal.get("buyer",  "")).lstrip("@")
    seller_uname = deal.get("seller_uname", deal.get("seller", "")).lstrip("@")
    buyer_has_bio  = False
    seller_has_bio = False
    bio_tag = getattr(state, "required_bio", None)

    if fee_pct is None:
        # Use stored bio values from deal (set at lock time) — no extra API call
        buyer_has_bio  = deal.get("buyer_has_bio",  False)
        seller_has_bio = deal.get("seller_has_bio", False)

        if bio_tag:
            if buyer_has_bio and seller_has_bio:
                fee_pct = get_deal_fee("ge", True)
                bio_status = f"🏷 Bio: Both ✅ → {fee_pct}% (bio rate)"
            else:
                fee_pct = get_deal_fee("ge", False)
                bio_status = f"🏷 Bio: {'Partial' if (buyer_has_bio or seller_has_bio) else 'None'} ❌ → {fee_pct}% (standard)"
        else:
            fee_pct = get_deal_fee("ge", False)
            bio_status = f"📊 Fee: {fee_pct}% (standard)"
    else:
        buyer_has_bio  = deal.get("buyer_has_bio",  False)
        seller_has_bio = deal.get("seller_has_bio", False)
        bio_status = f"📊 Custom Fee: {fee_pct}%"
    
    # Store bio status in deal
    deal["buyer_has_bio"]  = buyer_has_bio
    deal["seller_has_bio"] = seller_has_bio

    raw_amt = parse_amount_smart(str(deal.get("amount", "0"))) or 0.0

    if fee_pct > 0:
        fee_amt   = round(raw_amt * fee_pct / 100, 2)
        final_amt = round(raw_amt + fee_amt, 2)
    else:
        fee_amt   = 0.0
        final_amt = raw_amt

    tid    = deal["tid"]
    buyer  = deal.get("buyer", "—")
    seller = deal.get("seller", "—")
    escr   = deal.get("escrower_username", "—")
    ft     = deal.get("form_type", "NORMAL")
    type_label = {"NORMAL": "Normal Deal", "BET": "Bet Deal", "THIRD_PARTY": "Third Party Deal", "SERVICE": "Service Deal"}.get(ft, ft)

    try:
        qr_bytes_data = generate_upi_qr(upi_id, final_amt, upi_name.upper())
    except Exception as e:
        await update.message.reply_text(f"❌ QR generation failed: {e}")
        return

    if fee_pct > 0:
        caption = (
            f"💳 <b>PAYMENT QR</b> (Fee Included)\n\n"
            f"━━━━━━━━━━━━━━━━━━━\n"
            f"🆔 Trade ID: <code>{tid}</code>\n"
            f"📂 Type: {type_label}\n"
            f"━━━━━━━━━━━━━━━━━━━\n\n"
            f"💰 Deal Amount: ₹{raw_amt:.2f}\n"
            f"💸 Fee ({fee_pct}%): ₹{fee_amt:.2f}\n"
            f"✅ Total Payable: <b>₹{final_amt:.2f}</b>\n\n"
            f"🏷 UPI ID: <code>{upi_id}</code>\n\n"
            f"🛒 Buyer: {buyer}\n"
            f"🏪 Seller: {seller}\n"
            f"👨‍⚖️ Escrower: @{escr}\n"
            f"⏰ {ist_now()}"
        )
    else:
        caption = (
            f"💳 <b>PAYMENT QR</b>\n\n"
            f"━━━━━━━━━━━━━━━━━━━\n"
            f"🆔 Trade ID: <code>{tid}</code>\n"
            f"📂 Type: {type_label}\n"
            f"━━━━━━━━━━━━━━━━━━━\n\n"
            f"💰 Amount: <b>₹{final_amt:.2f}</b>\n\n"
            f"🏷 UPI ID: <code>{upi_id}</code>\n\n"
            f"🛒 Buyer: {buyer}\n"
            f"🏪 Seller: {seller}\n"
            f"👨‍⚖️ Escrower: @{escr}\n"
            f"⏰ {ist_now()}"
        )

    await ctx.bot.send_photo(
        chat_id=chat.id,
        photo=InputFile(io.BytesIO(qr_bytes_data), filename="payment_qr.png"),
        caption=caption,
        parse_mode="HTML"
    )

    await _ge_log(ctx, deal, "💳 PAYMENT QR SENT",
        f"UPI: {upi_id}\nAmount: ₹{final_amt:.2f}"
        + (f" (includes {fee_pct}% fee)" if fee_pct > 0 else ""))

# ── /add Command ───────────────────────────────────────────

async def cmd_ge_add(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/add [TID] AMOUNT — Confirm received amount. TID optional if replying to deal."""
    user = update.effective_user
    chat = update.effective_chat
    if not is_admin(user.id):
        await update.message.reply_text("❌ Admin only.")
        return

    deal = _resolve_ge_deal(update)
    if not deal:
        await update.message.reply_text(
            "❌ No deal found. Reply to locked deal message or give Trade ID.\n"
            "Usage: <code>/add TID AMOUNT</code>\n"
            "Example: <code>/add GE-2051649A 1050</code>", parse_mode="HTML"
        )
        return

    if not _can_control_deal(user.id, deal):
        await update.message.reply_text(
            f"❌ Only @{deal.get('escrower_username')} can control this deal.", parse_mode="HTML")
        return

    if deal.get("added"):
        await update.message.reply_text(
            f"⚠️ Deal already added!\n🆔 <code>{deal['tid']}</code>\n"
            f"💰 Received: ₹{deal.get('received_amount','—')}",
            parse_mode="HTML"
        )
        return

    if deal.get("status") in ("CLOSED", "CANCELLED"):
        await update.message.reply_text("❌ Deal is closed/cancelled.")
        return

    # ── Parse amount — skip Trade ID arg if present ──
    args = ctx.args or []
    amount_str = None
    for a in args:
        if a.upper().startswith(GE_TRADE_PREFIX):
            continue  # ye Trade ID hai, skip
        amount_str = a
        break

    if not amount_str:
        await update.message.reply_text(
            "❌ Amount do.\n"
            "Usage: <code>/add TID AMOUNT</code>\n"
            "Example: <code>/add GE-2051649A 1050</code>", parse_mode="HTML")
        return

    try:
        recv_amt = float(_re.sub(r"[^\d.]", "", amount_str))
        if recv_amt <= 0:
            raise ValueError
    except Exception:
        await update.message.reply_text(
            "❌ Invalid amount.\n"
            "Example: <code>/add GE-2051649A 1050</code>", parse_mode="HTML")
        return

    deal["added"]           = True
    deal["received_amount"] = recv_amt
    deal["status"]          = "ACTIVE"
    deal["added_at"]        = datetime.utcnow().isoformat()
    deal["added_by"]        = user.username or str(user.id)

    tid    = deal["tid"]
    buyer  = deal.get("buyer", "—")
    seller = deal.get("seller", "—")
    escr   = deal.get("escrower_username", "—")
    ft     = deal.get("form_type", "NORMAL")
    type_label = {"NORMAL": "Normal Deal", "BET": "Bet Deal", "THIRD_PARTY": "Third Party Deal", "SERVICE": "Service Deal"}.get(ft, ft)
    type_emoji = {"NORMAL": "🔹", "BET": "🎯", "THIRD_PARTY": "🤝", "SERVICE": "🛠️"}.get(ft, "🔹")

    await update.message.reply_text(
        f"✅ <b>DEAL CREATED</b>\n\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"{type_emoji} Type: <b>{type_label}</b>\n"
        f"🆔 Trade ID: <code>{tid}</code>\n"
        f"━━━━━━━━━━━━━━━━━━━\n\n"
        f"💰 Received: <b>₹{recv_amt:.2f}</b>\n"
        f"🛒 Buyer: {buyer}\n"
        f"🏪 Seller: {seller}\n"
        f"👨‍⚖️ Escrower: @{escr}\n"
        f"📊 Status: <b>ACTIVE</b>\n"
        f"⏰ {ist_now()}",
        parse_mode="HTML"
    )

    await _ge_log(ctx, deal, "✅ DEAL CREATED / AMOUNT RECEIVED",
        f"Received: ₹{recv_amt:.2f}\nAdded by: @{user.username or user.id}")

# ── /close Command ─────────────────────────────────────────

async def cmd_ge_close(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/close AMOUNT — Release amount and complete deal."""
    user = update.effective_user
    chat = update.effective_chat
    if not is_admin(user.id):
        await update.message.reply_text("❌ Admin only.")
        return

    deal = _resolve_ge_deal(update)
    if not deal:
        await update.message.reply_text(
            "❌ No deal found. Reply to locked deal message.\n"
            "Usage: <code>/close AMOUNT</code>", parse_mode="HTML"
        )
        return

    if not _can_control_deal(user.id, deal):
        await update.message.reply_text(
            f"❌ Only @{deal.get('escrower_username')} can control this deal.", parse_mode="HTML")
        return

    if deal.get("closed"):
        await update.message.reply_text(
            f"⚠️ Deal already closed!\n🆔 <code>{deal['tid']}</code>", parse_mode="HTML")
        return

    if deal.get("status") == "CANCELLED":
        await update.message.reply_text("❌ Deal is cancelled.")
        return

    # ── Parse amount — skip Trade ID arg if present ──
    args = ctx.args or []
    amount_str = None
    for a in args:
        if a.upper().startswith(GE_TRADE_PREFIX):
            continue
        amount_str = a
        break

    if not amount_str:
        await update.message.reply_text(
            "Usage: <code>/close TID AMOUNT</code>\n"
            "Example: <code>/close GE-2051649A 1050</code>", parse_mode="HTML")
        return

    try:
        release_amt = float(_re.sub(r"[^\d.]", "", amount_str))
        if release_amt <= 0:
            raise ValueError
    except Exception:
        await update.message.reply_text(
            "❌ Invalid amount.\n"
            "Example: <code>/close GE-2051649A 1050</code>", parse_mode="HTML")
        return

    deal["closed"]          = True
    deal["status"]          = "CLOSED"
    deal["released_amount"] = release_amt
    deal["closed_at"]       = datetime.utcnow().isoformat()
    deal["closed_by"]       = user.username or str(user.id)

    tid    = deal["tid"]
    buyer_uname  = deal.get("buyer_uname",  deal.get("buyer",  "—")).lstrip("@")
    seller_uname = deal.get("seller_uname", deal.get("seller", "—")).lstrip("@")
    buyer_tag    = f"@{buyer_uname}"
    seller_tag   = f"@{seller_uname}"
    escr   = deal.get("escrower_username", "—")
    ft     = deal.get("form_type", "NORMAL")
    amt    = deal.get("amount", "—")
    type_label = {"NORMAL": "Normal Deal", "BET": "Bet Deal", "THIRD_PARTY": "Third Party Deal", "SERVICE": "Service Deal"}.get(ft, ft)
    type_emoji = {"NORMAL": "🔹", "BET": "🎯", "THIRD_PARTY": "🤝", "SERVICE": "🛠️"}.get(ft, "🔹")
    closed_time = ist_now()

    # ── Update stats ──
    _ge_all_stats["total_deals"]  = _ge_all_stats.get("total_deals", 0) + 1
    _ge_all_stats["total_volume"] = _ge_all_stats.get("total_volume", 0.0) + release_amt
    _ge_all_stats["completed"]    = _ge_all_stats.get("completed", 0) + 1
    today = datetime.utcnow().strftime("%Y-%m-%d")
    if _ge_daily_stats.get("date") != today:
        _ge_daily_stats.update({"date": today, "deals": 0, "volume": 0.0, "completed": 0, "cancelled": 0})
    _ge_daily_stats["deals"]     = _ge_daily_stats.get("deals", 0) + 1
    _ge_daily_stats["volume"]    = _ge_daily_stats.get("volume", 0.0) + release_amt
    _ge_daily_stats["completed"] = _ge_daily_stats.get("completed", 0) + 1

    # ── Group completion message ──
    await update.message.reply_text(
        f"{PE_SPARK} <b>DEAL COMPLETED!</b>\n"
        f"━━━━━━━━━━━━━━\n"
        f"{type_emoji} <b>{type_label}</b> · 🪪 <code>{tid}</code>\n"
        f"━━━━━━━━━━━━━━\n"
        f"💸 Released: <b>₹{release_amt:.2f}</b>\n"
        f"🛒 {buyer_tag} · 🏪 {seller_tag}\n"
        f"👨‍⚖️ @{escr} · ⏰ {closed_time}\n\n"
        f"{PE_CONFETTI} Rate karo neeche!",
        parse_mode="HTML"
    )

    # ── Auto Receipt to buyer and seller ──
    receipt = (
        f"🧾 <b>DEAL RECEIPT</b>\n\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"{type_emoji} Type: <b>{type_label}</b>\n"
        f"🆔 Trade ID: <code>{tid}</code>\n"
        f"━━━━━━━━━━━━━━━━━━━\n\n"
        f"💰 Deal Amount: <b>{amt}</b>\n"
        f"💸 Released: <b>₹{release_amt:.2f}</b>\n\n"
        f"🛒 Buyer: {buyer_tag}\n"
        f"🏪 Seller: {seller_tag}\n"
        f"👨‍⚖️ Escrower: @{escr}\n\n"
        f"📅 Completed: {closed_time}\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"✅ Ye receipt apne paas screenshot karke rakh lo.\n"
        f"Koi issue ho to Trade ID share karo admin ke saath."
    )
    for uname in [buyer_uname, seller_uname]:
        if uname and uname != "—":
            try:
                chat_obj = await ctx.bot.get_chat(f"@{uname}")
                await ctx.bot.send_message(chat_id=chat_obj.id, text=receipt, parse_mode="HTML")
            except Exception:
                pass

    # ── Rating buttons ──
    rating_kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("⭐ Rate Buyer",    callback_data=f"ge_rate:buyer:{tid}:{buyer_uname}"),
            InlineKeyboardButton("⭐ Rate Seller",   callback_data=f"ge_rate:seller:{tid}:{seller_uname}"),
        ],
        [
            InlineKeyboardButton("⭐ Rate Escrower", callback_data=f"ge_rate:escrower:{tid}:{escr}"),
        ]
    ])
    await ctx.bot.send_message(
        chat_id=chat.id,
        text=(
            f"{PE_STAR} <b>Rate karo!</b> · <code>{tid}</code>\n\n"
            f"🛒 Buyer · 🏪 Seller · 👨‍⚖️ Escrower\n"
            f"Sabko rate karo — vouch group mein save hogi."
        ),
        parse_mode="HTML",
        reply_markup=rating_kb
    )

    await _ge_log(ctx, deal, "✅ DEAL COMPLETED",
        f"Released: ₹{release_amt:.2f}\nClosed by: @{user.username or user.id}")

    # ── Update user stats ──
    for uname in [buyer_uname, seller_uname]:
        if uname and uname != "—":
            ukey = uname.lower()
            if ukey not in _user_stats:
                _user_stats[ukey] = {"total_volume": 0, "deals": 0, "highest_deal": 0, "rank": "Bronze"}
            _user_stats[ukey]["total_volume"] += release_amt
            _user_stats[ukey]["deals"] += 1
            _user_stats[ukey]["highest_deal"] = max(_user_stats[ukey]["highest_deal"], release_amt)


# ══════════════════════════════════════════════════════════
# BLACKLIST SYSTEM
# ══════════════════════════════════════════════════════════

async def cmd_blacklist(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/blacklist @user REASON — Add to blacklist
    /blacklist remove @user — Remove
    /blacklist list — Show all"""
    user = update.effective_user
    if not is_admin(user.id):
        return
    args = ctx.args or []

    if not args:
        bl_count = len(_ge_blacklist)
        await update.message.reply_text(
            f"🚫 <b>Blacklist System</b>\n\n"
            f"Total blacklisted: <b>{bl_count}</b>\n\n"
            f"Commands:\n"
            f"<code>/blacklist @user REASON</code> — Add\n"
            f"<code>/blacklist remove @user</code> — Remove\n"
            f"<code>/blacklist list</code> — Show all",
            parse_mode="HTML"
        )
        return

    if args[0].lower() == "list":
        if not _ge_blacklist:
            await update.message.reply_text("✅ Blacklist empty hai. Koi banned nahi.")
            return
        lines = ["🚫 <b>BLACKLISTED USERS</b>\n"]
        for uname, data in _ge_blacklist.items():
            lines.append(
                f"• @{uname}\n"
                f"  Reason: {data.get('reason','—')}\n"
                f"  By: @{data.get('by','?')} | {data.get('at','?')[:10]}"
            )
        await update.message.reply_text("\n".join(lines), parse_mode="HTML")
        return

    if args[0].lower() == "remove":
        if len(args) < 2:
            await update.message.reply_text("Usage: <code>/blacklist remove @username</code>", parse_mode="HTML")
            return
        uname = args[1].lstrip("@").lower()
        if uname in _ge_blacklist:
            del _ge_blacklist[uname]
            await update.message.reply_text(f"✅ @{uname} blacklist se remove kar diya.")
        else:
            await update.message.reply_text(f"❌ @{uname} blacklist mein nahi hai.")
        return

    # Add to blacklist
    uname  = args[0].lstrip("@").lower()
    reason = " ".join(args[1:]) if len(args) > 1 else "No reason given"
    _ge_blacklist[uname] = {
        "reason": reason,
        "by": user.username or str(user.id),
        "at": datetime.utcnow().isoformat()
    }
    await update.message.reply_text(
        f"🚫 <b>Blacklisted!</b>\n\n"
        f"👤 @{uname}\n"
        f"📌 Reason: {reason}\n"
        f"👮 By: @{user.username}\n\n"
        f"Ab ye user kisi bhi deal mein participate nahi kar sakta.",
        parse_mode="HTML"
    )
    await _ge_log_raw(ctx,
        f"🚫 BLACKLISTED: @{uname}\nReason: {reason}\nBy: @{user.username}")


async def _ge_log_raw(ctx, text: str):
    if not state.log_group_id:
        return
    try:
        await ctx.bot.send_message(chat_id=state.log_group_id, text=text, parse_mode="HTML")
    except Exception:
        pass


# ══════════════════════════════════════════════════════════
# DEAL TIMEOUT SYSTEM
# ══════════════════════════════════════════════════════════

async def ge_timeout_watcher(ctx, tid: str, chat_id: int, hours: int):
    """Watch deal and auto-cancel if payment not received in time."""
    await asyncio.sleep(hours * 3600)
    deal = _ge_deals.get(tid)
    if not deal:
        return
    if deal.get("status") in ("CLOSED", "CANCELLED"):
        return
    if deal.get("added"):  # Payment received
        return

    # Warning at halfway point already sent — now cancel
    deal["status"]      = "CANCELLED"
    deal["cancelled_at"] = datetime.utcnow().isoformat()
    deal["cancel_reason"] = f"Auto-cancelled: no payment in {hours}h"

    _ge_all_stats["cancelled"] = _ge_all_stats.get("cancelled", 0) + 1
    _ge_daily_stats["cancelled"] = _ge_daily_stats.get("cancelled", 0) + 1

    buyer_uname  = deal.get("buyer_uname",  deal.get("buyer",  "")).lstrip("@")
    seller_uname = deal.get("seller_uname", deal.get("seller", "")).lstrip("@")
    buyer_tag  = f"@{buyer_uname}"  if buyer_uname  else "Buyer"
    seller_tag = f"@{seller_uname}" if seller_uname else "Seller"

    try:
        await ctx.bot.send_message(
            chat_id=chat_id,
            text=(
                f"⏰ <b>DEAL AUTO-CANCELLED</b>\n\n"
                f"━━━━━━━━━━━━━━━━━━━\n"
                f"🆔 <code>{tid}</code>\n"
                f"━━━━━━━━━━━━━━━━━━━\n\n"
                f"❌ {hours} ghante mein payment confirm nahi hua.\n"
                f"Deal automatically cancel ho gayi.\n\n"
                f"👥 {buyer_tag} {seller_tag}\n\n"
                f"⚠️ Agar galti se hua to admin se contact karo.\n"
                f"Naya deal karna ho to form dobara bhejo."
            ),
            parse_mode="HTML"
        )
    except Exception:
        pass
    await _ge_log_raw(ctx, f"⏰ AUTO-CANCELLED: <code>{tid}</code>\nReason: No payment in {hours}h")


async def ge_payment_reminder(ctx, tid: str, chat_id: int):
    """Send payment reminder at 2hrs, 5hrs, then auto-cancel if no payment."""
    deal = _ge_deals.get(tid, {})
    buyer_uname = deal.get("buyer_uname", deal.get("buyer", "")).lstrip("@")
    buyer_tag   = f"@{buyer_uname}" if buyer_uname else "Buyer"

    # Reminder at 2 hours
    await asyncio.sleep(2 * 3600)
    d = _ge_deals.get(tid, {})
    if d.get("status") in ("CLOSED", "CANCELLED") or d.get("added"):
        return
    try:
        await ctx.bot.send_message(
            chat_id=chat_id,
            text=(
                f"⏰ <b>PAYMENT REMINDER</b>\n\n"
                f"🪪 <code>{tid}</code>\n"
                f"👉 {buyer_tag}\n\n"
                f"⚠️ 2 hours ho gaye — payment confirm karo!\n"
                f"Jaldi karo warna deal cancel ho jayegi."
            ),
            parse_mode="HTML"
        )
    except:
        pass

    # Reminder at 5 hours
    await asyncio.sleep(3 * 3600)  # 3 more hours = total 5hrs
    d = _ge_deals.get(tid, {})
    if d.get("status") in ("CLOSED", "CANCELLED") or d.get("added"):
        return
    try:
        await ctx.bot.send_message(
            chat_id=chat_id,
            text=(
                f"🚨 <b>FINAL WARNING!</b>\n\n"
                f"🪪 <code>{tid}</code>\n"
                f"👉 {buyer_tag}\n\n"
                f"⚠️ 5 hours! Payment nahi hua to deal CANCEL hogi."
            ),
            parse_mode="HTML"
        )
    except:
        pass

    # Auto-cancel after 5hrs if still no payment
    await asyncio.sleep(1 * 3600)  # 1 more hour = total 6hrs
    d = _ge_deals.get(tid, {})
    if d.get("status") in ("CLOSED", "CANCELLED") or d.get("added"):
        return
    
    # Cancel deal
    d["status"] = "CANCELLED"
    d["cancel_reason"] = "Auto-cancelled: No payment after 6 hours"
    try:
        await ctx.bot.send_message(
            chat_id=chat_id,
            text=(
                f"❌ <b>DEAL CANCELLED!</b>\n\n"
                f"🪪 <code>{tid}</code>\n\n"
                f"Payment nahi hua 6 hours mein.\n"
                f"Deal automatically cancel ho gayi."
            ),
            parse_mode="HTML"
        )
    except:
        pass


async def cmd_settimeout(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/settimeout HOURS — Set deal timeout (default 24h)"""
    global _ge_deal_timeout_hrs
    if not is_admin(update.effective_user.id):
        return
    if not ctx.args:
        await update.message.reply_text(
            f"⏰ Current timeout: <b>{_ge_deal_timeout_hrs} hours</b>\n\n"
            f"Usage: <code>/settimeout 12</code>",
            parse_mode="HTML"
        )
        return
    try:
        hrs = int(ctx.args[0])
        if not (1 <= hrs <= 168):
            raise ValueError
        _ge_deal_timeout_hrs = hrs
        await update.message.reply_text(
            f"✅ Deal timeout set: <b>{hrs} hours</b>\n\n"
            f"Ab har naya deal {hrs}h mein payment nahi hua to auto-cancel hoga.",
            parse_mode="HTML"
        )
    except ValueError:
        await update.message.reply_text("❌ 1-168 ke beech number do. Example: <code>/settimeout 24</code>", parse_mode="HTML")


# ══════════════════════════════════════════════════════════
# CALC COMMAND — Fee Calculator
# ══════════════════════════════════════════════════════════

async def cmd_calc(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/calc AMOUNT or /calc FEE% AMOUNT — Fee calculator"""
    args = ctx.args or []
    if not args:
        await update.message.reply_text(
            f"💰 <b>Fee Calculator</b>\n\n"
            f"Usage:\n"
            f"<code>calc 5000</code> — Default fee\n"
            f"<code>calc 2% 5000</code> — Custom fee\n\n"
            f"Current fee: <b>{state.fee_percent}%</b>",
            parse_mode="HTML"
        )
        return

    import re as _re2
    custom_fee = None
    amount_str = None

    # Check if first arg is fee% like "2%" or "2"
    if len(args) >= 2:
        fee_match = _re2.match(r'^(\d+\.?\d*)%?$', args[0])
        if fee_match:
            custom_fee = float(fee_match.group(1))
            amount_str = args[1]
        else:
            amount_str = args[0]
    else:
        amount_str = args[0]

    try:
        amount = float(_re2.sub(r"[^\d.]", "", amount_str))
        if amount <= 0:
            raise ValueError
    except Exception:
        await update.message.reply_text(
            "❌ Valid amount do.\n"
            "Example: <code>calc 5000</code> ya <code>calc 2% 5000</code>",
            parse_mode="HTML"
        )
        return

    fee_pct  = custom_fee if custom_fee is not None else state.fee_percent
    fee_amt  = round(amount * fee_pct / 100, 2)
    total    = round(amount + fee_amt, 2)
    bio_disc = getattr(state, "bio_discount_percent", 0.0)
    bio_tag  = getattr(state, "required_bio", None)

    text = (
        f"💰 <b>Fee Calculator</b>\n"
        f"━━━━━━━━━━━━━━\n"
        f"💵 Amount:    <b>₹{amount:,.2f}</b>\n"
        f"📊 Fee ({fee_pct}%): <b>₹{fee_amt:,.2f}</b>\n"
        f"💸 Total:     <b>₹{total:,.2f}</b>\n"
        f"━━━━━━━━━━━━━━"
    )
    if bio_tag and bio_disc > 0 and custom_fee is None:
        disc_fee   = round(amount * bio_disc / 100, 2)
        disc_total = round(amount + disc_fee, 2)
        text += (
            f"\n\n🏷 With Bio (<code>{bio_tag}</code>):\n"
            f"📊 Fee ({bio_disc}%): <b>₹{disc_fee:,.2f}</b>\n"
            f"💸 Total: <b>₹{disc_total:,.2f}</b>"
        )
    await update.message.reply_text(text, parse_mode="HTML")


# ══════════════════════════════════════════════════════════
# DEAL STATUS — /mystatus TID
# ══════════════════════════════════════════════════════════

async def cmd_mystatus(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/mystatus TID — Check deal status (anyone can use)"""
    args = ctx.args or []
    if not args:
        await update.message.reply_text(
            "Usage: <code>/mystatus TID</code>\n"
            "Example: <code>/mystatus GE-XXXXXXXX</code>",
            parse_mode="HTML"
        )
        return
    tid  = args[0].upper().strip()
    deal = _ge_deals.get(tid)
    if not deal:
        await update.message.reply_text(
            f"❌ Deal <code>{tid}</code> nahi mili.\n\n"
            f"📌 Trade ID sahi hai? Lock message se copy karo.",
            parse_mode="HTML"
        )
        return

    ft         = deal.get("form_type", "NORMAL")
    status     = deal.get("status", "—")
    buyer_tag  = f"@{deal.get('buyer_uname',  deal.get('buyer',  '—')).lstrip('@')}"
    seller_tag = f"@{deal.get('seller_uname', deal.get('seller', '—')).lstrip('@')}"
    amt        = deal.get("amount", "—")
    escr       = deal.get("escrower_username", "—")
    locked_at  = deal.get("locked_at", "—")[:16].replace("T"," ") + " UTC" if deal.get("locked_at") else "—"
    closed_at  = deal.get("closed_at", "")[:16].replace("T"," ") + " UTC" if deal.get("closed_at") else "—"
    recv_amt   = deal.get("received_amount")
    rel_amt    = deal.get("released_amount")

    type_emoji = {"NORMAL": "🔹", "BET": "🎯", "THIRD_PARTY": "🤝", "SERVICE": "🛠️"}.get(ft, "🔹")
    type_label = {"NORMAL": "Normal Deal", "BET": "Bet Deal", "THIRD_PARTY": "Third Party Deal", "SERVICE": "Service Deal"}.get(ft, ft)

    status_emoji = {
        "LOCKED": "🔒", "ACTIVE": "🟢", "CLOSED": "✅", "CANCELLED": "❌"
    }.get(status, "❓")

    text = (
        f"📋 <b>DEAL STATUS</b>\n\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"{type_emoji} Type: <b>{type_label}</b>\n"
        f"🆔 Trade ID: <code>{tid}</code>\n"
        f"━━━━━━━━━━━━━━━━━━━\n\n"
        f"{status_emoji} Status: <b>{status}</b>\n"
        f"💰 Amount: <b>{amt}</b>\n"
        f"🛒 Buyer: {buyer_tag}\n"
        f"🏪 Seller: {seller_tag}\n"
        f"👨‍⚖️ Escrower: @{escr}\n\n"
        f"📅 Locked: {locked_at}\n"
    )
    if recv_amt:
        text += f"💵 Received: ₹{recv_amt:.2f}\n"
    if rel_amt:
        text += f"💸 Released: ₹{rel_amt:.2f}\n"
    if closed_at != "—":
        text += f"✅ Closed: {closed_at}\n"

    # Status-based guidance
    text += "\n━━━━━━━━━━━━━━━━━━━\n"
    if status == "LOCKED":
        text += "⏳ Deal locked hai. Dono parties agree karo aur admin ko UPI pe payment karo."
    elif status == "ACTIVE":
        text += "🟢 Deal active hai. Payment pending hai — admin confirm karega."
    elif status == "CLOSED":
        text += "✅ Deal successfully complete hua!"
    elif status == "CANCELLED":
        text += f"❌ Deal cancel hua.\nReason: {deal.get('cancel_reason', 'Admin cancelled')}"

    await update.message.reply_text(text, parse_mode="HTML")


# ══════════════════════════════════════════════════════════
# DUPLICATE DEAL DETECTION
# ══════════════════════════════════════════════════════════

def _check_duplicate_deal(buyer_uname: str, seller_uname: str) -> dict | None:
    """Check if active deal exists between same buyer and seller."""
    b = buyer_uname.lower().lstrip("@")
    s = seller_uname.lower().lstrip("@")
    for tid, deal in _ge_deals.items():
        if deal.get("status") in ("CLOSED", "CANCELLED"):
            continue
        db = deal.get("buyer_uname", deal.get("buyer", "")).lower().lstrip("@")
        ds = deal.get("seller_uname", deal.get("seller", "")).lower().lstrip("@")
        if (db == b and ds == s) or (db == s and ds == b):
            return deal
    return None


# ══════════════════════════════════════════════════════════
# ADMIN AVAILABILITY
# ══════════════════════════════════════════════════════════

_admin_availability: dict[int, dict] = {}  # admin_id -> {status, message, until}

async def cmd_available(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/available — Mark yourself as available"""
    user = update.effective_user
    if not is_admin(user.id):
        return
    _admin_availability[user.id] = {
        "status": "available",
        "message": " ".join(ctx.args) if ctx.args else "Available for deals",
        "since": datetime.utcnow().isoformat()
    }
    await update.message.reply_text(
        f"✅ <b>@{user.username}</b> — ab Available!\n\n"
        f"Users ko pata chalega tum online ho.",
        parse_mode="HTML"
    )

async def cmd_busy(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/busy MESSAGE — Mark yourself as busy"""
    user = update.effective_user
    if not is_admin(user.id):
        return
    msg = " ".join(ctx.args) if ctx.args else "Busy — thodi der mein wapas aaunga"
    _admin_availability[user.id] = {
        "status": "busy",
        "message": msg,
        "since": datetime.utcnow().isoformat()
    }
    await update.message.reply_text(
        f"🔴 <b>@{user.username}</b> — Busy set!\n\n"
        f"Message: {msg}",
        parse_mode="HTML"
    )

async def cmd_admin_status_public(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/adminstatus — Check admin availability (anyone can use)"""
    all_admins = [MAIN_ADMIN_ID] + list(state.sub_admins)
    lines = ["👨‍⚖️ <b>ADMIN STATUS</b>\n"]
    for aid in all_admins:
        avail = _admin_availability.get(aid, {})
        status = avail.get("status", "unknown")
        msg    = avail.get("message", "")
        emoji  = "🟢" if status == "available" else ("🔴" if status == "busy" else "⚪")
        try:
            chat_obj = await ctx.bot.get_chat(aid)
            uname    = f"@{chat_obj.username}" if chat_obj.username else str(aid)
        except Exception:
            uname = str(aid)
        line = f"{emoji} {uname}"
        if msg:
            line += f" — {msg}"
        lines.append(line)
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


# ══════════════════════════════════════════════════════════
# ENHANCED STATS + DAILY SUMMARY
# ══════════════════════════════════════════════════════════

async def cmd_ge_stats_full(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/stats — Full stats dashboard (admin only)"""
    if not is_admin(update.effective_user.id):
        # Non-admin can see limited stats
        total = _ge_all_stats.get("total_deals", 0)
        comp  = _ge_all_stats.get("completed", 0)
        vol   = _ge_all_stats.get("total_volume", 0.0)
        await update.message.reply_text(
            f"📊 <b>Baba Escrow Stats</b>\n\n"
            f"✅ Total Deals: <b>{total}</b>\n"
            f"💰 Total Volume: <b>₹{vol:,.2f}</b>\n"
            f"🎯 Completed: <b>{comp}</b>",
            parse_mode="HTML"
        )
        return

    today = datetime.utcnow().strftime("%Y-%m-%d")
    if _ge_daily_stats.get("date") != today:
        _ge_daily_stats.update({"date": today, "deals": 0, "volume": 0.0, "completed": 0, "cancelled": 0})

    active    = sum(1 for d in _ge_deals.values() if d.get("status") == "ACTIVE")
    locked    = sum(1 for d in _ge_deals.values() if d.get("status") == "LOCKED")
    cancelled = sum(1 for d in _ge_deals.values() if d.get("status") == "CANCELLED")
    bl_count  = len(_ge_blacklist)

    await update.message.reply_text(
        f"📊 <b>STATS DASHBOARD</b>\n\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"📅 <b>Aaj ({today}):</b>\n"
        f"🆕 New Deals: {_ge_daily_stats.get('deals', 0)}\n"
        f"✅ Completed: {_ge_daily_stats.get('completed', 0)}\n"
        f"❌ Cancelled: {_ge_daily_stats.get('cancelled', 0)}\n"
        f"💰 Volume: ₹{_ge_daily_stats.get('volume', 0.0):,.2f}\n\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"📈 <b>All Time:</b>\n"
        f"🔢 Total Deals: {_ge_all_stats.get('total_deals', 0)}\n"
        f"✅ Completed: {_ge_all_stats.get('completed', 0)}\n"
        f"❌ Cancelled: {_ge_all_stats.get('cancelled', 0)}\n"
        f"💰 Total Volume: ₹{_ge_all_stats.get('total_volume', 0.0):,.2f}\n\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"🔴 <b>Right Now:</b>\n"
        f"🔒 Locked (pending agree): {locked}\n"
        f"🟢 Active (pending payment): {active}\n"
        f"🚫 Blacklisted Users: {bl_count}\n"
        f"⏰ Timeout: {_ge_deal_timeout_hrs}h",
        parse_mode="HTML"
    )


async def ge_daily_summary(ctx):
    """Send daily summary to admin at end of day."""
    while True:
        now = datetime.utcnow()
        # Wait until 22:00 IST = 16:30 UTC
        target = now.replace(hour=16, minute=30, second=0, microsecond=0)
        if now >= target:
            target = target + timedelta(days=1)
        await asyncio.sleep((target - now).total_seconds())

        today = datetime.utcnow().strftime("%Y-%m-%d")
        if _ge_daily_stats.get("date") != today:
            continue  # No deals today

        summary = (
            f"🌙 <b>DAILY SUMMARY — {today}</b>\n\n"
            f"━━━━━━━━━━━━━━━━━━━\n"
            f"🆕 New Deals: <b>{_ge_daily_stats.get('deals', 0)}</b>\n"
            f"✅ Completed: <b>{_ge_daily_stats.get('completed', 0)}</b>\n"
            f"❌ Cancelled: <b>{_ge_daily_stats.get('cancelled', 0)}</b>\n"
            f"💰 Volume: <b>₹{_ge_daily_stats.get('volume', 0.0):,.2f}</b>\n\n"
            f"━━━━━━━━━━━━━━━━━━━\n"
            f"📈 All Time Volume: ₹{_ge_all_stats.get('total_volume', 0.0):,.2f}\n"
            f"🔢 All Time Deals: {_ge_all_stats.get('total_deals', 0)}\n\n"
            f"Good night! 🌙"
        )
        for aid in [MAIN_ADMIN_ID] + list(state.sub_admins):
            try:
                await ctx.bot.send_message(chat_id=aid, text=summary, parse_mode="HTML")
            except Exception:
                pass

        _ge_daily_stats.update({"date": "", "deals": 0, "volume": 0.0, "completed": 0, "cancelled": 0})


# ══════════════════════════════════════════════════════════
# RATING SYSTEM
# ══════════════════════════════════════════════════════════

_ge_ratings: dict[str, list] = {}  # username -> [{"stars": 5, "tid": "GE-X", "by": "uname"}]

async def handle_ge_rate(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Handle rating button press after deal complete."""
    q    = update.callback_query
    user = q.from_user
    data = q.data  # ge_rate:buyer/seller:TID:uname

    try:
        _, role, tid, target_uname = data.split(":", 3)
    except Exception:
        await q.answer("❌ Error.", show_alert=True)
        return

    deal = _ge_deals.get(tid)
    if not deal or deal.get("status") != "CLOSED":
        await q.answer("❌ Ye deal complete nahi hai.", show_alert=True)
        return

    # Show star buttons
    star_kb = InlineKeyboardMarkup([[
        InlineKeyboardButton(f"{i}⭐", callback_data=f"ge_stars:{i}:{tid}:{target_uname}:{role}")
        for i in range(1, 6)
    ]])
    await q.message.reply_text(
        f"⭐ <b>{role.title()} @{target_uname} ko rate karo:</b>\n\n"
        f"1⭐ = Bahut bura\n"
        f"3⭐ = Theek tha\n"
        f"5⭐ = Excellent!\n\n"
        f"Neeche stars dabao 👇",
        parse_mode="HTML",
        reply_markup=star_kb
    )
    await q.answer()


async def handle_ge_stars(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Handle star rating selection."""
    q    = update.callback_query
    user = q.from_user
    data = q.data  # ge_stars:STARS:TID:target_uname:role

    try:
        _, stars_str, tid, target_uname, role = data.split(":", 4)
        stars = int(stars_str)
    except Exception:
        await q.answer("❌ Error.", show_alert=True)
        return

    deal = _ge_deals.get(tid)
    if not deal:
        await q.answer("❌ Deal nahi mili.", show_alert=True)
        return

    if target_uname not in _ge_ratings:
        _ge_ratings[target_uname] = []
    _ge_ratings[target_uname].append({
        "stars": stars, "tid": tid,
        "by": user.username or str(user.id),
        "at": datetime.utcnow().isoformat()[:10]
    })

    await q.answer(f"✅ {stars}⭐ rating di!", show_alert=True)
    await q.edit_message_reply_markup(reply_markup=None)

    star_str = "⭐" * stars
    vouch_text = (
        f"⭐ <b>NEW RATING</b>\n\n"
        f"👤 @{target_uname} ({role.title()})\n"
        f"🌟 Rating: {star_str} ({stars}/5)\n"
        f"🆔 Deal: <code>{tid}</code>\n"
        f"👮 By: @{user.username or user.id}\n"
        f"📅 {datetime.utcnow().strftime('%d %b %Y')}"
    )
    if state.vouch_group_id:
        try:
            await ctx.bot.send_message(
                chat_id=state.vouch_group_id,
                text=vouch_text, parse_mode="HTML"
            )
        except Exception:
            pass
    await ctx.bot.send_message(
        chat_id=deal["group_id"],
        text=f"✅ @{target_uname} ko {star_str} rating mili!\n🆔 <code>{tid}</code>",
        parse_mode="HTML"
    )


async def cmd_reputation(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/rep @username — Check reputation"""
    args = ctx.args or []
    if not args:
        uname = (update.effective_user.username or "").lower()
    else:
        uname = args[0].lstrip("@").lower()

    if not uname:
        await update.message.reply_text("Usage: <code>/rep @username</code>", parse_mode="HTML")
        return

    ratings = _ge_ratings.get(uname, [])
    deals_as_buyer  = sum(1 for d in _ge_deals.values() if d.get("buyer_uname","").lower() == uname and d.get("status") == "CLOSED")
    deals_as_seller = sum(1 for d in _ge_deals.values() if d.get("seller_uname","").lower() == uname and d.get("status") == "CLOSED")
    is_blacklisted  = uname in _ge_blacklist

    if not ratings and not deals_as_buyer and not deals_as_seller:
        await update.message.reply_text(
            f"👤 @{uname}\n\n❓ Koi data nahi mila. Pehli deal karke reputation banao!",
            parse_mode="HTML"
        )
        return

    avg_stars = sum(r["stars"] for r in ratings) / len(ratings) if ratings else 0
    star_str  = "⭐" * round(avg_stars) if avg_stars else "—"

    bl_line = f"\n🚫 <b>BLACKLISTED</b>: {_ge_blacklist[uname]['reason']}" if is_blacklisted else ""

    await update.message.reply_text(
        f"👤 <b>@{uname} — Reputation</b>\n\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"⭐ Rating: {star_str} ({avg_stars:.1f}/5)\n"
        f"📊 Total Ratings: {len(ratings)}\n"
        f"🛒 Deals as Buyer: {deals_as_buyer}\n"
        f"🏪 Deals as Seller: {deals_as_seller}\n"
        f"✅ Total Completed: {deals_as_buyer + deals_as_seller}"
        + bl_line,
        parse_mode="HTML"
    )


# ══════════════════════════════════════════════════════════
# /SUMMARY — COMMAND REFERENCE
# ══════════════════════════════════════════════════════════

async def cmd_summary(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/summary — Full command reference for users and admins"""
    user = update.effective_user
    chat = update.effective_chat

    user_cmds = (
        f"📋 <b>USER COMMANDS</b>\n\n"
        f"━━━━━━━━━━━━━━━━━━━\n\n"
        f"📝 <b>Forms (bina slash ke):</b>\n"
        f"<code>form</code>      — Normal Deal form\n"
        f"<code>form2</code>     — Bet Deal form\n"
        f"<code>form3</code>     — Third Party form\n"
        f"<code>form4</code>     — Service Deal form\n"
        f"<code>formtype</code>  — Sabke details\n\n"
        f"💰 <b>Calculator:</b>\n"
        f"<code>calc 5000</code> — Fee calculate karo\n\n"
        f"📊 <b>Deal Info:</b>\n"
        f"<code>/mystatus TID</code>  — Apna deal status dekho\n"
        f"<code>/rep @user</code>     — Kisi ki reputation dekho\n\n"
        f"👨‍⚖️ <b>Admin Info:</b>\n"
        f"<code>/adminstatus</code>   — Admin online hai ya nahi\n\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"📌 <b>Deal Flow:</b>\n"
        f"1️⃣ Form copy karo, fill karo, bhejo\n"
        f"2️⃣ Bot blank fields check karega\n"
        f"3️⃣ Admin BOTH AGREE → Deal lock\n"
        f"4️⃣ Agree buttons dabao (buyer + seller)\n"
        f"5️⃣ Admin UPI pe payment karo\n"
        f"6️⃣ Admin release kare → Confirm karo\n"
        f"7️⃣ Deal complete → Receipt aayegi ✅"
    )

    admin_cmds = (
        f"\n\n━━━━━━━━━━━━━━━━━━━\n"
        f"👑 <b>ADMIN COMMANDS</b>\n\n"
        f"⚙️ <b>Setup:</b>\n"
        f"<code>/setescrowgroup</code>    — Group set karo\n"
        f"<code>/saveupi NAME ID</code>   — UPI save karo\n"
        f"<code>/setchannel LINK</code>   — Channel set karo\n"
        f"<code>/settimeout 24</code>     — Deal timeout set karo\n\n"
        f"💼 <b>Deal Management:</b>\n"
        f"<code>/pay TID UPI</code>       — Payment QR bhejo\n"
        f"<code>/add TID AMOUNT</code>    — Payment received\n"
        f"<code>/close TID AMOUNT</code>  — Deal complete\n"
        f"<code>/cancel TID</code>        — Deal cancel\n"
        f"<code>/transfer TID @admin</code> — Deal transfer\n\n"
        f"📊 <b>Info:</b>\n"
        f"<code>/stats</code>             — Full dashboard\n"
        f"<code>/myhold</code>            — Apne active deals\n"
        f"<code>/allhold</code>           — Sab deals\n"
        f"<code>/find @user</code>        — User ke deals\n\n"
        f"🚫 <b>Moderation:</b>\n"
        f"<code>/blacklist @user REASON</code> — Ban karo\n"
        f"<code>/blacklist remove @user</code> — Unban\n"
        f"<code>/blacklist list</code>         — List dekho\n\n"
        f"🟢 <b>Availability:</b>\n"
        f"<code>/available</code>         — Online mark karo\n"
        f"<code>/busy MESSAGE</code>      — Busy mark karo"
    )

    if is_admin(user.id):
        await update.message.reply_text(user_cmds + admin_cmds, parse_mode="HTML")
    else:
        await update.message.reply_text(user_cmds, parse_mode="HTML")

# ── /transfer Command ──────────────────────────────────────

async def cmd_ge_transfer(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/transfer @admin — Transfer deal to another admin."""
    user = update.effective_user
    if not is_admin(user.id):
        await update.message.reply_text("❌ Admin only.")
        return

    deal = _resolve_ge_deal(update)
    if not deal:
        await update.message.reply_text(
            "❌ No deal found. Reply to locked deal.\n"
            "Usage: <code>/transfer @admin</code>", parse_mode="HTML"
        )
        return

    if not _can_control_deal(user.id, deal):
        await update.message.reply_text(
            f"❌ Only @{deal.get('escrower_username')} can transfer this deal.", parse_mode="HTML")
        return

    if deal.get("status") in ("CLOSED", "CANCELLED"):
        await update.message.reply_text("❌ Deal is closed/cancelled.")
        return

    if not ctx.args:
        await update.message.reply_text("Usage: <code>/transfer @newadmin</code>", parse_mode="HTML")
        return

    new_admin_mention = ctx.args[0].lstrip("@").strip()

    # Try to find the new admin in sub_admins by username
    new_admin_id  = None
    new_admin_uname = new_admin_mention
    for uid in [MAIN_ADMIN_ID] + list(state.sub_admins):
        try:
            chat_obj = await ctx.bot.get_chat(uid)
            if (chat_obj.username or "").lower() == new_admin_mention.lower():
                new_admin_id    = uid
                new_admin_uname = chat_obj.username or str(uid)
                break
        except Exception:
            pass

    if not new_admin_id:
        await update.message.reply_text(
            f"❌ Admin @{new_admin_mention} not found in admin list.\n"
            f"Make sure they are added as admin first.", parse_mode="HTML"
        )
        return

    old_escrower_id    = deal["escrower_id"]
    old_escrower_uname = deal["escrower_username"]

    # Update deal
    deal["escrower_id"]       = new_admin_id
    deal["escrower_username"] = new_admin_uname
    deal["transfer_history"].append({
        "from": old_escrower_uname,
        "to":   new_admin_uname,
        "at":   datetime.utcnow().isoformat(),
    })

    # Update hold lists
    if old_escrower_id in _ge_admin_holds:
        _ge_admin_holds[old_escrower_id] = [t for t in _ge_admin_holds[old_escrower_id] if t != deal["tid"]]
    if new_admin_id not in _ge_admin_holds:
        _ge_admin_holds[new_admin_id] = []
    _ge_admin_holds[new_admin_id].append(deal["tid"])

    tid = deal["tid"]
    await update.message.reply_text(
        f"🔄 <b>DEAL TRANSFERRED</b>\n\n"
        f"🆔 Trade ID: <code>{tid}</code>\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"📤 Old Escrower: @{old_escrower_uname}\n"
        f"📥 New Escrower: @{new_admin_uname}\n"
        f"⏰ {ist_now()}\n\n"
        f"@{new_admin_uname} now has full control.",
        parse_mode="HTML"
    )

    await _ge_log(ctx, deal, "🔄 DEAL TRANSFERRED",
        f"From: @{old_escrower_uname} → To: @{new_admin_uname}")

# ── /canceldeal for GE ─────────────────────────────────────

async def cmd_ge_canceldeal(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/canceldeal — Cancel active GE deal (escrower only)."""
    user = update.effective_user
    if not is_admin(user.id):
        return

    deal = _resolve_ge_deal(update)
    if not deal or not deal.get("tid", "").startswith(GE_TRADE_PREFIX):
        return  # Let the original canceldeal handle non-GE deals

    if not _can_control_deal(user.id, deal):
        await update.message.reply_text(
            f"❌ Only @{deal.get('escrower_username')} can cancel this deal.", parse_mode="HTML")
        return

    if deal.get("status") in ("CLOSED", "CANCELLED"):
        await update.message.reply_text("❌ Already closed/cancelled.")
        return

    deal["status"]      = "CANCELLED"
    deal["cancelled_at"] = datetime.utcnow().isoformat()
    deal["cancelled_by"] = user.username or str(user.id)

    await update.message.reply_text(
        f"🚫 <b>DEAL CANCELLED</b>\n\n"
        f"🆔 <code>{deal['tid']}</code>\n"
        f"👨‍⚖️ By: @{user.username or user.id}\n"
        f"⏰ {ist_now()}",
        parse_mode="HTML"
    )
    await _ge_log(ctx, deal, "🚫 DEAL CANCELLED",
        f"Cancelled by: @{user.username or user.id}")

# ── /myhold & /allhold ─────────────────────────────────────

async def cmd_myhold(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/myhold — Show your active/live holdings."""
    user = update.effective_user
    if not is_admin(user.id):
        return

    my_tids = _ge_admin_holds.get(user.id, [])
    active  = [_ge_deals[t] for t in my_tids if t in _ge_deals and _ge_deals[t].get("status") not in ("CLOSED", "CANCELLED")]

    if not active:
        await update.message.reply_text(
            f"📊 <b>Your Holdings</b>\n\n"
            f"@{user.username or user.id}: No active deals.",
            parse_mode="HTML"
        )
        return

    total_hold = 0.0
    lines = [f"📊 <b>Your Holdings — @{user.username or user.id}</b>\n", "━━━━━━━━━━━━━━━━━━━"]
    for deal in active:
        amt_str = deal.get("received_amount") or deal.get("amount", "0")
        try:
            amt = float(_re.sub(r"[^\d.]", "", str(amt_str)))
        except Exception:
            amt = 0.0
        total_hold += amt
        ft = deal.get("form_type", "NORMAL")
        type_emoji = {"NORMAL": "🔹", "BET": "🎯", "THIRD_PARTY": "🤝", "SERVICE": "🛠️"}.get(ft, "🔹")
        lines.append(
            f"\n{type_emoji} <code>{deal['tid']}</code>  |  {deal.get('status','—')}\n"
            f"   💰 ₹{amt:.2f}  🛒 {deal.get('buyer','—')} ↔ {deal.get('seller','—')}"
        )

    lines.append(f"\n━━━━━━━━━━━━━━━━━━━")
    lines.append(f"💼 <b>Total Live Holding: ₹{total_hold:.2f}</b>")
    lines.append(f"📦 Active Deals: {len(active)}")

    await update.message.reply_text("\n".join(lines), parse_mode="HTML")

async def cmd_allhold(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/allhold — Show all escrowers' active holdings."""
    if not is_admin(update.effective_user.id):
        return

    all_admins_with_holds = {}
    for deal in _ge_deals.values():
        if deal.get("status") in ("CLOSED", "CANCELLED"):
            continue
        escr_uname = deal.get("escrower_username", "unknown")
        amt_str = deal.get("received_amount") or deal.get("amount", "0")
        try:
            amt = float(_re.sub(r"[^\d.]", "", str(amt_str)))
        except Exception:
            amt = 0.0
        if escr_uname not in all_admins_with_holds:
            all_admins_with_holds[escr_uname] = {"total": 0.0, "count": 0}
        all_admins_with_holds[escr_uname]["total"] += amt
        all_admins_with_holds[escr_uname]["count"]  += 1

    if not all_admins_with_holds:
        await update.message.reply_text("📊 <b>All Holdings</b>\n\nNo active deals right now.", parse_mode="HTML")
        return

    lines = ["📊 <b>ALL ESCROWER HOLDINGS</b>\n", "━━━━━━━━━━━━━━━━━━━"]
    grand_total = 0.0
    for uname, data in all_admins_with_holds.items():
        lines.append(f"\n👨‍⚖️ @{uname}\n   💰 ₹{data['total']:.2f}  |  {data['count']} active deal(s)")
        grand_total += data["total"]
    lines.append(f"\n━━━━━━━━━━━━━━━━━━━")
    lines.append(f"💼 <b>Grand Total: ₹{grand_total:.2f}</b>")

    await update.message.reply_text("\n".join(lines), parse_mode="HTML")

# ── /dd for GE (deal details) ──────────────────────────────

async def cmd_ge_dd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/dd TRADEID — Full deal details for GE deals."""
    if not ctx.args:
        return  # Let original /dd handle
    tid = ctx.args[0].upper()
    if not tid.startswith(GE_TRADE_PREFIX):
        return  # Let original /dd handle
    deal = _ge_deals.get(tid)
    if not deal:
        await update.message.reply_text(f"❌ GE Deal not found: <code>{tid}</code>", parse_mode="HTML")
        return

    ft     = deal.get("form_type", "NORMAL")
    type_emoji = {"NORMAL": "🔹", "BET": "🎯", "THIRD_PARTY": "🤝", "SERVICE": "🛠️"}.get(ft, "🔹")
    type_label = {"NORMAL": "Normal Deal", "BET": "Bet Deal", "THIRD_PARTY": "Third Party Deal", "SERVICE": "Service Deal"}.get(ft, ft)

    transfers = deal.get("transfer_history", [])
    transfer_lines = ""
    if transfers:
        transfer_lines = "\n\n🔄 <b>Transfer History:</b>"
        for t in transfers:
            at = t.get("at","")[:19].replace("T"," ") + " UTC"
            transfer_lines += f"\n  @{t['from']} → @{t['to']}  ({at})"

    details = ge_deal_summary(deal)
    extra = ""
    if ft == "NORMAL":
        extra = f"\n⏱ Max Time: {deal.get('max_time','—')}\n💳 Payment: {deal.get('payment_method','—')}"
    elif ft == "BET":
        extra = (f"\n🎮 Game: {deal.get('game_name','—')}\n"
                 f"⚠️ P1 Loss: {deal.get('p1_loss','—')}\n"
                 f"⚠️ P2 Loss: {deal.get('p2_loss','—')}\n"
                 f"⏱ Max Time: {deal.get('max_time','—')}")
    elif ft == "THIRD_PARTY":
        extra = (f"\n🔸 TP Role: {deal.get('tp_role','—')}\n"
                 f"💰 TP Charges: {deal.get('tp_charges','—')}\n"
                 f"🔨 Work: {deal.get('work_details','—')}\n"
                 f"⏱ Max Time: {deal.get('max_time','—')}")
    elif ft == "SERVICE":
        extra = (f"\n🔨 Work: {deal.get('work_details','—')}\n"
                 f"⏱ Completion: {deal.get('completion_time','—')}\n"
                 f"📋 Proof: {deal.get('proof_of_work','—')}")

    recv = f"\n💰 Received: ₹{deal['received_amount']:.2f}" if deal.get("received_amount") else ""
    released = f"\n💸 Released: ₹{deal['released_amount']:.2f}" if deal.get("released_amount") else ""
    closed_at = f"\n✅ Closed: {deal['closed_at'][:19].replace('T',' ')} UTC" if deal.get("closed_at") else ""

    await update.message.reply_text(
        f"{details}{extra}{recv}{released}{closed_at}{transfer_lines}",
        parse_mode="HTML"
    )

# ── /search ────────────────────────────────────────────────

async def cmd_ge_search(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/search USERNAME — Search user's deal history."""
    if not is_admin(update.effective_user.id):
        return
    if not ctx.args:
        await update.message.reply_text("Usage: <code>/search USERNAME</code>", parse_mode="HTML")
        return

    query = ctx.args[0].lstrip("@").lower().strip()
    matched = []
    total_vol = 0.0

    for deal in _ge_deals.values():
        buyer  = (deal.get("buyer") or "").lower().lstrip("@")
        seller = (deal.get("seller") or "").lower().lstrip("@")
        p1     = (deal.get("buyer") or "").lower().lstrip("@")  # BET: P1
        p2     = (deal.get("seller") or "").lower().lstrip("@")
        if query in buyer or query in seller or query in p1 or query in p2:
            matched.append(deal)
            try:
                amt = float(_re.sub(r"[^\d.]", "", str(deal.get("received_amount") or deal.get("amount", "0"))))
                total_vol += amt
            except Exception:
                pass

    active    = sum(1 for d in matched if d.get("status") not in ("CLOSED", "CANCELLED"))
    completed = sum(1 for d in matched if d.get("status") == "CLOSED")

    if not matched:
        await update.message.reply_text(
            f"❌ No deals found for <code>@{query}</code>", parse_mode="HTML")
        return

    # Simple rank by volume
    rank = "🥇 High Volume" if total_vol > 10000 else "🥈 Medium" if total_vol > 1000 else "🥉 Regular"

    lines = [
        f"🔍 <b>User Search: @{query}</b>",
        f"━━━━━━━━━━━━━━━━━━━",
        f"📦 Total Deals: <b>{len(matched)}</b>",
        f"🟢 Active: <b>{active}</b>",
        f"✅ Completed: <b>{completed}</b>",
        f"💰 Total Volume: <b>₹{total_vol:.2f}</b>",
        f"🏅 Rank: {rank}",
        f"━━━━━━━━━━━━━━━━━━━",
    ]
    for deal in matched[-5:]:  # last 5
        ft = deal.get("form_type","—")
        lines.append(f"• <code>{deal['tid']}</code>  [{ft}]  {deal.get('status','—')}")

    await update.message.reply_text("\n".join(lines), parse_mode="HTML")

# ── /stats ─────────────────────────────────────────────────

async def cmd_ge_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/stats — Group escrow statistics."""
    if not is_admin(update.effective_user.id):
        return
    total   = len(_ge_deals)
    active  = sum(1 for d in _ge_deals.values() if d.get("status") not in ("CLOSED","CANCELLED"))
    closed  = sum(1 for d in _ge_deals.values() if d.get("status") == "CLOSED")
    cancelled = sum(1 for d in _ge_deals.values() if d.get("status") == "CANCELLED")
    total_vol = 0.0
    for d in _ge_deals.values():
        if d.get("status") == "CLOSED":
            try:
                total_vol += float(_re.sub(r"[^\d.]", "", str(d.get("released_amount") or d.get("amount","0"))))
            except Exception:
                pass

    form_counts = {}
    for d in _ge_deals.values():
        ft = d.get("form_type","UNKNOWN")
        form_counts[ft] = form_counts.get(ft, 0) + 1

    lines = [
        "📊 <b>GROUP ESCROW STATS</b>",
        "━━━━━━━━━━━━━━━━━━━",
        f"📦 Total Deals: <b>{total}</b>",
        f"🟢 Active: <b>{active}</b>",
        f"✅ Closed: <b>{closed}</b>",
        f"🚫 Cancelled: <b>{cancelled}</b>",
        f"💰 Total Volume Cleared: <b>₹{total_vol:.2f}</b>",
        f"━━━━━━━━━━━━━━━━━━━",
        f"📋 By Form Type:",
    ]
    for ft, cnt in form_counts.items():
        emoji = {"NORMAL":"🔹","BET":"🎯","THIRD_PARTY":"🤝","SERVICE":"🛠️"}.get(ft,"•")
        lines.append(f"  {emoji} {ft}: {cnt}")

    await update.message.reply_text("\n".join(lines), parse_mode="HTML")

# ── /escrowers ─────────────────────────────────────────────

async def cmd_ge_escrowers(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/escrowers — List all admins with their deal counts."""
    if not is_admin(update.effective_user.id):
        return
    lines = ["👨‍⚖️ <b>ESCROWERS LIST</b>", "━━━━━━━━━━━━━━━━━━━"]
    lines.append(f"👑 Main: <code>{MAIN_ADMIN_ID}</code>")
    for uid in state.sub_admins:
        active_count = len([t for t in _ge_admin_holds.get(uid,[])
                            if t in _ge_deals and _ge_deals[t].get("status") not in ("CLOSED","CANCELLED")])
        try:
            chat_obj = await ctx.bot.get_chat(uid)
            uname = f"@{chat_obj.username}" if chat_obj.username else str(uid)
        except Exception:
            uname = str(uid)
        lines.append(f"👨‍⚖️ {uname} — {active_count} active deal(s)")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")

# ── /help ──────────────────────────────────────────────────

async def cmd_ge_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/help — Show all commands."""
    is_adm = is_admin(update.effective_user.id)
    user_cmds = (
        "📖 <b>ESCROW BOT HELP</b>\n\n"
        "━━━━━━━━━━━━━━━━━━━\n"
        "🔹 <b>Deal Forms:</b>\n"
        "<code>/form</code>  — Normal Deal Form\n"
        "<code>/form2</code> — Bet Deal Form\n"
        "<code>/form3</code> — Third Party Form\n"
        "<code>/form4</code> — Service Deal Form\n"
        "<code>/formtype</code> — Explain all form types\n\n"
        "━━━━━━━━━━━━━━━━━━━\n"
        "🔎 <b>Deal Info:</b>\n"
        "<code>/dd TRADEID</code>   — Deal details\n"
        "<code>/search @user</code> — User history\n"
        "<code>/stats</code>        — Group statistics\n"
    )
    admin_cmds = (
        "\n━━━━━━━━━━━━━━━━━━━\n"
        "👨‍⚖️ <b>Admin Commands:</b>\n"
        "<code>/pay UPI_NAME</code>      — Send payment QR\n"
        "<code>/pay FEE% UPI_NAME</code> — With fee\n"
        "<code>/add AMOUNT</code>        — Confirm received\n"
        "<code>/close AMOUNT</code>      — Complete deal\n"
        "<code>/transfer @admin</code>   — Transfer deal\n"
        "<code>/canceldeal</code>        — Cancel deal\n\n"
        "<code>/myhold</code>     — Your active holdings\n"
        "<code>/allhold</code>    — All escrowers\n"
        "<code>/escrowers</code>  — List all admins\n\n"
        "<code>/saveupi NAME ID</code>  — Save UPI\n"
        "<code>/listupi</code>          — List saved UPI\n"
        "<code>/deleteupi NAME</code>   — Remove UPI\n\n"
        "<code>/setescrowgroup</code>   — Set this as escrow group\n\n"
        "━━━━━━━━━━━━━━━━━━━\n"
        "💡 Reply 'BOTH AGREE' on any form to lock a deal!"
    )
    await update.message.reply_text(
        user_cmds + (admin_cmds if is_adm else ""),
        parse_mode="HTML"
    )

# ── /form commands ─────────────────────────────────────────

FORM1_TEXT = (
    "🔹 𝗘𝗦𝗖𝗥𝗢𝗪 𝗗𝗘𝗔𝗟 𝗙𝗢𝗥𝗠 𝗢𝗙 𝗕𝗔𝗕𝗔 𝗘𝗦𝗖𝗥𝗢𝗪\n\n"
    "━━━━━━━━━━━━━━━━━━━\n\n"
    "● Deal Of :-\n"
    "● Total Amount :-\n"
    "● Maximum Time :-\n\n"
    "● Buyer Username :-\n"
    "● Seller Username :-\n\n"
    "● Buyer Bank Name / Payment Method :-\n\n"
    "● Terms & Conditions :-\n\n"
    "━━━━━━━━━━━━━━━━━━\n"
    "⚠️ 𝗡𝗼𝘁𝗲: 𝗦𝗮𝗯 𝗰𝗼𝗻𝗱𝗶𝘁𝗶𝗼𝗻𝘀 𝗰𝗹𝗲𝗮𝗿𝗹𝘆 𝗹𝗶𝗸𝗵𝗼. 𝗕𝗮𝗮𝗱 𝗺𝗲 \"𝘆𝗲 𝗯𝗼𝗹𝗮 𝘁𝗵𝗮 / 𝘄𝗼 𝗯𝗼𝗹𝗮 𝘁𝗵𝗮\" 𝗮𝗰𝗰𝗲𝗽𝘁 𝗻𝗮𝗵𝗶 𝗵𝗼𝗴𝗮.\n"
    "⚠️ 𝗡𝗼𝘁𝗲: 𝗗𝗲𝗮𝗹 𝗰𝗼𝗺𝗽𝗹𝗲𝘁𝗲 𝗵𝗼𝗻𝗲 𝗸𝗲 𝗯𝗮𝗮𝗱 𝗮𝗴𝗮𝗿 𝗸𝗼𝗶 𝗽𝗮𝗿𝘁𝘆 𝗿𝗲𝗹𝗲𝗮𝘀𝗲/𝗿𝗲𝗳𝘂𝗻𝗱 𝗰𝗼𝗻𝗳𝗶𝗿𝗺 𝗻𝗮𝗵𝗶 𝗸𝗮𝗿𝘁𝗶, 𝗲𝘀𝗰𝗿𝗼𝘄𝗲𝗿 𝗽𝗿𝗼𝗼𝗳 𝘃𝗲𝗿𝗶𝗳𝘆 𝗸𝗮𝗿𝗲𝗴𝗮. 𝗨𝘀𝗸𝗲 𝗯𝗮𝗮𝗱 4 𝗵𝗼𝘂𝗿𝘀 𝗸𝗶 𝘄𝗮𝗿𝗻𝗶𝗻𝗴 𝗱𝗶 𝗷𝗮𝘆𝗲𝗴𝗶. 𝗜𝗴𝗻𝗼𝗿𝗲 𝗵𝗼𝗻𝗲 𝗽𝗮𝗿 𝗲𝘀𝗰𝗿𝗼𝘄𝗲𝗿 𝗱𝗶𝗿𝗲𝗰𝘁 𝗿𝗲𝗹𝗲𝗮𝘀𝗲/𝗿𝗲𝗳𝘂𝗻𝗱 𝗸𝗮𝗿 𝗱𝗲𝗴𝗮.\n\n"
    "⚠️ 𝗡𝗼𝘁𝗲:\n"
    "𝗧𝘆𝗽𝗲 \"form\" = Normal Deal\n"
    "𝗧𝘆𝗽𝗲 \"form2\" = Bet Deal\n"
    "𝗧𝘆𝗽𝗲 \"form3\" = Third Party Deal\n"
    "𝗧𝘆𝗽𝗲 \"form4\" = Service Deal\n\n"
    "For more information type \"formtype\""
)

FORM2_TEXT = (
    "🎯 𝗕𝗘𝗧 𝗗𝗘𝗔𝗟 𝗙𝗢𝗥𝗠 𝗢𝗙 𝗕𝗔𝗕𝗔 𝗘𝗦𝗖𝗥𝗢𝗪\n\n"
    "━━━━━━━━━━━━━━━━━━━\n\n"
    "● Bet Type :-\n"
    "● Total Bet Amount :-\n"
    "● Game Name  :-\n\n"
    "● Party 1 Username :-\n"
    "● Party 2 Username :-\n\n"
    "● Party 1 ka loss kis condition mai hoga:-\n\n"
    "● Party 2 ka loss kis condition mai hoga :-\n\n"
    "● Maximum Time :-\n\n"
    "● Terms & Conditions :-\n\n"
    "━━━━━━━━━━━━━━━━━━━\n"
    "⚠️ 𝗡𝗼𝘁𝗲: 𝗟𝗼𝘀𝘀 𝗵𝗼𝗻𝗲 𝗽𝗮𝗿 𝗯𝗶𝗻𝗮 𝗽𝗲𝗿𝗺𝗶𝘀𝘀𝗶𝗼𝗻 𝗲𝘀𝗰𝗿𝗼𝘄𝗲𝗿 𝘄𝗶𝗻𝗻𝗶𝗻𝗴 𝗽𝗮𝗿𝘁𝘆 𝗸𝗼 𝗮𝗺𝗼𝘂𝗻𝘁 𝗿𝗲𝗹𝗲𝗮𝘀𝗲/𝗿𝗲𝗳𝘂𝗻𝗱 𝗸𝗮𝗿 𝘀𝗮𝗸𝘁𝗮 𝗵𝗮𝗶.\n"
    "⚠️ 𝗡𝗼𝘁𝗲: 𝗥𝗲𝘀𝘂𝗹𝘁 𝗱𝗲𝗰𝗶𝗱𝗲 𝗵𝗼𝗻𝗲 𝗸𝗲 𝗯𝗮𝗮𝗱 𝗮𝗴𝗮𝗿 𝗸𝗼𝗶 𝗽𝗮𝗿𝘁𝘆 𝗰𝗼𝗻𝗳𝗶𝗿𝗺 𝗻𝗮𝗵𝗶 𝗸𝗮𝗿𝘁𝗶, 𝗲𝘀𝗰𝗿𝗼𝘄𝗲𝗿 𝗽𝗿𝗼𝗼𝗳 𝘃𝗲𝗿𝗶𝗳𝘆 𝗸𝗮𝗿𝗲𝗴𝗮. 𝗨𝘀𝗸𝗲 𝗯𝗮𝗮𝗱 4 𝗵𝗼𝘂𝗿𝘀 𝗸𝗶 𝘄𝗮𝗿𝗻𝗶𝗻𝗴 𝗱𝗶 𝗷𝗮𝘆𝗲𝗴𝗶. 𝗜𝗴𝗻𝗼𝗿𝗲 𝗵𝗼𝗻𝗲 𝗽𝗮𝗿 𝗱𝗶𝗿𝗲𝗰𝘁 𝗿𝗲𝗹𝗲𝗮𝘀𝗲/𝗿𝗲𝗳𝘂𝗻𝗱 𝗸𝗮𝗿 𝗱𝗶𝘆𝗮 𝗷𝗮𝘆𝗲𝗴𝗮."
)

FORM3_TEXT = (
    "🤝 𝗧𝗛𝗜𝗥𝗗 𝗣𝗔𝗥𝗧𝗬 𝗗𝗘𝗔𝗟 𝗙𝗢𝗥𝗠 𝗢𝗙 𝗕𝗔𝗕𝗔 𝗘𝗦𝗖𝗥𝗢𝗪\n\n"
    "━━━━━━━━━━━━━━━━━━━\n"
    "● Deal Of :-\n"
    "● Total Amount :-\n\n"
    "● Buyer Username :-\n"
    "● Seller Username :-\n"
    "● Third Party Username :-\n\n"
    "● Third Party Role :-\n\n"
    "● Third Party Charges :-\n\n"
    "● Work Details :-\n\n"
    "● Maximum Time :-\n\n"
    "● Terms & Conditions :-\n\n"
    "━━━━━━━━━━━━━━━━━━━\n"
    "⚠️ 𝗡𝗼𝘁𝗲: 𝗧𝗵𝗶𝗿𝗱 𝗽𝗮𝗿𝘁𝘆 𝗸𝗮 𝗿𝗼𝗹𝗲 𝗰𝗿𝘆𝘀𝘁𝗮𝗹 𝗰𝗹𝗲𝗮𝗿 𝗵𝗼𝗻𝗮 𝗰𝗵𝗮𝗵𝗶𝘆𝗲.\n"
    "⚠️ 𝗡𝗼𝘁𝗲: 𝗗𝗲𝗮𝗹 𝗰𝗼𝗺𝗽𝗹𝗲𝘁𝗲 𝗵𝗼𝗻𝗲 𝗸𝗲 𝗯𝗮𝗮𝗱 𝗮𝗴𝗮𝗿 𝗸𝗼𝗶 𝗽𝗮𝗿𝘁𝘆 𝗰𝗼𝗻𝗳𝗶𝗿𝗺 𝗻𝗮𝗵𝗶 𝗸𝗮𝗿𝘁𝗶, 𝗲𝘀𝗰𝗿𝗼𝘄𝗲𝗿 𝗽𝗿𝗼𝗼𝗳 𝘃𝗲𝗿𝗶𝗳𝘆 𝗸𝗮𝗿𝗲𝗴𝗮. 𝗨𝘀𝗸𝗲 𝗯𝗮𝗮𝗱 4 𝗵𝗼𝘂𝗿𝘀 𝗸𝗶 𝘄𝗮𝗿𝗻𝗶𝗻𝗴 𝗱𝗶 𝗷𝗮𝘆𝗲𝗴𝗶. 𝗜𝗴𝗻𝗼𝗿𝗲 𝗵𝗼𝗻𝗲 𝗽𝗮𝗿 𝗲𝘀𝗰𝗿𝗼𝘄𝗲𝗿 𝗱𝗶𝗿𝗲𝗰𝘁 𝗿𝗲𝗹𝗲𝗮𝘀𝗲/𝗿𝗲𝗳𝘂𝗻𝗱 𝗸𝗮𝗿 𝗱𝗲𝗴𝗮."
)

FORM4_TEXT = (
    "🛠️ 𝗦𝗘𝗥𝗩𝗜𝗖𝗘 𝗗𝗘𝗔𝗟 𝗙𝗢𝗥𝗠 𝗢𝗙 𝗕𝗔𝗕𝗔 𝗘𝗦𝗖𝗥𝗢𝗪\n"
    "━━━━━━━━━━━━━━━━━━━\n\n"
    "● Service Type :-\n"
    "(Example: Editing / Promotion / Coding / Boosting etc)\n\n"
    "● Work Details :-\n"
    "(Exactly kya kaam hoga)\n\n"
    "● Total Amount :-\n\n"
    "● Buyer Username :-\n"
    "● Seller Username :-\n\n"
    "● Work Completion Time :-\n\n"
    "● Proof of Work :-\n"
    "(Completion ke baad kya proof diya jayega)\n\n"
    "● Terms & Conditions :-\n\n"
    "━━━━━━━━━━━━━━━━━━━\n\n"
    "⚠️ 𝗡𝗼𝘁𝗲: 𝗞𝗮𝗮𝗺 𝗰𝗼𝗺𝗽𝗹𝗲𝘁𝗲 𝗵𝗼𝗻𝗲 𝗸𝗲 𝗯𝗮𝗮𝗱 𝗮𝗴𝗮𝗿 𝗸𝗼𝗶 𝗽𝗮𝗿𝘁𝘆 𝗿𝗲𝗹𝗲𝗮𝘀𝗲/𝗿𝗲𝗳𝘂𝗻𝗱 𝗰𝗼𝗻𝗳𝗶𝗿𝗺 𝗻𝗮𝗵𝗶 𝗸𝗮𝗿𝘁𝗶, 𝗲𝘀𝗰𝗿𝗼𝘄𝗲𝗿 𝗽𝗿𝗼𝗼𝗳 𝘃𝗲𝗿𝗶𝗳𝘆 𝗸𝗮𝗿𝗲𝗴𝗮. 𝗨𝘀𝗸𝗲 𝗯𝗮𝗮𝗱 4 𝗵𝗼𝘂𝗿𝘀 𝗸𝗶 𝘄𝗮𝗿𝗻𝗶𝗻𝗴 𝗱𝗶 𝗷𝗮𝘆𝗲𝗴𝗶. 𝗜𝗴𝗻𝗼𝗿𝗲 𝗵𝗼𝗻𝗲 𝗽𝗮𝗿 𝗲𝘀𝗰𝗿𝗼𝘄𝗲𝗿 𝗱𝗶𝗿𝗲𝗰𝘁 𝗿𝗲𝗹𝗲𝗮𝘀𝗲/𝗿𝗲𝗳𝘂𝗻𝗱 𝗸𝗮𝗿 𝗱𝗲𝗴𝗮."
)

async def cmd_form1(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(FORM1_TEXT)

async def cmd_form2(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(FORM2_TEXT)

async def cmd_form3(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(FORM3_TEXT)

async def cmd_form4(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(FORM4_TEXT)

async def cmd_formtype(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📋 𝗙𝗢𝗥𝗠 𝗧𝗬𝗣𝗘𝗦\n\n"
        "━━━━━━━━━━━━━━━━━━━\n\n"
        "🔹 form — Normal Deal\n"
        "Regular buyer-seller escrow.\n\n"
        "🎯 form2 — Bet Deal\n"
        "Two parties bet on a game/event.\n\n"
        "🤝 form3 — Third Party Deal\n"
        "Three-way deal with a mediator.\n\n"
        "🛠️ form4 — Service Deal\n"
        "Service provider delivers work.\n\n"
        "━━━━━━━━━━━━━━━━━━━\n"
        "Type form / form2 / form3 / form4 to get blank forms."
    )

# ── Group message handler for BOTH AGREE detection ─────────

async def _ge_handle_release(update: Update, ctx: ContextTypes.DEFAULT_TYPE, deal: dict):
    """Release request — admin ya buyer/seller trigger kar sakte hain."""
    chat   = update.effective_chat
    user   = update.effective_user
    tid    = deal.get("tid", "—")
    buyer_uname  = deal.get("buyer_uname",  deal.get("buyer",  "—")).lstrip("@")
    seller_uname = deal.get("seller_uname", deal.get("seller", "—")).lstrip("@")
    amt    = deal.get("amount", "—")

    buyer_tag  = f"@{buyer_uname}"  if buyer_uname  != "—" else "Buyer"
    seller_tag = f"@{seller_uname}" if seller_uname != "—" else "Seller"

    deal["release_buyer_agreed"]  = False
    deal["release_seller_agreed"] = False
    deal["release_admin_id"]      = deal.get("escrower_id")

    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Confirm (Buyer)",   callback_data=f"ge_rel_agree:buyer:{tid}:{buyer_uname}"),
            InlineKeyboardButton("✅ Confirm (Seller)",  callback_data=f"ge_rel_agree:seller:{tid}:{seller_uname}"),
        ],
        [
            InlineKeyboardButton("❌ Dispute (Buyer)",   callback_data=f"ge_rel_dispute:buyer:{tid}:{buyer_uname}"),
            InlineKeyboardButton("❌ Dispute (Seller)",  callback_data=f"ge_rel_dispute:seller:{tid}:{seller_uname}"),
        ]
    ])

    triggered_by = f"@{user.username}" if user.username else user.first_name
    await ctx.bot.send_message(
        chat_id=chat.id,
        text=(
            f"{PE_RELEASE} <b>Release Request</b> · <code>{tid}</code>\n"
            f"💰 {amt} · 🛒 {buyer_tag} · 🏪 {seller_tag}\n"
            f"👤 Triggered by: {triggered_by}\n\n"
            f"✅ Confirm ya ❌ Dispute dabao 👇\n"
            f"Ya group mein <b>agree</b> / <b>disagree</b> likho."
        ),
        parse_mode="HTML",
        reply_markup=kb
    )

    asyncio.create_task(_release_reminder(ctx, tid, chat.id, buyer_tag, seller_tag))


async def _release_reminder(ctx, tid: str, chat_id: int, buyer_tag: str, seller_tag: str):
    await asyncio.sleep(300)
    d = _ge_deals.get(tid, {})
    if d.get("release_buyer_agreed") and d.get("release_seller_agreed"):
        return
    pending = []
    if not d.get("release_buyer_agreed"):  pending.append(buyer_tag)
    if not d.get("release_seller_agreed"): pending.append(seller_tag)
    if pending:
        try:
            await ctx.bot.send_message(
                chat_id=chat_id,
                text=(
                    f"⏰ <b>Release Reminder</b> · <code>{tid}</code>\n\n"
                    f"{' '.join(pending)} — confirm/dispute nahi kiya abhi tak!\n"
                    f"⚠️ Issue ho to admin se baat karo."
                ),
                parse_mode="HTML"
            )
        except Exception:
            pass


async def handle_ge_rel_dispute(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Buyer/Seller ne release pe dispute kiya."""
    q    = update.callback_query
    user = q.from_user
    data = q.data  # ge_rel_dispute:buyer/seller:TID:expected_uname

    try:
        _, role, tid, expected_uname = data.split(":", 3)
    except Exception:
        await q.answer("❌ Error.", show_alert=True)
        return

    deal = _ge_deals.get(tid)
    if not deal:
        await q.answer("❌ Deal nahi mili.", show_alert=True)
        return

    actual_uname = (user.username or "").lower()
    if actual_uname != expected_uname.lower():
        await q.answer("⚠️ Ye button tumhare liye nahi!", show_alert=True)
        return

    await q.answer("⚠️ Dispute noted! Admin notify ho gaya.", show_alert=True)

    buyer_tag  = f"@{deal.get('buyer_uname',  '').lstrip('@')}"
    seller_tag = f"@{deal.get('seller_uname', '').lstrip('@')}"

    await ctx.bot.send_message(
        chat_id=q.message.chat.id,
        text=(
            f"⚠️ <b>Release Dispute!</b> · <code>{tid}</code>\n"
            f"@{user.username or user.first_name} ({role}) ne dispute kiya!\n\n"
            f"🛒 {buyer_tag} · 🏪 {seller_tag}\n\n"
            f"Admin se baat karo issue resolve karne ke liye.\n"
            f"Jab resolve ho jaye to dobara release karo."
        ),
        parse_mode="HTML"
    )

    admin_id = deal.get("release_admin_id") or deal.get("escrower_id")
    if admin_id:
        try:
            await ctx.bot.send_message(
                chat_id=admin_id,
                text=(
                    f"🚨 <b>Release Dispute!</b>\n"
                    f"🪪 <code>{tid}</code>\n"
                    f"@{user.username or user.first_name} ({role}) disagree kiya!\n\n"
                    f"Group mein ja ke resolve karo."
                ),
                parse_mode="HTML"
            )
        except Exception:
            pass
    await _ge_log_raw(ctx, f"⚠️ RELEASE DISPUTE: <code>{tid}</code>\nBy: @{user.username} ({role})")


async def handle_ge_rel_agree(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Handle buyer/seller release confirmation."""
    q    = update.callback_query
    user = q.from_user
    data = q.data  # ge_rel_agree:buyer/seller:TID:expected_uname

    try:
        _, role, tid, expected_uname = data.split(":", 3)
    except Exception:
        await q.answer("❌ Error.", show_alert=True)
        return

    deal = _ge_deals.get(tid)
    if not deal:
        await q.answer("❌ Deal nahi mili.", show_alert=True)
        return

    actual_uname = (user.username or "").lower()
    if actual_uname != expected_uname.lower():
        await q.answer(
            f"⚠️ Yeh button sirf {role.title()} ke liye hai!",
            show_alert=True
        )
        return

    if role == "buyer":
        if deal.get("release_buyer_agreed"):
            await q.answer("✅ Pehle se confirm kar chuke ho!", show_alert=True)
            return
        deal["release_buyer_agreed"] = True
        await q.answer("✅ Release confirm ho gaya!", show_alert=True)
    else:
        if deal.get("release_seller_agreed"):
            await q.answer("✅ Pehle se confirm kar chuke ho!", show_alert=True)
            return
        deal["release_seller_agreed"] = True
        await q.answer("✅ Release confirm ho gaya!", show_alert=True)

    buyer_tag  = f"@{deal.get('buyer_uname',  deal.get('buyer',  '—')).lstrip('@')}"
    seller_tag = f"@{deal.get('seller_uname', deal.get('seller', '—')).lstrip('@')}"

    b_ok = deal.get("release_buyer_agreed")
    s_ok = deal.get("release_seller_agreed")

    # Update buttons
    new_kb = InlineKeyboardMarkup([[
        InlineKeyboardButton(f"{'✅' if b_ok else '⏳'} Buyer",  callback_data=f"ge_rel_agree:buyer:{tid}:{deal.get('buyer_uname','')}"),
        InlineKeyboardButton(f"{'✅' if s_ok else '⏳'} Seller", callback_data=f"ge_rel_agree:seller:{tid}:{deal.get('seller_uname','')}"),
    ]])
    try:
        await q.edit_message_reply_markup(reply_markup=new_kb)
    except Exception:
        pass

    if b_ok and s_ok:
        try:
            await q.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        # Notify admin
        admin_id = deal.get("release_admin_id") or deal.get("escrower_id")
        await ctx.bot.send_message(
            chat_id=deal["group_id"],
            text=(
                f"⏳ <b>RELEASE IN PROGRESS</b>\n\n"
                f"━━━━━━━━━━━━━━━━━━━\n"
                f"🆔 <code>{tid}</code>\n"
                f"━━━━━━━━━━━━━━━━━━━\n\n"
                f"✅ {buyer_tag} — Confirmed\n"
                f"✅ {seller_tag} — Confirmed\n\n"
                f"🔄 Admin ab amount release kar raha hai...\n"
                f"Thodi der mein update aayega! ⏰"
            ),
            parse_mode="HTML"
        )
        if admin_id:
            try:
                await ctx.bot.send_message(
                    chat_id=admin_id,
                    text=(
                        f"🔔 <b>RELEASE CONFIRMED!</b>\n\n"
                        f"🆔 <code>{tid}</code>\n"
                        f"💰 Amount: {deal.get('amount','—')}\n\n"
                        f"✅ Buyer: Confirmed\n"
                        f"✅ Seller: Confirmed\n\n"
                        f"📌 Ab <code>/close {tid} AMOUNT</code> se deal complete karo."
                    ),
                    parse_mode="HTML"
                )
            except Exception:
                pass
        await _ge_log(ctx, deal, "💸 RELEASE CONFIRMED BY BOTH PARTIES")
    else:
        b_txt = "✅ Confirmed" if b_ok else "⏳ Pending"
        s_txt = "✅ Confirmed" if s_ok else "⏳ Pending"
        await ctx.bot.send_message(
            chat_id=deal["group_id"],
            text=(
                f"📋 <b>Release Status</b> — <code>{tid}</code>\n\n"
                f"🛒 Buyer {buyer_tag}: {b_txt}\n"
                f"🏪 Seller {seller_tag}: {s_txt}\n\n"
                f"⏳ Dono confirm karne ke baad release hogi."
            ),
            parse_mode="HTML"
        )

async def handle_check_username(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Check if user has set username after being muted."""
    q    = update.callback_query
    user = q.from_user
    data = q.data  # check_username:user_id:chat_id

    try:
        _, uid_str, chat_id_str = data.split(":")
        uid     = int(uid_str)
        chat_id = int(chat_id_str)
    except Exception:
        await q.answer("❌ Error.", show_alert=True)
        return

    # Sirf wahi user press kar sakta hai
    if user.id != uid:
        await q.answer("⚠️ Ye button tumhare liye nahi hai!", show_alert=True)
        return

    # Check karo username set hua ya nahi
    try:
        member = await ctx.bot.get_chat_member(chat_id, uid)
        has_username = bool(member.user.username)
    except Exception:
        await q.answer("❌ Check nahi ho saka. Try again.", show_alert=True)
        return

    if not has_username:
        await q.answer(
            "❌ Username abhi bhi set nahi hai!\n"
            "Settings → Edit Profile → Username set karo phir button dabao.",
            show_alert=True
        )
        return

    # Username set hai — UNMUTE karo
    try:
        from telegram import ChatPermissions
        await ctx.bot.restrict_chat_member(
            chat_id=chat_id,
            user_id=uid,
            permissions=ChatPermissions(
                can_send_messages=True,
                can_send_other_messages=True,
                can_add_web_page_previews=True,
                can_send_polls=True,
            )
        )
    except Exception:
        await q.answer("❌ Unmute nahi ho saka. Admin se contact karo.", show_alert=True)
        return

    uname = f"@{member.user.username}"
    await q.answer("✅ Username verified! Ab message kar sakte ho.", show_alert=True)

    try:
        await ctx.bot.send_message(
            chat_id=chat_id,
            text=(
                f"✅ <b>{uname}</b> ka username verify ho gaya!\n"
                f"Ab group mein freely message kar sakte hain. 🎉"
            ),
            parse_mode="HTML"
        )
        await q.message.delete()
    except Exception:
        pass


async def handle_ge_confirm_release(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Buyer/Seller ne confirm kiya — wait message phir release."""
    q    = update.callback_query
    user = q.from_user
    data = q.data  # ge_confirm_release:TID:admin_id

    try:
        _, tid, admin_id_str = data.split(":")
        admin_id = int(admin_id_str)
    except Exception:
        await q.answer("❌ Invalid data.", show_alert=True)
        return

    deal = _ge_deals.get(tid)
    if not deal:
        await q.answer("❌ Deal not found.", show_alert=True)
        return

    await q.answer("✅ Confirmed! Admin release kar raha hai...")

    # Send wait message
    await ctx.bot.send_message(
        chat_id=q.message.chat.id,
        text=(
            f"⏳ <b>RELEASE IN PROGRESS</b>\n\n"
            f"━━━━━━━━━━━━━━━━━━━\n"
            f"🆔 Trade ID: <code>{tid}</code>\n"
            f"━━━━━━━━━━━━━━━━━━━\n\n"
            f"✅ Confirmed by: @{user.username or user.first_name}\n\n"
            f"🔄 Wait karo... Admin amount release kar raha hai.\n"
            f"Thodi der mein update aayega!"
        ),
        parse_mode="HTML"
    )

    # Mark release confirmed in deal
    deal["release_confirmed_by"] = user.username or str(user.id)
    deal["release_confirmed_at"] = datetime.utcnow().isoformat()

    await _ge_log(ctx, deal, "✅ RELEASE CONFIRMED BY PARTY",
        f"Confirmed by: @{user.username or user.id}")


async def ge_group_message_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Group handler — forms, BOTH AGREE, release, calc."""
    msg  = update.message
    user = update.effective_user
    chat = update.effective_chat
    if not msg or not msg.text:
        return

    if getattr(state, "escrow_group_id", None) and chat.id != state.escrow_group_id:
        return

    text = msg.text.strip()
    sender_tag = f"@{user.username}" if user.username else user.first_name
    import re as _re
    text = _re.sub(r'(?<![a-zA-Z@])me(?![a-zA-Z])', sender_tag, text, flags=_re.IGNORECASE)

    norm       = normalize_text(text).upper()
    text_lower = text.lower().strip()

    # ── "agree" / "disagree" text trigger ──
    if text_lower in ("agree", "disagree"):
        uname = (user.username or "").lower()
        is_agreeing = text_lower == "agree"

        # 1. Pehle check karo — koi release pending hai?
        rel_deal = None
        rel_role = None
        for d in reversed(list(_ge_deals.values())):
            if d.get("group_id") != chat.id:
                continue
            if d.get("status") not in ("LOCKED", "ACTIVE"):
                continue
            b = d.get("buyer_uname", d.get("buyer", "")).lower().lstrip("@")
            s = d.get("seller_uname", d.get("seller", "")).lower().lstrip("@")
            # Release pending hai?
            if d.get("release_admin_id") and not (d.get("release_buyer_agreed") and d.get("release_seller_agreed")):
                if uname == b and not d.get("release_buyer_agreed"):
                    rel_deal = d
                    rel_role = "buyer"
                    break
                if uname == s and not d.get("release_seller_agreed"):
                    rel_deal = d
                    rel_role = "seller"
                    break

        if rel_deal:
            tid = rel_deal["tid"]
            buyer_tag2  = f"@{rel_deal.get('buyer_uname', '').lstrip('@')}"
            seller_tag2 = f"@{rel_deal.get('seller_uname', '').lstrip('@')}"

            if is_agreeing:
                rel_deal[f"release_{rel_role}_agreed"] = True
                b_ok = rel_deal.get("release_buyer_agreed")
                s_ok = rel_deal.get("release_seller_agreed")
                await msg.reply_text(
                    f"✅ <b>{rel_role.title()}</b> ne release confirm kiya!\n"
                    f"🪪 <code>{tid}</code>\n\n"
                    f"{'✅' if b_ok else '⏳'} {buyer_tag2} · {'✅' if s_ok else '⏳'} {seller_tag2}",
                    parse_mode="HTML"
                )
                if b_ok and s_ok:
                    rel_deal["status"] = "ACTIVE"
                    admin_id = rel_deal.get("release_admin_id") or rel_deal.get("escrower_id")
                    await ctx.bot.send_message(
                        chat_id=chat.id,
                        text=(
                            f"{PE_RELEASE} <b>Release Confirmed!</b>\n"
                            f"🪪 <code>{tid}</code>\n"
                            f"✅ {buyer_tag2} · ✅ {seller_tag2}\n\n"
                            f"🔄 Admin ab release kar raha hai..."
                        ),
                        parse_mode="HTML"
                    )
                    if admin_id:
                        try:
                            await ctx.bot.send_message(
                                chat_id=admin_id,
                                text=(
                                    f"🔔 <b>Release Confirmed!</b>\n"
                                    f"🪪 <code>{tid}</code>\n"
                                    f"💰 {rel_deal.get('amount','—')}\n\n"
                                    f"<code>/close {tid} AMOUNT</code> se complete karo."
                                ),
                                parse_mode="HTML"
                            )
                        except Exception:
                            pass
            else:
                # Disagree on release
                await msg.reply_text(
                    f"❌ <b>{rel_role.title()}</b> ne release se disagree kiya!\n"
                    f"🪪 <code>{tid}</code>\n\n"
                    f"{buyer_tag2} {seller_tag2}\n"
                    f"⚠️ Admin se baat karo issue resolve karne ke liye.",
                    parse_mode="HTML"
                )
                admin_id = rel_deal.get("release_admin_id") or rel_deal.get("escrower_id")
                if admin_id:
                    try:
                        await ctx.bot.send_message(
                            chat_id=admin_id,
                            text=(
                                f"⚠️ <b>Release Dispute!</b>\n"
                                f"🪪 <code>{tid}</code>\n"
                                f"@{user.username or user.first_name} ({rel_role}) ne disagree kiya!\n\n"
                                f"Group mein ja ke resolve karo."
                            ),
                            parse_mode="HTML"
                        )
                    except Exception:
                        pass
            return

        # 2. Deal agree (not release)
        matched_deal = None
        matched_role = None
        for d in reversed(list(_ge_deals.values())):
            if d.get("group_id") != chat.id:
                continue
            if d.get("status") not in ("LOCKED", "ACTIVE"):
                continue
            b = d.get("buyer_uname", d.get("buyer", "")).lower().lstrip("@")
            s = d.get("seller_uname", d.get("seller", "")).lower().lstrip("@")
            if uname == b and not d.get("buyer_agreed"):
                matched_deal = d
                matched_role = "buyer"
                break
            if uname == s and not d.get("seller_agreed"):
                matched_deal = d
                matched_role = "seller"
                break

        if matched_deal:
            tid = matched_deal["tid"]
            buyer_tag2  = f"@{matched_deal.get('buyer_uname', '').lstrip('@')}"
            seller_tag2 = f"@{matched_deal.get('seller_uname', '').lstrip('@')}"

            if is_agreeing:
                matched_deal[f"{matched_role}_agreed"] = True
                b_ok = matched_deal.get("buyer_agreed")
                s_ok = matched_deal.get("seller_agreed")
                await msg.reply_text(
                    f"✅ <b>{matched_role.title()}</b> @{user.username} ne agree kiya!\n"
                    f"🪪 <code>{tid}</code>\n\n"
                    f"{'✅' if b_ok else '⏳'} {buyer_tag2} · {'✅' if s_ok else '⏳'} {seller_tag2}",
                    parse_mode="HTML"
                )
                if b_ok and s_ok:
                    matched_deal["status"] = "ACTIVE"
                    await ctx.bot.send_message(
                        chat_id=chat.id,
                        text=(
                            f"{PE_DEAL} <b>Deal Active!</b> · <code>{tid}</code>\n"
                            f"✅ {buyer_tag2} · ✅ {seller_tag2}\n\n"
                            f"{PE_LIGHTNING} Ab admin UPI pe payment bhejo."
                        ),
                        parse_mode="HTML"
                    )
                    try:
                        await ctx.bot.send_message(
                            chat_id=matched_deal["escrower_id"],
                            text=(
                                f"🔔 <b>Deal Active!</b> · <code>{tid}</code>\n"
                                f"Dono agreed · 💰 {matched_deal.get('amount','—')}\n"
                                f"<code>/pay {tid} UPI_NAME</code> bhejo."
                            ),
                            parse_mode="HTML"
                        )
                    except Exception:
                        pass
            else:
                # Disagree on deal
                matched_deal["status"] = "CANCELLED"
                matched_deal["cancel_reason"] = f"{matched_role.title()} @{user.username} ne disagree kiya"
                await msg.reply_text(
                    f"❌ <b>{matched_role.title()}</b> ne disagree kiya!\n"
                    f"🪪 <code>{tid}</code>\n\n"
                    f"Deal cancel ho gayi. Naya form bhejo agar chahte ho.",
                    parse_mode="HTML"
                )
        else:
            await msg.reply_text(
                f"❌ Koi pending deal nahi mili {sender_tag}.\n"
                f"Pehle form bhejo aur admin se lock karwao.",
                parse_mode="HTML"
            )
        return

    # ── Form triggers ──
    form_map = {"form": FORM1_TEXT, "form2": FORM2_TEXT, "form3": FORM3_TEXT, "form4": FORM4_TEXT}
    if text_lower in form_map:
        await msg.reply_text(form_map[text_lower])
        return
    if text_lower == "formtype":
        await cmd_formtype(update, ctx)
        return
    if text_lower.startswith("calc"):
        parts = text.split(None, 1)
        ctx.args = [parts[1]] if len(parts) > 1 else []
        await cmd_calc(update, ctx)
        return

    # ── Release trigger — admin + buyer + seller ──
    is_release = text_lower in ("release", "/release") or \
                 text_lower.startswith("release ") or \
                 text_lower.startswith("/release ")
    if is_release:
        uname   = (user.username or "").lower()
        parts   = text.split()
        tid_arg = next((p.upper() for p in parts[1:] if p.upper().startswith("GE-")), None)

        if is_admin(user.id):
            deal = (_ge_deals.get(tid_arg) if tid_arg else None) or \
                   _resolve_ge_deal(update) or \
                   next((d for d in reversed(list(_ge_deals.values()))
                         if d.get("group_id") == chat.id and d.get("status") in ("LOCKED","ACTIVE")), None)
        else:
            deal = None
            for d in reversed(list(_ge_deals.values())):
                if d.get("group_id") != chat.id: continue
                if d.get("status") not in ("LOCKED", "ACTIVE"): continue
                b = d.get("buyer_uname", d.get("buyer", "")).lower().lstrip("@")
                s = d.get("seller_uname", d.get("seller", "")).lower().lstrip("@")
                if uname in (b, s):
                    deal = d
                    break

        if not deal:
            await msg.reply_text(
                "❌ Koi active deal nahi mili.\n"
                "📌 TID do: <code>release GE-XXXXXXXX</code>",
                parse_mode="HTML"
            )
            return
        if deal.get("status") not in ("LOCKED", "ACTIVE"):
            await msg.reply_text(
                f"❌ Deal <code>{deal['tid']}</code> already {deal.get('status')}.",
                parse_mode="HTML"
            )
            return
        await _ge_handle_release(update, ctx, deal)
        return

    # ── BOTH AGREE — admin reply pe form pe ──
    # Pehle check karo — agar BOTH AGREE hai to form detection skip karo
    norm_check = normalize_text(text).upper()
    is_both_agree = any(x in norm_check for x in ("BOTH AGREE", "BOTH AGREED", "BOTHAGREED"))

    if is_both_agree:
        if is_admin(user.id):
            await handle_both_agree(update, ctx)
        else:
            await msg.reply_text("❌ Sirf admin BOTH AGREE kar sakta hai.")
        return

    # ── Form detection — BOT validates FIRST ──
    # Sirf tab check karo jab BOTH AGREE nahi hai
    form_text = text
    if not detect_form_type(form_text) and msg.reply_to_message:
        rt = msg.reply_to_message.text or ""
        if detect_form_type(rt):
            sender2 = msg.reply_to_message.from_user
            if sender2:
                tag2 = f"@{sender2.username}" if sender2.username else sender2.first_name
                form_text = _re.sub(r'(?<![a-zA-Z@])me(?![a-zA-Z])', tag2, rt, flags=_re.IGNORECASE)
            else:
                form_text = rt

    if detect_form_type(form_text):
        _ge_group_latest_form[chat.id] = {"text": form_text, "message_id": msg.message_id}

        form_type = detect_form_type(form_text)
        form_data = parse_form(form_text, form_type)

        def is_blank(v):
            return not v or str(v).strip().lower() in ("", "—", "-", "nil", "none", "n/a")

        buyer_raw  = form_data.get("buyer", "")
        seller_raw = form_data.get("seller", "")
        blank = []

        if is_blank(buyer_raw):  blank.append("Buyer Username")
        if is_blank(seller_raw): blank.append("Seller Username")
        if is_blank(form_data.get("amount", "")): blank.append("Total Amount")

        if form_type == "NORMAL":
            if is_blank(form_data.get("deal_of", "")): blank.append("Deal Of")
            if is_blank(form_data.get("max_time", "")): blank.append("Maximum Time")
            if is_blank(form_data.get("payment_method", "")): blank.append("Payment Method")
            if is_blank(form_data.get("terms", "")): blank.append("Terms & Conditions")
        elif form_type == "BET":
            if is_blank(form_data.get("game_name", "")): blank.append("Game Name")
            if is_blank(form_data.get("bet_type", "")): blank.append("Bet Type")
            if is_blank(form_data.get("p1_loss", "")): blank.append("Party 1 loss condition")
            if is_blank(form_data.get("p2_loss", "")): blank.append("Party 2 loss condition")
            if is_blank(form_data.get("max_time", "")): blank.append("Maximum Time")
        elif form_type == "THIRD_PARTY":
            if is_blank(form_data.get("deal_of", "")): blank.append("Deal Of")
            if is_blank(form_data.get("third_party", "")): blank.append("Third Party Username")
            if is_blank(form_data.get("tp_role", "")): blank.append("Third Party Role")
            if is_blank(form_data.get("max_time", "")): blank.append("Maximum Time")
            if is_blank(form_data.get("terms", "")): blank.append("Terms & Conditions")
        elif form_type == "SERVICE":
            if is_blank(form_data.get("service_type", "")): blank.append("Service Type")
            if is_blank(form_data.get("work_details", "")): blank.append("Work Details")
            if is_blank(form_data.get("completion_time", "")): blank.append("Work Completion Time")
            if is_blank(form_data.get("proof_of_work", "")): blank.append("Proof of Work")
            if is_blank(form_data.get("terms", "")): blank.append("Terms & Conditions")

        if blank:
            b_tag = f"@{buyer_raw.lstrip('@')}" if not is_blank(buyer_raw) else ""
            s_tag = f"@{seller_raw.lstrip('@')}" if not is_blank(seller_raw) else ""
            tags  = " ".join(filter(None, [b_tag, s_tag])) or sender_tag
            fields = "\n".join(f"‣ {f}" for f in blank)
            await msg.reply_text(
                f"⚠️ <b>Form Incomplete!</b> {tags}\n\n"
                f"{fields}\n\n"
                f"Fill karke dobara bhejo — tab admin lock karega.",
                parse_mode="HTML"
            )
            return

        # ── Sab OK — admin ko batao ──
        b_tag = f"@{buyer_raw.lstrip('@')}"
        s_tag = f"@{seller_raw.lstrip('@')}"
        await msg.reply_text(
            f"✅ <b>Form Valid!</b>\n"
            f"🛒 {b_tag} · 🏪 {s_tag} · 💰 {form_data.get('amount','—')}\n\n"
            f"Admin form pe <b>reply</b> karke <b>BOTH AGREE</b> likhe.",
            parse_mode="HTML"
        )
        return

# ══════════════════════════════════════════════════════════
# NEW FEATURES - ADMIN HOLD LIMIT SYSTEM
# ══════════════════════════════════════════════════════════

def get_admin_hold_amount(admin_id: int) -> float:
    """Calculate total amount admin is currently holding"""
    holds = _ge_admin_holds.get(admin_id, [])
    total = 0.0
    for tid in holds:
        deal = _ge_deals.get(tid, {})
        if deal.get("status") not in ("CLOSED", "CANCELLED"):
            amt_str = deal.get("amount", "0")
            try:
                import re as _re_hold
                amt = float(_re_hold.sub(r'[^\d.]', '', str(amt_str)))
                total += amt
            except:
                pass
    return total


async def check_admin_hold_limit(ctx, admin_id: int, new_amount: float) -> bool:
    """Check if admin can take deal. Returns True if OK, False if limit exceeded."""
    if admin_id not in _admin_hold_limits:
        return True
    
    limit = _admin_hold_limits[admin_id]
    current = get_admin_hold_amount(admin_id)
    after = current + new_amount
    percent = (after / limit) * 100 if limit > 0 else 0
    
    if percent >= 100:
        try:
            await ctx.bot.send_message(
                chat_id=admin_id,
                text=(
                    f"🚫 <b>HOLD LIMIT REACHED!</b>\n\n"
                    f"💰 Current: ₹{current:,.0f}\n"
                    f"➕ New: ₹{new_amount:,.0f}\n"
                    f"📊 Total: ₹{after:,.0f} / ₹{limit:,.0f}\n\n"
                    f"❌ Kuch deals close karo pehle."
                ),
                parse_mode="HTML"
            )
        except:
            pass
        return False
    
    if percent >= 80:
        try:
            await ctx.bot.send_message(
                chat_id=admin_id,
                text=(
                    f"⚠️ <b>Hold Limit {percent:.0f}%</b>\n\n"
                    f"💰 ₹{after:,.0f} / ₹{limit:,.0f}\n"
                    f"Limit ke kareeb ho!"
                ),
                parse_mode="HTML"
            )
        except:
            pass
    return True


async def cmd_setlimit(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/setlimit @admin 6000 — Set admin hold limit"""
    user = update.effective_user
    if user.id != MAIN_ADMIN_ID:
        return
    
    args = ctx.args or []
    if len(args) < 2:
        limits_text = "\n".join(f"• {aid}: ₹{lim:,.0f}" for aid, lim in _admin_hold_limits.items()) if _admin_hold_limits else "No limits set"
        await update.message.reply_text(
            f"<b>Admin Hold Limits</b>\n\n{limits_text}\n\n"
            f"Usage: <code>/setlimit @admin 6000</code>",
            parse_mode="HTML"
        )
        return
    
    admin_uname = args[0].lstrip("@")
    amt_parsed = parse_amount_smart(args[1])
    if amt_parsed is None:
        await update.message.reply_text("❌ Invalid amount. Use: 6000 or 6k")
        return
    
    try:
        chat_obj = await ctx.bot.get_chat(f"@{admin_uname}")
        admin_id = chat_obj.id
    except:
        await update.message.reply_text(f"❌ @{admin_uname} not found.")
        return
    
    _admin_hold_limits[admin_id] = amt_parsed
    await update.message.reply_text(
        f"✅ Limit set!\n\n"
        f"👤 @{admin_uname}\n"
        f"💰 ₹{amt_parsed:,.0f}\n\n"
        f"Notify at 80%, block at 100%",
        parse_mode="HTML"
    )


# ══════════════════════════════════════════════════════════
# NEW FEATURES - WARN & AUTO-RELEASE SYSTEM
# ══════════════════════════════════════════════════════════

async def cmd_warn(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/warn @user 4h or /warn TID @user 4h"""
    user = update.effective_user
    if not is_admin(user.id):
        return
    
    args = ctx.args or []
    if len(args) < 2:
        await update.message.reply_text(
            "<b>Warn System</b>\n\n"
            "<code>/warn @user 4h</code>\n"
            "<code>/warn TID @user 4h</code>\n\n"
            "Time: 1h, 30m, 2h30m\n"
            "Auto-release if no response!",
            parse_mode="HTML"
        )
        return
    
    if args[0].upper().startswith("GE-"):
        tid = args[0].upper()
        warned_user = args[1].lstrip("@")
        time_str = args[2] if len(args) > 2 else "4h"
    else:
        warned_user = args[0].lstrip("@")
        time_str = args[1] if len(args) > 1 else "4h"
        tid = None
        for d in reversed(list(_ge_deals.values())):
            if d.get("status") not in ("LOCKED", "ACTIVE"):
                continue
            b = d.get("buyer_uname", "").lower().lstrip("@")
            s = d.get("seller_uname", "").lower().lstrip("@")
            if warned_user.lower() in (b, s):
                tid = d["tid"]
                break
        if not tid:
            await update.message.reply_text(f"❌ No active deal for @{warned_user}")
            return
    
    deal = _ge_deals.get(tid)
    if not deal:
        await update.message.reply_text(f"❌ {tid} not found")
        return
    
    import re as _re_time
    hours = int(_re_time.search(r'(\d+)h', time_str.lower()).group(1)) if _re_time.search(r'(\d+)h', time_str.lower()) else 0
    mins  = int(_re_time.search(r'(\d+)m', time_str.lower()).group(1)) if _re_time.search(r'(\d+)m', time_str.lower()) else 0
    total_secs = (hours * 3600) + (mins * 60) or (4 * 3600)
    
    end_time = datetime.utcnow() + timedelta(seconds=total_secs)
    
    buyer_uname  = deal.get("buyer_uname", "").lstrip("@")
    seller_uname = deal.get("seller_uname", "").lstrip("@")
    other_party = seller_uname if warned_user.lower() == buyer_uname.lower() else buyer_uname
    
    _active_warns[tid] = {
        "warned_user": warned_user,
        "other_party": other_party,
        "warned_by": user.username or str(user.id),
        "end_time": end_time,
        "total_seconds": total_secs
    }
    
    time_display = f"{hours}h {mins}m" if hours and mins else (f"{hours}h" if hours else f"{mins}m")
    
    await update.message.reply_text(
        f"⚠️ <b>WARNING!</b>\n\n"
        f"🪪 <code>{tid}</code>\n"
        f"👤 @{warned_user}\n"
        f"⏰ {time_display}\n\n"
        f"No response → Auto-release to @{other_party}",
        parse_mode="HTML"
    )
    
    try:
        chat_obj = await ctx.bot.get_chat(f"@{warned_user}")
        await ctx.bot.send_message(
            chat_id=chat_obj.id,
            text=(
                f"🚨 <b>URGENT!</b>\n\n"
                f"🪪 <code>{tid}</code>\n"
                f"⏰ {time_display}\n\n"
                f"Respond NOW ya @{other_party} ko release!"
            ),
            parse_mode="HTML"
        )
    except:
        pass
    
    asyncio.create_task(warn_countdown_task(ctx, tid, total_secs))


async def warn_countdown_task(ctx, tid: str, duration: int):
    """Countdown with reminders then auto-release"""
    intervals = []
    if duration >= 3600:  intervals.append((3600, "1h"))
    if duration >= 1800:  intervals.append((1800, "30m"))
    if duration >= 600:   intervals.append((600, "10m"))
    
    for wait_secs, label in intervals:
        await asyncio.sleep(duration - wait_secs)
        duration = wait_secs
        
        warn_data = _active_warns.get(tid)
        if not warn_data:
            return
        
        deal = _ge_deals.get(tid)
        if not deal or deal.get("status") in ("CLOSED", "CANCELLED"):
            return
        
        try:
            chat_obj = await ctx.bot.get_chat(f"@{warn_data['warned_user']}")
            await ctx.bot.send_message(
                chat_id=chat_obj.id,
                text=f"⏰ {label} left! <code>{tid}</code>",
                parse_mode="HTML"
            )
        except:
            pass
    
    await asyncio.sleep(duration)
    
    warn_data = _active_warns.get(tid)
    if not warn_data:
        return
    
    deal = _ge_deals.get(tid)
    if not deal or deal.get("status") in ("CLOSED", "CANCELLED"):
        del _active_warns[tid]
        return
    
    # AUTO-RELEASE!
    other_party = warn_data["other_party"]
    deal["status"] = "AUTO_RELEASED"
    deal["released_to"] = other_party
    deal["released_at"] = datetime.utcnow().isoformat()
    
    group_id = deal.get("group_id")
    if group_id:
        try:
            await ctx.bot.send_message(
                chat_id=group_id,
                text=(
                    f"⚡ <b>AUTO-RELEASED!</b>\n\n"
                    f"🪪 <code>{tid}</code>\n"
                    f"✅ @{other_party}\n\n"
                    f"Admin <code>/close {tid} AMT</code> karo."
                ),
                parse_mode="HTML"
            )
        except:
            pass
    
    del _active_warns[tid]


# ══════════════════════════════════════════════════════════
# NEW FEATURES - VISUAL PROFILE
# ══════════════════════════════════════════════════════════

async def cmd_setfees(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/setfees — Configure all fee percentages"""
    user = update.effective_user
    if user.id != MAIN_ADMIN_ID:
        return
    
    args = ctx.args or []
    
    # Get current config
    fee_config = getattr(state, "fee_config", {
        "p2p_bio": 1.0,
        "p2p_normal": 2.0,
        "product_bio": 1.5,
        "product_normal": 3.0,
        "ge_bio": 1.0,
        "ge_normal": 2.0,
    })
    
    if not args:
        await update.message.reply_text(
            f"💰 <b>Fee Configuration</b>\n\n"
            f"<b>P2P Escrow:</b>\n"
            f"├ Bio Discount: {fee_config.get('p2p_bio', 1.0)}%\n"
            f"└ Normal: {fee_config.get('p2p_normal', 2.0)}%\n\n"
            f"<b>Product Escrow:</b>\n"
            f"├ Bio Discount: {fee_config.get('product_bio', 1.5)}%\n"
            f"└ Normal: {fee_config.get('product_normal', 3.0)}%\n\n"
            f"<b>Group Escrow:</b>\n"
            f"├ Bio Discount: {fee_config.get('ge_bio', 1.0)}%\n"
            f"└ Normal: {fee_config.get('ge_normal', 2.0)}%\n\n"
            f"━━━━━━━━━━━━━━\n"
            f"Usage:\n"
            f"<code>/setfees p2p_bio 1</code>\n"
            f"<code>/setfees product_normal 3</code>\n"
            f"<code>/setfees ge_bio 1</code>",
            parse_mode="HTML"
        )
        return
    
    if len(args) < 2:
        await update.message.reply_text("Usage: <code>/setfees TYPE PERCENT</code>\nTypes: p2p_bio, p2p_normal, product_bio, product_normal, ge_bio, ge_normal", parse_mode="HTML")
        return
    
    fee_type = args[0].lower()
    valid_types = ["p2p_bio", "p2p_normal", "product_bio", "product_normal", "ge_bio", "ge_normal"]
    
    if fee_type not in valid_types:
        await update.message.reply_text(f"❌ Invalid type. Use: {', '.join(valid_types)}")
        return
    
    try:
        percent = float(args[1])
    except:
        await update.message.reply_text("❌ Invalid percentage")
        return
    
    fee_config[fee_type] = percent
    state.fee_config = fee_config
    
    await update.message.reply_text(
        f"✅ Updated!\n\n"
        f"<b>{fee_type}</b> = {percent}%",
        parse_mode="HTML"
    )


async def _generate_profile_card(
    first_name: str, username: str, vol: float,
    deals: int, highest: float, rank: str, level: int,
    pfp_bytes: bytes = None,
) -> bytes:
    """Generate premium blue profile card image using Pillow."""
    from PIL import Image, ImageDraw, ImageFont
    import io as _io

    W, H = 900, 520
    BG    = (10, 11, 18);   CARD  = (16, 18, 30)
    WHITE = (255,255,255);  GRAY  = (130,135,160); LGRAY = (80,85,110)
    DARK  = (24, 26, 42);   ACCENT= (60,140,255);  ACCENT2=(100,180,255)
    GREEN = (50, 215, 120)
    GOLD  = (255, 185, 0)

    RANK_COLORS = {
        "BRONZE":   (180,110,60),  "SILVER":  (180,190,210),
        "GOLD":     (255,185,0),   "PLATINUM":(100,210,255),
        "ELITE":    (180,100,255),
    }
    RANK_NEXT = {
        "BRONZE":("SILVER",25000), "SILVER":("GOLD",100000),
        "GOLD":("PLATINUM",500000),"PLATINUM":("ELITE",1000000),
        "ELITE":("ELITE",9999999),
    }
    RANK_PREV = {"BRONZE":0,"SILVER":25000,"GOLD":100000,"PLATINUM":500000,"ELITE":1000000}

    img  = Image.new("RGBA", (W, H), (*BG, 255))
    draw = ImageDraw.Draw(img, "RGBA")

    try:
        fb = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
        fn = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
        f56=ImageFont.truetype(fb,56); f44=ImageFont.truetype(fb,44)
        f32=ImageFont.truetype(fb,32); f22=ImageFont.truetype(fb,22)
        f18=ImageFont.truetype(fn,18); f15=ImageFont.truetype(fn,15)
        f13=ImageFont.truetype(fn,13)
    except Exception:
        f56=f44=f32=f22=f18=f15=f13=ImageFont.load_default()

    rank_col = RANK_COLORS.get(rank, ACCENT)

    # dot grid bg
    for x in range(0,W,36):
        for y in range(0,H,36):
            draw.ellipse([(x-1,y-1),(x+1,y+1)], fill=(255,255,255,12))

    # glow blobs
    for r in range(220,0,-10):
        draw.ellipse([(W-140-r,-r+60),(W-140+r,r+60)], fill=(*ACCENT,int(18*(1-r/220))))
    for r in range(160,0,-8):
        draw.ellipse([(-r+80,H-60-r),(r+80,H-60+r)], fill=(*rank_col,int(14*(1-r/160))))

    # card bg + border
    draw.rounded_rectangle([(20,20),(W-20,H-20)], radius=22,
                            fill=(*CARD,255), outline=(*ACCENT,120), width=2)
    draw.rounded_rectangle([(20,20),(26,H-20)], radius=4, fill=(*ACCENT,220))
    for i in range(12):
        draw.line([(44,20+i),(W-44,20+i)], fill=(*ACCENT,int(60*(1-i/12))))

    # avatar — thin circle
    AX,AY,AR = 94,104,54
    draw.ellipse([(AX-AR-2,AY-AR-2),(AX+AR+2,AY+AR+2)], outline=ACCENT, width=1)

    pfp_placed = False
    if pfp_bytes:
        try:
            pfp_img = Image.open(_io.BytesIO(pfp_bytes)).convert("RGBA")
            pfp_img = pfp_img.resize((AR*2, AR*2), Image.LANCZOS)
            mask = Image.new("L", (AR*2,AR*2), 0)
            ImageDraw.Draw(mask).ellipse([(0,0),(AR*2,AR*2)], fill=255)
            pfp_img.putalpha(mask)
            img.paste(pfp_img, (AX-AR, AY-AR), pfp_img)
            pfp_placed = True
        except Exception:
            pass
    if not pfp_placed:
        draw.ellipse([(AX-AR,AY-AR),(AX+AR,AY+AR)], fill=DARK)
        letter = (first_name[0] if first_name else "?").upper()
        lw = draw.textlength(letter, font=f44)
        draw.text((AX-lw//2, AY-28), letter, fill=ACCENT, font=f44)

    # name — vertically centered in top section (top section height ~160)
    name_text = first_name.upper()[:16]
    name_y = 52
    draw.text((168, name_y), name_text, fill=WHITE, font=f56)
    draw.text((170, name_y+68), f"@{username}", fill=GRAY, font=f18)

    # rank badge — solid yellow bg, white text, "NTH RANK" format
    def ordinal(n):
        s = {1:"ST", 2:"ND", 3:"RD"}
        return f"{n}{s.get(n if n < 20 else n % 10, 'TH')}"
    badge_text = f"{ordinal(level)} RANK"
    btw = int(draw.textlength(badge_text, font=f22))
    pad = 16
    bx1 = W - 44 - btw - pad * 2
    by1, by2 = 36, 82
    draw.rounded_rectangle([(bx1, by1), (W-44, by2)], radius=10, fill=GOLD)
    tw = int(draw.textlength(badge_text, font=f22))
    cx = bx1 + ((W-44 - bx1) - tw) // 2
    cy = by1 + ((by2 - by1) - 22) // 2
    draw.text((cx, cy), badge_text, fill=WHITE, font=f22)

    draw.line([(48,180),(W-48,180)], fill=(*ACCENT,40), width=1)

    # stat boxes — Rs. instead of rupee symbol (font safe)
    stats_d=[
        ("TOTAL VOLUME",  f"{vol:,.0f}",     "Rs.", "INR"),
        ("DEALS DONE",    str(deals),        "",    "COMPLETED"),
        ("HIGHEST DEAL",  f"{highest:,.0f}", "Rs.", "SINGLE"),
    ]
    box_w=(W-48*2-20)//3; box_y1,box_y2=194,318
    for i,(lbl,val,prefix,unit) in enumerate(stats_d):
        bx=48+i*(box_w+10)
        draw.rounded_rectangle([(bx,box_y1),(bx+box_w,box_y2)], radius=14,
                                fill=(22,24,40,255), outline=(*ACCENT,40), width=1)
        draw.rounded_rectangle([(bx,box_y1),(bx+box_w,box_y1+3)], radius=2, fill=(*ACCENT,120))
        draw.text((bx+12, box_y1+10), lbl, fill=LGRAY, font=f13)
        val_y = box_y1+28
        if prefix:
            draw.text((bx+12, val_y+14), prefix, fill=ACCENT2, font=f18)
            pw2 = int(draw.textlength(prefix, font=f18)) + 4
        else:
            pw2 = 0
        draw.text((bx+12+pw2, val_y), val, fill=ACCENT2, font=f44)
        draw.text((bx+12, box_y2-22), unit, fill=LGRAY, font=f13)

    # progress bar
    nr,nv=RANK_NEXT.get(rank,("ELITE",9999999)); pv=RANK_PREV.get(rank,0)
    prog=min(1.0,(vol-pv)/max(1,nv-pv)) if rank!="ELITE" else 1.0
    bar_x1,bar_x2,bar_y=48,W-48,336
    draw.text((bar_x1,bar_y),"RANK PROGRESS",fill=LGRAY,font=f13); bar_y+=18
    draw.rounded_rectangle([(bar_x1,bar_y),(bar_x2,bar_y+10)], radius=5, fill=(36,38,58))
    fw=max(10,int((bar_x2-bar_x1)*prog))
    draw.rounded_rectangle([(bar_x1,bar_y),(bar_x1+fw,bar_y+10)], radius=5, fill=ACCENT)
    for r2 in range(8,0,-2):
        draw.ellipse([(bar_x1+fw-r2,bar_y+5-r2),(bar_x1+fw+r2,bar_y+5+r2)],
                     fill=(*ACCENT2,int(60*(1-r2/8))))
    draw.text((bar_x1,bar_y+14), rank, fill=rank_col, font=f13)
    if rank!="ELITE":
        nrc=RANK_COLORS.get(nr,GRAY); nrw=draw.textlength(nr,font=f13)
        draw.text((bar_x2-nrw,bar_y+14), nr, fill=nrc, font=f13)
        pt=f"{prog*100:.0f}%"; pw=draw.textlength(pt,font=f13)
        draw.text(((bar_x1+bar_x2-pw)//2,bar_y+14), pt, fill=GRAY, font=f13)

    draw.line([(48,390),(W-48,390)], fill=(255,255,255,15), width=1)

    # trust score
    trust=min(100,deals*7+(15 if vol>10000 else 0)+(10 if highest>5000 else 0))
    ts_col=GREEN if trust>=70 else ACCENT if trust>=40 else (220,80,80)
    draw.text((48,400),"TRUST SCORE",fill=LGRAY,font=f13)
    for i in range(10):
        bx3=48+i*30
        draw.rounded_rectangle([(bx3,418),(bx3+24,428)], radius=3,
                                fill=ts_col if i<trust//10 else (36,38,58))
    draw.text((48,434), f"{trust}/100", fill=ts_col, font=f13)

    brand="BABA ESCROW"; bw3=draw.textlength(brand,font=f18)
    draw.text((W-48-bw3,462), brand, fill=LGRAY, font=f18)
    wm="SECURED + TRUSTED"; wmw=draw.textlength(wm,font=f13)
    draw.text(((W-wmw)//2,466), wm, fill=(*ACCENT,80), font=f13)

    buf=_io.BytesIO(); img.convert("RGB").save(buf,"PNG",optimize=True); buf.seek(0)
    return buf.getvalue()


async def cmd_profile(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/profile - Generate premium profile card"""
    user  = update.effective_user
    uname = (user.username or "").lower()

    if not uname:
        await update.message.reply_text("❌ Pehle Telegram username set karo!")
        return

    stats   = _user_stats.get(uname, {"total_volume":0,"deals":0,"highest_deal":0})
    vol     = float(stats.get("total_volume", 0))
    deals   = int(stats.get("deals", 0))
    highest = float(stats.get("highest_deal", 0))

    if vol >= 1000000:   rank,level = "ELITE",   min(10, int(vol/1000000))
    elif vol >= 500000:  rank,level = "PLATINUM", min(5, int((vol-500000)/100000)+1)
    elif vol >= 100000:  rank,level = "GOLD",     min(5, int((vol-100000)/20000)+1)
    elif vol >= 25000:   rank,level = "SILVER",   min(5, int((vol-25000)/15000)+1)
    else:                rank,level = "BRONZE",   min(5, int(vol/5000)+1)

    # Fetch Telegram profile photo
    pfp_bytes = None
    try:
        photos = await ctx.bot.get_user_profile_photos(user.id, limit=1)
        if photos.total_count > 0:
            file_id = photos.photos[0][-1].file_id  # largest size
            tg_file = await ctx.bot.get_file(file_id)
            pfp_buf = io.BytesIO()
            await tg_file.download_to_memory(pfp_buf)
            pfp_buf.seek(0)
            pfp_bytes = pfp_buf.read()
    except Exception as e:
        logger.warning(f"PFP fetch failed: {e}")

    await update.message.reply_chat_action("upload_photo")

    try:
        card = await _generate_profile_card(
            first_name=user.first_name or "User",
            username=user.username or uname,
            vol=vol, deals=deals, highest=highest,
            rank=rank, level=level,
            pfp_bytes=pfp_bytes,
        )
        card_buf = io.BytesIO(card)
        await update.message.reply_photo(
            photo=card_buf,
            caption=(
                f"🎴 <b>{user.first_name}'s Escrow Profile</b>\n\n"
                f"🏆 Rank: <b>{rank} LVL {level}</b>\n"
                f"💰 Volume: <b>₹{vol:,.0f}</b>\n"
                f"📦 Deals: <b>{deals}</b>\n"
                f"📈 Highest: <b>₹{highest:,.0f}</b>"
            ),
            parse_mode="HTML"
        )
    except Exception as e:
        logger.error(f"Profile card error: {e}")
        await update.message.reply_text(f"❌ Profile generate nahi hua: {e}")


async def cmd_myprofile(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/myprofile - Alias for /profile"""
    await cmd_profile(update, ctx)


# ══════════════════════════════════════════════════════════
# NEW FEATURES - FORM + PAYMENT LINKS TO LOG
# ══════════════════════════════════════════════════════════

async def send_links_to_log(ctx, deal: dict, payment_msg_id: int = None):
    """Send form+payment links to log group"""
    if not state.log_group_id:
        return
    
    tid = deal.get("tid", "—")
    form_msg_id = deal.get("form_message_id")
    group_id = str(deal.get("group_id", ""))
    escrower = deal.get("escrower_username", "—")
    amount = deal.get("amount", "—")
    
    buyer_bio  = deal.get("buyer_has_bio", False)
    seller_bio = deal.get("seller_has_bio", False)
    
    if buyer_bio and seller_bio:
        bio_line = "🏷 Both ✅ → Discount"
    elif buyer_bio or seller_bio:
        bio_line = "🏷 Single ✅ → Standard"
    else:
        bio_line = "🏷 Both ❌ → Standard"
    
    # t.me links (remove -100 prefix from group_id)
    group_short = group_id[4:] if group_id.startswith("-100") else group_id
    form_link = f"https://t.me/c/{group_short}/{form_msg_id}" if form_msg_id else "N/A"
    pay_link  = f"https://t.me/c/{group_short}/{payment_msg_id}" if payment_msg_id else "N/A"
    
    link_text = (
        f"📋 <b>Deal Links</b>\n\n"
        f"🪪 <code>{tid}</code>\n"
        f"💰 {amount}\n"
        f"👨‍⚖️ @{escrower}\n"
        f"━━━━━━━━━━━━━━\n"
        f"📋 Form: {form_link}\n"
        f"💳 Payment: {pay_link}\n"
        f"━━━━━━━━━━━━━━\n"
        f"{bio_line}"
    )
    # Send to log group
    try:
        await ctx.bot.send_message(
            chat_id=state.log_group_id,
            text=link_text,
            parse_mode="HTML",
            disable_web_page_preview=True
        )
    except Exception:
        pass
    # Also send to vouch group if set
    if getattr(state, "vouch_group_id", None):
        try:
            await ctx.bot.send_message(
                chat_id=state.vouch_group_id,
                text=link_text,
                parse_mode="HTML",
                disable_web_page_preview=True
            )
        except Exception:
            pass


async def handle_user_callbacks(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Handle user button callbacks from /start"""
    q = update.callback_query
    await q.answer()
    
    user = q.from_user
    uname = (user.username or "").lower()
    data = q.data
    
    if data == "user:stats":
        # Show detailed stats
        stats = _user_stats.get(uname, {"total_volume": 0, "deals": 0, "highest_deal": 0, "rank": "Unranked"})
        
        await q.edit_message_text(
            f"📊 <b>Your Stats</b>\n\n"
            f"👤 @{user.username or 'User'}\n"
            f"🏆 Rank: {stats.get('rank', 'Unranked')}\n"
            f"💰 Total Volume: ₹{stats.get('total_volume', 0):,.2f}\n"
            f"📈 Deals: {stats.get('deals', 0)}\n"
            f"🎯 Highest Deal: ₹{stats.get('highest_deal', 0):,.2f}\n\n"
            f"Keep trading to rank up!",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("⬅️ Back", callback_data="user:back")
            ]])
        )
    
    elif data == "user:pending":
        # Show pending deals
        pending = [d for d in _ge_deals.values() 
                   if d.get("status") in ("LOCKED", "ACTIVE") and 
                   (d.get("buyer_uname", "").lower().lstrip("@") == uname or 
                    d.get("seller_uname", "").lower().lstrip("@") == uname)]
        
        if not pending:
            text = "📝 <b>Pending Deals</b>\n\nNo pending deals."
        else:
            text = f"📝 <b>Pending Deals</b> ({len(pending)})\n\n"
            for d in pending[:5]:
                text += (
                    f"🪪 <code>{d.get('tid', '—')}</code>\n"
                    f"💰 {d.get('amount', '—')}\n"
                    f"Status: {d.get('status', '—')}\n\n"
                )
        
        await q.edit_message_text(
            text,
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("⬅️ Back", callback_data="user:back")
            ]])
        )
    
    elif data == "user:history":
        # Show completed deals
        completed = [d for d in _ge_deals.values() 
                     if d.get("status") == "CLOSED" and 
                     (d.get("buyer_uname", "").lower().lstrip("@") == uname or 
                      d.get("seller_uname", "").lower().lstrip("@") == uname)]
        
        if not completed:
            text = "📜 <b>History</b>\n\nNo completed deals yet."
        else:
            text = f"📜 <b>History</b> ({len(completed)} deals)\n\n"
            for d in list(reversed(completed))[:5]:
                text += (
                    f"🪪 <code>{d.get('tid', '—')}</code>\n"
                    f"💰 {d.get('amount', '—')}\n"
                    f"✅ Completed\n\n"
                )
        
        await q.edit_message_text(
            text,
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("⬅️ Back", callback_data="user:back")
            ]])
        )
    
    elif data == "user:ranks":
        # Global leaderboard
        sorted_users = sorted(_user_stats.items(), 
                             key=lambda x: x[1].get("total_volume", 0), 
                             reverse=True)[:10]
        
        text = "🌍 <b>Global Leaderboard</b>\n\n"
        for i, (u, s) in enumerate(sorted_users, 1):
            text += f"{i}. @{u} — ₹{s.get('total_volume', 0):,.0f}\n"
        
        await q.edit_message_text(
            text,
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("⬅️ Back", callback_data="user:back")
            ]])
        )
    
    elif data == "user:help":
        # Help guide
        await q.edit_message_text(
            f"📖 <b>Help & Guide</b>\n\n"
            f"<b>Commands:</b>\n"
            f"/calc AMOUNT — Fee calculator\n"
            f"/mystatus — Check deal status\n"
            f"/instructions — Full usage guide\n\n"
            f"<b>How it works:</b>\n"
            f"1. Press 🔄 P2P or 🛒 Product to start\n"
            f"2. Bot creates private group\n"
            f"3. Both parties join group\n"
            f"4. Fill /dd form in group\n"
            f"5. Admin locks deal — both agree\n"
            f"6. Buyer pays admin\n"
            f"7. Admin releases to seller ✅\n\n"
            f"<b>Support:</b>\n"
            f"Contact admins for help.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("⬅️ Back", callback_data="user:back")
            ]])
        )
    
    elif data == "user:noop":
        await q.answer()
        return

    elif data == "user:back":
        # Back to start
        stats = _user_stats.get(uname, {"total_volume": 0, "deals": 0, "highest_deal": 0, "rank": "Unranked"})

        welcome_text = (
            f"<b>Welcome to Premium ESCROW Bot</b>\n\n"
            f"Hello, {user.first_name}!\n\n"
            f"<b>Your Profile</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"Username: @{user.username or 'Not Set'}\n"
            f"Global Rank: {stats.get('rank', 'Unranked')}\n"
            f"Deals: {stats.get('deals', 0)}\n"
            f"Volume: ₹{stats.get('total_volume', 0):,.2f}\n"
            f"━━━━━━━━━━━━━━━━━━━━\n\n"
            f"<b>Secure • Fast • Trusted</b>\n\n"
            f"Use the buttons below to navigate:"
        )

        kb_rows = [
            [InlineKeyboardButton("⚙️ ── START A DEAL ──", callback_data="user:noop")],
            [
                InlineKeyboardButton("🔄 P2P Escrow", callback_data="deal_type:p2p"),
                InlineKeyboardButton("🛒 Product Escrow", callback_data="deal_type:product"),
            ],
            [InlineKeyboardButton("⚙️ ── MY ACCOUNT ──", callback_data="user:noop")],
            [
                InlineKeyboardButton("📊 My Stats", callback_data="user:stats"),
                InlineKeyboardButton("📝 Pending Deals", callback_data="user:pending"),
            ],
            [
                InlineKeyboardButton("📜 History", callback_data="user:history"),
                InlineKeyboardButton("🌍 Global Ranks", callback_data="user:ranks"),
            ],
            [InlineKeyboardButton("📖 Help & Guide", callback_data="user:help")],
        ]

        if is_admin(user.id):
            kb_rows.append([
                InlineKeyboardButton("👑 Admin Panel", callback_data="adm:status")
            ])

        kb = InlineKeyboardMarkup(kb_rows)
        
        await q.edit_message_text(welcome_text, parse_mode="HTML", reply_markup=kb)


if __name__ == "__main__":
    main()
