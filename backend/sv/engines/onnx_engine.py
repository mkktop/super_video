"""ONNX 超分引擎（Real-ESRGAN / animevideov3 等模型族）。

输入约定由 manifest 的 io 字段描述：
  color: "bgr" | "rgb"（模型训练时的通道序）
  range: "0-255"（float32，不归一化——Real-ESRGAN 系约定）
pad: 输入边长需对齐的最小倍数（pixelshuffle 需要，通常 = scale）；
  可按倍率分档 {"2":2,"3":4}（CUGAN 系 up3x 需 4 的倍数而非 3）
affine: [a, b]（0-1 域入图 x*a+b、出图 (y-b)/a——CUGAN Pro 动态范围压缩，
  对齐上游 vsmlrt conformance；带仿射的模型跳过 u8 包装）
"""
from __future__ import annotations

import os
from pathlib import Path

import numpy as np

from .base import BaseEngine

# 按 preference 排序，取运行时可用的第一个组合（onnxruntime-directml 提供 DML+CPU）
_PROVIDER_ORDER = [
    "CUDAExecutionProvider",
    "DmlExecutionProvider",
    "CPUExecutionProvider",
]

# TensorRT 模式：TRT 优先，其后照常回落（TRT 不可用/失败时与默认链路等价）
_TRT_CHAIN = [
    "TensorrtExecutionProvider",
    "CUDAExecutionProvider",
    "DmlExecutionProvider",
    "CPUExecutionProvider",
]


def _trt_provider_options() -> list[tuple[str, dict[str, object]]]:
    """TRT EP 选项：引擎缓存（二次运行免重建，分钟级差异）+ fp16 引擎。

    onnxruntime 的 TRT EP 只需 pip 包含该 provider（onnxruntime-gpu）；
    运行时另需 TensorRT 库（可选组件 tensorrt wheel）——缺失时 session
    创建失败，由 load() 分层回退到 CUDA/DML。
    """
    from ..paths import TEMP_DIR

    cache = TEMP_DIR / "trt_cache"
    cache.mkdir(parents=True, exist_ok=True)
    return [(
        "TensorrtExecutionProvider",
        {
            "device_id": 0,
            "trt_engine_cache_enable": True,
            "trt_engine_cache_path": str(cache),
            "trt_timing_cache_enable": True,
            "trt_fp16_enable": True,
            "trt_max_workspace_size": 4294967296,  # 4GB
        },
    )]


