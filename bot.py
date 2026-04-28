import logging
import asyncio
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    filters
)
from config import BOT_TOKEN
from handlers import (
    cmd_start, cmd_instructions, callback_handler,
    cmd_dd, cmd_buyer, cmd_seller, cmd_token,
    cmd_deposit, cmd_verify, cmd_dispute,
    cmd_setloggroup, cmd_addadmin, cmd_removeadmin,
    cmd_setfee, cmd_setbio, cmd_setoxapay,
    cmd_checkoxapay, cmd_resetoxapay,
    cmd_releaseto, cmd_status, cmd_dealinfo,
    cmd_initdeal, cmd_listadmins, cmd_canceldeal
)

# Logging setup
logging.basicConfig(
    format="%(asctime)s | %(name)s | %(levelname)s | %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)


def main():
    logger.info("Starting P2P Escrow Bot...")

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # ── General Commands ──
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("instructions", cmd_instructions))
    app.add_handler(CommandHandler("initdeal", cmd_initdeal))

    # ── Deal Flow Commands ──
    app.add_handler(CommandHandler("dd", cmd_dd))
    app.add_handler(CommandHandler("buyer", cmd_buyer))
    app.add_handler(CommandHandler("seller", cmd_seller))
    app.add_handler(CommandHandler("token", cmd_token))
    app.add_handler(CommandHandler("deposit", cmd_deposit))
    app.add_handler(CommandHandler("verify", cmd_verify))
    app.add_handler(CommandHandler("dispute", cmd_dispute))
    app.add_handler(CommandHandler("dealinfo", cmd_dealinfo))

    # ── Admin Commands ──
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

    # ── Additional Admin Commands ──
    app.add_handler(CommandHandler("listadmins", cmd_listadmins))
    app.add_handler(CommandHandler("canceldeal", cmd_canceldeal))

    # ── Callback Queries ──
    app.add_handler(CallbackQueryHandler(callback_handler))

    logger.info("All handlers registered. Bot is running...")
    app.run_polling(allowed_updates=["message", "callback_query", "chat_member"])


if __name__ == "__main__":
    main()
