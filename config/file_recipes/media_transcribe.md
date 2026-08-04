# 🎙️ 音视频转文字 (media_transcribe.md)

## 触发条件
- **文件类型**: mp4, mov, mkv, webm, avi（视频）/ mp3, m4a, wav, ogg, flac（音频）
- **意图关键词**: 转文字, 转写, 语音识别, 说了什么, 提取对话, 字幕, 听写, 会议记录

## 处理逻辑 (FFmpeg 抽轨 → Speaches/Whisper)

分两步：先用 ffmpeg 把音轨转成 16 kHz 单声道 WAV（Whisper 的标准输入格式），
再 POST 给本机的 Speaches 服务。

```bash
# 占位符必须替换成任务给定的真实绝对路径
# -vn 丢弃视频轨；16kHz 单声道是 Whisper 的期望输入
ffmpeg -v warning -i <输入绝对路径> -vn -ar 16000 -ac 1 -c:a pcm_s16le -y <输出目录>/_audio.wav
curl -s --max-time 600 -X POST http://127.0.0.1:8000/v1/audio/transcriptions -F "file=@<输出目录>/_audio.wav" -F "model=Systran/faster-whisper-base" -F "language=zh" -F "response_format=text" -F "prompt=以下是简体中文的记录。" -o <输出目录>/<原名>_transcript.txt
rm -f <输出目录>/_audio.wav
```

- **`prompt` 参数不能省**：不带它时模型会输出**繁体**且几乎没有标点；
  带上后输出简体并自动断句。非中文内容把 `language` 与 `prompt` 换成对应语言。
- 产物是 `.txt`，流水线会直接把内容作为消息发出（超长才转为附件），
  用户无需下载即可阅读。

### 需要带时间戳的字幕
把 `response_format` 换成 `srt`（或 `vtt`），产物扩展名同步改：

```bash
curl -s --max-time 600 -X POST http://127.0.0.1:8000/v1/audio/transcriptions -F "file=@<输出目录>/_audio.wav" -F "model=Systran/faster-whisper-base" -F "language=zh" -F "response_format=srt" -F "prompt=以下是简体中文的记录。" -o <输出目录>/<原名>.srt
```

### 时长限制
识别耗时约为音频时长的 1/2 ~ 1/10（取决于模型）。单条命令超过 180 秒会被终止，
因此**超过 20 分钟的音视频应先截取**再转写，并在回复中说明只转写了哪一段：

```bash
ffmpeg -v warning -ss 0 -t 1200 -i <输入绝对路径> -vn -ar 16000 -ac 1 -c:a pcm_s16le -y <输出目录>/_audio.wav
```

## 输出规范
- 产出文件必须保存在任务指定的输出目录中。
- 文件名建议格式: `[原文件名]_transcript.txt` 或 `[原文件名].srt`。
- 中间产生的 `_audio.wav` 必须在最后一条命令中删除。
- 输入没有音轨时 ffmpeg 会失败，此时应说明"该文件不含音轨，无法转写"。
