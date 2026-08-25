"""组装/打包/上传 TRT 可选运行时组件（安装版 TRT 加速的资产管家）。

产物（backend/dist/trt_component/）：
- trt-core-win-x64-vN.7z            python/onnxruntime + dlls/（CUDA 运行库 + TRT 核心库）
- trt-builder-<arch>-win-x64-vN.7z  各 GPU 架构的引擎编译资源（单 DLL，按卡选装）

并回填 sv/server/trt_component_assets.json（url/size/sha256/raw）。
素材源 = 本机 .venv-cuda（onnxruntime-gpu + nvidia-*-cu12 + tensorrt-cu12-libs），
Python ABI 以主 .venv 为准（组件是给打包版 sidecar 用的，ABI 必须与其一致）。

用法:
  python scripts/build_trt_component.py              # 组装 + 打包 + 回填清单
  python scripts/build_trt_component.py --upload     # 追加：上传到 GitHub release runtime-v1
  python scripts/build_trt_component.py --compress-only core   # 只重压一个包（调试）
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
ROOT = BACKEND.parent
SITE = ROOT / ".venv-cuda" / "Lib" / "site-packages"
OUT = BACKEND / "dist" / "trt_component"
ASSETS_JSON = BACKEND / "sv" / "server" / "trt_component_assets.json"

# 组件版本：ABI/依赖集合变化时 bump（老组件 manifest 对不上会被判不兼容）
VERSION = 1
RELEASE_TAG = "runtime-v1"
REPO = "mkktop/super_video"
URL_BASE = f"https://github.com/{REPO}/releases/download/{RELEASE_TAG}/"

# CUDA 运行库裁剪：providers_cuda.dll 实测不依赖 nvrtc（179MB 白省）；
# cufft 是硬依赖（静态导入表），砍了 CUDA EP 直接加载失败
NV_SKIP = ("nvrtc",)


def _abi() -> str:
    main = ROOT / ".venv" / "Scripts" / "python.exe"
    out = subprocess.run([str(main), "-c",
                          "import sys; print(f'cp{sys.version_info.major}{sys.version_info.minor}')"],
                         capture_output=True, text=True)
    abi = out.stdout.strip()
    assert re.fullmatch(r"cp\d+", abi), f"主 venv ABI 探测失败: {out.stdout} {out.stderr}"
    return abi


def _ort_version() -> str:
    for d in SITE.glob("onnxruntime_gpu-*.dist-info"):
        m = re.match(r"onnxruntime_gpu-(.+)\.dist-info", d.name)
        if m:
            return m.group(1)
    raise SystemExit("找不到 onnxruntime_gpu dist-info（.venv-cuda 未就绪？）")


def _trt_version() -> str:
    for d in SITE.glob("tensorrt_cu12_libs-*.dist-info"):
        m = re.match(r"tensorrt_cu12_libs-(.+)\.dist-info", d.name)
        if m:
            return m.group(1)
    raise SystemExit("找不到 tensorrt_cu12_libs dist-info")


def assemble(staging: Path) -> dict[str, list[tuple[Path, str]]]:
    """组装 staging 并返回 {包名: [(绝对路径, 7z 内路径), ...]}。"""
    if staging.exists():
        shutil.rmtree(staging)
    py_dir = staging / "python" / "onnxruntime"
    dlls = staging / "dlls"

    print("[1/3] 复制 onnxruntime 包 ...", flush=True)
    shutil.copytree(SITE / "onnxruntime", py_dir,
                    ignore=shutil.ignore_patterns("__pycache__"))

    print("[2/3] 收集 CUDA/TRT DLL ...", flush=True)
    dlls.mkdir(parents=True)
    n = 0
    for f in sorted((SITE / "nvidia").glob("*/bin/*.dll")):
        if f.name.lower().startswith(NV_SKIP):
            continue
        shutil.copy2(f, dlls / f.name)
        n += 1
    for name in ("nvinfer_10.dll", "nvinfer_plugin_10.dll", "nvonnxparser_10.dll"):
        src = SITE / "tensorrt_libs" / name
        if not src.exists():
            raise SystemExit(f"缺少 {name}（.venv-cuda tensorrt-cu12-libs 未装？）")
        shutil.copy2(src, dlls / name)
    print(f"      dlls/ 共 {n + 3} 个文件", flush=True)

    packs: dict[str, list[tuple[Path, str]]] = {"core": []}
    for f in sorted(staging.rglob("*")):
        if f.is_file():
            packs["core"].append((f, f.relative_to(staging).as_posix()))

    print("[3/3] 分架构 builder 包 ...", flush=True)
    for f in sorted((SITE / "tensorrt_libs").glob("nvinfer_builder_resource_*_10.dll")):
        m = re.match(r"nvinfer_builder_resource_(.+?)_10\.dll", f.name)
        if not m:
            continue
        packs[f"builder-{m.group(1)}"] = [(f, f"dlls/{f.name}")]
    return packs


def compress(name: str, members: list[tuple[Path, str]], out_dir: Path,
             version: int) -> Path:
    dst = out_dir / f"trt-{name}-win-x64-v{version}.7z"
    import py7zr

    with py7zr.SevenZipFile(dst, "w") as z:
        for src, arc in members:
            z.write(src, arc)
    return dst


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fp:
        for chunk in iter(lambda: fp.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def write_manifest_json(parts: dict[str, dict], abi: str, ort: str, trt: str) -> None:
    data = {
        "version": VERSION, "python": abi, "ort": ort, "trt": trt,
        "assets": parts,
    }
    ASSETS_JSON.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n",
                           encoding="utf-8")
    print(f"清单已回填 -> {ASSETS_JSON.relative_to(ROOT)}")


# ---- 上传（gh CLI 不可用：git credential fill 取 token，API 直传） ----

def _token() -> str:
    inp = "protocol=https\nhost=github.com\n\n"
    out = subprocess.run(["git", "credential", "fill"], input=inp,
                         capture_output=True, text=True)
    for line in out.stdout.splitlines():
        if line.startswith("password="):
            return line.split("=", 1)[1]
    raise SystemExit("git credential 未取到 GitHub token")


def _api(url: str, token: str, method: str = "GET",
         body: bytes | None = None, ctype: str = "application/json"):
    req = urllib.request.Request(url, data=body, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Accept", "application/vnd.github+json")
    if body is not None:
        req.add_header("Content-Type", ctype)
    req.add_header("User-Agent", "super-video-builder")
    with urllib.request.urlopen(req) as r:
        raw = r.read()
        return r.status, json.loads(raw) if raw and ctype == "application/json" else raw


def _upload_asset(url: str, token: str, path: Path) -> None:
    total = path.stat().st_size
    sent = 0

    def chunks():
        nonlocal sent
        with open(path, "rb") as fp:
            while True:
                b = fp.read(1 << 23)
                if not b:
                    break
                sent += len(b)
                print(f"\r      {sent / 1e9:.2f}/{total / 1e9:.2f} GB", end="", flush=True)
                yield b

    req = urllib.request.Request(url, data=chunks(), method="POST")
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Content-Type", "application/octet-stream")
    req.add_header("Content-Length", str(total))
    req.add_header("User-Agent", "super-video-builder")
    with urllib.request.urlopen(req) as r:
        print(f" -> HTTP {r.status}")
        assert r.status == 201, f"上传失败 HTTP {r.status}"


def upload(files: list[Path]) -> None:
    token = _token()
    api = f"https://api.github.com/repos/{REPO}/releases"
    try:
        _, rel = _api(f"{api}/tags/{RELEASE_TAG}", token)
        rel_id = rel["id"]
        print(f"release {RELEASE_TAG} 已存在 (id={rel_id})")
    except urllib.error.HTTPError as e:
        if e.code != 404:
            raise
        body = json.dumps({
            "tag_name": RELEASE_TAG, "name": "TRT runtime components",
            "body": "安装版 TensorRT 加速可选组件（core + 分架构 builder 资源），"
                    "由 backend/scripts/build_trt_component.py 产出。",
        }).encode()
        _, rel = _api(api, token, "POST", body)
        rel_id = rel["id"]
        print(f"release {RELEASE_TAG} 已创建 (id={rel_id})")

    existing = {a["name"]: a["id"] for a in rel.get("assets", [])} \
        if isinstance(rel, dict) else {}
    for f in files:
        name = f.name
        print(f"上传 {name} ({f.stat().st_size / 1e9:.2f} GB)", flush=True)
        if name in existing:
            _api(f"https://api.github.com/repos/{REPO}/releases/assets/{existing[name]}",
                 token, "DELETE")
            print(f"      已删除旧资产 {name}")
        _upload_asset(
            f"https://uploads.github.com/repos/{REPO}/releases/{rel_id}/assets?name={name}",
            token, f)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--upload", action="store_true", help="打包后上传到 GitHub release")
    ap.add_argument("--upload-only", action="store_true",
                    help="跳过构建，直接上传既有 7z 并按清单回填（修资产重传用）")
    ap.add_argument("--compress-only", default=None, metavar="PACK",
                    help="只重压指定包（core / builder-sm120 / ...）并回填清单")
    args = ap.parse_args()

    if not SITE.is_dir():
        raise SystemExit(f".venv-cuda 不存在: {SITE}")

    if args.upload_only:
        data = json.loads(ASSETS_JSON.read_text(encoding="utf-8"))
        names = list(data.get("assets", {}))
        upload([OUT / f"trt-{n}-win-x64-v{VERSION}.7z" for n in names])
        return 0

    abi, ort, trt = _abi(), _ort_version(), _trt_version()
    print(f"ABI={abi} ORT={ort} TRT={trt}")
    OUT.mkdir(parents=True, exist_ok=True)

    if args.compress_only:
        # 复用已有 staging（调压缩参数用），只重打一个包
        staging = OUT / "staging"
        packs = {"core": [(f, f.relative_to(staging).as_posix())
                          for f in sorted(staging.rglob("*")) if f.is_file()]}
        m = re.match(r"builder-(.+)", args.compress_only)
        if m:
            src = SITE / "tensorrt_libs" / f"nvinfer_builder_resource_{m.group(1)}_10.dll"
            packs = {args.compress_only: [(src, f"dlls/{src.name}")]}
        names = [args.compress_only]
    else:
        packs = assemble(OUT / "staging")
        names = list(packs)

    parts: dict[str, dict] = {}
    for name in names:
        members = packs[name]
        raw = sum(s.stat().st_size for s, _ in members)
        print(f"压缩 {name}（{len(members)} 文件, raw {raw / 1e9:.2f} GB）...", flush=True)
        dst = compress(name, members, OUT, VERSION)
        size, digest = dst.stat().st_size, sha256(dst)
        print(f"      -> {dst.name} {size / 1e9:.2f} GB (ratio {size / raw:.2f})")
        parts[name] = {"url": URL_BASE + dst.name, "size": size,
                       "sha256": digest, "raw": raw}

    # 保留其余包的既有清单条目（--compress-only 时只覆盖单个包）
    if args.compress_only:
        old = json.loads(ASSETS_JSON.read_text(encoding="utf-8"))["assets"]
        old.update(parts)
        parts = old
    write_manifest_json(parts, abi, ort, trt)

    if args.upload:
        upload([OUT / f"trt-{n}-win-x64-v{VERSION}.7z" for n in names])
    print("完成")
    return 0


if __name__ == "__main__":
    sys.exit(main())
