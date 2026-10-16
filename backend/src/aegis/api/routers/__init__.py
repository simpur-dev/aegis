"""按资源拆分的 API 路由（结构重构 P3）：每条路由只在自己的资源模块里，`create_app` 只做装配。

拆法按"读哪一块平台状态"，不按 HTTP 方法——找一条端点先定位资源、再定位方法。
鉴权不在这一层：`create_app` 的 HTTP 中间件对 `/api/` 整面生效，新增端点不会因为忘了挂而裸奔。
每个模块导出 `build_router()`，与 `workflow_api.py` / `assistant_api.py` 同形。
"""
