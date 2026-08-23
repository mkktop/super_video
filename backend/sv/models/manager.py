"""模型下载与校验：流式下载 + sha256 + 原子落盘；支持 7z 包内单文件提取。"""
from __future__ import annotations

import hashlib
import urllib.request
from pathlib import Path

from ..paths import MODELS_DIR
from .registry import BUNDLED_DIR, ModelSpec, model_dir, local_files


class DownloadError(RuntimeError):
    pass


def _extract_member(archive_path: Path, member: str, dest: Path) -> None:
    """从 7z 包提取单个成员到 dest。"""
    import py7zr

    with py7zr.SevenZipFile(archive_path) as z:
        z.extract(targets=[member], path=str(dest.parent))
    extracted = dest.parent / member
    if not extracted.exists():
        raise DownloadError(f"{archive_path.name} 中找不到 {member}")
    if extracted != dest:
        extracted.replace(dest)


def _resolve(spec: ModelSpec, name: str) -> Path | None:
    """文件位置：models_store 或随包 bundled。"""
    p = model_dir(spec.id) / name
    if p.exists():
        return p
    b = BUNDLED_DIR / name
    return b if b.exists() else None


def is_downloaded(spec: ModelSpec) -> bool:
    if not spec.files:
        return False
    for f in spec.files:
        p = _resolve(spec, f["name"])
        if p is None:
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
        member = f.get("archive")  # 7z 包内成员路径（vs-mlrt 系模型）

        if dest.exists() and (not expect_sha or _sha256(dest) == expect_sha):
            done += dest.stat().st_size
            continue
        if not url:
            raise DownloadError(
                f"{name} 无下载源（bundled 缺失），见 backend/README.md 模型章节"
            )

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
            mirrors = [u for u in f.get("mirror_urls", []) if u]
            if not mirrors:
                tmp.unlink(missing_ok=True)
                raise DownloadError(f"下载 {name} 失败: {e}") from e
            # 主源失败 -> 依次镜像重试（国内 ghproxy 类镜像，M4 启用）
            ok = False
            for m in mirrors:
                try:
                    req = urllib.request.Request(m, headers={"User-Agent": "super-video/0.1"})
                    with urllib.request.urlopen(req, timeout=60) as resp, open(tmp, "wb") as out:
                        while chunk := resp.read(1 << 20):
                            out.write(chunk)
                    ok = True
                    break
                except (urllib.error.URLError, OSError):
                    continue
            if not ok:
                tmp.unlink(missing_ok=True)
                raise DownloadError(f"下载 {name} 失败（含 {len(mirrors)} 个镜像）: {e}") from e

        # 校验/提取中间产物 -> 最终 dest
        try:
            if member:
                inner = d / (name + ".inner")
                _extract_member(tmp, member, inner)
                tmp.unlink(missing_ok=True)
                tmp = inner
            if expect_size and tmp.stat().st_size != expect_size:
                raise DownloadError(f"{name} 大小不符: 期望 {expect_size}, 实际 {tmp.stat().st_size}")
            if expect_sha:
                actual = _sha256(tmp)
                if actual != expect_sha:
                    raise DownloadError(f"{name} sha256 校验失败")
        except DownloadError:
            tmp.unlink(missing_ok=True)
            raise
        tmp.replace(dest)  # 原子落盘


def ensure_downloaded(spec: ModelSpec, progress_cb=None) -> None:
    if not is_downloaded(spec):
        download(spec, progress_cb)
    else:
        # 大小一致但未校验过 sha 的，做一次完整校验（bundled 文件跳过）
        for f in spec.files:
            p = _resolve(spec, f["name"])
            if p is None:
                continue
            if f.get("sha256") and BUNDLED_DIR not in p.parents:
                if not p.with_suffix(".verified").exists():
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
