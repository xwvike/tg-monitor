import glob
import json
import logging
import os
import re
import shutil
import threading

from core.file_pipeline import INLINE_TEXT_MAX_CHARS, TEXT_EXTS, AUDIO_EXTS, VIDEO_EXTS, INTERNAL_MARKER
from core.run_archive import prune, archive_root
from core.tg_format import esc, send_html, code_block
from core.handlers.agy.constants import BRAIN_DIR, LEGACY_INTERNAL_SIGNATURES, conv_locks, conv_locks_guard, WORKSPACE_ROOT

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

def _send_product(bot, chat_id, reply_to, path):
    ext = os.path.splitext(path)[1].lower()

    if ext in TEXT_EXTS:
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                content = fh.read().strip()
        except Exception:
            content = ""
        if content and len(content) <= INLINE_TEXT_MAX_CHARS:
            send_html(
                bot, chat_id,
                f"📝 <b>{esc(os.path.basename(path))}</b>\n{code_block(content)}",
                reply_to_message_id=reply_to,
            )
            return

    with open(path, "rb") as fh:
        if ext == ".gif":
            bot.send_chat_action(chat_id, "upload_document")
            bot.send_document(
                chat_id, fh, reply_to_message_id=reply_to,
                disable_content_type_detection=True,
            )
        elif ext in VIDEO_EXTS:
            bot.send_chat_action(chat_id, "upload_video")
            bot.send_video(chat_id, fh, reply_to_message_id=reply_to)
        elif ext in AUDIO_EXTS:
            bot.send_chat_action(chat_id, "upload_audio")
            bot.send_audio(chat_id, fh, reply_to_message_id=reply_to)
        else:
            bot.send_chat_action(chat_id, "upload_document")
            bot.send_document(chat_id, fh, reply_to_message_id=reply_to)

def sweep_workspaces():
    protected = os.path.abspath(archive_root(WORKSPACE_ROOT))
    removed = 0
    for sub in ("in", "out"):
        root = os.path.join(WORKSPACE_ROOT, sub)
        if not os.path.isdir(root):
            continue
        for name in os.listdir(root):
            path = os.path.join(root, name)
            if not os.path.isdir(path):
                continue
            if os.path.abspath(path).startswith(protected + os.sep):
                continue
            try:
                shutil.rmtree(path)
                removed += 1
            except Exception as e:
                logger.warning(f"清理遗留工作区 {path} 失败: {e}")
    if removed:
        logger.info(f"🧹 启动清扫：已回收 {removed} 个遗留文件工作区")
    try:
        prune(archive_root(WORKSPACE_ROOT))
    except Exception as e:
        logger.warning(f"启动时回收归档失败: {e}")

def _cleanup_dirs(dirs):
    for d in dirs or []:
        if d and os.path.exists(d):
            try:
                shutil.rmtree(d)
            except Exception as e:
                logger.warning(f"清理工作区 {d} 失败: {e}")
