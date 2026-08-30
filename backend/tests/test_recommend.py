"""智能推荐：规则引擎单测 + 采样帧统计（合成素材）+ probe 端点附带块。"""
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from sv.paths import TEMP_DIR, ffmpeg_bin
from sv.pipeline.analyze import sample_frame_stats
from sv.pipeline.probe import MediaInfo, probe
from sv.pipeline.recommend import build_recommendation, recommend
from sv.utils.process import WINDOWS_CREATE_FLAGS


def _info(**over) -> MediaInfo:
    """轻量 MediaInfo：只填推荐用到的字段。"""
    d = dict(
        path=Path("v.mp4"), container="mp4", duration_s=100.0,
        width=720, height=480, fps=23.976, fps_str="24000/1001",
        vfr=False, video_codec="h264", pix_fmt="yuv420p",
        bit_depth=8, color_transfer="bt709",
    )
    d.update(over)
    return MediaInfo(**d)


ANIME_STATS = {"flat_ratio": 0.72, "comb_frac": 0.01, "frames": 4}
LIVE_STATS = {"flat_ratio": 0.15, "comb_frac": 0.01, "frames": 4}
COMB_STATS = {"flat_ratio": 0.3, "comb_frac": 0.4, "frames": 4}


# ---- 规则引擎单测（无 ffmpeg 依赖） ----


def test_anime_content_picks_anime_model():
    rec = recommend(_info(), ANIME_STATS)
    assert rec["animated"] is True
    assert rec["model_id"] == "realesr-animevideov3"
    assert rec["deband"] is True  # 480p 动画（≤600p）默认建议去色带
    assert rec["interp"] == "off"  # 补帧只提示不自动开


def test_anime_h264_720p_no_deband():
    """较高分辨率的 h264 动画（BD/web rip）通常干净：不建议去色带。"""
    rec = recommend(_info(height=720), ANIME_STATS)
    assert rec["deband"] is False


def test_anime_lowres_old_codec_suggests_deband():
    rec = recommend(_info(height=480, video_codec="mpeg2video"), ANIME_STATS)
    assert rec["deband"] is True
    assert any("色带" in r for r in rec["reasons"])
    assert any("老编码" in r for r in rec["reasons"])


def test_live_content_picks_x4plus():
    """SD 真人源（480p）：x4plus x4 = 1920p，正好是修复模型的甜点位。"""
    rec = recommend(_info(height=480), LIVE_STATS)
    assert rec["animated"] is False
    assert rec["model_id"] == "realesrgan-x4plus"
    assert rec["target_scale"] == 4
    assert rec["deband"] is False


def test_live_hd_avoids_8k_trap():
    """高清真人（1080p）只有 x4 的修复模型会踩 8K 编码上限：退到 x2 档。"""
    rec = recommend(_info(height=1080), LIVE_STATS)
    assert rec["model_id"] == "realesr-animevideov3"
    assert rec["target_scale"] == 2
    assert any("8K" in r for r in rec["reasons"])


def test_interlace_from_field_order_metadata():
    rec = recommend(_info(field_order="tt"), LIVE_STATS)
    assert rec["deinterlace"] is True
    assert any("反交错" in r for r in rec["reasons"])


def test_interlace_from_comb_stats():
    """容器没标场序（常见误标 progressive）但帧统计有梳齿：也要建议反交错。"""
    rec = recommend(_info(), COMB_STATS)
    assert rec["deinterlace"] is True


def test_scale_rule_prefers_smallest_reaching_1080p():
    # 480p：x3 达到 1440≥1080 → x3（比 x4 省算力）
    assert recommend(_info(height=480), ANIME_STATS)["target_scale"] == 3
    # 720p：x2 → 1440 ≥1080 → x2
    assert recommend(_info(height=720), ANIME_STATS)["target_scale"] == 2
    # 1080p：x2 → 2160
    assert recommend(_info(height=1080), ANIME_STATS)["target_scale"] == 2
    # SD 真人只有 x4 模型：480×4=1920 合理；高清真人已在上面单独验证退 x2


def test_no_stats_still_recommends():
    """采样失败（stats=None）：按保守默认推荐（视频向模型线，最坏不踩陷阱）。"""
    rec = recommend(_info(), None)
    assert rec["animated"] is None
    assert rec["model_id"] == "realesr-animevideov3"
    assert rec["deband"] is False


