"""Worker 引擎装配：onnx 引擎构建+预热+降档重试（视频/图片任务共用）。"""
from __future__ import annotations

import sys
from pathlib import Path

from sv.engines.onnx_engine import OnnxSrEngine
from sv.models.fp16 import ensure_fp16_file
from sv.server import settings


def _cugan_alt_hint() -> str:
    """CUGAN 拒绝信息的替代路径：按显卡品牌给可行建议（非 N 卡装 TRT 是死路）。"""
    nvidia = False
    try:
        from .hardware import hardware_info

        gpus = hardware_info().get("gpus") or []
        nvidia = any("nvidia" in str(g.get("name", "")).lower() for g in gpus)
    except Exception:  # noqa: BLE001 — 硬件检测失败按通用建议
        pass
    if nvidia:
        return ("请安装 TensorRT 组件并将引擎设为 TensorRT（设置 → 推理后端），"
                "或显式切换到 CPU 后端（较慢）。")
    return ("本机无 N 卡时 TensorRT 组件不可用：请将引擎切到 CPU（设置 → 推理后端，"
            "较慢），或改用 AnimeJaNai / MangaJaNai 系模型（DirectML 正常）。")


# 常驻引擎缓存（进程内单条）：serve 模式下同签名任务跨任务复用已预热的会话。
# 签名只用入口参数（全是任务间确定性的量）；加载循环里的降 tile/降链（OOM 减半、
# TRT→trt_cpu、CUDA→cpu）是同签名下的确定性修复路径——降链成功后的会话对下一个
# 同签名任务同样适用，故缓存条目按初值签名存「落定后的引擎」。
# 失败任务后进程被 runner 丢弃重建（runner 只在 done 后续喂），不会带着损坏会话复用。
_ENGINE_CACHE: dict = {}


def _engine_sig(weight: Path, scale: int, variant: str | None, precision: str,
                tile: int, batch: int, warmup_hw: tuple[int, int], ort_device: str) -> tuple:
    return (str(weight), int(scale), variant or "", precision, int(tile),
            int(batch), tuple(warmup_hw), ort_device)


