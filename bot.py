"""
P2P Telegram Escrow Bot — Full Implementation
Single file | No database | LOG GROUP = storage | Telethon auto group creation
"""

import asyncio
import aiohttp
import uuid
import io
import logging
import qrcode
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputFile
from telegram.ext import ApplicationBuilder, CommandHandler, CallbackQueryHandler, ContextTypes
from telethon import TelegramClient
from telethon.tl.functions.channels import (
    CreateChannelRequest, InviteToChannelRequest,
    EditAdminRequest, ExportInviteRequest
)
from telethon.tl.types import ChatAdminRights
from config import BOT_TOKEN, MAIN_ADMIN_ID, API_ID, API_HASH, PHONE, state

logging.basicConfig(format="%(asctime)s | %(levelname)s | %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

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

        invite = await client(ExportInviteRequest(peer=channel))
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
        "<b>5️⃣</b> <b>/deposit</b> → get escrow address + QR\n\n"
        "<b>6️⃣</b> <b>/verify</b> → mark funded → buyer pays seller privately\n\n"
        "<b>7️⃣</b> Both press <b>Confirm</b> → deal releases automatically\n\n"
        "<b>8️⃣</b> <b>/dispute</b> → call admin if any issue\n\n"
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
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Add Admin", callback_data="adm:addadmin"),
         InlineKeyboardButton("➖ Remove Admin", callback_data="adm:removeadmin")],
        [InlineKeyboardButton("💸 Set Fee", callback_data="adm:setfee"),
         InlineKeyboardButton("🏷 Set Bio Tag", callback_data="adm:setbio")],
        [InlineKeyboardButton("🔑 Set OxaPay", callback_data="adm:setoxapay"),
         InlineKeyboardButton("✅ Check OxaPay", callback_data="adm:checkoxapay")],
        [InlineKeyboardButton("🗑 Reset OxaPay", callback_data="adm:resetoxapay"),
         InlineKeyboardButton("📋 Set Log Group", callback_data="adm:setloggroup")],
        [InlineKeyboardButton("📊 Bot Status", callback_data="adm:status"),
         InlineKeyboardButton("👥 List Admins", callback_data="adm:listadmins")]
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
    if d == "start_deal":               await handle_start_deal(update, ctx)
    elif d == "show_instructions":      await cmd_instructions(update, ctx)
    elif d.startswith("token_select:"): await handle_token_pick(update, ctx, d)
    elif d.startswith("token_confirm:"): await handle_token_confirm(update, ctx, d)
    elif d.startswith("token_reselect:"): await handle_token_reselect(update, ctx, d)
    elif d.startswith("confirm:"):      await handle_confirmation(update, ctx, d)
    elif d.startswith("dispute_handle:"): await handle_dispute_admin(update, ctx, d)
    elif d == "dispute_call":           await handle_dispute_call(update, ctx)
    elif d.startswith("adm:"):          await handle_admin_panel_cb(update, ctx, d)

# ══════════════════════════════════════════════════════════
# ADMIN PANEL CALLBACKS
# ══════════════════════════════════════════════════════════

