# 🎬 视频压缩 (video_compression.md)

## 触发条件
- **文件类型**: mp4, mov, avi, webm, mkv 等常见视频格式
- **意图关键词**: 压缩, 压一下, 小一点, 减小, 瘦身, 太大, 发不出去, 邮件附件

## ⚠️ 先看码率：低码率视频再压会越压越大

任务信息中已给出输入的**码率**。H.264 重编码不是"总能变小" ——
实测对一段 1396 kbps 的视频再压：CRF 23 → **体积增加 36%**，CRF 26 → 持平，
CRF 28 才开始变小。

- 码率 **> 2500 kbps**：正常压，按下表选 CRF。
- 码率 **1000~2500 kbps**：已经压过一轮，CRF 至少用 30，或直接降分辨率。
- 码率 **< 1000 kbps**：不要再调 CRF，只能降分辨率；若用户没有明确要求，
  应如实告知"该视频码率已很低，继续压缩会明显损伤画质"。

## 处理逻辑 (FFmpeg / libx264)

### 各杠杆的实测效力

| 手段 | 效果 |
|---|---|
| CRF 23 → 28 | 降至约 1/2 |
| CRF 28 → 32 | 再降约 1/3 |
| 分辨率 720p → 480p | **降至 8%~21%（最强）** |
| 音频 128k → 64k | 仅降约 10%，且只对讲话类有意义 |
| preset 调慢 | **不是体积杠杆**，实测 slow 反而比 veryfast 更大 |

CRF 是画质目标而非体积目标，调慢 preset 只会在同 CRF 下换取更好画质，
不会让文件变小。一律用 `veryfast`，省时间。

### 命令

```bash
# 占位符必须替换成任务给定的真实绝对路径
# CRF 28 是默认档；-movflags +faststart 让视频边下边播
ffmpeg -v warning -i <输入绝对路径> -c:v libx264 -crf 28 -preset veryfast -pix_fmt yuv420p -c:a copy -movflags +faststart -y <输出目录>/<原名>_compressed.mp4
```

需要更小时优先降分辨率（`-2` 保证宽高为偶数，否则 H.264 编码会失败）：

```bash
ffmpeg -v warning -i <输入绝对路径> -vf "scale=-2:480" -c:v libx264 -crf 28 -preset veryfast -pix_fmt yuv420p -c:a aac -b:a 96k -movflags +faststart -y <输出目录>/<原名>_compressed.mp4
```

- 用户强调"清晰一点"用 CRF 23；强调"越小越好"用 CRF 32 并降到 480p。
- `-c:a copy` 保留原音轨即可；只有讲话类内容且体积吃紧时才降到 `-b:a 64k`。

## 输出规范
- 产出文件必须保存在任务指定的输出目录中。
- 输出文件名建议格式: `[原文件名]_compressed.mp4`
- 若压缩后体积不降反增，应放弃该结果并如实告知用户源视频已无压缩空间。
