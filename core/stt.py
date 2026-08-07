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

logging.basicConfig(
    level=logging.INFO, format="[%(asctime)s] [%(levelname)s] %(message)s"
)
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

    logger.warning(
        "⚠️ 阶段 1 直传识别未成功，启动【阶段 2】FFmpeg 16kHz 重采样归一化兜底..."
    )

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


# ------------------------------------------------------------------------------
# 长音频转写
# ------------------------------------------------------------------------------

# 转写速率实测（Intel N100 四核纯 CPU，模型已加载）：small 约为音频时长的一半，
# large-v3-turbo 约等于音频时长本身，冷启动首次加载再加 3~4 分钟。811 秒的讲座
# 实测 520 秒。给足余量，超时按音频时长动态算而不是拍一个常数。
LONG_STT_MIN_TIMEOUT = 600
LONG_STT_TIMEOUT_RATIO = 4.0

SUBTITLE_FORMATS = ("srt", "vtt")
TRANSCRIPT_FORMATS = SUBTITLE_FORMATS + ("text", "json", "verbose_json")


def audio_duration(file_path: str) -> float:
    """读音频/视频时长（秒）。读不到返回 0。"""
    try:
        res = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", file_path],
            capture_output=True, text=True, timeout=30,
        )
        return float((res.stdout or "0").strip() or 0)
    except Exception:
        return 0.0


def extract_audio(video_path: str, out_path: str) -> tuple[bool, str]:
    """从视频抽音轨并归一化成 16kHz 单声道 WAV。

    这条命令里没有任何需要判断的东西 —— Whisper 只吃 16k 单声道，
    参数是常量，不该拿去问模型。
    """
    cmd = ["ffmpeg", "-y", "-v", "error", "-i", video_path,
           "-vn", "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", out_path]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
    except Exception as e:
        return False, f"抽音轨异常: {e}"
    if res.returncode != 0 or not os.path.exists(out_path) or os.path.getsize(out_path) == 0:
        return False, f"抽音轨失败: {(res.stderr or '')[:300]}"
    return True, out_path


def transcribe_long_audio(
    file_path: str,
    model: str = DEFAULT_STT_MODEL,
    language: str = "zh",
    response_format: str = "srt",
    hotwords: str = "",
    on_progress=None,
) -> tuple[bool, str]:
    """一次请求转写整段长音频，返回 (是否成功, 字幕文本或错误说明)。

    **不切片。** 切片曾经是绕开 agy 阻塞超时的手段，但它对质量是净损失：
    定长切会切在词中间，两边都会被幻觉补全；更糟的是 Whisper 解码时靠前文
    维持专名一致，切开等于每段从零开始，同一个词在段与段之间来回漂移。
    Whisper 内部本来就是带上下文的 30 秒滑动窗口，长音频它自己处理得了。
    实测 811 秒讲座一次请求跑完：365 条字幕、最大空洞 1.0 秒、专名全程稳定。

    `hotwords` 是唯一值得给的质量杠杆：同一段音频同一个模型，加上领域词之后
    「教考有接近→捷径」「考卿分析→考情」「背考制导→备考指导」三处同音字错
    全部消失。代价是解码变慢近一倍。
    （`prompt` 参数刻意不暴露：它会连输出格式一起带偏 —— 实测标点变成空格、
    阿拉伯数字变成汉字数字。）
    """
    if not os.path.exists(file_path):
        return False, f"找不到目标音频文件: {file_path}"
    if response_format not in TRANSCRIPT_FORMATS:
        return False, (f"不支持的输出格式 {response_format!r}，"
                       f"可选：{'/'.join(TRANSCRIPT_FORMATS)}")

    duration = audio_duration(file_path)
    timeout = max(LONG_STT_MIN_TIMEOUT, duration * LONG_STT_TIMEOUT_RATIO)
    logger.info(
        f"🎙️ 长音频转写: {os.path.basename(file_path)} "
        f"({duration:.0f}s) model={model} format={response_format} "
        f"hotwords={hotwords!r} timeout={timeout:.0f}s"
    )
    if on_progress:
        try:
            on_progress(duration, timeout)
        except Exception:
            pass

    data = {"model": model, "language": language, "response_format": response_format}
    if hotwords:
        data["hotwords"] = hotwords

    try:
        with open(file_path, "rb") as fh:
            resp = requests.post(
                STT_API_URL, files={"file": (os.path.basename(file_path), fh)},
                data=data, timeout=timeout,
            )
    except requests.Timeout:
        return False, (
            f"转写超过 {timeout / 60:.0f} 分钟仍未返回。"
            f"音频长 {duration / 60:.1f} 分钟，可换更快的模型或只转其中一段。"
        )
    except Exception as e:
        return False, f"请求转写服务失败: {e}"

    if resp.status_code != 200:
        return False, f"转写服务返回 {resp.status_code}: {resp.text[:300]}"

    body = resp.text.strip()
    if not body:
        return False, "转写服务返回了空内容"
    logger.info(f"✅ 长音频转写完成，{len(body)} 字符")
    return True, body


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
