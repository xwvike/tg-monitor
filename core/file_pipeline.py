"""
Layer 3.5: 文件处理流水线 (core/file_pipeline.py)

职责边界（与旧实现相比做了一次反转）：
  - Python 负责**确定性**的部分：选菜谱、探元数据、执行命令、捕获 stderr、回收产物
  - LLM 只负责**需要判断力**的部分：把"用户想干什么"翻译成一组 bash 命令

关键改动：
  1. 菜谱由扩展名 + 关键词在 Python 侧命中后直接内联进 prompt，Planner 不再自己 ls 目录
  2. 执行带 cwd / timeout / capture_output，失败时把 stderr **回喂**给 Planner 重规划
  3. 产物落错目录（相对路径）时自动兜底回收，不再静默返回"未生成任何文件"
"""

import json
import logging
import os
import re
import shutil
import subprocess
import zipfile

from core.tg_format import esc

logger = logging.getLogger("FilePipeline")

AGY_BIN = os.path.expanduser("~/.local/bin/agy")
PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RECIPE_DIR = os.path.join(PROJECT_DIR, "config", "file_recipes")
TOOLCHAIN_FILE = os.path.join(PROJECT_DIR, "config", "TOOLCHAIN.md")

# agy 没有提供 brain 目录隔离参数，内部调用会和用户的真实会话混在同一个 BRAIN_DIR 里。
# 所有内部 prompt 统一打上此标记，供 get_brain_conversations() 过滤，
# 否则 Planner 的一次性会话会污染 /history，甚至在 conv_id 为空时被误绑为用户主会话。
INTERNAL_MARKER = "[[TG-MONITOR-INTERNAL]]"

MAX_PLAN_ATTEMPTS = 2  # 首次规划 + 1 次带错误回喂的重规划
# 产物体积的"关注线"：低于它一律不折腾（几 MB 的产物没人在意比例）。
# 它**不是**硬上限 —— 超过它还要与输入体量比较，才判断是否属于参数失当。
OVERSIZE_FLOOR_BYTES = 5 * 1024 * 1024
# 产物相对输入的膨胀倍数。超过才认为是**参数选错**而非任务本身就大。
OVERSIZE_RATIO = 1.5
# Telegram Bot API 的单文件上传上限
TG_UPLOAD_LIMIT_BYTES = 50 * 1024 * 1024
# 产物数量上限。超过则打包成单个 zip 再投递 —— "PDF 按页转图片"这类任务
# 动辄产出几十个文件，逐个发送会刷屏并触碰 Telegram 频率限制。
# 打包放在 Python 侧（标准库 zipfile），不依赖系统 zip，也不指望 Planner 记得。
MAX_INLINE_PRODUCTS = 8
CMD_TIMEOUT = 180  # 单条 bash 命令超时（秒）
PLAN_TIMEOUT = 150  # 单次规划调用超时（秒）
ROUTER_TIMEOUT = 20  # 意图兜底判定超时（秒）

# 文本类产物：转写稿、提取的文字等，用户是要"读"而不是"下载"
TEXT_EXTS = {".txt", ".srt", ".vtt", ".md", ".csv", ".json"}
# 超过这个长度就仍按文件发送，避免刷屏
INLINE_TEXT_MAX_CHARS = 3000

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tiff", ".tif", ".gif"}
VIDEO_EXTS = {".mp4", ".mov", ".avi", ".webm", ".mkv", ".flv", ".m4v"}
AUDIO_EXTS = {".mp3", ".m4a", ".ogg", ".oga", ".wav", ".flac", ".aac", ".opus"}


# ------------------------------------------------------------------------------
# 菜谱索引：扩展名 + 关键词 → Recipe 文件
# ------------------------------------------------------------------------------

