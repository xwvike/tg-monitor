# 🎬 视频转 GIF 动图 (video_to_gif.md)

## 触发条件
- **文件类型**: mp4, mov, avi, webm, mkv 等常见视频格式
- **意图关键词**: 转gif, gif, 做成表情包, 动图

## 处理逻辑 (FFmpeg 两步调色板法)

### 1. 宽度与抖动

> ⚠️ 任务里若给出「本次 GIF 参数建议」，**直接采用**那一行的 `scale` 与 `dither`。
> 那是代码按本次源视频的实测复杂度算出来的，比下面的通用值准；冲突时以它为准。
> 本节只在没有给出建议时使用。

GIF 无帧间压缩，体积由「输出像素数 × 画面复杂度」决定，而复杂度在素材之间可差
20 倍以上 —— **不存在放之四海皆准的默认宽度，也不要按时长套宽度表**。

- **纯色块 / 文字 / 界面为主**（屏幕录制、动画、演示）：宽度可给到 **1440**，
  配 `dither=none`。这类画面是纯色加锐利边缘，抖动只是往上加噪点，而 GIF 的
  LZW 压不掉噪点 —— 既更胖又更糊。高分屏录屏压到 720 时文字已不可读。
- **实拍 / 渐变 / 全屏运动**：宽度维持 **720**，配 `dither=bayer:bayer_scale=5`。
  这类素材关掉抖动会出明显色带；`bayer_scale` 取 5 优于 3 与 1。
  宽度也不能跟着放宽 —— 同样 1440 宽，这类素材的体积是屏幕录制的二十几倍。
- 用户表达"小一点 / 压一压 / 发微信" → 用 480。
- 输入本身比目标宽度更窄时沿用原宽，**绝不放大**。
- **任何内容都不要用 `sierra2_4a`**：体积翻三倍，画质差异可以忽略。
- 产物明显大于输入视频时，流水线会带着实际体积要求重做；此时**把宽度减半**
  （宽度是平方级杠杆；降帧率只会让动作发卡）。

### 2. 生成命令
两条命令里的 `fps` 与 `scale` 必须完全一致，否则调色板与实际帧不匹配，画质明显劣化。

```bash
# 占位符必须替换成任务给定的真实绝对路径；<宽度> 与 <抖动> 按上一节确定
# max_colors=128 已足够：提到 256 只换来微不足道的画质提升，体积却多两成
ffmpeg -v warning -i <输入绝对路径> -vf "fps=12,scale=<宽度>:-1:flags=lanczos,palettegen=max_colors=128" -y <输出目录>/_palette.png
ffmpeg -v warning -i <输入绝对路径> -i <输出目录>/_palette.png -lavfi "fps=12,scale=<宽度>:-1:flags=lanczos [x]; [x][1:v] paletteuse=dither=<抖动>" -y <输出目录>/<原名>_converted.gif
rm -f <输出目录>/_palette.png
```

不要加 `palettegen=stats_mode=diff`：看似能针对变化区域优化，实测反而增大约 5%。

### 3. 超长视频先截取
超过 30 秒的视频整段转 GIF 必然产出几十 MB。用户未指定范围时默认取**前 10 秒**，
并在回复中说明已截取。

```bash
# -ss 放在 -i 之前可快速定位
ffmpeg -v warning -ss 0 -t 10 -i <输入绝对路径> -vf "fps=12,scale=<宽度>:-1:flags=lanczos,palettegen=max_colors=128" -y <输出目录>/_palette.png
ffmpeg -v warning -ss 0 -t 10 -i <输入绝对路径> -i <输出目录>/_palette.png -lavfi "fps=12,scale=<宽度>:-1:flags=lanczos [x]; [x][1:v] paletteuse=dither=<抖动>" -y <输出目录>/<原名>_converted.gif
rm -f <输出目录>/_palette.png
```

## 输出规范
- 产出文件必须保存在任务指定的输出目录中。
- 输出文件名建议格式: `[原文件名]_converted.gif`
- 调色板等临时文件一律放在输出目录下，并在最后一条命令中删除。
