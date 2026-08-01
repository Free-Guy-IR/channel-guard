#!/bin/bash
# Channel Guard installer.
# Run as root on a fresh Ubuntu/Debian server, from inside the cloned repo:
#   sudo bash install.sh
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
  echo "Run this as root (sudo bash install.sh)" >&2
  exit 1
fi

INSTALL_DIR=/opt/channel-guard
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "== Installing OS packages =="
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip curl rsync openssl

INSTALL_MYSQL=Y
if [ -z "${CI:-}" ]; then
  read -rp "Install MySQL server locally? [Y/n] " INSTALL_MYSQL
  INSTALL_MYSQL=${INSTALL_MYSQL:-Y}
fi
if [[ "$INSTALL_MYSQL" =~ ^[Yy] ]]; then
  apt-get install -y -qq mysql-server
  systemctl enable --now mysql
fi

echo "== Copying app to $INSTALL_DIR =="
mkdir -p "$INSTALL_DIR"
rsync -a --exclude='.venv' --exclude='.git' --exclude='data' "$SCRIPT_DIR"/ "$INSTALL_DIR"/
mkdir -p "$INSTALL_DIR/data"

id -u channelguard >/dev/null 2>&1 || useradd --system --no-create-home channelguard

echo "== Python venv + dependencies =="
python3 -m venv "$INSTALL_DIR/.venv"
"$INSTALL_DIR/.venv/bin/pip" install -q --upgrade pip
"$INSTALL_DIR/.venv/bin/pip" install -q -r "$INSTALL_DIR/requirements.txt"

echo
echo "== Configuration =="
if [ -f "$INSTALL_DIR/config.json" ]; then
  echo "config.json already exists at $INSTALL_DIR/config.json - leaving it as-is."
else
  read -rp "Admin bot token (BotFather token for the bot that posts reports/serves the panel): " BOT_TOKEN
  while true; do
    read -rp "Admin numeric Telegram chat ID (where reports get sent): " ADMIN_CHAT_ID
    [[ "$ADMIN_CHAT_ID" =~ ^-?[0-9]+$ ]] && break
    echo "That doesn't look like a number, try again."
  done
  while true; do
    read -rp "Channel numeric ID (e.g. -1001234567890 - the bot must be an admin of this channel): " CHANNEL_ID
    [[ "$CHANNEL_ID" =~ ^-?[0-9]+$ ]] && break
    echo "That doesn't look like a number, try again."
  done
  read -rp "Channel display name (for the dashboard, any text): " CHANNEL_NAME
  read -rp "Sales bot API base URL (leave blank if you don't have one): " SALES_API_URL
  read -rp "Sales bot API token (leave blank if none): " SALES_API_TOKEN
  read -rp "Public URL this panel will be reachable at, including port (e.g. https://example.com:900): " PUBLIC_URL

  PANEL_PASSWORD="$(openssl rand -base64 18 | tr -dc 'a-zA-Z0-9' | head -c 20)"
  ADMIN_PATH="$(openssl rand -hex 12)"
  MYSQL_PASSWORD="$(openssl rand -base64 24 | tr -dc 'a-zA-Z0-9' | head -c 28)"

  if [[ "$INSTALL_MYSQL" =~ ^[Yy] ]]; then
    mysql -e "CREATE DATABASE IF NOT EXISTS channel_guard_main;"
    mysql -e "CREATE USER IF NOT EXISTS 'channelguard_db'@'localhost' IDENTIFIED BY '${MYSQL_PASSWORD}';"
    mysql -e "GRANT ALL PRIVILEGES ON channel_guard_main.* TO 'channelguard_db'@'localhost';"
    mysql -e "FLUSH PRIVILEGES;"
  else
    echo "Skipped local MySQL setup - create a 'channel_guard_main' database and a user for it yourself,"
    echo "then edit the \"mysql\" block in $INSTALL_DIR/config.json to match."
  fi

  export BOT_TOKEN ADMIN_CHAT_ID CHANNEL_ID CHANNEL_NAME SALES_API_URL SALES_API_TOKEN PUBLIC_URL
  export PANEL_PASSWORD ADMIN_PATH MYSQL_PASSWORD
  python3 - "$INSTALL_DIR/config.example.json" "$INSTALL_DIR/config.json" <<'PYEOF'
import json, os, sys

data = json.load(open(sys.argv[1], encoding="utf-8"))
data["panel_password"] = os.environ["PANEL_PASSWORD"]
data["admin_path"] = os.environ["ADMIN_PATH"]
data["public_site_url"] = os.environ["PUBLIC_URL"]
data["mysql"]["password"] = os.environ["MYSQL_PASSWORD"]
ch = data["channels"][0]
ch["admin_bot_token"] = os.environ["BOT_TOKEN"]
ch["admin_chat_id"] = int(os.environ["ADMIN_CHAT_ID"])
ch["channel_id"] = int(os.environ["CHANNEL_ID"])
ch["name"] = os.environ.get("CHANNEL_NAME") or "My Channel"
ch["sales_api_base_url"] = os.environ.get("SALES_API_URL", "")
ch["sales_api_token"] = os.environ.get("SALES_API_TOKEN", "")

json.dump(data, open(sys.argv[2], "w", encoding="utf-8"), indent=2, ensure_ascii=False)
PYEOF

  echo "Wrote $INSTALL_DIR/config.json"
fi

chown -R channelguard:channelguard "$INSTALL_DIR"

echo "== systemd service =="
cp "$INSTALL_DIR/channel-guard.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now channel-guard
sleep 2
systemctl --no-pager status channel-guard | head -8

ADMIN_PATH_SHOWN="$(python3 -c "import json;print(json.load(open('$INSTALL_DIR/config.json'))['admin_path'])" 2>/dev/null || echo "<see config.json>")"
PUBLIC_URL_SHOWN="$(python3 -c "import json;print(json.load(open('$INSTALL_DIR/config.json'))['public_site_url'])" 2>/dev/null || echo "<see config.json>")"

echo
echo "== Done =="
echo "Panel:  ${PUBLIC_URL_SHOWN}/${ADMIN_PATH_SHOWN}/"
if [ -n "${PANEL_PASSWORD:-}" ]; then
  echo "Password: ${PANEL_PASSWORD}   (also saved in $INSTALL_DIR/config.json - save this now)"
fi
echo "Logs:   journalctl -u channel-guard -f"
echo "Config: $INSTALL_DIR/config.json"
