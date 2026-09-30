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
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable

from . import __version__
from .paths import TEMP_DIR

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
        "model_id": args["model_id"],
        "params": params,
        "overwrite": bool(args.get("overwrite")),
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
    res: dict = {"total_matching": len(tasks),
                 "tasks": [_slim_task(t) for t in tasks[:_MAX_LIST]]}
    if len(tasks) > _MAX_LIST:
        res["truncated"] = True
        res["hint"] = f"仅显示前 {_MAX_LIST} 条，可用 status/q 收窄"
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
     "列出模型注册表：id、名称、倍率、适用场景（视频/漫画/图片）、速度档、显存要求、"
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
     "（漫画建议 kind=manga + input_folder）。常用可选参数：scale 倍率、codec h264/hevc、"
     "crf、tile 分块、denoise 去噪档。输出路径冲突时返回 409，确认覆盖后以 overwrite=true "
     "重交。成功立即返回任务 id，用 rf_task 轮询进度。其余任务参数（target_w/target_h、"
     "deinterlace、deband、folder_src、merge_pdf 等）经 extra_params 透传。",
     {"type": "object",
      "properties": {
          "input": {"type": "string", "description": "单个输入文件路径（视频或单张图片）"},
          "inputs": {"type": "array", "items": {"type": "string"},
                     "description": "图片批量输入路径列表（提供时忽略 input/input_folder）"},
          "input_folder": {"type": "string",
                           "description": "扫描整个文件夹建批量图片/漫画任务（自动枚举，清单不占上下文）"},
          "model_id": {"type": "string", "description": "模型 id，见 rf_models"},
          "output": {"type": "string", "description": "输出路径（省略则按应用默认规则命名）"},
          "kind": {"type": "string", "enum": ["image", "manga"],
                   "description": "图片任务语义：manga=漫画页（走漫画优化路径）"},
          "scale": {"type": "integer", "description": "超分倍率（模型可用档位见 rf_models）"},
          "codec": {"type": "string", "enum": ["h264", "hevc"], "description": "输出编码"},
          "crf": {"type": "integer", "description": "质量档（默认 18）"},
          "tile": {"type": "integer", "description": "分块尺寸（显存不足时调大；0=auto）"},
          "denoise": {"type": "integer", "description": "去噪级别（模型支持时）"},
          "interp": {"type": "string", "description": "补帧设置（实验性，probe 未推荐勿开）"},
          "overwrite": {"type": "boolean", "description": "确认覆盖已存在的输出（收到 409 后置 true 重交）"},
          "extra_params": {"type": "object",
                           "description": "透传其他任务参数（target_w/target_h、deinterlace、deband、folder_src、merge_pdf、pdf_out、format、model_id_color、mix_pass 等）"},
      },
      "required": ["model_id"]}, _t_task_create),
    ("rf_tasks",
     "列出任务，可按 status 过滤（queued/running/done/failed/canceled）、q 搜索。最多回 30 条摘要。",
     {"type": "object",
      "properties": {
          "status": {"type": "string",
                     "enum": ["queued", "running", "done", "failed", "canceled"]},
          "q": {"type": "string", "description": "搜索词（路径/模型名等）"},
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
]


# ---------------------------------------------------------------- 协议层

class _RpcError(Exception):
    """JSON-RPC 协议级错误（-32601/-32602 等）。"""

    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


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
        fn = next((f for n, _d, _s, f in _TOOLS if n == name), None)
        if fn is None:
            raise _RpcError(-32602, f"未知工具 {name!r}（tools/list 查看可用工具）")
        args = params.get("arguments") or {}
        if not isinstance(args, dict):
            raise _RpcError(-32602, "arguments 必须是对象")
        try:
            out = fn(args)
        except KeyError as e:
            raise _RpcError(-32602, f"缺少必填参数: {e.args[0]}") from None
        except ToolError as e:
            # 工具执行失败≠协议错误：MCP 约定用 isError 让客户端读错误文本
            return {"content": [{"type": "text", "text": str(e)}], "isError": True}
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
