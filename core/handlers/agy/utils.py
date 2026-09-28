import glob
import json
import logging
import os
import re
import shutil
import subprocess
import threading

from core.handlers.agy.constants import (
    AGY_BIN,
    BRAIN_DIR,
    INTERNAL_MARKER,
    LEGACY_INTERNAL_SIGNATURES,
    conv_locks,
    conv_locks_guard,
)

logger = logging.getLogger("AGYHandler")

def _get_conv_lock(user_id):
    with conv_locks_guard:
        if user_id not in conv_locks:
            conv_locks[user_id] = threading.Lock()
        return conv_locks[user_id]

def _is_internal_conversation(first_msg):
    if INTERNAL_MARKER in first_msg:
        return True
    return any(sig in first_msg for sig in LEGACY_INTERNAL_SIGNATURES)

def _clean_preview(raw):
    text = re.sub(r"<ADDITIONAL_METADATA>.*?</ADDITIONAL_METADATA>", "", raw, flags=re.DOTALL)
    text = re.sub(r"<USER_SETTINGS_CHANGE>.*?</USER_SETTINGS_CHANGE>", "", text, flags=re.DOTALL)
    text = re.sub(r"</?USER_REQUEST>", "", text)
    return " ".join(text.split()) or "（新对话或空记录）"

def get_brain_conversations():
    brain_dirs = glob.glob(os.path.join(BRAIN_DIR, "*"))
    conversations = []
    for d in brain_dirs:
        cid = os.path.basename(d)
        log_file = os.path.join(d, ".system_generated", "logs", "transcript.jsonl")
        if not os.path.exists(log_file):
            continue

        first_msg = "（新对话或空记录）"
        try:
            with open(log_file, "r", encoding="utf-8") as f:
                for line in f:
                    if '"type":"USER_INPUT"' in line:
                        data = json.loads(line)
                        first_msg = data.get("content", first_msg)
                        break
        except Exception:
            pass

        if _is_internal_conversation(first_msg):
            continue

        conversations.append((cid, _clean_preview(first_msg), os.path.getmtime(log_file)))

    conversations.sort(key=lambda x: x[2], reverse=True)
    return conversations

def _cleanup_dirs(dirs):
    for d in dirs or []:
        if d and os.path.exists(d):
            try:
                shutil.rmtree(d)
            except Exception as e:
                logger.warning(f"清理工作区 {d} 失败: {e}")

_UNSAFE_NAME_CHARS = re.compile(r"[^\w.\-]", re.UNICODE)


def safe_filename(name, fallback="file"):
    """把外部提供的文件名收敛成 shell 安全的形式。

    agy 以 --dangerously-skip-permissions 运行，附件的绝对路径会被它原样写进
    自己执行的命令。转发来的文件名里的 `;` `$()` 反引号等会被 shell 解释，
    os.path.basename() 只挡路径穿越，不挡元字符。
    """
    name = os.path.basename(str(name or "")).strip()
    stem, ext = os.path.splitext(name)

    ext = "." + _UNSAFE_NAME_CHARS.sub("", ext.lstrip("."))[:16] if ext else ""
    stem = _UNSAFE_NAME_CHARS.sub("_", stem)[:80].strip("._-")

    if not stem:
        stem = fallback
    return f"{stem}{ext if ext != '.' else ''}"


def agy_env():
    """调用 agy 的环境变量：代理取自 .env 的 TG_PROXY，并确保 ~/.local/bin 在 PATH 中。"""
    env = os.environ.copy()
    proxy = os.getenv("TG_PROXY", "").strip()
    if proxy:
        env["HTTP_PROXY"] = proxy
        env["HTTPS_PROXY"] = proxy
        env["http_proxy"] = proxy
        env["https_proxy"] = proxy
    env.setdefault("PATH", "/usr/local/bin:/usr/bin:/bin")
    env["PATH"] = os.path.expanduser("~/.local/bin") + ":" + env["PATH"]
    return env


def list_agy_models():
    """实时读取 `agy models`，返回 [(model_id, 显示名), ...]。"""
    res = subprocess.run(
        [AGY_BIN, "models"], capture_output=True, text=True, timeout=30, env=agy_env()
    )
    models = []
    for line in res.stdout.splitlines():
        model_id, sep, label = line.partition("\t")
        if sep and model_id.strip():
            models.append((model_id.strip(), label.strip() or model_id.strip()))
    if res.returncode != 0 or not models:
        detail = (res.stderr or res.stdout).strip().splitlines()
        raise RuntimeError(detail[-1] if detail else f"agy models 退出码 {res.returncode}")
    return models

