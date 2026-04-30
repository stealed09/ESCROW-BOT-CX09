import os
from dotenv import load_dotenv

load_dotenv()

# ── Bot Credentials ──
BOT_TOKEN    = os.getenv("BOT_TOKEN", "YOUR_BOT_TOKEN")
MAIN_ADMIN_ID = int(os.getenv("MAIN_ADMIN_ID", "123456789"))

# ── Telethon (User Client) — for auto group creation ──
API_ID   = int(os.getenv("API_ID", "0"))      # from my.telegram.org
API_HASH = os.getenv("API_HASH", "")           # from my.telegram.org
PHONE    = os.getenv("PHONE", "")              # e.g. +1234567890

# ── Runtime In-Memory Storage ──
class BotState:
    def __init__(self):
        self.log_group_id    = None
        self.sub_admins      = set()
        self.fee_percent     = 1.0
        self.required_bio    = None
        self.oxapay_key      = None
        self.deals           = {}
        self.group_to_deal   = {}
        self.dispute_admins  = {}
        self.telethon_client = None   # Telethon client instance

state = BotState()
