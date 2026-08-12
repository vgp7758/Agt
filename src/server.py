"""server.py —— Agent Web 视图（FastAPI + WebSocket），由 chat.py 内嵌启动。

设计要点（与旧 web.py 的区别）：
  - 不再独立装配 Agent：agent / work_q / mcp_mgr 由 chat.main 经 start_server() 注入，
    与 CLI 共享同一个 Agent、同一个 work_q、同一个事件流 → 天然串行一致，无装配漂移。
  - 按需启停：/web [start] [port] 调 start_server 起后台 uvicorn 线程并监听 0.0.0.0:port；
    /web stop 调 stop_server 释放端口。默认纯 CLI 不占端口。
  - WS 普通文本消息 → 喂进 chat 主循环的 work_q（("user", text)），由主循环跑 agent.run；
    工作流调试 / RAG 建库这类占用 agent 的任务 → 进 work_q 的 ("task", fn)，与聊天同流串行。
    Agent 事件经 on_event=broadcast 推给所有 WS 客户端。
  - broadcast 加守卫：无 WS 客户端（服务未起 / 无连接）时直接 return，纯 CLI 零开销。
"""
from __future__ import annotations

import asyncio
import contextlib
import io
import json
import re
import socket
import threading
import time
import webbrowser
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, FileResponse

import config
from commands import CommandContext, apply_config, build_default_registry, read_config
from rag import get_rag
from real_tools import WORKSPACE

app = FastAPI(title="Agt Agent WebUI")

# ===== 注入态（start_server 时由 chat.main 填入；服务未起时为 None） =====
_agent = None          # chat.build_agent 装配好的全局 Agent 单例
_work_q = None         # chat 主循环的 work_q（WS 文本 / task 喂入它）
_mcp_mgr = None        # MCPManager（/api/tools 用其 get_tools）
_state = None          # chat 主循环的 state dict（含 busy 标志，供 WS 文本按忙/闲路由）
_workspace = WORKSPACE

# ===== 服务实例 =====
_server = None         # uvicorn.Server
_port = None
_server_thread = None
_server_error = None

# ===== 事件缓冲 + 多客户端广播 =====
_clients: list[dict] = []       # [{ws, queue}]  所有活跃连接
_event_log: list[tuple[int, dict]] = []
_seq: int = 0
_main_loop = None               # uvicorn 线程的 asyncio loop（broadcast 跨线程推送用）

_STATIC_DIR = Path(__file__).resolve().parent / "static"
_WF_DIR = WORKSPACE / ".agent" / "workflows"
_INDEX_HTML = (_STATIC_DIR / "index.html").read_text(encoding="utf-8")
_EDITOR_HTML = (_STATIC_DIR / "workflow_editor.html").read_text(encoding="utf-8")
_RAG_HTML = (_STATIC_DIR / "rag.html").read_text(encoding="utf-8")
_WF_DEBUG_HTML = (_STATIC_DIR / "workflow_debug.html").read_text(encoding="utf-8")
_MEMORY_HTML = (_STATIC_DIR / "memory.html").read_text(encoding="utf-8")


def _broadcast(ev: dict):
    """记录事件到日志缓冲 + 广播给所有活跃客户端。
    无 WS 客户端（服务未起 / 无连接）时直接 return —— 纯 CLI 模式零开销，
    且 _main_loop 未就绪时也不会因 call_soon_threadsafe 报错。"""
    if not _clients:
        return
    global _seq
    _seq += 1
    _event_log.append((_seq, ev))
    if len(_event_log) > 500:
        _event_log.pop(0)
    loop = _main_loop
    if loop is None:
        return
    for c in _clients:
        try:
            loop.call_soon_threadsafe(c["queue"].put_nowait, ev)
        except Exception:
            pass


# 公开接口别名：chat.build_agent 用它作 Agent.on_event（无 WS 客户端时 no-op，纯 CLI 零开销）
broadcast = _broadcast


async def _send(ws: WebSocket, obj: dict):
    await ws.send_text(json.dumps(obj, ensure_ascii=False))


def _safe_wf_path(name: str) -> Path:
    """解析工作流文件名，防越界。自动补 .json 后缀。"""
    safe = Path(name).name
    if safe != name or not safe:
        raise ValueError(f"非法文件名: {name!r}")
    if not safe.endswith(".json"):
        safe = safe + ".json"
    return _WF_DIR / safe


# ===================== 静态页 =====================

@app.get("/")
async def index():
    return HTMLResponse(_INDEX_HTML)


@app.get("/editor")
async def workflow_editor():
    return HTMLResponse(_EDITOR_HTML)


@app.get("/wfdebug")
async def workflow_debug():
    """工作流调试页：只读渲染画布 + 流式执行 + 逐节点查看输出。"""
    return HTMLResponse(_WF_DEBUG_HTML)


@app.get("/rag")
async def rag_page():
    """RAG 文档库管理页：配置 + 建库 + 查询测试。"""
    return HTMLResponse(_RAG_HTML)


@app.get("/memory")
async def memory_page():
    """长期记忆管理页：查看/编辑/删除三类记忆。"""
    return HTMLResponse(_MEMORY_HTML)


@app.get("/icons/{name}")
async def serve_icon(name: str):
    """站点图标（favicon / logo）—— 仅允许 static/icons/ 下的文件。"""
    icons_dir = (_STATIC_DIR / "icons").resolve()
    try:
        p = (_STATIC_DIR / "icons" / name).resolve()
        p.relative_to(icons_dir)  # 越界抛 ValueError
    except ValueError:
        return HTMLResponse("not found", status_code=404)
    if not p.is_file():
        return HTMLResponse("not found", status_code=404)
    return FileResponse(p, media_type="image/png")


@app.get("/manifest.json")
async def manifest():
    """PWA manifest。"""
    return FileResponse(_STATIC_DIR / "manifest.json", media_type="application/manifest+json")


# ===================== 工作流编辑器 REST API =====================

@app.get("/api/wf/list")
async def api_wf_list():
    """列出所有工作流（名称+状态摘要）。"""
    from workflow import workflows_info
    items = []
    for it in workflows_info(_workspace):
        items.append({"name": it["name"], "tool": it["tool"], "status": it["status"],
                       "detail": it["detail"], "description": it["description"], "coze_url": it["coze_url"]})
    return {"items": items}


