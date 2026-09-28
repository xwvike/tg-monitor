import logging
import os
import tempfile

from core.handlers.agy.constants import MAX_TG_FILE_SIZE
from core.handlers.agy.tasks import execute_agy_prompt
from core.handlers.agy.utils import safe_filename

logger = logging.getLogger("AGYHandler")

_DEFAULT_EXTS = {"photo": ".jpg", "video": ".mp4", "video_note": ".mp4", "audio": ".ogg"}


def register_media_handlers(bot, allowed_user_id, get_user_state_fn, save_user_states_fn,
                            begin_attachment_fn, end_attachment_fn):

    def _reject_oversize(message, size_bytes):
        size_mb = round(size_bytes / (1024 * 1024), 2)
        bot.send_message(
            message.chat.id,
            f"⚠️ <b>文件过大 ({size_mb} MB)</b>\n"
            f"──────────────────────\n"
            f"Telegram 官方标准 Bot API 限制单文件接收不能超过 <b>20MB</b>。",
            parse_mode="HTML",
            reply_to_message_id=message.message_id,
        )

    def _attach(message, file_obj, file_name):
        size = getattr(file_obj, "file_size", 0) or 0
        if size > MAX_TG_FILE_SIZE:
            _reject_oversize(message, size)
            return

        begin_attachment_fn(message)
        caption = (message.caption or "").strip()
        tmp_dir = None
        try:
            bot.send_chat_action(message.chat.id, "typing")
            file_info = bot.get_file(file_obj.file_id)
            blob = bot.download_file(file_info.file_path)
            tmp_dir = tempfile.mkdtemp(prefix="tg_attach_")
            path = os.path.join(tmp_dir, safe_filename(file_name))
            with open(path, "wb") as fh:
                fh.write(blob)
        except Exception as e:
            logger.error(f"附件接收失败: {e}")
            bot.send_message(
                message.chat.id,
                f"❌ 附件接收失败: {e}",
                reply_to_message_id=message.message_id,
            )
            end_attachment_fn(message, caption, None, tmp_dir)
            return
        end_attachment_fn(message, caption, path, tmp_dir)

    @bot.message_handler(content_types=["photo", "document", "video", "audio", "video_note"])
    def handle_attachment(message):
        if message.from_user.id != allowed_user_id:
            return
        if not get_user_state_fn(message.from_user.id).get("in_chat", False):
            return

        kind = message.content_type
        file_obj = message.photo[-1] if kind == "photo" else getattr(message, kind)
        file_name = getattr(file_obj, "file_name", None) or (
            f"{kind}_{message.message_id}{_DEFAULT_EXTS.get(kind, '.bin')}"
        )
        _attach(message, file_obj, file_name)

    @bot.message_handler(content_types=["sticker"])
    def handle_sticker(message):
        if message.from_user.id != allowed_user_id:
            return
        if not get_user_state_fn(message.from_user.id).get("in_chat", False):
            return

        emoji = message.sticker.emoji if message.sticker.emoji else "未知内容"
        bot.send_chat_action(message.chat.id, "typing")
        execute_agy_prompt(
            bot,
            message,
            f"[用户发送了一个贴纸，其代表的表情是: {emoji}]",
            get_user_state_fn,
            save_user_states_fn,
        )
