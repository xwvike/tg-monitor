# 📝 文档格式互转 (document_convert.md)

## 触发条件
- **文件类型**: md, markdown, html, htm, docx, odt, epub, rst, txt, tex
- **意图关键词**: 转word, 转docx, 转md, 转markdown, 转html, 转网页, 转epub,
  转电子书, 转txt, 格式转换

## ⚠️ 能力边界（先判断请求是否成立）

Pandoc 做的是**文本结构之间**的转换。以下矩阵之外的请求都不成立：

| 方向 | 是否可行 |
|---|---|
| md ↔ html ↔ docx ↔ odt ↔ epub ↔ rst ↔ latex ↔ txt | ✅ 任意互转 |
| 上述格式 → **PDF** | ❌ 本机无 LaTeX 引擎，`-o x.pdf` 必然失败 |
| **视频 / 音频 / 图片** → 任何文档格式 | ❌ 没有转换关系 |
| PDF → 上述格式 | ⚠️ 用 `pdftotext` 取纯文本，不要用 pandoc |

**请求落在 ❌ 行时不要硬凑命令**，输出 `<reject>` 说明原因并给出可行替代，例如：
- "视频转 Word" → 说明两者无转换关系；若用户想要的是台词，建议改做语音转写
- "转成 PDF" → 说明缺少 PDF 引擎；建议先转 docx 或 html，或改用其它路径
- "扫描件 PDF 转 Word" → 说明该 PDF 是图片、无文字层，建议先做 OCR

## 处理逻辑 (Pandoc)

格式由**输出文件的扩展名**自动推断，通常无需显式指定：

```bash
# 占位符必须替换成任务给定的真实绝对路径
pandoc <输入绝对路径> -o <输出目录>/<原名>.docx
```

### 需要注意的几种情形

```bash
# HTML → Markdown：--wrap=none 避免莫名换行，-t gfm 用通用的 GitHub 风格
pandoc <输入绝对路径> -t gfm --wrap=none -o <输出目录>/<原名>.md

# Markdown → 独立可用的 HTML：--standalone 才会生成完整 <html> 骨架
pandoc <输入绝对路径> --standalone -o <输出目录>/<原名>.html

# → EPUB：加上标题元数据，否则阅读器里显示为无标题
pandoc <输入绝对路径> --metadata title="<原名>" -o <输出目录>/<原名>.epub

# PDF → 纯文本：用 poppler，不要用 pandoc
pdftotext -layout <输入绝对路径> <输出目录>/<原名>.txt
```

## 输出规范
- 产出文件必须保存在任务指定的输出目录中。
- 输出文件名沿用原文件主名，仅替换扩展名。
- 转成 `.txt` / `.md` 时产物较小，流水线会直接把内容作为消息发出。
