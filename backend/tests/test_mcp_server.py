"""MCP stdio bridge 测试：协议层纯函数 + 假 bridge / 假 HTTP，不碰真 socket 与 GPU。"""
import json
import os
import urllib.parse
from pathlib import Path

import pytest

from sv import mcp_server as ms

# ---------------------------------------------------------------- 假件与工具


class FakeBridge:
    """按 (method, path) 路由的假连接器；路由值是 Exception 则抛。"""

    def __init__(self, routes=None):
        self.routes = routes or {}
        self.calls = []

    def call(self, method, path, body=None):
        self.calls.append((method, path, body))
        v = self.routes[(method, path)]
        if isinstance(v, Exception):
            raise v
        return v


def http_by_url(routes):
    """按完整 URL 路由的假 _http_json；未登记的 URL 视为连不通。"""

    def _f(url, method, body, token, timeout):
        if url in routes:
            v = routes[url]
            if isinstance(v, Exception):
                raise v
            return v
        raise ms.SidecarOfflineError("offline")

    return _f


def rpc(method, params=None, msg_id=1):
    return ms.dispatch({"jsonrpc": "2.0", "id": msg_id, "method": method,
                        "params": params or {}})


def call_tool(monkeypatch, name, args, routes=None):
    """换上假 bridge 调一个工具，返回 (fake, 应答)。"""
    fb = FakeBridge(routes or {})
    monkeypatch.setattr(ms, "_BRIDGE", fb)
    return fb, ms.dispatch({"jsonrpc": "2.0", "id": 7, "method": "tools/call",
                            "params": {"name": name, "arguments": args}})


def ok_text(resp):
    assert resp["result"].get("isError") is not True, resp
    return json.loads(resp["result"]["content"][0]["text"])


# ---------------------------------------------------------------- 协议层


def test_initialize_echoes_supported_version():
    for v in ms._SUPPORTED_PROTOCOLS:
        assert rpc("initialize", {"protocolVersion": v})["result"]["protocolVersion"] == v


def test_initialize_falls_back_on_unknown_version():
    r = rpc("initialize", {"protocolVersion": "2099-09-09"})["result"]
    assert r["protocolVersion"] == ms._SUPPORTED_PROTOCOLS[-1]
    assert r["serverInfo"]["name"] == "rainframe"
    assert "tools" in r["capabilities"]


def test_tools_list_shape():
    tools = rpc("tools/list")["result"]["tools"]
    names = [t["name"] for t in tools]
    assert len(tools) == 10 and len(set(names)) == 10
    assert all(n.startswith("rf_") for n in names)
    assert {"rf_status", "rf_probe", "rf_models", "rf_model_download",
            "rf_task_create", "rf_tasks", "rf_task", "rf_task_cancel",
            "rf_task_resume", "rf_scan_folder"} == set(names)
    for t in tools:
        assert t["description"]
        assert t["inputSchema"]["type"] == "object"


