# 🎬 视频转 GIF 动图 (video_to_gif.md)

## 触发条件
- **文件类型**: mp4, mov, avi, webm, mkv 等常见视频格式
- **意图关键词**: 转gif, gif, 做成表情包, 动图

## 处理逻辑 (FFmpeg 两步调色板法)

### 1. 选择输出宽度
GIF 无帧间压缩，体积约与宽度平方成正比；但实际大小高度依赖画面内容
（同参数下静态画面与全屏运动可相差二十倍），**不要按时长预设宽度**。

- **默认 720px**。输入本身更窄时沿用原宽，不要放大。
- 用户表达"小一点 / 压一压 / 发微信" → 用 480px。
- 产物明显大于输入视频时，流水线会带着实际体积要求重做；此时**把宽度减半**
  （宽度是平方级杠杆；降帧率只会让动作发卡）。

### 2. 生成命令
两条命令里的 `fps` 与 `scale` 必须完全一致，否则调色板与实际帧不匹配，画质明显劣化。

```bash
# 占位符必须替换成任务给定的真实绝对路径
# max_colors=128 与 dither=bayer 是两个关键减重手段：
# 默认的误差扩散抖动每帧噪点不同，会破坏 GIF 的帧间冗余，实测体积大 14%~17%
ffmpeg -v warning -i <输入绝对路径> -vf "fps=12,scale=720:-1:flags=lanczos,palettegen=max_colors=128" -y <输出目录>/_palette.png
ffmpeg -v warning -i <输入绝对路径> -i <输出目录>/_palette.png -lavfi "fps=12,scale=720:-1:flags=lanczos [x]; [x][1:v] paletteuse=dither=bayer:bayer_scale=5" -y <输出目录>/<原名>_converted.gif
rm -f <输出目录>/_palette.png
```

不要加 `palettegen=stats_mode=diff`：看似能针对变化区域优化，实测反而增大约 5%。

### 3. 超长视频先截取
超过 30 秒的视频整段转 GIF 必然产出几十 MB。用户未指定范围时默认取**前 10 秒**，
并在回复中说明已截取。

```bash
# -ss 放在 -i 之前可快速定位
ffmpeg -v warning -ss 0 -t 10 -i <输入绝对路径> -vf "fps=12,scale=720:-1:flags=lanczos,palettegen=max_colors=128" -y <输出目录>/_palette.png
ffmpeg -v warning -ss 0 -t 10 -i <输入绝对路径> -i <输出目录>/_palette.png -lavfi "fps=12,scale=720:-1:flags=lanczos [x]; [x][1:v] paletteuse=dither=bayer:bayer_scale=5" -y <输出目录>/<原名>_converted.gif
rm -f <输出目录>/_palette.png
```

## 输出规范
- 产出文件必须保存在任务指定的输出目录中。
- 输出文件名建议格式: `[原文件名]_converted.gif`
- 调色板等临时文件一律放在输出目录下，并在最后一条命令中删除。
