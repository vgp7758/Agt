"""插件 HTTP 路由的 reload 接管（2026-11-24 实锤）：/reload plugins 后同 path 的插件路由必须绑到
新模块的新函数。此前 _attach_one「已存在即跳过」——改了 backend 里的路由处理器再 reload 永不生效
（工具/命令/钩子是真换了，唯独路由还连着旧闭包；voice-input 插件改了两版都没接管，才暴露出来）。

同 path 的内置路由不顶替（既有语义不变）。
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import plugins as P


class _Manifest:
    name = "probe"


@pytest.fixture
def app():
    saved = P._RT
    P._RT = P._Runtime()
    app = FastAPI()
    P._RT.attached_app = app          # 服务已起的形态：register_route 即时挂载
    yield app
    P._RT = saved                     # 还原——_RT 是模块级单例，泄漏会打穿后续测试


def _register(fn, path="/api/probe/x", method="POST"):
    P.PluginAPI(P._Loaded(manifest=_Manifest())).register_route(method, path, fn)


def _plugin_routes(app, path):
    return [r for r in app.router.routes if getattr(r, "path", None) == path
            and (getattr(r, "name", "") or "").startswith("plugin_")]


def test_reload_rebinds_route_to_new_handler(app):
    async def v1(request):
        return {"v": 1}

    async def v2(request):
        return {"v": 2}

    _register(v1)
    c = TestClient(app)
    assert c.post("/api/probe/x").json() == {"v": 1}
    _register(v2)                                   # /reload plugins：新模块的新函数注册同一路由
    assert c.post("/api/probe/x").json() == {"v": 2}
    assert len(_plugin_routes(app, "/api/probe/x")) == 1     # 旧的已摘，不留双份


def test_reload_keeps_other_methods_of_same_plugin(app):
    async def get_v1(request):
        return {"v": 1}

    async def post_v1(request):
        return {"v": 1}

    async def get_v2(request):
        return {"v": 2}

    async def post_v2(request):
        return {"v": 2}

    _register(get_v1, method="GET")
    _register(post_v1, method="POST")
    _register(get_v2, method="GET")                 # 按注册顺序逐条接管：此刻 POST 还是旧的
    c = TestClient(app)
    assert c.get("/api/probe/x").json() == {"v": 2}
    assert c.post("/api/probe/x").json() == {"v": 1}
    _register(post_v2, method="POST")
    assert c.post("/api/probe/x").json() == {"v": 2}
    assert len(_plugin_routes(app, "/api/probe/x")) == 2


def test_builtin_route_same_path_not_shadowed(app):
    @app.get("/api/probe/y")
    def builtin():
        return {"builtin": True}

    async def mine(request):
        return {"mine": True}

    _register(mine, path="/api/probe/y", method="GET")
    _register(mine, path="/api/probe/y", method="POST")    # 既有语义：同 path 有内置路由 → 插件不挂
    c = TestClient(app)
    assert c.get("/api/probe/y").json() == {"builtin": True}
    assert c.post("/api/probe/y").status_code == 405
    assert _plugin_routes(app, "/api/probe/y") == []
