#!/usr/bin/env python3
"""
Layer 0: 核心通信微内核入口 (core/bot.py)
零外部复杂依赖！仅保留网络代理、Polling 长轮询与全局崩溃防护盖板。
按优先级依次挂载 Layer 1 (自救与快照) -> Layer 2 (系统监测) -> Layer 3 (AGY AI 交互)。
"""

import logging
import os
import sys

import telebot
from dotenv import load_dotenv
from telebot import apihelper, types

sys.path.append(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
load_dotenv(os.path.join(os.path.dirname(__file__), "../.env"))

import datetime

from core.tg_format import esc, send_html
from core.user_state import UserStateStore


def _get_dynamic_version():
    try:
        mtime = os.path.getmtime(__file__)
        return datetime.datetime.fromtimestamp(mtime).strftime("%Y.%m.%d-%H%M")
    except Exception:
        return datetime.datetime.now().strftime("%Y.%m.%d")


VERSION = _get_dynamic_version()
TOKEN = os.getenv("TG_BOT_TOKEN", "")
ALLOWED_USER_ID = int(os.getenv("TG_CHAT_ID", "0"))
AGY_BIN = os.path.expanduser("~/.local/bin/agy")
STATE_FILE = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "../config/user_states.json")
)

logging.basicConfig(
    level=logging.INFO, format="[%(asctime)s] [%(levelname)s] %(message)s"
)
logger = logging.getLogger("BotCore")

# 设置网络代理以访问 Telegram API
PROXY_URL = os.getenv("TG_PROXY", "")
if PROXY_URL:
    apihelper.proxy = {"http": PROXY_URL, "https": PROXY_URL}

bot = telebot.TeleBot(TOKEN, parse_mode="HTML")

# 状态持久化：原子写 + 线程锁，实现见 core/user_state.py
state_store = UserStateStore(STATE_FILE)


def load_user_states():
    state_store.load()


def save_user_states():
    state_store.save()


def get_user_state(user_id):
    return state_store.get(user_id)


def get_main_keyboard(user_id):
    """主面板 Reply 键盘视图"""
    markup = types.ReplyKeyboardMarkup(row_width=3, resize_keyboard=True)
    uid_str = str(user_id)
    in_chat = state_store.all.get(uid_str, {}).get("in_chat", False)

    if in_chat:
        btn_exit = types.KeyboardButton("🚪 退出对话")
        btn_new = types.KeyboardButton("🆕 新建对话")
        btn_hist = types.KeyboardButton("📜 历史会话")
        markup.add(btn_exit, btn_new, btn_hist)
    else:
        btn_status = types.KeyboardButton("📊 系统状态")
        btn_docker = types.KeyboardButton("🐳 Docker 容器")
        btn_top = types.KeyboardButton("⚡ 资源 Top5")
        btn_net = types.KeyboardButton("🌐 网络卡状态")
        btn_svc = types.KeyboardButton("⚙️ 服务状态")
        btn_rescue = types.KeyboardButton("🚨 一键自救")
        btn_chat = types.KeyboardButton("💬 进入 AGY 对话")
        markup.add(
            btn_status, btn_docker, btn_top, btn_net, btn_svc, btn_rescue, btn_chat
        )
    return markup