RECIPE_INDEX = [
    {
        "file": "image_compression.md",
        "exts": {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tiff"},
        "keywords": [
            "压缩", "压一下", "压小", "小一点", "缩小", "减小体积", "减肥",
            "瘦身", "compress", "reduce size", "降质量",
        ],
    },
    {
        "file": "image_stitching.md",
        "exts": {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".zip", ".rar"},
        "keywords": [
            "拼接", "拼图", "拼成", "长图", "左右拼", "上下拼", "合成一张",
            "合并成", "stitch", "montage",
        ],
    },
    {
        "file": "video_to_gif.md",
        "exts": {".mp4", ".mov", ".avi", ".webm", ".mkv", ".flv"},
        "keywords": ["gif", "动图", "表情包"],
    },
    {
        "file": "media_transcribe.md",
        "exts": {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v",
                 ".mp3", ".m4a", ".wav", ".ogg", ".oga", ".flac", ".aac", ".opus"},
        "keywords": [
            "转文字", "转写", "语音识别", "说了什么", "讲了什么", "提取对话",
            "提取台词", "字幕", "听写", "会议记录", "转成文字", "文字稿",
        ],
    },
    {
        "file": "document_convert.md",
        "exts": {".md", ".markdown", ".html", ".htm", ".docx", ".odt",
                 ".epub", ".rst", ".txt", ".tex"},
        "keywords": [
            "转word", "转docx", "转md", "转markdown", "转html", "转网页",
            "转epub", "转电子书", "转txt", "格式转换", "转成word", "转rst",
        ],
    },
    {
        "file": "office_convert.md",
        "exts": {".xls", ".xlsx", ".doc", ".docx", ".ppt", ".pptx",
                 ".ods", ".odt", ".odp", ".rtf"},
        "keywords": [
            "转pdf", "转成pdf", "转为pdf", "导出pdf", "打印成pdf", "存成pdf",
            "转excel", "转xlsx", "转ppt", "转pptx", "转表格", "转幻灯片",
            "另存为",
        ],
    },
    {
        "file": "video_compression.md",
        "exts": {".mp4", ".mov", ".avi", ".webm", ".mkv", ".flv", ".m4v"},
        "keywords": [
            "压缩", "压一下", "压压", "小一点", "减小", "缩小", "瘦身",
            "太大", "发不出去", "邮件附件", "compress", "reduce size",
        ],
    },
    {
        "file": "video_trim.md",
        "exts": {".mp4", ".mov", ".avi", ".webm", ".mkv", ".flv", ".m4v"},
        "keywords": [
            "剪辑", "剪掉", "剪去", "剪一下", "去掉", "删掉", "截取", "只要",
            "保留", "掐头去尾", "合并视频", "拼接视频", "接起来", "拼起来",
            "分段", "从第", "秒到",
        ],
    },
    {
        "file": "images_to_pdf.md",
        "exts": {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tiff", ".tif"},
        "keywords": [
            "转pdf", "转成pdf", "做成pdf", "合成pdf", "合并成pdf",
            "生成pdf", "打包成pdf", "存成pdf", "导出pdf", "转为pdf",
        ],
    },
    {
        "file": "pdf_compression.md",
        "exts": {".pdf"},
        "keywords": [
            "压缩", "压一下", "减小", "小一点", "缩小", "瘦身", "太大",
            "compress", "reduce size", "邮件附件",
        ],
    },
    {
        "file": "pdf_to_images.md",
        "exts": {".pdf"},
        "keywords": [
            "转图片", "转成图", "转为图", "按页", "拆成图", "每页一张",
            "转png", "转jpg", "转jpeg", "导出图片", "截图",
            "每页", "导出", "分页", "一页一张",
        ],
    },
]

# 明确指向"物理处理"的措辞
PROCESS_PHRASES = [
    "压缩", "压一下", "压小", "小一点", "缩小", "减小体积", "减肥", "瘦身",
    "转成", "转为", "转换", "转格式", "转码", "转gif", "动图", "表情包",
    "拼接", "拼图", "拼成", "长图", "合并", "合成", "裁剪", "裁切", "切掉",
    "旋转", "翻转", "缩放", "改尺寸", "分辨率", "加水印", "水印",
    "去除元数据", "去exif", "去掉exif", "提取音频", "抽取音频", "分离音轨",
    "静音", "变速", "加速", "减速", "帧率", "码率", "导出", "另存",
    "转pdf", "转word", "transcode", "转docx", "转md", "转markdown",
    "剪辑", "剪掉", "剪去", "掐头去尾", "合并视频", "拼接视频", "接起来",
    "转文字", "转写", "语音识别", "提取对话", "提取台词", "字幕", "听写",
    "文字稿", "转成文字", "会议记录", "转word", "转docx", "转html", "转网页", "转epub",
    "转电子书", "转txt", "格式转换", "转成word",
    "去掉", "删掉", "秒到", "秒删", "秒剪",
    "太大", "压压", "转图片", "转成图", "转为图", "转png", "转jpg", "转jpeg",
    "按页", "每页", "拆成图", "分页",
    "转成pdf", "做成pdf", "合成pdf", "合并成pdf", "生成pdf", "打包成pdf",
    "转html", "转epub", "电子书", "分割", "切割", "截取", "降噪",
    "转为pdf", "打印成pdf", "存成pdf", "转excel", "转xlsx", "转ppt",
    "转pptx", "转表格", "转幻灯片", "另存为",
    "compress", "resize", "crop", "rotate", "merge", "stitch", "watermark",
    "extract audio", "convert to",
]

# 明确指向"视觉问答 / 内容理解"的措辞
QA_PHRASES = [
    "这是什么", "是什么", "什么意思", "解释", "讲解", "分析", "看看", "看下",
    "帮我看", "识别", "认出", "翻译", "总结", "概括", "提取文字", "提取文本",
    "识别文字", "ocr", "读一下", "念一下", "描述", "说了什么", "写的什么",
    "报错", "错误原因", "为什么", "怎么办", "如何解决", "哪里有问题",
    "有没有", "是不是", "内容是", "讲了什么", "评价", "点评",
]


_UNSAFE_NAME_CHARS = re.compile(r"[^\w.\-]", re.UNICODE)


def safe_filename(name, fallback="file"):
    """把外部提供的文件名收敛成 shell 安全的形式。

    execute_commands 以 shell=True 执行 Planner 生成的命令，而输入文件的
    **绝对路径会原样出现在命令里**。文件名中的 `;` `$()` 反引号 `|` `&`
    会被 shell 解释 —— 用户只要转发一个来自频道的恶意命名文件即可触发
    任意命令执行。os.path.basename() 只挡路径穿越，不挡元字符。

    保留中日韩等文字（它们不是 shell 元字符），其余一律替换为下划线。
    """
    name = os.path.basename(str(name or "")).strip()
    stem, ext = os.path.splitext(name)

    ext = "." + _UNSAFE_NAME_CHARS.sub("", ext.lstrip("."))[:16] if ext else ""
    stem = _UNSAFE_NAME_CHARS.sub("_", stem)[:80].strip("._-")

    if not stem:
        stem = fallback
    return f"{stem}{ext if ext != '.' else ''}"


def agy_env():
    """构造调用 agy 及执行工具命令时的环境变量。

    代理地址取自 .env 的 TG_PROXY（由 bot.py 的 load_dotenv 注入），
    不再硬编码 —— 换台机器或改端口时只需改一处配置。
    """
    env = os.environ.copy()
    proxy = os.getenv("TG_PROXY", "").strip().strip("\"'") or os.getenv("HTTP_PROXY", "")
    if proxy:
        env["HTTP_PROXY"] = proxy
        env["HTTPS_PROXY"] = proxy
        # 本机服务（TTS/STT/OCR）必须直连，否则要依赖代理自身的私网路由规则
        env.setdefault("NO_PROXY", "localhost,127.0.0.1,::1")
    env["PATH"] = f"{os.path.expanduser('~/.local/bin')}:{env.get('PATH', '')}"
    return env


_agy_env = agy_env  # 兼容旧调用点


def _is_auth_failure(text):
    lowered = text.lower()
    return (
        "authentication required" in lowered
        or "authentication failed" in lowered
        or "authentication timed out" in lowered
    )


AUTH_HINT = (
    "🔑 <b>AGY 认证失效</b>\n"
    "──────────────────────\n"
    "底层 agy CLI 的登录凭证已过期。\n\n"
    "💡 请在服务器终端运行 <code>agy</code> 重新完成登录认证。"
)


def call_agy(prompt, model, timeout):
    """统一的 agy 一次性调用入口。返回 (ok, stdout, err_kind)。

    err_kind ∈ {None, "auth", "timeout", "exit", "exception"}，
    让调用方能区分"模型格式没吐对"和"根本没跑起来"——旧实现把两者混为一谈。
    """
    cmd = [AGY_BIN, "--dangerously-skip-permissions"]
    if model:
        cmd.extend(["--model", model])
    cmd.extend(["-p", prompt])

    try:
        res = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=_agy_env(),
            cwd=PROJECT_DIR,
        )
    except subprocess.TimeoutExpired:
        return False, "", "timeout"
    except Exception as e:
        logger.error(f"调用 agy 异常: {e}")
        return False, "", "exception"

    combined = (res.stdout or "") + (res.stderr or "")
    if _is_auth_failure(combined):
        return False, combined, "auth"
    if res.returncode != 0:
        logger.error(f"agy 退出码 {res.returncode}: {combined[-800:]}")
        return False, combined, "exit"
    return True, res.stdout or "", None


# ------------------------------------------------------------------------------
# 意图判定：关键词短路优先，仅在真模糊时才烧一次模型调用
# ------------------------------------------------------------------------------


# 对**音视频**而言，"说了什么/讲了什么"要的是把话转成文字，而不是让模型去
# 描述画面 —— 视觉问答链路拿到视频也听不懂音轨。同样的问法对图片则确实是问答。
SPEECH_QA_PHRASES = (
    "说了什么", "讲了什么", "说什么", "讲什么", "说了啥", "讲了啥",
    "聊了什么", "内容是什么", "在说什么",
)
SPEECH_MEDIA_EXTS = (
    VIDEO_EXTS | AUDIO_EXTS | {".m4v", ".oga"}
)


def _is_speech_media(file_names):
    return any(
        os.path.splitext(n)[1].lower() in SPEECH_MEDIA_EXTS for n in file_names
    )


def classify_intent(caption, file_names, model):
    """判断是"物理处理"还是"视觉问答"。返回 (is_processing, how)。

    how ∈ {"empty", "keyword", "model", "fallback"}，仅用于日志排查。
    """
    if not caption or not caption.strip():
        # 无附言一律走问答：非破坏性，用户想处理时补一句话即可。
        # 旧实现里文档无附言会被强行"压缩/规范化"，那是纯粹的误伤。
        return False, "empty"

    low = caption.lower()

    # 音视频 + "讲了什么"这类问法 → 实际需求是语音转写
    if _is_speech_media(file_names) and any(p in low for p in SPEECH_QA_PHRASES):
        return True, "keyword"

    proc_hits = [p for p in PROCESS_PHRASES if p in low]
    qa_hits = [p for p in QA_PHRASES if p in low]

    if proc_hits and not qa_hits:
        return True, "keyword"
    if qa_hits and not proc_hits:
        return False, "keyword"

    # 双方都命中或都没命中 —— 这才值得问模型
    names = ", ".join(file_names) or "未知文件"
    prompt = (
        f"{INTERNAL_MARKER}\n"
        f"系统具备的文件处理能力：FFmpeg 音视频处理、ImageMagick/pngquant 图片处理、"
        f"Pandoc/Poppler 文档转换。\n"
        f"用户上传了文件 [{names}]，附带指令：\"{caption}\"\n\n"
        f"判断用户的核心意图：想对文件做物理处理/转换/压缩（回答 True），"
        f"还是视觉问答/内容解释/文字提取（回答 False）。\n"
        f"只输出一个单词：True 或 False。"
    )
    ok, out, err_kind = call_agy(prompt, "gemini-3.6-flash-high", ROUTER_TIMEOUT)

    if ok:
        # 只看 stdout 的最后一个非空行并做整词匹配。
        # 旧实现在 stdout+stderr 上做 `"true" in output` 子串判断，
        # 任何一行带 true 的 CLI 日志都会把它带偏。
        for line in reversed((out or "").strip().splitlines()):
            token = line.strip().strip("`*。.！!\"' ").lower()
            if token in ("true", "yes", "是"):
                return True, "model"
            if token in ("false", "no", "否"):
                return False, "model"

    logger.warning(f"意图判定未取得明确结论 (err={err_kind})，回退到关键词倾向")
    # 判定失败时按关键词倾向兜底，而不是无条件倒向问答
    return bool(proc_hits), "fallback"


# ------------------------------------------------------------------------------
# 菜谱与工具链：Python 侧选好直接内联，省掉 Planner 的目录探索往返
# ------------------------------------------------------------------------------


def select_recipes(file_names, caption):
    """按扩展名 + 关键词命中菜谱，返回 [(文件名, 正文), ...]。"""
    exts = {os.path.splitext(n)[1].lower() for n in file_names}
    low = (caption or "").lower()

    scored = []
    for entry in RECIPE_INDEX:
        if not (exts & entry["exts"]):
            # 扩展名对不上就别塞 —— 给 PDF 任务提供讲 pngquant 的手册只会干扰规划
            continue
        # 按**最长命中关键词**打分：「合并成pdf」应判定为图片转 PDF，
        # 而不是被更短的「合并成」抢给拼接手册
        hits = [k for k in entry["keywords"] if k in low]
        scored.append((max((len(k) for k in hits), default=0), entry["file"]))

    # 只要有关键词命中，就不再提供纯靠扩展名兜底的手册（那些多半是噪声）
    if any(score > 0 for score, _ in scored):
        scored = [item for item in scored if item[0] > 0]

    scored.sort(key=lambda x: -x[0])
    picked = []
    for _score, fname in scored[:2]:
        path = os.path.join(RECIPE_DIR, fname)
        try:
            with open(path, "r", encoding="utf-8") as f:
                picked.append((fname, f.read()))
        except Exception as e:
            logger.warning(f"读取菜谱 {fname} 失败: {e}")
    return picked


def load_toolchain():
    """载入工具链清单，裁掉与文件处理无关的服务列表段落。"""
    try:
        with open(TOOLCHAIN_FILE, "r", encoding="utf-8") as f:
            text = f.read()
    except Exception as e:
        logger.warning(f"读取 TOOLCHAIN.md 失败: {e}")
        return "（工具链清单不可用，请仅使用 ffmpeg / convert / pngquant / pandoc / pdftotext 等常见工具）"

    # "## 📦 其他已知服务" 之后是 Postgres/Redis/qBittorrent 之类的噪声，对规划无用
    cut = text.find("## 📦")
    return text[:cut].strip() if cut > 0 else text.strip()


# ------------------------------------------------------------------------------
# 元数据探针：给 Planner 事实，省得它自己开一轮 tool call 去 identify
# ------------------------------------------------------------------------------


def _human_size(num):
    for unit in ("B", "KB", "MB", "GB"):
        if num < 1024 or unit == "GB":
            return f"{num:.1f}{unit}" if unit != "B" else f"{int(num)}B"
        num /= 1024
    return f"{num:.1f}GB"


def probe_file(path):
    """返回一行人类可读的文件元数据描述，任何探针失败都不影响主流程。"""
    name = os.path.basename(path)
    ext = os.path.splitext(name)[1].lower()
    try:
        size = os.path.getsize(path)
    except OSError:
        return f"{name} (无法读取大小)"

    parts = [f"大小 {_human_size(size)}"]

    try:
        if ext in IMAGE_EXTS:
            res = subprocess.run(
                ["identify", "-format", "%wx%h %m", path],
                capture_output=True, text=True, timeout=15,
            )
            if res.returncode == 0 and res.stdout.strip():
                parts.append(res.stdout.strip().split("\n")[0])
        elif ext in VIDEO_EXTS or ext in AUDIO_EXTS:
            res = subprocess.run(
                ["ffprobe", "-v", "error", "-print_format", "json",
                 "-show_format", "-show_streams", path],
                capture_output=True, text=True, timeout=25,
            )
            if res.returncode == 0:
                data = json.loads(res.stdout)
                dur = data.get("format", {}).get("duration")
                if dur:
                    parts.append(f"时长 {float(dur):.1f}s")
                vstream = next(
                    (s for s in data.get("streams", []) if s.get("codec_type") == "video"),
                    None,
                )
                if vstream and vstream.get("width"):
                    parts.append(f"{vstream['width']}x{vstream['height']} {vstream.get('codec_name', '')}".strip())
                # 码率是判断"是否已经压过"的关键依据：对低码率视频再压
                # 很容易越压越大（实测 CRF23 重压 CRF28 的片子会大 36%）
                bitrate = vstream.get("bit_rate") if vstream else None
                if not bitrate:
                    bitrate = data.get("format", {}).get("bit_rate")
                if bitrate:
                    try:
                        parts.append(f"码率 {int(bitrate) // 1000}kbps")
                    except (TypeError, ValueError):
                        pass
                astream = next(
                    (s for s in data.get("streams", []) if s.get("codec_type") == "audio"),
                    None,
                )
                if astream:
                    parts.append(f"音轨 {astream.get('codec_name', '?')}")
        elif ext == ".pdf":
            res = subprocess.run(
                ["pdfinfo", path], capture_output=True, text=True, timeout=15
            )
            if res.returncode == 0:
                for line in res.stdout.splitlines():
                    if line.startswith("Pages:"):
                        parts.append(line.strip())
                        break
    except Exception as e:
        logger.debug(f"元数据探针跳过 {name}: {e}")

    return f"{name} ({', '.join(parts)})"


# ------------------------------------------------------------------------------
# 规划
# ------------------------------------------------------------------------------


def extract_rejection(output):
    """取出 Planner 的拒绝说明。

    需求本身不成立时（视频转 Word 之类），硬凑命令只会失败并回一句
    "未能输出合法的命令序列" —— 那是把"做不到"伪装成了"格式错误"。
    给它一条明确的拒绝通道，用户才能拿到有用的解释。
    """
    m = re.search(r"<reject>(.*?)</reject>", output, re.DOTALL)
    return " ".join(m.group(1).split()) if m else ""


def _extract_commands(output):
    """从 Planner 输出里提取命令数组。优先 <json> 标签，再退到代码块与裸数组。"""
    candidates = []
    m = re.search(r"<json>(.*?)</json>", output, re.DOTALL)
    if m:
        candidates.append(m.group(1))
    for m in re.finditer(r"```(?:json)?\s*(\[.*?\])\s*```", output, re.DOTALL):
        candidates.append(m.group(1))
    for m in re.finditer(r"(\[\s*\".*?\"\s*\])", output, re.DOTALL):
        candidates.append(m.group(1))

    for raw in candidates:
        try:
            parsed = json.loads(raw.strip())
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, list) and all(isinstance(c, str) for c in parsed):
            cleaned = [c.strip() for c in parsed if c.strip()]
            if cleaned:
                return cleaned
    return None


