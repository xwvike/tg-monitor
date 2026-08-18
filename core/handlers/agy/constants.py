import os
import threading

AGY_BIN = os.path.expanduser("~/.local/bin/agy")
BRAIN_DIR = os.path.expanduser("~/.gemini/antigravity-cli/brain")

_PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
WORKSPACE_ROOT = os.getenv("TG_WORKSPACE_ROOT", os.path.join(_PROJECT_DIR, "workspace"))

MAX_TG_FILE_SIZE = 20 * 1024 * 1024
MEDIA_GROUP_WINDOW = 2.5
FILE_CAPTION_WINDOW = 3.5
TEXT_ABSORB_MAX_AGE = 3.0

TG_PHOTO_NOTICE = (
    "ℹ️ 这次的图片是以「<b>图片</b>」方式发送的 —— Telegram 在上传时已经把它"
    "转成 JPEG 并缩小了尺寸，我处理的是这份已经压过一轮的副本。\n"
    "要基于原图处理（保留 PNG、原始分辨率与画质），发送时<b>取消勾选「压缩」</b>，"
    "或直接用「<b>文件</b>」方式发送。"
)

LEGACY_INTERNAL_SIGNATURES = (
    "你是文件处理命令 Planner",
    "你是一个专门用于生成文件处理命令的智能 Planner",
    "请判断：用户的核心意图是想利用系统能力对文件进行物理处理",
    "判断用户的核心意图：想对文件做物理处理",
)

user_buffers = {}
user_buffers_lock = threading.Lock()

file_batches = {}
file_batches_lock = threading.Lock()

conv_locks = {}
conv_locks_guard = threading.Lock()
