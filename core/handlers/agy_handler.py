"""
Layer 3: AGY 智能体交互层 (agy_handler.py)
处理 /chat, /new, /history, /exit, 图片多模态及沉浸式对话。
100% 继承原版 4s typing 守护线程、会话 ID 持久化与 1.5s 消息防抖合并机制。
"""

import glob
import json
import logging
import os
import re
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
from core.file_pipeline import (
    AUDIO_EXTS,
    INLINE_TEXT_MAX_CHARS,
    INTERNAL_MARKER,
    TEXT_EXTS,
    TG_UPLOAD_LIMIT_BYTES,
    VIDEO_EXTS,
    agy_env,
    package_products,
    run_task,
    safe_filename,
)
from core.run_archive import archive_root, archive_run, prune
from core.stt import transcribe_voice_file
from core.tg_format import code_block, esc, send_html
from core.tts import clean_text_for_tts, generate_telegram_voice, should_auto_speak

AGY_BIN = os.path.expanduser("~/.local/bin/agy")
BRAIN_DIR = os.path.expanduser("~/.gemini/antigravity-cli/brain")

# 工作区必须落在**真实磁盘**上，不能用 /tmp —— 本机 /tmp 是 tmpfs，占的是内存
# （3.8G 上限，而可用内存只有 2.6G）。输入文件本身受 MAX_TG_FILE_SIZE 约束还好，
# 但视频转码的中间产物不受它约束：抽帧、调色板、重编码的峰值轻易上 GB，
# 落在 tmpfs 上就是直接吃内存，撞上去表现为"任务莫名其妙失败"，极难排查。
# 放大 tmpfs 解决不了问题 —— 它本来就是内存，放大只会让 OOM 来得更狠。
_PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
WORKSPACE_ROOT = os.getenv(
    "TG_WORKSPACE_ROOT", os.path.join(_PROJECT_DIR, "workspace")
)
MAX_TG_FILE_SIZE = 20 * 1024 * 1024
MEDIA_GROUP_WINDOW = 2.5  # 相册各分片是独立 message，需要一个收集窗口攒成单次任务
FILE_CAPTION_WINDOW = 3.5  # 无附言的文件先等一个窗口，接住用户随后补打的那句指令
TEXT_ABSORB_MAX_AGE = 3.0  # 文件到达时，回头吸收这个时限内刚到的文本作为其指令
logger = logging.getLogger("AGYHandler")

user_buffers = {}  # Message debouncing: {user_id: {"messages": list, "timer": Timer}}
user_buffers_lock = threading.Lock()  # 保护 user_buffers 防止防抖 Timer 竞态

# 每个用户当前正在组装的一批文件。相册聚合与"等你补一句指令"合并成同一个结算
# 定时器 —— 拆成两套会留下下载窗口的空档，那几秒到达的文字两头都认领不到。
file_batches = {}
file_batches_lock = threading.Lock()

# 同一用户的 agy 调用必须串行：并发进程写同一个 --conversation 会让模型
# 在一次回答里收到两份互相干扰的 prompt（"重复提示词扰动"）。
conv_locks = {}
conv_locks_guard = threading.Lock()


def _get_conv_lock(user_id):
    with conv_locks_guard:
        if user_id not in conv_locks:
            conv_locks[user_id] = threading.Lock()
        return conv_locks[user_id]


# INTERNAL_MARKER 只对打标记之后新建的会话生效。这些是打标记之前留在 BRAIN_DIR
# 里的内部会话特征，同样必须识别出来，否则它们会一直挂在 /history 里，
# 并且在 conv_id 为空时仍可能被误绑为用户主会话。
LEGACY_INTERNAL_SIGNATURES = (
    "你是文件处理命令 Planner",
    "你是一个专门用于生成文件处理命令的智能 Planner",
    "请判断：用户的核心意图是想利用系统能力对文件进行物理处理",
    "判断用户的核心意图：想对文件做物理处理",
)


def _is_internal_conversation(first_msg):
    if INTERNAL_MARKER in first_msg:
        return True
    return any(sig in first_msg for sig in LEGACY_INTERNAL_SIGNATURES)