# GIF 宽度不能预设成一个常数。GIF 无帧间压缩，体积几乎只由「输出像素数 ×
# 画面复杂度」决定，而复杂度在真实素材间相差极大 —— 实测同为 2940x1912、
# 同为 1440 宽输出，屏幕录制 6s 只有 1.8 MB，全屏渐变 10s 却是 39 MB。
#
# 源视频的 h264 每像素字节数（bpp）是免费拿得到的复杂度探针：它衡量的正是
# 时空冗余，也正是驱动 GIF 体积的同一件事。实测锚点：
#
#   bpp 0.0046  屏幕录制（Retina 2940x1912）  1440 宽 / 6s  →  1.8 MB
#   bpp 0.0201  全屏渐变（mandelbrot）        1440 宽 / 10s →   39 MB
#                                             720 宽 / 10s  →   12 MB
#   bpp 0.0683  纯噪声                         720 宽 / 10s  →   17 MB
#
# 因此分档而不是套公式：只有两端有实测锚点，中间档是两者之间的插值，
# 没有独立实测支撑。高 bpp 档沿用原来的 720，保证不会比改动前更糟。
GIF_WIDTH_TIERS = (
    (0.008, 1440),   # 屏幕录制 / 界面 / 动画：大片纯色，放大到 1440 也很小
    (0.02, 960),     # 一般实拍（插值档，无独立锚点）
    (float("inf"), 720),  # 全屏运动 / 高熵：维持原默认，不加码
)
# 抖动同样由这个信号决定，两端都实测过：屏幕内容是纯色块 + 锐利边缘，
# 抖动只是加噪点而 LZW 压不掉 —— 实测 1470 宽下 dither=none 比
# bayer:bayer_scale=5 体积相同而文字区 SSIM 更高（0.9723 vs 0.9702）。
# 渐变/实拍素材上结论相反，bayer_scale=5 明显优于关闭抖动（会出色带）。
GIF_FLAT_CONTENT_BPP = 0.008


