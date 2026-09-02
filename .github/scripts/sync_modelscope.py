#!/usr/bin/env python3
"""注册表驱动：把模型资产对账同步到 ModelScope 镜像仓库。

backend/sv/models/registry_json/*.json 是唯一事实源（与 release.yml 的 models-v1
同步步同源）。取全部文件条目的 name/url/size/sha256 去重后：
  1. 本地解析：bundled/ 有副本用副本，否则从 url（models-v1）下载进缓存目录；
  2. sha256 + size 对注册表逐一校验，防坏源入库；
  3. 远端（ModelScope 仓库）已有同名且同大小的文件则跳过——幂等可重跑，
     release.yml 每次发版直接全量对账即可，缺什么传什么；
  4. 缺失的经 SDK upload_file 上传（大文件自动走 LFS 分块）。

用法：
  python sync_modelscope.py                     # 全量对账
  python sync_modelscope.py --only hat,manga    # 文件名含关键字的子集
  python sync_modelscope.py --readme            # 顺带生成并上传 README.md（许可证表）
  python sync_modelscope.py --dry-run           # 只打印计划不下载不上传

环境变量：MODELSCOPE_TOKEN（必填）、MODELSCOPE_REPO（默认 mengkaikun/super-video-models）。
ModelScope 是国内源：SDK 调用全程清代理直连；GitHub 下载走系统代理（若有）。
"""
from __future__ import annotations

import argparse
import contextlib
import glob
import hashlib
import json
import os
import sys
import time
import urllib.request
from pathlib import Path

# windows runner 管道默认 cp1252，中文 print 直接 UnicodeEncodeError（见项目 memory）
for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, "reconfigure"):
        _s.reconfigure(encoding="utf-8", errors="replace")

REPO_ROOT = Path(__file__).resolve().parents[2]
REGISTRY_GLOB = str(REPO_ROOT / "backend" / "sv" / "models" / "registry_json" / "*.json")
BUNDLED_DIR = REPO_ROOT / "backend" / "sv" / "models" / "bundled"
DEFAULT_REPO = "mengkaikun/super-video-models"
PROXY_KEYS = ("http_proxy", "https_proxy", "all_proxy")

_README_HEADER = """---
license: other
---

# super_video 模型镜像

[super_video](https://github.com/mkktop/super_video)（视频/图片 AI 超分，Windows 桌面版）
全部模型资产的国内加速镜像，与 GitHub `models-v1` Release 内容逐字节一致
（客户端按 sha256 校验）。应用内下载主源走本仓库，GitHub 为备用源。

模型版权归各自上游项目，随其原始许可再分发；标 NC（非商业）的条目仅限非商业使用。
"""


def collect_files() -> dict[str, dict]:
    """注册表全部文件条目按 name 去重（不同 manifest 引用同一文件取一份）。"""
    files: dict[str, dict] = {}
    for p in sorted(glob.glob(REGISTRY_GLOB)):
        spec = json.loads(Path(p).read_text(encoding="utf-8"))
        for f in spec["files"]:
            if not f.get("url"):
                continue
            if f["name"] in files and files[f["name"]] != f:
                print(f"!! 注册表内 {f['name']} 条目不一致，以先出现为准", file=sys.stderr)
                continue
            files.setdefault(f["name"], f)
    return files


@contextlib.contextmanager
def direct_connection():
    """临时清掉代理环境变量（requests/urllib 按次读取 env），保证国内直连。"""
    saved = {}
    for k in list(os.environ):
        if k.lower() in PROXY_KEYS:
            saved[k] = os.environ.pop(k)
    try:
        yield
    finally:
        os.environ.update(saved)


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fp:
        for chunk in iter(lambda: fp.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def source_file(entry: dict, cache_dir: Path) -> Path:
    """拿到待上传的本地文件：bundled 优先，否则缓存下载（3 次重试）。"""
    name, url = entry["name"], entry["url"]
    local = BUNDLED_DIR / name
    if local.exists():
        return local
    dest = cache_dir / name
    if dest.exists() and dest.stat().st_size == entry.get("size"):
        return dest
    part = dest.with_suffix(dest.suffix + ".part")
    last_err: Exception | None = None
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "super-video-sync/1.0"})
            with urllib.request.build_opener().open(req, timeout=60) as resp, open(part, "wb") as out:
                got, total = 0, entry.get("size") or 0
                while chunk := resp.read(1 << 20):
                    out.write(chunk)
                    got += len(chunk)
                    if total and got // (32 << 20) != (got - len(chunk)) // (32 << 20):
                        print(f"    {name}: {got / 1e6:.0f}/{total / 1e6:.0f} MB", flush=True)
            part.replace(dest)
            return dest
        except (urllib.error.URLError, OSError) as e:
            last_err = e
            part.unlink(missing_ok=True)
            time.sleep(5 * (attempt + 1))
    raise RuntimeError(f"下载 {name} 失败（3 次）: {last_err}")