def test_low_fps_mentioned_in_reasons_only():
    rec = recommend(_info(fps=23.976), ANIME_STATS)
    assert rec["interp"] == "off"
    assert any("补帧" in r for r in rec["reasons"])
    hi = recommend(_info(fps=60.0), ANIME_STATS)
    assert not any("补帧" in r for r in hi["reasons"])


def test_build_recommendation_resolves_registry():
    rec = build_recommendation(_info(), ANIME_STATS)
    assert rec["model_id"] == "realesr-animevideov3"
    assert rec["model_name"]  # 非空展示名
    # 倍率落在注册表该模型真实支持的档位内（注册表档位少时收口会重算）
    from sv.models.registry import load_registry

    assert rec["target_scale"] in load_registry()[rec["model_id"]].scale


# ---- 采样帧统计（合成素材，真实 ffmpeg） ----


def _make(path: Path, vf: str | None = None) -> MediaInfo:
    cmd = [ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-y",
           "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=24", "-t", "2"]
    if vf:
        cmd += ["-vf", vf]
    cmd += ["-c:v", "libx264", "-crf", "20", "-pix_fmt", "yuv420p", str(path)]
    subprocess.run(cmd, check=True, creationflags=WINDOWS_CREATE_FLAGS)
    return probe(path)


def test_flat_clip_reads_as_anime_like():
    """判别面是"有无处处微差的颗粒/纹理"（真人实拍特征），不是渐变本身：
    平涂色块 flat_ratio≈1；加噪后（实拍近似）显著回落到动画阈值之下。"""
    TEMP_DIR.mkdir(parents=True, exist_ok=True)
    flat = sample_frame_stats(_make(TEMP_DIR / "rec_flat.mp4",
                                    vf="drawbox=w=iw:h=ih:color=0x3355AA@1:t=fill"))
    noisy = sample_frame_stats(_make(TEMP_DIR / "rec_noisy.mp4",
                                     vf="noise=alls=18:allf=t"))
    assert flat["flat_ratio"] > 0.85
    assert noisy["flat_ratio"] < 0.42
    assert flat["flat_ratio"] > noisy["flat_ratio"] + 0.4


def test_comb_clip_detected(tmp_path):
    """红蓝逐帧交替 + tinterlace = 全场梳齿的极端隔行源：comb_frac 大幅超标。"""
    src = tmp_path / "comb.mp4"
    r = subprocess.run(
        [ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", "color=c=red:size=320x240:rate=24",
         "-f", "lavfi", "-i", "color=c=blue:size=320x240:rate=24",
         "-filter_complex",
         "[0][1]interleave=nb_inputs=2,tinterlace=interleave_top,format=yuv420p",
         "-t", "2", "-c:v", "libx264", "-crf", "20", str(src)],
        check=True, capture_output=True, creationflags=WINDOWS_CREATE_FLAGS)
    assert r.returncode == 0
    st = sample_frame_stats(probe(src))
    assert st["comb_frac"] > 0.5
    rec = build_recommendation(probe(src), st)
    assert rec["deinterlace"] is True


def test_corrupt_source_returns_none(tmp_path):
    bad = tmp_path / "bad.mp4"
    bad.write_bytes(b"not a video at all")
    assert sample_frame_stats(_info(path=bad)) is None


# ---- probe 端点附带推荐块 ----


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("SV_DB", str(tmp_path / "rec.db"))
    from sv.server import db as sv_db

    sv_db.init_db()
    monkeypatch.setattr(sv_db, "next_queued", lambda: None)
    from sv.server.app import app

    with TestClient(app) as c:
        yield c


def test_probe_with_recommend_flag(client, tmp_path):
    clip = tmp_path / "p.mp4"
    _make(clip)
    r = client.post("/api/probe", json={"path": str(clip), "recommend": True})
    assert r.status_code == 200, r.text
    body = r.json()
    assert "recommend" in body
    rec = body["recommend"]
    assert rec["reasons"] and rec["target_scale"] >= 2
    # 默认不带（省 1s 采样开销）
    plain = client.post("/api/probe", json={"path": str(clip)}).json()
    assert "recommend" not in plain


def test_probe_recommend_silent_on_analysis_failure(client, tmp_path, monkeypatch):
    """源分析抛异常：probe 主流程不受影响，只是没有推荐块。"""
    clip = tmp_path / "p2.mp4"
    _make(clip)

    def boom(info):
        raise RuntimeError("decoder broke")

    monkeypatch.setattr("sv.pipeline.analyze.sample_frame_stats", boom)
    r = client.post("/api/probe", json={"path": str(clip), "recommend": True})
    assert r.status_code == 200
    assert "recommend" not in r.json()
