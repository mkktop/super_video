"""ONNX 超分引擎（Real-ESRGAN / animevideov3 等模型族）。

输入约定由 manifest 的 io 字段描述：
  color: "bgr" | "rgb"（模型训练时的通道序）
  range: "0-255"（float32，不归一化——Real-ESRGAN 系约定）
pad: 输入边长需对齐的最小倍数（pixelshuffle 需要，通常 = scale）
"""
from __future__ import annotations

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
        u8_wrap: bool = True,
        validate_hw: tuple[int, int] | None = None,  # 源帧 (H,W)：u8 校验形状
    ):
        self.model_path = Path(model_path)
        self.scale = scale
        io = io or {}
        self.color = io.get("color", "bgr")
        self.value_range = io.get("range", "0-255")
        self.pad = int(io.get("pad", scale))
        # "basic"|"disable"：个别老导出模型（如 real-cugan）在 DML 默认扩展优化下
        # 算子融合会触发 DML 自定义算子崩溃，降级图优化即可稳定运行
        self.graph_opt = io.get("graph_opt", "all")
        self.device = device
        self.tile = tile
        self.tile_overlap = tile_overlap
        self.batch = max(1, batch)
        self.u8_wrap_enabled = u8_wrap  # uint8 直进直出图手术（BENCH.md 2026-08-25）
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
        self.u8_wrapped = False

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
            self._u8_sess = None
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
        in_name = sess.get_inputs()[0].name
        if not marker.exists():  # 每模型×每后端一次 A/B 逐位校验，通过后落标记
            self._validate_u8(sess, in_name)
            marker.touch()
        self._u8_sess = sess
        self._u8_in_name = in_name
        self.u8_wrapped = True
        print(f"[engine] GPU 前后处理优化已启用: {cache.name}")

    def _validate_u8(self, sess, in_name: str) -> None:
        """包装前后输出逐位对比（≤1/255 容差，覆盖偶/奇尺寸与 pad 路径）。

        测试形状必须用源帧真实尺寸（validate_hw）：GPU 会话跑过 96x128 这类
        小形状后，真实尺寸的执行路径被不可逆拖慢（v0.2.3 结论，实测 +50%）。
        未提供时（临时 session / CPU）退回小形状。
        """
        h, w = self.validate_hw if self.validate_hw else (96, 128)
        # 对齐尺寸时取 -1 变体，覆盖 pad 补边分支；已非对齐则本体即覆盖
        oh = h - 1 if h % self.pad == 0 and h > self.pad else h
        ow = w - 1 if w % self.pad == 0 and w > self.pad else w
        rng = np.random.default_rng(7)
        for f in (np.zeros((h, w, 3), np.uint8),
                  rng.integers(0, 256, (h, w, 3), dtype=np.uint8),
                  rng.integers(0, 256, (oh, ow, 3), dtype=np.uint8)):
            a = self._infer(f)
            b = self._run_u8(sess, in_name, f)
            diff = int(np.abs(a.astype(np.int16) - b.astype(np.int16)).max())
            if a.shape != b.shape or diff > 1:
                raise ValueError(f"u8 包装输出不一致 shape {a.shape}/{b.shape} maxdiff={diff}")

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
        if self._in_fp16:
            x = x.astype(np.float16)

        y = self.session.run(self._out_names, {self._in_name: x})[0]
        y = np.squeeze(y, axis=0).transpose(1, 2, 0).astype(np.float32)  # CHW -> HWC
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
        if self._in_fp16:
            x = x.astype(np.float16)

        y = self.session.run(self._out_names, {self._in_name: x})[0]  # N,3,H',W'
        if self.value_range == "0-1":
            y = y * 255.0
        y = np.clip(y.astype(np.float32), 0, 255).astype(np.uint8).transpose(0, 2, 3, 1)  # NHWC
        if self.color == "bgr":
            y = y[..., ::-1]
        if ph or pw:
            y = y[:, : h * self.scale, : w * self.scale]
        return np.ascontiguousarray(y)