def init_commands():
    """向 Telegram API 注册斜杠快捷指令菜单"""
    try:
        bot.delete_my_commands()
        bot.set_my_commands(
            [
                types.BotCommand("rescue", "🚨 紧急诊断并恢复最新稳态快照"),
                types.BotCommand("backup", "📸 创建当前系统全量打包快照"),
                types.BotCommand("backups", "📜 查看历史备份快照列表"),
                types.BotCommand("restore", "🔄 还原指定或最新稳态快照"),
                types.BotCommand("status", "📊 查看 CPU/内存/磁盘/温度"),
                types.BotCommand("docker", "🐳 监控与管理 Docker 容器"),
                types.BotCommand("top", "⚡ 查看资源占用 Top5 进程"),
                types.BotCommand("net", "🌐 查看网络卡接口与 IP"),
                types.BotCommand("chat", "💬 进入 AGY 沉浸对话模式"),
                types.BotCommand("model", "🤖 切换 AGY AI 模型 (Flash/Pro/Claude/GPT)"),
                types.BotCommand("effort", "⚡ 切换思考推理深度 (Low/Medium/High)"),
                types.BotCommand("voice", "🔊 开启/关闭短复自动发语音"),
                types.BotCommand("settings", "⚙️ 查看 AGY 当前配置与参数"),
                types.BotCommand("new", "🆕 新建空白对话"),
                types.BotCommand("history", "📜 查看并恢复历史会话"),
                types.BotCommand("exit", "🚪 退出对话模式"),
                types.BotCommand("systemctl", "⚙️ 检查核心 Systemd 服务状态"),
                types.BotCommand("version", "ℹ️ 查看版本与沙箱探针状态"),
                types.BotCommand("help", "🤖 显示帮助与菜单"),
            ]
        )
    except Exception as e:
        logger.warning(f"初始化注册 Telegram 斜杠菜单失败: {e}")


# ------------------------------------------------------------------------------
# 挂载 Handler 优先级分层（带隔离保护）
# L1/L2 为基础设施层，必须注册成功。L3 若导入炸了只降级不崩溃。
# ------------------------------------------------------------------------------

# Layer 1: 高优先级远程自救与快照指令
from handlers.rescue_handler import register_rescue_handlers

rescue_handlers = register_rescue_handlers(bot, ALLOWED_USER_ID)

# Layer 2: 系统与容器状态监测指令
from handlers.system_handler import register_system_handlers

system_handlers = register_system_handlers(bot, ALLOWED_USER_ID)

# Layer 3: AGY AI 对话与会话管理
# L3 依赖链最重（telegramify_markdown, file_pipeline, stt, tts 等），
# 如果这里炸了（比如某个第三方包没装、Python 版本不兼容），
# 不应该把 L0/L1/L2 一起拖下水。降级后自救和系统监控仍可用。
_l3_available = False
dispatch_text_fn = None
render_history_fn = None
agy_button_handlers = None
try:
    from handlers.agy_handler import register_agy_handlers

    dispatch_text_fn, render_history_fn, agy_button_handlers = register_agy_handlers(
        bot, ALLOWED_USER_ID, get_user_state, save_user_states, get_main_keyboard
    )
    _l3_available = True
except Exception as e:
    logger.error(f"Layer 3 (AGY) 挂载失败，已降级运行: {e}")

# ------------------------------------------------------------------------------
# 全局常规指令与分发入口
# ------------------------------------------------------------------------------


@bot.message_handler(commands=["start", "help"])
def send_welcome(message):
    if message.from_user.id != ALLOWED_USER_ID:
        return
    kb = get_main_keyboard(message.from_user.id)
    help_text = (
        f"🤖 <b>Linux 系统 Telegram 智能监控与 AGY 智能体 (v{VERSION})</b>\n"
        "──────────────────────\n"
        "<b>🚨 远程自救与快照:</b>\n"
        "• <b>/rescue</b>: 紧急诊断并恢复最新稳态快照\n"
        "• <b>/backup</b>: 手动创建系统全量打包快照\n"
        "• <b>/backups</b>: 查看历史备份还原点列表\n"
        "• <b>/restore</b>: 还原指定或最新稳态快照\n\n"
        "<b>📊 系统状态监控:</b>\n"
        "• <b>/status</b>: 查看 CPU/内存/磁盘/温度\n"
        "• <b>/docker</b>: 监控 Docker 容器运行状态并支持看日志/重启\n"
        "• <b>/top</b>: 查看 CPU/内存 占用 Top5 进程\n"
        "• <b>/net</b>: 查看网卡与 IP 状态\n"
        "• <b>/systemctl</b>: 检查 Systemd 核心服务健康度\n\n"
        "<b>💬 AGY AI 智能体控制:</b>\n"
        "• <b>/chat</b>: 进入沉浸式 AGY 对话模式\n"
        "• <b>/model</b>: 切换 AI 模型 (Flash/Pro/Claude/GPT)\n"
        "• <b>/effort</b>: 切换思考推理深度 (Low/Medium/High)\n"
        "• <b>/settings</b>: 查看 AGY 会话配置与参数\n"
        "• <b>/history</b>: 查看并恢复以往的对话记录\n"
        "• <b>/new</b>: 开启新的空白对话"
    )
    if not _l3_available:
        help_text += "\n\n⚠️ <b>AGY 层当前不可用，仅自救与系统监控功能正常。</b>"
    bot.send_message(message.chat.id, help_text, reply_markup=kb)