def _clean_preview(raw):
    """剥掉 agy 的 XML 包装，只留用户真正说的那句话，供 /history 列表展示。"""
    text = re.sub(
        r"<ADDITIONAL_METADATA>.*?</ADDITIONAL_METADATA>", "", raw, flags=re.DOTALL
    )
    text = re.sub(
        r"<USER_SETTINGS_CHANGE>.*?</USER_SETTINGS_CHANGE>", "", text, flags=re.DOTALL
    )
    text = re.sub(r"</?USER_REQUEST>", "", text)
    return " ".join(text.split()) or "（新对话或空记录）"


def get_brain_conversations():
    """列出用户的真实会话。

    agy 没有 brain 目录隔离参数，Planner / 意图判定这类内部一次性调用同样会在
    BRAIN_DIR 里落一个会话。这些必须过滤掉，否则不但污染 /history，还会在
    conv_id 为空时被 execute_agy_prompt 当成"最近的会话"误绑为用户主会话。
    """
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
    """按产物类型选择投递方式。

    图片一律走 send_document：send_photo 会被 Telegram 二次压缩，
    那会直接抵消掉压缩/画质类任务的全部意义。

    GIF 同理但更狠：Telegram 按**文件内容**嗅探，只要认出是 GIF 就在服务端
    转成 H.264 MP4。实测 720x405 / 723 KB 的产物，无论走 send_animation
    还是 send_document，回传的都是 `x.gif.mp4` / video/mp4 / 320x180 / 21 KB
    —— 用户拿到的既不是 GIF，尺寸和画质也全丢了。唯一能逐字节保住原文件的
    是 send_document + disable_content_type_detection=True（实测下载回来
    sha256 与原文件一致）。代价是没有内联动图预览。

    其余类型无需特殊处理：mp4 / png / mp3 实测经 send_video / send_document /
    send_audio 回传后均与原文件逐字节相同。
    """
    ext = os.path.splitext(path)[1].lower()

    # 文本类产物（转写稿、提取的文字）用户是要读的，直接作为消息发出更顺手；
    # 过长才退回附件，避免刷屏
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


# 以「图片」方式上传时，Telegram 客户端会先把文件转成 JPEG 并压掉尺寸，
# 服务端收到的从来不是原图。不说这一句，用户看到的就是"我发的 png 怎么
# 变成又小又糊的 jpg"，而那三件事其实都发生在上传阶段。
TG_PHOTO_NOTICE = (
    "ℹ️ 这次的图片是以「<b>图片</b>」方式发送的 —— Telegram 在上传时已经把它"
    "转成 JPEG 并缩小了尺寸，我处理的是这份已经压过一轮的副本。\n"
    "要基于原图处理（保留 PNG、原始分辨率与画质），发送时<b>取消勾选「压缩」</b>，"
    "或直接用「<b>文件</b>」方式发送。"
)


def run_file_task(bot, message, file_paths, workspace_in, workspace_out,
                  caption, model, tg_photo=False):
    """驱动文件处理流水线，并用单条可编辑消息汇报进度。"""
    chat_id = message.chat.id
    try:
        status_msg = bot.send_message(
            chat_id, "⚙️ 已交给 AGY 处理...", reply_to_message_id=message.message_id
        )
    except Exception:
        status_msg = None

    last_text = {"value": ""}
    trace = {"started_at": time.strftime("%Y-%m-%d %H:%M:%S")}
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
        ok, products, reply, error = run_task(
            file_paths, workspace_in, workspace_out, caption, model, on_status,
            trace=trace,
        )

        if ok:
            # agy 的文字回复先发：它可能是"这就是答案"（用户只是问了个问题），
            # 也可能是"我这么做的、参数为什么这么选"的说明。分流已经删了，
            # 两种情况现在走同一条路 —— 有产物就连产物一起发，没产物就只发话。
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
                    # 超出 Bot API 上限，直接发会得到一句难懂的原始错误
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
        # 先尽力归档（move 走后原目录自然不存在），再无条件走一遍清理兜底 ——
        # 这样"归档没跑成"和"留痕被关掉"都不会留下泄漏路径
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


def sweep_workspaces():
    """启动时清空遗留的**在途**工作区（in/ 与 out/）。

    进程被 kill 或重启时，正在处理的任务走不到收尾分支，其工作区会永久留下。
    工作区在真实磁盘上（不再是重启即清空的 tmpfs），这道清扫是唯一的回收点。
    启动那一刻不可能有任务在飞，因此整体清空是安全的。

    archive/ 绝不在清扫范围内 —— 那是留痕，是任务结束后特意留下的证据，
    它的回收由 run_archive.prune 按配额负责。这里顺手 prune 一次：
    进程停了一段时间再起来时，过期的归档该在此刻就被回收，而不是等下一次任务。
    """
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
            # 显式复核而非只靠 ("in","out") 这个白名单：留痕一旦被误删就没了
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


