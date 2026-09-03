"""模型下载与校验：流式下载 + sha256 + 原子落盘；支持 7z 包内单文件提取。"""
from __future__ import annotations

import hashlib
import urllib.request
from pathlib import Path

from ..paths import MODELS_DIR
from .registry import BUNDLED_DIR, ModelSpec, model_dir, local_files


class DownloadError(RuntimeError):
    pass


def _opener() -> urllib.request.OpenerDirector:
    """按设置构造下载 opener：
    - ""（默认）：urllib 默认行为（环境变量 + Windows 注册表系统代理）
    - "direct"：强制直连，忽略一切代理
    - "http://host:port"：显式走该代理（Clash 等本地代理最稳）
    """
    from ..server.settings import load as load_settings

    proxy = (load_settings().get("download_proxy") or "").strip()
    if proxy == "direct":
        return urllib.request.build_opener(urllib.request.ProxyHandler({}))
    if proxy.startswith("http://") or proxy.startswith("https://"):
        return urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": proxy, "https": proxy})
        )
    return urllib.request.build_opener()


def _download_to(url: str, dest: Path, opener: urllib.request.OpenerDirector,
                 progress_cb=None, done: int = 0, total: int = 0, label: str = "") -> int:
    """流式下载 url 到 dest，实时回调进度（bytes_done, bytes_total, label）。

    返回本次实际下载字节数——调用方据此累计多文件进度（done 按值传入，
    不回写调用方的计数器，此前外层漏加导致多权重模型进度每个文件从 0 重爬）。
    """
    req = urllib.request.Request(url, headers={"User-Agent": "super-video/0.1"})
    with opener.open(req, timeout=60) as resp, open(dest, "wb") as out:
        got = 0
        while True:
            chunk = resp.read(1 << 20)
            if not chunk:
                break
            out.write(chunk)
            got += len(chunk)
            done += len(chunk)
            if progress_cb:
                progress_cb(done, total, label)
    return got


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


def download(spec: ModelSpec, progress_cb=None, only_files: list[dict] | None = None,
             source_cb=None) -> None:
    """下载模型文件。progress_cb(bytes_done, bytes_total, file_label)。

    only_files：只下载给定子集（变体懒下载——任务实际用哪个权重下哪个）；
    已在本地且校验通过的文件照旧跳过。
    source_cb(url, file_label)：每次实际发起下载前回调（主源 + 逐镜像），
    供上层透出当前下载渠道（前端显示 ModelScope / GitHub 镜像）。
    """
    d = model_dir(spec.id)
    d.mkdir(parents=True, exist_ok=True)
    targets = list(only_files) if only_files is not None else list(spec.files)
    # 进度口径：total 只算 targets，done 基线必须为 0——曾把非 targets 的已存在
    # 文件大小累进 done 初值（total 却不含它们），子集下载进度一上来就 >100%
    total = sum(f.get("size", 0) for f in targets)
    done = 0
    for f in targets:
        url = f["url"]
        name = f["name"]
        dest = d / name
        tmp = d / (name + ".part")
        expect_size = f.get("size")
        expect_sha = f.get("sha256", "")
        member = f.get("archive")  # 7z 包内成员路径（vs-mlrt 系模型）

        if dest.exists() and (not expect_sha or _sha256(dest) == expect_sha):
            done += expect_size if expect_size else dest.stat().st_size
            continue
        if not url:
            raise DownloadError(
                f"{name} 无下载源（bundled 缺失），见 backend/README.md 模型章节"
            )

        try:
            done_at_file = done  # 镜像重试时进度回退，避免重复累计
            opener = _opener()
            if source_cb:
                source_cb(url, name)
            done += _download_to(url, tmp, opener, progress_cb, done, total, name)
        except (urllib.error.URLError, OSError) as e:
            mirrors = [u for u in f.get("mirror_urls", []) if u]
            if not mirrors:
                tmp.unlink(missing_ok=True)
                raise DownloadError(f"下载 {name} 失败: {e}") from e
            # 主源失败 -> 依次镜像重试（国内 ghproxy 类镜像，M4 启用）
            ok = False
            for m in mirrors:
                try:
                    if source_cb:
                        source_cb(m, name)
                    done = done_at_file + _download_to(
                        m, tmp, opener, progress_cb, done_at_file, total, name)
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

    # 收尾兜底：尾部跳过的本地文件（懒下载过的权重）在静默累加 done 后不再有
    # 下载事件，末次回调停在 total 之前——UI 永远看不见 100%。补发终值
    if progress_cb and total > 0:
        progress_cb(total, total, targets[-1]["name"] if targets else "")


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


def ensure_files(spec: ModelSpec, files: list[dict], progress_cb=None) -> None:
    """只保障给定文件子集在本地（变体懒下载：任务用哪个权重下哪个）。

    与 ensure_downloaded 的区别：多档位模型（如 real-cugan 11 个权重）不必
    全量下载即可开跑；缺哪个下哪个，已在本地的不动。
    """
    missing = []
    for f in files:
        p = _resolve(spec, f["name"])
        if p is None or (f.get("size") and p.stat().st_size != f["size"]):
            missing.append(f)
    if not missing:
        return
    download(spec, progress_cb, only_files=missing)


def pin_checksums(spec_id: str) -> dict[str, str]:
    """开发工具：对已下载文件计算 sha256，用于回填 manifest。"""
    from .registry import get_model

    spec = get_model(spec_id)
    out = {}
    for p in local_files(spec):
        out[p.name] = _sha256(p)
    return out
