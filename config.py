import os
from pathlib import Path
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
try:
    ADMIN_ID = int(os.getenv("ADMIN_ID", "0").strip())
except ValueError:
    ADMIN_ID = 0

SUPPORT_USERNAME = os.getenv("SUPPORT_USERNAME", "Support").strip().lstrip("@")
BINANCE_ID = os.getenv("BINANCE_ID", "غير محدد").strip()
INSTAPAY_ADDRESS = os.getenv("INSTAPAY_ADDRESS", "غير محدد").strip()
CURRENCY = os.getenv("CURRENCY", "USD").strip()
DB_PATH = os.getenv("DB_PATH", str(BASE_DIR / "esim_store.db")).strip()
QR_DIR = BASE_DIR / "qr_codes"
QR_DIR.mkdir(parents=True, exist_ok=True)