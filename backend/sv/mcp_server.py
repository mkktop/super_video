"""MCP stdio bridge：把运行中 sidecar 的能力暴露给 AI 客户端（Claude Desktop / ZCode / Cursor）。

入口：安装版 `sidecar.exe mcp`（cli.py 子命令）；dev `python -m sv.mcp_server`。

协议层零依赖手写——MCP stdio 传输就是逐行 JSON-RPC 2.0，工具服务端只需
initialize / tools/list / tools/call / ping（该子集自首个协议版本 2024-11-05
以来未变）。不引入官方 mcp SDK：其 httpx 传递依赖与本仓库 pin 的 httpx2
（starlette 1.6 TestClient 专用变体）共存未经验证，且过版本纪律/
PyInstaller/selftest 三道闸的成本远高于这 200 行协议层。

stdout 是协议通道：除 JSON-RPC 应答行外绝无输出，诊断一律走 stderr。
bridge 只代理不拉起 sidecar——双 runner 会抢 GPU 队列与 sqlite（v1 拍板，
详见 backend/README「MCP 接入」）。
"""
from __future__ import annotations

import json
import base64
import io
import math
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable

from . import __version__
from .paths import TEMP_DIR
from .server.consts import _CODECS

# 客户端请求版本在支持集内则回显，否则回最高支持版（规范允许的降级协商）
_SUPPORTED_PROTOCOLS = ("2024-11-05", "2025-03-26", "2025-06-18")

# 与 Electron 主进程 startOrReuseSidecar 同款扫描范围（含"端口被非 sidecar
# 进程占用则跳过"的语义：靠 /api/health 的 ok 健康标记排除）
_PORT_RANGE = range(8730, 8740)
_TIMEOUT_S = 30.0
_MAX_LIST = 30  # rf_tasks 最多回给 AI 的条数
_MAX_SCAN_FILES = 20  # rf_scan_folder 预览文件数
_IMAGES_SLIM_AT = 20  # params.images 超过此数即瘦身（漫画批量可达 10 万页）
_IMAGES_KEEP = 3
_MAX_WATERMARK_ERRORS = 20
_MAX_ASSET_BYTES = 32 * 1024 * 1024


class _ToolContent(list):
    """Native MCP content blocks, for image previews instead of base64 in JSON text."""


class ToolError(Exception):
    """工具级失败：转为 isError=true 的文本结果（不是协议错误）。"""


class SidecarOfflineError(ToolError):
    """sidecar 未运行或连接失败。"""


class UnauthorizedError(ToolError):
    """token 候选全部被拒（含重读重试后）。"""


class ApiError(ToolError):
    """sidecar 返回 4xx/5xx，detail 原样透传（409 冲突等信息对 AI 有意义）。"""

    def __init__(self, status: int, detail: str):
        super().__init__(f"sidecar 返回 HTTP {status}: {detail}")
        self.status = status
        self.detail = detail