def source_bits_per_pixel(path):
    """源视频的 h264 每像素字节数。探针失败返回 None（调用方据此不给建议）。"""
    try:
        res = subprocess.run(
            ["ffprobe", "-v", "error", "-print_format", "json",
             "-show_format", "-show_streams", path],
            capture_output=True, text=True, timeout=25,
        )
        if res.returncode != 0:
            return None
        data = json.loads(res.stdout)
        v = next((s for s in data.get("streams", [])
                  if s.get("codec_type") == "video"), None)
        if not v or not v.get("width") or not v.get("height"):
            return None
        dur = float(data.get("format", {}).get("duration")
                    or v.get("duration") or 0)
        num, _, den = (v.get("avg_frame_rate") or "0/1").partition("/")
        fps = float(num) / float(den or 1) if float(den or 1) else 0
        frames = dur * fps
        if frames <= 0:
            return None
        size = os.path.getsize(path)
        return size / (int(v["width"]) * int(v["height"]) * frames)
    except Exception as e:
        logger.warning(f"探测源复杂度失败 {os.path.basename(path)}: {e}")
        return None


def suggest_gif_params(path):
    """按源复杂度给出 GIF 的建议宽度与抖动方式。无法判断时返回 None。

    这是"确定的事交给代码"的一例：宽度该多少是可以从元数据算出来的，
    交给 Planner 凭感觉挑只会挑出一个常数 —— 旧菜谱写死 720，于是
    2940 宽的 Retina 录屏被压到 24.5% 宽，文字必糊（文字区 SSIM 0.916，
    同素材 1470 宽为 0.970）。
    """
    bpp = source_bits_per_pixel(path)
    if bpp is None:
        return None
    try:
        res = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width", "-of", "csv=p=0", path],
            capture_output=True, text=True, timeout=15,
        )
        src_width = int((res.stdout or "").strip().split(",")[0])
    except Exception:
        return None

    cap = next(w for threshold, w in GIF_WIDTH_TIERS if bpp < threshold)
    return {
        "bpp": bpp,
        "src_width": src_width,
        # 绝不放大：源比建议值还窄时沿用源宽
        "width": min(src_width, cap),
        "dither": "none" if bpp < GIF_FLAT_CONTENT_BPP else "bayer:bayer_scale=5",
    }


