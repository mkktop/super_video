---
name: vset-model-gap-and-tier1
description: 模型扩容梯队与图片/漫画超分候选；2026-09-01 用户澄清项目非商用→NC 许可解锁，漫画首选 MangaJaNai
metadata:
  node_type: memory
  type: project
  originSessionId: sess_d586f378-24f9-4f14-8371-bf87966cb77e
---

对标 NangInShell/VSET（VapourSynth+vs-mlrt 图形壳）后的模型差距与落地状态：

**已落地（2026-08-30，第一梯队）**：real-cugan-pro（x2/x3 × 保守/denoise0/denoise3，io.affine=[0.7,0.15] 动态范围压缩 + pad 按倍率分档 {"2":2,"3":4}，引擎已支持，spec.id 含 cugan 自动继承 DML/CUDA 拒绝围栏）；ani4k-v2-compact / ani4k-v2-ultracompact（任意边长免对齐、fp16 转换已验证、u8 包装生效）。顺带修复 latest real-cugan x3 pad=3 潜伏 bug（x3 实际需 4 的倍数）。资产已传 models-v1。详见 BENCH.md「第一梯队新模型实证」节。

**V3.1 已落地（2026-08-31）**：AnimeJaNai V3.1 HD 四权重（Performance/Balanced × Standard/Sharp1 独立权重，SPANF3 fp16 原生，上游 mpv-AnimeJaNai 3.3.0+）——RGB/0-1/无仿射/任意边长/batch 固定 1（io.batch_hint=1，fp16: false 同 V2 惯例）。落地时挖出 **DML u8 包装校验失败→回退→首帧原生崩**：包装 session 创建后被 GC 析构损坏 DML 设备（CUGAN 互踩的析构侧变种）；修复=失败 session 保引用（_u8_sess_try）+ DML 校验容差 1→2。教训：DML 类崩溃排查必须 python -u（管道缓冲会吞掉崩溃前全部输出）。v0.3.2 已发布（tag 挪过一次让安装包带上模型页描述优化）。

**许可口径已变（2026-09-01 用户澄清：项目非商用）**：CC BY-NC 不再是排除条件——MangaJaNai、OpenModelDB 社区热门（UltraSharp/Remacri/AnimeSharp 等）、NTU S-Lab 系（ResShift/StableSR）、SUPIR 全部解锁，Ani4K 无摘除压力；只剩署名义务 + 个别"许可不明"条目需逐个核。此前"商业化须摘除 Ani4K"的结论仅在未来商业化时重启。

**图片/漫画超分扩容已落地（2026-09-02，commit 65b3045）**：14 卡 29 资产传 models-v1（38→67，API digest 全对）。漫画 MangaJaNai×14（2x/4x×7 高度档，io.auto_variant="height" 引擎按源高度自动选权重，worker 两路径+compare）+ IllustrationJaNai×3（2x 官方 fp16 SPAN_S / 4x ESRGAN / 4x DAT2）+ HAT real GAN 标准/Sharp + SwinIR-L + x4v3/wdn + anime_6B + SeemoRe B x2/x3/x4 + 社区三款（UltraSharp/AnimeSharp 官方 ONNX，Remacri 自导）。content 新增 "comic"、市场页漫画 tab、FAMILIES 支持 ids 列表。导出工具链：spandrel 0.4.2 + torch2.14-cpu + opset17 动态轴即可（无需 CUDA）；**窗口注意力/MoE 一律 256 定长导出**（HAT/SwinIR/DAT2 动态导出 Reshape 烤死常量、SeemoRe 动态导出 H≥96 MoE 路由发散 47% 像素——小尺寸验证会掩盖，必须多尺寸逐位对照）；spandrel ESRGAN pad 分支被 trace 折叠 → io.pad 按倍率补边。测试 test_imagesr_batch.py +20。waifu2x 不入库（被 MangaJaNai 覆盖）；OSEDiff=独立 AI 修复功能待立项。

