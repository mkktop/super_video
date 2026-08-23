"""模型下载与校验：流式下载 + sha256 + 原子落盘。"""
from __future__ import annotations

import hashlib
import urllib.request
from pathlib import Path

from ..paths import MODELS_DIR
from .registry import ModelSpec, model_dir, local_files


class DownloadError(RuntimeError):
    pass


def is_downloaded(spec: ModelSpec) -> bool:
    if not spec.files:
        return False
    d = model_dir(spec.id)
    for f in spec.files:
        p = d / f["name"]
        if not p.exists():
            return False
        if f.get("size") and p.stat().st_size != f["size"]:
            return False
    return True


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fp:
        for chunk in iter(lambda: fp.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def download(spec: ModelSpec, progress_cb=None) -> None:
    """下载模型全部文件。progress_cb(bytes_done, bytes_total, file_label)。"""
    d = model_dir(spec.id)
    d.mkdir(parents=True, exist_ok=True)
    total = sum(f.get("size", 0) for f in spec.files)
    done = 0
    for f in spec.files:
        url = f["url"]
        name = f["name"]
        dest = d / name
        tmp = d / (name + ".part")
        expect_size = f.get("size")
        expect_sha = f.get("sha256", "")

        if dest.exists() and (not expect_sha or _sha256(dest) == expect_sha):
            done += dest.stat().st_size
            continue

        try:
            req = urllib.request.Request(url, headers={"User-Agent": "super-video/0.1"})
            with urllib.request.urlopen(req, timeout=60) as resp, open(tmp, "wb") as out:
                got = 0
                while True:
                    chunk = resp.read(1 << 20)
                    if not chunk:
                        break
                    out.write(chunk)
                    got += len(chunk)
                    done += len(chunk)
                    if progress_cb:
                        progress_cb(done, total, name)
        except (urllib.error.URLError, OSError) as e:
            tmp.unlink(missing_ok=True)
            raise DownloadError(f"下载 {name} 失败: {e}") from e

        if expect_size and got != expect_size:
            tmp.unlink(missing_ok=True)
            raise DownloadError(f"{name} 大小不符: 期望 {expect_size}, 实际 {got}")
        if expect_sha:
            actual = _sha256(tmp)
            if actual != expect_sha:
                tmp.unlink(missing_ok=True)
                raise DownloadError(f"{name} sha256 校验失败")
        tmp.replace(dest)  # 原子落盘


def ensure_downloaded(spec: ModelSpec, progress_cb=None) -> None:
    if not is_downloaded(spec):
        download(spec, progress_cb)
    else:
        # 大小一致但未校验过 sha 的，做一次完整校验（首次安装时已验，这里兜底）
        for f, p in zip(spec.files, local_files(spec)):
            if f.get("sha256") and not p.with_suffix(".verified").exists():
                if _sha256(p) != f["sha256"]:
                    raise DownloadError(f"{p.name} 校验失败，请删除后重新下载")
                p.with_suffix(".verified").touch()


def pin_checksums(spec_id: str) -> dict[str, str]:
    """开发工具：对已下载文件计算 sha256，用于回填 manifest。"""
    from .registry import get_model

    spec = get_model(spec_id)
    out = {}
    for p in local_files(spec):
        out[p.name] = _sha256(p)
    return out
