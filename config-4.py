import os

BOT_TOKEN    = os.getenv("BOT_TOKEN", "YOUR_BOT_TOKEN")
MAIN_ADMIN_ID = int(os.getenv("MAIN_ADMIN_ID", "123456789"))
API_ID       = int(os.getenv("API_ID", "0")) or None
API_HASH     = os.getenv("API_HASH", "") or None
PHONE        = os.getenv("PHONE", "") or None

class BotState:
    def __init__(self):
        self.log_group_id         = None
        self.dispute_group_id     = None
        self.fee_percent          = 1.0
        self.bio_discount_percent = 0.0
        self.required_bio         = None
        self.oxapay_key           = None
        self.sub_admins           = set()
        self.deals                = {}   # tid -> deal dict
        self.group_to_deal        = {}   # group_id -> tid
        self.dispute_admins       = {}   # tid -> admin_id
        self.telethon_client      = None
        self.api_id               = int(os.getenv("API_ID","0")) or None
        self.api_hash             = os.getenv("API_HASH","") or None
        self.phone                = os.getenv("PHONE","") or None
        self._pending_telethon    = None
        self._waiting_otp         = False
        self.vouch_group_id       = None
        self.vouch_enabled        = True
        self.upi_methods          = {}   # name -> {upi_id, added_by}
        self.channel_link         = None # /setchannel se set karo
        self.channel_name         = "Our Channel"  # /setchannel se naam bhi set hoga
        self.escrow_group_id      = None # /setescrowgroup se set hoga — sirf yahan bot kaam karega

state = BotState()