def build_readme(manifests: list[dict]) -> str:
    rows = []
    for spec in sorted(manifests, key=lambda s: s["id"]):
        size = sum(f.get("size", 0) for f in spec["files"])
        rows.append(
            f"| {spec['name']} | `{spec['id']}` | {spec.get('vendor', '')} "
            f"| {spec.get('license', '')} | {len(spec['files'])} | {size / 1e6:.1f} MB |"
        )
    return _README_HEADER + (
        "\n## 模型清单\n\n"
        "| 模型 | id | 上游 | 许可 | 文件数 | 大小 |\n"
        "| --- | --- | --- | --- | --- | --- |\n" + "\n".join(rows) + "\n"
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="", help="逗号分隔的文件名子串过滤")
    ap.add_argument("--readme", action="store_true", help="生成并上传 README.md")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--cache-dir", default=str(REPO_ROOT / ".tmp" / "modelscope_cache"))
    args = ap.parse_args()

    token = os.environ.get("MODELSCOPE_TOKEN", "")
    repo = os.environ.get("MODELSCOPE_REPO", DEFAULT_REPO)
    if not args.dry_run and not token:
        print("缺少 MODELSCOPE_TOKEN", file=sys.stderr)
        return 2

    needles = [s.strip().lower() for s in args.only.split(",") if s.strip()]
    files = {n: f for n, f in collect_files().items()
             if not needles or any(s in n.lower() for s in needles)}
    total = sum(f.get("size", 0) for f in files.values())
    print(f"注册表对账：{len(files)} 个文件，共 {total / 1e9:.2f} GB")

    from modelscope.hub.api import HubApi

    api = HubApi()
    if token:
        with direct_connection():
            api.login(token)
    with direct_connection():
        remote = {fi.path: fi.size for fi in api.list_repo_files(repo, "model")}
    print(f"远端已有 {len(remote)} 个文件")

    pending = {}
    for name, f in files.items():
        if remote.get(name) == f.get("size"):
            continue
        if name in remote:
            print(f"!! 远端 {name} 大小不符（{remote[name]} != {f.get('size')}），将重传")
        pending[name] = f
    print(f"待上传 {len(pending)} 个")
    if args.dry_run:
        for name in sorted(pending):
            print(f"  - {name} ({files[name].get('size', 0) / 1e6:.1f} MB)")
        return 0

    cache_dir = Path(args.cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    failed = []
    for i, (name, f) in enumerate(sorted(pending.items()), 1):
        print(f"[{i}/{len(pending)}] {name}", flush=True)
        try:
            path = source_file(f, cache_dir)
            if f.get("sha256") and sha256(path) != f["sha256"]:
                raise RuntimeError(f"{name} sha256 校验失败")
            with direct_connection():
                api.upload_file(path_or_fileobj=str(path), path_in_repo=name,
                                repo_id=repo, repo_type="model",
                                commit_message=f"sync {name} from models-v1")
        except Exception as e:  # noqa: BLE001 —— 单文件失败不阻塞整批
            failed.append(name)
            print(f"  !! {type(e).__name__}: {e}", file=sys.stderr)

    if args.readme:
        manifests = [json.loads(Path(p).read_text(encoding="utf-8"))
                     for p in sorted(glob.glob(REGISTRY_GLOB))]
        readme = cache_dir / "README.md"
        readme.write_text(build_readme(manifests), encoding="utf-8")
        with direct_connection():
            api.upload_file(path_or_fileobj=str(readme), path_in_repo="README.md",
                            repo_id=repo, repo_type="model", commit_message="sync README")
        print("README.md 已更新")

    print(f"完成：上传 {len(pending) - len(failed)}，失败 {len(failed)}")
    for name in failed:
        print(f"  !! {name}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
