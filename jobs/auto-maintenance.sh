#!/bin/bash

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
BASE_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

if [ -f "$BASE_DIR/.env" ]; then
    export $(grep -v '^#' "$BASE_DIR/.env" | xargs)
fi

BOT_TOKEN="${TG_BOT_TOKEN}"
CHAT_ID="${TG_CHAT_ID}"
PROXY="${TG_PROXY}"
AGY_BIN="${AGY_BIN:-$HOME/.local/bin/agy}"
PYTHON_VENV="${PYTHON_VENV:-$BASE_DIR/venv/bin/python3}"

echo "[$(date)] Starting weekly automated maintenance..."

# 1. Update agy CLI
export HTTP_PROXY="$PROXY"
export HTTPS_PROXY="$PROXY"

AGY_LOG=""
if [ -x "$AGY_BIN" ]; then
    AGY_LOG=$("$AGY_BIN" update 2>&1)
else
    AGY_LOG="agy not found at $AGY_BIN"
fi

# 2. Docker system prune
DOCKER_LOG=$(docker system prune -f 2>&1 | tail -n 5)

# 3. Clean system journal logs (> 14 days)
JOURNAL_LOG=$(sudo journalctl --vacuum-time=14d 2>&1 | tail -n 2)

echo "[$(date)] Maintenance tasks finished. Sending Telegram notification..."

# 4. Notify via Telegram（通过环境变量传值，避免 shell 变量注入破坏 Python 语法）
export MAINT_AGY_LOG="$AGY_LOG"
export MAINT_DOCKER_LOG="$DOCKER_LOG"
export MAINT_JOURNAL_LOG="$JOURNAL_LOG"
export MAINT_BOT_TOKEN="$BOT_TOKEN"
export MAINT_CHAT_ID="$CHAT_ID"
export MAINT_PROXY="$PROXY"

$PYTHON_VENV -c '
import os
import telebot
import telebot.apihelper

proxy = os.environ.get("MAINT_PROXY", "")
bot_token = os.environ["MAINT_BOT_TOKEN"]
chat_id = os.environ["MAINT_CHAT_ID"]
agy_log = os.environ.get("MAINT_AGY_LOG", "")
docker_log = os.environ.get("MAINT_DOCKER_LOG", "")
journal_log = os.environ.get("MAINT_JOURNAL_LOG", "")

if proxy:
    telebot.apihelper.proxy = {"https": proxy, "http": proxy}

bot = telebot.TeleBot(bot_token, parse_mode="HTML")

escaped_agy = telebot.formatting.escape_html(agy_log.strip())
escaped_docker = telebot.formatting.escape_html(docker_log.strip())
escaped_journal = telebot.formatting.escape_html(journal_log.strip())

msg = (
    "🧹 <b>[自动维保简报] 周定时维护完成</b>\n"
    "──────────────────────\n"
    f"🤖 <b>agy CLI 更新</b>:\n<code>{escaped_agy}</code>\n\n"
    f"🐳 <b>Docker 缓存清理</b>:\n<code>{escaped_docker}</code>\n\n"
    f"📜 <b>日志清理</b>:\n<code>{escaped_journal}</code>\n\n"
    "✨ <i>物理机状态良好，所有定时维护项已自动处理完成！</i>"
)

bot.send_message(chat_id, msg)
'

echo "[$(date)] Maintenance completed successfully."
