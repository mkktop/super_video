"""编码输出选项（项5）：新编码器 / 容器 / 音频编码 / 字幕轨保留 的参数层与校验层测试。

校验层直接调用路由函数（不起 TestClient/runner）：避免与 test_server 的
runner 共享 SV_DB 产生双消费者竞争；任务行即建即删。
"""
import os
import subprocess
from pathlib import Path

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from sv.paths import TEMP_DIR, ffmpeg_bin, ffprobe_bin
from sv.pipeline.stream import (
    EncodeOpts,
    audio_args,
    encoder_cmd,
    subtitle_args,
    video_codec_args,
)
from sv.utils.process import WINDOWS_CREATE_FLAGS


@pytest.fixture(scope="module", autouse=True)
def own_db(tmp_path_factory):
    """独立库并在运行期生效：直调路由函数不经过 lifespan，
    必须自建 schema；env 只在测试期改写、退出即还原，
    不受其他模块 collection 期改写 SV_DB 的影响，也不影响它们。"""
    old = os.environ.get("SV_DB")
    os.environ["SV_DB"] = str(tmp_path_factory.mktemp("encode_opts") / "t.db")
    from sv.server import db

    db.init_db()
    yield
    if old is None:
        os.environ.pop("SV_DB", None)
    else:
        os.environ["SV_DB"] = old


# ---- 参数层 ----

def test_video_codec_args_families():
    # NVENC 家族：p5 + vbr + cq；h264/hevc 带 hq tune，av1 不带
    nv = video_codec_args(EncodeOpts(codec="hevc_nvenc", crf=20))
    assert nv[nv.index("-c:v") + 1] == "hevc_nvenc"
    assert "-tune" in nv
    av1 = video_codec_args(EncodeOpts(codec="av1_nvenc", crf=20))
    assert av1[av1.index("-c:v") + 1] == "av1_nvenc"
    assert "-tune" not in av1
    # AMF：cqp 近似（qp_i=crf, qp_p=crf+2）
    amf = video_codec_args(EncodeOpts(codec="h264_amf", crf=18))
    assert amf[amf.index("-rc") + 1] == "cqp"
    assert amf[amf.index("-qp_i") + 1] == "18"
    assert amf[amf.index("-qp_p") + 1] == "20"
    # SVT-AV1：CRF 量程 0-63 等比映射（18 -> 22），preset 名转数字
    svt = video_codec_args(EncodeOpts(codec="av1_svt", crf=18, preset="medium"))
    assert svt[svt.index("-c:v") + 1] == "libsvtav1"
    assert svt[svt.index("-crf") + 1] == "22"
    assert svt[svt.index("-preset") + 1] == "6"
    # 软编原样
    x264 = video_codec_args(EncodeOpts(codec="h264", crf=18, preset="slow"))
    assert x264[x264.index("-c:v") + 1] == "libx264"
    assert x264[x264.index("-preset") + 1] == "slow"


def test_audio_args_modes():
    enc = EncodeOpts(audio_mode="none")
    assert audio_args(enc, True, "aac") == []
    assert audio_args(EncodeOpts(audio_mode="copy"), True, "flac") == ["-c:a", "copy"]
    assert audio_args(EncodeOpts(audio_mode="flac"), False, "x") == ["-c:a", "flac"]
    a = audio_args(EncodeOpts(audio_mode="aac"), True, "aac")
    assert a[:2] == ["-c:a", "aac"] and "-b:a" in a
    # auto：mp4 家族可拷则拷；mkv 下 aac 源转 aac；不可拷转 aac
    assert audio_args(EncodeOpts(), True, "aac") == ["-c:a", "copy"]
    assert audio_args(EncodeOpts(), False, "flac")[1] == "aac"
    assert audio_args(EncodeOpts(), True, "opus")[1] == "aac"


def test_subtitle_args_by_container():
    # mkv：全部字幕流原样保留
    enc_mkv = EncodeOpts(subtitle_mode="auto", container="mkv")
    assert subtitle_args(enc_mkv, ["subrip", "hdmv_pgs_subtitle"]) == \
        ["-map", "1:s?", "-c:s", "copy"]
    # mp4：全文本字幕 → mov_text；含图形字幕 → 整体丢弃（不产生半套映射）
    enc_mp4 = EncodeOpts(subtitle_mode="auto", container="mp4")
    assert subtitle_args(enc_mp4, ["subrip", "ass"]) == \
        ["-map", "1:s?", "-c:s", "mov_text"]
    assert subtitle_args(enc_mp4, ["subrip", "hdmv_pgs_subtitle"]) == []
    # 关闭 / 无字幕
    assert subtitle_args(EncodeOpts(), ["subrip"]) == []
    assert subtitle_args(enc_mkv, []) == []


