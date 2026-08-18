import logging
import os
import subprocess
import threading
import time

import telegramify_markdown
from telebot import types

from core.file_pipeline import agy_env, package_products, run_task, TG_UPLOAD_LIMIT_BYTES
from core.run_archive import archive_run
from core.tg_format import code_block, esc, send_html
from core.tts import generate_telegram_voice, should_auto_speak
from core.handlers.agy.constants import AGY_BIN, TG_PHOTO_NOTICE, WORKSPACE_ROOT
from core.handlers.agy.utils import _cleanup_dirs, _get_conv_lock, _send_product, get_brain_conversations

logger = logging.getLogger("AGYHandler")

def run_file_task(bot, message, file_paths, workspace_in, workspace_out,
                  caption, model, tg_photo=False):
    chat_id = message.chat.id
    try:
        status_msg = bot.send_message(
            chat_id, "⚙️ 已交给 AGY 处理...", reply_to_message_id=message.message_id
        )
    except Exception:
        status_msg = None

    last_text = {"value": ""}
    trace: dict[str, object] = {"started_at": time.strftime("%Y-%m-%d %H:%M:%S")}
    ok, error = False, None

    def on_status(text):
        if status_msg is None or text == last_text["value"]:
            return
        last_text["value"] = text
        try:
            bot.edit_message_text(text, chat_id, status_msg.message_id)
        except Exception:
            pass

    try:
        ok, products, reply, error, warning = run_task(
            file_paths, workspace_in, workspace_out, caption, model, on_status,
            trace=trace,
        )

        if ok:
            if warning:
                send_html(bot, chat_id, warning,
                          reply_to_message_id=message.message_id)

            if reply:
                send_html(
                    bot, chat_id,
                    f"🤖 <b>agy：</b>\n──────────────────────\n{esc(reply)}",
                    reply_to_message_id=message.message_id,
                )

            count = len(products)
            products, packed = package_products(
                products, workspace_out, os.path.splitext(
                    os.path.basename(file_paths[0]))[0] if file_paths else "output"
            )
            if packed:
                on_status(f"📦 共 {count} 个产物，已打包为压缩包回传...")
            elif count:
                on_status(f"✅ 处理完成，正在回传 {count} 个文件...")
            for path in products:
                size = os.path.getsize(path)
                if size > TG_UPLOAD_LIMIT_BYTES:
                    bot.send_message(
                        chat_id,
                        f"⚠️ 产物 <code>{esc(os.path.basename(path))}</code> 为 "
                        f"{size / 1048576:.1f} MB，超过 Telegram 机器人 "
                        f"{TG_UPLOAD_LIMIT_BYTES // 1048576} MB 的上传上限，无法回传。\n"
                        f"可以让我按更小的尺寸重做，或拆成几批分别处理。",
                        parse_mode="HTML",
                    )
                    continue
                try:
                    _send_product(bot, chat_id, message.message_id, path)
                except Exception as e:
                    logger.error(f"回传产物 {path} 失败: {e}")
                    send_html(bot, chat_id,
                              f"⚠️ 产物 {esc(os.path.basename(path))} 回传失败: {esc(e)}")
            if tg_photo:
                send_html(bot, chat_id, TG_PHOTO_NOTICE)
            if status_msg is not None:
                try:
                    bot.delete_message(chat_id, status_msg.message_id)
                except Exception:
                    pass
        else:
            if status_msg is not None:
                try:
                    bot.edit_message_text(
                        error, chat_id, status_msg.message_id, parse_mode="HTML"
                    )
                    return
                except Exception:
                    pass
            bot.send_message(chat_id, error, parse_mode="HTML")
    except Exception as e:
        logger.error(f"文件流水线异常: {e}")
        error = f"流水线异常: {e}"
        try:
            bot.send_message(chat_id, f"❌ 文件处理流水线异常: {e}")
        except Exception:
            pass
    finally:
        trace.update({
            "finished_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "ok": ok,
            "error": error,
            "chat_id": chat_id,
            "message_id": message.message_id,
            "tg_photo": tg_photo,
        })
        try:
            archive_run(WORKSPACE_ROOT, workspace_in, workspace_out, trace)
        except Exception as e:
            logger.warning(f"任务留痕失败: {e}")
        _cleanup_dirs([workspace_in, workspace_out])


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
            final_prompt = (
                f"{prompt}\n\n请读取并结合以下附件文件进行分析或回答：\n{joined}"
            )

        conv_lock = _get_conv_lock(message.from_user.id)
        conv_lock.acquire()

        cmd = [AGY_BIN, "--dangerously-skip-permissions"]

        model = state.get("model")
        if model:
            cmd.extend(["--model", model])

        effort = state.get("effort")
        if effort:
            cmd.extend(["--effort", effort])

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

            if res.returncode != 0 and "--effort is not supported" in output_err:
                logger.info(
                    f"模型 [{model}] 不支持 --effort，自动移除 --effort 参数并重试..."
                )
                retry_cmd = []
                skip_next = False
                for token in cmd:
                    if skip_next:
                        skip_next = False
                        continue
                    if token == "--effort":
                        skip_next = True
                        continue
                    retry_cmd.append(token)
                res = subprocess.run(
                    retry_cmd,
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
