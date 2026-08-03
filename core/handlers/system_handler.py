"""
Layer 2: 系统与容器状态监测层 (system_handler.py)
处理 /status, /docker, /top, /net, /systemctl 及 Docker 内联按键交互。
硬件或 Docker 故障被捕获在内部，绝不上抛崩溃主服务。
"""

import logging
import subprocess

import docker
import psutil
import telebot
from telebot import types

logger = logging.getLogger("SystemHandler")


def get_cpu_temp():
    try:
        temps = psutil.sensors_temperatures()
        for key in ["coretemp", "k10temp", "zenpower", "cpu_thermal", "acpitz"]:
            if temps.get(key):
                return temps[key][0].current
        for v in temps.values():
            if v:
                return v[0].current
    except Exception:
        pass
    return None


def register_system_handlers(bot, allowed_user_id: int):
    """注册 Layer 2 系统监测与 Docker 管理指令"""

    @bot.message_handler(commands=["status"])
    def handle_status(message):
        if message.from_user.id != allowed_user_id:
            return
        bot.send_chat_action(message.chat.id, "typing")
        try:
            cpu_usage = psutil.cpu_percent(interval=1)
            _load1, load5, _load15 = psutil.getloadavg()
            mem = psutil.virtual_memory()
            disk = psutil.disk_usage("/")
            temp = get_cpu_temp()

            temp_str = f"{temp:.1f}°C" if temp is not None else "N/A"
            msg = (
                "📊 <b>Linux 系统健康诊断</b>\n"
                "──────────────────────\n"
                f"🌡️ <b>CPU 温度</b>: {temp_str}\n"
                f"⚡ <b>CPU 使用率</b>: {cpu_usage}%\n"
                f"📈 <b>系统 5分钟负载</b>: {load5:.2f}\n"
                f"🧠 <b>内存占用</b>: {mem.percent}% ({mem.used // (1024**2)}MB / {mem.total // (1024**2)}MB)\n"
                f"💾 <b>系统磁盘 (/) 占用</b>: {disk.percent}% ({disk.free // (1024**3)}GB 剩余)\n"
                "──────────────────────\n"
                "✨ <i>系统状态监控正常</i>"
            )
            bot.send_message(message.chat.id, msg)
        except Exception as e:
            bot.send_message(message.chat.id, f"❌ 获取系统状态失败: {e}")

    @bot.message_handler(commands=["docker"])
    def handle_docker(message):
        if message.from_user.id != allowed_user_id:
            return
        bot.send_chat_action(message.chat.id, "typing")
        try:
            client = docker.from_env()
            containers = client.containers.list(all=True)
            markup = types.InlineKeyboardMarkup(row_width=2)

            summary = []
            for c in containers:
                status_icon = "🟢" if c.status == "running" else "🔴"
                summary.append(f"{status_icon} <b>{c.name}</b> ({c.status})")
                btn_log = types.InlineKeyboardButton(
                    f"📜 {c.name} 日志", callback_data=f"docker_log:{c.id[:12]}"
                )
                btn_restart = types.InlineKeyboardButton(
                    f"🔄 重启 {c.name}", callback_data=f"docker_restart:{c.id[:12]}"
                )
                markup.add(btn_log, btn_restart)

            msg = (
                f"🐳 <b>Docker 容器监控看板 ({len(containers)} 个)</b>\n──────────────────────\n"
                + "\n".join(summary)
            )
            bot.send_message(message.chat.id, msg, reply_markup=markup)
        except Exception as e:
            bot.send_message(message.chat.id, f"❌ 获取 Docker 容器状态失败: {e}")

    @bot.callback_query_handler(func=lambda call: call.data.startswith("docker_"))
    def handle_docker_callback(call):
        if call.from_user.id != allowed_user_id:
            return
        action, cid = call.data.split(":")
        try:
            client = docker.from_env()
            container = client.containers.get(cid)
            if action == "docker_log":
                bot.answer_callback_query(call.id, f"正在获取 {container.name} 日志...")
                logs = container.logs(tail=30).decode("utf-8", errors="replace")
                escaped_logs = telebot.formatting.escape_html(logs[-3000:])
                msg = f"📜 <b>{container.name} 最新 30 条日志</b>:\n<pre>{escaped_logs}</pre>"
                bot.send_message(call.message.chat.id, msg)
            elif action == "docker_restart":
                bot.answer_callback_query(call.id, f"正在重启 {container.name}...")
                container.restart()
                bot.send_message(
                    call.message.chat.id,
                    f"✅ 容器 <b>{container.name}</b> 已成功重启！",
                )
        except Exception as e:
            bot.send_message(call.message.chat.id, f"❌ Docker 操作失败: {e}")

    @bot.message_handler(commands=["top"])
    def handle_top(message):
        if message.from_user.id != allowed_user_id:
            return
        bot.send_chat_action(message.chat.id, "typing")
        try:
            # 预采样：首次调用 cpu_percent 返回 0.0，需先触发一次基线采集
            for p in psutil.process_iter():
                try:
                    p.cpu_percent()
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass
            import time as _time

            _time.sleep(0.5)

            processes = sorted(
                psutil.process_iter(["pid", "name", "cpu_percent", "memory_percent"]),
                key=lambda p: p.info["cpu_percent"] or 0,
                reverse=True,
            )[:5]
            lines = ["⚡ <b>进程资源占用 Top 5</b>\n──────────────────────"]
            for p in processes:
                lines.append(
                    f"• <b>{p.info['name']}</b> (PID: {p.info['pid']}) - CPU: {p.info['cpu_percent']}% | RAM: {p.info['memory_percent']:.1f}%"
                )
            bot.send_message(message.chat.id, "\n".join(lines))
        except Exception as e:
            bot.send_message(message.chat.id, f"❌ 获取进程 Top5 失败: {e}")

    @bot.message_handler(commands=["net"])
    def handle_net(message):
        if message.from_user.id != allowed_user_id:
            return
        bot.send_chat_action(message.chat.id, "typing")
        try:
            addrs = psutil.net_if_addrs()
            lines = ["🌐 <b>网络接口与 IP 状态</b>\n──────────────────────"]
            for iface, addr_list in addrs.items():
                ips = [a.address for a in addr_list if a.family.name == "AF_INET"]
                if ips:
                    lines.append(f"• <b>{iface}</b>: {', '.join(ips)}")
            bot.send_message(message.chat.id, "\n".join(lines))
        except Exception as e:
            bot.send_message(message.chat.id, f"❌ 获取网络状态失败: {e}")

    @bot.message_handler(commands=["systemctl"])
    def handle_systemctl(message):
        if message.from_user.id != allowed_user_id:
            return
        bot.send_chat_action(message.chat.id, "typing")
        try:
            svcs = [
                "tg-monitor.service",
                "tg-task-engine.service",
                "docker.service",
                "ssh.service",
            ]
            lines = ["⚙️ <b>核心 Systemd 服务健康度</b>\n──────────────────────"]
            for s in svcs:
                res = subprocess.run(
                    ["systemctl", "is-active", s], capture_output=True, text=True
                )
                status = res.stdout.strip()
                icon = "🟢" if status == "active" else "🔴"
                lines.append(f"{icon} <b>{s}</b>: {status}")
            bot.send_message(message.chat.id, "\n".join(lines))
        except Exception as e:
            bot.send_message(message.chat.id, f"❌ 获取 Systemctl 状态失败: {e}")

    return {
        "status": handle_status,
        "docker": handle_docker,
        "top": handle_top,
        "net": handle_net,
        "systemctl": handle_systemctl,
    }