@bot.message_handler(commands=["version"])
def show_version(message):
    if message.from_user.id != ALLOWED_USER_ID:
        return
    ver_msg = (
        f"ℹ️ <b>Telegram 监控与 AGY 机器人版本信息</b>\n"
        "──────────────────────\n"
        f"📌 <b>当前运行版本</b>: v{VERSION}\n"
        f"🛡️ <b>模块化分层微内核</b>: Layer 0 -> Layer 3\n"
        f"⚡ <b>底层 AGY 引擎</b>: Google Antigravity\n"
        f"⚙️ <b>服务状态</b>: 生产环境正常运行中"
    )
    if not _l3_available:
        ver_msg += "\n⚠️ <b>Layer 3 (AGY) 降级中</b>"
    bot.send_message(message.chat.id, ver_msg)


@bot.message_handler(func=lambda message: True, content_types=["text"])
def global_text_router(message):
    if message.from_user.id != ALLOWED_USER_ID:
        return

    text = message.text.strip()
    uid = message.from_user.id

    # L2 底部 Reply 按钮快捷匹配
    button_to_handler = {
        "📊 系统状态": system_handlers["status"],
        "🐳 Docker 容器": system_handlers["docker"],
        "⚡ 资源 Top5": system_handlers["top"],
        "🌐 网络卡状态": system_handlers["net"],
        "⚙️ 服务状态": system_handlers["systemctl"],
    }
    if text in button_to_handler:
        button_to_handler[text](message)
        return

    # L1 自救按钮 → 委托给 rescue_handler 的统一实现
    if text == "🚨 一键自救":
        rescue_handlers["rescue"](message)
        return

    # L3 对话模式按钮 → 委托给 agy_handler
    if _l3_available and agy_button_handlers is not None:
        l3_buttons = {
            "💬 进入 AGY 对话": "chat",
            "🚪 退出对话": "exit",
            "🆕 新建对话": "new",
            "📜 历史会话": "history",
        }
        if text in l3_buttons:
            agy_button_handlers[l3_buttons[text]](message)
            return
    elif text in ("💬 进入 AGY 对话", "🚪 退出对话", "🆕 新建对话", "📜 历史会话"):
        send_html(bot, message.chat.id, f"⚠️ AGY 层当前不可用: {esc(text)}")
        return

    # 路由给 Layer 3 AGY 处理器（若当前处于 chat 模式）
    if _l3_available and dispatch_text_fn is not None:
        handled = dispatch_text_fn(message)
    else:
        handled = False
    if not handled and not text.startswith("/"):
        bot.send_message(
            message.chat.id,
            "💡 当前处于控制面板模式。点击【💬 进入 AGY 对话】开启 AI 对话，或使用 /help 查看所有可用命令。",
            reply_markup=get_main_keyboard(uid),
        )


# ------------------------------------------------------------------------------
# 主入口 & --test-sandbox 探针处理
# ------------------------------------------------------------------------------

if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--test-preflight":
        from core.preflight import run as run_preflight

        print(f"=== 🧪 启动分层微内核预检流水线与四级校验 (v{VERSION}) ===")
        ok = run_preflight(bot=bot, agy_bin=AGY_BIN)
        sys.exit(0 if ok else 1)

    load_user_states()
    init_commands()
    logger.info(f"Telegram Monitoring & AGY Layered Bot (v{VERSION}) 正在启动轮询...")
    bot.infinity_polling(timeout=20, long_polling_timeout=10)
