"""
Layer 3.5: 文件任务转发 (core/file_pipeline.py)

**职责只有一件事：把用户发来的东西原样交给 agy，再把 agy 干出来的东西发回去。**

这里曾经是一整套"流水线"：关键词意图分流、11 份菜谱按扩展名命中后内联、
素材判定前置调用、Planner 输出命令数组、Python 执行并把 stderr 回喂重规划、
产物失衡再降参重做。全部删掉了，原因是它们都在替 agy 做判断，而每一处
预判都要靠猜用户会怎么说话、素材会是什么样 —— 猜错就是硬伤，猜对也只是
把 agy 本来就会做的事又做了一遍。

现在的形状：

    落盘文件 → 组 prompt（工具说明 + 用户原话 + in/out 路径）
             → agy（一次性调用，天然是新会话，自己跑命令、自己看报错、自己重试）
             → 扫 out/ 目录 → 回传产物 + agy 的文字回复

Python 仍然负责的是**确定性的、agy 够不着的**部分：文件名收敛（路径会进 shell）、
产物回收与打包、Telegram 投递保真、任务留痕。
"""

import json
import logging
import os
import re
import shutil
import subprocess
import zipfile

logger = logging.getLogger("FilePipeline")

AGY_BIN = os.path.expanduser("~/.local/bin/agy")
PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOLCHAIN_FILE = os.path.join(PROJECT_DIR, "config", "TOOLCHAIN.md")

# agy 没有提供 brain 目录隔离参数，内部调用会和用户的真实会话混在同一个 BRAIN_DIR 里。
# 所有内部 prompt 统一打上此标记，供 get_brain_conversations() 过滤，
# 否则文件任务的一次性会话会污染 /history，甚至在 conv_id 为空时被误绑为主会话。
INTERNAL_MARKER = "[[TG-MONITOR-INTERNAL]]"

TASK_TIMEOUT = 900  # agy 要自己跑完整个任务（含转码），给足时间
# Telegram Bot API 的单文件上传上限
TG_UPLOAD_LIMIT_BYTES = 50 * 1024 * 1024
# 产物数量上限。超过则打包成单个 zip 再投递 —— "PDF 按页转图片"这类任务
# 动辄产出几十个文件，逐个发送会刷屏并触碰 Telegram 频率限制。
MAX_INLINE_PRODUCTS = 8

# 文本类产物：转写稿、提取的文字等，用户是要"读"而不是"下载"
TEXT_EXTS = {".txt", ".srt", ".vtt", ".md", ".csv", ".json"}
# 超过这个长度就仍按文件发送，避免刷屏
INLINE_TEXT_MAX_CHARS = 3000

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tiff", ".tif", ".gif"}
VIDEO_EXTS = {".mp4", ".mov", ".avi", ".webm", ".mkv", ".flv", ".m4v"}
AUDIO_EXTS = {".mp3", ".m4a", ".ogg", ".oga", ".wav", ".flac", ".aac", ".opus"}


_UNSAFE_NAME_CHARS = re.compile(r"[^\w.\-]", re.UNICODE)


