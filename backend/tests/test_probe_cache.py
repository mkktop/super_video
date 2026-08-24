"""probe 结果缓存：同文件二次探测不再起 ffprobe；mtime 变化即失效。"""
import os
import subprocess

from sv.paths import TEMP_DIR, ffmpeg_bin
from sv.utils.process import WINDOWS_CREATE_FLAGS


def _make_clip(path):
    subprocess.run(
        [ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", "testsrc2=size=160x90:rate=24", "-t", "1",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", str(path)],
        check=True, creationflags=WINDOWS_CREATE_FLAGS,
    )


def test_cache_hit_and_invalidate(monkeypatch):
    p = TEMP_DIR / "probe_cache.mp4"
    _make_clip(p)

    from sv.pipeline import probe as P

    calls = []
    real = P._ffprobe_json

    def counting(args):
        calls.append(list(args))
        return real(args)

    monkeypatch.setattr(P, "_ffprobe_json", counting)

    i1 = P.probe(p)
    i2 = P.probe(p)
    assert i1 is i2, "同一文件两次探测必须命中缓存（同一对象）"
    assert len([c for c in calls if "-show_format" in c]) == 1, "第二次不应再起 ffprobe"

    st = p.stat()
    os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))
    i3 = P.probe(p)
    assert i3 is not i1, "mtime 变化后必须重新探测"
    assert i3 == i1, "内容等值（dataclass 比较）"