@pytest.mark.parametrize("msg", [
    {"jsonrpc": "2.0", "method": "notifications/initialized"},
    {"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"id": 1}},
    {"jsonrpc": "2.0", "method": "tools/list"},  # 无 id 的请求按通知处理
])
def test_notifications_and_requests_without_id_get_no_response(msg):
    assert ms.dispatch(msg) is None


def test_ping():
    assert rpc("ping")["result"] == {}


def test_unknown_method():
    resp = rpc("resources/list")
    assert resp["error"]["code"] == -32601


def test_non_dict_message_rejected():
    assert ms.dispatch(["batch", 1])["error"]["code"] == -32600


def test_tools_call_unknown_tool():
    resp = rpc("tools/call", {"name": "rf_nope", "arguments": {}})
    assert resp["error"]["code"] == -32602


def test_tools_call_missing_required_param(monkeypatch):
    _fb, resp = call_tool(monkeypatch, "rf_probe", {})
    assert resp["error"]["code"] == -32602
    assert "path" in resp["error"]["message"]


def test_process_line_garbage_and_blank():
    out = ms._process_line("not json{{{")
    assert json.loads(out)["error"]["code"] == -32700
    assert ms._process_line("") is None
    assert ms._process_line("  ") is None
    line = json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"})
    assert ms._process_line(line) is None


def test_serve_stops_quietly_when_stdout_closes():
    """客户端中途关管道（Windows 抛 OSError 22 而非 BrokenPipeError）须静默结束。"""
    import io

    class DeadStdout(io.StringIO):
        def write(self, s):
            raise OSError(22, "Invalid argument")

    lines = [json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"})] * 3
    ms._serve(iter(lines), DeadStdout())  # 不抛即通过


# ---------------------------------------------------------------- 工具层


def test_status_combines_three_endpoints(monkeypatch):
    fb, resp = call_tool(monkeypatch, "rf_status", {}, routes={
        ("GET", "/api/health"): {"ok": True, "version": "9.9.9"},
        ("GET", "/api/hardware"): {"gpus": [{"name": "RTX 5080", "vram_gb": 16}]},
        ("GET", "/api/engine"): {"backend": "trt", "detail": ""},
    })
    out = ok_text(resp)
    assert out["version"] == "9.9.9"
    assert out["hardware"]["gpus"][0]["name"] == "RTX 5080"
    assert out["engine"]["backend"] == "trt"
    assert len(fb.calls) == 3


def test_probe_sends_recommend_true(monkeypatch):
    probe_resp = {"ok": True, "width": 1920, "height": 1080,
                  "recommend": {"model_id": "realesr-animevideov3"}}
    fb, resp = call_tool(monkeypatch, "rf_probe", {"path": "D:/a.mp4"},
                         routes={("POST", "/api/probe"): probe_resp})
    assert fb.calls[0] == ("POST", "/api/probe",
                           {"path": "D:/a.mp4", "recommend": True})
    assert ok_text(resp)["recommend"]["model_id"] == "realesr-animevideov3"


def test_models_installed_only_filter(monkeypatch):
    models = [{"id": "a", "installed": True}, {"id": "b", "installed": False}]
    _fb, resp = call_tool(monkeypatch, "rf_models", {"installed_only": True},
                          routes={("GET", "/api/models"): models})
    out = ok_text(resp)
    assert out["count"] == 1 and out["models"][0]["id"] == "a"


def test_model_download_messages(monkeypatch):
    _fb, resp = call_tool(
        monkeypatch, "rf_model_download", {"model_id": "m1"},
        routes={("POST", "/api/models/m1/download"): {"ok": True, "already": True}})
    assert "已安装" in ok_text(resp)["message"]

    _fb, resp = call_tool(
        monkeypatch, "rf_model_download", {"model_id": "m1"},
        routes={("POST", "/api/models/m1/download"): {"ok": True, "started": True}})
    assert "轮询" in ok_text(resp)["hint"]


def test_task_create_param_assembly(monkeypatch):
    task_row = {"id": "abc123", "status": "queued", "model_id": "m1",
                "input_path": "D:/v.mp4", "output_path": "D:/v_2x.mp4",
                "progress_frames": 0, "total_frames": 100,
                "params": {"scale": 2}}
    fb, resp = call_tool(
        monkeypatch, "rf_task_create",
        {"input": "D:/v.mp4", "model_id": "m1", "kind": "manga", "scale": 2,
         "crf": 20, "overwrite": True,
         "extra_params": {"deband": True, "scale": 4}},
        routes={("POST", "/api/tasks"): task_row})
    method, path, body = fb.calls[0]
    assert (method, path) == ("POST", "/api/tasks")
    assert body["input"] == "D:/v.mp4" and body["model_id"] == "m1"
    assert body["overwrite"] is True
    assert body["params"] == {"kind": "manga", "scale": 4, "crf": 20,
                              "deband": True}  # extra_params 覆盖便捷字段
    out = ok_text(resp)
    assert out["id"] == "abc123" and out["status"] == "queued"
    assert "rf_task" in out["hint"]


def test_task_create_input_folder_scans_and_expands(monkeypatch):
    scan = {"folder": "D:/manga", "total": 2, "dirs": 0,
            "files": [{"path": "D:/manga/1.png", "rel": "1.png"},
                      {"path": "D:/manga/2.png", "rel": "2.png"}]}
    task_row = {"id": "t", "status": "queued", "model_id": "m1",
                "params": {"kind": "manga", "images": [{"in": "a"}, {"in": "b"}]}}
    fb, resp = call_tool(
        monkeypatch, "rf_task_create",
        {"input_folder": "D:/manga", "model_id": "m1", "kind": "manga"},
        routes={("POST", "/api/images/scan"): scan,
                ("POST", "/api/tasks"): task_row})
    body = fb.calls[1][2]
    assert body["inputs"] == ["D:/manga/1.png", "D:/manga/2.png"]
    out = ok_text(resp)
    assert out["images_count"] == 2  # 批量清单不回传，只报数量


def test_task_create_empty_folder_is_tool_error(monkeypatch):
    _fb, resp = call_tool(
        monkeypatch, "rf_task_create",
        {"input_folder": "D:/empty", "model_id": "m1"},
        routes={("POST", "/api/images/scan"): {"total": 0, "files": []}})
    assert resp["result"]["isError"] is True
    assert "没有图片" in resp["result"]["content"][0]["text"]


def test_tool_error_surfaces_as_iserror_text(monkeypatch):
    for exc, needle in [
        (ms.SidecarOfflineError("未发现运行中的雨帧 sidecar（已扫描）。请先打开雨帧应用。"),
         "打开雨帧"),
        (ms.ApiError(409, "输出文件已存在"), "409"),
    ]:
        _fb, resp = call_tool(monkeypatch, "rf_tasks", {},
                              routes={("GET", "/api/tasks"): exc})
        assert resp["result"]["isError"] is True
        assert needle in resp["result"]["content"][0]["text"]


def test_tasks_filter_and_truncation(monkeypatch):
    tasks = [{"id": f"t{i}", "status": "running" if i % 2 else "done",
              "params": {}, "progress_frames": i, "total_frames": 100}
             for i in range(40)]
    q_path = "/api/tasks?" + urllib.parse.urlencode({"q": "x"})
    fb, resp = call_tool(monkeypatch, "rf_tasks", {"status": "running", "q": "x"},
                         routes={(("GET", q_path)): tasks})
    assert fb.calls[0][1] == q_path  # q 透传服务端搜索
    out = ok_text(resp)
    assert out["total_matching"] == 20 and len(out["tasks"]) == 20
    assert all(t["status"] == "running" for t in out["tasks"])

    fb, resp = call_tool(monkeypatch, "rf_tasks", {},
                         routes={("GET", "/api/tasks"): tasks})
    out = ok_text(resp)
    assert len(out["tasks"]) == ms._MAX_LIST and out["truncated"] is True


def test_task_detail_slims_images_manifest(monkeypatch):
    row = {"id": "t", "status": "running", "params": {
        "kind": "manga", "scale": 2,
        "images": [{"in": f"p{i}.png", "out": f"p{i}.png"} for i in range(25)]}}
    _fb, resp = call_tool(monkeypatch, "rf_task", {"task_id": "t"},
                          routes={("GET", "/api/tasks/t"): row})
    p = ok_text(resp)["params"]
    assert "images" not in p
    assert p["images_count"] == 25 and len(p["images_preview"]) == 3
    assert ok_text(resp)["kind"] == "manga"


def test_task_resume_adds_hint(monkeypatch):
    _fb, resp = call_tool(monkeypatch, "rf_task_resume", {"task_id": "t"},
                          routes={("POST", "/api/tasks/t/resume"): {"ok": True}})
    assert ok_text(resp)["hint"]


def test_scan_folder_truncation(monkeypatch):
    scan = {"folder": "D:/m", "total": 25, "dirs": 1,
            "files": [{"path": f"D:/m/{i}.png", "rel": f"{i}.png"}
                      for i in range(25)]}
    _fb, resp = call_tool(monkeypatch, "rf_scan_folder", {"folder": "D:/m"},
                          routes={("POST", "/api/images/scan"): scan})
    out = ok_text(resp)
    assert out["total"] == 25 and len(out["files_preview"]) == ms._MAX_SCAN_FILES
    assert out["truncated"] is True and "input_folder" in out["hint"]


# ---------------------------------------------------------------- 连接器


class _FakeOpener:
    """捕获 urllib Request 的假 opener（smoke 实测教训：POST 带体必须显式
    Content-Type: application/json，否则 FastAPI 按表单解析返回 422）。"""

    def __init__(self, payload):
        self.payload = payload
        self.requests = []

    def open(self, req, timeout=None):
        self.requests.append(req)
        import io
        return io.BytesIO(json.dumps(self.payload).encode("utf-8"))


def test_http_json_post_sets_json_content_type(monkeypatch):
    opener = _FakeOpener({"ok": True})
    monkeypatch.setattr(ms, "_OPENER", opener)
    out = ms._http_json("http://127.0.0.1:8730/api/probe", "POST",
                        {"path": "D:/a.mp4", "recommend": True}, None, 5.0)
    assert out == {"ok": True}
    req = opener.requests[0]
    assert req.get_method() == "POST"
    assert req.get_header("Content-type") == "application/json"
    body = json.loads(req.data.decode("utf-8"))
    assert body == {"path": "D:/a.mp4", "recommend": True}


def test_http_json_get_has_no_body(monkeypatch):
    opener = _FakeOpener({"ok": True})
    monkeypatch.setattr(ms, "_OPENER", opener)
    ms._http_json("http://127.0.0.1:8730/api/health", "GET", None, None, 5.0)
    req = opener.requests[0]
    assert req.data is None
    assert req.get_header("Content-type") is None


@pytest.fixture
def no_tokens(monkeypatch):
    monkeypatch.delenv("SV_TOKEN", raising=False)
    monkeypatch.setattr(ms, "_token_file_candidates", lambda: [])


def _engine_ok(url):
    return url.endswith("/api/engine")


def test_discovery_skips_non_sidecar_and_prefers_version_match(monkeypatch, no_tokens):
    monkeypatch.setattr(ms, "_http_json", http_by_url({
        "http://127.0.0.1:8730/api/health": {"ok": False},           # 非 sidecar 占用
        "http://127.0.0.1:8731/api/health": {"ok": True, "version": "0.0.1"},
        "http://127.0.0.1:8732/api/health": {"ok": True, "version": ms.__version__},
        "http://127.0.0.1:8732/api/engine": {"backend": "trt"},
        "http://127.0.0.1:8731/api/engine": {"backend": "dml"},
    }))
    b = ms._Bridge()
    b.discover()
    assert b.base == f"http://127.0.0.1:8732"  # 版本一致者优先于先命中


def test_discovery_none_found_message(monkeypatch, no_tokens):
    monkeypatch.setattr(ms, "_http_json", http_by_url({}))
    b = ms._Bridge()
    with pytest.raises(ms.SidecarOfflineError, match="请先打开雨帧"):
        b.call("GET", "/api/models")


def test_token_candidates_env_first_then_mtime(monkeypatch, tmp_path):
    monkeypatch.setenv("SV_TOKEN", "envtok")
    f_old, f_new = tmp_path / "a.token", tmp_path / "b.token"
    f_old.write_text("old", encoding="utf-8")
    f_new.write_text("new", encoding="utf-8")
    os.utime(f_old, (1_000_000, 1_000_000))
    os.utime(f_new, (2_000_000, 2_000_000))
    monkeypatch.setattr(ms, "_token_file_candidates", lambda: [f_old, f_new])
    assert ms._token_candidates() == ["envtok", "new", "old", None]


def test_authenticate_picks_working_candidate(monkeypatch):
    """候选逐个试 /api/engine，401 换下一个，None（dev 无鉴权）兜底。"""
    seen_tokens = []

    def fake(url, method, body, token, timeout):
        if _engine_ok(url):
            seen_tokens.append(token)
            if token == "good":
                return {"backend": "trt"}
            raise ms.ApiError(401, "unauthorized")
        return {"ok": True, "version": ms.__version__}

    monkeypatch.setattr(ms, "_http_json", fake)
    monkeypatch.delenv("SV_TOKEN", raising=False)

    def cands():
        return ["stale", "good", None]

    monkeypatch.setattr(ms, "_token_candidates", cands)
    b = ms._Bridge()
    b.discover()
    assert b.token == "good" and seen_tokens == ["stale", "good"]


def test_all_token_candidates_rejected(monkeypatch):
    def fake(url, method, body, token, timeout):
        if _engine_ok(url):
            raise ms.ApiError(401, "unauthorized")
        return {"ok": True, "version": ms.__version__}

    monkeypatch.setattr(ms, "_http_json", fake)
    monkeypatch.delenv("SV_TOKEN", raising=False)
    monkeypatch.setattr(ms, "_token_candidates", lambda: ["x", None])
    b = ms._Bridge()
    with pytest.raises(ms.UnauthorizedError, match="令牌"):
        b.discover()


class _RotatingHttp:
    """模拟 UI 重启轮换令牌：token 文件内容变了，旧 token 开始 401。"""

    def __init__(self, token_file: Path):
        self.token_file = token_file
        self.current = token_file.read_text(encoding="utf-8").strip()

    def __call__(self, url, method, body, token, timeout):
        if url.endswith("/api/health"):
            return {"ok": True, "version": ms.__version__}
        if token == self.current:
            return [{"id": "m"}] if url.endswith("/api/models") else {"backend": "trt"}
        raise ms.ApiError(401, "unauthorized")


def test_401_after_token_rotation_recovers_once(monkeypatch, tmp_path):
    tok = tmp_path / "sidecar.token"
    tok.write_text("tok1", encoding="utf-8")
    monkeypatch.delenv("SV_TOKEN", raising=False)
    monkeypatch.setattr(ms, "_token_file_candidates", lambda: [tok])
    fake = _RotatingHttp(tok)
    monkeypatch.setattr(ms, "_http_json", fake)

    b = ms._Bridge()
    assert b.call("GET", "/api/models") == [{"id": "m"}]
    assert b.token == "tok1"

    tok.write_text("tok2", encoding="utf-8")  # UI 重启：新令牌落盘
    fake.current = "tok2"
    assert b.call("GET", "/api/models") == [{"id": "m"}]  # 401→重发现→重试成功
    assert b.token == "tok2"


def test_offline_resets_base_for_rediscovery(monkeypatch, no_tokens):
    state = {"alive": True}

    def fake(url, method, body, token, timeout):
        if state["alive"]:
            return [{"id": "m"}] if url.endswith("/api/models") else (
                {"ok": True, "version": ms.__version__}
                if url.endswith("/api/health") else {"backend": "trt"})
        raise ms.SidecarOfflineError("down")

    monkeypatch.setattr(ms, "_http_json", fake)
    b = ms._Bridge()
    assert b.call("GET", "/api/models") == [{"id": "m"}]
    state["alive"] = False
    with pytest.raises(ms.SidecarOfflineError):
        b.call("GET", "/api/models")
    assert b.base is None  # 掉线清空连接，下次调用重新发现（重启可能换端口）


# ---------------------------------------------------------------- 准入闸门（sidecar 侧）


@pytest.fixture(scope="module")
def api_client():
    """真 FastAPI app（TestClient 进程内 ASGI），验 mcp_enabled 闸门中间件。"""
    if os.environ.get("SV_DB") and Path(os.environ["SV_DB"]).exists():
        Path(os.environ["SV_DB"]).unlink()
    from fastapi.testclient import TestClient

    from sv.server.app import app

    with TestClient(app) as c:
        yield c


_BRIDGE_UA = {"User-Agent": "rainframe-mcp/0.8.0"}


def test_gate_blocks_bridge_when_disabled(api_client, monkeypatch):
    from sv.server import settings as sv_settings

    monkeypatch.setattr(sv_settings, "load", lambda: {"mcp_enabled": False})
    r = api_client.get("/api/models", headers=_BRIDGE_UA)
    assert r.status_code == 403
    assert "MCP 服务" in r.json()["detail"]  # 文案自解释，bridge 原样转告
    # health 豁免：bridge 靠它发现 sidecar，才能报出「去开闸」而不是连不上
    assert api_client.get("/api/health", headers=_BRIDGE_UA).status_code == 200


def test_gate_ignores_normal_traffic_and_open_gate(api_client, monkeypatch):
    from sv.server import settings as sv_settings

    monkeypatch.setattr(sv_settings, "load", lambda: {"mcp_enabled": False})
    assert api_client.get("/api/models").status_code == 200  # 普通 UI 流量不受闸门影响
    monkeypatch.setattr(sv_settings, "load", lambda: {"mcp_enabled": True})
    assert api_client.get("/api/models", headers=_BRIDGE_UA).status_code == 200


def test_settings_mcp_enabled_default_and_validation(tmp_path, monkeypatch):
    from sv.server import settings as sv_settings

    monkeypatch.setattr(sv_settings, "SETTINGS_PATH", tmp_path / "settings.json")
    assert sv_settings.load()["mcp_enabled"] is True  # 默认开
    assert sv_settings.save({"mcp_enabled": False})["mcp_enabled"] is False
    assert sv_settings.load()["mcp_enabled"] is False
    with pytest.raises(ValueError):
        sv_settings.save({"mcp_enabled": "yes"})
