# 🧰 这台机器上有什么 (Toolchain Manifest)

> 这份清单会被**整段内联进任务 prompt**。它只回答"有什么"，不回答"该怎么用" ——
> 参数怎么定、素材该怎么处理，由你看清楚素材后自己决定。
> 清单里没有的工具就是没装，不要尝试安装。

## 命令行工具

| 命令 | 用途 |
|---|---|
| `ffmpeg` | 音视频转码、转 GIF、抽音轨、裁剪拼接、加水印 |
| `ffprobe` | 探分辨率/时长/帧率/码率/编码 —— 动手前先看清素材 |
| `convert` / `magick` | ImageMagick：图片转换、缩放、拼接、加字 |
| `identify` | 读图片尺寸、格式、色深 |
| `pngquant` | PNG 有损量化压缩，通常比重编码更划算 |
| `pandoc` | 文档互转（md / html / docx / odt / epub / rst / txt） |
| `soffice` | LibreOffice headless：Office 文档互转与转 PDF |
| `gs` | Ghostscript：PDF 压缩与合并 |
| `pdftotext` | 取 PDF 文本层 |
| `pdftoppm` | PDF 按页转图片 |
| `pdfimages` | 抽出 PDF 内嵌的原始图片 |
| `pdfinfo` | 读 PDF 页数与元信息 |

标准 shell 工具（`rm` `cp` `mv` `mkdir` `unzip` `curl` 等）照常可用。

**两个本机事实，省得你白跑一轮：**
- `pandoc` **出不了 PDF** —— 本机没装 LaTeX 引擎，`pandoc x.md -o x.pdf` 必失败。
  要 PDF 就先转 HTML/docx 再用 `soffice` 转。
- `soffice` **报错时仍返回 0** —— 缺组件或文件损坏时只在 stderr 打印。
  别只看返回码，要确认产物真的生成了。

## 本机服务

| 服务 | 端点 | 用途 |
|---|---|---|
| WeChat OCR | `POST http://127.0.0.1:5000/ocr` | 图片取字（中英文精度高）。JSON 传 base64 图片，返回 `ocr_response[].text`。纯提字比多模态省得多；要理解图文语义还是你自己看 |
| Speaches (Whisper) | `localhost:8000` | 语音转文字后端 |
| OpenAI Edge-TTS | `127.0.0.1:5050` | 文字转语音后端 |

## 项目内的 Python 能力

需要时用 `cd /home/xwvike/tg-monitor && ./venv/bin/python -c "..."` 调用：

- `core/tts.py` → `generate_telegram_voice(text, voice=...)` 文字转 Telegram 语音卡片
- `core/stt.py` → `transcribe_voice_file(file_path, model=..., language='zh')` 语音转文字

---

> **扩展规范**：新增工具或容器时，同步更新本清单与 `install.sh` 的 `TOOLCHAIN`
> 映射（否则新机器上不会被安装）。`tests/test_toolchain_doc.py` 会校验本文声明的
> Python 入口函数真实存在、声明的每个二进制都被 `install.sh` 覆盖且本机可执行。