def _gif_hint_block(file_paths, recipe_names):
    """命中转 GIF 手册时，把代码算出的宽度/抖动建议附在 prompt 里。"""
    if "video_to_gif.md" not in recipe_names:
        return ""
    lines = []
    for p in file_paths:
        if os.path.splitext(p)[1].lower() not in VIDEO_EXTS:
            continue
        hint = suggest_gif_params(p)
        if not hint:
            continue
        lines.append(
            f"  - {os.path.basename(p)}: 建议 scale={hint['width']}:-1、"
            f"dither={hint['dither']}"
            f"（源宽 {hint['src_width']}，实测复杂度 bpp={hint['bpp']:.4f}）"
        )
    if not lines:
        return ""
    return (
        "\n## 本次 GIF 参数建议（由代码按源视频实测复杂度推算，优先采用）\n"
        + "\n".join(lines)
        + "\n手册里的默认宽度是通用值；上面这行是针对本次素材算出来的，冲突时以它为准。\n"
    )


def build_plan_prompt(file_paths, workspace_out, caption, failure=None):
    metadata = "\n".join(f"  - {probe_file(p)}" for p in file_paths)
    path_list = "\n".join(f"  - {p}" for p in file_paths)

    recipes = select_recipes([os.path.basename(p) for p in file_paths], caption)
    if recipes:
        recipe_block = "\n\n".join(
            f"===== 标准作业手册: {name} =====\n{body}" for name, body in recipes
        )
    else:
        recipe_block = "（本次任务没有匹配到预置手册，请依据工具链自行规划）"

    prompt = (
        f"{INTERNAL_MARKER}\n"
        "你是文件处理命令 Planner。你的唯一输出是一组可直接执行的 bash 命令。\n\n"
        "## 输入文件（绝对路径，已为你探好元数据，不要再去读取文件内容）\n"
        f"{path_list}\n\n"
        "## 文件元数据\n"
        f"{metadata}\n\n"
        "## 用户需求\n"
        f"{caption}\n\n"
        "## 可用工具链\n"
        f"{load_toolchain()}\n\n"
        "## 相关标准作业手册\n"
        f"{recipe_block}\n"
        f"{_gif_hint_block(file_paths, [name for name, _ in recipes])}\n"
        "## 硬性约束\n"
        f"1. 所有产物必须写入输出目录：{workspace_out}\n"
        "2. 命令中**所有**输入与输出路径必须是绝对路径。手册示例里的 "
        "`image1.jpg`、`$INPUT`、`$OUTPUT_DIR` 等占位写法必须由你替换成上面的真实绝对路径——"
        "执行环境不会为你定义任何变量。\n"
        "3. 需要临时文件时，放在输出目录下并在最后一条命令中删除，不要用 /tmp 里的固定文件名。\n"
        "4. 不要读取、解析或向用户描述文件内容；只做转换。\n"
        "5. **你只负责产出命令，不要自己动手执行任何东西**：不得安装软件包\n"
        "   （apt/pip/npm 等）、不得改动系统配置、不得试运行命令。缺少工具时\n"
        "   改用现有工具链完成，或按第 7 条说明做不到的原因。\n"
        "6. 只输出被 <json></json> 包裹的字符串数组，数组外不要有任何解释文字。\n"
        "7. **若该需求在技术上不成立**（例如把视频转成 Word、把音频转成图片这类\n"
        "   源格式与目标格式毫无关系的要求），不要硬凑命令，改为输出\n"
        "   <reject>一句话说明为什么做不到，并给出可行的替代做法</reject>\n\n"
        f"输出格式示例：\n<json>[\"convert /abs/in.jpg -strip -quality 70 {workspace_out}/out.jpg\"]</json>"
    )

    if failure:
        prompt += (
            "\n\n## ⚠️ 上一轮规划执行失败，请修正后重新输出\n"
            f"失败的命令：\n{failure['cmd']}\n\n"
            f"错误输出：\n{failure['stderr']}\n\n"
            "请分析上述错误原因（路径不存在？参数不被支持？工具没装？输出没落到目标目录？），"
            "给出修正后的完整命令序列。"
        )
    return prompt


