#!/usr/bin/env bash
set -e

echo "============================================================"
echo "   Telegram eSIM Store Bot (Arabic) - 24/7 VPS Installer    "
echo "============================================================"
echo ""

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_DIR"

# 1. Interactive Configuration Prompts
read -rp "1) Enter Telegram BOT_TOKEN (from @BotFather): " BOT_TOKEN
while [[ -z "$BOT_TOKEN" ]]; do
    read -rp "   BOT_TOKEN is required! Enter BOT_TOKEN: " BOT_TOKEN
done

read -rp "2) Enter Owner Telegram numeric ADMIN_ID (from @userinfobot): " ADMIN_ID
while [[ ! "$ADMIN_ID" =~ ^[0-9]+$ ]]; do
    read -rp "   ADMIN_ID must be numbers only! Enter ADMIN_ID: " ADMIN_ID
done

read -rp "3) Enter Support Telegram Username (without @, e.g. MySupport): " SUPPORT_USERNAME
SUPPORT_USERNAME="${SUPPORT_USERNAME#@}"
SUPPORT_USERNAME="${SUPPORT_USERNAME:-Support}"

read -rp "4) Enter your Binance Pay ID (BINANCE_ID): " BINANCE_ID
BINANCE_ID="${BINANCE_ID:-Not_Set}"

read -rp "5) Enter your InstaPay EG Address/Phone (INSTAPAY_ADDRESS): " INSTAPAY_ADDRESS
INSTAPAY_ADDRESS="${INSTAPAY_ADDRESS:-Not_Set}"

read -rp "6) Enter Default Store Currency [USD / EGP] (default: USD): " CURRENCY
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
echo "[OK] Created .env file successfully."

# 3. Install Python & Virtual Environment on VPS
echo "[..] Installing Python3 and required packages..."
if command -v apt-get >/dev/null 2>&1; then
    sudo apt-get update -y
    sudo apt-get install -y python3 python3-venv python3-pip sqlite3 locales
elif command -v yum >/dev/null 2>&1; then
    sudo yum install -y python3 python3-pip sqlite
fi

if [[ ! -d "$PROJECT_DIR/venv" ]]; then
    python3 -m venv "$PROJECT_DIR/venv"
fi

"$PROJECT_DIR/venv/bin/pip" install --upgrade pip
"$PROJECT_DIR/venv/bin/pip" install -r "$PROJECT_DIR/requirements.txt"
echo "[OK] Installed Python dependencies inside venv."

# 4. Configure systemd service with explicit UTF-8 environment for 24/7 execution
SERVICE_FILE="/etc/systemd/system/esim-bot.service"
CURRENT_USER="$(whoami)"

echo "[..] Configuring systemd service (esim-bot.service) for 24/7 auto-restart..."
sudo bash -c "cat > $SERVICE_FILE" <<EOF
[Unit]
Description=Arabic Telegram eSIM Store Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${CURRENT_USER}
WorkingDirectory=${PROJECT_DIR}
Environment="LANG=C.UTF-8"
Environment="LC_ALL=C.UTF-8"
Environment="PYTHONUTF8=1"
Environment="PYTHONIOENCODING=utf-8"
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
echo "[SUCCESS] Bot is now running 24/7 and will auto-start on VPS reboot!"
echo "Useful commands:"
echo "  sudo systemctl status esim-bot"
echo "  sudo journalctl -u esim-bot -f"
echo "  sudo systemctl restart esim-bot"
echo "============================================================"