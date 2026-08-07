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
| `anydoc` | **读**文档：docx/doc/xlsx/xls/pptx/ppt/odt/ods/odp/rtf/epub/csv/pdf → Markdown。只出不进，见下方说明 |
| `pandoc` | 文档互转（md / html / docx / odt / epub / rst / txt）。**产出**文档用它 |
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

**要读一个文档的内容，第一选择是 `anydoc`**，别再去 soffice 转一圈或者拆 XML：

```bash
anydoc 报告.docx              # 输出到 stdout
anydoc 课表.xlsx -o 课表.md    # 写文件
anydoc 幻灯片.pptx | head -50  # 先看个开头
```

一条命令吃 14 种格式，统一吐 GitHub 风格 Markdown，标题层级、表格、列表、
脚注都保住。本机实测每份 ~50 毫秒，而且是**结构没丢**——同样一份 pptx，
soffice→PDF→pdftotext 要 1441 毫秒且表格全散成文本。

几个必须知道的边界：
- **只能读，不能写。** 它只有"任意格式 → Markdown"这一个方向。
  要产出 docx/html 用 `pandoc`，要产出 PDF 用 `soffice`。
- **扫描件 PDF 直接报 Unsupported**，它不做 OCR。那条路走 WeChat OCR。
- **PDF 的表格会塌成一行。** 纯文本层的 PDF 想保住表格对齐，`pdftotext -layout`
  反而更好；但它不给标题层级。两个都试一下再选。
- 图片只保留 alt 文本，图里的字要另外走 OCR。

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

**所以超过约 3 分钟的音频，别自己跑 —— 交给系统代跑。**

### 长音频：把活交回给系统

在输出目录里写一个 `stt_request.json` 然后**就此收工**（不要等，不要轮询）：

```json
{
  "file": "备考指导考情分析.mp4",
  "model": "Systran/faster-whisper-small",
  "format": "srt",
  "language": "zh",
  "hotwords": "新途径 教师招聘 考情分析 备考指导 特岗教师"
}
```

- `file`：输入或输出目录里的文件名（视频会自动抽音轨，不用你先转）。
- `format`：`srt` / `vtt` / `text` / `json` / `verbose_json`。
- `hotwords`：空格分隔的领域词表，由你根据文件名、用户原话、素材内容判断该放
  什么。**但它是一笔有代价的交易，得看用户要什么再决定给不给：**

  | 同一段 13.5 分钟讲座、同一个 small 模型 | 条数 | 单条中位时长 | 最大空洞 |
  |---|---|---|---|
  | 不给 hotwords | 365 | 约 2 秒 | 1.0 秒 |
  | 给 hotwords | 64 | 9 秒（最长 30 秒） | 29.8 秒 |

  给了词表，专名和术语明显更准（「教考有接近→捷径」「考卿分析→考情」这类错
  会消失）；但分段会变得又长又粗，还可能整段漏掉。**要拿去当字幕挂在视频上
  就别给**（或者只给极少数关键专名）；要的是可读的文字稿、摘要、会议记录，
  那就给足。拿不准就照用户的用途选，并在回复里说明你为什么这么选。

系统会替你跑完整段（**不切片**：切会切在词中间，还会让专名在段与段之间漂移；
Whisper 内部本来就是带上下文的滑动窗口，长音频它自己处理得了），
然后**主动再叫你一次**，把转写稿和用户最初那句话一起交给你收尾。

第二轮里：
- 要改转写稿的错字，**写一份 `stt_corrections.json` 改正表**
  （`[{"from": "干资", "to": "甘孜"}]`），系统会拿它做逐字替换。
- **不要自己重写那份转写稿。** 实测让模型改写一份 365 条的字幕，交回来只剩
  82 条 —— 内容被丢、剩下的被并成几十秒一条，而它还声称"时间轴完全不变"。
  系统会核对时间轴，动过就会当着用户的面报出来。
- 用户要的如果不止是字幕（摘要、提纲、纪要），照做，那些是新文件，随你怎么写。
- **不要**再写 `stt_request.json`，那一轮就是收尾。

短音频（几分钟以内）不需要这套，直接一条 `curl` 出 `srt` 就行。

## 项目内的 Python 能力

需要时用 `cd /home/xwvike/tg-monitor && ./venv/bin/python -c "..."` 调用：

- `core/tts.py` → `generate_telegram_voice(text, voice=...)` 文字转 Telegram 语音卡片
- `core/stt.py` → `transcribe_voice_file(file_path, model=..., language='zh')`
  语音转文字。**只适合几十秒的语音消息**：内部 HTTP 超时写死 30 秒、ffmpeg 转码
  超时 20 秒，且只返回纯文本没有时间戳。字幕任务直接调上面的 HTTP 接口；
  长音频走 `stt_request.json` 那套握手。

---

> **扩展规范**：新增工具或容器时，同步更新本清单与 `install.sh` 的 `TOOLCHAIN`
> 映射（否则新机器上不会被安装）。`tests/test_toolchain_doc.py` 会校验本文声明的
> Python 入口函数真实存在、声明的每个二进制都被 `install.sh` 覆盖且本机可执行。