# ------------------------------------------------------------------------------
# 执行
# ------------------------------------------------------------------------------


def execute_commands(commands, cwd):
    """逐条执行。返回 (ok, failure)；failure 为 {"cmd", "stderr"}。

    与旧实现的区别：带 cwd（相对路径不再污染仓库根目录）、带 timeout（写坏的
    ffmpeg 不会永久挂死线程）、捕获 stderr（用户能拿到真正的错因而不是"返回码 1"）。
    """
    env = _agy_env()
    for idx, raw_cmd in enumerate(commands, 1):
        logger.info(f"[{idx}/{len(commands)}] 执行: {raw_cmd}")
        try:
            res = subprocess.run(
                raw_cmd,
                shell=True,
                executable="/bin/bash",
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=CMD_TIMEOUT,
                env=env,
            )
        except subprocess.TimeoutExpired:
            return False, {
                "cmd": raw_cmd,
                "stderr": f"命令执行超过 {CMD_TIMEOUT} 秒被强制终止。",
            }
        except Exception as e:
            return False, {"cmd": raw_cmd, "stderr": f"命令无法启动: {e}"}

        if res.returncode != 0:
            detail = (res.stderr or res.stdout or "").strip() or "(工具没有输出任何错误信息)"
            logger.error(f"命令失败 (rc={res.returncode}): {detail[-500:]}")
            return False, {"cmd": raw_cmd, "stderr": detail[-1500:]}
    return True, None


