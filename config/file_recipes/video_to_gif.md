# 🎬 视频转 GIF 动图 (video_to_gif.md)

## 触发条件
- **文件类型**: mp4, mov, avi, webm 等常见视频格式
- **意图关键词**: 转gif, gif, 做成表情包, 动图

## 处理逻辑 (FFmpeg)
使用 FFmpeg 进行视频到 GIF 的转换。为保证 GIF 体积不至于过大，请遵循以下参数：
1. **帧率**: 建议降低到 10-15 fps。
2. **分辨率**: 宽度最大限制为 720px（保持比例）。
3. **调色板优化**: 先生成调色板，再用调色板生成 GIF，这样画质最好且体积小。

**标准执行命令**:
```bash
# 假设环境变量 INPUT 为输入视频路径，OUTPUT 为输出 GIF 路径
# 1. 生成全局调色板
ffmpeg -v warning -i "$INPUT" -vf "fps=12,scale=720:-1:flags=lanczos,palettegen" -y /tmp/palette.png

# 2. 使用调色板生成高质量 GIF
ffmpeg -v warning -i "$INPUT" -i /tmp/palette.png -lavfi "fps=12,scale=720:-1:flags=lanczos [x]; [x][1:v] paletteuse" -y "$OUTPUT"
```

## 输出规范
- 产出文件必须保存在用户请求时分配的 `workspace_out` 目录中。
- 输出文件名建议格式: `[原文件名]_converted.gif`
- 完成后删除临时的 `palette.png`。
