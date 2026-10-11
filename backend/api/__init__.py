"""附加式 HTTP 面（`master` 有、26.3 缺、且**本机真有数据源**的端点）。

只新增路由：`api.http_surface.install(app)` 由 `server.py` 在**文件末尾**调用，
不改 `server.py` 的启动/鉴权/静态文件逻辑，也不改任何既有路由的行为。
"""
