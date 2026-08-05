# 📊 Office 文档转换 (office_convert.md)

## 触发条件
- **文件类型**: xls, xlsx, doc, docx, ppt, pptx, ods, odt, odp, rtf
- **意图关键词**: 转pdf, 转成pdf, 导出pdf, 打印成pdf, 转excel, 转ppt,
  转表格, 转幻灯片, 另存为

## ⚠️ 与 document_convert.md 的分工

| 场景 | 用哪个 |
|---|---|
| Office 二进制/OOXML（xls/doc/ppt 系）→ 任何格式 | **本手册，soffice** |
| 任何 Office 文档 → **PDF** | **本手册**（pandoc 无 PDF 引擎，必然失败） |
| md / html / epub / rst / txt 之间互转 | document_convert.md，pandoc |
| 视频 / 音频 / 图片 → 文档 | ❌ 不成立，输出 `<reject>` 说明原因 |

## ⚠️ LibreOffice 失败时退出码仍是 0

源文件解析不了时它只在 stderr 打印 `Error: source file could not be loaded`，
**退出码照样是 0**。因此不能靠退出码判断成败，唯一可信的判据是产物是否存在。
（流水线已有兜底：命令全返回 0 但输出目录为空时会判定失败并回喂重规划。）

`Warning: failed to launch javaldx` 是本机没装 JRE 的正常噪声，可忽略。

## 处理逻辑 (LibreOffice headless)

```bash
# 占位符必须替换成任务给定的真实绝对路径
# -env:UserInstallation 给本次转换一个独立配置目录。多个 soffice 共用默认
# profile 时会互相抢锁：实测 4 个并发只成功 3 个，另一个退出码 1、无产物
soffice --headless -env:UserInstallation=file:///tmp/lo_conv_$$ --convert-to pdf --outdir <输出目录> <输入绝对路径>
```

- 产物名 = **原文件主名 + 新扩展名**，固定落在 `--outdir` 里，不能自定义；
  需要别的名字就在转换后加一条 `mv`。
- 换目标格式只需替换 `--convert-to` 的值，常用：
  `pdf` `docx` `odt` `xlsx` `ods` `pptx` `csv` `html` `txt`。
- 一份表格转 `pdf` 会导出**全部工作表**（每表至少一页），转 `csv`
  则**只导出第一个工作表** —— 多表文件转 csv 时要向用户说明这一点。

### 表格转 PDF 的分页
宽表按纸宽被切成多页是 LibreOffice 的默认行为，命令行没有"缩放到一页"的开关。
用户抱怨排版散乱时应如实说明该限制，并建议改转 `html`（不分页、宽表完整）。

## 输出规范
- 产出文件必须保存在任务指定的输出目录中。
- 输出文件名沿用原文件主名，仅替换扩展名。
- 转成 `.txt` / `.csv` 时产物较小，流水线会直接把内容作为消息发出。
