#!/usr/bin/env python3
"""
素材判定测试 (tests/test_material.py)

按 GEMINI.md 3.01 的判据，这里**只测确定性管道**：
  - 事实提取的算术（bpp = 字节 / 像素数）
  - 抽帧是否真抽出了图
  - 什么任务才值得多花一次判定调用（门控）
  - 判定失败时必须降级而不是中断规划

**不测**判定结论本身说了什么 —— 那是判断力，且措辞每次都不同。
结论好不好靠 workspace/archive/ 里的实际产物验证，不靠断言字符串。
"""

import os
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import file_pipeline as fp
from core import material as mt
from tests.harness import main


def _clip(path, seconds=2, size="320x240", rate=10):
    res = subprocess.run(
        ["ffmpeg", "-v", "error", "-f", "lavfi",
         "-i", f"testsrc2=size={size}:rate={rate}",
         "-t", str(seconds), "-pix_fmt", "yuv420p", "-y", path],
        capture_output=True, timeout=120,
    )
    return res.returncode == 0 and os.path.exists(path)


def test_facts_are_measurable(s):
    """事实提取只做算术，不下任何结论。"""
    if not shutil.which("ffprobe"):
        s.section("素材事实（跳过：未安装 ffprobe）")
        return
    work = tempfile.mkdtemp()
    try:
        clip = os.path.join(work, "c.mp4")
        if not _clip(clip):
            s.section("素材事实（跳过：素材生成失败）")
            return

        s.section("可度量的事实")
        f = mt.probe_video_facts(clip)
        s.check("宽", f.get("width"), 320)
        s.check("高", f.get("height"), 240)
        s.truthy("有时长", f.get("duration", 0) > 0)
        s.truthy("有帧率", f.get("fps", 0) > 0)
        s.check("体积与实际一致", f.get("bytes"), os.path.getsize(clip))

        s.section("bpp 就是字节除以像素数")
        expect = f["bytes"] / (f["width"] * f["height"] * f["fps"] * f["duration"])
        s.truthy("bpp 与手算一致",
                 abs(f["bytes_per_pixel"] - expect) < 1e-4)

        s.section("坏文件不抛异常")
        bad = os.path.join(work, "bad.mp4")
        with open(bad, "wb") as fh:
            fh.write(b"not a video")
        s.check("返回空事实", mt.probe_video_facts(bad), {})
        s.check("抽帧返回空列表", mt.extract_sample_frames(bad, work), [])
    finally:
        shutil.rmtree(work, ignore_errors=True)


def test_frames_are_extracted(s):
    if not shutil.which("ffmpeg"):
        s.section("抽帧（跳过：未安装 ffmpeg）")
        return
    work = tempfile.mkdtemp()
    try:
        clip = os.path.join(work, "c.mp4")
        if not _clip(clip, seconds=3):
            s.section("抽帧（跳过：素材生成失败）")
            return
        s.section("按时间均匀抽帧")
        frames = mt.extract_sample_frames(clip, work)
        s.check("抽出的帧数", len(frames), mt.SAMPLE_FRAMES)
        for p in frames:
            s.truthy(f"{os.path.basename(p)} 非空", os.path.getsize(p) > 0)
    finally:
        shutil.rmtree(work, ignore_errors=True)


def test_gate_avoids_pointless_calls(s):
    """判定要多花一次模型调用，只有参数取决于画面时才值得。"""
    s.section("值得判定")
    s.check("单个视频转 GIF", fp._needs_material_check(
        ["/a/clip.mp4"], ["video_to_gif.md"]), True)
    s.check("单个视频压缩", fp._needs_material_check(
        ["/a/clip.mp4"], ["video_compression.md"]), True)

    s.section("不值得判定")
    # 转写/剪辑的参数与画面长什么样无关
    s.check("语音转写", fp._needs_material_check(
        ["/a/clip.mp4"], ["media_transcribe.md"]), False)
    s.check("视频剪辑", fp._needs_material_check(
        ["/a/clip.mp4"], ["video_trim.md"]), False)
    s.check("图片任务", fp._needs_material_check(
        ["/a/x.jpg"], ["image_compression.md"]), False)
    s.check("无命中菜谱", fp._needs_material_check(["/a/clip.mp4"], []), False)
    # 一次判定代表不了一批异质素材
    s.check("多文件", fp._needs_material_check(
        ["/a/1.mp4", "/a/2.mp4"], ["video_to_gif.md"]), False)


def test_classification_degrades_gracefully(s):
    """判定是增益不是前提：拿不到结论时规划必须照常进行。"""
    if not shutil.which("ffmpeg"):
        s.section("判定降级（跳过：未安装 ffmpeg）")
        return
    work = tempfile.mkdtemp()
    try:
        clip = os.path.join(work, "c.mp4")
        if not _clip(clip):
            s.section("判定降级（跳过：素材生成失败）")
            return

        s.section("模型调用失败 → 空结论，不抛异常")
        s.check("调用失败", mt.classify_material(
            clip, lambda *a, **k: (False, "", "timeout"), "m"), "")
        s.check("返回空文本", mt.classify_material(
            clip, lambda *a, **k: (True, "", None), "m"), "")

        s.section("标签缺失时退回取最后一段文本")
        got = mt.classify_material(
            clip, lambda *a, **k: (True, "前言\n这是一段屏幕录制", None), "m")
        s.check("拿到了结论", got, "这是一段屏幕录制")

        s.section("空结论时 prompt 里不留空段落")
        # 断言段落头而非"素材判定"四个字 —— 后者菜谱正文里也有，
        # 那样测的是菜谱怎么写的，不是代码怎么拼的
        p = fp.build_plan_prompt([clip], "/out", "转gif", material="")
        s.check("无判定段", "## 素材判定" in p, False)
        p2 = fp.build_plan_prompt([clip], "/out", "转gif", material="X 素材")
        s.truthy("有判定段", "## 素材判定" in p2 and "X 素材" in p2)
    finally:
        shutil.rmtree(work, ignore_errors=True)


SUITES = [
    ("素材事实", test_facts_are_measurable),
    ("抽帧", test_frames_are_extracted),
    ("判定门控", test_gate_avoids_pointless_calls),
    ("判定降级", test_classification_degrades_gracefully),
]

if __name__ == "__main__":
    main(SUITES)