def collect_outputs(workspace_in, workspace_out, original_inputs):
    """回收产物。若输出目录为空，兜底把输入目录里新增的文件搬过去。

    Planner 抄手册示例写出相对路径时产物会落在 cwd（即输入目录），
    旧实现在这种情况下只会回一句"未生成任何文件"，把成功的处理判成失败。
    """
    products = sorted(
        f for f in os.listdir(workspace_out)
        if os.path.isfile(os.path.join(workspace_out, f))
    )
    if products:
        return [os.path.join(workspace_out, f) for f in products]

    strays = [
        f for f in os.listdir(workspace_in)
        if os.path.isfile(os.path.join(workspace_in, f)) and f not in original_inputs
    ]
    for f in strays:
        try:
            shutil.move(os.path.join(workspace_in, f), os.path.join(workspace_out, f))
            logger.info(f"兜底回收落在输入目录的产物: {f}")
        except Exception as e:
            logger.warning(f"回收产物 {f} 失败: {e}")

    return sorted(
        os.path.join(workspace_out, f)
        for f in os.listdir(workspace_out)
        if os.path.isfile(os.path.join(workspace_out, f))
    )


def package_products(products, workspace_out, archive_stem="output"):
    """产物过多时打包成单个 zip；未超阈值则原样返回。

    返回 (最终投递列表, 是否已打包)。
    """
    if len(products) <= MAX_INLINE_PRODUCTS:
        return products, False

    archive = os.path.join(workspace_out, f"{safe_filename(archive_stem, 'output')}.zip")
    try:
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
            for path in sorted(products):
                zf.write(path, os.path.basename(path))
    except Exception as e:
        logger.error(f"打包产物失败，改为逐个投递: {e}")
        return products, False

    for path in products:
        try:
            os.remove(path)
        except OSError:
            pass
    logger.info(f"产物 {len(products)} 个超过 {MAX_INLINE_PRODUCTS}，已打包为 {os.path.basename(archive)}")
    return [archive], True


def _disproportionate_products(products, input_bytes):
    """判断产物是否**不成比例地大**（即参数选错），而非任务本身就大。

    只用绝对阈值会误伤合理的大任务：100 张图合成 PDF 无论如何都超 10 MB，
    此时要求降参只会白白毁画质、且仍然超标。真正该拦的是"输出远大于输入"
    这种参数失当 —— 比如 2 MB 视频转出 9 MB 的 GIF。

    返回描述串；判定为正常则返回空串。
    """
    total = sum(os.path.getsize(p) for p in products)
    if total <= OVERSIZE_FLOOR_BYTES:
        return ""
    if input_bytes and total <= input_bytes * OVERSIZE_RATIO:
        # 与输入体量相称 —— 任务本身就大，不是参数问题
        return ""
    return (
        f"产物合计 {_human_size(total)}，而输入仅 {_human_size(input_bytes)}"
        if input_bytes else f"产物合计 {_human_size(total)}"
    )


