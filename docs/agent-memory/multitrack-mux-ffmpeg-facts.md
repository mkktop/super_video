---
name: multitrack-mux-ffmpeg-facts
description: 多音轨/章节/字体附件保留已产品化；ffmpeg 混流实证结论（mp4 挂附件必败等）
metadata:
  node_type: memory
  type: project
  originSessionId: sess_dbca44ab-06c2-4d5d-8458-e20ad2642c11
---

2026-08-27 落地「多音轨全保留」：三条管线（stream/segmented/chunked）合成命令从 `-map 1:a:0?` 改为 `-map 1:a?`；MP4 auto 模式逐轨定编码（全可拷=统一 copy、全不可拷=统一 aac、混合=逐轨 `-c:a:N`），MKV 全轨 copy。同时显式 `-map_chapters 1`、MKV 保留字幕时连字体附件 `-map 1:t?`（subtitle_args 一并返回）。probe API 加 `audio_tracks` 字段，前端音轨下拉旁提示「将保留全部 N 条音轨」。

**Why:** 之前多轨源静默只剩第一条音轨；用户问「多字幕多音轨超分保不保留」促成此改。

**How to apply:** 实证过的 ffmpeg 混流事实（Windows BtbN 8.1）：
- MP4/MOV 输出显式 `-map 1:t?` 挂附件会**硬失败**（Could not find tag for codec ttf），附件只能进 MKV，必须按容器门控。
- 章节默认从「首个含章节的输入」复制（非仅 input 0），所以旧版其实已在隐式保留；显式 `-map_chapters 1` 只是固化。
- mkv 源章节进 mp4 时 ffmpeg 自动生成 tx3g 文本轨（ffprobe 显示 data/bin_data 流），是 QuickTime 标准章节轨，非异常。
- `-attach` 会重置默认流选择只留一条最佳音轨——生成测试样本时要配显式 `-map`。
- ffmetadata 无自动探测，`-i meta.txt` 必须带 `-f ffmetadata`；文件需 LF 行尾。
- 假字节流 .ttf 附件（mimetype 标 application/x-truetype-font）ffprobe 照样识别为 attachment/ttf，E2E 测试不依赖系统字体。

关联 [[super-video-project-goal]]。
