---
name: encoder-choice-drives-output-size
description: 成片体积差异第一嫌疑=编码器选择；同"质量18"软编/硬编刻度不同（实测2.4~2.6倍）；编码链路关键事实
metadata:
  node_type: memory
  type: project
  originSessionId: sess_f65c667e-d92b-4888-91d1-62f949bea1b5
---

2026-09-18 排障定论：用户觉得家里 RTX 5080 机的成片比公司 GTX 1060 机大近 1 倍。两边同模型（AnimeJaNai V3.1 balanced-sharp）、同 x2 1080p→4K、同质量 18，唯一变量是编码器：5080 用 `h264_nvenc`（26.3 Mbps，3.6~4.9GB/集，TRT fps_avg≈72），1060 用 `h264` 软编 libx264（10~11 Mbps，1.4~1.5GB/集，DML 1.88fps）。**两台都正常，不是 bug**；同数字不同刻度 + 硬编压缩效率低一截，同向叠加 ≈2.5 倍。已建议用户换 hevc_nvenc（动画省 40%+）。

编码链路事实（backend/sv/pipeline/stream.py）：
- `_VCODECS`：h264/h265=软编 libx264/x265、*_nvenc=N卡硬编、*_amf=A卡、av1_svt=libsvtav1；codec/crf 是**逐任务参数**（默认 h264/18），不进 settings.json、不跟随源、无自动选择。
- 质量映射各一把尺：软编 `-crf`；NVENC `-rc vbr -cq`（preset 固定 p5，用户的 preset 不生效）；AMF cqp qp_i/qp_p(+2)。
- 全分支恒 `yuv420p` 8bit，无 yuv444/10bit；硬编能力缺失在创建任务时直接 400 拒绝，**不存在运行中静默换编码器**。
- 双路并行/分段不影响体积（concat 是 `-c:v copy`，segmented.py:107）；段按帧数切（约 8 段、每段≤600帧）与机器无关。
- 核对历史任务实际编码参数：查 `sidecar.db` tasks 表 params 列（sr_logs 头部只记推理后端，**不含 codec/crf**）。

公司 1060 机 = [[gbk-settings-unicode-engine-fail]] 里的 1060 实机；其瓶颈在 DML 推理（infer 526ms/帧），软编 write 才 13.7ms/帧。本机装版数据在 D:\soft\super_video_data（见 [[d-soft-install-healthy]]）。