async def handle_admin_panel_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE, d: str):
    q = update.callback_query
    if not is_main_admin(q.from_user.id):
        await q.answer("❌ Access denied.", show_alert=True)
        return
    action = d.split(":")[1]

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
            async with aiohttp.ClientSession() as s:
                async with s.post("https://api.oxapay.com/merchants/balance",
                                  json={"merchant": state.oxapay_key},
                                  timeout=aiohttp.ClientTimeout(total=10)) as r:
                    data = await r.json()
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

    elif action in ("addadmin", "removeadmin", "setfee", "setbio", "setoxapay"):
        prompts = {
            "addadmin":    ("➕ <b>Add Sub Admin</b>",   "/addadmin {user_id}"),
            "removeadmin": ("➖ <b>Remove Sub Admin</b>","/removeadmin {user_id}"),
            "setfee":      ("💸 <b>Set Fee</b>",         "/setfee {percentage}"),
            "setbio":      ("🏷 <b>Set Bio Tag</b>",     "/setbio {tag}"),
            "setoxapay":   ("🔑 <b>Set OxaPay Key</b>", "/setoxapay {api_key}"),
        }
        title, usage = prompts[action]
        await q.edit_message_text(f"{title}\n\nUsage: <code>{usage}</code>\n\n⬅️ /adminpanel", parse_mode="HTML")

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
        async with aiohttp.ClientSession() as s:
            async with s.post("https://api.oxapay.com/merchants/request",
                              json={"merchant": state.oxapay_key, "amount": float(deal.get("quantity", 1)),
                                    "currency": currency, "network": network,
                                    "description": f"Escrow {did}", "lifeTime": 60}) as r:
                data = await r.json()
        if data.get("result") != 100:
            raise Exception(data.get("message", "Unknown error"))
        address = data.get("payAddress", "N/A")
        deal["deposit_address"] = address
        deal["status"] = "AWAITING_DEPOSIT"
        await send_qr(ctx, chat.id, address,
            f"✅ <b>DEPOSIT ADDRESS READY</b>\n\n🪙 Token: {deal['token']}\n"
            f"📬 Address:\n<code>{address}</code>\n💰 Amount: {deal.get('quantity')}\n\n"
            f"⚠️ Send EXACT amount.\n\n➡️ <b>Next step:</b> After sending, use <b>/verify</b>"
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
# STEP 7: /verify
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
        await update.message.reply_text("⚠️ Deal already FUNDED.")
        return
    if deal["status"] != "AWAITING_DEPOSIT":
        await update.message.reply_text("❌ Use /deposit first.", parse_mode="HTML")
        return

    deal["funded"]    = True
    deal["status"]    = "FUNDED"
    deal["funded_by"] = user.username or user.first_name
    deal["funded_at"] = datetime.utcnow().isoformat()

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Buyer Confirm", callback_data=f"confirm:buyer:{did}"),
         InlineKeyboardButton("✅ Seller Confirm", callback_data=f"confirm:seller:{did}")],
        [InlineKeyboardButton("🚨 Dispute / Call Admin", callback_data="dispute_call")]
    ])
    await update.message.reply_text(
        f"✅ <b>PAYMENT FUNDED!</b>\n\n"
        f"🆔 Trade ID: <code>{did}</code>\n🪙 Token: {deal.get('token')}\n💰 Amount: {deal.get('quantity')}\n\n"
        f"────────────────────\n"
        f"<b>BUYER:</b> Now send the agreed payment to the seller privately.\n"
        f"Once seller receives it, BOTH press Confirm below.\n\n"
        f"⚠️ Deal releases ONLY when BOTH confirm.",
        reply_markup=kb, parse_mode="HTML"
    )
    await log(ctx,
        f"💰 <b>DEAL FUNDED</b>\n\n🆔 <code>{did}</code>\n🪙 {deal.get('token')}\n"
        f"💵 {deal.get('quantity')}\n👤 @{deal['funded_by']}\n⏰ {deal['funded_at']}\n📊 FUNDED"
    )

# ══════════════════════════════════════════════════════════
# STEP 8: CONFIRMATION
# ══════════════════════════════════════════════════════════

async def handle_confirmation(update: Update, ctx: ContextTypes.DEFAULT_TYPE, d: str):
    q = update.callback_query
    user = q.from_user
    _, role, did = d.split(":")
    deal = deal_by_id(did)
    if not deal:
        await q.answer("❌ Deal not found.", show_alert=True)
        return
    if not deal.get("funded"):
        await q.answer("❌ Deal not funded yet.", show_alert=True)
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
        await q.edit_message_text("🎉 <b>BOTH CONFIRMED!</b>\n\n✅ Buyer\n✅ Seller\n\n⏳ Processing release…", parse_mode="HTML")
        await release_deal(ctx, did, deal, q.message.chat_id)
    else:
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton(f"{'✅' if b else '⏳'} Buyer Confirm", callback_data=f"confirm:buyer:{did}"),
             InlineKeyboardButton(f"{'✅' if s else '⏳'} Seller Confirm", callback_data=f"confirm:seller:{did}")],
            [InlineKeyboardButton("🚨 Dispute / Call Admin", callback_data="dispute_call")]
        ])
        await q.edit_message_text(
            f"📊 <b>CONFIRMATION STATUS</b>\n\n"
            f"🛒 Buyer: {'✅ Confirmed' if b else '⏳ Waiting'}\n"
            f"🏪 Seller: {'✅ Confirmed' if s else '⏳ Waiting'}\n\n"
            f"⚠️ Both must confirm for release.",
            reply_markup=kb, parse_mode="HTML"
        )

