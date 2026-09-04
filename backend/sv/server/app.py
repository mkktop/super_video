"""FastAPI sidecar：HTTP + WS 对外接口，串行队列调度。

路由 handler 按域拆在 routes/{system,models,tasks,trim,compare}.py，本模块只保留：
进程级装配（lifespan）、令牌鉴权中间件、WS 端点。共享单例在 state.py。
部分符号（create_task 等）在此 re-export——既有测试直接 from sv.server.app import。
"""
from __future__ import annotations

import asyncio
import json
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .. import __version__
from ..paths import TEMP_DIR, migrate_legacy_data
from . import compare, db, task_stills
from .routes import compare as compare_routes
from .routes import models as models_routes
from .routes import system as system_routes
from .routes import tasks as tasks_routes
from .routes import trim as trim_routes
from .routes.tasks import gc_sr_logs, sweep_orphan_workdirs  # lifespan 用
from .state import bus, perf, runner

# ---- 测试兼容 re-export（handler 已迁 routes，勿在此新增实现） ----
from .routes.models import ModelImport, PresetCreate, create_preset, get_models  # noqa: F401
from .routes.tasks import (  # noqa: F401
    TaskCreate,
    _render_output_stem,
    create_task,
)


def _expected_tokens() -> set[str]:
    """本地令牌（未配置=开发/测试模式不鉴权）。

    监听 127.0.0.1 并非浏览器隔离：任意网页可直接 POST 本端口（CORS 挡不住
    no-cors/表单 POST，WS 更不受 CORS 约束）。带上随机 token 后恶意页面无从
    获得令牌，本机 API 不再是 drive-by 攻击面。

    两个来源取并集：
    - SV_TOKEN 环境变量：Electron 拉起 sidecar 时注入（本次会话令牌）；
    - TEMP_DIR/sidecar.token 文件：UI 重启生成新 token 复用旧 sidecar 时，
      旧进程 env 里还是老 token——按文件实时校验才能接受新 token。
    """
    toks: set[str] = set()
    env_tok = os.environ.get("SV_TOKEN")
    if env_tok:
        toks.add(env_tok)
    try:
        f = TEMP_DIR / "sidecar.token"
        if f.is_file():
            t = f.read_text(encoding="utf-8").strip()
            if t:
                toks.add(t)
    except OSError:
        pass
    return toks


async def _token_auth(request: Request, call_next):
    toks = _expected_tokens()
    if toks and request.method != "OPTIONS" and request.url.path != "/api/health":
        got = request.headers.get("x-sv-token") or request.query_params.get("token")
        if got not in toks:
            return JSONResponse({"detail": "unauthorized"}, status_code=401)
    return await call_next(request)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 一次性迁移 ≤v0.1.20 遗留在安装目录里的数据（模型/组件/设置/缓存）。
    # 必须在 init_db 之前——任务库也在 .tmp 里
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, migrate_legacy_data)
    db.init_db()
    await loop.run_in_executor(None, sweep_orphan_workdirs)
    await loop.run_in_executor(None, task_stills.sweep_orphans)
    await loop.run_in_executor(None, gc_sr_logs)
    trim_routes._start_trim_worker()
    compare.start(bus)
    runner.start()
    perf.start()
    yield
    await runner.stop()
    perf.stop()


app = FastAPI(title="super_video sidecar", version=__version__, lifespan=lifespan)
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
)
app.middleware("http")(_token_auth)

app.include_router(system_routes.router)
app.include_router(models_routes.router)
app.include_router(tasks_routes.router)
app.include_router(trim_routes.router)
app.include_router(compare_routes.router)


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    toks = _expected_tokens()
    if toks and ws.query_params.get("token") not in toks:  # WS 不受 CORS 约束，必须自查令牌
        await ws.close(code=4401)
        return
    await ws.accept()
    q = bus.subscribe()
    # 客户端不发消息；并发挂一个 receive 作断连哨兵，否则 q.get() 永远发现不了断开
    sentinel = asyncio.create_task(ws.receive())
    try:
        while True:
            send_task = asyncio.create_task(q.get())
            done, _ = await asyncio.wait(
                {sentinel, send_task}, return_when=asyncio.FIRST_COMPLETED
            )
            if sentinel in done:
                send_task.cancel()
                break
            if send_task not in done:
                continue
            event = send_task.result()
            await ws.send_text(json.dumps(event, ensure_ascii=False))
    except Exception:  # 断开时 send 抛异常，走统一清理
        pass
    finally:
        sentinel.cancel()
        bus.unsubscribe(q)