def safe_filename(name, fallback="file"):
    """把外部提供的文件名收敛成 shell 安全的形式。

    agy 会把输入文件的**绝对路径原样写进它自己执行的命令**。文件名中的
    `;` `$()` 反引号 `|` `&` 会被 shell 解释 —— 用户只要转发一个来自频道的
    恶意命名文件即可触发任意命令执行。os.path.basename() 只挡路径穿越，
    不挡元字符。这道收敛不能因为"执行方换成了 agy"就省掉，反而更重要。

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
    """构造调用 agy 及其子命令时的环境变量。

    代理地址取自 .env 的 TG_PROXY（由 bot.py 的 load_dotenv 注入），
    不再硬编码 —— 换台机器或改端口时只需改一处配置。
    """
    env = os.environ.copy()
    proxy = os.getenv("TG_PROXY", "").strip()
    if proxy:
        env["HTTP_PROXY"] = proxy
        env["HTTPS_PROXY"] = proxy
        env["http_proxy"] = proxy
        env["https_proxy"] = proxy
    env.setdefault("PATH", "/usr/local/bin:/usr/bin:/bin")
    env["PATH"] = os.path.expanduser("~/.local/bin") + ":" + env["PATH"]
    return env


_AUTH_HINTS = (
    "authentication", "unauthorized", "not logged in", "login required",
    "请先登录", "未登录", "invalid api key", "credential",
)

AUTH_HINT = (
    "🔑 <b>AGY 未登录或凭证已失效</b>\n"
    "──────────────────────\n"
    "请在服务器终端执行 <code>agy</code> 完成登录后重试。"
)


def _is_auth_failure(text):
    low = (text or "").lower()
    return any(h in low for h in _AUTH_HINTS)


def load_toolchain():
    """载入工具链清单。

    这是**唯一**还会被内联进 prompt 的项目文档 —— 菜谱已经全部删除。
    它只回答"这台机器上有什么"，不回答"该怎么用" —— 后者是 agy 的事。
    """
    try:
        with open(TOOLCHAIN_FILE, "r", encoding="utf-8") as f:
            return f.read().strip()
    except Exception as e:
        logger.warning(f"读取 TOOLCHAIN.md 失败: {e}")
        return "（工具链清单不可用，请自行确认可用工具后再动手。）"


# ------------------------------------------------------------------------------
# 转发
# ------------------------------------------------------------------------------

READ_ONLY_NOTICE = (
    "\n用户**没有给出任何指令**，只发来了文件。因此本次只解读内容并回答，"
    "不要生成、转换或修改任何文件。若你判断用户可能想做某种处理，"
    "在回复里问一句，让他补一句话再发一次。\n"
)


def build_task_prompt(file_paths, workspace_out, message):
    """把工具说明 + 用户原话 + 路径拼成一次性任务 prompt。

    用户那句话是**原样**转进去的：不做关键词分流、不改写、不加解读。
    之前那套"物理处理 vs 视觉问答"的关键词表已经删了 —— 是回答还是动手，
    agy 看着文件和这句话自己判断，那本来就是它比谁都清楚的事。
    """
    paths = "\n".join(f"  - {p}" for p in file_paths)
    words = (message or "").strip()

    return (
        f"{INTERNAL_MARKER}\n"
        "你是这台 Linux 服务器上的文件处理助手。用户通过 Telegram 发来了文件，"
        "下面是文件路径与他说的话。你有完整的 shell，请**自己动手完成**：\n\n"
        "## 用户发来的文件\n"
        f"{paths}\n\n"
        "## 用户说的话\n"
        f"{words if words else '（没有附任何文字）'}\n"
        f"{'' if words else READ_ONLY_NOTICE}\n"
        "## 这台机器上有什么\n"
        f"{load_toolchain()}\n\n"
        "## 规则\n"
        f"1. **要回传给用户的产物，一律写进这个目录**：{workspace_out}\n"
        "   写在别处用户永远拿不到 —— 这个目录之外的路径对他毫无意义，不要在回复里报。\n"
        f"   目录里可以有子目录（解压之类）；产物少时系统会摊平逐个发，"
        f"超过 {MAX_INLINE_PRODUCTS} 个会连目录结构一起打包成 zip 再发。\n"
        "2. 输入文件保持原样不动，需要中间文件就放输出目录并在结束前删掉。\n"
        "3. 不要安装软件包，也不要改动系统配置。缺工具就用现有的完成，"
        "或直接说明做不到以及可行的替代做法。\n"
        "4. 参数自己定：先看清素材是什么（该 ffprobe 就 ffprobe，该抽帧看一眼就抽），"
        "再决定怎么做最合适。没有一套参数对所有素材都对。\n"
        "5. 若需求本身不成立（比如把视频转成 Word），不要硬凑，直接说明原因并给替代方案。\n"
        "6. 回复用中文，简短说明你做了什么、为什么这么选参数。产物本身会由系统自动发给用户，"
        "**不要**在回复里罗列文件路径。\n"
    )


def run_task(file_paths, workspace_in, workspace_out, message, model,
             on_status=None, trace=None):
    """把任务整个交给 agy 跑完。返回 (ok, products, reply, error)。

    与旧实现的根本区别：这里不解析命令、不执行命令、不回喂重规划。
    agy 自己看得见报错，自己会重试 —— 那是它的强项，而把 stderr 抠出来
    再拼一段"请修正"喂回去，只是在模仿它本来就有的能力。
    """

    def status(text):
        if on_status:
            try:
                on_status(text)
            except Exception:
                pass

    # 进来时快照一次输入清单：agy 跑完后据此认出"落错目录的产物"
    original_inputs = tuple(_files_in(workspace_in))

    prompt = build_task_prompt(file_paths, workspace_out, message)
    if trace is not None:
        trace["message"] = message
        trace["model"] = model
        trace["prompt_chars"] = len(prompt)

    status("🤖 正在处理...")
    cmd = [AGY_BIN, "--dangerously-skip-permissions"]
    if model:
        cmd.extend(["--model", model])
    cmd.extend(["-p", prompt])

    try:
        res = subprocess.run(
            cmd, capture_output=True, text=True, timeout=TASK_TIMEOUT,
            env=agy_env(), cwd=workspace_in,
        )
    except subprocess.TimeoutExpired:
        if trace is not None:
            trace["agy_error"] = "timeout"
        return False, [], "", f"⏰ 任务超过 {TASK_TIMEOUT // 60} 分钟仍未完成，已中止。"
    except Exception as e:
        logger.error(f"调用 agy 异常: {e}")
        if trace is not None:
            trace["agy_error"] = str(e)
        return False, [], "", f"❌ 无法启动 AGY: {e}"

    combined = (res.stdout or "") + (res.stderr or "")
    if _is_auth_failure(combined):
        if trace is not None:
            trace["agy_error"] = "auth"
        return False, [], "", AUTH_HINT

    reply = (res.stdout or "").strip()
    if trace is not None:
        trace["returncode"] = res.returncode
        trace["reply"] = reply[:4000]

    products = collect_outputs(workspace_in, workspace_out, original_inputs)
    if trace is not None:
        trace["product_names"] = [os.path.relpath(p, workspace_out) for p in products]

    if res.returncode != 0 and not products:
        logger.error(f"agy 退出码 {res.returncode}: {combined[-800:]}")
        return False, [], reply, (
            "❌ AGY 未能完成本次任务。\n"
            + (f"它的说明：\n{reply[-1500:]}" if reply else "（没有返回任何说明）")
        )

    return True, products, reply, None


def _files_in(d):
    """递归列出目录下的全部普通文件，返回相对 d 的路径。

    必须递归：解压类任务的产物天然带目录结构，只看顶层的话
    `out/报告/正文.docx` 是看不见的，一次成功的解压会被判成"什么都没产出"。

    符号链接一律跳过。压缩包里可以塞一条指向 /etc/shadow 的软链，
    解压后它就躺在输出目录里，照单全收等于把宿主机文件投递给用户。
    """
    found = []
    try:
        for root, dirs, files in os.walk(d):
            dirs[:] = [x for x in dirs if not os.path.islink(os.path.join(root, x))]
            for f in files:
                full = os.path.join(root, f)
                if os.path.islink(full):
                    logger.warning(f"跳过符号链接产物: {os.path.relpath(full, d)}")
                    continue
                if os.path.isfile(full):
                    found.append(os.path.relpath(full, d))
    except OSError:
        return []
    return sorted(found)


def _prune_empty_dirs(root):
    """自底向上清掉空目录。摊平/打包之后留下的空壳没有意义。"""
    for cur, dirs, files in os.walk(root, topdown=False):
        if cur == root:
            continue
        try:
            os.rmdir(cur)
        except OSError:
            pass


def collect_outputs(workspace_in, workspace_out, original_inputs=()):
    """回收产物。若输出目录为空，兜底把输入目录里新增的文件搬过去。

    agy 偶尔会把产物写在 cwd（即输入目录）。没有这道兜底的话，
    一次成功的处理会被判成"什么都没产出"。
    """
    products = _files_in(workspace_out)
    if products:
        return [os.path.join(workspace_out, f) for f in products]

    # 输入目录里比原始输入多出来的文件，就是落错地方的产物
    for rel in _files_in(workspace_in):
        if rel in original_inputs:
            continue
        dst = os.path.join(workspace_out, rel)
        try:
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.move(os.path.join(workspace_in, rel), dst)
            logger.info(f"兜底回收落在输入目录的产物: {rel}")
        except Exception as e:
            logger.warning(f"回收产物 {rel} 失败: {e}")
    _prune_empty_dirs(workspace_in)

    return [os.path.join(workspace_out, f) for f in _files_in(workspace_out)]


def _flatten_products(products, workspace_out):
    """把嵌套产物就地摊平成顶层文件，返回新路径列表。

    Telegram 投递没有"目录"这个概念，`_send_product` 认的是单个文件。
    嵌套的 `报告/图/p1.png` 摊成 `报告_图_p1.png`：既保住了它原本在哪，
    也避免两个不同子目录下的同名文件互相覆盖。
    """
    rels = {p: os.path.relpath(p, workspace_out) for p in products}
    # 顶层文件先占名，嵌套的再让位 —— 否则用户原本就看得懂的名字会被挤走
    taken = {rel for rel in rels.values() if os.sep not in rel}
    result = [p for p in sorted(products) if os.sep not in rels[p]]

    for path in sorted(products):
        rel = rels[path]
        if os.sep not in rel:
            continue
        name = safe_filename(rel.replace(os.sep, "_"), "output")
        stem, ext = os.path.splitext(name)
        n = 1
        while name in taken:
            n += 1
            name = f"{stem}_{n}{ext}"
        taken.add(name)
        dest = os.path.join(workspace_out, name)
        try:
            shutil.move(path, dest)
            result.append(dest)
        except Exception as e:
            logger.warning(f"摊平产物 {rel} 失败: {e}")
            result.append(path)

    _prune_empty_dirs(workspace_out)
    return sorted(result)


def package_products(products, workspace_out, archive_stem="output"):
    """决定产物怎么投递。返回 (最终投递列表, 是否已打包)。

    - 超过 MAX_INLINE_PRODUCTS：打成单个 zip，**包内保留原有目录结构**。
      到了这个量级，目录结构本身就是内容的一部分，摊平反而是破坏。
    - 未超过：摊平成顶层文件逐个投递。解压出三个文件就该收到三个文件，
      而不是收到一个重新压好的包。
    """
    if len(products) <= MAX_INLINE_PRODUCTS:
        return _flatten_products(products, workspace_out), False

    archive = os.path.join(workspace_out, f"{safe_filename(archive_stem, 'output')}.zip")
    try:
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
            for path in sorted(products):
                zf.write(path, os.path.relpath(path, workspace_out))
    except Exception as e:
        logger.error(f"打包产物失败，改为逐个投递: {e}")
        return _flatten_products(products, workspace_out), False

    for path in products:
        try:
            os.remove(path)
        except OSError:
            pass
    _prune_empty_dirs(workspace_out)
    logger.info(f"产物 {len(products)} 个超过 {MAX_INLINE_PRODUCTS}，已打包")
    return [archive], True


def probe_file(path):
    """一行人类可读的文件元数据。仅用于日志与留痕，不再进 prompt ——
    agy 自己有 ffprobe，替它预消化只会多一层可能出错的转述。"""
    name = os.path.basename(path)
    try:
        size = os.path.getsize(path)
    except OSError:
        return f"{name} (无法读取大小)"
    parts = [_human_size(size)]
    ext = os.path.splitext(name)[1].lower()
    if ext in VIDEO_EXTS or ext in AUDIO_EXTS:
        try:
            res = subprocess.run(
                ["ffprobe", "-v", "error", "-print_format", "json",
                 "-show_format", "-show_streams", path],
                capture_output=True, text=True, timeout=25,
            )
            if res.returncode == 0:
                data = json.loads(res.stdout)
                dur = data.get("format", {}).get("duration")
                if dur:
                    parts.append(f"{float(dur):.1f}s")
                v = next((s for s in data.get("streams", [])
                          if s.get("codec_type") == "video"), None)
                if v and v.get("width"):
                    parts.append(f"{v['width']}x{v['height']}")
        except Exception as e:
            logger.debug(f"元数据探针跳过 {name}: {e}")
    return f"{name} ({', '.join(parts)})"


def _human_size(num):
    for unit in ("B", "KB", "MB", "GB"):
        if num < 1024 or unit == "GB":
            return f"{num:.1f}{unit}" if unit != "B" else f"{int(num)}B"
        num /= 1024
    return f"{num:.1f}GB"
