# -*- coding: utf-8 -*-
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
BINANCE_ID = os.getenv("BINANCE_ID", "\u063a\u064a\u0631 \u0645\u062d\u062f\u062f").strip()
INSTAPAY_ADDRESS = os.getenv("INSTAPAY_ADDRESS", "\u063a\u064a\u0631 \u0645\u062d\u062f\u062f").strip()
CURRENCY = os.getenv("CURRENCY", "USD").strip()
DB_PATH = os.getenv("DB_PATH", str(BASE_DIR / "esim_store.db")).strip()
QR_DIR = BASE_DIR / "qr_codes"
QR_DIR.mkdir(parents=True, exist_ok=True)
# ----------------- Binance Pay Auto Top-Up -----------------
# نظام التشغيل: merchant (حساب تاجر + Webhook) أو monitor (حساب عادي، مفتاح قراءة فقط)
BINANCE_PAY_MODE = os.getenv("BINANCE_PAY_MODE", "").strip().lower()

# بيانات وضع التاجر (merchant) — من لوحة تحكم Binance Pay Merchant
BINANCE_PAY_API_KEY = os.getenv("BINANCE_PAY_API_KEY", "").strip()
BINANCE_PAY_SECRET_KEY = os.getenv("BINANCE_PAY_SECRET_KEY", "").strip()
BINANCE_PAY_WEBHOOK_URL = os.getenv("BINANCE_PAY_WEBHOOK_URL", "").strip()

# بيانات وضع مراقبة الإيداعات (monitor) — مفتاح API عادي بصلاحية قراءة فقط
BINANCE_API_KEY = os.getenv("BINANCE_API_KEY", "").strip()
BINANCE_API_SECRET = os.getenv("BINANCE_API_SECRET", "").strip()
# مفتاح OCR.space لقراءة صور إيصالات التحويل (الطبقة المجانية)
OCR_SPACE_API_KEY = os.getenv("OCR_SPACE_API_KEY", "").strip()
# إعدادات مشتركة
BINANCE_PAY_CURRENCY = os.getenv("BINANCE_PAY_CURRENCY", "USDT").strip() or "USDT"
BINANCE_PAY_LISTEN_HOST = os.getenv("BINANCE_PAY_LISTEN_HOST", "127.0.0.1").strip() or "127.0.0.1"
try:
    BINANCE_PAY_LISTEN_PORT = int(os.getenv("BINANCE_PAY_LISTEN_PORT", "8080").strip() or "8080")
except ValueError:
    BINANCE_PAY_LISTEN_PORT = 8080
