import logging
import os
import time

from core.stt import transcribe_voice_file
from core.handlers.agy.tasks import execute_agy_prompt

logger = logging.getLogger("AGYHandler")

def register_voice_handlers(bot, allowed_user_id, get_user_state_fn, save_user_states_fn):
    @bot.message_handler(content_types=["voice"])
    def handle_voice(message):
        if message.from_user.id != allowed_user_id:
            return
        st = get_user_state_fn(message.from_user.id)
        if not st.get("in_chat", False):
            return

        bot.send_chat_action(message.chat.id, "upload_voice")

        try:
            file_info = bot.get_file(message.voice.file_id)
            downloaded_file = bot.download_file(file_info.file_path)
            ts = int(time.time() * 1000)
            voice_ogg_path = f"/tmp/tg_voice_input_{message.from_user.id}_{ts}.ogg"

            with open(voice_ogg_path, "wb") as f:
                f.write(downloaded_file)

            ok, transcribed_text = transcribe_voice_file(voice_ogg_path)

            try:
                if os.path.exists(voice_ogg_path):
                    os.remove(voice_ogg_path)
            except Exception:
                pass

            if not ok or not transcribed_text:
                bot.reply_to(
                    message,
                    "⚠️ <b>语音识别未成功（环境较嘈杂或无清晰语音）</b>\n"
                    "──────────────────────\n"
                    "💡 <b>建议</b>：请尝试重新清晰发音，或直接使用<b>文字输入</b>与 AGY 交流。",
                    parse_mode="HTML",
                )
                return

            bot.send_chat_action(message.chat.id, "typing")
            execute_agy_prompt(
                bot,
                message,
                transcribed_text,
                get_user_state_fn,
                save_user_states_fn,
            )
        except Exception as ve:
            logger.error(f"处理语音消息发生异常: {ve}")
            bot.reply_to(
                message,
                "⚠️ <b>语音处理遇到异常</b>\n"
                "──────────────────────\n"
                "💡 <b>建议</b>：请尝试使用<b>文字输入</b>与 AGY 交流。",
                parse_mode="HTML",
            )
