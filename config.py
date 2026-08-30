import os
from typing import Dict, List, Optional, TypedDict

# Telegram Bot token
BOT_TOKEN = os.getenv("BOT_TOKEN", "1234567890:ABCdefGHIjklMNOpqrsTUVwxyz123456789")

# Mongo
MONGO_URI = os.getenv(
    "MONGO_URI",
    "mongodb+srv://username:password@cluster0.mongodb.net/clientidstore_db?retryWrites=true&w=majority",
)
DB_NAME = os.getenv("DB_NAME", "clientidstore_db")

# Admin Telegram user IDs (comma-separated)
ADMIN_USER_IDS: List[int] = [
    int(x)
    for x in os.getenv("ADMIN_USER_IDS", "123456789,987654321").split(",")
    if x.strip().isdigit()
]

# Start screen image
START_IMAGE = "https://i.postimg.cc/example/your-start-image.jpg"

# Bot username (without @) for referral links
BOT_USERNAME = os.getenv("BOT_USERNAME", "YourBotUsername")

# Support bot username (without @)
SUPPORT_USERNAME = os.getenv("SUPPORT_USERNAME", "YourSupportUsername")

# Public URL of the deployed api_server.py (set this once you deploy the API
# as a second Railway service). Leave the default if not deployed yet.
API_BASE_URL = os.getenv("API_BASE_URL", "https://your-api-url.up.railway.app")

# Referral program percentage (3% forever)
REFERRAL_PERCENT = float(os.getenv("REFERRAL_PERCENT", "3.0"))

# Channel join requirement
CHANNEL_USERNAME = os.getenv("CHANNEL_USERNAME", "YourChannelUsername")  # without @

# Report channel (bot must be admin there). Without @
REPORT_CHANNEL_USERNAME = os.getenv("REPORT_CHANNEL_USERNAME", "YourReportChannel")

# Fixed Telegram API credentials used for adding accounts (admin flow)
TELEGRAM_API_ID = int(os.getenv("TELEGRAM_API_ID", "12345678"))
TELEGRAM_API_HASH = os.getenv("TELEGRAM_API_HASH", "abcdef1234567890abcdef1234567890")

# ----------------------------
# BharatPe UPI Deposit (real transaction verification via BharatPe API)
# ----------------------------
BHARATPE_UPI_ID = os.getenv("BHARATPE_UPI_ID", "BHARATPE.8X0M0S6J8F70781@fbpe")
BHARATPE_TOKEN = os.getenv("BHARATPE_TOKEN", "")
BHARATPE_MERCHANT_ID = os.getenv("BHARATPE_MERCHANT_ID", "")