# 直连 opener：本机代理会劫持 localhost（curl 探活必须 --noproxy 的同款坑），
# urllib 默认 opener 会采信系统/注册表代理，必须显式清空
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _http_json(url: str, method: str, body: dict | None, token: str | None,
               timeout: float) -> Any:
    """一次 JSON 请求；失败统一抛 ToolError 子类（HTTPError 先于 URLError 捕获）。"""
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if body is not None:
        # urllib 对带 data 的请求默认 Content-Type 是 x-www-form-urlencoded，
        # FastAPI 会按表单解析收到一个字符串 → 422，必须显式声明 JSON
        req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("x-sv-token", token)
    req.add_header("User-Agent", f"rainframe-mcp/{__version__}")
    try:
        with _OPENER.open(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            payload = json.loads(e.read().decode("utf-8"))
            detail = str(payload.get("detail", payload))
        except Exception:  # noqa: BLE001 — 错误体不是 JSON 就用 reason
            detail = str(e.reason or "")
        raise ApiError(e.code, detail) from None
    except (urllib.error.URLError, OSError) as e:
        raise SidecarOfflineError(
            f"连不上 sidecar（{e}）。雨帧可能已被关闭，重新打开后重试。") from None


def _http_read(url: str, token: str | None, timeout: float) -> bytes:
    req = urllib.request.Request(url)
    req.add_header("User-Agent", f"rainframe-mcp/{__version__}")
    if token:
        req.add_header("x-sv-token", token)
    try:
        with _OPENER.open(req, timeout=timeout) as response:
            data = response.read(_MAX_ASSET_BYTES + 1)
            if len(data) > _MAX_ASSET_BYTES:
                raise ToolError("预览或日志超过 32 MiB，请使用更小的样本或缩短日志范围")
            return data
    except urllib.error.HTTPError as error:
        try:
            detail = str(json.loads(error.read().decode("utf-8")).get("detail", error.reason))
        except (ValueError, UnicodeError):
            detail = str(error.reason)
        raise ApiError(error.code, detail) from None
    except (urllib.error.URLError, OSError) as error:
        raise SidecarOfflineError(f"连不上 sidecar（{error}）。请重新打开雨帧后重试。") from None


# ---------------------------------------------------------------- token 候选

def _token_file_candidates() -> list[Path]:
    """候选 token 文件路径（存在与否调用时再判）。

    bridge 由 MCP 客户端拉起，拿不到 Electron 注入的 SV_DATA/SV_TOKEN，
    frozen 时须从自身 exe 位置复刻 Electron resolveDataRoot 的候选序列：
    exe = <安装目录>/resources/sidecar/sidecar.exe，正位=安装目录父级下
    super_video_data；≤v0.2.1 误建位=安装目录内；Program Files 不可写时
    的兜底位 = %APPDATA%/super_video_data。dev 模式直接用仓库 .tmp。
    """
    files: list[Path] = []
    if os.environ.get("SV_DATA"):  # 显式指定优先（自定义数据根的安装形态）
        files.append(Path(os.environ["SV_DATA"]) / ".tmp" / "sidecar.token")
    if getattr(sys, "frozen", False):
        exe = Path(sys.executable).resolve()
        if len(exe.parents) > 3:
            files.append(exe.parents[3] / "super_video_data" / ".tmp" / "sidecar.token")
            files.append(exe.parents[2] / "super_video_data" / ".tmp" / "sidecar.token")
        appdata = os.environ.get("APPDATA")
        if appdata:
            files.append(Path(appdata) / "super_video_data" / ".tmp" / "sidecar.token")
    else:
        files.append(TEMP_DIR / "sidecar.token")
    return files


def _token_candidates() -> list[str | None]:
    """鉴权候选序列：SV_TOKEN → token 文件（mtime 新者优先）→ None。

    None 收尾对应 sidecar 无令牌来源=不鉴权（dev `cli.py serve`）；实际能否
    通过由 sidecar 逐候选校验（_Bridge._authenticate），顺序只是命中率优化。
    """
    cands: list[str | None] = []
    env_tok = os.environ.get("SV_TOKEN")
    if env_tok:
        cands.append(env_tok)
    found: list[tuple[float, str]] = []
    for f in _token_file_candidates():
        try:
            if not f.is_file():
                continue
            tok = f.read_text(encoding="utf-8").strip()
            if tok:
                found.append((f.stat().st_mtime, tok))
        except OSError:
            continue
    # UI 每次重启都轮换令牌重写文件，mtime 最新的最可能是现行令牌
    for _mtime, tok in sorted(found, reverse=True):
        if tok not in cands:
            cands.append(tok)
    cands.append(None)
    return cands


# ---------------------------------------------------------------- 连接器

class _Bridge:
    """sidecar 连接器：发现（端口扫描）→ 鉴权（token 候选试探）→ 请求。"""

    def __init__(self) -> None:
        self.base: str | None = None
        self.token: str | None = None

    def discover(self) -> None:
        hits: list[tuple[int, str | None]] = []
        for port in _PORT_RANGE:
            try:
                resp = _http_json(f"http://127.0.0.1:{port}/api/health",
                                  "GET", None, None, timeout=2.0)
            except (ToolError, ValueError):  # 不通/非 JSON/非 dict 都跳过
                continue
            if isinstance(resp, dict) and resp.get("ok") is True:
                hits.append((port, resp.get("version")))
        if not hits:
            raise SidecarOfflineError(
                "未发现运行中的雨帧 sidecar（已扫描 127.0.0.1:8730-8739）。"
                "请先打开雨帧应用，再重试本工具。")
        # 版本一致者优先（升级后残留旧 sidecar 的场景），否则取最先命中
        port = next((p for p, v in hits if v == __version__), hits[0][0])
        self.base = f"http://127.0.0.1:{port}"
        self.token = None
        self._authenticate()

    def _authenticate(self) -> None:
        assert self.base
        for tok in _token_candidates():
            try:
                _http_json(self.base + "/api/engine", "GET", None, tok, _TIMEOUT_S)
            except SidecarOfflineError:
                raise  # sidecar 都不在线，换候选无意义
            except ToolError:
                continue  # 401（或其他 4xx）→ 试下一个候选
            self.token = tok
            return
        raise UnauthorizedError(
            "sidecar 令牌校验全部失败（SV_TOKEN / token 文件候选均被拒）。"
            "雨帧可能刚重启轮换了令牌，重试一次；仍失败请重启本 MCP 服务。")

    def call(self, method: str, path: str, body: dict | None = None) -> Any:
        if self.base is None:
            self.discover()
        assert self.base
        try:
            return _http_json(self.base + path, method, body, self.token, _TIMEOUT_S)
        except ApiError as e:
            if e.status != 401:
                raise
            # UI 重启轮换令牌（adoptReuseToken 语义）：重读候选、重新发现后
            # 重试一次；仍 401 则给明确指引
            self.discover()
            assert self.base
            try:
                return _http_json(self.base + path, method, body, self.token, _TIMEOUT_S)
            except ApiError as e2:
                if e2.status == 401:
                    raise UnauthorizedError(
                        "令牌在请求中途失效（雨帧重启）。请重试本工具。") from None
                raise
        except SidecarOfflineError:
            self.base = None  # 掉线后下次调用重新发现（重启可能换端口）
            raise

    def read(self, path: str) -> bytes:
        """Authenticated local image/text assets, with the same rotation recovery as JSON."""
        if self.base is None:
            self.discover()
        try:
            return _http_read(self.base + path, self.token, _TIMEOUT_S)
        except ApiError as error:
            if error.status != 401:
                raise
            self.discover()
            try:
                return _http_read(self.base + path, self.token, _TIMEOUT_S)
            except ApiError as retry:
                if retry.status == 401:
                    raise UnauthorizedError("令牌在读取预览时失效，请重试或重启 MCP 服务") from None
                raise
        except SidecarOfflineError:
            self.base = None
            raise


_BRIDGE = _Bridge()


# ---------------------------------------------------------------- 任务瘦身

def _slim_params(p: dict) -> dict:
    """params 瘦身：漫画批量的 images 清单可达 10 万条，整包回给 AI 会
    灌爆上下文——保留计数与前几条示例。"""
    imgs = p.get("images")
    if isinstance(imgs, list) and len(imgs) > _IMAGES_SLIM_AT:
        p = dict(p)
        p["images_count"] = len(imgs)
        p["images_preview"] = imgs[:_IMAGES_KEEP]
        del p["images"]
    return p


def _slim_task(t: dict, *, detail: bool = False) -> dict:
    """任务行瘦身：只留 AI 决策需要的字段（status 枚举/进度/输出/错误）。"""
    p = t.get("params") or {}
    out: dict = {
        "id": t.get("id"),
        "status": t.get("status"),
        "kind": p.get("kind") or p.get("out_kind") or "video",
        "model_id": t.get("model_id"),
        "input_path": t.get("input_path"),
        "output_path": t.get("output_path"),
        "progress_frames": t.get("progress_frames"),
        "total_frames": t.get("total_frames"),
        "fps_run": t.get("fps_run"),
        "eta_sec": t.get("eta_sec"),
        "error": t.get("error"),
    }
    if t.get("queue_position") is not None:
        out["queue_position"] = t["queue_position"]
    imgs = p.get("images")
    if isinstance(imgs, list):
        out["images_count"] = len(imgs)
    if detail:
        out.update({
            "created_at": t.get("created_at"),
            "elapsed_s": t.get("elapsed_s"),
            "fps_avg": t.get("fps_avg"),
            "out_bytes": t.get("out_bytes"),
            "input_exists": t.get("input_exists"),
            "has_sr_log": t.get("has_sr_log"),
            "params": _slim_params(p),
        })
    return out


# ---------------------------------------------------------------- 工具实现

def _t_status(_args: dict) -> dict:
    health = _BRIDGE.call("GET", "/api/health")
    hardware = _BRIDGE.call("GET", "/api/hardware")
    engine = _BRIDGE.call("GET", "/api/engine")
    return {"app": "雨帧 RainFrame", "version": health.get("version"),
            "hardware": hardware, "engine": engine}


def _t_probe(args: dict) -> dict:
    return _BRIDGE.call("POST", "/api/probe",
                        {"path": args["path"], "recommend": True})


def _t_models(args: dict) -> dict:
    models = _BRIDGE.call("GET", "/api/models")
    if args.get("installed_only"):
        models = [m for m in models if m.get("installed")]
    return {"count": len(models), "models": models}


def _t_model_download(args: dict) -> dict:
    mid = args["model_id"]
    r = _BRIDGE.call(
        "POST", f"/api/models/{urllib.parse.quote(mid, safe='')}/download")
    if r.get("already"):
        return {"ok": True, "message": f"模型 {mid} 已安装"}
    return {"ok": True, "message": "已开始下载（异步）",
            "hint": "用 rf_models 轮询该模型的 installed 字段直到 true"}


def _t_task_create(args: dict) -> dict:
    params: dict = {}
    for key in ("kind", "scale", "codec", "crf", "tile", "denoise", "interp"):
        v = args.get(key)
        if v is not None:
            params[key] = v
    extra = args.get("extra_params")
    if isinstance(extra, dict):
        params.update(extra)  # extra 显式值优先于便捷字段
    body: dict = {
        "input": args.get("input") or "",
        "model_id": args.get("model_id") or "",
        "params": params,
        "overwrite": args.get("overwrite", False),
    }
    if args.get("output"):
        body["output"] = args["output"]
    if args.get("inputs"):
        body["inputs"] = args["inputs"]
    elif args.get("input_folder"):
        # 大批量（漫画可达数万页）由 bridge 扫描后整单提交，清单不过 AI 上下文
        scan = _BRIDGE.call("POST", "/api/images/scan",
                            {"folder": args["input_folder"]})
        paths = [f["path"] for f in scan.get("files") or []]
        if not paths:
            raise ToolError(f"文件夹 {args['input_folder']} 中没有图片")
        body["inputs"] = paths
        params.setdefault("folder_src", scan.get("folder") or args["input_folder"])
    if not body["model_id"]:
        from .pipeline.recommend import DEFAULT_ANIME_MODEL, DEFAULT_MANGA_MODEL, DEFAULT_COLOR_MODEL
        content = args.get("content_type") or ("manga_bw" if params.get("kind") == "manga" else "anime")
        body["model_id"] = {"anime": DEFAULT_ANIME_MODEL, "manga_bw": DEFAULT_MANGA_MODEL,
                            "manga_color": DEFAULT_COLOR_MODEL}[content]
        params.setdefault("scale", 4 if content == "manga_color" else 2)
        if content.startswith("manga_"):
            params.setdefault("kind", "manga")
    task = _BRIDGE.call("POST", "/api/tasks", body)
    out = _slim_task(task)
    out["hint"] = ("任务已创建。用 rf_task 轮询进度"
                   "（status: queued→running→done/failed/canceled）")
    return out


def _t_tasks(args: dict) -> dict:
    path = "/api/tasks"
    if args.get("q"):
        path += "?" + urllib.parse.urlencode({"q": args["q"]})
    tasks = _BRIDGE.call("GET", path)
    if args.get("status"):
        tasks = [t for t in tasks if t.get("status") == args["status"]]
    offset, limit = args.get("offset", 0), args.get("limit", _MAX_LIST)
    res: dict = {"total_matching": len(tasks), "offset": offset, "limit": limit,
                 "tasks": [_slim_task(t) for t in tasks[offset:offset + limit]],
                 "has_more": offset + limit < len(tasks)}
    if res["has_more"]:
        res["truncated"] = True
        res["next_offset"] = offset + limit
        res["hint"] = "用 next_offset 作为 offset 继续读取；status/q/limit 保持一致"
    return res


def _t_task(args: dict) -> dict:
    tid = urllib.parse.quote(args["task_id"], safe="")
    return _slim_task(_BRIDGE.call("GET", f"/api/tasks/{tid}"), detail=True)


def _t_task_cancel(args: dict) -> dict:
    tid = urllib.parse.quote(args["task_id"], safe="")
    return _BRIDGE.call("POST", f"/api/tasks/{tid}/cancel")


def _t_task_resume(args: dict) -> dict:
    tid = urllib.parse.quote(args["task_id"], safe="")
    r = _BRIDGE.call("POST", f"/api/tasks/{tid}/resume")
    if r.get("ok"):
        r = dict(r)
        r["hint"] = "已重新排队，用 rf_task 轮询"
    return r


def _t_scan_folder(args: dict) -> dict:
    r = _BRIDGE.call("POST", "/api/images/scan", {"folder": args["folder"]})
    files = r.get("files") or []
    out: dict = {
        "folder": r.get("folder"),
        "total": r.get("total"),
        "dirs": r.get("dirs"),
        "files_preview": [f.get("path") for f in files[:_MAX_SCAN_FILES]],
    }
    if len(files) > _MAX_SCAN_FILES:
        out["truncated"] = True
        out["hint"] = (f"共 {len(files)} 个文件，仅预览前 {_MAX_SCAN_FILES} 个。"
                       "整夹建任务直接用 rf_task_create 的 input_folder，无需逐个传路径")
    return out


def _watermark_options(args: dict) -> dict:
    body = {"mode": args.get("mode", "fixed"), "removal": args.get("removal", "auto")}
    for key in ("mask", "sample", "threshold"):
        if args.get(key) is not None:
            body[key] = args[key]
    return body


def _t_watermark_preview(args: dict) -> _ToolContent:
    body = {"path": args["path"], **_watermark_options(args)}
    result = dict(_BRIDGE.call("POST", "/api/watermark/preview", body))
    images = [(key, result.pop(key, None)) for key in ("original", "processed")]
    result["hint"] = ("预览未处理该图；根据 reason 调整区域、样本或清除方式。" if result.get("detected") is False
                      else "检查原图和处理后预览；整批处理用 rf_watermark_batch，并沿用相同参数。")
    content = _ToolContent([{"type": "text", "text": json.dumps(result, ensure_ascii=False)}])
    if args.get("include_images", True):
        for key, url in images:
            if not isinstance(url, str) or not url.startswith("data:image/png;base64,"):
                raise ToolError("后端未返回有效的 PNG 预览，请检查后端版本")
            content.append({"type": "text", "text": "原图" if key == "original" else "处理后预览"})
            content.append({"type": "image", "mimeType": "image/png", "data": url.split(",", 1)[1]})
    return content


def _slim_watermark_job(result: dict) -> dict:
    out = dict(result)
    errors = out.get("errors") or []
    out["errors"] = errors[:_MAX_WATERMARK_ERRORS]
    out["error_count"] = len(errors)
    if len(errors) > _MAX_WATERMARK_ERRORS:
        out["errors_truncated"] = True
        out["report_hint"] = "完整逐张记录见输出目录中的 watermark-report.json（智能定位或增强清除模式）"
    return out


def _t_watermark_batch(args: dict) -> dict:
    body = _watermark_options(args)
    if args.get("folder") is not None:
        body["folder"] = args["folder"]
    if args.get("output_dir") is not None:
        body["output_dir"] = args["output_dir"]
    if "paths" in args:
        body["paths"] = args["paths"]
    elif args.get("folder"):
        scan = _BRIDGE.call("POST", "/api/images/scan", {"folder": args["folder"]})
        body["folder"] = scan.get("folder") or args["folder"]
        body["paths"] = [item["path"] for item in scan.get("files") or []]
        if not body["paths"]:
            raise ToolError(f"文件夹 {args['folder']} 中没有图片")
    else:
        raise ToolError("请提供 paths 图片路径列表或 folder 图片文件夹")
    result = _slim_watermark_job(_BRIDGE.call("POST", "/api/watermark/batch", body))
    result["hint"] = "用 rf_watermark_job 和返回的 id 查询进度；用 rf_watermark_cancel 停止。原图保留，结果另存 PNG。"
    return result


def _t_watermark_job(args: dict) -> dict:
    job_id = urllib.parse.quote(args["job_id"], safe="")
    return _slim_watermark_job(_BRIDGE.call("GET", f"/api/watermark/batch/{job_id}"))


def _t_watermark_cancel(args: dict) -> dict:
    job_id = urllib.parse.quote(args["job_id"], safe="")
    result = _slim_watermark_job(_BRIDGE.call("POST", f"/api/watermark/batch/{job_id}/cancel"))
    result["hint"] = "已请求停止，当前图片处理完后结束；用 rf_watermark_job 确认 status=cancelled。"
    return result


def _image_block(data: bytes) -> dict:
    from PIL import Image, ImageOps
    try:
        with Image.open(io.BytesIO(data)) as source:
            image = ImageOps.exif_transpose(source)
            image.thumbnail((1200, 1200), Image.Resampling.LANCZOS)
            if image.mode not in ("RGB", "RGBA", "L", "LA"):
                image = image.convert("RGB")
            buffer = io.BytesIO()
            image.save(buffer, format="PNG")
    except (OSError, ValueError, Image.DecompressionBombError) as error:
        raise ToolError(f"无法读取预览图像: {error}") from None
    return {"type": "image", "mimeType": "image/png",
            "data": base64.b64encode(buffer.getvalue()).decode("ascii")}


def _image_pair(metadata: dict, paths: list[tuple[str, str]]) -> _ToolContent:
    blocks = []
    missing = []
    for label, path in paths:
        try:
            block = _image_block(_BRIDGE.read(path))
        except ApiError as error:
            if error.status != 404:
                raise
            missing.append(label)
            continue
        blocks.extend([{"type": "text", "text": label}, block])
    if missing:
        metadata["missing"] = missing
        metadata["hint"] = "部分预览暂不可用，可能尚未生成或产物已不存在。"
    if not blocks:
        raise ToolError("暂无可用预览，图片可能尚未生成或产物已不存在")
    return _ToolContent([{"type": "text", "text": json.dumps(metadata, ensure_ascii=False)}, *blocks])


def _t_task_preview(args: dict) -> _ToolContent:
    task_id = urllib.parse.quote(args["task_id"], safe="")
    if "sample_index" in args:
        status = _BRIDGE.call("GET", f"/api/tasks/{task_id}/stills")
        if status.get("status") != "ready":
            return _ToolContent([{"type": "text", "text": json.dumps({
                **status, "hint": "静帧尚未就绪时稍后重试；unsupported 时省略 sample_index 使用普通预览。"
            }, ensure_ascii=False)}])
        if args["sample_index"] >= status.get("count", 0):
            raise ToolError("静帧索引超出此任务的 count")
        stem = f"/api/tasks/{task_id}/stills/{args['sample_index']}"
    else:
        stem = f"/api/tasks/{task_id}/preview"
    return _image_pair({"task_id": args["task_id"], "sample_index": args.get("sample_index"),
                        "preview_max_edge": 1200}, [("源图", stem + "?src=1"), ("处理后", stem)])


def _t_diagnostics(args: dict) -> dict:
    result = {"queue": _BRIDGE.call("GET", "/api/stats")}
    history = _BRIDGE.call("GET", "/api/perf/history")
    result["performance"] = {"interval_s": history.get("interval_s"),
                             "samples": (history.get("samples") or [])[-args.get("perf_limit", 20):]}
    count = args.get("log_lines", 80)
    logs = _BRIDGE.call("GET", "/api/log-tail?" + urllib.parse.urlencode({"n": count}))
    # Also bound very long individual lines, not only the line count.
    result["log_lines"] = [str(line)[-2000:] for line in (logs.get("lines") or [])[-count:]]
    if args.get("task_id"):
        result["task"] = _t_task(args)
        if result["task"].get("has_sr_log"):
            tid = urllib.parse.quote(args["task_id"], safe="")
            try:
                text = _BRIDGE.read(f"/api/tasks/{tid}/sr-log?n={count}").decode("utf-8", "replace")
                result["sr_log_lines"] = [line[-2000:] for line in text.splitlines()[-count:]]
            except ApiError as error:
                if error.status != 404:
                    raise
                result["sr_log_unavailable"] = True
    return result


def _t_compare_create(args: dict) -> dict:
    body = {key: args[key] for key in ("kind", "input", "models", "scale")}
    for key in ("start_s", "end_s"):
        if key in args:
            body[key] = args[key]
    result = dict(_BRIDGE.call("POST", "/api/compare", body))
    result["hint"] = "用 rf_compare_job 查询作业进度，模型结果就绪后用 rf_compare_preview 查看；停止用 rf_compare_cancel。"
    return result


def _t_compare_job(args: dict) -> dict:
    jid = urllib.parse.quote(args["job_id"], safe="")
    return _BRIDGE.call("GET", f"/api/compare/{jid}")


def _t_compare_cancel(args: dict) -> dict:
    jid = urllib.parse.quote(args["job_id"], safe="")
    result = dict(_BRIDGE.call("POST", f"/api/compare/{jid}/cancel"))
    result["hint"] = "在模型之间停止，当前模型可能仍在处理；用 rf_compare_job 确认终态。"
    return result


def _t_compare_preview(args: dict) -> _ToolContent:
    job = _t_compare_job(args)
    mid = args["model_id"]
    if not any(entry.get("model_id") == mid for entry in job.get("entries") or []):
        raise ToolError("该模型不在此对比作业中")
    jid = urllib.parse.quote(args["job_id"], safe="")
    index = args.get("sample_index", 0)
    if job.get("kind") == "video":
        if index >= job.get("still_count", 1):
            raise ToolError("静帧索引超出此作业的 still_count")
        src, out = f"src_still/{index}", f"still/{mid}/{index}"
    else:
        src, out = "src_still", f"still/{mid}"
    base = f"/api/compare/{jid}/asset/"
    return _image_pair({"job_id": args["job_id"], "model_id": mid,
                        "sample_index": index, "preview_max_edge": 1200}, [
        ("源图", base + urllib.parse.quote(src, safe="/")),
        ("模型结果", base + urllib.parse.quote(out, safe="/"))])


def _t_trim_create(args: dict) -> dict:
    body = {key: args[key] for key in ("input", "start_s", "end_s")}
    for key in ("mode", "output", "overwrite"):
        if key in args:
            body[key] = args[key]
    result = dict(_BRIDGE.call("POST", "/api/trim", body))
    result["hint"] = "用 rf_trim_job 查询 state（queued/running/done/failed/canceled）；done 后 output 可作为超分输入。"
    return result


def _t_trim_job(args: dict) -> dict:
    jid = urllib.parse.quote(args["job_id"], safe="")
    return _BRIDGE.call("GET", f"/api/trim/{jid}")


def _t_trim_cancel(args: dict) -> dict:
    jid = urllib.parse.quote(args["job_id"], safe="")
    return _BRIDGE.call("POST", f"/api/trim/{jid}/cancel")


_WATERMARK_MASK_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "description": "以右下角为基准的区域，需先预览检查。percent 为图片宽高的百分比。",
    "properties": {
        "unit": {"type": "string", "enum": ["px", "percent"], "default": "px"},
        "width": {"type": "number", "exclusiveMinimum": 0, "maximum": 100000, "default": 175},
        "height": {"type": "number", "exclusiveMinimum": 0, "maximum": 100000, "default": 75},
        "right": {"type": "number", "minimum": 0, "maximum": 100000, "default": 0},
        "bottom": {"type": "number", "minimum": 0, "maximum": 100000, "default": 0},
    },
}
_WATERMARK_PROPERTIES = {
    "mode": {"type": "string", "enum": ["fixed", "smart"], "default": "fixed",
             "description": "fixed 沿用框选区域；smart 在右下角自动寻找同款水印，必须提供 sample"},
    "removal": {"type": "string", "enum": ["auto", "repair", "white"], "default": "auto",
                "description": "auto 自动填白/填黑，混有画面时跳过；repair 纯色填充或局部修补（可能模糊）；white 整个矩形填白"},
    "mask": _WATERMARK_MASK_SCHEMA,
    "sample": {"type": "object", "additionalProperties": False,
               "description": "纯色页边上的同款水印及少量空白。smart 必填；fixed+repair 可选，按文字生成遮罩。不同样式需分批。",
               "properties": {"path": {"type": "string", "description": "水印样本原图的本机绝对路径"},
                              "mask": _WATERMARK_MASK_SCHEMA}, "required": ["path", "mask"]},
    "threshold": {"type": "number", "minimum": .80, "maximum": .99, "default": .88,
                  "description": "智能定位最低匹配度，匹配不足或有多个相近候选时跳过"},
}


