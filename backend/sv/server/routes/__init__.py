"""FastAPI 路由按域拆分：system/models/tasks/trim/compare。

app.py 保留鉴权中间件、lifespan、WS 端点与单例装配；各域 handler 在此。
拆分为纯搬家（handler 逻辑不变），测试直接 import 的符号在 app.py 保留
re-export。
"""
