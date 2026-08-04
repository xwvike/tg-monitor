# 📄 PDF 压缩 (pdf_compression.md)

## 触发条件
- **文件类型**: pdf
- **意图关键词**: 压缩, 减小, 小一点, 瘦身, 太大了, compress, 邮件附件

## 处理逻辑 (Ghostscript)

用 `gs` 重新采样内嵌图像。它**保留文字层**，压缩后仍可搜索、可复制 ——
先转图片再合成 PDF 会丢掉文字层，属于错误做法。

### 选择预设

`-dPDFSETTINGS` 只在源图分辨率**高于**目标时才会下采样；源本身已经很低时，
换预设不会有额外收益。

| 预设 | 目标分辨率 | 适用 |
|---|---|---|
| `/printer` | 300 dpi | 需要打印 |
| `/ebook` | 150 dpi | **默认**，屏幕阅读足够清晰 |
| `/screen` | 72 dpi | 用户强调"越小越好"、仅供预览 |

实测 6 页扫描件 1.3 MB：`/ebook` → 639 KB（50%），`/screen` → 63 KB（4%）。

### 命令

```bash
# 占位符必须替换成任务给定的真实绝对路径
gs -q -dNOPAUSE -dBATCH -dSAFER -sDEVICE=pdfwrite -dPDFSETTINGS=/ebook -dCompatibilityLevel=1.4 -sOutputFile=<输出目录>/<原名>_compressed.pdf <输入绝对路径>
```

产物超过 10 MB 时流水线会带着实际体积要求重做，此时换用 `/screen`。

### 纯文字 PDF
文字型 PDF 本身就很小，`gs` 几乎压不动。若压缩后体积没有明显下降，
应如实告知用户"该 PDF 以文字为主，已无明显压缩空间"，不要反复尝试更激进的参数。

## 输出规范
- 产出文件必须保存在任务指定的输出目录中。
- 输出文件名建议格式: `[原文件名]_compressed.pdf`
- 压缩后应确认产物页数与原文件一致（`pdfinfo`）。