# name, 描述, inputSchema, handler——描述面向 AI 客户端，写清前置条件与后续动作
_TOOLS: list[tuple[str, str, dict, Callable[[dict], Any]]] = [
    ("rf_status",
     "查看雨帧（RainFrame，视频/图片/漫画超分工具）整体状态：版本、GPU 与硬件能力、"
     "当前推理引擎。调用其他工具前先看一眼确认后端在线。",
     {"type": "object", "properties": {}}, _t_status),
    ("rf_probe",
     "探测视频/图片文件信息：分辨率、时长、帧率、编码、音轨等，并附带智能推荐"
     "（推荐模型、倍率、反交错/去色带等预处理及理由）。创建超分任务前先用它定参数。",
     {"type": "object",
      "properties": {"path": {"type": "string", "description": "本机绝对路径"}},
      "required": ["path"]}, _t_probe),
    ("rf_models",
     "列出模型注册表：id、名称、权重版本、主用途 category、倍率、适用场景（视频/漫画/图片）、速度档、显存要求、"
     "是否已安装。installed_only=true 只看已安装。",
     {"type": "object",
      "properties": {"installed_only": {"type": "boolean",
                                        "description": "只列已安装的模型"}},
      }, _t_models),
    ("rf_model_download",
     "启动模型下载（异步，立即返回）。用 rf_models 轮询该模型的 installed 字段确认完成。",
     {"type": "object",
      "properties": {"model_id": {"type": "string", "description": "模型 id，见 rf_models"}},
      "required": ["model_id"]}, _t_model_download),
    ("rf_task_create",
     "创建超分任务。视频传 input 单文件；图片批量传 inputs 列表或 input_folder 整夹扫描"
     "（漫画建议 kind=manga + input_folder）。常用可选参数：scale 倍率、codec h264/h265 或硬件编码、"
     "crf、tile 分块、denoise 去噪档。输出路径冲突时返回 409，确认覆盖后以 overwrite=true "
     "重交。成功立即返回任务 id，用 rf_task 轮询进度。其余任务参数（target_w/target_h、"
     "deinterlace、deband、folder_src、merge_pdf 等）经 extra_params 透传。",
     {"type": "object",
      "properties": {
          "input": {"type": "string", "description": "单个输入文件路径（视频或单张图片）"},
          "inputs": {"type": "array", "minItems": 1, "maxItems": 100000, "items": {"type": "string"},
                     "description": "图片批量输入路径列表（提供时忽略 input/input_folder）"},
          "input_folder": {"type": "string",
                           "description": "扫描整个文件夹建批量图片/漫画任务（自动枚举，清单不占上下文）"},
          "model_id": {"type": "string", "description": "可选；显式模型优先。动漫默认 AnimeJaNai V3.1 Balanced Sharp，黑白漫画 MangaJaNai，彩漫 IllustrationJaNai DAT2"},
          "content_type": {"type": "string", "enum": ["anime", "manga_bw", "manga_color"],
                           "description": "省略 model_id 时决定默认模型；kind=manga 默认黑白漫画，否则默认动漫。彩漫指定 manga_color"},
          "output": {"type": "string", "description": "输出路径（省略则按应用默认规则命名）"},
          "kind": {"type": "string", "enum": ["image", "manga"],
                   "description": "图片任务语义：manga=漫画页（走漫画优化路径）"},
          "scale": {"type": "integer", "description": "超分倍率（模型可用档位见 rf_models）"},
          "codec": {"type": "string", "enum": list(_CODECS), "description": "输出编码；软编 H.265 为 h265，硬编需 rf_status 对应能力可用"},
          "crf": {"type": "integer", "minimum": 0, "maximum": 51, "description": "质量档（默认 18）"},
          "tile": {"type": "integer", "minimum": 0, "maximum": 4096, "description": "分块尺寸（显存不足时调小；0=auto，非零需为 64~4096 的偶数）"},
          "denoise": {"type": "integer", "description": "去噪级别（模型支持时）"},
          "interp": {"type": "string", "enum": ["off", "rife2x"], "description": "补帧设置（实验性，probe 未推荐勿开）"},
          "overwrite": {"type": "boolean", "description": "确认覆盖已存在的输出（收到 409 后置 true 重交）"},
          "extra_params": {"type": "object",
                           "description": "透传其他任务参数。视频字幕烧录：subtitle_mode=burn；烧录并保留原字幕轨用 burn_keep。subtitle={source:external,path:字幕路径,delay_s:0}；指定内嵌文本轨用 source:embedded,stream:0；批量内嵌用 source:embedded,selection:match,language:zh,title:简体，按语言和标题包含匹配唯一文本轨，不依赖轨道序号；同名外挂用 source:matching。支持 SRT/ASS/SSA，还可含 encoding、font_name、font_size、font_color(#RRGGBB)、outline、shadow、margin_v、fonts_dir。其它参数：target_w/target_h、deinterlace、deband、folder_src、merge_pdf、pdf_out、format、model_id_color、mix_pass 等"},
      },
      "anyOf": [{"required": ["input"]}, {"required": ["inputs"]}, {"required": ["input_folder"]}]}, _t_task_create),
    ("rf_tasks",
     "分页列出任务，可按 status 过滤或 q 搜索。默认每页 30 条，用 next_offset 继续；列表变化时分页位置可能变化。",
     {"type": "object",
      "properties": {
          "status": {"type": "string",
                     "enum": ["queued", "running", "done", "failed", "canceled"]},
          "q": {"type": "string", "description": "搜索词（路径/模型名等）"},
          "offset": {"type": "integer", "minimum": 0, "default": 0},
          "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 30},
      }}, _t_tasks),
    ("rf_task",
     "查看任务详情与进度：status、已完成帧数/总帧数、速度、ETA、输出路径、错误信息。"
     "创建任务后轮询本工具直到 status 变为 done/failed/canceled。",
     {"type": "object",
      "properties": {"task_id": {"type": "string"}},
      "required": ["task_id"]}, _t_task),
    ("rf_task_cancel",
     "取消一个 queued/running 任务（保留现场，之后可 rf_task_resume 续跑）。",
     {"type": "object",
      "properties": {"task_id": {"type": "string"}},
      "required": ["task_id"]}, _t_task_cancel),
    ("rf_task_resume",
     "重跑一个 failed/canceled 任务（断点续跑；输入或断点数据缺失会返回 409 及原因）。",
     {"type": "object",
      "properties": {"task_id": {"type": "string"}},
      "required": ["task_id"]}, _t_task_resume),
    ("rf_scan_folder",
     "扫描文件夹的图片清单（漫画批量第一步）：返回总数与前 20 个文件。"
     "整夹建任务用 rf_task_create 的 input_folder，无需把清单传回。",
     {"type": "object",
      "properties": {"folder": {"type": "string", "description": "文件夹绝对路径"}},
      "required": ["folder"]}, _t_scan_folder),
    ("rf_watermark_preview",
     "预览单张图片去水印，返回原图与处理后 PNG 图像，以及实际清除方式、定位框、匹配分数或跳过原因。"
     "支持固定区域或智能定位、白底/黑底自动识别、局部修补和样本文字遮罩。"
     "先检查预览再提交批量；repair 可能留下模糊。不会写入或覆盖原图。",
     {"type": "object", "additionalProperties": False,
      "properties": {"path": {"type": "string", "description": "图片的本机绝对路径"},
                     **_WATERMARK_PROPERTIES,
                     "include_images": {"type": "boolean", "default": True,
                                        "description": "是否返回原图与处理后图像；false 仅返回定位和处理信息"}},
      "required": ["path"]}, _t_watermark_preview),
    ("rf_watermark_batch",
     "批量图片去水印：传 paths 列表或 folder 整夹递归扫描，整夹清单不会返回给 AI。"
     "沿用 rf_watermark_preview 验证过的参数。异步返回独立去水印作业 id，"
     "用 rf_watermark_job 查询进度或 rf_watermark_cancel 停止。保留原图和目录结构，另存 PNG。",
     {"type": "object", "additionalProperties": False,
      "properties": {**_WATERMARK_PROPERTIES,
                     "paths": {"type": "array", "minItems": 1, "maxItems": 100000,
                               "items": {"type": "string"}, "description": "图片绝对路径列表，提供时不扫描 folder"},
                     "folder": {"type": "string", "description": "源文件夹；未传 paths 时递归扫描，传 paths 时用于保留相对目录结构"},
                     "output_dir": {"type": "string", "description": "输出父目录；后端在其下创建独立结果文件夹，同名自动避让"}},
      "anyOf": [{"required": ["paths"]}, {"required": ["folder"]}]}, _t_watermark_batch),
    ("rf_watermark_job",
     "查询去水印作业进度：status、total、completed、succeeded、skipped、failed、current、输出目录与错误原因。"
     "轮询至 done/cancelled；done 后仍需检查 failed/skipped。错误最多返回前 20 条。此 id 不能用于 rf_task。",
     {"type": "object", "properties": {"job_id": {"type": "string"}},
      "required": ["job_id"]}, _t_watermark_job),
    ("rf_watermark_cancel",
     "请求停止独立的去水印作业；当前图片完成后停止，已有结果保留。"
     "用 rf_watermark_job 确认 cancelled。停止后的作业不支持 rf_task_resume。",
     {"type": "object", "properties": {"job_id": {"type": "string"}},
      "required": ["job_id"]}, _t_watermark_cancel),
    ("rf_diagnostics",
     "读取队列统计与处理闸门状态、最近性能样本及后端日志尾。可传 task_id 附带任务详情和性能日志尾，辅助诊断失败或卡顿；只读，不修改设置。",
     {"type": "object", "additionalProperties": False, "properties": {
         "task_id": {"type": "string"},
         "log_lines": {"type": "integer", "minimum": 1, "maximum": 200, "default": 80},
         "perf_limit": {"type": "integer", "minimum": 1, "maximum": 60, "default": 20},
     }}, _t_diagnostics),
    ("rf_task_preview",
     "返回超分任务的源图与结果预览（MCP 原生 PNG，最长边 1200 像素）。省略 sample_index 取普通预览；提供零起始索引则使用多帧静帧，首次可能触发后台构建并返回 building，请稍后重试。",
     {"type": "object", "additionalProperties": False, "properties": {
         "task_id": {"type": "string"}, "sample_index": {"type": "integer", "minimum": 0}},
      "required": ["task_id"]}, _t_task_preview),
    ("rf_compare_create",
     "创建同一图片或视频片段的多模型对比，2~6 个不同模型、共同支持的倍率；torch 模型暂不参与。视频必须提供 start_s/end_s，超过 20 秒会截短。异步返回独立作业 id，用 rf_compare_job 查询。",
     {"type": "object", "additionalProperties": False, "properties": {
         "kind": {"type": "string", "enum": ["image", "video"]},
         "input": {"type": "string"},
         "models": {"type": "array", "items": {"type": "string"}, "minItems": 2, "maxItems": 6, "uniqueItems": True},
         "scale": {"type": "integer", "minimum": 1},
         "start_s": {"type": "number", "minimum": 0}, "end_s": {"type": "number", "exclusiveMinimum": 0}},
      "required": ["kind", "input", "models", "scale"]}, _t_compare_create),
    ("rf_compare_job",
     "查询独立模型对比作业：status=queued/running/done/failed/canceled、entries 各模型进度/速度/错误、still_count。模型结果就绪后用 rf_compare_preview 查看；此 id 不用于 rf_task。",
     {"type": "object", "properties": {"job_id": {"type": "string"}}, "required": ["job_id"]}, _t_compare_job),
    ("rf_compare_cancel",
     "请求停止模型对比，在模型之间停止，当前模型会完成；用 rf_compare_job 确认终态，已有结果保留。",
     {"type": "object", "properties": {"job_id": {"type": "string"}}, "required": ["job_id"]}, _t_compare_cancel),
    ("rf_compare_preview",
     "查看对比作业中某模型的源图/结果图，返回 MCP 原生 PNG，最长边 1200 像素。视频用 sample_index（默认 0，需小于 still_count）取同一时间的静帧；图片取成品图。结果未生成时提示或返回可用的一侧。",
     {"type": "object", "additionalProperties": False, "properties": {
         "job_id": {"type": "string"}, "model_id": {"type": "string"},
         "sample_index": {"type": "integer", "minimum": 0, "default": 0}},
      "required": ["job_id", "model_id"]}, _t_compare_preview),
    ("rf_trim_create",
     "创建视频剪切作业：start_s/end_s 为秒，smart 智能精确剪切，fast 关键帧剪切，exact 重编码。输出冲突返回 409，仅在用户已明确同意时传 overwrite=true。异步返回 job_id，用 rf_trim_job 查询，产物可作为超分输入。",
     {"type": "object", "additionalProperties": False, "properties": {
         "input": {"type": "string"}, "start_s": {"type": "number", "minimum": 0},
         "end_s": {"type": "number", "exclusiveMinimum": 0},
         "mode": {"type": "string", "enum": ["smart", "fast", "exact"], "default": "smart"},
         "output": {"type": "string"}, "overwrite": {"type": "boolean", "default": False}},
      "required": ["input", "start_s", "end_s"]}, _t_trim_create),
    ("rf_trim_job",
     "查询视频剪切作业 state=queued/running/done/failed/canceled、progress、output 和 error；done 后 output 为可用产物路径。此 job_id 独立于超分任务。",
     {"type": "object", "properties": {"job_id": {"type": "string"}}, "required": ["job_id"]}, _t_trim_job),
    ("rf_trim_cancel",
     "停止排队或正在剪切的视频作业，用 rf_trim_job 确认终态；已结束时后端返回 409。",
     {"type": "object", "properties": {"job_id": {"type": "string"}}, "required": ["job_id"]}, _t_trim_cancel),
]