def test_encoder_cmd_mux_source_without_audio():
    """只有字幕没有音轨：仍挂第二输入，且显式 -an。"""
    enc = EncodeOpts(subtitle_mode="auto", container="mkv")
    cmd = encoder_cmd(Path("in.mkv"), Path("out.mkv"), 320, 180, 320, 180,
                      "24/1", enc, True, None, ["subrip"])
    assert cmd.count(str(Path("in.mkv"))) == 1
    assert "-map" in cmd and cmd[cmd.index("-map") + 1] == "0:v:0"
    assert "-an" in cmd
    assert "-movflags" not in cmd  # mkv 不加 faststart
    assert cmd[cmd.index("-c:s") + 1] == "copy"


def test_encoder_cmd_container_faststart():
    cmd_mp4 = encoder_cmd(Path("in.mp4"), Path("out.mp4"), 320, 180, 320, 180,
                          "24/1", EncodeOpts(), True, "aac")
    assert "-movflags" in cmd_mp4
    cmd_mov = encoder_cmd(Path("in.mp4"), Path("out.mov"), 320, 180, 320, 180,
                          "24/1", EncodeOpts(container="mov"), True, "aac")
    assert "-movflags" in cmd_mov


# ---- 校验层（直接调路由函数，不起 runner）----

@pytest.fixture(scope="module")
def clip():
    TEMP_DIR.mkdir(exist_ok=True)
    p = TEMP_DIR / "eo_clip.mp4"
    if not p.exists():
        subprocess.run(
            [ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-y",
             "-f", "lavfi", "-i", "testsrc2=size=160x90:rate=24",
             "-f", "lavfi", "-i", "sine=frequency=440",
             "-t", "1", "-c:v", "libx264", "-crf", "22", "-pix_fmt", "yuv420p",
             "-c:a", "aac", "-shortest", str(p)],
            check=True, creationflags=WINDOWS_CREATE_FLAGS,
        )
    return p


def _create(clip, params):
    from sv.server.app import TaskCreate, create_task

    return create_task(TaskCreate(
        input=str(clip), model_id="realesr-animevideov3", params=params))


def _create_ok(clip, params):
    """合法参数：返回任务行并即刻删除（不起 runner，纯校验路径）。"""
    from sv.server import db

    t = _create(clip, params)
    assert db.delete_task(t["id"])
    return t


def test_validation_new_params(clip):
    for bad in ({"container": "avi"}, {"audio_mode": "mp3"}, {"subtitle_mode": "always"},
                {"audio_mode": "flac"},  # FLAC 只允许 mkv
                {"codec": "vp9"}):
        with pytest.raises(HTTPException) as e:
            _create(clip, bad)
        assert e.value.status_code == 400
    t = _create_ok(clip, {"audio_mode": "flac", "container": "mkv", "subtitle_mode": "auto"})
    assert t["params"]["container"] == "mkv"
    assert t["params"]["audio_mode"] == "flac"
    assert t["output_path"].endswith(".mkv")


def test_validation_hw_gated_codec(clip):
    r = None
    try:
        r = _create(clip, {"codec": "av1_nvenc"})
        from sv.server import db

        db.delete_task(r["id"])  # 本机支持则任务成立（测试机多为 RTX，AV1 可用）
    except HTTPException as e:
        # 裸 except 的 as e 就是异常对象本身，取属性直接 e.status_code
        assert e.status_code == 400 and "不可用" in str(e.detail)


def test_validation_denoise_levels(clip):
    t = _create_ok(clip, {"denoise": 2})
    assert t["params"]["denoise"] == 2
    with pytest.raises(HTTPException):
        _create(clip, {"denoise": 5})


def test_registry_denoise_and_animejanai():
    from sv.models.registry import file_for_scale, get_model

    cugan = get_model("real-cugan")
    assert file_for_scale(cugan, 2, "denoise0").get("variant") == "denoise0"
    assert file_for_scale(cugan, 2, "denoise1").get("variant") == "denoise1"
    assert file_for_scale(cugan, 3, "denoise0").get("variant") == "denoise0"
    assert file_for_scale(cugan, 4, "denoise3").get("variant") == "denoise3"
    for mid in ("animejanai-v2-l1", "animejanai-v2-l3", "animejanai-v3-hd-l3"):
        spec = get_model(mid)
        assert spec.scale == [2] and spec.engine == "onnx"
        assert spec.files[0]["url"].startswith(
            "https://github.com/mkktop/super_video/releases/download/models-v1/")
        assert len(spec.files[0]["sha256"]) == 64


