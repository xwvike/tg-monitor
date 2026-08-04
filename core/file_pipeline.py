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
CMD_TIMEOUT = 180  # 单条 bash 命令超时（秒）
PLAN_TIMEOUT = 150  # 单次规划调用超时（秒）
ROUTER_TIMEOUT = 20  # 意图兜底判定超时（秒）

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
    "转html", "转epub", "电子书", "分割", "切割", "截取", "降噪",
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


def _agy_env():
    env = os.environ.copy()
    env["HTTP_PROXY"] = "http://127.0.0.1:10809"
    env["HTTPS_PROXY"] = "http://127.0.0.1:10809"
    env["PATH"] = f"{os.path.expanduser('~/.local/bin')}:{env.get('PATH', '')}"
    return env


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


def classify_intent(caption, file_names, model):
    """判断是"物理处理"还是"视觉问答"。返回 (is_processing, how)。

    how ∈ {"empty", "keyword", "model", "fallback"}，仅用于日志排查。
    """
    if not caption or not caption.strip():
        # 无附言一律走问答：非破坏性，用户想处理时补一句话即可。
        # 旧实现里文档无附言会被强行"压缩/规范化"，那是纯粹的误伤。
        return False, "empty"

    low = caption.lower()
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
        kw_hit = any(k in low for k in entry["keywords"])
        ext_hit = bool(exts & entry["exts"])
        if kw_hit and ext_hit:
            score = 3
        elif kw_hit:
            score = 2
        elif ext_hit:
            score = 1
        else:
            continue
        scored.append((score, entry["file"]))

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
        f"{recipe_block}\n\n"
        "## 硬性约束\n"
        f"1. 所有产物必须写入输出目录：{workspace_out}\n"
        "2. 命令中**所有**输入与输出路径必须是绝对路径。手册示例里的 "
        "`image1.jpg`、`$INPUT`、`$OUTPUT_DIR` 等占位写法必须由你替换成上面的真实绝对路径——"
        "执行环境不会为你定义任何变量。\n"
        "3. 需要临时文件时，放在输出目录下并在最后一条命令中删除，不要用 /tmp 里的固定文件名。\n"
        "4. 不要读取、解析或向用户描述文件内容；只做转换。\n"
        "5. 只输出被 <json></json> 包裹的字符串数组，数组外不要有任何解释文字。\n\n"
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


def plan_and_execute(file_paths, workspace_in, workspace_out, caption, model, on_status=None):
    """规划 → 执行 → 失败回喂重规划。返回 (ok, products, error_message)。"""

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
    failure = None

    for attempt in range(1, MAX_PLAN_ATTEMPTS + 1):
        if attempt == 1:
            status("⚙️ 正在规划处理步骤...")
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

        commands = _extract_commands(output)
        if not commands:
            logger.error(f"无法从规划输出提取命令: {output[-1200:]}")
            if attempt < MAX_PLAN_ATTEMPTS:
                failure = {
                    "cmd": "(上一轮没有输出合法的 <json> 命令数组)",
                    "stderr": "输出格式不符合要求，请严格只输出 <json>[...]</json>。",
                }
                continue
            return False, [], "❌ AGY 未能输出合法的命令序列，请换个说法描述需求。"

        status(f"🚀 正在执行 {len(commands)} 步处理...")
        exec_ok, failure = execute_commands(commands, workspace_in)

        if exec_ok:
            products = collect_outputs(workspace_in, workspace_out, original_inputs)
            if products:
                return True, products, None
            # 命令全绿但没产物，同样值得回喂重来
            failure = {
                "cmd": commands[-1],
                "stderr": "所有命令返回码为 0，但输出目录中没有任何文件。请检查输出路径是否写对。",
            }

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
