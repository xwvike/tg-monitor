# 🧰 AGY 可用工具链清单 (Toolchain Manifest)

> **本文件是 AGY 处理文件任务时的"能力地图"。**
> 接到文件处理请求时，请先阅读此清单确认可用工具，优先使用已注册的能力，避免盲目尝试未安装的工具。

---

## 🎬 音视频处理

### FFmpeg
- **类型**: 系统二进制
- **路径**: `/usr/bin/ffmpeg`
- **能力**:
  - 视频格式互转 (mp4/mkv/avi/webm/mov)
  - 视频转 GIF 动图
  - 视频/音频 抽取分离
  - 音频格式转换与重采样 (mp3/wav/ogg/flac)
  - 图片格式转换
  - 视频裁剪、拼接、加水印
- **调用示例**: `ffmpeg -i INPUT [options] OUTPUT`

---

## 🎙️ 语音处理

### TTS 语音合成引擎
- **类型**: Python 模块
- **路径**: `core/tts.py`
- **入口函数**: `generate_telegram_voice(text, voice=...)`
- **能力**:
  - 中英文文本转语音
  - 两阶段流水线: Edge-TTS API → FFmpeg OGG/Opus 转码
  - 输出 Telegram 原生语音卡片格式
- **依赖**: Edge-TTS API (`openai-edge-tts` 容器 `127.0.0.1:5050`)、FFmpeg

### STT 语音识别引擎
- **类型**: Python 模块
- **路径**: `core/stt.py`
- **入口函数**: `transcribe_voice_file(file_path, model=..., language='zh')`
- **能力**:
  - 语音/音频文件转文字
  - 多语言自动识别
  - 自动 FFmpeg 兜底重采样 (处理非标准采样率)
- **依赖**: Speaches 容器 (`127.0.0.1:8000`, Faster-Whisper)、FFmpeg

---

## 👁️ OCR 文字识别

### WeChat OCR API
- **类型**: Docker 容器
- **容器名**: `wechat-ocr-api`
- **镜像**: `golangboyme/wxocr`
- **端点**: `http://127.0.0.1:5000/ocr`
- **能力**:
  - 图片文字提取 (中英文高精度)
  - 返回文字内容与坐标位置信息
- **调用方式**: POST JSON，图片需 base64 编码
  ```bash
  curl -X POST http://localhost:5000/ocr \
    -H "Content-Type: application/json" \
    -d '{"image": "BASE64_ENCODED_IMAGE_DATA"}'
  ```
- **返回格式**:
  ```json
  {
    "errcode": 0,
    "ocr_response": [
      {"text": "识别出的文字", "left": 80.63, ...}
    ]
  }
  ```
- **适用场景**: 省 token 的轻量 OCR 任务；对于需要语义理解的复杂图文，优先使用 AGY 多模态视觉能力

---

## 🖼️ 图片处理

### ImageMagick
- **类型**: 系统二进制
- **路径**: `/usr/bin/convert` / `/usr/bin/magick`
- **能力**:
  - 图片缩放、裁剪、旋转、翻转
  - 格式转换 (jpg/png/webp/bmp/gif/tiff)
  - 添加文字水印、边框、滤镜
  - 批量图片处理
  - 图片拼接与合成
- **调用示例**: `convert INPUT -resize 50% -quality 75 OUTPUT`

### pngquant
- **类型**: 系统二进制
- **路径**: `/usr/bin/pngquant`
- **能力**:
  - PNG 图片有损压缩 (大幅缩小体积，肉眼几乎无差别)
- **调用示例**: `pngquant --quality=50-75 --force --output OUTPUT INPUT`
- **适用场景**: 专门针对 PNG 格式的极致压缩，比 ImageMagick 效果更好

---

## 📄 文档处理

### Pandoc
- **类型**: 系统二进制
- **路径**: `/usr/bin/pandoc`
- **能力**:
  - 文档格式万能互转 (Markdown ↔ HTML ↔ Word ↔ PDF ↔ EPUB ↔ LaTeX)
  - 电子书格式转换
  - 幻灯片生成
- **调用示例**: `pandoc INPUT.md -o OUTPUT.docx`

### poppler-utils (PDF 工具集)
- **类型**: 系统二进制
- **核心命令**:
  - `pdftotext` — PDF 转纯文本
  - `pdfimages` — 提取 PDF 中的图片
  - `pdfinfo` — 查看 PDF 元信息 (页数、作者等)
  - `pdftoppm` — PDF 页面转图片 (PNG/JPEG)
- **调用示例**: `pdftotext INPUT.pdf OUTPUT.txt`

---

## 📦 其他已知服务 (非文件处理，但 AGY 可调用)

| 服务 | 容器名 | 端口 | 用途 |
|------|--------|------|------|
| Speaches (Whisper) | `speaches` | `localhost:8000` | STT 语音识别后端 |
| OpenAI Edge-TTS | `openai-edge-tts` | `127.0.0.1:5050` | TTS 语音合成后端 |
| qBittorrent | `qbittorrent` | — | 下载管理 |
| MinIO | `minio` | `localhost:9000` | 对象存储 |
| PostgreSQL | `postgres` | `localhost:5432` | 关系型数据库 |
| MySQL | `mysql` | `localhost:3306` | 关系型数据库 |
| Redis | `redis` | `localhost:6379` | 缓存/消息队列 |

---

> **⚠️ 工具链扩展规范**
>
> 本文件由 `load_toolchain()` **直接内联进 Planner 的 prompt** —— 它不是给人看的
> 说明书，而是模型规划命令时依据的"能力地图"。写错一个函数名或声明一个没安装的
> 工具，就会直接误导规划。
>
> 因此新增任何工具或容器时，必须同步：
> 1. 更新本清单
> 2. 在 `install.sh` 的 `TOOLCHAIN` 映射中登记（否则新机器上不会被安装）
>
> `tests/test_toolchain_doc.py` 会校验：本文声明的 Python 入口函数在对应模块中
> 真实存在，且声明的每个二进制都被 `install.sh` 覆盖并在本机可执行。