def plan_and_execute(file_paths, workspace_in, workspace_out, caption, model,
                     on_status=None, trace=None):
    """规划 → 执行 → 失败回喂重规划。返回 (ok, products, error_message)。

    trace 是调用方传进来的 dict，本函数往里填「这次到底干了什么」：命中的菜谱、
    每一轮的命令与失败原因。它会被 run_archive 写到产物旁边的 run.json —— 命令
    单看日志也有，但和它产出的文件对不上号，调参就只能靠猜。
    """

    def status(text):
        if on_status:
            try:
                on_status(text)
            except Exception:
                pass

    original_inputs = {
        f for f in os.listdir(workspace_in)
        if os.path.isfile(os.path.join(workspace_in, f))
    }
    input_bytes = sum(
        os.path.getsize(p) for p in file_paths if os.path.exists(p)
    )
    failure = None
    retry_reason = None

    if trace is not None:
        trace["caption"] = caption
        trace["model"] = model
        trace["recipes"] = [
            name for name, _body in
            select_recipes([os.path.basename(p) for p in file_paths], caption)
        ]
        trace["attempts"] = []

    def record(attempt, commands, ok, failure=None, note=None):
        if trace is None:
            return
        trace["attempts"].append({
            "attempt": attempt, "commands": commands,
            "ok": ok, "failure": failure, "note": note,
        })

    for attempt in range(1, MAX_PLAN_ATTEMPTS + 1):
        if attempt == 1:
            status("⚙️ 正在规划处理步骤...")
        else:
            # 区分重规划的原因：产物偏大不是"报错"，说成报错会误导用户
            if retry_reason == "oversize":
                status(f"📦 上一轮产物偏大，正在按实际体积重新规划 (第 {attempt} 次)...")
            else:
                status(f"🔧 上一轮执行报错，正在重新规划 (第 {attempt} 次)...")

        prompt = build_plan_prompt(file_paths, workspace_out, caption, failure)
        ok, output, err_kind = call_agy(prompt, model, PLAN_TIMEOUT)

        if not ok:
            if err_kind == "auth":
                return False, [], AUTH_HINT
            if err_kind == "timeout":
                return False, [], f"⏰ 规划超时（超过 {PLAN_TIMEOUT} 秒），请简化需求后重试。"
            return False, [], "❌ AGY 规划调用失败，未能返回任何内容（可能是网络或模型侧异常）。"

        rejection = extract_rejection(output)
        if rejection:
            logger.info(f"Planner 判定需求不成立: {rejection}")
            return False, [], (
                "🤔 <b>这个需求恐怕做不到</b>\n"
                "──────────────────────\n"
                f"{esc(rejection)}"
            )

        commands = _extract_commands(output)
        if not commands:
            logger.error(f"无法从规划输出提取命令: {output[-1200:]}")
            record(attempt, [], False, note="规划输出里没有合法的 <json> 命令数组")
            if attempt < MAX_PLAN_ATTEMPTS:
                failure = {
                    "cmd": "(上一轮没有输出合法的 <json> 命令数组)",
                    "stderr": "输出格式不符合要求，请严格只输出 <json>[...]</json>。",
                }
                continue
            return False, [], "❌ AGY 未能输出合法的命令序列，请换个说法描述需求。"

        status(f"🚀 正在执行 {len(commands)} 步处理...")
        exec_ok, failure = execute_commands(commands, workspace_in)
        record(attempt, commands, exec_ok, failure)
        if not exec_ok:
            retry_reason = "error"
            if attempt < MAX_PLAN_ATTEMPTS:
                logger.warning(
                    f"第 {attempt} 轮执行失败，回喂重规划: "
                    f"{(failure or {}).get('stderr', '')[:200]}"
                )

        if exec_ok:
            products = collect_outputs(workspace_in, workspace_out, original_inputs)
            if products:
                oversized = _disproportionate_products(products, input_bytes)
                if oversized and attempt < MAX_PLAN_ATTEMPTS:
                    # 不是失败，只是明显失衡 —— 把实际体积告诉 Planner 让它降参重做
                    logger.info(f"产物与输入失衡，回喂重规划: {oversized}")
                    # 这一轮命令是绿的，产物却被丢弃重做 —— 不记下来，
                    # 归档里就只剩最后一轮，看不出"曾经生成过更大的版本"
                    if trace is not None and trace["attempts"]:
                        trace["attempts"][-1]["note"] = f"产物被判定失衡并丢弃重做：{oversized}"
                    retry_reason = "oversize"
                    for path in products:
                        try:
                            os.remove(path)
                        except OSError:
                            pass
                    failure = {
                        "cmd": commands[-1],
                        "stderr": (
                            f"命令执行成功，但{oversized}，说明输出参数偏大。\n"
                            f"请降低输出尺寸后重做（体积通常与宽度的平方成正比，"
                            f"宽度减半即可降到约四分之一；优先降宽度而非帧率/画质）。\n"
                            f"注意：若该体积是任务本身决定的（如页数很多、时长很长），"
                            f"不要为了压体积而过度降质 —— 保持合理参数原样输出即可。"
                        ),
                    }
                    continue
                return True, products, None
            # 命令全绿但没产物，同样值得回喂重来
            # 有些工具（如 libreoffice 缺少组件时）会**报错却返回 0**，
            # 只看返回码抓不到，必须以"有没有产物"为准
            logger.warning(
                "命令全部返回 0 但输出目录为空，回喂重规划。最后一条命令: "
                f"{commands[-1][:200]}"
            )
            if trace is not None and trace["attempts"]:
                trace["attempts"][-1]["note"] = "命令全部返回 0，但输出目录为空"
            failure = {
                "cmd": commands[-1],
                "stderr": "所有命令返回码为 0，但输出目录中没有任何文件。请检查输出路径是否写对。",
            }
            retry_reason = "error"

        if attempt >= MAX_PLAN_ATTEMPTS:
            break

    detail = (failure or {}).get("stderr", "未知原因")
    bad_cmd = (failure or {}).get("cmd", "")
    return False, [], (
        "❌ <b>处理失败</b>\n"
        "──────────────────────\n"
        f"<b>失败的命令</b>:\n<pre>{_escape(bad_cmd)}</pre>\n"
        f"<b>错误输出</b>:\n<pre>{_escape(detail[-600:])}</pre>"
    )


def _escape(text):
    return (
        str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    )
