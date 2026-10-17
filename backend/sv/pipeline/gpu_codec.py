"""GPU 直连链的进程内编解码层（NVDEC/NVENC，帧全程驻显存）。

依赖 PyNvVideoCodec 2.x + cuda-bindings（仅 .venv-cuda / TRT 组件环境），
全部惰性导入——DML 主环境 import 本模块零成本。

分段定位：PyNvDemuxer.Seek 的实参语义实测是「包序号」而非时间戳（校准
Seek(1000)→41s，24.4 包/秒 全流计数），不可靠；改用 ffmpeg 切段级 MKV
（-ss 输入寻道 + -c:v copy + -copyts，无重编码、保留原始毫秒 pts），
解码侧按 pts 丢弃段起点之前的帧（CFR 精确，VFR 与现有管线同容差）。

关键坑（尖刺实测）：
- CreateDecoder 默认 usedevicememory=False（宿主内存帧）——必须显式 True；
- 解码表面有池化复用：帧数据须当帧消费（D2D/D2H 即时执行）；
- NVENC 设备输入协议：Encode(obj.cuda() → [CAIMemoryView(Y), (UV)])，
  且 Y/UV 必须同块连续显存（"u plane must follow yplane"）——
  由 pack 缓冲 + 两笔 D2D 组装；
- 调用线程须持有 current CUDA context（primary retain + set current）。
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from ..paths import ffmpeg_bin
from ..utils.process import WINDOWS_CREATE_FLAGS
from .probe import MediaInfo

# 源编码 → NVDEC codec 名 / ffmpeg copy 输出格式（切段 MKV 保时间戳）
NVDEC_CODECS = {"h264": "h264", "hevc": "hevc", "av1": "av1"}

_CTX = {"ready": False, "handle": 0}


def ensure_ctx() -> int:
    """主线程挂主 CUDA 上下文（幂等），返回上下文句柄。

    关键：NVDEC/NVENC/ORT 必须同上下文——跨上下文的设备指针 D2D 会
    静默拷贝垃圾（尖刺实测：解码器私有上下文的表面经主上下文 memcpy
    得到无效数据，不报错）。
    """
    if not _CTX["ready"]:
        from cuda.bindings import driver as cu

        cu.cuInit(0)
        _, ctx = cu.cuDevicePrimaryCtxRetain(0)
        cu.cuCtxSetCurrent(ctx)
        _CTX["handle"] = int(ctx)
        _CTX["ready"] = True
    return _CTX["handle"]


def cut_segment(src: Path, dst: Path, start_s: float) -> None:
    """切段级 MKV：输入寻道 + 流拷贝 + 原始时间戳（无重编码，秒级）。"""
    cmd = [
        ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
        "-ss", f"{start_s:.6f}", "-i", str(src),
        "-map", "0:v:0", "-an", "-sn", "-dn", "-c:v", "copy", "-copyts",
        "-f", "matroska", str(dst),
    ]
    r = subprocess.run(cmd, capture_output=True, text=True,
                       creationflags=WINDOWS_CREATE_FLAGS)
    if r.returncode != 0 or not dst.exists():
        raise RuntimeError(f"切段失败: {(r.stderr or '')[-300:]}")


class GpuSegmentDecoder:
    """一个分段一个实例：解封装 → NVDEC → 逐帧设备指针。

    帧生命周期：decode 循环内当帧消费（表面池会复用）。
    """

    def __init__(self, seg_mkv: Path):
        import PyNvVideoCodec as nvc

        ctx = ensure_ctx()
        self._nvc = nvc
        self.dm = nvc.CreateDemuxer(str(seg_mkv))
        # cudacontext 显式传主上下文：表面与 ORT/NVENC 同域，跨域 D2D 无效
        self.dc = nvc.CreateDecoder(gpuid=0, codec=self.dm.GetNvCodecId(),
                                    cudacontext=ctx, usedevicememory=True)
        # 注意 GetPixelFormat() 在首帧产出前返回默认 NV12，真实格式须查帧
        self._fmt_checked = False

    def frames(self, start_ms: float, n: int):
        """产出 (frame, y_cai, uv_cai, pts_ms) 共 n 个，或到干净 EOF。

        EOF 契约（实测）：Demux 到片尾不返回 None，而是无限返回 bsl=0 的
        空包——该空包喂给 Decode 即冲刷出解码器缓冲的尾帧（幂等，再喂得 0）。
        消费完少于 n 个 = 片尾（decode_eof 语义）。段起点之前的帧按 pts
        丢弃（copyts 原始毫秒）。
        """
        dm, dc = self.dm, self.dc
        out = 0
        while out < n:
            pkt = dm.Demux()
            eos = pkt is None or pkt.bsl == 0
            drained = dc.Decode(pkt) if pkt is not None else []
            for f in drained or []:
                if not self._fmt_checked:
                    self._fmt_checked = True
                    # 8bit NV12 表面在本机栈是「字节洗牌」布局（前半值在偶字节、
                    # 后半在奇字节，h264/hevc 双编码实证），非稳定契约——拒绝；
                    # 10bit P016 布局干净（BENCH 尖刺 104dB 位级验证）
                    if f.format.name != "P016":
                        raise RuntimeError(
                            f"NVDEC 表面布局非 P016（{f.format.name}），"
                            "GPU 直连链仅支持 10bit 源")
                pts = f.getPTS()
                if pts < start_ms - 0.5:
                    continue
                views = f.cuda()
                yield f, views[0], views[1], pts
                out += 1
                if out >= n:
                    return
            if eos:
                return

    def sync(self) -> None:
        """解码写完后再被其他流访问（D2D 前）。"""
        self.dc.SyncOnCUStream()

    def close(self) -> None:
        self.dc = None
        self.dm = None


# NVENC 编码族（任务 codec 参数 → PyNvVideoCodec codec 名 / ES demuxer）
NVENC_CODECS = {"h264_nvenc": ("h264", "h264"), "hevc_nvenc": ("hevc", "hevc")}


class GpuSegmentEncoder:
    """NVENC 段编码器：设备指针直进，裸 ES 落盘，close 时 ffmpeg remux 成段 mp4。

    为什么不用 FFmpegMuxer 直出 mp4：其 Finalize 对每段独立做 B 帧 DTS 补偿
    （负 DTS + edit list），多段 concat 后时间轴互相踩踏 → 解码帧数漂移/
    首帧重复（实测 48→50）。ES 无时间戳概念，remux 按 CFR 重建 0..N-1，
    段拼接干净；remux 为纯拷贝（毫秒级）。
    """

    def __init__(self, out_mp4: Path, w: int, h: int, fps_num: int, fps_den: int,
                 codec: str = "h264_nvenc", cq: int = 18):
        import PyNvVideoCodec as nvc
        from cuda.bindings import runtime as cudart

        ensure_ctx()
        entry = NVENC_CODECS.get(codec)
        if entry is None:
            raise RuntimeError(f"GPU 直连链不支持编码 {codec}（仅 h264/hevc_nvenc）")
        name, es_fmt = entry
        self._nvc = nvc
        self._cudart = cudart
        self.w, self.h = w, h
        # bf=0（IPP）：无 B 帧则解码序==显示序，裸 ES 复洗时间戳天然干净；
        # B 帧路径的 FFmpegMuxer/ES 复洗实测均产重复 pts（段拼接后解码
        # 帧数漂移 48→50）。constqp≈产品 nvenc 的 -cq 口径。
        self.enc = nvc.CreateEncoder(w, h, "NV12", False, codec=name, bf=0,
                                     constqp=cq, colorspace="bt709",
                                     repeatspspps=1)
        self.packed = int(cudart.cudaMalloc(w * h * 3 // 2)[1])
        self._views = [
            nvc.CAIMemoryView([h, w, 1], [w, 1, 1], "|u1", 0, self.packed, False),
            nvc.CAIMemoryView([h // 2, w // 2, 2], [w, 2, 1], "|u1", 0,
                              self.packed + w * h, False),
        ]

        class _Frame:
            def cuda(self_this):
                return self._views

        self._frame = _Frame()
        self.out_path = Path(out_mp4)
        if self.out_path.exists():
            self.out_path.unlink()
        self._es_path = self.out_path.with_suffix(".es")
        self._es = open(self._es_path, "wb")
        self._es.write(self.enc.GetSequenceParams())  # SPS/PPS（Annex-B）
        self._es_fmt = es_fmt
        self._fps = (fps_num, fps_den)
        self._i = 0

    def encode(self, y_ptr: int, uv_ptr: int) -> None:
        """ORT 输出双平面 → 打包 D2D → NVENC → ES 追加（零宿主像素拷贝）。"""
        cu = self._cudart
        w, h = self.w, self.h
        cu.cudaMemcpy2DAsync(self.packed, w, y_ptr, w, w, h,
                             cu.cudaMemcpyKind.cudaMemcpyDeviceToDevice, 0)
        cu.cudaMemcpy2DAsync(self.packed + w * h, w, uv_ptr, w, w, h // 2,
                             cu.cudaMemcpyKind.cudaMemcpyDeviceToDevice, 0)
        cu.cudaStreamSynchronize(0)
        for p in self.enc.Encode(self._frame):
            self._es.write(p["data"])
        self._i += 1

    def close(self) -> None:
        for p in self.enc.EndEncode():
            self._es.write(p["data"])
        self._es.close()
        self.enc = None
        # remux：ES 按 CFR 重建时间戳 → 干净段 mp4（无 edit list / 负 DTS）
        fps_n, fps_d = self._fps
        cmd = [
            ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
            "-f", self._es_fmt, "-framerate", f"{fps_n}/{fps_d}",
            "-i", str(self._es_path), "-map", "0:v:0",
            # 色彩标签：NVENC 的 bt709 limited 语义显式落盘——缺标签时播放器
            # 按 bt601 解码会整体色偏（直链产物 RGB 全域 709 正确、标签丢失曾被
            # 误判为色彩错误）
            "-colorspace", "bt709", "-color_primaries", "bt709",
            "-color_trc", "bt709", "-color_range", "tv",
            "-c:v", "copy", "-movflags", "+faststart", str(self.out_path),
        ]
        r = subprocess.run(cmd, capture_output=True, text=True,
                           creationflags=WINDOWS_CREATE_FLAGS)
        self._es_path.unlink(missing_ok=True)
        if r.returncode != 0:
            raise RuntimeError(f"段 remux 失败: {(r.stderr or '')[-300:]}")


def probe_bit_depth(info: MediaInfo) -> int:
    """源位深（GPU 链 8/10 二档；HDR/12bit 由既有 probe 拒绝）。"""
    return info.bit_depth
