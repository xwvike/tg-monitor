import os
import threading

AGY_BIN = os.path.expanduser("~/.local/bin/agy")
BRAIN_DIR = os.path.expanduser("~/.gemini/antigravity-cli/brain")

MAX_TG_FILE_SIZE = 20 * 1024 * 1024

# 已下线的文件流水线留在 brain/ 里的一次性会话，/history 需要继续过滤掉它们
INTERNAL_MARKER = "[[TG-MONITOR-INTERNAL]]"
LEGACY_INTERNAL_SIGNATURES = (
    "你是文件处理命令 Planner",
    "你是一个专门用于生成文件处理命令的智能 Planner",
    "请判断：用户的核心意图是想利用系统能力对文件进行物理处理",
    "判断用户的核心意图：想对文件做物理处理",
)

user_buffers = {}
user_buffers_lock = threading.Lock()

conv_locks = {}
conv_locks_guard = threading.Lock()