def test_models_endpoint_denoise_levels():
    from sv.server.app import get_models

    models = {m["id"]: m for m in get_models()}
    assert models["real-cugan"]["denoise_levels"] == [0, 1, 2, 3]
    assert models["animejanai-v3-hd-l2"]["denoise_levels"] == []


def test_probe_reports_subtitles(clip):
    """带 SRT 字幕的 mkv：probe 返回字幕编码列表。"""
    sub = TEMP_DIR / "eo_sub.mkv"
    srt = TEMP_DIR / "eo.srt"
    srt.write_text("1\n00:00:00,000 --> 00:00:01,000\nhello\n", encoding="utf-8")
    subprocess.run(
        [ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-y",
         "-i", str(clip), "-i", str(srt), "-c", "copy", "-c:s", "srt", str(sub)],
        check=True, creationflags=WINDOWS_CREATE_FLAGS,
    )
    from sv.pipeline.probe import probe

    info = probe(sub)
    assert info.subtitle_count == 1
    assert info.subtitles == ["subrip"]


def test_ensure_files_targets_missing_only(monkeypatch, tmp_path):
    """变体懒下载：只补缺失文件，不重下已在本地的那份。"""
    from sv.models import manager
    from sv.models.registry import ModelSpec

    spec = ModelSpec.from_dict({
        "id": "t-variant", "name": "t", "engine": "onnx", "scale": [2],
        "files": [
            {"name": "a.onnx", "url": "u://a", "size": 10, "sha256": ""},
            {"name": "b.onnx", "url": "u://b", "size": 20, "sha256": ""},
        ],
    })
    d = tmp_path / "models" / "t-variant"
    d.mkdir(parents=True)
    (d / "a.onnx").write_bytes(b"0" * 10)
    monkeypatch.setattr(manager, "model_dir", lambda mid: tmp_path / "models" / mid)
    calls = []
    monkeypatch.setattr(manager, "download",
                        lambda spec, cb=None, only_files=None: calls.append(only_files))
    manager.ensure_files(spec, spec.files)  # b 缺失 -> 只下 b
    assert calls == [[spec.files[1]]]
    manager.ensure_files(spec, spec.files[:1])  # a 在 -> 不触发
    assert calls == [[spec.files[1]]]


# ---- 端到端：mkv + FLAC 音轨 + 字幕保留 ----

class _Nearest2x:
    scale = 2

    def process(self, frame):
        import numpy as np

        return np.repeat(np.repeat(frame, 2, axis=0), 2, axis=1)


def test_segmented_mkv_flac_subtitle_e2e(clip):
    """完整链路：分段超分 → concat 时挂 FLAC 音轨 + srt 字幕进 mkv。"""
    import asyncio
    import json as _json

    from sv.pipeline.probe import probe, validate_m0
    from sv.pipeline.segmented import SegmentedPipeline

    sub = TEMP_DIR / "eo_sub.mkv"  # test_probe_reports_subtitles 的产物（含 srt）
    if not sub.exists():
        pytest.skip("依赖前置测试生成的字幕样本")
    info = probe(sub)
    validate_m0(info)
    out = TEMP_DIR / "eo_out.mkv"
    enc = EncodeOpts(codec="h264", crf=22, container="mkv",
                     audio_mode="flac", subtitle_mode="auto")
    asyncio.run(SegmentedPipeline(
        info, out, _Nearest2x(), enc, task_id="t_eo_mkv", seg_frames=24,
    ).run())
    assert out.exists()
    data = _json.loads(subprocess.run(
        [ffprobe_bin(), "-hide_banner", "-loglevel", "error",
         "-print_format", "json", "-show_streams", str(out)],
        capture_output=True, check=True, creationflags=WINDOWS_CREATE_FLAGS,
    ).stdout)
    codecs = {s["codec_type"]: s["codec_name"] for s in data["streams"]}
    assert codecs["video"] == "h264"
    assert codecs["audio"] == "flac"
    assert codecs["subtitle"] == "subrip"