def _infer_tool_group(name: str) -> str:
    """工具名 → 模块分组（前缀推断；build_agent 标注优先）。"""
    if name.startswith("wf_"):
        return "工作流"
    if name.startswith(("cs_", "py_")):
        return "LSP"
    return "其它"


@app.get("/api/tools")
async def api_tools():
    """返回 agent 已注册的全部工具（按模块分组），供工作流编辑器生成工具节点。
    每个含 name/display/group/description/params/outputs。group 来自 build_agent 标注或前缀推断。"""
    from real_tools import infer_tool_outputs
    if _agent is None:
        return {"tools": []}
    groups_map = getattr(_agent, "tool_groups", {})
    out, seen = [], set()
    for t in _agent.tools:
        if t.name in seen:
            continue
        seen.add(t.name)
        s = t.schema["function"]
        props = s.get("parameters", {}).get("properties", {}) or {}
        params = [{"name": pn, "type": (ps.get("type") if isinstance(ps, dict) else "string") or "string"}
                  for pn, ps in props.items()]
        outputs = getattr(t, "user_outputs", None) or infer_tool_outputs(t)
        name = s["name"]
        if name.startswith("__mcp__"):
            server = getattr(t, "server", "") or ""
            orig = getattr(t, "orig_name", "") or name
            group = f"MCP · {server}" if server else "MCP"
            display = f"{server}.{orig}" if server else orig
        else:
            group = groups_map.get(name) or _infer_tool_group(name)
            display = name
        out.append({"name": name, "display": display, "group": group,
                    "description": s.get("description", ""), "params": params, "outputs": outputs})
    return {"tools": out, "schema": True}   # schema=True 告诉前端带完整参数信息


@app.get("/api/wf/{name}")
async def api_wf_get(name: str):
    """获取单个工作流画布 JSON + meta。优先 .json，否则 .xml（转 JSON）。"""
    import json as _j
    import xml.etree.ElementTree as ET
    from workflow_xml import parse_xml_fragment
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", Path(name).name).strip("_") or "workflow"
    jf = _WF_DIR / f"{safe}.json"
    xf = _WF_DIR / f"{safe}.xml"
    if jf.exists():
        canvas = _j.loads(jf.read_text(encoding="utf-8"))
        meta = {}
        mp = jf.with_name(jf.name + ".meta")
        if mp.exists():
            try:
                meta = _j.loads(mp.read_text(encoding="utf-8")) or {}
            except Exception:
                meta = {}
        meta.setdefault("name", safe)
        return {"name": safe, "canvas": canvas, "meta": meta, "format": "json"}
    if xf.exists():
        from workflow_xml import xml_to_canvas, WorkflowXmlError
        try:
            xml_text = xf.read_text(encoding="utf-8")
            root = ET.fromstring(xml_text)
            meta = {"name": root.get("name") or safe,
                    "description": root.get("description", ""),
                    "coze_url": root.get("coze_url", ""),
                    "enabled": root.get("enabled", "true") != "false"}
            if root.get("auto"):
                meta["auto"] = root.get("auto") == "true"
            if root.get("auto_param"):
                meta["auto_param"] = root.get("auto_param")
            if root.get("hook"):
                meta["hook"] = root.get("hook")
            xmp = xf.with_name(xf.name + ".meta")
            if xmp.exists():
                try:
                    meta = {**meta, **(_j.loads(xmp.read_text(encoding="utf-8")) or {})}
                except Exception:
                    pass
            canvas = xml_to_canvas(xml_text)
        except (WorkflowXmlError, ET.ParseError) as e:
            return {"error": f"XML 解析失败：{e}"}
        meta.setdefault("name", safe)
        return {"name": safe, "canvas": canvas, "meta": meta, "format": "xml"}
    return {"error": f"工作流 {name!r} 不存在"}


