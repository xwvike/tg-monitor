"""
Layer 1: 远程自救与快照控制系统 (rescue_handler.py)
最高指令优先级！零外部依赖（独立于 Docker 和 AGY）。
处理 /rescue, /backup, /backups, /restore 以及 '🚨 一键自救'。
"""

import logging
import os
import subprocess
import sys

sys.path.append(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
)
from core.tg_format import code_block, esc, send_html

MANAGE_BIN = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "../../bin/manage.sh")
)
logger = logging.getLogger("RescueHandler")


def register_rescue_handlers(bot, allowed_user_id: int):
    """注册 Layer 1 高优先级自救与快照指令"""

    @bot.message_handler(commands=["rescue", "fix"])
    def handle_rescue(message):
        if message.from_user.id != allowed_user_id:
            return
        bot.send_chat_action(message.chat.id, "typing")
        bot.reply_to(message, "🚑 正在运行系统自救与自愈诊断...")

        try:
            res = subprocess.run(
                [MANAGE_BIN, "rescue"], capture_output=True, text=True, timeout=60
            )
            output = res.stdout.strip() or res.stderr.strip() or "自救诊断处理完毕。"
            send_html(
                bot, message.chat.id, f"<b>[🚨 系统自救诊断结果]</b>\n{code_block(output)}"
            )
        except Exception as e:
            send_html(bot, message.chat.id, f"❌ 自救命令执行异常: {esc(e)}")

    @bot.message_handler(commands=["backup", "snapshot"])
    def handle_backup(message):
        if message.from_user.id != allowed_user_id:
            return
        bot.send_chat_action(message.chat.id, "typing")
        args = message.text.split()
        tag = args[1] if len(args) > 1 else "tg_manual"

        try:
            res = subprocess.run(
                [MANAGE_BIN, "backup", tag], capture_output=True, text=True, timeout=60
            )
            output = res.stdout.strip()
            send_html(
                bot, message.chat.id, f"<b>[📸 快照备份结果]</b>\n{code_block(output)}"
            )
        except Exception as e:
            send_html(bot, message.chat.id, f"❌ 快照备份执行失败: {esc(e)}")

    @bot.message_handler(commands=["backups", "snapshots"])
    def handle_backups_list(message):
        if message.from_user.id != allowed_user_id:
            return
        bot.send_chat_action(message.chat.id, "typing")

        try:
            res = subprocess.run(
                [MANAGE_BIN, "backups"], capture_output=True, text=True, timeout=30
            )
            output = res.stdout.strip()
            send_html(
                bot, message.chat.id, f"<b>[📜 历史备份快照列表]</b>\n{code_block(output)}"
            )
        except Exception as e:
            send_html(bot, message.chat.id, f"❌ 获取快照列表失败: {esc(e)}")

    @bot.message_handler(commands=["restore"])
    def handle_restore(message):
        if message.from_user.id != allowed_user_id:
            return
        bot.send_chat_action(message.chat.id, "typing")
        args = message.text.split()
        target = args[1] if len(args) > 1 else ""

        bot.send_message(
            message.chat.id, f"🔄 正在发起系统还原指令... ({esc(target) or '最新稳态'})"
        )

        try:
            cmd = [MANAGE_BIN, "restore"]
            if target:
                cmd.append(target)
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
            output = res.stdout.strip() or res.stderr.strip()
            send_html(
                bot, message.chat.id, f"<b>[🔄 恢复还原结果]</b>\n{code_block(output)}"
            )
        except Exception as e:
            send_html(bot, message.chat.id, f"❌ 快照还原失败: {esc(e)}")