# ══════════════════════════════════════════════════════════
# STEP 9: RELEASE
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

    deal["status"]       = "COMPLETED"
    deal["final_amount"] = final
    deal["fee_deducted"] = fee_amt
    deal["completed_at"] = datetime.utcnow().isoformat()

    try:
        await ctx.bot.send_message(
            chat_id=group_id,
            text=(
                f"🎉 <b>DEAL COMPLETED!</b>\n\n"
                f"🆔 Trade ID: <code>{did}</code>\n🪙 Token: {deal.get('token')}\n"
                f"💰 Original: {qty}\n💸 Fee ({state.fee_percent}%): {fee_amt:.4f}\n"
                f"✅ Final Amount: {final:.4f}\n\n"
                f"🛒 Buyer: @{deal.get('buyer_username')}\n🏪 Seller: @{deal.get('seller_username')}\n\n"
                f"📊 Status: <b>COMPLETED</b>\n⏰ {deal['completed_at']}\n\n"
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
        f"📦 <code>{group_id}</code>\n📊 COMPLETED\n⏰ {deal['completed_at']}"
    )

    for p in ("buyer", "seller"):
        pid = deal.get(f"{p}_id")
        if pid:
            try:
                await ctx.bot.send_message(
                    chat_id=pid,
                    text=f"✅ <b>Deal Completed: {did}</b>\n\nFinal: <b>{final:.4f} {deal.get('token')}</b>\nGroup closes shortly.",
                    parse_mode="HTML"
                )
            except Exception:
                pass

    await asyncio.sleep(10)
    try:
        await ctx.bot.send_message(chat_id=group_id, text="🗑 <b>Group closing in 10 seconds. Thank you!</b>", parse_mode="HTML")
        await asyncio.sleep(10)
        await ctx.bot.leave_chat(group_id)
        # Optionally delete via Telethon
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
# STEP 10: /dispute
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
        f"🚨 <b>DISPUTE TRIGGERED!</b>\n\n👤 By: @{deal['dispute_by']}\n📝 Reason: {reason}\n\n⏳ An admin will join shortly.",
        parse_mode="HTML"
    )

    group_link = f"https://t.me/c/{str(chat.id).replace('-100','')}/1"
    await alert_admins(ctx,
        f"🚨 <b>DISPUTE ALERT!</b>\n\n🆔 <code>{did}</code>\n"
        f"🛒 @{deal.get('buyer_username','N/A')}  🏪 @{deal.get('seller_username','N/A')}\n"
        f"⚠️ By: @{deal['dispute_by']}\n📝 {reason}\n🔗 {group_link}\n⏰ {deal['dispute_at']}",
        deal_id=did
    )
    await log(ctx,
        f"⚠️ <b>DISPUTE OPENED</b>\n\n🆔 <code>{did}</code>\n"
        f"🛒 @{deal.get('buyer_username')}  🏪 @{deal.get('seller_username')}\n"
        f"⚠️ By: @{deal['dispute_by']}\n📝 {reason}\n📦 <code>{chat.id}</code>\n📊 DISPUTED\n⏰ {deal['dispute_at']}"
    )

async def handle_dispute_call(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    chat_id = q.message.chat_id
    did, deal = deal_by_group(chat_id)
    if not deal:
        await q.answer("❌ No active deal.", show_alert=True)
        return
    if deal.get("status") == "DISPUTED":
        await q.answer("⚠️ Dispute already open.", show_alert=True)
        return

    user = q.from_user
    deal["status"]         = "DISPUTED"
    deal["dispute_by"]     = user.username or user.first_name
    deal["dispute_reason"] = "Triggered via inline button"
    deal["dispute_at"]     = datetime.utcnow().isoformat()

    await q.edit_message_text("🚨 <b>DISPUTE TRIGGERED!</b>\n\nAdmin notified. Please remain in the group.", parse_mode="HTML")

    group_link = f"https://t.me/c/{str(chat_id).replace('-100','')}/1"
    await alert_admins(ctx,
        f"🚨 <b>DISPUTE ALERT!</b>\n\n🆔 <code>{did}</code>\n"
        f"🛒 @{deal.get('buyer_username','N/A')}  🏪 @{deal.get('seller_username','N/A')}\n"
        f"⚠️ By: @{deal['dispute_by']}\n🔗 {group_link}",
        deal_id=did
    )
    await log(ctx, f"⚠️ <b>DISPUTE OPENED</b>\n\n🆔 <code>{did}</code>\n📊 DISPUTED\n⏰ {deal['dispute_at']}")

async def handle_dispute_admin(update: Update, ctx: ContextTypes.DEFAULT_TYPE, d: str):
    q = update.callback_query
    user = q.from_user
    did = d.split(":")[1]
    if not is_admin(user.id):
        await q.answer("❌ Not authorized.", show_alert=True)
        return
    deal = deal_by_id(did)
    if not deal:
        await q.answer("❌ Deal not found.", show_alert=True)
        return
    if did in state.dispute_admins and state.dispute_admins[did] != user.id:
        await q.answer("❌ Another admin is already handling this.", show_alert=True)
        return

    state.dispute_admins[did] = user.id
    deal["dispute_admin"] = user.username or str(user.id)

    await q.edit_message_text(
        f"✅ <b>You are handling this dispute.</b>\n\n🆔 <code>{did}</code>\n"
        f"🛒 @{deal.get('buyer_username')}  🏪 @{deal.get('seller_username')}\n\n"
        f"Commands:\n"
        f"<code>/releaseto buyer {did}</code>\n"
        f"<code>/releaseto seller {did}</code>\n"
        f"<code>/canceldeal {did}</code>",
        parse_mode="HTML"
    )
    try:
        await ctx.bot.send_message(
            chat_id=deal["group_id"],
            text=f"👨‍💼 <b>Admin @{user.username or 'Admin'} joined.</b>\nHandling dispute. <b>Other admins cannot join.</b>",
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
        async with aiohttp.ClientSession() as s:
            async with s.post("https://api.oxapay.com/merchants/balance",
                              json={"merchant": state.oxapay_key},
                              timeout=aiohttp.ClientTimeout(total=10)) as r:
                data = await r.json()
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
    app.add_handler(CallbackQueryHandler(callback_handler))

    logger.info("All handlers registered. Bot running…")
    app.run_polling(allowed_updates=["message", "callback_query", "chat_member"])

if __name__ == "__main__":
    main()
