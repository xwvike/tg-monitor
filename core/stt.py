#!/usr/bin/env python3
"""
TG Monitor 独立 STT 语音识别与转译引擎 (core/stt.py)
两阶段处理管道：
1. 阶段 1 [直接识别]: 调用 Speaches Faster-Whisper API (http://127.0.0.1:8000/v1/audio/transcriptions)
2. 阶段 2 [格式重采样兜底]: 若阶段 1 遇特殊格式失败，自动调用 ffmpeg 转码为标准 16kHz WAV 二次重试

特性：
- 支持 OGG, MP3, WAV, AAC, M4A 等任意音频格式。
- 具备完整的成功/失败状态校验、日志记录与防崩溃兜底。
- 独立运行：既可被其他 Python 模块 import 调用，亦可在命令行单独执行测试。
"""

import logging
import os
import subprocess
import sys
import time

import requests

logging.basicConfig(level=logging.INFO, format="[%(asctime)s] [%(levelname)s] %(message)s")
logger = logging.getLogger("STTEngine")

STT_API_URL = os.getenv("STT_API_URL", "http://127.0.0.1:8000/v1/audio/transcriptions")
DEFAULT_STT_MODEL = os.getenv("STT_MODEL", "Systran/faster-whisper-small")
LARGE_STT_MODEL = "deepdml/faster-whisper-large-v3-turbo-ct2"


def _request_speaches_api(
    audio_path: str, model: str = DEFAULT_STT_MODEL, language: str = "zh"
) -> tuple[bool, str]:
    """向 Speaches Docker API 发送音频识别请求"""
    if not os.path.exists(audio_path) or os.path.getsize(audio_path) == 0:
        return False, f"音频文件不存在或为空: {audio_path}"

    try:
        with open(audio_path, "rb") as f:
            files = {"file": (os.path.basename(audio_path), f)}
            data = {"model": model, "language": language}
            resp = requests.post(STT_API_URL, files=files, data=data, timeout=30)

        if resp.status_code == 200:
            result = resp.json()
            text = result.get("text", "").strip()
            if text:
                logger.info(f"✅ STT 语音识别成功 ({model}): 「{text}」")
                return True, text
            else:
                return False, "语音识别返回空文本"
        else:
            err_msg = f"Speaches API 状态码异常 {resp.status_code}: {resp.text[:100]}"
            logger.warning(f"⚠️ Speaches API 错误: {err_msg}")
            return False, err_msg
    except Exception as e:
        err_msg = f"请求 Speaches API 抛出异常: {e}"
        logger.warning(f"⚠️ Speaches API 请求失败: {err_msg}")
        return False, err_msg


def normalize_audio_to_wav(
    input_path: str, output_wav_path: str | None = None
) -> tuple[bool, str]:
    """使用 ffmpeg 将任意音频重采样规范化为 16kHz 单声道 WAV 文件"""
    if not output_wav_path:
        ts = int(time.time() * 1000)
        output_wav_path = f"/tmp/stt_norm_{ts}.wav"

    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        input_path,
        "-ar",
        "16000",
        "-ac",
        "1",
        "-c:a",
        "pcm_s16le",
        output_wav_path,
    ]

    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
        if (
            res.returncode == 0
            and os.path.exists(output_wav_path)
            and os.path.getsize(output_wav_path) > 0
        ):
            return True, output_wav_path
        else:
            return False, f"ffmpeg 转码失败: {res.stderr[:100]}"
    except Exception as e:
        return False, f"ffmpeg 执行异常: {e}"


def transcribe_voice_file(
    file_path: str, model: str = DEFAULT_STT_MODEL, language: str = "zh"
) -> tuple[bool, str]:
    """
    两阶段 STT 核心识别函数：
    返回元组: (is_success, transcribed_text_or_error)

    具备自动兜底：
    - 阶段 1：直接用原音频请求 Speaches API。
    - 阶段 2（若阶段 1 失败）：自动调用 ffmpeg 将音频归一化为 16kHz WAV 重试识别。
    """
    if not os.path.exists(file_path):
        return False, f"找不到目标音频文件: {file_path}"

    logger.info(f"🎙️ 启动 STT 语音识别流水线: {file_path}...")

    # 阶段 1: 直接提交 Speaches API
    ok1, res1 = _request_speaches_api(file_path, model=model, language=language)
    if ok1:
        return True, res1

    logger.warning("⚠️ 阶段 1 直传识别未成功，启动【阶段 2】FFmpeg 16kHz 重采样归一化兜底...")

    # 阶段 2: 本地重采样后二次提交
    ok_norm, norm_wav = normalize_audio_to_wav(file_path)
    if not ok_norm:
        return False, f"STT 流水线失败: {res1} (重采样失败: {norm_wav})"

    ok2, res2 = _request_speaches_api(norm_wav, model=model, language=language)

    # 清理归一化产生的临时 WAV 文件
    try:
        if os.path.exists(norm_wav):
            os.remove(norm_wav)
    except Exception:
        pass

    if ok2:
        logger.info(f"🎉 阶段 2 归一化兜底识别成功: 「{res2}」")
        return True, res2
    else:
        logger.error(f"❌ STT 语音识别流水线最终失败: {res2}")
        return False, res2


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("用法: python3 core/stt.py <音频文件路径> [模型名称]")
        print("示例: python3 core/stt.py /tmp/test_voice.ogg")
        sys.exit(1)

    target_file = sys.argv[1]
    target_model = sys.argv[2] if len(sys.argv) > 2 else DEFAULT_STT_MODEL

    print("=== 🎙️ 运行 STT 独立语音识别流水线测试 ===")
    print(f"📄 目标音频: {target_file}")
    print(f"🤖 使用模型: {target_model}")
    print("------------------------------------------------------------------")

    success, text_or_err = transcribe_voice_file(target_file, model=target_model)

    print("------------------------------------------------------------------")
    if success:
        print(f"✅ 识别成功: 「{text_or_err}」")
    else:
        print(f"❌ 识别失败: {text_or_err}")
