# ✂️ 视频剪辑与拼接 (video_trim.md)

## 触发条件
- **文件类型**: mp4, mov, avi, webm, mkv 等常见视频格式
- **意图关键词**: 剪辑, 剪掉, 剪去, 去掉, 删掉, 截取, 只要, 保留, 掐头去尾,
  合并视频, 拼接视频, 接起来

## 处理逻辑 (FFmpeg select 滤镜)

### 一律重编码，不要用流拷贝
`-c copy` 只能从关键帧起切，多段剪辑时误差会累积。
实测 30 秒视频剪掉两段：流拷贝方案的画面**整体偏移约 1 秒**，
而 select 滤镜逐帧精确。剪辑对时间点敏感，速度不值得拿正确性换。

### 剪掉指定片段（保留其余）
条件是各区间取"非"再相乘 —— 落在任一区间内即被丢弃。

```bash
# 占位符必须替换成任务给定的真实绝对路径。剪掉 10~15 秒与 20~25 秒：
ffmpeg -v warning -i <输入绝对路径> -vf "select='not(between(t,10,15))*not(between(t,20,25))',setpts=N/FRAME_RATE/TB" -af "aselect='not(between(t,10,15))*not(between(t,20,25))',asetpts=N/SR/TB" -c:v libx264 -preset veryfast -c:a aac -y <输出目录>/<原名>_trimmed.mp4
```

### 只保留指定片段
条件改为各区间相加 —— 落在任一区间内即保留。

```bash
# 只要 5~10 秒与 30~40 秒：
ffmpeg -v warning -i <输入绝对路径> -vf "select='between(t,5,10)+between(t,30,40)',setpts=N/FRAME_RATE/TB" -af "aselect='between(t,5,10)+between(t,30,40)',asetpts=N/SR/TB" -c:v libx264 -preset veryfast -c:a aac -y <输出目录>/<原名>_clip.mp4
```

- `setpts` / `asetpts` **必须带上**，否则丢弃的时间段会留下卡顿的空洞。
- 输入没有音轨时 `-af` 会被 ffmpeg 自动忽略，无需先行判断，照写即可。

### 合并多个视频
**不要用 `-f concat -c copy`**：各输入参数不一致时它不会报错，而是产出坏文件 ——
实测两段各 3 秒、分辨率不同的视频，合并后时长变成 7.2 秒且分辨率被锁成第一个输入。
应统一尺寸后用 concat 滤镜：

```bash
ffmpeg -v warning -i <视频1绝对路径> -i <视频2绝对路径> -filter_complex "[0:v]scale=1280:720:force_original_aspect_ratio=decrease,pad=1280:720:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=30[a];[1:v]scale=1280:720:force_original_aspect_ratio=decrease,pad=1280:720:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=30[b];[a][b]concat=n=2:v=1:a=0[out]" -map "[out]" -c:v libx264 -preset veryfast -y <输出目录>/merged.mp4
```

目标尺寸取各输入中最大的一个；输入超过两个时按同样格式追加 `[2:v]...[c]` 并把 `n=` 改成实际段数。

## 输出规范
- 产出文件必须保存在任务指定的输出目录中。
- 输出文件名建议格式: `[原文件名]_trimmed.mp4` / `[原文件名]_clip.mp4` / `merged.mp4`。
- 用户用「分钟:秒」描述时间点时，换算成秒再填入 `between()`。