# ---------------------------------------------------------------- 协议层

class _RpcError(Exception):
    """JSON-RPC 协议级错误（-32601/-32602 等）。"""

    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _validate_args(value: Any, schema: dict, path: str = "arguments") -> None:
    """Validate the JSON-schema subset used by tools without coercing values."""
    kind = schema.get("type")
    valid = {"object": isinstance(value, dict), "array": isinstance(value, list),
             "string": isinstance(value, str), "boolean": isinstance(value, bool),
             "integer": type(value) is int, "number": type(value) in (int, float)}
    if kind and not valid.get(kind, False):
        raise _RpcError(-32602, f"{path} 必须是 {kind}，不接受自动类型转换")
    if "enum" in schema and value not in schema["enum"]:
        raise _RpcError(-32602, f"{path} 必须取 {schema['enum']}")
    if type(value) in (int, float):
        if type(value) is float and not math.isfinite(value):
            raise _RpcError(-32602, f"{path} 必须是有限数值")
        for key, invalid in (("minimum", value < schema.get("minimum", -math.inf)),
                             ("maximum", value > schema.get("maximum", math.inf)),
                             ("exclusiveMinimum", value <= schema.get("exclusiveMinimum", -math.inf))):
            if invalid:
                raise _RpcError(-32602, f"{path} 不符合 {key}={schema[key]}")
    if isinstance(value, dict):
        for key in schema.get("required", []):
            if key not in value:
                raise _RpcError(-32602, f"缺少必填参数: {path}.{key}")
        if "anyOf" in schema and not any(all(key in value for key in branch.get("required", [])) for branch in schema["anyOf"]):
            raise _RpcError(-32602, f"{path} 需满足 {schema['anyOf']}")
        properties = schema.get("properties", {})
        for key, item in value.items():
            if key in properties:
                _validate_args(item, properties[key], f"{path}.{key}")
            elif schema.get("additionalProperties") is False:
                raise _RpcError(-32602, f"未知参数: {path}.{key}")
    if isinstance(value, list):
        if len(value) < schema.get("minItems", 0) or len(value) > schema.get("maxItems", math.inf):
            raise _RpcError(-32602, f"{path} 列表长度不符合要求")
        if schema.get("uniqueItems") and len({json.dumps(v, sort_keys=True) for v in value}) != len(value):
            raise _RpcError(-32602, f"{path} 不允许重复项")
        for index, item in enumerate(value):
            _validate_args(item, schema.get("items", {}), f"{path}[{index}]")