def _load_onnx_engine(
    weight: Path,
    spec,
    scale: int,
    variant: str | None,
    precision: str,
    tile: int,
    warmup_hw: tuple[int, int],
    *,
    batch: int = 1,
    log=None,
) -> tuple["OnnxSrEngine", str]:
    """构建并预热 onnx 推理引擎（视频/图片任务共用）。

    含 fp16 惰性补转、TRT 组件激活与编译提示、显存不足自动减半 tile 重试。
    warmup 必须用源帧真实尺寸：DML 会话跑过小形状后拖慢真实尺寸且不可逆。
    返回 (engine, 实际使用精度)。失败向上抛，由调用方转 failed 事件。
    serve 常驻模式下同签名任务直接复用已预热会话（TRT 反序列化 5-8s/路、
    DML 预热数十帧的开销从「每任务一次」降为「每签名一次」）。
    """
    # 惰性补转 fp16（一次），失败回退 fp32；spec.fp16=False 的模型（如 real-cugan，
    # 转换后 ShapeInference 崩）直接用 fp32 原件
    if precision == "fp16" and spec.fp16 and not weight.stem.endswith("_fp16"):
        if log:
            log({"type": "log", "line": f"生成 fp16 变体: {weight.name}"})
        weight = ensure_fp16_file(weight)
    used_precision = "fp16" if weight.stem.endswith("_fp16") else "fp32"

    def _oom(e: Exception) -> bool:
        t = str(e).lower()
        if "memory" in t or "alloc" in t or "oom" in t:
            return True
        # DML 显存不足在中文 Windows 上被 UnicodeDecodeError 掩盖：ORT 的错误
        # 消息 bytes 内嵌系统 GBK 错误描述（0x8007000E「存储空间不足…」），
        # Python 绑定层按 UTF-8 硬解直接抛解码错误——str(e) 只剩无用的解码
        # 报文。原始 bytes 挂在 UnicodeDecodeError.args[1]：GBK/UTF-8 双解
        # 后按错误码/关键字识别（MangaJaNai 大图 4x 全尺寸 warmup 实锤，
        # 此前降 tile 重试被假象骗过直接失败）
        for a in e.args:
            if not isinstance(a, (bytes, bytearray)):
                continue
            blob = bytes(a)
            s = blob.decode("gbk", "replace") + blob.decode("utf-8", "replace")
            s = s.lower()
            if ("8007000e" in s or "e_outofmemory" in s or "out of memory" in s
                    or "存储空间不足" in s):
                return True
        return False

    def _cuda_kernel_fault(e: Exception) -> bool:
        """CUDA 执行层内核崩溃特征：报错串带 GPU 版 ORT 的 providers/cuda 源路径
        （如 fast_divmod 断言的 Resize 缺陷）。只认精确特征，避免把普通
        CUDA 初始化/驱动问题误判成「换链能救」。"""
        t = str(e)
        return "RUNTIME_EXCEPTION" in t and "providers/cuda" in t

    # 推理后端：设置 engine=trt 走 TensorRT 链（TRT 不可用引擎层自动回退）；
    # engine=cpu 显式锁 CPUExecutionProvider（CUGAN×DML 泄漏模型的兜底通道）
    eng_setting = settings.load().get("engine")
    ort_device = eng_setting if eng_setting in ("trt", "cpu") else "auto"

    # 常驻缓存查询：签名要素（含 ort_device 初值）全部命中才复用——
    # 已预热的会话跳过整个构建+预热（warmup 一帧在 DML 上也不是零成本）。
    # 注意precision 要用补转后落定的值：fp16 补转会改变 weight 路径，
    # sig 里的 weight 串已经反映，这里只补充 used_precision 口径一致。
    sig = _engine_sig(weight, scale, variant, precision, tile, batch, warmup_hw, ort_device)
    cached = _ENGINE_CACHE.get("sig")
    if cached == sig:
        if log:
            log({"type": "log", "line": "复用常驻引擎（同模型/同后端/同分辨率，跳过加载预热）"})
        return _ENGINE_CACHE["engine"], _ENGINE_CACHE["precision"]
    # 大图输出像素预算：无 tile 且放大后像素超 36M（1080p x4=33M 不动、
    # 漫画扫描页 2133p x4≈51M 命中）时预设 512 分块——全尺寸会话会把 GPU
    # 打爆（中文 Windows 上 OOM 被 GBK 错误消息的解码异常掩盖成
    # UnicodeDecodeError），且 OOM 后同进程 DML 会话已损坏，降档重建只会
    # 被静默回退 CPU（实测 gc 释放也救不回）——干净会话一次到位才是正解
    if tile == 0 and warmup_hw[0] * warmup_hw[1] * scale * scale > 36_000_000:
        tile = 512
        if log:
            log({"type": "log", "line":
                 "输出分辨率较大，自动启用 512px 分块处理（避免显存不足）"})
    # CUGAN×DML 逐帧显存泄漏（实测 1080p BASIC≈8 帧、ALL≈30 帧即 0x887A0006
    # 设备摘除，全 GPU 会话连坐；540p 阈值更高但终会爆；与线程/包装/内容无关），
    # CUDA EP 实测同样挂起。仅 TRT 与显式 CPU 可用（TRT 端到端 14.9fps 验证）。
    # 证据链 BENCH.md §13。此处直接拒绝，避免用户任务跑到一半设备崩溃。
    if "cugan" in spec.id.lower() and eng_setting not in ("trt", "cpu"):
        raise RuntimeError(
            f"模型 {spec.id} 在 DirectML/CUDA 后端存在 GPU 显存泄漏崩溃（实测高分辨率"
            f"约 8~30 帧即设备摘除），当前设置 engine={eng_setting or 'auto'} 不可用。"
            + _cugan_alt_hint())
    if ort_device == "trt":
        if getattr(sys, "frozen", False):
            # 安装版：激活 TRT 组件（GPU 版 onnxruntime 重定向）。必须在
            # 进程内首次 import onnxruntime 之前（OnnxSrEngine.load 惰性导入）；
            # 组件缺失/不兼容返回 False → 引擎层自然回退 DML
            from sv.engines.trt_runtime import activate_component

            if activate_component():
                if log:
                    log({"type": "log", "line": "TensorRT 加速组件已加载"})
        if log:
            log({"type": "log", "line":
                 "TensorRT 引擎加载中：新模型或新分辨率首次使用需编译引擎（约 1~2 分钟），完成后可直接加载"})
    engine = None
    if _ENGINE_CACHE.get("sig") != sig:
        # 换签名重建：先释放上一个任务的常驻引擎（DML/CUDA 资源回收靠析构）
        _ENGINE_CACHE.clear()
        import gc

        gc.collect()
    # 显存不足自动降档 tile 减半（最多 3 次）；CUDA 执行层内核崩溃换链重试（一次）
    for attempt in range(4):
        try:
            engine = OnnxSrEngine(
                weight, scale, io=spec.io, tile=tile, batch=batch,
                device=ort_device, validate_hw=warmup_hw)
            engine.load()
            import numpy as np
            # 预热兼显存探测。必须用源帧真实尺寸：DML 会话一旦跑过 64x64 这类
            # 小形状，后续真实尺寸的执行路径被拖慢且不可逆（实测 960x720：
            # 25.7ms -> 38.5ms/帧，+50%；先小后大也无法自愈）
            engine.process(np.zeros((warmup_hw[0], warmup_hw[1], 3), dtype=np.uint8))
            break
        except Exception as e:  # noqa: BLE001
            # CUDA/TRT 执行层的内核崩溃（非显存问题）：MxNet 导出的 Resize 节点
            # （MangaJaNai 系）在 CUDA EP 有 fast_divmod 断言缺陷，DML/CPU 正常。
            # TRT 场景换「TRT+CPU 兜底」链重试（主图仍 TRT 编译，坏节点落 CPU，
            # 速度几乎无损）；纯 CUDA 场景无 GPU 替代，退 CPU 保出片并说明。
            # 用 eng_setting 判定（cuda 设置映射的 ort_device 是 auto）；
            # 降链后 ort_device 离开初值，天然只降一次
            if (not _oom(e) and _cuda_kernel_fault(e)
                    and (ort_device == "trt" or eng_setting == "cuda")):
                if eng_setting == "trt":
                    ort_device = "trt_cpu"
                    if log:
                        log({"type": "log", "line":
                             "CUDA 执行层在个别算子上崩溃，已切换为 TensorRT+CPU 混合执行重试"})
                else:
                    ort_device = "cpu"
                    if log:
                        log({"type": "log", "line":
                             "CUDA 执行层在个别算子上崩溃，已切换为 CPU 推理重试（速度较慢）"})
                continue
            # 最后一次尝试仍失败必须 raise：带着没加载成功的 engine 继续走，
            # 后面会以更难懂的方式崩（如 provider_used AttributeError）
            if not _oom(e) or tile in (1,) or attempt == 3:
                raise
            new_tile = 256 if tile == 0 else max(64, tile // 2)
            # 先释放失败引擎再降档重建：OOM 的旧 session 仍攥着 DML/CUDA 资源，
            # 不放手时降 tile 后的新会话会接着爆（DML 资源回收依赖对象析构）
            engine = None
            import gc

            gc.collect()
            if log:
                log({"type": "log", "line":
                     f"显存不足，分块大小调整为 {new_tile} 后重试"})
            tile = new_tile
    _ENGINE_CACHE.clear()  # 单条语义：换签名即释放旧引擎（显存）再换入
    _ENGINE_CACHE.update({"sig": sig, "engine": engine, "precision": used_precision})
    return engine, used_precision

