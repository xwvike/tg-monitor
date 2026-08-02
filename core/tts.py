#!/usr/bin/env python3
"""
TG Monitor 独立 TTS 语音流水线与转码引擎 (core/tts.py)
两阶段管道：
1. 阶段 1 [文本转 MP3]: 调用 OpenAI Edge-TTS API (http://127.0.0.1:5050/v1/audio/speech)
2. 阶段 2 [格式与报头转换]: 使用 ffmpeg 转码为 Telegram 原生 Voice Note 格式 (OGG + Opus, 48kHz, 单声道)

特性：
- 两阶段具备严格的成功/失败状态判断。
- 阶段 1 失败：打印警告并降级，向上层返回原文本。
- 阶段 2 失败：打印警告并降级，向上层返回原文本。
- 独立运行：既可被其他 Python 模块 import 调用，亦可在命令行单独执行测试。
"""

import logging
import os
import re
import subprocess
import sys
import time

import requests

logging.basicConfig(
    level=logging.INFO, format="[%(asctime)s] [%(levelname)s] %(message)s"
)
logger = logging.getLogger("TTSEngine")

TTS_API_URL = os.getenv("TTS_API_URL", "http://127.0.0.1:5050/v1/audio/speech")
TTS_API_KEY = os.getenv("TTS_API_KEY", "openclaw-tts")
DEFAULT_VOICE = os.getenv("TTS_DEFAULT_VOICE", "zh-CN-XiaoxiaoNeural")


def clean_text_for_tts(raw_text: str, max_len: int = 1000) -> str:
    """
    思路 1 智能文本清洗器：剥离 Markdown/HTML 标记与代码块，提取纯净朗读文本
    """
    if not raw_text:
        return ""

    # 1. 移除多行代码块 ``` ... ```
    text = re.sub(r"```[\s\S]*?```", "", raw_text)

    # 2. 移除单行代码 `code`
    text = re.sub(r"`[^`]+`", "", text)

    # 3. 移除 HTML 标签
    text = re.sub(r"<[^>]+>", "", text)

    # 4. 移除 Markdown 链接 [text](url) -> text
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)

    # 5. 移除 Markdown 标题/粗体/斜体/引用/表格符号
    text = re.sub(r"[*#_~>|]", "", text)

    # 6. 整理多余空行与空格
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    cleaned = " ".join(lines)

    # 7. 截取适度长度
    if len(cleaned) > max_len:
        cleaned = cleaned[:max_len] + "。"

    return cleaned


def should_auto_speak(raw_text: str) -> tuple[bool, str]:
    """
    思路 1 智能过滤判定：判断回复文本是否适合自动转换为语音
    规则：无代码块、无表格、清洗后有效字数在 15~150 字之间
    返回: (should_speak, cleaned_text)
    """
    if not raw_text or "```" in raw_text or "| --- |" in raw_text or "|---" in raw_text:
        return False, ""

    cleaned = clean_text_for_tts(raw_text, max_len=200)
    if 15 <= len(cleaned) <= 150:
        return True, cleaned
    return False, cleaned


def text_to_mp3(
    text: str, voice: str = DEFAULT_VOICE, output_path: str | None = None
) -> tuple[bool, str]:
    """
    阶段 1：请求 Edge-TTS API 将文本转换为 MP3 文件
    返回: (is_success, output_path_or_error_msg)
    """
    if not text or not text.strip():
        return False, "输入文本为空"

    if not output_path:
        ts = int(time.time() * 1000)
        output_path = f"/tmp/tts_stage1_{ts}.mp3"

    headers = {
        "Authorization": f"Bearer {TTS_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": "tts-1",
        "input": text.strip(),
        "voice": voice,
        "response_format": "mp3",
    }

    try:
        resp = requests.post(TTS_API_URL, json=payload, headers=headers, timeout=60)
        if resp.status_code == 200 and len(resp.content) > 0:
            with open(output_path, "wb") as f:
                f.write(resp.content)
            logger.info(
                f"✅ 阶段 1 [文本转 MP3] 成功: {output_path} (大小: {len(resp.content)} 字节)"
            )
            return True, output_path
        else:
            err_msg = f"TTS API 状态码异常 {resp.status_code}: {resp.text[:100]}"
            logger.warning(f"❌ 阶段 1 [文本转 MP3] 失败: {err_msg}")
            return False, err_msg
    except Exception as e:
        err_msg = f"请求 TTS API 异常: {e}"
        logger.warning(f"❌ 阶段 1 [文本转 MP3] 失败: {err_msg}")
        return False, err_msg


