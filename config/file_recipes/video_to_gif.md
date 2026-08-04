# 🎬 视频转 GIF 动图 (video_to_gif.md)

## 触发条件
- **文件类型**: mp4, mov, avi, webm, mkv 等常见视频格式
- **意图关键词**: 转gif, gif, 做成表情包, 动图

## ⚠️ 核心约束：GIF 体积极易失控

GIF 没有帧间压缩，体积约与 `宽度² × 帧率 × 时长` 成正比。
**分辨率是最大的杠杆** —— 宽度减半，体积降到约 1/4。

实测（1280x720 / 30fps / 5 秒的渐变类素材）：

| 参数 | 体积 |
|---|---|
| 720px / 12fps / 默认 dither | **9.0 MB** |
| 720px / 12fps / bayer dither | 7.7 MB |
| 480px / 12fps / bayer | 3.7 MB |
| 480px / 10fps / bayer | 3.3 MB |
| 480px / 10fps / 128 色 / bayer | 2.5 MB |
| 360px / 10fps / 128 色 / bayer | 1.5 MB |

**因此严禁默认输出 720px。** 除非用户明确要求高清，一律按下方分档取参。

## 处理逻辑 (FFmpeg 两步调色板法)

### 第 1 步：按输入时长选档

任务信息中已给出输入视频的**时长与分辨率**，据此选择：

| 输入时长 | 宽度 | 帧率 | 调色板色数 | 实测体积 |
|---|---|---|---|---|
| ≤ 6 秒 | 480 | 12 | 128 | ~2.5 MB (5s) |
| 6–20 秒 | 400 | 10 | 128 | ~4.3 MB (15s) |
| > 20 秒 | 360 | 8 | 96 | ~2.6 MB (15s) |

- 输入本身宽度小于档位值时**不要放大**，直接沿用原宽。
- 用户明确说"高清/清晰一点"才可提到 640px，并在回复中说明体积会显著增大。
- 用户给了目标体积时按 `体积 ∝ 宽度²` 外推调整宽度。

### 第 2 步：生成命令

```bash
# 占位符必须替换成任务给定的真实绝对路径。
# 调色板放在输出目录下（不要用 /tmp/palette.png 这类固定名，并发任务会互相覆盖）

# 1. 生成调色板：max_colors 是重要的减重手段，128 色对多数素材肉眼无损
ffmpeg -v warning -i <输入绝对路径> -vf "fps=12,scale=480:-1:flags=lanczos,palettegen=max_colors=128" -y <输出目录>/_palette.png

# 2. 应用调色板：dither=bayer 是关键。
#    默认的 sierra2_4a 误差扩散抖动会在每帧引入不同噪点，破坏 GIF 的帧间冗余，
#    实测比 bayer 大 14%~17%；bayer 的有序抖动在相邻帧间保持一致，压缩友好得多。
ffmpeg -v warning -i <输入绝对路径> -i <输出目录>/_palette.png -lavfi "fps=12,scale=480:-1:flags=lanczos [x]; [x][1:v] paletteuse=dither=bayer:bayer_scale=5" -y <输出目录>/<原名>_converted.gif

# 3. 清理调色板
rm -f <输出目录>/_palette.png
```

> 两条命令里的 `fps` 与 `scale` **必须完全一致**，否则调色板与实际帧不匹配，画质明显劣化。

### 超长视频：截取而非整段转换

超过 30 秒的视频转成 GIF 必然是几十 MB 的产物，既难传输也无实用价值。
此时应当截取片段：

```bash
# 从第 5 秒起截取 10 秒（-ss 放在 -i 之前可快速定位）
ffmpeg -v warning -ss 5 -t 10 -i <输入绝对路径> -vf "fps=10,scale=400:-1:flags=lanczos,palettegen=max_colors=128" -y <输出目录>/_palette.png
ffmpeg -v warning -ss 5 -t 10 -i <输入绝对路径> -i <输出目录>/_palette.png -lavfi "fps=10,scale=400:-1:flags=lanczos [x]; [x][1:v] paletteuse=dither=bayer:bayer_scale=5" -y <输出目录>/<原名>_converted.gif
rm -f <输出目录>/_palette.png
```

用户未指定截取范围时，默认取**前 10 秒**，并在回复中说明已截取。

## ❌ 经实测排除的做法

- **`palettegen=stats_mode=diff`**：理论上应针对变化区域分配调色板，
  实测反而使体积增加约 5%（3.7 MB → 3.9 MB）。**不要使用**。
- **调高 `bayer_scale`**：3 与 5 的体积差异在 1% 以内，用 5 即可，不必纠结。
- **`dither=none`**：体积仅比 bayer 小约 5%，却会在平滑渐变处出现明显色带，不划算。

## 输出规范
- 产出文件必须保存在任务指定的输出目录中。
- 输出文件名建议格式: `[原文件名]_converted.gif`
- 完成后删除临时调色板文件。
- 若产物仍超过 10 MB，下一轮应进一步**降低宽度**（而非帧率）——宽度是平方级杠杆。
