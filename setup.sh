#!/usr/bin/env bash
# One-shot setup: venv, deps, .env, systemd user service.
set -euo pipefail
cd "$(dirname "$0")"
DIR="$(pwd)"

python3 -m venv .venv
.venv/bin/pip install -q -r requirements.txt

if [ ! -f .env ]; then
  read -rsp "Bot token (from @BotFather): " TOKEN; echo
  echo "Token check:"
  curl -fsS "https://api.telegram.org/bot${TOKEN}/getMe" | python3 -c "import sys,json; print('  OK @'+json.load(sys.stdin)['result']['username'])"

  echo "Now open your bot in Telegram and send it /start, then press Enter."
  read -r _
  CHAT_ID=$(curl -fsS "https://api.telegram.org/bot${TOKEN}/getUpdates" | python3 -c "
import sys,json
r=json.load(sys.stdin)['result']
print(r[-1]['message']['chat']['id'] if r else '')")
  [ -n "$CHAT_ID" ] || { echo "No message found. Send /start to the bot and rerun."; exit 1; }
  echo "Chat ID: $CHAT_ID"

  read -rp "eclass username: " EUSER
  read -rsp "eclass password: " EPASS; echo

  cat > .env <<EOF
BOT_TOKEN=${TOKEN}
CHAT_ID=${CHAT_ID}
ECLASS_USERNAME=${EUSER}
ECLASS_PASSWORD=${EPASS}
POLL_MINUTES=20
EOF
  chmod 600 .env
fi

mkdir -p ~/.config/systemd/user
cat > ~/.config/systemd/user/eclass-bot.service <<EOF
[Unit]
Description=eclass Telegram bot
After=network-online.target

[Service]
WorkingDirectory=${DIR}
ExecStart=${DIR}/.venv/bin/python ${DIR}/bot.py
Restart=always
RestartSec=10

[Install]
WantedBy=default.target
EOF

systemctl --user daemon-reload
systemctl --user enable --now eclass-bot
loginctl enable-linger "$USER" 2>/dev/null || true
echo "Running. Logs: journalctl --user -u eclass-bot -f"