class OnnxSrEngine(BaseEngine):
    def __init__(
        self,
        model_path: str | Path,
        scale: int,
        io: dict | None = None,
        device: str = "auto",
        tile: int = 0,
        tile_overlap: int = 16,
        batch: int = 1,
        u8_wrap: bool = True,  # 调用方可强制关；manifest 亦可声明 u8_wrap=false
        validate_hw: tuple[int, int] | None = None,  # 源帧 (H,W)：u8 校验形状
        manifest_allow_wrap: bool = True,
    ):
        self.model_path = Path(model_path)
        self.scale = scale
        io = io or {}
        self.color = io.get("color", "bgr")
        self.value_range = io.get("range", "0-255")
        # pad 可按倍率分档（{"2":2,"3":4}）：同族不同倍率的整除需求不同——
        # CUGAN 系 up2x 只需偶数、up3x 需 4 的倍数（UNet 两次下采样，实测
        # 30x30 拒跑而 32x32 通过），单一值要么错要么过度补边
        pad_spec = io.get("pad", scale)
        if isinstance(pad_spec, dict):
            pad_spec = pad_spec.get(str(scale), scale)
        self.pad = int(pad_spec)
        # 入图仿射（0-1 域 x*a+b，出图 (y-b)/a）：CUGAN Pro 训练时输入动态
        # 范围压缩到 [0.15,0.85]，缺省不喂会输出爆炸（conservative-up3x 实测
        # ±1e2 量级），语义对齐上游 vsmlrt 的 conformance 开关
        self.affine = tuple(io["affine"]) if io.get("affine") else None
        # "basic"|"disable"：个别老导出模型（如 real-cugan）在 DML 默认扩展优化下
        # 算子融合会触发 DML 自定义算子崩溃，降级图优化即可稳定运行
        self.graph_opt = io.get("graph_opt", "all")
        self.device = device
        self.tile = tile
        self.tile_overlap = tile_overlap
        self.batch = max(1, batch)
        # manifest u8_wrap=false 仅禁 DML 主链的包装（CUGAN×DML 双会话互踩前科）；
        # TRT 主链的包装 A/B 已验证健康（14.9fps 端到端），不受此限。
        self._spec_allow_wrap = manifest_allow_wrap or device == "trt"
        self.u8_wrap_enabled = u8_wrap and self._spec_allow_wrap
        # 包装图只内嵌 color/range 前后处理，不含仿射——affine 模型跳过包装
        #（cugan-pro 系本就 manifest 禁包装，此处兜底未来带仿射的新模型）
        if self.affine:
            self.u8_wrap_enabled = False
        self.validate_hw = validate_hw
        self.session = None
        self.provider_used: list[str] = []
        self.fixed_hw: tuple[int, int] | None = None
        self.max_batch: int = 0  # 0 = 动态 batch；>0 = 导出时固定
        self._in_name = None
        self._out_names = None
        self._in_fp16 = False  # 模型本体即 fp16 导出（如 AnimeJaNai 系）：喂 fp16 输入
        self._u8_sess = None  # 包装 session（uint8 HWC 直进直出）
        self._u8_in_name = None
        self._u8_sess_try = None  # 校验失败的包装 session 引用（防 DML GC 析构崩设备）
        self.u8_wrapped = False
        # CUGAN×DML 特异：会话首个 run 若发生在非进程主线程 → GPU 栈死锁
        # （2026-08-27 排障：预热/COM MTA/会话选项/主线程先行全部无效，
        # CPU EP 与其他模型不受影响；证据链见 BENCH.md §13）。
        # True 时 StreamPipeline 改在事件循环线程（进程主线程）内联推理。
        self.main_thread_only = False

    def load(self) -> None:
        import onnxruntime as ort

        from .nvidia_dlls import register_nvidia_dlls

        register_nvidia_dlls()  # CUDA 版 ORT 的 DLL 在 pip 包内，需先挂载
        available = set(ort.get_available_providers())
        if self.device == "cpu":
            chosen: list[str] = ["CPUExecutionProvider"]
        elif self.device == "trt":
            chosen = [p for p in _TRT_CHAIN if p in available] or ["CPUExecutionProvider"]
        else:
            chosen = [p for p in _PROVIDER_ORDER if p in available] or ["CPUExecutionProvider"]
        so = self._session_options()
        self.session = self._try_session(ort, so, chosen)
        self.provider_used = self.session.get_providers()
        inp = self.session.get_inputs()[0]
        self._in_name = inp.name
        self._out_names = [o.name for o in self.session.get_outputs()]
        self._in_dtype = inp.type  # 'tensor(float)' 等
        self._in_fp16 = inp.type == "tensor(float16)"
        # 固定输入尺寸的导出版本（如 [1,3,64,64]）：推理前补边、推理后裁剪
        shape = inp.shape
        if len(shape) == 4 and isinstance(shape[2], int) and isinstance(shape[3], int):
            self.fixed_hw = (shape[2], shape[3])
        else:
            self.fixed_hw = None
        # batch 轴：动态(字符串)可批处理；固定 int 则只能按该值
        if len(shape) >= 1 and isinstance(shape[0], int):
            self.max_batch = shape[0]
            self.batch = min(self.batch, self.max_batch) if self.max_batch > 0 else 1
        else:
            self.max_batch = 0
        if self.fixed_hw is not None or self.tile:
            self.batch = 1  # 固定尺寸/分块路径不批帧（tile 批处理为后续项）
        # CUGAN×DML 围栏（证据链 BENCH.md §13）：该权重族在 DML 下存在逐帧
        # 显存泄漏（实测 1080p BASIC≈8 帧/ALL≈30 帧即 0x887A0006 设备摘除，
        # 与线程/包装/内容无关），且 CPU 与 DML 输出语义级分歧（maxdiff~0.76）
        # 使任何跨 EP 校验不成立 → 禁包装保持单会话 + 主线程内联执行。
        # TRT 主链验证健康（14.9fps 端到端），不受此限。
        is_cugan = "cugan" in Path(self.model_path).parent.name.lower()
        on_dml = "DmlExecutionProvider" in self.provider_used
        if is_cugan and on_dml:
            self.u8_wrap_enabled = False
        self.main_thread_only = is_cugan and on_dml
        if self.u8_wrap_enabled:
            self._try_u8_wrap()

    # ---- uint8 直进直出图手术（前后处理全在 GPU，推理 3.8x，见 u8_wrap.py）----

    def _try_u8_wrap(self) -> None:
        """惰性生成包装模型并做逐位校验；任何失败静默回退原路径。

        TRT 同样包装（实测 TRT+包装是最佳组合：推理 13.9ms vs 原路径 114ms，
        见 BENCH.md）——TRT 引擎按图各自构建/缓存，代价只是首任务多一次编译。
        """
        if not self.u8_wrap_enabled:
            return
        if self.tile or self.fixed_hw is not None or self.batch > 1:
            return
        if self.session is None or len(self.session.get_outputs()) != 1:
            return
        try:
            self._setup_u8()
        except Exception as e:  # noqa: BLE001 — 优化项，失败必须回退而不是带崩任务
            print(f"[engine] GPU 前后处理优化不可用，使用标准路径: {type(e).__name__}: {e}")
            # 校验失败的包装 session 必须保引用到引擎销毁，不能交给 GC：
            # DML 下"创建后再析构"第二个会话会损坏设备状态，主 session 之后
            # 首帧推理直接原生崩（AnimeJaNai V3.1 Sharp 实证，2026-08-31）；
            # 存活的双会话共存则健康（其余 3 个 V3.1 权重全过）。
            self.u8_wrapped = False

    def _setup_u8(self) -> None:
        import onnxruntime as ort

        from ..paths import TEMP_DIR
        from .u8_wrap import wrap_u8

        cache_dir = TEMP_DIR / "u8_wrap"
        cache = cache_dir / f"{self.model_path.stem}_u8.onnx"
        # 源模型重新下载/更新后（同名新文件）包装缓存作废重建
        if (not cache.exists()
                or cache.stat().st_mtime < self.model_path.stat().st_mtime):
            wrap_u8(self.model_path, cache,
                    color=self.color, range_01=self.value_range == "0-1")
        provider = self.provider_used[0] if self.provider_used else "x"
        marker = cache_dir / f"{self.model_path.stem}_u8.ok.{provider}"
        so = ort.SessionOptions()
        # BASIC：全量优化会重排被输出边界保护的 Cast/Clip 触发 DML 崩溃（u8_wrap.py 头注）
        so.graph_optimization_level = (
            ort.GraphOptimizationLevel.ORT_DISABLE_ALL if self.graph_opt == "disable"
            else ort.GraphOptimizationLevel.ORT_ENABLE_BASIC
        )
        # 复用主 session 的 provider 链构造（含 TRT 引擎缓存选项与逐级回退）
        available = set(ort.get_available_providers())
        if self.device == "trt":
            chosen: list = [p for p in _TRT_CHAIN if p in available] or ["CPUExecutionProvider"]
        elif self.device == "cpu":
            chosen = ["CPUExecutionProvider"]
        else:
            chosen = [p for p in _PROVIDER_ORDER if p in available] or ["CPUExecutionProvider"]
        sess = self._try_session(ort, so, chosen, model_path=cache)
        self._u8_sess_try = sess  # 先挂实例：校验失败也不能被 GC（见 _try_u8_wrap 兜底注释）
        in_name = sess.get_inputs()[0].name
        if not marker.exists():  # 每模型×每后端一次 A/B 逐位校验，通过后落标记
            self._validate_u8(sess, in_name)
            marker.touch()
        self._u8_sess = sess
        self._u8_in_name = in_name
        self.u8_wrapped = True
        print(f"[engine] GPU 前后处理优化已启用: {cache.name}")

    def _validate_u8(self, sess, in_name: str) -> None:
        """包装前后输出对比（容差按后端分档，见下；覆盖偶/奇尺寸与 pad 路径）。

        测试形状必须用源帧真实尺寸（validate_hw）：GPU 会话跑过 96x128 这类
        小形状后，真实尺寸的执行路径被不可逆拖慢（v0.2.3 结论，实测 +50%）。
        未提供时（临时 session / CPU）退回小形状。

        对照沿用主链 provider（同链双会话）：CUGAN 已由 manifest 禁包装、
        不再进入本校验，其余模型同链对照历史稳定（数十模型×后端全过）。
        """
        h, w = self.validate_hw if self.validate_hw else (96, 128)
        # 对齐尺寸时取 -1 变体，覆盖 pad 补边分支；已非对齐则本体即覆盖
        oh = h - 1 if h % self.pad == 0 and h > self.pad else h
        ow = w - 1 if w % self.pad == 0 and w > self.pad else w
        # 容差按后端分档：DML/主链两路径同源但图拓扑不同，fp16 卷积舍入各自
        # 累积，历史全绿 ≤1、AnimeJaNai V3.1 Sharp 实测踩到 2（2026-08-31）——
        # DML 放宽到 ≤2；TRT 下包装跳过模型尾部 f32→f16 输出量化 + EP 融合
        # 舍入不同，常态 1~3/255 且方向无害（更贴近 fp32 真值），放宽到 ≤3。
        # 真错误（通道序/尺寸/数值约定错）都是两位数量级，该门依旧拦截。
        tol = 3 if (self.provider_used[:1] or [""])[0].startswith("Tensorrt") else 2
        rng = np.random.default_rng(7)
        for f in (np.zeros((h, w, 3), np.uint8),
                  rng.integers(0, 256, (h, w, 3), dtype=np.uint8),
                  rng.integers(0, 256, (oh, ow, 3), dtype=np.uint8)):
            a = self._infer(f)
            b = self._run_u8(sess, in_name, f)
            diff = int(np.abs(a.astype(np.int16) - b.astype(np.int16)).max())
            if a.shape != b.shape or diff > tol:
                raise ValueError(f"u8 包装输出不一致 shape {a.shape}/{b.shape} "
                                 f"maxdiff={diff} (tol={tol})")

    def _run_u8(self, sess, in_name: str, frame: np.ndarray) -> np.ndarray:
        """uint8 HWC 直进直出；pad 在 uint8 输入侧做（廉价），输出侧裁剪。"""
        h, w = frame.shape[:2]
        ph = (self.pad - h % self.pad) % self.pad
        pw = (self.pad - w % self.pad) % self.pad
        x = frame
        if ph or pw:
            x = np.pad(x, ((0, ph), (0, pw), (0, 0)), mode="edge")
        y = sess.run(None, {in_name: x[None]})[0][0]
        if ph or pw:
            y = np.ascontiguousarray(y[: h * self.scale, : w * self.scale])
        return y

    def process(self, frame: np.ndarray) -> np.ndarray:
        if self._u8_sess is not None:
            return self._run_u8(self._u8_sess, self._u8_in_name, frame)
        return super().process(frame)

    def _session_options(self):
        import onnxruntime as ort

        so = ort.SessionOptions()
        level = {
            "basic": ort.GraphOptimizationLevel.ORT_ENABLE_BASIC,
            "disable": ort.GraphOptimizationLevel.ORT_DISABLE_ALL,
        }.get(self.graph_opt)
        if level is not None:
            so.graph_optimization_level = level
        return so

    def _try_session(self, ort, so, chosen: list[str], model_path: Path | None = None):
        """按 provider 链建 session，逐层回退：TRT 失败去 TRT 重试，GPU 失败回落 CPU。"""
        model = str(model_path or self.model_path)
        while True:
            providers: list = chosen
            if "TensorrtExecutionProvider" in chosen:
                providers = _trt_provider_options() + [
                    p for p in chosen if p != "TensorrtExecutionProvider"]
            try:
                return ort.InferenceSession(model, so, providers=providers)
            except Exception as e:  # noqa: BLE001
                if "TensorrtExecutionProvider" in chosen:
                    rest = [p for p in chosen if p != "TensorrtExecutionProvider"]
                    print(f"[engine] TensorRT 初始化失败（{e}），已回退至 {'/'.join(rest) or 'CPU'}")
                    chosen = rest
                    continue
                if self.device == "cpu" or not (set(chosen) - {"CPUExecutionProvider"}):
                    raise
                print(f"[engine] {chosen} 初始化失败（{e}），已回退至 CPU")
                chosen = ["CPUExecutionProvider"]

    def _infer(self, frame: np.ndarray) -> np.ndarray:
        h, w = frame.shape[:2]
        if self.fixed_hw is not None:
            fh, fw = self.fixed_hw
            if h > fh or w > fw:
                raise ValueError(
                    f"输入 {w}x{h} 超过固定尺寸模型上限 {fw}x{fh}，应启用更小的 tile"
                )
            ph, pw = fh - h, fw - w
        else:
            ph = (self.pad - h % self.pad) % self.pad
            pw = (self.pad - w % self.pad) % self.pad
        x = frame
        if ph or pw:
            x = np.pad(x, ((0, ph), (0, pw), (0, 0)), mode="edge")
        if self.color == "bgr":
            x = x[..., ::-1]
        x = np.ascontiguousarray(x.transpose(2, 0, 1)[None].astype(np.float32))
        if self.value_range == "0-1":
            x = x / 255.0
        if self.affine:
            a, b = self.affine
            x = x * a + b
        if self._in_fp16:
            x = x.astype(np.float16)

        y = self.session.run(self._out_names, {self._in_name: x})[0]
        y = np.squeeze(y, axis=0).transpose(1, 2, 0).astype(np.float32)  # CHW -> HWC
        if self.affine:
            a, b = self.affine
            y = (y - b) / a
        if self.value_range == "0-1":
            y = y * 255.0
        # 截断（非 round）是刻意的：与 u8 包装图内 Cast 的量化方式保持一致，
        # 两路径才能通过 ≤1/255 的逐位 A/B 校验。torch_engine 用 round 带来的
        # ≤1/255 系统性差异经 2026-08-26 审查拍板接受，不统一（不可感知）。
        y = np.clip(y, 0, 255).astype(np.uint8)
        if self.color == "bgr":
            y = y[..., ::-1]
        if ph or pw:
            y = y[: h * self.scale, : w * self.scale]
        return np.ascontiguousarray(y)

    def process_batch(self, frames: np.ndarray) -> np.ndarray:
        """[N,H,W,3] -> [N,H*s,W*s,3]，动态尺寸模型单次 run 批量推理。"""
        if (
            self.batch <= 1
            or self.session is None
            or self.fixed_hw is not None
            or self.tile
        ):
            return super().process_batch(frames)
        n, h, w, _ = frames.shape
        ph = (self.pad - h % self.pad) % self.pad
        pw = (self.pad - w % self.pad) % self.pad
        x = frames
        if ph or pw:
            x = np.pad(x, ((0, 0), (0, ph), (0, pw), (0, 0)), mode="edge")
        if self.color == "bgr":
            x = x[..., ::-1]
        x = np.ascontiguousarray(x.transpose(0, 3, 1, 2).astype(np.float32))  # NCHW
        if self.value_range == "0-1":
            x = x / 255.0
        if self.affine:
            a, b = self.affine
            x = x * a + b
        if self._in_fp16:
            x = x.astype(np.float16)

        y = self.session.run(self._out_names, {self._in_name: x})[0]  # N,3,H',W'
        if self.affine:
            a, b = self.affine
            y = (y - b) / a
        if self.value_range == "0-1":
            y = y * 255.0
        y = np.clip(y.astype(np.float32), 0, 255).astype(np.uint8).transpose(0, 2, 3, 1)  # NHWC
        if self.color == "bgr":
            y = y[..., ::-1]
        if ph or pw:
            y = y[:, : h * self.scale, : w * self.scale]
        return np.ascontiguousarray(y)