def execute_agy_prompt(
    bot,
    message,
    prompt,
    get_user_state_fn,
    save_user_states_fn,
    attached_files=None,
    cleanup_dirs=None,
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

        env = agy_env()

        final_prompt = prompt
        if attached_files:
            joined = "\n".join(f"  - {p}" for p in attached_files)
            final_prompt = (
                f"{prompt}\n\n请读取并结合以下附件文件进行分析或回答：\n{joined}"
            )

        # 串行化同一用户的 agy 调用：conv_id 的读取、进程执行与回写必须原子完成，
        # 否则并发的两次调用会往同一个会话里塞两份 prompt，或各自绑定到不同的会话。
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
            conv_lock.release()
            stop_typing.set()
            typing_thread.join(timeout=1)
            _cleanup_dirs(cleanup_dirs)

    threading.Thread(target=process).start()


def register_agy_handlers(
    bot,
    allowed_user_id: int,
    get_user_state_fn,
    save_user_states_fn,
    get_main_keyboard_fn,
):
    """注册 Layer 3 AGY AI 指令与交互处理器"""
    sweep_workspaces()

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
            f"📌 <b>当前选定模型</b>: <code>{esc(current_m)}</code>",
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
            f"📌 <b>当前选定级别</b>: <code>{esc(current_e)}</code>",
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
        conv_str = f"<code>{esc(cid)}</code>" if cid else "<i>(无 / 新会话)</i>"
        mode_str = "💬 沉浸对话模式" if st.get("in_chat", False) else "📊 基础监控模式"
        voice_str = "🟢 开启" if st.get("auto_voice", False) else "🔴 关闭 (按需点播)"

        msg = (
            "⚙️ <b>AGY 会话配置与全局参数</b>\n"
            "──────────────────────\n"
            f"🤖 <b>运行 AI 模型</b>: <code>{esc(st.get('model', 'gemini-3.6-flash-high'))}</code>\n"
            f"⚡ <b>思考推理深度</b>: <code>{esc(st.get('effort', 'high'))}</code>\n"
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
                f"✅ <b>已将 AGY 交互模型切换为：</b> <code>{esc(m_id)}</code>",
                parse_mode="HTML",
            )
        elif call.data.startswith("seteffort:"):
            e_id = call.data.replace("seteffort:", "")
            st["effort"] = e_id
            save_user_states_fn()
            bot.answer_callback_query(call.id, f"思考深度已切换为: {e_id}")
            bot.send_message(
                call.message.chat.id,
                f"✅ <b>已将 AGY 思考推理深度切换为：</b> <code>{esc(e_id)}</code>",
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
        """把文件与用户原话原样交给 agy —— 不再分流。

        以前这里按关键词表判"物理处理 vs 视觉问答"，走两条不同链路。删掉了：
        那张表要靠猜用户会怎么说话，而"是回答还是动手"agy 看着文件和那句话
        自己就能判断。少一次预判，就少一处猜错的机会。
        """
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

        # 转发件把原作者的 caption 降级成了 context。用户自己写了评论就以评论
        # 为准；没写时那句原文是唯一的指令信号，不能当它不存在。
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
        """结算一批文件，交给意图判定分流。"""
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
        """重排结算定时器。必须在持有 file_batches_lock 时调用。

        等待时间按"距最后一个文件落地已过去多久"折算，而不是每次重排都从头
        再等一遍固定窗口 —— 否则用户补完指令后还要空等一个相册窗口才开工。
        """
        batch = file_batches.get(uid)
        if not batch or batch["fired"] or batch["downloading"] > 0:
            return  # 还有分片在下载，等最后一个下载完再排

        elapsed = time.time() - batch["last_file_at"]
        # 相册还可能有后续分片
        group_wait = (
            max(0.0, MEDIA_GROUP_WINDOW - elapsed) if batch["group_id"] else 0.0
        )
        # 还没拿到指令，再给用户一点时间补一句
        caption_wait = (
            0.0 if batch["caption"] else max(0.0, FILE_CAPTION_WINDOW - elapsed)
        )
        delay = max(0.05, group_wait, caption_wait)

        if batch["timer"]:
            batch["timer"].cancel()
        batch["timer"] = threading.Timer(delay, _batch_launch, args=(uid,))
        batch["timer"].start()

    def _claim_batch(message):
        """文本消息认领待定的文件批次，作为它的指令。

        与旧实现的关键差别：批次在**下载开始之前**就已登记，所以下载那几秒里
        到达的评论也能认领得到。旧实现要等下载完才登记驻留槽位，那段窗口里
        评论两头都够不着，只能作为独立对话请求发出去。
        """
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
            _arm_batch_locked(uid)  # 下载未完时此调用是空操作，由下载收尾负责排期
        return True

    def _claim_recent_text(user_id):
        """文件到达时反向吸收刚入防抖队列的文本（文本先到的情形）。

        Telegram 的"转发+评论"会把评论和内容拆成两条消息，且**评论先到**。
        评论会先进文本防抖队列，随后到达的文件若不把它吸收过来，两者就会
        各自发起一次请求，并发写同一个 conversation。
        """
        with user_buffers_lock:
            buf = user_buffers.get(user_id)
            if not buf:
                return ""
            # 只吸收刚刚到达的文本，避免把无关的上一句闲聊误当成文件指令
            if time.time() - buf.get("created_at", 0) > TEXT_ABSORB_MAX_AGE:
                return ""
            if buf["timer"]:
                buf["timer"].cancel()
            del user_buffers[user_id]
            return "\n".join(buf["messages"]).strip()

    def _ingest_file(message, file_id, preferred_name, known_size=0):
        """把文件并入该用户当前的"文件批次"，下载完成后由批次统一结算。

        两条铁律：
        1. **批次必须在下载开始之前登记**。下载是阻塞的网络 I/O，可能耗时数秒，
           而这几秒里到达的评论若找不到可认领的对象，就只能作为独立对话请求
           发出去 —— 于是一次操作收到两条互不相干的回复。
        2. 相册（media group）的每张图是独立 message，必须按 group_id 归入
           同一批次、同一工作区，否则"拼接/长图"这类多图任务不可能成功。
        """
        if known_size and known_size > MAX_TG_FILE_SIZE:
            _reject_oversize(message, known_size)
            return

        uid = message.from_user.id
        group_id = message.media_group_id

        # 转发件会把**原作者写的 caption** 一并带过来，那不是你的指令。
        # 你的指令是 TG 拆出去的那条独立评论消息。把原 caption 降级为上下文。
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
                # 与上一批无关的新文件：先让旧批按现状发车，不要混进来
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
                    # Telegram 的「图片」上传会在客户端就转码成 JPEG 并缩小
                    # 尺寸，我们拿到的从来不是原图。处理这类输入时要如实告知，
                    # 否则用户只会看到"我发的 png 怎么变成又小又糊的 jpg"。
                    "tg_photo": False,
                }
                file_batches[uid] = batch
                # 文本先到的情形（转发+评论：评论先发、内容后发）：
                # 把刚进防抖队列的评论吸收过来，别让它自己发一次
                absorbed = _claim_recent_text(uid)
                if absorbed:
                    batch["caption"] = absorbed
                    logger.info(f"文件批次吸收了刚到达的文本作为指令: {absorbed!r}")

            # preferred_name 为空只可能来自 handle_photo：handle_any_file 拿不到
            # 文件名时也会兜底成 file_<ts>.<ext>，绝不会传空
            if preferred_name is None:
                batch["tg_photo"] = True

            if caption:
                batch["caption"] = caption
            if context and not batch["context"]:
                batch["context"] = context
            # 用组内最早的 message 作为回复锚点，回传时挂在相册头部
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
            """落盘并把 downloading 计数归还，随后重排结算定时器。"""
            with file_batches_lock:
                if blob is not None:
                    os.makedirs(workspace_in, exist_ok=True)
                    os.makedirs(workspace_out, exist_ok=True)
                    existing = [
                        f for f in os.listdir(workspace_in)
                        if os.path.isfile(os.path.join(workspace_in, f))
                    ]
                    # 文件名会原样进入 Planner 生成的 shell 命令，必须先收敛
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

    @bot.message_handler(content_types=["voice"])
    def handle_voice(message):
        if message.from_user.id != allowed_user_id:
            return
        # 与图片/文档/视频保持一致：仅在对话模式下响应
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

        # 刚发了文件还没给指令？这句文字就是它的指令，不另起一次会话请求
        if _claim_batch(message):
            return True

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
                    "created_at": time.time(),
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
