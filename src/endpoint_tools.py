"""endpoint_tools.py —— 自定义 HTTP 端点（agent 给正在运行的 Web 服务动态挂路由）。

场景：需要接收外部 webhook/回调/轮询请求，且处理逻辑可纯代码完成（零 token）——
如 GitHub webhook、支付回调、定时器触发。端点挂在本实例 Web 端口上（与 /api/* 框架
端点同进程同端口；路径强制前缀 /custom/，与框架端点物理隔离永不冲突），处理代码在
请求到达时执行；需要 Agent 参与时在代码里调 push() 把事件推进 inbox（唤醒一轮）。

持久化：端点清单存 <workspace>/.agent/endpoints.json，服务重启后自动重挂（零丢失）。
鉴权：默认 token（校验 X-Cb-Token / ?token= == settings.json 的 callback_token），
auth="none" 显式豁免（公开 webhook 场景自行在代码里验签）。

挂载机制复用 FastAPI 运行时路由（与插件路由同通道——plugins.py 先例），无需重启服务。
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from fastapi import Request as _FastAPIRequest   # 模块级引用：handler 的字符串注解（future
# annotations 下）运行时按模块 globals resolve——局部 import 会 resolve 失败，FastAPI 把
# request 当必填 query 参数 → 一律 422（plugins.py L408 同款坑，2026-10-15 实锤）

from real_tools import WORKSPACE
from tools import Tool

_STORE = WORKSPACE / ".agent" / "endpoints.json"
_MAX_BODY = 10 * 1024 * 1024   # 超过直接 413（防超大 body 炸内存）
_TEXT_CAP = 64 * 1024          # 传给 code 的 text 截断长度

# (method, path) -> {"main": fn, "auth": str, "desc": str, "code": str, "created": ts}
_ns_registry: dict = {}
# (method, path) -> {"count": int, "last": ts}（内存计数，重启清零）
_HITS: dict = {}


# ==================== 存取 ====================

def _load_store() -> list:
    try:
        return json.loads(_STORE.read_text(encoding="utf-8")) or []
    except Exception:
        return []


def _save_store(items: list):
    _STORE.parent.mkdir(parents=True, exist_ok=True)
    _STORE.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")


def _cb_token() -> str:
    try:
        from config import load_runtime_settings
        return str(load_runtime_settings().get("callback_token") or "")
    except Exception:
        return ""


# ==================== 挂载 ====================

def _mount(app, method: str, path: str):
    """把一条端点挂上 FastAPI（同 method+path 已存在则先摘旧——更新语义）。"""
    key = (method.upper(), path)
    if key not in _ns_registry:
        return
    # 摘旧（更新注册时旧 handler 必须真正失效——FastAPI 同 path 两条路由时旧的优先匹配）
    _unmount(app, method, path)
    from fastapi.responses import JSONResponse
    ent = _ns_registry[key]

    async def _handler(request: "_FastAPIRequest"):
        # —— 鉴权（默认 token；"none" 豁免）——
        if ent["auth"] != "none":
            _want = _cb_token()
            tok = str(request.headers.get("x-cb-token") or request.query_params.get("token") or "")
            if not _want:
                return JSONResponse({"error": "未配置 callback_token——token 模式端点拒绝服务"}, status_code=503)
            if tok != _want:
                return JSONResponse({"error": "token 校验失败"}, status_code=401)
        # —— 组装 req ——
        try:
            raw = await request.body()
        except Exception as e:
            return JSONResponse({"error": f"body 读取失败: {e}"}, status_code=400)
        if len(raw) > _MAX_BODY:
            return JSONResponse({"error": f"body 超过 {_MAX_BODY // 1024 // 1024}MB 限制"}, status_code=413)
        text = raw.decode("utf-8", errors="replace")[:_TEXT_CAP]
        try:
            body_json = json.loads(text) if text else None
        except Exception:
            body_json = None
        req = {
            "method": request.method,
            "path": request.url.path,
            "query": dict(request.query_params),
            "headers": {k.lower(): v for k, v in request.headers.items()},
            "json": body_json,
            "text": text,
            "client": (request.client.host if request.client else ""),
        }
        h = _HITS.setdefault(key, {"count": 0, "last": 0})
        h["count"] += 1
        h["last"] = time.time()
        # —— 执行 code 的 main ——
        import asyncio
        import inspect as _insp
        try:
            rv = ent["main"](req)
            if _insp.iscoroutine(rv):
                rv = await asyncio.wait_for(rv, timeout=60)
        except asyncio.TimeoutError:
            return JSONResponse({"error": "端点代码执行超时（>60s）"}, status_code=504)
        except Exception as e:
            import traceback
            return JSONResponse({"error": f"{type(e).__name__}: {e}",
                                 "traceback": traceback.format_exc()[-1500:]}, status_code=500)
        # —— 宽容解析返回值 ——
        if isinstance(rv, tuple) and len(rv) == 2 and isinstance(rv[0], int):
            status, body = rv
        else:
            status, body = 200, rv
        if isinstance(body, (dict, list)):
            return JSONResponse(body, status_code=status)
        return JSONResponse({"text": str(body)}, status_code=status)

    try:
        app.add_api_route(path, _handler, methods=[method.upper()],
                          name=f"custom_{method.lower()}_{path.strip('/').replace('/', '_')}")
        return True
    except Exception:
        return False


def _unmount(app, method: str, path: str) -> bool:
    """从 app.routes 摘掉一条自定义端点（按 name 前缀 custom_ 精确识别）。"""
    name = f"custom_{method.lower()}_{path.strip('/').replace('/', '_')}"
    before = len(app.routes)
    app.router.routes[:] = [r for r in app.routes if getattr(r, "name", "") != name]
    return len(app.routes) < before


def _get_app():
    """进程内拿 server.app（服务未起返回 None）。"""
    try:
        import server
        if getattr(server, "_server", None) is not None:
            return server.app
    except Exception:
        pass
    return None


def restore_all(app, agent=None) -> int:
    """服务启动时重放挂载全部已存端点（server.start_server 尾部调用）。
    agent 缺省从 server._agent 取（start_server 注入态）——端点 code 里的 push() 靠它
    绑定（L3 实锤 2026-11-16：不传则重放后的 push() NameError → 端点 500）。"""
    if agent is None:
        try:
            import server as _sv
            agent = getattr(_sv, "_agent", None)
        except Exception:
            agent = None
    push_fn = _make_push(agent) if agent is not None else None
    items = _load_store()
    n = 0
    for it in items:
        try:
            ns = _make_ns(it["path"], push_fn)
            exec(it["code"], ns)
            main = ns.get("main")
            if main is None:
                continue
            _ns_registry[(it["method"].upper(), it["path"])] = {
                "main": main, "auth": it.get("auth", "token"),
                "desc": it.get("desc", ""), "code": it["code"], "created": it.get("created", 0)}
            if _mount(app, it["method"], it["path"]):
                n += 1
        except Exception:
            continue
    return n


# ==================== code 执行环境 ====================

def _make_push(agent):
    """构造端点 code 内置的 push()（绑 agent）。"""
    def _push(text: str, source: str = ""):
        """端点代码内置：把事件推进本 Agent 的 inbox 并唤醒一轮（同步返回入队结果）。"""
        try:
            agent.push_message(str(text), source or "custom-endpoint", wake=True)
            return "已入队"
        except Exception as e:
            return f"入队失败: {e}"
    return _push


def _make_ns(path: str, push_fn) -> dict:
    """端点 code 的 exec 全局命名空间。"""
    ns = {"__builtins__": __builtins__}
    if push_fn is not None:
        ns["push"] = push_fn
    return ns


# ==================== 工具（LLM 可见）====================

def make_endpoint_tools(agent) -> list:
    """生成端点管理工具三件套（serve_endpoint / remove_endpoint / list_endpoints）。"""
    _push = _make_push(agent)

    def serve_endpoint(method: str = "POST", path: str = "", code: str = "",
                       auth: str = "token", desc: str = "") -> str:
        """给本实例正在运行的 Web 服务挂一条自定义 HTTP 端点（无需重启，即时生效）。
        典型用途：接收外部 webhook/回调——处理逻辑纯代码完成（零 token），需要 Agent 参与
        时在 code 里调 push(text, source) 把事件推进 inbox 唤醒一轮。

        path: 必须 /custom/ 开头（如 /custom/github-hook），与框架端点隔离；URL 即
              http://<实例地址><path>。
        method: GET / POST / PUT / DELETE / PATCH。
        code: Python，定义 main(req) 处理每个请求（sync 或 async def 均可，>60s 超时）。
              req = {"method","path","query","headers","json","text","client"}（json 为解析后的
              body，非 JSON 时为 None；text 为原始文本截 64KB）。返回 dict/list/str → 200；
              返回 (status, body) → 定制状态码。
              特殊：push(text, source="") 可用——事件注入本 Agent（走 inbox 持久化+唤醒）。
        auth: "token"（默认）= 校验 X-Cb-Token 或 ?token=（值=settings.json 的 callback_token，
              未配置则端点拒绝服务）；"none" = 完全公开（公开 webhook 自行在 code 里验签）。
        desc: 用途一句话（list_endpoints 展示用）。
        重复注册同 method+path = 更新（旧的处理逻辑立即失效）。
        持久化在 .agent/endpoints.json——服务/实例重启后自动重挂。CLI 无 Web 服务时仅存盘，
        服务启动后自动生效。"""
        m = (method or "POST").strip().upper()
        if m not in ("GET", "POST", "PUT", "DELETE", "PATCH"):
            return f"❌ method 需为 GET/POST/PUT/DELETE/PATCH，收到 {method!r}"
        p = (path or "").strip()
        if not p.startswith("/custom/") or len(p) <= len("/custom/"):
            return f"❌ path 必须 /custom/ 开头（如 /custom/github-hook），收到 {path!r}"
        if not (code or "").strip():
            return "❌ code 不能为空：需定义 main(req) 函数"
        # 语法/契约校验（exec 一次；main 必须在）
        ns = _make_ns(p, _push)
        try:
            exec(code, ns)
        except Exception as e:
            import traceback
            return f"❌ code 执行失败（注册即验证）:\n{traceback.format_exc()[-1200:]}"
        main = ns.get("main")
        if not callable(main):
            return "❌ code 里没有定义 main(req) 函数（def main(req): ... 或 async def）"
        if auth not in ("token", "none"):
            return f"❌ auth 需为 token/none，收到 {auth!r}"
        # 落盘 + 内存注册 + 挂载
        items = [it for it in _load_store()
                 if not (it.get("method", "").upper() == m and it.get("path") == p)]
        items.append({"method": m, "path": p, "code": code, "auth": auth, "desc": desc,
                      "created": time.time()})
        _save_store(items)
        _ns_registry[(m, p)] = {"main": main, "auth": auth, "desc": desc,
                                "code": code, "created": time.time()}
        _HITS.pop((m, p), None)
        app = _get_app()
        if app is None:
            return (f"✅ 端点已注册并存盘（{m} {p}）——当前无 Web 服务在跑，服务启动后自动挂载生效")
        if not _mount(app, m, p):
            return f"⚠️ 已存盘但挂载失败（{m} {p}）——检查路径合法性"
        tok_note = ("鉴权 X-Cb-Token/?token=（callback_token）" if auth == "token"
                    else "⚠️ 完全公开（无鉴权）")
        return (f"✅ 端点已挂载并即时生效：{m} {p}（{tok_note}）。\n"
                f"测试：curl -X {m} 'http://127.0.0.1:<端口>{p}' ... "
                f"（token 模式需 -H 'X-Cb-Token: <callback_token>'）")

    def remove_endpoint(method: str = "", path: str = "") -> str:
        """摘除一条自定义端点（serve_endpoint 注册的）：从运行中服务卸载 + 清持久化记录。
        method+path 定位（与注册时一致）。"""
        m = (method or "").strip().upper()
        p = (path or "").strip()
        if not p.startswith("/custom/"):
            return f"❌ path 必须 /custom/ 开头，收到 {path!r}"
        items = _load_store()
        rest = [it for it in items if not (it.get("method", "").upper() == m and it.get("path") == p)]
        if len(rest) == len(items):
            return f"未找到端点 {m} {p}（list_endpoints 查看现有清单）"
        _save_store(rest)
        _ns_registry.pop((m, p), None)
        _HITS.pop((m, p), None)
        app = _get_app()
        if app is not None:
            _unmount(app, m, p)
        return f"✅ 端点已摘除：{m} {p}"

    def list_endpoints() -> str:
        """列出全部自定义端点（方法/路径/鉴权/描述/挂载状态/命中计数）。"""
        items = _load_store()
        if not items:
            return "（暂无自定义端点——serve_endpoint 注册）"
        app = _get_app()
        mounted = set()
        if app is not None:
            for r in app.routes:
                name = getattr(r, "name", "")
                if isinstance(name, str) and name.startswith("custom_"):
                    mounted.add(name)
        lines = [f"共 {len(items)} 条自定义端点（Web 服务{'运行中' if app is not None else '未启动'}）："]
        for it in items:
            m, p = it.get("method", "?"), it.get("path", "?")
            name = f"custom_{m.lower()}_{p.strip('/').replace('/', '_')}"
            st = "✅已挂载" if name in mounted else ("⏸服务未启动" if app is None else "❌未挂载")
            h = _HITS.get((m.upper(), p)) or {}
            hit_s = f"，命中 {h.get('count', 0)} 次" if h else ""
            lines.append(f"- {m:6s} {p}  [{st}；auth={it.get('auth', 'token')}{hit_s}]"
                         f"{' —— ' + it['desc'] if it.get('desc') else ''}")
        return "\n".join(lines)

    return [Tool(serve_endpoint), Tool(remove_endpoint), Tool(list_endpoints)]
