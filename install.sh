#!/usr/bin/env bash
set -e

echo "============================================================"
echo "   🚀 مثبت بوت تيليجرام لبيع شرائح eSIM (يعمل 24/7 تلقائياً)   "
echo "============================================================"
echo ""

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_DIR"

# 1. Interactive Configuration Prompts
read -rp "🔑 أدخل توكن البوت (BOT_TOKEN من @BotFather): " BOT_TOKEN
while [[ -z "$BOT_TOKEN" ]]; do
    read -rp "⚠️ التوكن مطلوب! أدخل BOT_TOKEN: " BOT_TOKEN
done

read -rp "👑 أدخل معرف المالك الرقمي في تيليجرام (ADMIN_ID): " ADMIN_ID
while [[ ! "$ADMIN_ID" =~ ^[0-9]+$ ]]; do
    read -rp "⚠️ يجب أن يكون معرف المالك أرقاماً فقط! أدخل ADMIN_ID: " ADMIN_ID
done

read -rp "🛟 أدخل يوزر الدعم الفني في تيليجرام (مثال: MySupport بدون @): " SUPPORT_USERNAME
SUPPORT_USERNAME="${SUPPORT_USERNAME#@}"
SUPPORT_USERNAME="${SUPPORT_USERNAME:-Support}"

read -rp "🟡 أدخل معرف الدفع عبر بينانس (BINANCE_ID): " BINANCE_ID
BINANCE_ID="${BINANCE_ID:-غير محدد}"

read -rp "🟣 أدخل عنوان أو رقم الدفع عبر إنستا باي مصر (INSTAPAY_ADDRESS): " INSTAPAY_ADDRESS
INSTAPAY_ADDRESS="${INSTAPAY_ADDRESS:-غير محدد}"

read -rp "💵 أدخل عملة المتجر الافتراضية [USD / EGP] (الافتراضي: USD): " CURRENCY
CURRENCY="${CURRENCY:-USD}"

# 2. Write .env file
cat > "$PROJECT_DIR/.env" <<EOF
BOT_TOKEN=${BOT_TOKEN}
ADMIN_ID=${ADMIN_ID}
SUPPORT_USERNAME=${SUPPORT_USERNAME}
BINANCE_ID=${BINANCE_ID}
INSTAPAY_ADDRESS=${INSTAPAY_ADDRESS}
CURRENCY=${CURRENCY}
DB_PATH=${PROJECT_DIR}/esim_store.db
EOF
chmod 600 "$PROJECT_DIR/.env"
echo "✅ تم إنشاء ملف الإعدادات (.env) بنجاح."

# 3. Install Python & Virtual Environment on VPS
echo "📦 جاري تحديث النظام وتثبيت متطلبات Python..."
if command -v apt-get >/dev/null 2>&1; then
    sudo apt-get update -y
    sudo apt-get install -y python3 python3-venv python3-pip sqlite3
elif command -v yum >/dev/null 2>&1; then
    sudo yum install -y python3 python3-pip sqlite
fi

if [[ ! -d "$PROJECT_DIR/venv" ]]; then
    python3 -m venv "$PROJECT_DIR/venv"
fi

"$PROJECT_DIR/venv/bin/pip" install --upgrade pip
"$PROJECT_DIR/venv/bin/pip" install -r "$PROJECT_DIR/requirements.txt"
echo "✅ تم تثبيت جميع مكاتب البوت داخل البيئة الافتراضية (venv)."

# 4. Configure systemd service for 24/7 execution & auto-restart on boot
SERVICE_FILE="/etc/systemd/system/esim-bot.service"
CURRENT_USER="$(whoami)"

echo "⚙️ جاري إعداد خدمة النظام (systemd) ليعمل البوت 24/7 ويفتح تلقائياً عند إعادة التشغيل..."
sudo bash -c "cat > $SERVICE_FILE" <<EOF
[Unit]
Description=Arabic Telegram eSIM Store Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${CURRENT_USER}
WorkingDirectory=${PROJECT_DIR}
EnvironmentFile=${PROJECT_DIR}/.env
ExecStart=${PROJECT_DIR}/venv/bin/python ${PROJECT_DIR}/bot.py
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable esim-bot.service
sudo systemctl restart esim-bot.service

echo ""
echo "============================================================"
echo "🎉 تم تثبيت وتشغيل البوت بنجاح! يعمل الآن 24/7 وسيعمل تلقائياً عند إعادة تشغيل الـ VPS."
echo "============================================================"
echo "📌 أوامر مفيدة لإدارة البوت على الخادم:"
echo "  • عرض حالة البوت:      sudo systemctl status esim-bot"
echo "  • عرض السجلات المباشرة: sudo journalctl -u esim-bot -f"
echo "  • إعادة تشغيل البوت:   sudo systemctl restart esim-bot"
echo "  • إيقاف البوت:         sudo systemctl stop esim-bot"
echo "============================================================"
