import logging
import os
import threading
import time

from telebot import types

from core.handlers.agy.constants import user_buffers, user_buffers_lock
from core.handlers.agy.tasks import execute_agy_prompt
from core.handlers.agy.utils import (
    _cleanup_dirs,
    get_brain_conversations,
    list_agy_models,
)
from core.tg_format import esc
from core.tts import clean_text_for_tts, generate_telegram_voice

logger = logging.getLogger("AGYHandler")

def register_chat_handlers(bot, allowed_user_id, get_user_state_fn, save_user_states_fn, get_main_keyboard_fn):
    @bot.message_handler(commands=["chat"])
    def handle_chat(message):
        if message.from_user.id != allowed_user_id:
            return
        st = get_user_state_fn(message.from_user.id)
        st["in_chat"] = True
        save_user_states_fn()
        bot.reply_to(
            message,
            "💬 已进入 AGY 沉浸对话模式。请直接发送文本或图片，无需加 /前缀。发送 /exit 随时退出。",
            reply_markup=get_main_keyboard_fn(message.from_user.id),
        )

    @bot.message_handler(commands=["exit"])
    def handle_exit(message):
        if message.from_user.id != allowed_user_id:
            return
        st = get_user_state_fn(message.from_user.id)
        st["in_chat"] = False
        save_user_states_fn()
        bot.reply_to(
            message,
            "🚪 已退出 AGY 对话模式，恢复为普通监控面板模式。",
            reply_markup=get_main_keyboard_fn(message.from_user.id),
        )

    @bot.message_handler(commands=["new"])
    def handle_new(message):
        if message.from_user.id != allowed_user_id:
            return
        st = get_user_state_fn(message.from_user.id)
        st["conv_id"] = None
        st["in_chat"] = True
        save_user_states_fn()
        bot.reply_to(
            message,
            "🆕 已重置为新的空白 AGY 会话！请直接输入。",
            reply_markup=get_main_keyboard_fn(message.from_user.id),
        )

    @bot.message_handler(commands=["history"])
    def handle_history(message):
        if message.from_user.id != allowed_user_id:
            return
        bot.send_chat_action(message.chat.id, "typing")
        render_history_page(bot, message.chat.id, 0)

    def render_history_page(bot_inst, chat_id, page=0):
        conversations = get_brain_conversations()
        if not conversations:
            bot_inst.send_message(chat_id, "⚠️ 暂无历史 AGY 对话记录。")
            return

        per_page = 5
        total_pages = (len(conversations) + per_page - 1) // per_page
        page = max(0, min(page, total_pages - 1))
        current_page_items = conversations[page * per_page : (page + 1) * per_page]

        markup = types.InlineKeyboardMarkup()
        for cid, msg_preview, mtime in current_page_items:
            dt_str = time.strftime("%m-%d %H:%M", time.localtime(mtime))
            snippet = msg_preview.replace("\n", " ")[:22]
            btn_text = f"📄 [{dt_str}] {snippet}"
            markup.add(
                types.InlineKeyboardButton(btn_text, callback_data=f"resume_{cid}")
            )

        nav_row = []
        if page > 0:
            nav_row.append(
                types.InlineKeyboardButton("◀️ 上一页", callback_data=f"page_{page - 1}")
            )
        nav_row.append(
            types.InlineKeyboardButton(
                f"📄 {page + 1}/{total_pages}", callback_data="page_noop"
            )
        )
        if page < total_pages - 1:
            nav_row.append(
                types.InlineKeyboardButton("下一页 ▶️", callback_data=f"page_{page + 1}")
            )

        markup.add(*nav_row)
        bot_inst.send_message(
            chat_id,
            f"📜 <b>AGY 历史对话会话列表 (共 {len(conversations)} 条记录)：</b>\n点击对应按钮可一键切回历史上下文。",
            reply_markup=markup,
            parse_mode="HTML",
        )

    @bot.callback_query_handler(
        func=lambda call: (
            call.data.startswith("page_") or call.data.startswith("resume_")
        )
    )
    def handle_history_callback(call):
        if call.from_user.id != allowed_user_id:
            return
        if call.data.startswith("page_"):
            parts = call.data.split("_")
            if parts[1] == "noop":
                bot.answer_callback_query(call.id)
                return
            page = int(parts[1])
            bot.answer_callback_query(call.id)
            try:
                bot.delete_message(call.message.chat.id, call.message.message_id)
            except Exception:
                pass
            render_history_page(bot, call.message.chat.id, page)

        elif call.data.startswith("resume_"):
            cid = call.data.replace("resume_", "")
            st = get_user_state_fn(call.from_user.id)
            st["conv_id"] = cid
            st["in_chat"] = True
            save_user_states_fn()
            bot.answer_callback_query(call.id, "已成功恢复选中的历史会话！")
            bot.send_message(
                call.message.chat.id,
                f"✅ <b>成功切回并恢复会话：</b> <code>{esc(cid[:8])}...</code>\n现在您可以继续与其上下文进行对话了！",
                reply_markup=get_main_keyboard_fn(call.from_user.id),
            )

    @bot.message_handler(commands=["model"])
    def handle_model_select(message):
        if message.from_user.id != allowed_user_id:
            return
        current_m = get_user_state_fn(message.from_user.id).get("model")
        loading = bot.send_message(message.chat.id, "⏳ 正在从 agy 获取可用模型列表...")

        def job():
            try:
                models = list_agy_models()
            except Exception as e:
                bot.edit_message_text(
                    f"❌ <b>获取模型列表失败：</b> {esc(e)}",
                    message.chat.id, loading.message_id, parse_mode="HTML",
                )
                return

            markup = types.InlineKeyboardMarkup(row_width=1)
            markup.add(*[
                types.InlineKeyboardButton(
                    f"{'✅ ' if m_id == current_m else ''}{label}",
                    callback_data=f"setmodel:{m_id}",
                )
                for m_id, label in models
            ])
            bot.edit_message_text(
                f"🤖 <b>请选择 AGY 当前调用的 AI 模型：</b>\n"
                f"──────────────────────\n"
                f"📌 <b>当前选定模型</b>: <code>{esc(current_m or 'agy 默认')}</code>",
                message.chat.id, loading.message_id,
                reply_markup=markup, parse_mode="HTML",
            )

        threading.Thread(target=job).start()

    @bot.message_handler(commands=["voice"])
    def handle_voice_toggle(message):
        if message.from_user.id != allowed_user_id:
            return
        st = get_user_state_fn(message.from_user.id)
        current = st.get("auto_voice", False)
        st["auto_voice"] = not current
        save_user_states_fn()

        status_str = (
            "🟢 开启 (短文本回复自动发语音包)"
            if st["auto_voice"]
            else "🔴 关闭 (仅按需点击按钮朗读)"
        )
        bot.send_message(
            message.chat.id,
            f"🔊 <b>AGY 自动语音回复模式</b>\n"
            f"──────────────────────\n"
            f"📌 <b>当前状态</b>: {status_str}\n\n"
            f"💡 无论开启与否，点击回复下方的 [🔊 朗读此条] 按钮均可随时点播朗读。",
            parse_mode="HTML",
        )

    @bot.message_handler(commands=["settings", "state"])
    def handle_settings(message):
        if message.from_user.id != allowed_user_id:
            return
        st = get_user_state_fn(message.from_user.id)
        cid = st.get("conv_id")
        conv_str = f"<code>{esc(cid)}</code>" if cid else "<i>(无 / 新会话)</i>"
        mode_str = "💬 沉浸对话模式" if st.get("in_chat", False) else "📊 基础监控模式"
        voice_str = "🟢 开启" if st.get("auto_voice", False) else "🔴 关闭 (按需点播)"

        msg = (
            "⚙️ <b>AGY 会话配置与全局参数</b>\n"
            "──────────────────────\n"
            f"🤖 <b>运行 AI 模型</b>: <code>{esc(st.get('model') or 'agy 默认')}</code>\n"
            f"🔊 <b>自动语音答复</b>: {voice_str}\n"
            f"💬 <b>当前绑定会话 ID</b>: {conv_str}\n"
            f"🚪 <b>当前工作状态</b>: {mode_str}\n"
            "──────────────────────\n"
            "💡 提示: 发送 /model 切换模型，/voice 切换自动语音。"
        )
        bot.send_message(message.chat.id, msg, parse_mode="HTML")

    @bot.callback_query_handler(
        func=lambda call: (
            call.data.startswith("setmodel:")
            or call.data == "tts_speak"
        )
    )
    def handle_model_callback(call):
        if call.from_user.id != allowed_user_id:
            return
        st = get_user_state_fn(call.from_user.id)
        if call.data.startswith("setmodel:"):
            m_id = call.data.replace("setmodel:", "")
            st["model"] = m_id
            save_user_states_fn()
            bot.answer_callback_query(call.id, f"模型已切换为: {m_id}")
            bot.send_message(
                call.message.chat.id,
                f"✅ <b>已将 AGY 交互模型切换为：</b> <code>{esc(m_id)}</code>",
                parse_mode="HTML",
            )
        elif call.data == "tts_speak":
            raw_text = call.message.text or call.message.caption or ""
            cleaned = clean_text_for_tts(raw_text)

            if not cleaned or len(cleaned) < 3:
                bot.answer_callback_query(
                    call.id, "该消息包含大量代码或表格，无可朗读文本", show_alert=True
                )
                return

            bot.answer_callback_query(call.id, "正在合成语音消息...")

            def process_tts():
                ok, ogg_path, _duration, _ = generate_telegram_voice(cleaned)
                if ok and os.path.exists(ogg_path):
                    try:
                        with open(ogg_path, "rb") as voice_f:
                            bot.send_voice(call.message.chat.id, voice_f)
                    except Exception as e:
                        bot.send_message(call.message.chat.id, f"❌ 发送语音失败: {e}")
                    finally:
                        try:
                            os.remove(ogg_path)
                        except Exception:
                            pass
                else:
                    bot.send_message(
                        call.message.chat.id, f"⚠️ 语音合成失败或已降级: {ogg_path}"
                    )

            threading.Thread(target=process_tts).start()

    def _forward_prefix(message):
        origin = message.forward_origin
        if not origin:
            return ""
        if origin.type == "chat":
            return f"[转发自频道/群组: {origin.chat.title}] "
        if origin.type == "user":
            return f"[转发自用户: {origin.sender_user.first_name}] "
        return ""

    def _get_buffer_locked(message):
        uid = message.from_user.id
        buf = user_buffers.get(uid)
        if buf is None:
            buf = {
                "message": message,
                "messages": [],
                "files": [],
                "dirs": [],
                "pending": 0,
                "timer": None,
                "has_forward": False,
                "created_at": time.time(),
            }
            user_buffers[uid] = buf
        if message.forward_origin:
            buf["has_forward"] = True
        return buf

    def _arm_locked(uid):
        buf = user_buffers[uid]
        if buf["timer"]:
            buf["timer"].cancel()
            buf["timer"] = None
        if buf["pending"] > 0:
            return
        debounce_sec = 4.0 if buf["has_forward"] else 1.5
        buf["timer"] = threading.Timer(debounce_sec, _send_buffered, args=(uid,))
        buf["timer"].start()

    def _send_buffered(uid):
        with user_buffers_lock:
            buf = user_buffers.get(uid)
            if not buf or buf["pending"] > 0:
                return
            del user_buffers[uid]
        execute_agy_prompt(
            bot,
            buf["message"],
            "\n".join(buf["messages"]),
            get_user_state_fn,
            save_user_states_fn,
            attached_files=buf["files"] or None,
            cleanup_dirs=buf["dirs"] or None,
        )

    def begin_attachment(message):
        with user_buffers_lock:
            buf = _get_buffer_locked(message)
            buf["pending"] += 1
            if buf["timer"]:
                buf["timer"].cancel()
                buf["timer"] = None

    def end_attachment(message, caption="", path=None, tmp_dir=None):
        uid = message.from_user.id
        with user_buffers_lock:
            buf = user_buffers.get(uid)
            if buf is None:
                _cleanup_dirs([tmp_dir])
                return
            buf["pending"] -= 1
            if caption:
                buf["messages"].append(_forward_prefix(message) + caption)
            if path:
                buf["files"].append(path)
            if tmp_dir:
                buf["dirs"].append(tmp_dir)
            if not buf["messages"] and not buf["files"] and buf["pending"] == 0:
                del user_buffers[uid]
                _cleanup_dirs(buf["dirs"])
                return
            _arm_locked(uid)

    def dispatch_text_message(message):
        uid = message.from_user.id
        st = get_user_state_fn(uid)
        if not st.get("in_chat", False):
            return False

        with user_buffers_lock:
            is_first_msg = uid not in user_buffers
            buf = _get_buffer_locked(message)
            buf["messages"].append(_forward_prefix(message) + message.text)
            _arm_locked(uid)

        if is_first_msg:
            try:
                bot.send_chat_action(message.chat.id, "typing")
            except Exception:
                pass

        return True

    button_handlers = {
        "chat": handle_chat,
        "exit": handle_exit,
        "new": handle_new,
        "history": handle_history,
    }

    return dispatch_text_message, render_history_page, button_handlers, begin_attachment, end_attachment