def _error(msg_id: Any, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": msg_id,
            "error": {"code": code, "message": message}}


def _handle(method: str, params: dict) -> dict:
    if method == "initialize":
        want = params.get("protocolVersion")
        chosen = want if want in _SUPPORTED_PROTOCOLS else _SUPPORTED_PROTOCOLS[-1]
        return {
            "protocolVersion": chosen,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "rainframe", "version": __version__},
        }
    if method == "ping":
        return {}
    if method == "tools/list":
        return {"tools": [{"name": n, "description": d, "inputSchema": s}
                          for n, d, s, _fn in _TOOLS]}
    if method == "tools/call":
        name = params.get("name")
        tool = next((entry for entry in _TOOLS if entry[0] == name), None)
        if tool is None:
            raise _RpcError(-32602, f"未知工具 {name!r}（tools/list 查看可用工具）")
        args = params.get("arguments", {})
        if not isinstance(args, dict):
            raise _RpcError(-32602, "arguments 必须是对象")
        _validate_args(args, tool[2])
        try:
            out = tool[3](args)
        except KeyError as e:
            raise _RpcError(-32602, f"缺少必填参数: {e.args[0]}") from None
        except ToolError as e:
            # 工具执行失败≠协议错误：MCP 约定用 isError 让客户端读错误文本
            return {"content": [{"type": "text", "text": str(e)}], "isError": True}
        if isinstance(out, _ToolContent):
            return {"content": list(out)}
        return {"content": [{"type": "text",
                             "text": json.dumps(out, ensure_ascii=False)}]}
    raise _RpcError(-32601, f"method not found: {method}")


