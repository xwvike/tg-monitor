"""
Layer 3: AGY 智能体交互层 (agy_handler.py)
处理 /chat, /new, /history, /exit, 图片多模态及沉浸式对话。
100% 继承原版 4s typing 守护线程、会话 ID 持久化与 1.5s 消息防抖合并机制。
"""

import glob
import json
import logging
import os
import shutil
import subprocess
import sys
import threading
import time

import telebot
import telegramify_markdown
from telebot import types

sys.path.append(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
)
from core.stt import transcribe_voice_file
from core.tts import clean_text_for_tts, generate_telegram_voice, should_auto_speak

AGY_BIN = os.path.expanduser("~/.local/bin/agy")
BRAIN_DIR = os.path.expanduser("~/.gemini/antigravity-cli/brain")
logger = logging.getLogger("AGYHandler")

user_buffers = {}  # Message debouncing: {user_id: {"messages": list, "timer": Timer}}
user_buffers_lock = threading.Lock()  # 保护 user_buffers 防止防抖 Timer 竞态


def get_brain_conversations():
    brain_dirs = glob.glob(os.path.join(BRAIN_DIR, "*"))
    conversations = []
    for d in brain_dirs:
        cid = os.path.basename(d)
        log_file = os.path.join(d, ".system_generated", "logs", "transcript.jsonl")
        if os.path.exists(log_file):
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
            mtime = os.path.getmtime(log_file)
            conversations.append((cid, first_msg, mtime))
    conversations.sort(key=lambda x: x[2], reverse=True)
    return conversations


def execute_agy_prompt(
    bot, message, prompt, get_user_state_fn, save_user_states_fn, image_path=None, workspaces=None
):
    """主执行逻辑：含 4秒 typing 守护线程与离线进程管理"""
    chat_id = message.chat.id
    state = get_user_state_fn(message.from_user.id)

    def process():
        stop_typing = threading.Event()

        # 4秒定时续期 typing 状态守护线程
        def send_typing_loop():
            while not stop_typing.is_set():
                try:
                    bot.send_chat_action(chat_id, "typing")
                except Exception:
                    pass
                stop_typing.wait(4)

        typing_thread = threading.Thread(target=send_typing_loop)
        typing_thread.start()

        env = os.environ.copy()
        env["HTTP_PROXY"] = "http://127.0.0.1:10809"
        env["HTTPS_PROXY"] = "http://127.0.0.1:10809"
        local_bin = os.path.expanduser("~/.local/bin")
        env["PATH"] = f"{local_bin}:{env.get('PATH', '')}"

        final_prompt = prompt
        if image_path and "[系统文件注入]" not in prompt:
            final_prompt = f"[{prompt}] 请识别分析这张附件图片文件：{image_path}"

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

            # 自动容错：若当前选定的 AI 模型（如 Claude Opus）不支持 --effort 参数，自动剥离 --effort 并静默重试
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

            # 自动捕获 OAuth 认证失联/过期错误，明确告知用户
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

            # 若原先为空白对话，则获取新建的 conversation_id 并保存状态
            if not state.get("conv_id"):
                try:
                    recent = get_brain_conversations()
                    if recent:
                        state["conv_id"] = recent[0][0]
                        save_user_states_fn()
                except Exception:
                    pass

            # 思路 2: 默认挂载 [ 🔊 朗读此条 ] 按键
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
                if len(output) > 3800:
                    output = output[:3800] + "\n...(内容较长，已截断)"
                reply_text = f"🤖 <b>agy：</b>\n──────────────────────\n<pre>{telebot.formatting.escape_html(output)}</pre>"
                bot.send_message(
                    chat_id, reply_text, parse_mode="HTML", reply_markup=tts_markup
                )

            # 思路 1: 若用户启用了 auto_voice 且满足短文无代码块条件，自动发送语音泡泡
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
            stop_typing.set()
            typing_thread.join(timeout=1)
            if image_path and os.path.exists(image_path):
                try:
                    os.remove(image_path)
                except Exception:
                    pass

            if workspaces:
                out_dir = workspaces.get("out")
                if out_dir and os.path.exists(out_dir):
                    try:
                        for f_name in os.listdir(out_dir):
                            f_path = os.path.join(out_dir, f_name)
                            if os.path.isfile(f_path):
                                bot.send_chat_action(chat_id, "upload_document")
                                with open(f_path, "rb") as out_f:
                                    bot.send_document(chat_id, out_f, reply_to_message_id=message.message_id)
                    except Exception as e:
                        logger.error(f"发送产出文件失败: {e}")
                
                try:
                    if workspaces.get("in") and os.path.exists(workspaces.get("in")):
                        shutil.rmtree(workspaces.get("in"))
                    if out_dir and os.path.exists(out_dir):
                        shutil.rmtree(out_dir)
                except Exception as e:
                    logger.error(f"清理临时工作区失败: {e}")

    threading.Thread(target=process).start()