def mp3_to_tg_ogg(
    mp3_path: str, output_ogg_path: str | None = None
) -> tuple[bool, str, int]:
    """
    阶段 2：使用 ffmpeg 将 MP3 转码为 Telegram 原生 Voice Note 格式 (OGG + Opus, 48kHz, 单声道)
    返回: (is_success, output_ogg_path_or_error_msg, duration_seconds)
    """
    if not os.path.exists(mp3_path) or os.path.getsize(mp3_path) == 0:
        return False, f"MP3 输入文件不存在或为空: {mp3_path}", 0

    if not output_ogg_path:
        ts = int(time.time() * 1000)
        output_ogg_path = f"/tmp/tts_stage2_{ts}.ogg"

    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        mp3_path,
        "-c:a",
        "libopus",
        "-b:a",
        "32k",
        "-vbr",
        "on",
        "-ar",
        "48000",
        "-ac",
        "1",
        output_ogg_path,
    ]

    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if (
            res.returncode == 0
            and os.path.exists(output_ogg_path)
            and os.path.getsize(output_ogg_path) > 0
        ):
            duration = get_audio_duration(output_ogg_path)
            logger.info(
                f"✅ 阶段 2 [MP3 转 OGG/Opus] 成功: {output_ogg_path} (时长: {duration}s)"
            )
            return True, output_ogg_path, duration
        else:
            err_msg = res.stderr.strip() or "ffmpeg 转码命令失败"
            logger.warning(f"❌ 阶段 2 [MP3 转 OGG/Opus] 失败: {err_msg[:120]}")
            return False, err_msg, 0
    except Exception as e:
        err_msg = f"调用 ffmpeg 异常: {e}"
        logger.warning(f"❌ 阶段 2 [MP3 转 OGG/Opus] 失败: {err_msg}")
        return False, err_msg, 0


def get_audio_duration(file_path: str) -> int:
    """使用 ffprobe 获取音频时长 (秒)"""
    try:
        cmd = [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            file_path,
        ]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
        if res.returncode == 0 and res.stdout.strip():
            return int(float(res.stdout.strip()))
    except Exception:
        pass
    return 0


def generate_telegram_voice(
    text: str, voice: str = DEFAULT_VOICE
) -> tuple[bool, str, int, str]:
    """
    两阶段完整流水线：文本 ➔ Telegram 原生 Voice Note (.ogg)
    返回元组: (is_success, ogg_path_or_error_msg, duration_seconds, original_text)

    具备友善降级：
    - 任何阶段失败，均返回 is_success=False 并带上原始文本，上层可平滑降级使用纯文本回复。
    - 成功则自动清理阶段 1 生成的中间临时 MP3 文件。
    """
    logger.info(f"🎙️ 启动语音流水线处理 (文本字数: {len(text)})...")

    # 阶段 1: 文本 -> MP3
    ok1, stage1_res = text_to_mp3(text, voice)
    if not ok1:
        logger.warning(
            f"⚠️ [阶段 1 失败] 触发降级机制：将回退使用原始文本。原因: {stage1_res}"
        )
        return False, stage1_res, 0, text

    mp3_file = stage1_res

    # 阶段 2: MP3 -> OGG/Opus
    ok2, ogg_file, duration = mp3_to_tg_ogg(mp3_file)

    # 自动清理阶段 1 的中间 MP3 缓存
    try:
        if os.path.exists(mp3_file):
            os.remove(mp3_file)
    except Exception:
        pass

    if not ok2:
        logger.warning(
            f"⚠️ [阶段 2 失败] 触发降级机制：将回退使用原始文本。原因: {ogg_file}"
        )
        return False, ogg_file, 0, text

    logger.info(f"🎉 语音流水线处理完成！生成目标文件: {ogg_file} (时长: {duration}s)")
    return True, ogg_file, duration, text


if __name__ == "__main__":
    input_text = (
        sys.argv[1]
        if len(sys.argv) > 1
        else "你好！这是 TG Monitor 独立 TTS 两阶段语音流水线的自动化测试。"
    )
    print("=== 🎙️ 运行 TTS 独立流水线测试 ===")
    print(f"输入文本: {input_text}")
    print("------------------------------------------------------------------")

    success, result_path_or_err, dur_sec, raw_txt = generate_telegram_voice(input_text)

    print("------------------------------------------------------------------")
    if success:
        print("✅ 流水线总体状态: 成功 (PASS)")
        print(f"📄 目标语音文件: {result_path_or_err}")
        print(f"⏱️ 语音播放时长: {dur_sec} 秒")
    else:
        print("❌ 流水线总体状态: 失败 (FAILED - 降级回退)")
        print(f"⚠️ 错误原因: {result_path_or_err}")
        print(f"📝 降级使用原文: {raw_txt}")
