"""GPU 直连流式管线：切段 MKV → NVDEC → 图内转换+SR（TRT/CUDA）→ NVENC 段 MP4。

构造签名与 StreamPipeline 兼容（SegmentedPipeline 经 pipeline_factory 注入），
不支持的能力（补帧/字幕烧录/水印/预滤镜/target 缩放/tile/批处理）由 worker
门控排除——这里对不支持参数直接拒绝，防止静默走样。

段产出契约与 StreamPipeline 对齐：stage_stats["frames"] 帧数、干净 EOF 短产
置 decode_eof、进度/预览回调同款节流。帧数据全程显存（D2D 进 ORT IOBinding、
输出平面指针直喂 NVENC），宿主只见过压缩码流与预览 JPEG。
"""
from __future__ import annotations

import asyncio
import time
from pathlib import Path

import numpy as np

from ..engines.gpu_graph import np_nv12_to_rgb, np_yuv_to_rgb
from .gpu_codec import GpuSegmentDecoder, GpuSegmentEncoder, cut_segment, ensure_ctx
from .probe import MediaInfo
from .stream import EncodeOpts, PipelineError, RunStats, TaskCanceled

PREVIEW_INTERVAL_S = 5.0
PROGRESS_INTERVAL_S = 0.5

# GPU 直连链入口条件（全部满足才启用；不满足走标准管线并记一条日志）
_GPU_SRC_CODECS = {"h264", "hevc", "av1"}
_GPU_CODECS = {"h264_nvenc", "hevc_nvenc"}


def gpu_chain_gate(*, setting: bool, spec_engine: str, interp, out_kind: str,
                   codec: str, decode_vf, burn_subtitle, watermark,
                   target_size, tile: int, batch: int, src_codec: str,
                   bit_depth: int, width: int, height: int,
                   io: dict | None) -> tuple[bool, str]:
    """纯逻辑判定（可 CPU 单测）。worker 调用；导入可用性由 GpuInfer 构造兜底。"""
    io = io or {}
    if setting is False:
        return False, "设置已关闭 gpu_zero_copy"
    if spec_engine != "onnx":
        return False, f"引擎 {spec_engine} 非直连对象"
    if interp is not None:
        return False, "补帧任务不支持"
    if out_kind != "video":
        return False, "图片序列输出不支持"
    if codec not in _GPU_CODECS:
        return False, f"编码 {codec} 非 h264/hevc_nvenc"
    if decode_vf:
        return False, "预处理滤镜（反交错/去色带）不支持"
    if burn_subtitle or watermark:
        return False, "字幕烧录/水印不支持"
    if target_size is not None:
        return False, "精确目标尺寸不支持"
    if tile:
        return False, "分块 tile 不支持"
    if batch != 1:
        return False, "批处理不支持"
    if src_codec not in _GPU_SRC_CODECS:
        return False, f"源编码 {src_codec} 不在 NVDEC 支持集"
    if bit_depth != 10:
        return False, f"{bit_depth}bit 源不支持（8bit NV12 表面布局异常，仅 10bit P016）"
    if width % 2 or height % 2:
        return False, f"奇数尺寸 {width}x{height} 不支持"
    if io.get("color") == "y" or io.get("affine") or io.get("fixed_hw"):
        return False, "模型 IO 约定不兼容（Y 通道/仿射/定长）"
    return True, ""


