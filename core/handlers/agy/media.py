import logging
import os
import threading
import time

from core.handlers.agy.constants import MAX_TG_FILE_SIZE, MEDIA_GROUP_WINDOW, FILE_CAPTION_WINDOW, TEXT_ABSORB_MAX_AGE, WORKSPACE_ROOT, file_batches, file_batches_lock, user_buffers, user_buffers_lock
from core.handlers.agy.tasks import run_file_task
from core.handlers.agy.utils import _cleanup_dirs
from core.tg_format import esc

logger = logging.getLogger("AGYHandler")

def register_media_handlers(bot, allowed_user_id, get_user_state_fn, save_user_states_fn):

    def _reject_oversize(message, size_bytes):
        size_mb = round(size_bytes / (1024 * 1024), 2)
        bot.send_message(
            message.chat.id,
            f"⚠️ <b>文件过大 ({size_mb} MB)</b>\n"
            f"──────────────────────\n"
            f"Telegram 官方标准 Bot API 限制单文件接收不能超过 <b>20MB</b>。\n"
            f"💡 <b>破局方案</b>：您可以在文字中附带文件的外部下载链接，让 AGY 自己通过 <code>wget</code> 或 <code>curl</code> 下载处理。",
            parse_mode="HTML",
            reply_to_message_id=message.message_id,
        )

    def _launch_file_task(message, workspace_in, workspace_out, caption,
                          context="", tg_photo=False):
        try:
            file_paths = sorted(
                os.path.join(workspace_in, f)
                for f in os.listdir(workspace_in)
                if os.path.isfile(os.path.join(workspace_in, f))
            )
        except OSError:
            file_paths = []

        if not file_paths:
            logger.warning(f"工作区 {workspace_in} 为空，放弃本次任务")
            _cleanup_dirs([workspace_in, workspace_out])
            return

        st = get_user_state_fn(message.from_user.id)
        model = st.get("model", "gemini-3.6-flash-high")

        instruction = caption or context
        if context and caption:
            instruction = f"{caption}\n\n（该文件为转发内容，原始附带说明：{context}）"

        def job():
            logger.info(
                f"文件任务 → 交给 agy | 指令={instruction!r} | "
                f"files={[os.path.basename(p) for p in file_paths]}"
            )
            run_file_task(
                bot, message, file_paths, workspace_in, workspace_out,
                instruction, model, tg_photo,
            )

        threading.Thread(target=job).start()

    def _batch_launch(uid):
        with file_batches_lock:
            batch = file_batches.get(uid)
            if not batch or batch["fired"] or batch["downloading"] > 0:
                return
            batch["fired"] = True
            file_batches.pop(uid, None)
            if batch["timer"]:
                batch["timer"].cancel()
        _launch_file_task(
            batch["message"], batch["in"], batch["out"],
            batch["caption"], batch["context"], batch["tg_photo"],
        )

    def _arm_batch_locked(uid):
        batch = file_batches.get(uid)
        if not batch or batch["fired"] or batch["downloading"] > 0:
            return

        elapsed = time.time() - batch["last_file_at"]
        group_wait = (
            max(0.0, MEDIA_GROUP_WINDOW - elapsed) if batch["group_id"] else 0.0
        )
        caption_wait = (
            0.0 if batch["caption"] else max(0.0, FILE_CAPTION_WINDOW - elapsed)
        )
        delay = max(0.05, group_wait, caption_wait)

        if batch["timer"]:
            batch["timer"].cancel()
        batch["timer"] = threading.Timer(delay, _batch_launch, args=(uid,))
        batch["timer"].start()

    def _claim_recent_text(user_id):
        with user_buffers_lock:
            buf = user_buffers.get(user_id)
            if not buf:
                return ""
            if time.time() - buf.get("created_at", 0) > TEXT_ABSORB_MAX_AGE:
                return ""
            if buf["timer"]:
                buf["timer"].cancel()
            del user_buffers[user_id]
            return "\n".join(buf["messages"]).strip()

    def claim_batch(message):
        uid = message.from_user.id
        text = (message.text or "").strip()
        if not text:
            return False
        with file_batches_lock:
            batch = file_batches.get(uid)
            if not batch or batch["fired"]:
                return False
            batch["caption"] = text
            logger.info(f"文本 {text!r} 认领了待定的文件批次")
            _arm_batch_locked(uid)
        return True

    def _ingest_file(message, file_id, preferred_name, known_size=0):
        if known_size and known_size > MAX_TG_FILE_SIZE:
            _reject_oversize(message, known_size)
            return

        uid = message.from_user.id
        group_id = message.media_group_id

        own_caption = (message.caption or "").strip()
        if message.forward_origin:
            context, caption = own_caption, ""
        else:
            context, caption = "", own_caption

        key = group_id if group_id else f"{message.message_id}_{int(time.time())}"
        stale = None

        with file_batches_lock:
            batch = file_batches.get(uid)
            if batch and batch["group_id"] != group_id and batch["downloading"] == 0:
                if not batch["fired"]:
                    batch["fired"] = True
                    if batch["timer"]:
                        batch["timer"].cancel()
                    stale = batch
                file_batches.pop(uid, None)
                batch = None

            if batch is None:
                batch = {
                    "message": message,
                    "group_id": group_id,
                    "in": os.path.join(WORKSPACE_ROOT, "in", key),
                    "out": os.path.join(WORKSPACE_ROOT, "out", key),
                    "caption": "",
                    "context": "",
                    "downloading": 0,
                    "last_file_at": time.time(),
                    "timer": None,
                    "fired": False,
                    "tg_photo": False,
                }
                file_batches[uid] = batch
                absorbed = _claim_recent_text(uid)
                if absorbed:
                    batch["caption"] = absorbed
                    logger.info(f"文件批次吸收了刚到达的文本作为指令: {absorbed!r}")

            if preferred_name is None:
                batch["tg_photo"] = True

            if caption:
                batch["caption"] = caption
            if context and not batch["context"]:
                batch["context"] = context
            if message.message_id < batch["message"].message_id:
                batch["message"] = message

            batch["downloading"] += 1
            if batch["timer"]:
                batch["timer"].cancel()
                batch["timer"] = None
            workspace_in, workspace_out = batch["in"], batch["out"]

        if stale:
            threading.Thread(
                target=_launch_file_task,
                args=(stale["message"], stale["in"], stale["out"],
                      stale["caption"], stale["context"], stale["tg_photo"]),
            ).start()

        def finish_download(blob):
            with file_batches_lock:
                if blob is not None:
                    os.makedirs(workspace_in, exist_ok=True)
                    os.makedirs(workspace_out, exist_ok=True)
                    existing = [
                        f for f in os.listdir(workspace_in)
                        if os.path.isfile(os.path.join(workspace_in, f))
                    ]
                    from core.file_pipeline import safe_filename
                    base = (
                        safe_filename(preferred_name, f"file_{len(existing) + 1:02d}")
                        if preferred_name
                        else f"photo_{len(existing) + 1:02d}.jpg"
                    )
                    if os.path.exists(os.path.join(workspace_in, base)):
                        root, ext = os.path.splitext(base)
                        base = f"{root}_{len(existing) + 1:02d}{ext}"
                    with open(os.path.join(workspace_in, base), "wb") as fh:
                        fh.write(blob)
                batch["downloading"] -= 1
                if blob is not None:
                    batch["last_file_at"] = time.time()
                _arm_batch_locked(uid)

        try:
            file_info = bot.get_file(file_id)
            blob = bot.download_file(file_info.file_path)
        except Exception:
            finish_download(None)
            raise

        if len(blob) > MAX_TG_FILE_SIZE:
            finish_download(None)
            _reject_oversize(message, len(blob))
            return

        finish_download(blob)

    @bot.message_handler(content_types=["photo"])
    def handle_photo(message):
        if message.from_user.id != allowed_user_id:
            return
        st = get_user_state_fn(message.from_user.id)
        if not st.get("in_chat", False):
            return

        bot.send_chat_action(message.chat.id, "typing")
        try:
            photo = message.photo[-1]
            _ingest_file(
                message, photo.file_id, None, getattr(photo, "file_size", 0) or 0
            )
        except Exception as e:
            logger.error(f"处理图片失败: {e}")
            bot.send_message(
                message.chat.id,
                f"❌ 处理图片失败: {e}",
                reply_to_message_id=message.message_id,
            )

    @bot.message_handler(content_types=["sticker"])
    def handle_sticker(message):
        if message.from_user.id != allowed_user_id:
            return
        st = get_user_state_fn(message.from_user.id)
        if not st.get("in_chat", False):
            return

        emoji = message.sticker.emoji if message.sticker.emoji else "未知内容"
        prompt = f"[用户发送了一个贴纸，其代表的表情是: {emoji}]"

        bot.send_chat_action(message.chat.id, "typing")
        from core.handlers.agy.tasks import execute_agy_prompt
        execute_agy_prompt(
            bot,
            message,
            prompt,
            get_user_state_fn,
            save_user_states_fn,
        )

    @bot.message_handler(content_types=["document", "video", "audio", "video_note"])
    def handle_any_file(message):
        if message.from_user.id != allowed_user_id:
            return
        st = get_user_state_fn(message.from_user.id)
        if not st.get("in_chat", False):
            return

        bot.send_chat_action(message.chat.id, "upload_document")
        try:
            file_obj = {
                "document": message.document,
                "video": message.video,
                "audio": message.audio,
                "video_note": message.video_note,
            }.get(message.content_type)
            if file_obj is None:
                return

            file_name = getattr(file_obj, "file_name", None)
            if not file_name:
                ext = {
                    "video": ".mp4",
                    "video_note": ".mp4",
                    "audio": ".ogg",
                }.get(message.content_type, ".bin")
                file_name = f"file_{int(time.time())}{ext}"

            _ingest_file(
                message,
                file_obj.file_id,
                os.path.basename(file_name),
                getattr(file_obj, "file_size", 0) or 0,
            )
        except Exception as e:
            logger.error(f"文件接收失败: {e}")
            bot.send_message(
                message.chat.id,
                f"❌ 文件接收失败: {e}",
                reply_to_message_id=message.message_id,
            )

    return claim_batch
