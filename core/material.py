"""
Layer 3.4: 素材判定 (core/material.py)

文件处理的参数好不好，取决于**素材是什么**，而这件事代码判断不了。
写死 `scale=720` 是把这个判断固化成一个常数；按 bpp 分三档是固化成三个常数
——同一个错误，后者只是穿了件带数据的外衣（见 GEMINI.md 3.04）。

所以这里只做代码该做的事：**摆证据**。抽几帧、探元数据、算复杂度，
然后让模型看着证据回答"这是什么素材、什么优先"。结论进规划调用的 prompt。

判定与规划必须是**两次**调用，不能合并：多模态模型一旦在规划调用里拿到图片，
就会开始描述画面而不是动手输出命令（实测结论）。
"""

import json
import logging
import os
import re
import shutil
import subprocess
import tempfile

logger = logging.getLogger("Material")

SAMPLE_FRAMES = 3       # 首/中/尾各一帧，足够看出素材类型
FRAME_WIDTH = 640       # 判定只需看清"是什么"，不必原分辨率
CLASSIFY_TIMEOUT = 60


def probe_video_facts(path):
    """把可度量的事实取出来。任何一项探不到都不影响其余项。

    这些都是**证据**，不是结论 —— 不在这里做任何阈值判断。
    """
    facts = {}
    try:
        res = subprocess.run(
            ["ffprobe", "-v", "error", "-print_format", "json",
             "-show_format", "-show_streams", path],
            capture_output=True, text=True, timeout=25,
        )
        if res.returncode != 0:
            return facts
        data = json.loads(res.stdout)
        v = next((s for s in data.get("streams", [])
                  if s.get("codec_type") == "video"), None)
        if not v:
            return facts
        facts["width"] = v.get("width")
        facts["height"] = v.get("height")
        facts["codec"] = v.get("codec_name")
        num, _, den = (v.get("avg_frame_rate") or "0/1").partition("/")
        if float(den or 0):
            facts["fps"] = round(float(num) / float(den), 2)
        dur = data.get("format", {}).get("duration") or v.get("duration")
        if dur:
            facts["duration"] = round(float(dur), 2)
        facts["bytes"] = os.path.getsize(path)
        # 每像素字节数：h264 花了多少码率才编下来，直接反映时空冗余多少。
        # 屏幕录制远低于实拍 —— 交给模型当证据，不在这里划线。
        if facts.get("width") and facts.get("fps") and facts.get("duration"):
            frames = facts["fps"] * facts["duration"]
            if frames > 0:
                facts["bytes_per_pixel"] = round(
                    facts["bytes"] / (facts["width"] * facts["height"] * frames), 5
                )
    except Exception as e:
        logger.warning(f"探测素材事实失败 {os.path.basename(path)}: {e}")
    return facts


def extract_sample_frames(path, outdir, count=SAMPLE_FRAMES):
    """按时间均匀抽帧。返回抽出的图片路径列表（可能为空）。"""
    facts = probe_video_facts(path)
    duration = facts.get("duration") or 0
    if duration <= 0:
        return []

    frames = []
    for i in range(count):
        # 避开首尾各一点，纯黑的片头片尾看不出素材类型
        ts = duration * (i + 0.5) / count
        dst = os.path.join(outdir, f"_frame{i}.jpg")
        try:
            res = subprocess.run(
                ["ffmpeg", "-v", "error", "-ss", f"{ts:.2f}", "-i", path,
                 "-vf", f"scale={FRAME_WIDTH}:-1", "-frames:v", "1", "-y", dst],
                capture_output=True, timeout=60,
            )
            if res.returncode == 0 and os.path.exists(dst) and os.path.getsize(dst) > 0:
                frames.append(dst)
        except Exception as e:
            logger.warning(f"抽帧失败 @{ts:.2f}s: {e}")
    return frames


CLASSIFY_PROMPT = """{marker}
你是素材判定器。看过附带的几帧画面与下面的客观数据后，只回答"这是什么素材、
转换时什么优先"，**不要输出任何命令，也不要描述画面内容**。

客观数据：
{facts}

判断维度（自行取舍，不必套模板）：
- 画面构成：大片纯色与锐利边缘（屏幕录制/界面/演示/动画/矢量图形），
  还是连续渐变与噪点（实拍/摄像/游戏画面）？
- 有没有必须读得清的文字？占多大比例？
- 运动特征：整屏几乎静止、局部小范围变化、还是全屏持续运动？
- 因此转换时应当优先保住什么、可以牺牲什么？

输出格式（不超过 120 字，直接写结论，不要解释推理过程）：
<material>一段话：素材类型 + 文字情况 + 运动特征 + 什么优先什么可牺牲</material>
"""


def classify_material(path, call_model, model, marker=""):
    """判定素材。返回一段自然语言结论；判定不成立时返回空串。

    call_model 由调用方注入（file_pipeline.call_agy），避免本模块反向依赖。
    失败一律返回空串：判定是**增益**不是前提，拿不到结论时规划照常进行，
    只是退回到菜谱的通用建议。
    """
    facts = probe_video_facts(path)
    if not facts:
        return ""

    workdir = tempfile.mkdtemp(prefix="material_")
    try:
        frames = extract_sample_frames(path, workdir)
        facts_text = "\n".join(f"  - {k}: {v}" for k, v in sorted(facts.items()))
        prompt = CLASSIFY_PROMPT.format(marker=marker, facts=facts_text)
        if frames:
            prompt += "\n请读取以下画面帧后作答：\n" + "\n".join(
                f"  - {p}" for p in frames
            )
        else:
            prompt += "\n（本次未能抽出画面帧，请仅依据上面的客观数据作答。）"

        ok, out, _err = call_model(prompt, model, CLASSIFY_TIMEOUT)
        if not ok:
            logger.info("素材判定调用失败，规划将退回菜谱通用建议")
            return ""

        m = re.search(r"<material>(.*?)</material>", out or "", re.DOTALL)
        verdict = (m.group(1) if m else "").strip()
        if not verdict:
            # 没按格式回也不算失败：取最后一段非空文本，够用就行
            tail = [ln.strip() for ln in (out or "").splitlines() if ln.strip()]
            verdict = tail[-1] if tail else ""
        verdict = " ".join(verdict.split())[:300]
        if verdict:
            logger.info(f"素材判定 → {verdict}")
        return verdict
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
