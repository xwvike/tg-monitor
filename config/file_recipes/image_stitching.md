# 🖼️ 图片拼接与排版 (image_stitching.md)

## 触发条件
- **文件类型**: 包含多张图片的压缩包 (zip/rar) 或单次接收到的多图目录，格式为 jpg/png/webp 等
- **意图关键词**: 拼接, 拼图, 长图, 左右拼接, 上下拼接, 合成

## 处理逻辑 (ImageMagick)

图片拼接的核心在于**尺寸对齐**。如果多张图片原尺寸不同，直接拼接会导致边缘参差不齐。请务必根据用户要求的拼接方向，使用 `convert` 命令统一它们的宽或高。

### 1. 垂直拼接（长图 / 上下拼接）
**规则**：所有图片必须统一**宽度**。
**步骤**：
```bash
# 假设多张图片在当前目录下
# -resize 1080x 表示将所有图片按比例缩放，使得宽度统一为 1080 像素
# -append 表示垂直拼接（从上到下）
convert image1.jpg image2.jpg image3.jpg -resize 1080x -append "$OUTPUT_DIR/stitched_vertical.jpg"
```

### 2. 水平拼接（左右拼接）
**规则**：所有图片必须统一**高度**。
**步骤**：
```bash
# -resize x1080 表示将所有图片按比例缩放，使得高度统一为 1080 像素
# +append 表示水平拼接（从左到右）
convert image1.jpg image2.jpg image3.jpg -resize x1080 +append "$OUTPUT_DIR/stitched_horizontal.jpg"
```

### 3. 拼接前的单图预处理（按需）
如果用户要求在拼接前对特定图片进行处理，可以在 `convert` 管道中加入参数，或者分步执行：
```bash
# 旋转 90 度
convert image1.jpg -rotate 90 temp1.jpg

# 裁剪 (宽高+X偏移+Y偏移)
convert image2.jpg -crop 800x800+10+10 temp2.jpg

# 最后再将预处理好的图片进行统一拼接
convert temp1.jpg temp2.jpg -resize 1080x -append "$OUTPUT_DIR/final_stitched.jpg"
```

## 输出规范
- 最终生成的拼接图片必须保存在分配的 `workspace_out` 目录中。
- 输出文件名建议格式: `stitched_result.jpg` (如果是透明背景拼接，请使用 `.png`)
- 拼接完成后，请自行清理中途产生的解压文件或临时图片。