class GpuInfer:
    """wrapped 图会话：NV12/P016 平面 → 模型 → NV12 平面（IOBinding 全显存）。

    一个任务一个实例（TRT 引擎缓存跨任务复用）；双路并行各自实例。
    """

    def __init__(self, weight: Path, h: int, w: int, bit_depth: int, scale: int = 2):
        import onnxruntime as ort

        from ..engines.gpu_graph import ensure_wrapped
        from ..engines.nvidia_dlls import register_nvidia_dlls

        if bit_depth != 10:
            # 8bit NV12 表面的 riffle 在字节粒度（u8），图内反洗牌按 u16 槽
            # 实现——暂不支持；10bit P016（u16 槽）已多文件位级验证
            raise PipelineError(
                f"GPU 直连链当前仅支持 10bit 源（收到 {bit_depth}bit），8bit 请使用标准管线")
        ensure_ctx()
        register_nvidia_dlls()
        from ..engines.onnx_engine import _trt_provider_options

        path = ensure_wrapped(Path(weight), h, w, bit_depth, "709", scale)
        providers: list = _trt_provider_options(True)  # 返回 [(TRT, opts)] 形态
        providers += ["CUDAExecutionProvider", "CPUExecutionProvider"]
        self.session = ort.InferenceSession(str(path), providers=providers)
        self.h, self.w, self.scale, self.bit_depth = h, w, scale, bit_depth
        self.h2, self.w2 = h * scale, w * scale
        np_dt = np.uint16 if bit_depth == 10 else np.uint8
        self._elem = np_dt().itemsize
        self.ov_y = ort.OrtValue.ortvalue_from_shape_and_type(
            [1, h, w], np_dt, "cuda", 0)
        self.ov_uv = ort.OrtValue.ortvalue_from_shape_and_type(
            [1, h // 2, w // 2, 2], np_dt, "cuda", 0)
        self.ov_y_out = ort.OrtValue.ortvalue_from_shape_and_type(
            [1, 1, self.h2, self.w2], np.uint8, "cuda", 0)
        self.ov_uv_out = ort.OrtValue.ortvalue_from_shape_and_type(
            [1, self.h2 // 2, self.w2 // 2, 2], np.uint8, "cuda", 0)
        self.bind = self.session.io_binding()
        self.bind.bind_ortvalue_input("y_in", self.ov_y)
        self.bind.bind_ortvalue_input("uv_in", self.ov_uv)
        self.bind.bind_ortvalue_output("out_y", self.ov_y_out)
        self.bind.bind_ortvalue_output("out_uv", self.ov_uv_out)
        self.provider_used = self.session.get_providers()

    def run(self, y_cai, uv_cai) -> tuple[int, int]:
        """平面 CAI → D2D 进绑定输入 → 推理；返回输出双平面设备指针。"""
        from cuda.bindings import runtime as cudart

        pitch = self.w * self._elem
        yv = y_cai.__cuda_array_interface__
        uvv = uv_cai.__cuda_array_interface__
        cudart.cudaMemcpy2DAsync(
            self.ov_y.data_ptr(), pitch, yv["data"][0], yv["strides"][0] * self._elem,
            pitch, self.h, cudart.cudaMemcpyKind.cudaMemcpyDeviceToDevice, 0)
        cudart.cudaMemcpy2DAsync(
            self.ov_uv.data_ptr(), pitch, uvv["data"][0],
            uvv["strides"][0] * self._elem,
            pitch, self.h // 2, cudart.cudaMemcpyKind.cudaMemcpyDeviceToDevice, 0)
        cudart.cudaStreamSynchronize(0)
        self.session.run_with_iobinding(self.bind)
        return self.ov_y_out.data_ptr(), self.ov_uv_out.data_ptr()

    def host_nv12(self) -> np.ndarray:
        """输出帧一次性 D2H（预览/校验用，非主链路）。"""
        from cuda.bindings import runtime as cudart

        y = self.ov_y_out.numpy()[0, 0]
        uv = self.ov_uv_out.numpy()[0]
        nv = np.zeros((self.h2 * 3 // 2, self.w2), np.uint8)
        nv[:self.h2] = y
        nv[self.h2:] = uv.reshape(self.h2 // 2, self.w2)
        return nv

    def host_src_rgb(self, y_cai, uv_cai) -> np.ndarray:
        """输入帧一次性 D2H 转 RGB（源预览）。"""
        from cuda.bindings import runtime as cudart

        yv = y_cai.__cuda_array_interface__
        uvv = uv_cai.__cuda_array_interface__
        yb = np.empty(self.h * self.w * self._elem, np.uint8)
        ub = np.empty(self.h // 2 * self.w * self._elem, np.uint8)
        cudart.cudaMemcpy(yb.ctypes.data, yv["data"][0], yb.nbytes,
                          cudart.cudaMemcpyKind.cudaMemcpyDeviceToHost)
        cudart.cudaMemcpy(ub.ctypes.data, uvv["data"][0], ub.nbytes,
                          cudart.cudaMemcpyKind.cudaMemcpyDeviceToHost)
        dt = np.uint16 if self.bit_depth == 10 else np.uint8
        y = np.frombuffer(yb, dt).reshape(self.h, self.w)
        uv = np.frombuffer(ub, dt).reshape(self.h // 2, self.w // 2, 2)
        return np_yuv_to_rgb(y, uv, self.bit_depth)


def _save_jpg(arr: np.ndarray, path: Path) -> None:
    from PIL import Image

    img = Image.fromarray(arr)
    img.thumbnail((960, 960))
    tmp = path.with_suffix(".tmp.jpg")
    img.save(tmp, "JPEG", quality=88)
    tmp.replace(path)


def _parse_fps(fps_str: str) -> tuple[int, int]:
    try:
        if "/" in fps_str:
            n, d = fps_str.split("/")
            return int(n), int(d)
        return int(float(fps_str) * 1000 + 0.5), 1000
    except ValueError:
        return 24000, 1000


class GpuStreamPipeline:
    """单段 GPU 直连管线（SegmentedPipeline 的 pipeline_factory 注入形态）。"""

    def __init__(self, info: MediaInfo, out_path: Path, tx, enc: EncodeOpts, *,
                 progress_cb=None, cancel_event=None, preview_path=None,
                 src_preview_path=None, target_scale=None, target_size=None,
                 interp=None, seek_s=None, max_frames=None, with_audio=True,
                 seg_start=0, seg_total=0, frame_start=1, decode_hwaccel=None,
                 decode_vf=None, gpu_infer: GpuInfer | None = None):
        # 与 StreamPipeline 同签名；GPU 链不支持的能力在工厂层门控，
        # 此处防御性拒绝（防未来调用点漏判静默走样）
        for name, val in (("interp", interp), ("target_size", target_size),
                          ("decode_vf", decode_vf)):
            if val not in (None, False, ""):
                raise PipelineError(f"GPU 直连管线不支持 {name}={val!r}")
        if enc.burn_subtitle or enc.watermark:
            raise PipelineError("GPU 直连管线不支持字幕烧录/水印")
        if gpu_infer is None:
            raise PipelineError("gpu_infer 未提供")
        if enc.codec not in ("h264_nvenc", "hevc_nvenc", "av1_nvenc"):
            raise PipelineError(f"GPU 直连管线仅支持 NVENC 编码族，收到 {enc.codec}")
        self.info, self.out_path, self.enc = info, Path(out_path), enc
        self.progress_cb = progress_cb
        self.cancel_event = cancel_event
        self.preview_path = Path(preview_path) if preview_path else None
        self.src_preview_path = Path(src_preview_path) if src_preview_path else None
        self.seek_s = seek_s or 0.0
        self.max_frames = max_frames
        self.seg_start, self.seg_total = seg_start, (seg_total or 0)
        self.gi = gpu_infer
        self.stage_stats: dict[str, float] = {}
        self._cut: Path | None = None

    async def run(self) -> RunStats:
        loop = asyncio.get_running_loop()
        info, gi, enc = self.info, self.gi, self.enc
        if (info.width, info.height) != (gi.w, gi.h):
            raise PipelineError(
                f"gpu_infer 尺寸 {gi.w}x{gi.h} 与源 {info.width}x{info.height} 不符")
        fps_n, fps_d = _parse_fps(info.fps_str)
        self.stage_stats = {}
        t0 = time.perf_counter()
        last_prog = 0.0
        last_prev = 0.0
        frames = 0
        decode_eof = False

        self._cut = self.out_path.with_suffix(".cut.mkv")
        await loop.run_in_executor(None, cut_segment, info.path, self._cut,
                                   self.seek_s)
        try:
            dec = GpuSegmentDecoder(self._cut)
            encoder = GpuSegmentEncoder(self.out_path, gi.w2, gi.h2, fps_n, fps_d,
                                        enc.codec, cq=enc.crf)
            try:
                for f, yv, uvv, pts in dec.frames(self.seek_s * 1000.0,
                                                  self.max_frames or 10 ** 9):
                    if self.cancel_event is not None and self.cancel_event.is_set():
                        raise TaskCanceled()
                    t = time.perf_counter()
                    dec.sync()
                    y_ptr, uv_ptr = gi.run(yv, uvv)
                    self.stage_stats["infer"] = \
                        self.stage_stats.get("infer", 0.0) + time.perf_counter() - t
                    t = time.perf_counter()
                    encoder.encode(y_ptr, uv_ptr)
                    self.stage_stats["write"] = \
                        self.stage_stats.get("write", 0.0) + time.perf_counter() - t
                    frames += 1
                    now = time.perf_counter()
                    if (self.preview_path is not None
                            and (frames == 1 or now - last_prev >= PREVIEW_INTERVAL_S)):
                        last_prev = now
                        self._previews(yv, uvv)
                    if (self.progress_cb is not None and self.seg_total
                            and (now - last_prog >= PROGRESS_INTERVAL_S
                                 or self._is_last(frames))):
                        last_prog = now
                        el = now - t0
                        fps = frames / el if el > 0 else 0.0
                        total = self.seg_total
                        self.progress_cb(self.seg_start + frames, total, fps,
                                         (total - self.seg_start - frames) / fps
                                         if fps > 0 else 0.0)
                requested = self.max_frames or frames
                if frames < requested:
                    # 干净 EOF（生成器自然结束而非被 max_frames 截断）
                    decode_eof = True
                if self.progress_cb is not None and self.seg_total:
                    el = time.perf_counter() - t0
                    fps = frames / el if el > 0 else 0.0
                    self.progress_cb(self.seg_start + frames, self.seg_total, fps, 0.0)
            finally:
                encoder.close()
                dec.close()
        finally:
            # 解码帧对象经引用链延迟释放文件句柄（Windows 锁）：重试删除切段
            import gc as _gc

            _gc.collect()
            if self._cut is not None and self._cut.exists():
                for _ in range(10):
                    try:
                        self._cut.unlink()
                        break
                    except PermissionError:
                        time.sleep(0.1)
                # 删不掉也无妨：段工作目录随任务清理
            self._cut = None
        wall = time.perf_counter() - t0
        self.stage_stats.update(frames=float(frames), wall=wall,
                                decode_eof=1.0 if decode_eof else 0.0)
        return RunStats(frames=frames, elapsed_s=wall,
                        fps=frames / wall if wall else 0.0, out_path=self.out_path,
                        out_bytes=self.out_path.stat().st_size
                        if self.out_path.exists() else 0)

    def _is_last(self, frames: int) -> bool:
        return self.max_frames is not None and frames >= self.max_frames

    def _previews(self, yv, uvv) -> None:
        try:
            if self.preview_path is not None:
                nv = self.gi.host_nv12()
                rgb = np_nv12_to_rgb(nv[:self.gi.h2],
                                     nv[self.gi.h2:].reshape(self.gi.h2 // 2,
                                                             self.gi.w2 // 2, 2))
                _save_jpg(rgb, self.preview_path)
            if self.src_preview_path is not None:
                _save_jpg(self.gi.host_src_rgb(yv, uvv), self.src_preview_path)
        except Exception:  # noqa: BLE001 — 预览失败不影响任务
            pass
