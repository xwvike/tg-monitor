# 🧰 这台机器上有什么 (Toolchain Manifest)

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
| `unar` | 解压：zip / rar / 7z / cab / lzh 通吃，且会自动识别文件名编码 |
| `lsar` | 只列压缩包内容不解压 —— 动手前先看清里面有什么 |
| `zip` | 打 zip 包（手机和 Windows 都能直接打开） |
| `unzip` | 解 zip。**文件名编码认死 UTF-8**，中文包见下方说明 |
| `zstd` | zstd 压缩/解压 |

标准 shell 工具（`rm` `cp` `mv` `mkdir` `tar` `gzip` `bzip2` `xz` `curl` 等）照常可用。

**几个本机事实，省得你白跑一轮：**
- `pandoc` **出不了 PDF** —— 本机没装 LaTeX 引擎，`pandoc x.md -o x.pdf` 必失败。
  要 PDF 就先转 HTML/docx 再用 `soffice` 转。
- `soffice` **报错时仍返回 0** —— 缺组件或文件损坏时只在 stderr 打印。
  别只看返回码，要确认产物真的生成了。
- 解压中文压缩包**一律用 `unar`，别用 `unzip`**。本机的 `unzip` 6.00 没有编码补丁
  （`-O CP936` 直接报用法错误），Windows 来源的 GBK 文件名会解成乱码。
  `unar` 自己会猜编码，猜不准可以 `unar -e GBK`。
- 解压产物直接写进输出目录即可，**可以带子目录**，系统会处理投递形态。

## 本机服务

| 服务 | 端点 | 用途 |
|---|---|---|
| WeChat OCR | `POST http://127.0.0.1:5000/ocr` | 图片取字（中英文精度高）。JSON 传 base64 图片，返回 `ocr_response[].text`。纯提字比多模态省得多；要理解图文语义还是你自己看 |
| Speaches (Whisper) | `POST http://127.0.0.1:8000/v1/audio/transcriptions` | 语音转文字，OpenAI 兼容接口。详见下方 |
| OpenAI Edge-TTS | `127.0.0.1:5050` | 文字转语音后端 |

### 语音转文字：动手前必读

**要字幕就直接让它出字幕**，别自己拼时间轴：

```bash
curl -s -X POST http://127.0.0.1:8000/v1/audio/transcriptions \
  -F "file=@a.wav" -F "model=Systran/faster-whisper-small" \
  -F "language=zh" -F "response_format=srt" -o a.srt
```

`response_format` 可取 `srt` / `vtt` / `verbose_json`（带 segment 级时间戳）/
`json` / `text`。时间轴是接口自带的。

**这台机器是 Intel N100 四核、纯 CPU 跑推理，很慢。** 实测每 60 秒音频（模型已加载）：

| 模型 | 60s 音频耗时 | 相对实时 | 识别质量 |
|---|---|---|---|
| `Systran/faster-whisper-base` | 11 秒 | 5.5× | 差，中文人名地名基本靠猜 |
| `Systran/faster-whisper-small` | 30 秒 | 2× | 可用 |
| `deepdml/faster-whisper-large-v3-turbo-ct2` | 50 秒 | **1×，即多长音频就跑多久** | 好 |

**冷启动首次加载某个模型另加 3~4 分钟。**

由此推出一条硬约束：

> **你自己会 `timeout waiting for response`，而且没有固定预算。**
> 同一个 13 分钟的视频实测死过两次：一次撑到 709 秒，一次只撑到 321 秒。
> 触发条件是**某条命令阻塞太久**，不是总时长到点。冷启动加载模型的那一条
> `curl` 就足以单独把一轮拖死。

因此：**这台机器上，超过约 3 分钟的音频转写，一次性做不完。别硬上。**

正确的做法是先量、再报、然后问：

1. `ffprobe` 读出总时长。
2. 按上表估算：`small` 约等于音频时长的一半，`large-v3-turbo` 约等于音频时长本身，
   首次加载某模型再加 3~4 分钟。
3. **把这个账算给用户看**，然后给出可选项 —— 只转前几分钟、换更快的模型、
   或者他把音频切短了分几次发。

短音频（几分钟以内）直接一条 `curl` 出 `srt` 就行，不需要这套。

## 项目内的 Python 能力

需要时用 `cd /home/xwvike/tg-monitor && ./venv/bin/python -c "..."` 调用：

- `core/tts.py` → `generate_telegram_voice(text, voice=...)` 文字转 Telegram 语音卡片
- `core/stt.py` → `transcribe_voice_file(file_path, model=..., language='zh')`
  语音转文字。**只适合几十秒的语音消息**：内部 HTTP 超时写死 30 秒、ffmpeg 转码
  超时 20 秒，且只返回纯文本没有时间戳。长音频和字幕任务一律直接调上面的 HTTP 接口。

---

> **扩展规范**：新增工具或容器时，同步更新本清单与 `install.sh` 的 `TOOLCHAIN`
> 映射（否则新机器上不会被安装）。`tests/test_toolchain_doc.py` 会校验本文声明的
> Python 入口函数真实存在、声明的每个二进制都被 `install.sh` 覆盖且本机可执行。
