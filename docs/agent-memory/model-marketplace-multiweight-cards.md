---
name: model-marketplace-multiweight-cards
description: 模型市场 5 张卡是多权重合集（mangajanai 14/CUGAN 11 等）；市场=全量下载 vs 任务=懒下载单权重，安装判定要求全量——体验矛盾与待拍板改进
metadata:
  node_type: memory
  type: project
  originSessionId: sess_f9e027fd-a96b-402a-bf3c-a90f940f6206
---

2026-09-03 用户问「有的一个卡片是不是包含了多个模型」——是。卡片=registry 一个 model id，5 个 id 的 manifest files 挂多个权重：**mangajanai 14 个**（2x/4x × 7 档高度权重，io.auto_variant="height" 按源高自动选档）、**real-cugan 11 个**（2/3/4x × denoise0~3/conservative）、**real-cugan-pro 6 个**、**seemore-b 3 个**（x2/3/4）、**realesr-animevideov3 2 个**（x4+x2）。对照组：AnimeJaNai 家族拆成独立 id 各一张卡（V3.1 四件），同家族两代组织法不一致。

**双口径并存（回答用户此类问题的机制底座）**：
- 市场点「下载」= **全量**：manager.download 拉 manifest 全部 files，卡片 size_mb=全家族体积之和（app.py 对 spec.files sum）。
- 任务运行 = **懒下载单权重**：worker `ensure_files([need])` 只下实际要用的那一个（manager.py 注释明确「多档位模型不必全量下载即可开跑」），file_for_scale 按倍率+变体选、auto_variant 按源高度选。
- `is_downloaded` 安装判定要求**全部**文件在位且大小一致 → 任务懒下载 1 个权重后市场卡仍显「未安装」。

**由此的体验矛盾（已向用户指出，改进方向待拍板）**：①懒下载后卡显未安装，再点下载会把用不到的权重全拉；②全量下载 Real-CUGAN 类用户大概率只用 1~2 个变体；③卡片无「N 个权重档位/已装 x/N」信息。候选方案（已给用户，未实施）：卡透出档位数+部分安装状态；下载提供「只下常用档」或文案说明；懒下载过的不再引导补全。关联 [[modelscope-primary-mirror]]（下载源）、[[bundled-models-v31-swap]]（拆卡组织法对照）。

**多权重下载进度条 bug（2026-09-03 用户实报「到不了 100」，commit ceb0c32 已修，随 v0.4.5 转正发布）**：manager.download 两处漏账叠加——①`_download_to` 的 done 按值传递，外层循环从不累加，文件 2 起进度从 0 重爬，多文件卡峰值=最大单文件/总量（单文件模型 done 初值恰 0 才一直正常）；②尾部跳过的本地文件（懒下载过的权重）静默累加 done 不发事件。修法：`_download_to` 回传本次字节数（主路径 `done +=`，镜像重试 `done = done_at_file + 返回值`）+ 循环末补发 `(total,total)` 终值兜底；回归 `test_multi_file_progress_monotonic_to_full` 断言单调不减且末次=总量。