def dispatch(msg: object) -> dict | None:
    """处理一条已解析的 JSON-RPC 消息，返回应答（通知/无 id 返回 None）。"""
    if not isinstance(msg, dict):
        return _error(None, -32600, "invalid request: 消息必须是对象")
    method = msg.get("method")
    if not isinstance(method, str):
        return None  # 本服务端只收请求/通知，客户端不会发响应类消息
    if method.startswith("notifications/") or "id" not in msg:
        return None  # 通知一律不答（initialized / cancelled / 进度通知等）
    msg_id = msg.get("id")
    try:
        result = _handle(method, msg.get("params") or {})
    except _RpcError as e:
        return _error(msg_id, e.code, e.message)
    except Exception as e:  # noqa: BLE001 — 协议层不能崩，任何异常都转错误应答
        return _error(msg_id, -32603, f"internal error: {type(e).__name__}: {e}")
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def _process_line(line: str) -> str | None:
    """处理 stdin 一行，返回应写的应答行（None=不答）。"""
    line = line.strip()
    if not line:
        return None
    try:
        msg = json.loads(line)
    except ValueError:
        return json.dumps(_error(None, -32700, "parse error: 非法 JSON"),
                          ensure_ascii=False)
    resp = dispatch(msg)
    if resp is None:
        return None
    return json.dumps(resp, ensure_ascii=False)


def _serve(stdin, stdout) -> None:
    for line in stdin:
        out = _process_line(line)
        if out is not None:
            try:
                stdout.write(out + "\n")
                stdout.flush()
            except OSError:
                # 写管道失败=客户端已关。Windows 上关掉的管道抛 EINVAL(22)
                # 而非 EPIPE，靠 BrokenPipeError 捕不到，统一按 OSError 处理
                return


def main() -> int:
    # stdin 必须 UTF-8：MCP 客户端按协议发 UTF-8 JSON，Windows 管道默认 locale
    # （GBK）解码会让中文路径静默变乱码。cli.main 只重配了 stdout/stderr，
    # 所以这里三个流都配（重复调用幂等，兼容两条入口路径）。
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass
    try:
        _serve(sys.stdin, sys.stdout)
    except BrokenPipeError:
        pass  # 客户端退出即关管道，属正常结束
    except Exception as e:  # noqa: BLE001 — 兜底，错误码非 0 便于客户端诊断
        print(f"[mcp] 致命错误: {type(e).__name__}: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
