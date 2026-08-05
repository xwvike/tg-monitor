# 🖼️ 图片压缩 (image_compression.md)

## 触发条件
- **文件类型**: jpg, jpeg, png, webp
- **意图关键词**: 压缩, 小一点, 缩小体积, compress, reduce size, 降质量

## 处理逻辑

根据不同的输入格式，选择最优的压缩工具和参数。

### 1. PNG 图片 (使用 pngquant)
PNG 图片首选 `pngquant` 进行有损压缩，可以在肉眼难辨画质损失的情况下减小 60%-80% 的体积。
```bash
# 占位符仅用于说明；生成命令时必须替换成任务给定的真实绝对路径
pngquant --quality=50-80 --force --output <输出目录>/<原名>_compressed.png <输入绝对路径>
```

### 2. JPEG 图片 (使用 ImageMagick)
```bash
# -strip 去除元数据，-quality 70 降低质量
convert <输入绝对路径> -strip -quality 70 <输出目录>/<原名>_compressed.jpg
```

### 3. WebP 图片 (使用 ImageMagick，但**必须输出 webp**)
WebP 本身就比 JPEG 更省，转成 JPEG 是**倒退**：实测同一张图
31.6 KB 的 webp 转成 q70 的 jpg 反而涨到 36.5 KB，而压成 q70 的 webp
只有 25.7 KB。所以输出扩展名照抄输入，不要换格式。
```bash
convert <输入绝对路径> -strip -quality 70 <输出目录>/<原名>_compressed.webp
```

## 输出规范
- 产出文件必须保存在任务指定的输出目录中。
- 文件名格式: `[原文件名]_compressed.[原后缀]` —— **压缩不改变格式**，
  png 出 png、webp 出 webp、jpg 出 jpg。用户明确要求换格式时才另说。