@app.put("/api/wf/{name}")
async def api_wf_save(name: str, request: Request):
    """保存工作流画布 + meta。format='xml' 转 XML；否则 Coze JSON + .meta。"""
    import json as _j
    try:
        body = await request.json()
    except Exception:
        return {"error": "请求体需为 JSON"}
    canvas = body.get("canvas") or {}
    meta = body.get("meta") or {}
    fmt = (body.get("format") or "json").lower()
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", name).strip("_") or "workflow"
    meta.setdefault("name", safe)
    _WF_DIR.mkdir(parents=True, exist_ok=True)
    if fmt == "xml":
        from workflow_xml import canvas_to_xml
        xf = _WF_DIR / f"{safe}.xml"
        try:
            xf.write_text(canvas_to_xml(canvas, meta), encoding="utf-8")
        except Exception as e:
            return {"error": f"转 XML 失败：{type(e).__name__}: {e}"}
        for old in (_WF_DIR / f"{safe}.json", _WF_DIR / f"{safe}.json.meta", _WF_DIR / f"{safe}.xml.meta"):
            if old.exists():
                try:
                    old.unlink()
                except OSError:
                    pass
        saved_name = safe
    else:
        jf = _WF_DIR / f"{safe}.json"
        mp = jf.with_name(jf.name + ".meta")
        jf.write_text(_j.dumps(canvas, ensure_ascii=False, indent=2), encoding="utf-8")
        mp.write_text(_j.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        xo = _WF_DIR / f"{safe}.xml"
        if xo.exists():
            try:
                xo.unlink()
            except OSError:
                pass
        saved_name = safe
    if _agent is not None:
        try:
            from workflow import refresh_workflow_tools
            refresh_workflow_tools(_agent.tools, _workspace, _agent)
        except Exception:
            pass
    return {"ok": True, "name": saved_name, "format": fmt}


@app.post("/api/wf/create")
async def api_wf_create(request: Request):
    """创建新工作流。请求体: {name}。"""
    import json as _j
    try:
        body = await request.json()
    except Exception:
        return {"error": "请求体需为 JSON"}
    wname = (body.get("name") or "").strip()
    if not wname:
        return {"error": "name 不能为空"}
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", wname).strip("_") or "new_workflow"
    jf = _WF_DIR / f"{safe}.json"
    mp = jf.with_name(jf.name + ".meta")
    _WF_DIR.mkdir(parents=True, exist_ok=True)
    default_canvas = {"nodes": [
        {"id": "100001", "type": "1", "data": {"outputs": [], "trigger_parameters": []}},
        {"id": "900001", "type": "2", "data": {"inputs": {"terminatePlan": "returnVariables", "inputParameters": []}}}
    ], "edges": [], "versions": {}}
    default_meta = {"name": safe, "description": "", "enabled": True, "coze_url": ""}
    jf.write_text(_j.dumps(default_canvas, ensure_ascii=False, indent=2), encoding="utf-8")
    mp.write_text(_j.dumps(default_meta, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"ok": True, "name": safe}


@app.delete("/api/wf/{name}")
async def api_wf_delete(name: str):
    """删除工作流文件 + meta。"""
    try:
        jf = _safe_wf_path(name)
        mp = jf.with_name(jf.name + ".meta")
    except ValueError as e:
        return {"error": str(e)}
    if not jf.exists():
        return {"error": f"工作流 {name!r} 不存在"}
    jf.unlink()
    if mp.exists():
        mp.unlink()
    return {"ok": True}


# ===================== 模型配置 API =====================

@app.get("/api/models")
async def api_models():
    """返回模型列表+默认模型名。"""
    return {"models": config.MODELS, "default": config.DEFAULT_MODEL}


@app.get("/api/model-list")
async def api_model_list():
    """返回模型名→显示名映射（不含敏感信息），供工作流编辑器选模型用。"""
    return {"models": {name: p.get("display", name) for name, p in config.MODELS.items()},
            "default": config.DEFAULT_MODEL}


@app.put("/api/models")
async def api_models_save(request: Request):
    """保存模型配置到 ~/.agt/models.json。"""
    try:
        body = await request.json()
    except Exception:
        return {"error": "请求体需为 JSON"}
    models = body.get("models") or {}
    default = body.get("default") or ""
    config.save_user_models(models, default)
    m, d = config._load_models()
    config.MODELS.clear(); config.MODELS.update(m)
    config.DEFAULT_MODEL = d or config.DEFAULT_MODEL
    return {"ok": True, "default": config.DEFAULT_MODEL}


# ===================== MCP 配置 API =====================

@app.get("/api/mcp")
async def api_mcp_get():
    """读取 workspace/.mcp.json。"""
    p = _workspace / ".mcp.json"
    if not p.exists():
        return {"mcpServers": {}}
    try:
        import json as _j
        return _j.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {"mcpServers": {}}


@app.put("/api/mcp")
async def api_mcp_save(request: Request):
    """保存 workspace/.mcp.json。"""
    try:
        body = await request.json()
    except Exception:
        return {"error": "请求体需为 JSON"}
    p = _workspace / ".mcp.json"
    import json as _j
    p.write_text(_j.dumps(body, ensure_ascii=False, indent=2), encoding="utf-8")
    # 热重载：重连所有 MCP server（断开旧的，按新配置重新连）
    if _mcp_mgr:
        try:
            # 先全部断开
            for name in list(_mcp_mgr.sessions.keys()):
                _mcp_mgr.sessions.pop(name, None)
            # 重新连
            _mcp_mgr.connect_from_config(str(_workspace / ".mcp.json"))
            return {"ok": True, "reloaded": True,
                    "servers": list(_mcp_mgr.sessions.keys())}
        except Exception as e:
            return {"ok": True, "reloaded": False, "error": f"热重载失败: {e}"}
    return {"ok": True}


@app.get("/api/stats")
async def api_stats(scope: str = "current"):
    """LLM 调用可靠性统计（per-model 聚合）。scope=current/all。"""
    from llm_call_log import aggregate_calls, load_all_calls
    from session import _repo_sessions_dir
    if _agent is None:
        return {"scope": scope, "calls": 0, "stats": {}}
    if scope == "all":
        records = load_all_calls(_repo_sessions_dir(_workspace))
    else:
        records = _agent.session.llm_calls.all_records()
    return {"scope": scope, "calls": len(records), "stats": aggregate_calls(records)}


# ===================== RAG 文档库 API =====================

@app.get("/api/rag/config")
async def api_rag_config():
    """返回当前 RAG 配置（repo 字段 merge 全局 embed）。额外返回 global_embed_keys 供 UI 标注。"""
    result = dict(config.load_rag_config(_workspace))
    result["_global_embed_keys"] = list(config._GLOBAL_EMBED_KEYS)
    return result


@app.put("/api/rag/config")
async def api_rag_config_save(request: Request):
    """保存 RAG 配置并热重建实例（复用 chat.init_rag，保持两端一致）。"""
    try:
        body = await request.json()
    except Exception:
        return {"error": "请求体需为 JSON"}
    if not isinstance(body, dict):
        return {"error": "配置需为对象"}
    config.save_rag_config(_workspace, body)
    import chat as chatmod
    chatmod.init_rag(_workspace)
    return {"ok": True}


@app.get("/api/rag/stats")
async def api_rag_stats():
    inst = get_rag()
    if inst is None:
        return {"ready": False, "total_docs": 0, "dim": 0}
    return inst.stats()


@app.get("/api/rag/query")
async def api_rag_query(q: str = "", top_k: int = 5):
    """页内查询测试。"""
    inst = get_rag()
    if inst is None or inst.index.ntotal == 0:
        return {"results": [], "error": "索引未建立，请先建库"}
    try:
        hits = inst.query(q, top_k=top_k)
    except Exception as e:
        return {"results": [], "error": f"{type(e).__name__}: {e}"}
    return {"results": hits}


# ===================== 长期记忆 API =====================

def _get_ltm():
    """获取当前 Agent 的 LongTermMemory 实例。"""
    if _agent is None:
        return None
    return getattr(_agent, "ltm", None)


@app.get("/api/memory/list")
async def api_memory_list(type: str = "", q: str = ""):
    """列出记忆（可按类型+关键词过滤）。"""
    ltm = _get_ltm()
    if ltm is None:
        return {"items": [], "error": "Agent 未就绪"}
    t = type.strip() or None
    items = ltm.list(type_=t, query=q.strip() or None)
    return {"items": items}


@app.get("/api/memory/{memory_id}")
async def api_memory_get(memory_id: str):
    """获取单条记忆详情。"""
    ltm = _get_ltm()
    if ltm is None:
        return {"error": "Agent 未就绪"}
    rec = ltm.get(memory_id)
    if rec is None:
        return {"error": f"未找到 id={memory_id}"}
    return rec


@app.post("/api/memory")
async def api_memory_add(request: Request):
    """新增记忆。"""
    ltm = _get_ltm()
    if ltm is None:
        return {"error": "Agent 未就绪"}
    try:
        body = await request.json()
    except Exception:
        return {"error": "请求体需为 JSON"}
    try:
        tags = [t.strip() for t in (body.get("tags") or "").split(",") if t.strip()]
        res = ltm.add(body.get("type", "semantic"), body.get("title", ""),
                      body.get("content", ""), tags)
        return {"ok": True, **res}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


@app.put("/api/memory/{memory_id}")
async def api_memory_update(memory_id: str, request: Request):
    """更新记忆（title/content/tags 至少传一个）。"""
    ltm = _get_ltm()
    if ltm is None:
        return {"error": "Agent 未就绪"}
    try:
        body = await request.json()
    except Exception:
        return {"error": "请求体需为 JSON"}
    fields = {}
    if body.get("title"):
        fields["title"] = body["title"]
    if body.get("content"):
        fields["content"] = body["content"]
    if "tags" in body:
        fields["tags"] = [t.strip() for t in (body["tags"] or "").split(",") if t.strip()]
    if not fields:
        return {"error": "至少传 title/content/tags 之一"}
    ok = ltm.update(memory_id, **fields)
    return {"ok": ok} if ok else {"error": f"未找到 id={memory_id}"}


@app.delete("/api/memory/{memory_id}")
async def api_memory_delete(memory_id: str):
    """删除记忆。"""
    ltm = _get_ltm()
    if ltm is None:
        return {"error": "Agent 未就绪"}
    ok = ltm.delete(memory_id)
    return {"ok": ok} if ok else {"error": f"未找到 id={memory_id}"}


# ===================== WebSocket 端点 =====================

@app.websocket("/ws")
async def ws_endpoint(websocket: WebSocket):
    global _main_loop
    await websocket.accept()
    _main_loop = asyncio.get_running_loop()   # 保存 loop 供 broadcast 跨线程推送
    loop = _main_loop

    agent = _agent
    if agent is None:
        # 服务未正确注入 agent（理论上不会发生，start_server 已注入）
        await _send(websocket, {"type": "system", "text": "⚠️ Agent 未就绪，服务异常"})
        await websocket.close()
        return

    queue: asyncio.Queue = asyncio.Queue()
    registry = build_default_registry()
    client = {"ws": websocket, "queue": queue}
    _clients.append(client)

    is_reconnect = len(_event_log) > 0
    if is_reconnect:
        await _send(websocket, {"type": "system",
                                "text": "✅ 已重连（前端会自动请求当前对话历史）",
                                "models": [{"name": n, "desc": m.get("desc", "")} for n, m in config.MODELS.items()],
                                "current_model": agent.model_name})
    else:
        await _send(websocket, {
            "type": "system",
            "text": f"已连接。模型={agent.model_name}，工具 {len(list(agent.tools))} 个。直接对话，或输入 /help 看命令。",
            "models": [{"name": n, "desc": m.get("desc", "")} for n, m in config.MODELS.items()],
            "current_model": agent.model_name,
        })
    from session import list_sessions
    await _send(websocket, {"type": "sessions",
                           "names": list_sessions(workspace=_workspace)})
    from workflow import workflows_info
    await _send(websocket, {"type": "workflows", "items": workflows_info(_workspace)})

    try:
        while True:
            ws_task = asyncio.create_task(websocket.receive_text())
            queue_task = asyncio.create_task(queue.get())
            ping_task = asyncio.create_task(asyncio.sleep(30))
            done, pending = await asyncio.wait([ws_task, queue_task, ping_task],
                                               return_when=asyncio.FIRST_COMPLETED)
            for t in pending:
                t.cancel()
            if ping_task in done:
                try:
                    await websocket.send_json({"type": "_ping"})
                except Exception:
                    break
                continue
            if queue_task in done:
                ev = queue_task.result()
                try:
                    await _send(websocket, ev)
                except Exception:
                    pass
                continue
            if ws_task in done:
                raw = ws_task.result()
                await _handle_user_input(websocket, agent, raw, queue, loop, registry)
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        if client in _clients:
            _clients.remove(client)


async def _handle_user_input(ws, agent, raw, queue, loop, registry):
    """处理一条用户输入（文本/命令/action）。"""
    # JSON action?
    try:
        _d = json.loads(raw) if raw.lstrip().startswith("{") else None
    except Exception:
        _d = None
    if isinstance(_d, dict) and _d.get("action") == "restore":
        # 走 work_q → worker 线程执行：print 到 CLI + 广播 session_history 给所有 WS 客户端
        _sha = _d.get("sha", "")
        if _work_q is None:
            await _send(ws, {"type": "system", "text": "⚠️ 服务未接入主循环"})
            return
        def _do_restore():
            import chat as chatmod
            try:
                target = chatmod.restore_snapshot(agent, _sha)
                print(f"⏮ 已回溯到检查点（截掉的轮：「{(target or '')[:60]}」）")
            except Exception as e:
                print(f"❌ 回溯失败：{type(e).__name__}: {e}")
                return
            _broadcast({"type": "restored", "target": target or ""})
            _broadcast({"type": "session_history",
                        "name": agent.session.name or "(当前会话)",
                        "turns": agent.session.to_history()})
        _work_q.put(("task", _do_restore))
        await _send(ws, {"type": "system", "text": "⏮ 回溯中…"})
        return
    if isinstance(_d, dict) and _d.get("action") == "get_config":
        await _send(ws, {"type": "config", "values": read_config(agent)})
        return
    if isinstance(_d, dict) and _d.get("action") == "set_config":
        values = _d.get("values") or {}
        config.save_runtime_settings(values)
        lines = apply_config(agent, values)
        await _send(ws, {"type": "system", "text": "\n".join(lines) or "（无更改）"})
        return
    if isinstance(_d, dict) and _d.get("action") == "stop":
        agent._stop_flag = True
        await _send(ws, {"type": "system", "text": "⏹ 已请求停止…"})
        return
    if isinstance(_d, dict) and _d.get("action") == "open_terminal":
        import subprocess
        cmd = (_d.get("command") or "").strip()
        if not cmd:
            await _send(ws, {"type": "system", "text": "⚠ 未提供命令"})
            return
        try:
            subprocess.Popen(["cmd", "/c", f"start cmd /k {cmd}"], cwd=_workspace, shell=False)
            await _send(ws, {"type": "system", "text": f"✅ 已在终端中执行：{cmd[:80]}"})
        except Exception as e:
            await _send(ws, {"type": "system", "text": f"❌ 打开终端失败：{e}"})
        return
    if isinstance(_d, dict) and _d.get("action") == "list_sessions":
        from session import list_sessions
        await _send(ws, {"type": "sessions",
                         "names": list_sessions(workspace=_workspace)})
        return
    if isinstance(_d, dict) and _d.get("action") == "current_history":
        # 重连后前端请求：返回当前内存中 session 的完整历史（不从磁盘重载）
        await _send(ws, {"type": "session_history",
                         "name": agent.session.name or "(当前会话)",
                         "turns": agent.session.to_history()})
        # 补发活动 spec：spec 面板靠 spec 事件驱动，重连不会自动重放。
        # 如果有 pending spec（committed 态），用 spec_pending 让前端渲染交互气泡。
        from spec_tools import check_pending_spec, _spec_event_payload
        from survey_tools import check_pending_survey
        if not check_pending_spec(agent) and not check_pending_survey(agent):
            if getattr(agent, "active_spec", None):
                await _send(ws, _spec_event_payload(agent))
        return
    if isinstance(_d, dict) and _d.get("action") == "new_session":
        # 走 work_q：/reset 命令走和 CLI 完全相同的路径（worker dispatch → print 到 CLI）
        if _work_q is None:
            await _send(ws, {"type": "system", "text": "⚠️ 服务未接入主循环"})
            return
        _work_q.put(("user", "/reset"))
        def _sync_new():
            _broadcast({"type": "session_history",
                        "name": "(新会话)", "turns": agent.session.to_history()})
        _work_q.put(("task", _sync_new))
        await _send(ws, {"type": "system", "text": "🔄 新建中…"})
        return
    if isinstance(_d, dict) and _d.get("action") == "save_session":
        name = (_d.get("name") or "").strip() or None
        p = agent.session.save(name)
        await _send(ws, {"type": "saved", "name": agent.session.name or name})
        from session import list_sessions
        _broadcast({"type": "sessions",
                    "names": list_sessions(workspace=_workspace)})
        return
    if isinstance(_d, dict) and _d.get("action") == "load_session":
        # 走 work_q：/resume 命令走和 CLI 完全相同的路径（worker dispatch → _cmd_resume → print 到 CLI）
        _ls_name = (_d.get("name") or "").strip()
        if not _ls_name:
            await _send(ws, {"type": "system", "text": "⚠️ 未指定要恢复的会话"})
            return
        if _work_q is None:
            await _send(ws, {"type": "system", "text": "⚠️ 服务未接入主循环"})
            return
        _work_q.put(("user", f"/resume {_ls_name}"))
        # 紧随其后：广播 session_history 给所有 WS 客户端（/resume 完成后串行执行）
        def _sync_loaded():
            _broadcast({"type": "session_history",
                        "name": agent.session.name or _ls_name,
                        "turns": agent.session.to_history()})
            # 切会话后检查是否有 pending spec（committed 态→恢复等待裁定状态）
            from spec_tools import check_pending_spec, _spec_event_payload
            if not check_pending_spec(agent):
                if getattr(agent, "active_spec", None):
                    _broadcast(_spec_event_payload(agent))
        _work_q.put(("task", _sync_loaded))
        await _send(ws, {"type": "system", "text": f"🔄 恢复「{_ls_name}」中…"})
        return
    if isinstance(_d, dict) and _d.get("action") == "insert_message":
        text = (_d.get("text") or "").strip()
        if text and agent.autonomous_mode and agent.is_autonomous_active():
            agent.queue_user_message(text)
            await _send(ws, {"type": "system", "text": f"✅ 消息已入队（队列：{len(agent.pending_messages)} 条）"})
        else:
            await _send(ws, {"type": "system", "text": "⚠️ 自主模式未开启"})
        return
    if isinstance(_d, dict) and _d.get("action") == "list_workflows":
        from workflow import workflows_info
        await _send(ws, {"type": "workflows", "items": workflows_info(_workspace)})
        return
    if isinstance(_d, dict) and _d.get("action") == "reload_workflows":
        from workflow import workflows_info, refresh_workflow_tools
        ok, broken = refresh_workflow_tools(agent.tools, _workspace, agent)
        await _send(ws, {"type": "workflows", "items": workflows_info(_workspace)})
        await _send(ws, {"type": "system", "text":
                         f"🔄 已重载工作流：{len(ok)} 可用" + (f"，{len(broken)} 个失败" if broken else "")})
        return
    # spec 批阅：用户在 WebUI 点击通过/返工后，解除 commit_spec 的阻塞
    if isinstance(_d, dict) and _d.get("action") == "spec_decision":
        from spec_tools import resolve_spec_decision
        resolve_spec_decision(agent, _d.get("decision", ""), _d.get("feedback", ""))
        return
    # survey 回答：用户在 WebUI 提交问卷答案后，解除 ask_user 的阻塞
    if isinstance(_d, dict) and _d.get("action") == "survey_decision":
        from survey_tools import resolve_survey
        resolve_survey(agent, _d.get("answers", {}))
        return
    # /agent 命令的 WebUI 支持：列出团队 / 切换交互目标
    if isinstance(_d, dict) and _d.get("action") == "list_team":
        reg = getattr(agent, "registry", None)
        if not reg:
            await _send(ws, {"type": "system", "text": "(多 Agent 通信未启用：无 registry)"})
        else:
            sdir = getattr(agent.session, "session_dir", None)
            team = reg.format_team(exclude_id="", session_dir=sdir)
            cur = getattr(agent, "_active_target", "_main_")
            await _send(ws, {"type": "team_list", "team": team, "current_target": cur})
        return
    if isinstance(_d, dict) and _d.get("action") == "switch_agent":
        target_id = (_d.get("target_id") or "").strip()
        reg = getattr(agent, "registry", None)
        if not reg:
            await _send(ws, {"type": "system", "text": "(多 Agent 通信未启用：无 registry)"})
            return
        if target_id in ("_main_", "main", "back"):
            agent._active_target = "_main_"
            await _send(ws, {"type": "system", "text": "✅ 已切回主 Agent"})
            # 广播主 Agent 的 session 历史
            _broadcast({"type": "session_history",
                        "name": agent.session.name or "(当前会话)",
                        "turns": agent.session.to_history()})
            return
        entry = reg.lookup(target_id)
        if entry is None:
            await _send(ws, {"type": "system", "text": f"❌ agent_id='{target_id}' 不在注册表中"})
            return
        if entry.status == "running":
            await _send(ws, {"type": "system", "text": f"⏳ '{target_id}' 正在执行任务，完成后才能切换直接交互"})
            return
        agent._active_target = target_id
        await _send(ws, {"type": "system", "text": f"✅ 已切换到与 '{entry.name}' [{target_id}] 直接交互"})
        # 广播目标 Agent 的 session 历史
        if entry.agent:
            _broadcast({"type": "session_history",
                        "name": f"{entry.name} [{target_id}]",
                        "turns": entry.agent.session.to_history()})
        return
    if isinstance(_d, dict) and _d.get("action") == "open_coze":
        from workflow import workflows_info
        name = _d.get("name")
        url = next((it["coze_url"] for it in workflows_info(_workspace)
                    if it["name"] == name or it["tool"] == name), "") or "https://www.coze.com"
        await _send(ws, {"type": "coze_url", "url": url, "name": name})
        return
    if isinstance(_d, dict) and _d.get("action") == "debug_run":
        await _start_debug_run(ws, agent, _d.get("name", ""), _d.get("inputs") or {})
        return
    # —— 工作流热调试：替换节点配置 / 单节点重跑（保持上次 debug 的上下文）——
    if isinstance(_d, dict) and _d.get("action") == "hotswap_node":
        await _hotswap_node(ws, agent, _d)
        return
    if isinstance(_d, dict) and _d.get("action") == "rerun_node":
        await _rerun_node(ws, agent, _d)
        return
    if isinstance(_d, dict) and _d.get("action") == "list_node_outputs":
        await _list_node_outputs(ws, _d)
        return
    if isinstance(_d, dict) and _d.get("action") == "eval_node_output":
        await _eval_node_output(ws, _d)
        return
    if isinstance(_d, dict) and _d.get("action") == "rag_build":
        await _start_rag_build(ws)
        return
    if isinstance(_d, dict) and _d.get("action") == "feedback_meta":
        from feedback import VALID_KINDS, author_contact_str, _gather_env
        await _send(ws, {"type": "feedback_meta",
                         "kinds": VALID_KINDS,
                         "contact": author_contact_str(),
                         "env": _gather_env(None, agent=agent)})
        return
    if isinstance(_d, dict) and _d.get("action") == "feedback":
        from feedback import submit_feedback
        kind = (_d.get("kind") or "建议").strip()
        content = (_d.get("content") or "").strip()
        contact = (_d.get("contact") or "").strip()
        env_info = None if _d.get("include_env", True) else {}
        msg = submit_feedback(kind, content, contact, env_info=env_info, agent=agent)
        await _send(ws, {"type": "feedback_result", "ok": msg.startswith("✅"), "text": msg})
        return

    # 文本消息
    text, images = _parse_client_msg(raw)
    text = text.strip()
    if not text and not images:
        return

    # 斜杠命令（即时处理，不进 work_q）
    if text.startswith("/"):
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf):
                registry.dispatch(text, CommandContext(agent=agent))
            out = buf.getvalue().strip()
        except Exception as e:
            out = f"⚠️ 命令执行出错：{type(e).__name__}: {e}"
        if out:
            await _send(ws, {"type": "system", "text": out})
        return

    # 自主模式下插消息（即时入队，不进 work_q）
    if agent.autonomous_mode and agent.is_autonomous_active():
        agent.queue_user_message(text)
        await _send(ws, {"type": "system", "text": "✅ 消息已入队"})
        return

    # 普通对话：忙时走"中途注入"（下一步边界模型即可见、可改向），闲时正常入队下一轮。
    # 事件经 broadcast 自动推回（agent.run 在 worker 线程跑时触发）。
    if _work_q is None:
        await _send(ws, {"type": "system", "text": "⚠️ 服务未接入主循环（work_q 缺失）"})
        return
    if _state is not None and _state.get("busy"):
        # Agent 正在跑：入 pending_messages，本步边界注入，不另起下一轮
        agent.queue_user_message(text)
        await _send(ws, {"type": "system",
                         "text": f"📥 已排队并将在下一步注入当前任务（队列 {len(agent.pending_messages)} 条）"})
    else:
        _work_q.put(("user", text))
        await _send(ws, {"type": "system", "text": "✅ 已接收，处理中…"})


def _parse_client_msg(raw: str):
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            return data.get("text", ""), data.get("images") or []
    except Exception:
        pass
    return raw, []


async def _start_debug_run(ws, agent, name, inputs):
    """工作流调试执行：读画布 → 包成 task 进 work_q（与聊天串行）→ 逐节点事件经 broadcast 推流。"""
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", Path(name).name).strip("_") or "workflow"
    jf = _WF_DIR / f"{safe}.json"
    xf = _WF_DIR / f"{safe}.xml"
    canvas = None
    if jf.exists():
        canvas = json.loads(jf.read_text(encoding="utf-8"))
    elif xf.exists():
        try:
            from workflow_xml import xml_to_canvas
            canvas = xml_to_canvas(xf.read_text(encoding="utf-8"))
        except Exception as e:
            await _send(ws, {"type": "wf_debug_error", "text": f"XML 解析失败：{type(e).__name__}: {e}"})
            return
    else:
        await _send(ws, {"type": "wf_debug_error", "text": f"工作流 {name!r} 不存在"})
        return
    if _work_q is None:
        await _send(ws, {"type": "wf_debug_error", "text": "服务未接入主循环（work_q 缺失）"})
        return
    await _send(ws, {"type": "wf_debug_start", "name": name})

    def run_it():
        try:
            from workflow import execute_debug

            def on_node(ev: dict):
                phase = ev.get("phase")
                _broadcast({
                    "type": "wf_debug_node_start" if phase == "start" else "wf_debug_node_end",
                    "id": ev.get("id"), "title": ev.get("title"),
                    "ntype": ev.get("ntype"), "outputs": ev.get("outputs"),
                })
            exit_dict, order, trace = execute_debug(
                canvas, inputs or {}, tools=agent.tools, llm=agent.llm, on_node=on_node)
            _broadcast({"type": "wf_debug_done", "exit": exit_dict, "order": order, "trace": trace})
        except Exception as e:
            _broadcast({"type": "wf_debug_error", "text": f"{type(e).__name__}: {e}"})

    _work_q.put(("task", run_it))


async def _hotswap_node(ws, agent, msg: dict):
    """WS action: hotswap_node — 接收一段 XML 节点定义（或完整的替换节点 JSON），替换画布中对应节点。
    不改 edges、不动 ctx.node_outputs，下次 rerun_node 用新配置执行。"""
    import xml.etree.ElementTree as ET
    from workflow_xml import parse_xml_fragment
    from workflow import _debug_ctx as _dc
    if not _dc.get("ctx"):
        await _send(ws, {"type": "wf_debug_error", "text": "没有缓存的调试上下文——请先跑一次 debug_run"})
        return
    nid = str(msg.get("id", ""))
    xml_frag = (msg.get("xml") or "").strip()
    if not nid or not xml_frag:
        await _send(ws, {"type": "wf_debug_error", "text": "需要 id + xml"})
        return
    try:
        # 解析 XML 片段为节点 dict
        root = ET.fromstring(f"<node id=\"{nid}\">{xml_frag}</node>")
    except ET.ParseError as e:
        await _send(ws, {"type": "wf_debug_error", "text": f"XML 解析失败: {e}"})
        return
    # 在 nodes 里找到并替换
    from workflow import _debug_ctx as _dc
    nodes = _dc.get("nodes", {})
    old = nodes.get(nid)
    if old is None:
        await _send(ws, {"type": "wf_debug_error", "text": f"节点 {nid!r} 不在画布中"})
        return
    # 保留 id/type，覆盖 data（新 XML 片段只提供 data 层的增量）
    ntype = old["type"]
    new_data = parse_xml_fragment(root, ntype)
    old["data"] = {**old["data"], **new_data}  # 增量 merge
    await _send(ws, {"type": "wf_debug_hotswap", "id": nid, "text": f"节点 {nid} 配置已热替换"})


async def _rerun_node(ws, agent, msg: dict):
    """WS action: rerun_node — 用缓存 ctx 单跑指定节点，返回新的 outputs。
    不跑扇出/后续节点，只跑这一个。"""
    from workflow import _debug_ctx as _dc, _run_node_with_batch, NODE_HANDLERS
    ctx = _dc.get("ctx")
    nodes = _dc.get("nodes", {})
    if not ctx or not nodes:
        await _send(ws, {"type": "wf_debug_error", "text": "没有缓存的调试上下文——请先跑一次 debug_run"})
        return
    nid = str(msg.get("id", ""))
    node = nodes.get(nid)
    if not node:
        await _send(ws, {"type": "wf_debug_error", "text": f"节点 {nid!r} 不在画布中"})
        return
    handler = NODE_HANDLERS.get(str(node.get("type")))
    if not handler:
        await _send(ws, {"type": "wf_debug_error", "text": f"节点 {nid} 类型不支持"})
        return
    try:
        result = _run_node_with_batch(node, handler, ctx)
        outs = result.get("outputs") or {}
        ctx.node_outputs[nid] = outs   # 更新缓存
        await _send(ws, {"type": "wf_debug_rerun", "id": nid, "outputs": outs, "port": result.get("port", "")})
    except Exception as e:
        import traceback
        await _send(ws, {"type": "wf_debug_error", "text": f"重跑 {nid} 失败: {type(e).__name__}: {e}\n{traceback.format_exc()}"})


async def _list_node_outputs(ws, msg: dict):
    from workflow import _debug_ctx as _dc
    ctx = _dc.get("ctx")
    if not ctx:
        await _send(ws, {"type": "wf_debug_error", "text": "没有缓存——请先跑一次 debug_run"})
        return
    nodes = _dc.get("nodes", {})
    raw_ids = str(msg.get("node_ids", "") or "")
    ids = [x.strip() for x in raw_ids.replace("，", ",").split(",") if x.strip()] if raw_ids else []
    items = []
    targets = ids if ids else list(ctx.node_outputs.keys())
    for nid in targets:
        outs = ctx.node_outputs.get(nid)
        if outs is None: continue
        n = nodes.get(nid, {})
        items.append({"id": nid, "type": n.get("type","?"),
                      "title": (n.get("data",{}).get("nodeMeta",{}).get("title","")),
                      "ntype": str(n.get("type","")), "outputs": outs})
    await _send(ws, {"type": "wf_debug_outputs", "items": items})


async def _eval_node_output(ws, msg: dict):
    from workflow import _debug_ctx as _dc
    ctx = _dc.get("ctx")
    if not ctx:
        await _send(ws, {"type": "wf_debug_error", "text": "请先 debug_run"})
        return
    nid = str(msg.get("id", ""))
    script = (msg.get("script") or "").strip()
    outs = ctx.node_outputs.get(nid)
    if outs is None:
        await _send(ws, {"type": "wf_debug_error", "text": f"节点 {nid} 无输出"})
        return
    if not script:
        await _send(ws, {"type": "wf_debug_error", "text": "请提供 script"})
        return
    try:
        code = compile(script, f"<eval:{nid}>", "eval")
        local_vars = {"output": outs}
        result = eval(code, {"__builtins__": __builtins__}, local_vars)
        await _send(ws, {"type": "wf_debug_eval", "id": nid, "result": str(result) if result is not None else "(空)"})
    except SyntaxError:
        try:
            code = compile(script, f"<eval:{nid}>", "exec")
            local_vars = {"output": outs}
            exec(code, {"__builtins__": __builtins__}, local_vars)
            val = local_vars.get("_", None)
            await _send(ws, {"type": "wf_debug_eval", "id": nid, "result": str(val) if val is not None else "(空)"})
        except Exception as e2:
            await _send(ws, {"type": "wf_debug_error", "text": f"[脚本错误] {type(e2).__name__}: {e2}"})
    except Exception as e:
        await _send(ws, {"type": "wf_debug_error", "text": f"[脚本错误] {type(e).__name__}: {e}"})
    """RAG 建库：校验 → 包成 task 进 work_q（与聊天串行）→ 进度/完成事件经 broadcast 推流。"""
    inst = get_rag()
    cfg = config.load_rag_config(_workspace)
    if inst is None:
        await _send(ws, {"type": "rag_index_error", "text": "RAG 未启用或模型路径无效，请先保存有效配置"})
        return
    docs_dir = cfg.get("docs_dir", "")
    if not docs_dir or not Path(docs_dir).exists():
        await _send(ws, {"type": "rag_index_error", "text": f"docs_dir 不存在：{docs_dir}"})
        return
    if _work_q is None:
        await _send(ws, {"type": "rag_index_error", "text": "服务未接入主循环（work_q 缺失）"})
        return
    await _send(ws, {"type": "rag_index_start"})

    def run_it():
        try:
            def on_progress(done, total, f):
                _broadcast({"type": "rag_index_progress", "done": done, "total": total, "file": f})
            res = inst.index_dir(
                docs_dir,
                exts=tuple(cfg.get("exts") or [".md", ".txt", ".json"]),
                exclude_globs=cfg.get("exclude_globs") or [],
                lines_per=cfg.get("lines_per", 60),
                overlap=cfg.get("overlap", 15),
                batch=cfg.get("batch", 32),
                on_progress=on_progress,
            )
            _broadcast({"type": "rag_index_done", **res})
        except Exception as e:
            _broadcast({"type": "rag_index_error", "text": f"{type(e).__name__}: {e}"})

    _work_q.put(("task", run_it))


# ===================== 服务启停（供 chat.py / commands.py 调用） =====================

def lan_urls(port) -> list:
    """枚举本机网卡 IPv4，返回 [http://<ip>:<port>/ ...]（供局域网设备连接提示）。"""
    urls = []
    try:
        hostname = socket.gethostname()
        for info in socket.getaddrinfo(hostname, None):
            ip = info[4][0]
            if ":" in ip or ip.startswith("127."):
                continue
            u = f"http://{ip}:{port}/"
            if u not in urls:
                urls.append(u)
    except Exception:
        pass
    return urls or [f"http://<本机IP>:{port}/"]


def open_browser(port):
    """在本机默认浏览器打开 WebUI。"""
    try:
        webbrowser.open(f"http://127.0.0.1:{port}/")
    except Exception:
        pass


def server_status() -> dict:
    running = _server is not None and getattr(_server, "started", False)
    return {
        "running": running,
        "port": _port,
        "local_url": f"http://127.0.0.1:{_port}/" if _port else "",
        "lan_urls": lan_urls(_port) if _port else [],
        "error": _server_error,
    }


def start_server(*, agent, work_q, mcp_mgr=None, workspace=WORKSPACE, port=8000, state=None):
    """启动内嵌 Web 服务（后台 daemon 线程跑 uvicorn），注入 agent/work_q/state。
    state 为 chat 主循环的 state dict（含 busy），供 WS 文本按忙/闲路由。
    返回 (ok, msg)。端口被占 / 已在运行 → (False, 原因)。"""
    global _agent, _work_q, _mcp_mgr, _workspace, _server, _port, _server_thread, _server_error, _state
    if _server is not None:
        return (False, f"服务已在运行（端口 {_port}），先用 /web stop 再启动")
    # 端口探测（占用则立即失败，不进 uvicorn）
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("0.0.0.0", port))
    except OSError:
        return (False, f"端口 {port} 已占用或无权限")
    _agent, _work_q, _mcp_mgr, _workspace, _state = agent, work_q, mcp_mgr, workspace, state
    _port, _server_error = port, None
    config_obj = uvicorn.Config(app, host="0.0.0.0", port=port, log_level="warning")
    srv = uvicorn.Server(config_obj)
    _server = srv

    def _run():
        global _main_loop, _server_error
        loop = asyncio.new_event_loop()
        _main_loop = loop
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(srv.serve())
        except Exception as e:
            _server_error = f"{type(e).__name__}: {e}"
        finally:
            _main_loop = None

    _server_thread = threading.Thread(target=_run, daemon=True)
    _server_thread.start()
    # 等 uvicorn 起来（serve() 内部置 started=True）
    for _ in range(40):
        if srv.started:
            break
        if _server_error:
            break
        time.sleep(0.1)
    if not srv.started:
        _server = None
        return (False, f"启动失败：{_server_error or '未知原因'}")
    return (True, f"服务已启动 @ 0.0.0.0:{port}")


def stop_server():
    """停止服务并释放端口。返回 (ok, msg)。"""
    global _server, _main_loop, _clients
    if _server is None:
        return (False, "服务未运行")
    port = _port
    try:
        _server.should_exit = True
    except Exception:
        pass
    _server = None
    _main_loop = None
    _clients = []          # 断开所有 WS 客户端
    return (True, f"服务已停止（端口 {port} 已释放）")


def stop_server_if_running():
    """chat 退出兜底：若 /web 起过服务，停掉释放端口（无服务时 no-op）。"""
    if _server is not None:
        stop_server()
