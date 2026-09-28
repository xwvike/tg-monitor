import logging
import os
import subprocess
import threading

import telegramify_markdown
from telebot import types

from core.handlers.agy.constants import AGY_BIN
from core.handlers.agy.utils import (
    _cleanup_dirs,
    _get_conv_lock,
    agy_env,
    get_brain_conversations,
)
from core.tg_format import code_block
from core.tts import generate_telegram_voice, should_auto_speak

logger = logging.getLogger("AGYHandler")


def execute_agy_prompt(
    bot,
    message,
    prompt,
    get_user_state_fn,
    save_user_states_fn,
    attached_files=None,
    cleanup_dirs=None,
):
    chat_id = message.chat.id
    state = get_user_state_fn(message.from_user.id)

    def process():
        stop_typing = threading.Event()

        def send_typing_loop():
            while not stop_typing.is_set():
                try:
                    bot.send_chat_action(chat_id, "typing")
                except Exception:
                    pass
                stop_typing.wait(4)

        typing_thread = threading.Thread(target=send_typing_loop)
        typing_thread.start()

        env = agy_env()

        final_prompt = prompt
        if attached_files:
            joined = "\n".join(f"  - {p}" for p in attached_files)
            final_prompt = f"{prompt}\n\n[随消息附带的文件]\n{joined}".strip()

        conv_lock = _get_conv_lock(message.from_user.id)
        conv_lock.acquire()

        cmd = [AGY_BIN, "--dangerously-skip-permissions"]

        model = state.get("model")
        if model:
            cmd.extend(["--model", model])

        if state.get("conv_id"):
            cmd.extend(["--conversation", state["conv_id"]])

        cmd.extend(["-p", final_prompt])

        try:
            res = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=240,
                env=env,
                cwd=os.path.expanduser("~"),
            )
            output_err = (res.stderr or "") + (res.stdout or "")

            if (
                "Authentication required" in output_err
                or "authentication failed" in output_err
                or "authentication timed out" in output_err
            ):
                logger.error("🚨 检测到底层 agy CLI 认证过期或需要登录授权！")
                msg = (
                    "🔑 <b>AGY 认证失效提示</b>\n"
                    "──────────────────────\n"
                    "底层的 agy CLI 登录凭证已过期，触发了 OAuth 登录授权。\n\n"
                    "💡 <b>解决方案</b>: 请在服务器终端运行 <code>agy</code> 命令重新完成登录认证。"
                )
                bot.send_message(chat_id, msg, parse_mode="HTML")
                return

            output = res.stdout.strip() or res.stderr.strip() or "(无输出内容)"

            if not state.get("conv_id"):
                try:
                    recent = get_brain_conversations()
                    if recent:
                        state["conv_id"] = recent[0][0]
                        save_user_states_fn()
                except Exception:
                    pass

            tts_markup = types.InlineKeyboardMarkup()
            tts_markup.add(
                types.InlineKeyboardButton("🔊 朗读此条", callback_data="tts_speak")
            )

            try:
                formatted_md = telegramify_markdown.markdownify(output)
                if len(formatted_md) > 3800:
                    formatted_md = (
                        formatted_md[:3800] + "\n\\.\\.\\.\\(内容较长，已截断\\)"
                    )
                bot.send_message(
                    chat_id,
                    formatted_md,
                    parse_mode="MarkdownV2",
                    reply_markup=tts_markup,
                )
            except Exception as format_err:
                logger.warning(f"MarkdownV2 Render Fallback: {format_err}")
                reply_text = f"🤖 <b>agy：</b>\n──────────────────────\n{code_block(output, limit=3800)}"
                bot.send_message(
                    chat_id, reply_text, parse_mode="HTML", reply_markup=tts_markup
                )

            if state.get("auto_voice", False):
                can_speak, cleaned = should_auto_speak(output)
                if can_speak and cleaned:

                    def auto_voice_job():
                        ok, ogg, _dur, _ = generate_telegram_voice(cleaned)
                        if ok and os.path.exists(ogg):
                            try:
                                with open(ogg, "rb") as vf:
                                    bot.send_voice(chat_id, vf)
                            except Exception as ve:
                                logger.warning(f"发送自动语音失败: {ve}")
                            finally:
                                try:
                                    os.remove(ogg)
                                except Exception:
                                    pass

                    threading.Thread(target=auto_voice_job).start()

        except subprocess.TimeoutExpired:
            bot.send_message(
                chat_id,
                "⏰ <b>agy 处理超时（超过 4 分钟），请尝试简化任务。</b>",
                parse_mode="HTML",
            )
        except Exception as e:
            bot.send_message(
                chat_id, f"❌ <b>调用 agy 失败：</b> {e}", parse_mode="HTML"
            )
        finally:
            conv_lock.release()
            stop_typing.set()
            typing_thread.join(timeout=1)
            _cleanup_dirs(cleanup_dirs)

    threading.Thread(target=process).start()