**图片/漫画候选调研明细（2026-09-01 两轮，许可全部查证源头）**：
- **漫画首选 MangaJaNai**（the-database/AnimeJaNai 同作者姊妹项目，CC BY-NC 4.0）：唯一认真漫画专模——14 个黑白模型按源图高度分档（1200p~2048p，网点频率对位）×2x/4x + IllustrationJaNai 彩色页（DAT2/FDAT/HAT-L/SPAN）+ DeJPEG 变体；仅 PyTorch .pth 需自导 ONNX，按高度自动选模型需引擎支持。GitHub 全站无第二个像样漫画专模。
- 真人图片 4x：HAT Real_HAT_GAN_SRx4(+sharper)（XPixelGroup，Apache-2.0，比 x4plus 稳）/ SwinIR-L realSR x4 GAN（Apache-2.0，权重挂 repo Releases，训练集含 Manga109）——都要自导 ONNX + 窗口注意力 padding；快档 realesr-general-x4v3(+wdn)（BSD-3，SRVGG tiny）。
- SeemoRe（eduardzamfir，Apache-2.0，ICML'24 效率档 2x/3x/4x）补通用 3x 空档（MoE 结构导出需验证）；SPAN 官方 DIV2K 权重（hongyuanyu/SPAN，Apache-2.0，NTIRE'24 效率冠军，AnimeJaNai V3.1 同架构）优先级低。
- RealESRGAN_x4plus_anime_6B（BSD-3，release v0.2.2.4）动漫图片经典专模。
- waifu2x 现代版在 nagadomi/nunif（MIT）：art/scan 风格 + cunet 画质档，噪点 0-3 × 1x/2x/4x，scan 面向扫描稿；比 vs-mlrt 那包 swin_unet（许可未验证）干净，优先用 nunif 线。
- 扩散修复档（画质天花板，独立功能非市场卡片）：OSEDiff（cswry，Apache-2.0，一步扩散）优先；ResShift/StableSR（NTU S-Lab 1.0，非商用可用）、SUPIR（README 附加 NC 声明）次之——都是 SD 底座多组件管线，跑在 ORT 单模型语义外。
- ❌ MambaIR 系（技术原因不变）：selective_scan 自定义 CUDA 算子 ONNX 出不来。

**第二梯队候选（2026-09-01 全面复核后改写）**：

- **❌ AniSD / AniScale / AniScale2 家族被否决**（推翻 8-30 清单）：ONNX 只随 VSET 百度网盘整合包私下分发（提取码 nang），GitHub/HuggingFace/fork 均无公开权重、无许可证声明；vs-mlrt 上游枚举只到 7001（Ani4K），7010-7016 是 VSET 私改 vsmlrt。接入需作者（NangInShell，QQ 群 711185279/BiliBili）书面授权，商业风险未知。x1 精修（AniSD_DB/AniScale2_Refiner）随之搁置——引擎侧已核实 scale=1 近乎免费（pad 默认=scale、裁剪 h*scale），只差 UI/任务语义，待有合法 x1 权重再启用。
- **✅ ArtCNN 已落地（2026-09-01，Artoriuz，MIT）**：实测修正——主力线（C4F16/C4F32/R8F64+DN/DS）全是单通道 Y 的 2x luma doubler（非 4x 非 RGB），为此引擎新增 io.color="y"（RGB↔YCbCr BT.601 全域 + PIL mode F BICUBIC 色度放大，u8 包装/批处理对 y 关闭）。入库 3 款：artcnn-c4f16 / c4f16-dn / r8f64（0-1 域判别式实证、pad=1 免对齐、DML 对照 ≤1、fp16 转换 3.7→1.9MB 差 ≤1）。R8F64_RGB 变体未入库（避免与 Y 线重叠）；Chroma/JPEG 系仍不碰。
- **✅ DIS 已落地（2026-09-01，Kim2091，Apache-2.0）**：dis-2x-balanced / dis-2x-fast，三通道 0-1 原生 fp16、严格 2x、奇数边长直跑、u8 包装双后端过 A/B；填真人 x2 空档。
- **✅ RIFE 4.26_heavy（hzwer，MIT）**：补帧质量档，与 v4.26 同架构同 mod64 约束，预计近乎 drop-in；vs-mlrt external-models 单包直链。官方最新仍是 v4.26（无 4.27+）。
- **△ SwinIR lightweightSR x2/x3/x4（Apache-2.0 常识未复核）**：在 vs-mlrt swinir_v1.7z（1.1GB，需解包取 3 文件重传 models-v1）；窗口注意力需固定 tile 尺寸，我们 tile 引擎理论支持但需实测。
- **△ waifu2x swin_unet（art/photo/photo_v2/art_scan，noise0-3 × x1/x2/x4）**：权重在 waifu2x_swin_unet_v5.7z，**许可证未验证**（社区转换，源头模糊），接入前先核。
- **△ SAFA v0.1-0.5（CVPR'25 高效超分）**：vs-mlrt 2025 新增（safa_v0.x.7z），许可证未验证，候选观察。
- SCUNet（505MB 去噪）太重跳过；AnimeJaNai 上游 3.6.0 仍是最新（3.2 起只是打包升级，权重线未变），无新东西可跟。

**Why:** 模型扩容是对标竞品的核心维度，梯队清单和许可边界是后续会话的决策输入。
**How to apply:** 做第二梯队时先读 BENCH.md 该节与 [[model-broadening-and-trt]] 的通道序/fp16 实测惯例；TRT 真机基准出来后再调 recommend.py 的 _ANIME_PREF 推荐位次；新模型一律重传 models-v1 平铺引用（[[models-v1-single-source]]）。
