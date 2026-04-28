import os
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
MAIN_ADMIN_ID = int(os.getenv("MAIN_ADMIN_ID", "123456789"))

# Runtime storage (replaces database)
class BotState:
    def __init__(self):
        self.log_group_id = None          # LOG GROUP chat_id
        self.sub_admins = set()           # Sub admin user IDs
        self.fee_percent = 1.0            # Default 1% fee
        self.required_bio = None          # Required username in bio
        self.oxapay_key = None            # OxaPay API key
        self.deals = {}                   # deal_id -> deal_data
        self.group_to_deal = {}           # group_chat_id -> deal_id
        self.dispute_admins = {}          # deal_id -> admin_user_id
        self.awaiting_log_group = set()   # admins who triggered /setloggroup

state = BotState()