def register_agy_handlers(
    bot,
    allowed_user_id: int,
    get_user_state_fn,
    save_user_states_fn,
    get_main_keyboard_fn,
):
    """注册 Layer 3 AGY AI 指令与交互处理器"""

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
                f"✅ <b>成功切回并恢复会话：</b> <code>{cid[:8]}...</code>\n现在您可以继续与其上下文进行对话了！",
                reply_markup=get_main_keyboard_fn(call.from_user.id),
            )

    @bot.message_handler(commands=["model"])
    def handle_model_select(message):
        if message.from_user.id != allowed_user_id:
            return
        st = get_user_state_fn(message.from_user.id)
        current_m = st.get("model", "gemini-3.6-flash-high")

        markup = types.InlineKeyboardMarkup(row_width=2)
        models_map = [
            ("gemini-3.6-flash-high", "⚡ Gemini 3.6 Flash"),
            ("gemini-3.1-pro-high", "🧠 Gemini 3.1 Pro"),
            ("claude-sonnet-4-6", "🎭 Claude Sonnet 4.6"),
            ("claude-opus-4-6-thinking", "🔮 Claude Opus 4.6"),
            ("gpt-oss-120b-medium", "🤖 GPT-OSS 120B"),
        ]
        btns = []
        for m_id, m_label in models_map:
            prefix = "✅ " if m_id == current_m else ""
            btns.append(
                types.InlineKeyboardButton(
                    f"{prefix}{m_label}", callback_data=f"setmodel:{m_id}"
                )
            )
        markup.add(*btns[:2])
        markup.add(*btns[2:4])
        markup.add(btns[4])

        bot.send_message(
            message.chat.id,
            f"🤖 <b>请选择 AGY 当前调用的 AI 模型：</b>\n"
            f"──────────────────────\n"
            f"📌 <b>当前选定模型</b>: <code>{current_m}</code>",
            reply_markup=markup,
            parse_mode="HTML",
        )

    @bot.message_handler(commands=["effort"])
    def handle_effort_select(message):
        if message.from_user.id != allowed_user_id:
            return
        st = get_user_state_fn(message.from_user.id)
        current_e = st.get("effort", "high")

        markup = types.InlineKeyboardMarkup(row_width=3)
        efforts = [
            ("low", "🟢 Low (极速)"),
            ("medium", "🟡 Medium (平衡)"),
            ("high", "🔴 High (深度)"),
        ]
        btns = []
        for e_id, e_label in efforts:
            prefix = "✅ " if e_id == current_e else ""
            btns.append(
                types.InlineKeyboardButton(
                    f"{prefix}{e_label}", callback_data=f"seteffort:{e_id}"
                )
            )
        markup.add(*btns)

        bot.send_message(
            message.chat.id,
            f"⚡ <b>请选择 AGY 思考推理深度 (Reasoning Effort)：</b>\n"
            f"──────────────────────\n"
            f"📌 <b>当前选定级别</b>: <code>{current_e}</code>",
            reply_markup=markup,
            parse_mode="HTML",
        )

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
        conv_str = f"<code>{cid}</code>" if cid else "<i>(无 / 新会话)</i>"
        mode_str = "💬 沉浸对话模式" if st.get("in_chat", False) else "📊 基础监控模式"
        voice_str = "🟢 开启" if st.get("auto_voice", False) else "🔴 关闭 (按需点播)"

        msg = (
            "⚙️ <b>AGY 会话配置与全局参数</b>\n"
            "──────────────────────\n"
            f"🤖 <b>运行 AI 模型</b>: <code>{st.get('model', 'gemini-3.6-flash-high')}</code>\n"
            f"⚡ <b>思考推理深度</b>: <code>{st.get('effort', 'high')}</code>\n"
            f"🔊 <b>自动语音答复</b>: {voice_str}\n"
            f"💬 <b>当前绑定会话 ID</b>: {conv_str}\n"
            f"🚪 <b>当前工作状态</b>: {mode_str}\n"
            "──────────────────────\n"
            "💡 提示: 发送 /model 切换模型，/effort 切换推理深度，/voice 切换自动语音。"
        )
        bot.send_message(message.chat.id, msg, parse_mode="HTML")

    @bot.callback_query_handler(
        func=lambda call: (
            call.data.startswith("setmodel:")
            or call.data.startswith("seteffort:")
            or call.data == "tts_speak"
        )
    )
    def handle_model_effort_callback(call):
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
                f"✅ <b>已将 AGY 交互模型切换为：</b> <code>{m_id}</code>",
                parse_mode="HTML",
            )
        elif call.data.startswith("seteffort:"):
            e_id = call.data.replace("seteffort:", "")
            st["effort"] = e_id
            save_user_states_fn()
            bot.answer_callback_query(call.id, f"思考深度已切换为: {e_id}")
            bot.send_message(
                call.message.chat.id,
                f"✅ <b>已将 AGY 思考推理深度切换为：</b> <code>{e_id}</code>",
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

    @bot.message_handler(content_types=["photo"])
    def handle_photo(message):
        if message.from_user.id != allowed_user_id:
            return
        st = get_user_state_fn(message.from_user.id)
        if not st.get("in_chat", False):
            return

        bot.send_chat_action(message.chat.id, "typing")
        try:
            fileID = message.photo[-1].file_id
            file_info = bot.get_file(fileID)
            downloaded_file = bot.download_file(file_info.file_path)

            msg_id = f"{message.message_id}_{int(time.time())}"
            workspace_in = f"/tmp/tg_files/in/{msg_id}"
            workspace_out = f"/tmp/tg_files/out/{msg_id}"
            os.makedirs(workspace_in, exist_ok=True)
            os.makedirs(workspace_out, exist_ok=True)

            tmp_path = os.path.join(workspace_in, f"photo_{int(time.time())}.jpg")
            with open(tmp_path, "wb") as new_file:
                new_file.write(downloaded_file)

            file_size = getattr(file_info, "file_size", len(downloaded_file))
            size_mb = round(file_size / (1024 * 1024), 2)
            caption = message.caption

            if caption:
                prompt = (
                    f"[系统文件注入] 用户上传了图片请求处理。\\n"
                    f"▶️ 文件路径: {tmp_path}\\n"
                    f"▶️ 文件大小: {size_mb} MB\\n"
                    f"▶️ 预期输出目录: {workspace_out}\\n"
                    f"▶️ 用户的说明: {caption}\\n\\n"
                    f"🚨 你的任务: 查阅 config/TOOLCHAIN.md 与 config/file_recipes/，优先调用终端工具(如 ImageMagick/pngquant)处理此文件。\\n"
                    f"如果有产出文件，请务必将其生成到预期输出目录 ({workspace_out}) 中。系统会自动回传给用户。\\n"
                    f"如果是视觉分析问题，请直接回答。"
                )
            else:
                prompt = (
                    f"[系统文件注入] 用户上传了一张图片 (大小: {size_mb} MB)，存放于 {tmp_path}，预期输出目录 {workspace_out}。\\n"
                    f"用户未提供说明。请详细描述这张图片的内容，若包含代码或报错信息请指出并解释。"
                )

            execute_agy_prompt(
                bot,
                message,
                prompt,
                get_user_state_fn,
                save_user_states_fn,
                image_path=tmp_path,
                workspaces={"in": workspace_in, "out": workspace_out},
            )
        except Exception as e:
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
            if message.content_type == "document":
                file_info_obj = message.document
            elif message.content_type == "video":
                file_info_obj = message.video
            elif message.content_type == "audio":
                file_info_obj = message.audio
            elif message.content_type == "video_note":
                file_info_obj = message.video_note
            else:
                return

            file_id = file_info_obj.file_id
            file_size = getattr(file_info_obj, "file_size", 0)
            file_name = getattr(file_info_obj, "file_name", None)
            if not file_name:
                ext = ".mp4" if message.content_type in ["video", "video_note"] else ".ogg" if message.content_type == "audio" else ".bin"
                file_name = f"file_{int(time.time())}{ext}"

            file_info = bot.get_file(file_id)
            downloaded_file = bot.download_file(file_info.file_path)

            msg_id = f"{message.message_id}_{int(time.time())}"
            workspace_in = f"/tmp/tg_files/in/{msg_id}"
            workspace_out = f"/tmp/tg_files/out/{msg_id}"
            os.makedirs(workspace_in, exist_ok=True)
            os.makedirs(workspace_out, exist_ok=True)

            tmp_path = os.path.join(workspace_in, file_name)
            with open(tmp_path, "wb") as new_file:
                new_file.write(downloaded_file)

            caption = message.caption or "无附加说明"
            size_mb = round(file_size / (1024 * 1024), 2)

            prompt = (
                f"[系统文件注入] 用户上传了文件请求处理。\\n"
                f"▶️ 文件路径: {tmp_path}\\n"
                f"▶️ 文件大小: {size_mb} MB\\n"
                f"▶️ 预期输出目录: {workspace_out}\\n"
                f"▶️ 用户的说明: {caption}\\n\\n"
                f"🚨 你的任务: 查阅 config/TOOLCHAIN.md 与 config/file_recipes/，调用终端工具处理此文件。\\n"
                f"如果有产出文件，请务必将其生成到预期输出目录 ({workspace_out}) 中。系统会自动将该目录下的产物回传给用户。\\n"
                f"如果不需要产生文件（如信息提取），直接输出分析结果文本即可。"
            )

            execute_agy_prompt(
                bot,
                message,
                prompt,
                get_user_state_fn,
                save_user_states_fn,
                workspaces={"in": workspace_in, "out": workspace_out},
            )
        except Exception as e:
            bot.send_message(
                message.chat.id,
                f"❌ 文件接收失败: {e}",
                reply_to_message_id=message.message_id,
            )

    @bot.message_handler(content_types=["voice"])
    def handle_voice(message):
        if message.from_user.id != allowed_user_id:
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

            # 静默完成 STT 识别，直接提交 AGY 执行，不发送多余中间确认卡片
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

    def dispatch_text_message(message):
        """处理普通 Chat 模式下的 1.5s/4.0s 防抖合并文本提交流程"""
        uid = message.from_user.id
        st = get_user_state_fn(uid)
        if not st.get("in_chat", False):
            return False

        forward_prefix = ""
        if message.forward_origin:
            origin = message.forward_origin
            if origin.type == "chat":
                forward_prefix = f"[转发自频道/群组: {origin.chat.title}] "
            elif origin.type == "user":
                forward_prefix = f"[转发自用户: {origin.sender_user.first_name}] "

        with user_buffers_lock:
            is_first_msg = uid not in user_buffers
            if is_first_msg:
                user_buffers[uid] = {
                    "messages": [],
                    "timer": None,
                    "has_forward": False,
                }

            if message.forward_origin:
                user_buffers[uid]["has_forward"] = True

            user_buffers[uid]["messages"].append(forward_prefix + message.text)

            if user_buffers[uid]["timer"]:
                user_buffers[uid]["timer"].cancel()

            def send_buffered():
                with user_buffers_lock:
                    if uid not in user_buffers:
                        return
                    combined_prompt = "\n".join(user_buffers[uid]["messages"])
                    del user_buffers[uid]
                execute_agy_prompt(
                    bot,
                    message,
                    combined_prompt,
                    get_user_state_fn,
                    save_user_states_fn,
                )

            # 转发消息延长防抖窗口至 4 秒，给用户足够时间追加附言
            debounce_sec = 4.0 if user_buffers[uid].get("has_forward") else 1.5
            timer = threading.Timer(debounce_sec, send_buffered)
            user_buffers[uid]["timer"] = timer
            timer.start()

        # typing 指示器放在锁外：避免阻塞式 HTTP 调用撕裂 buffer/timer 的原子性
        if is_first_msg:
            try:
                bot.send_chat_action(message.chat.id, "typing")
            except Exception:
                pass

        return True

    return dispatch_text_message, render_history_page
