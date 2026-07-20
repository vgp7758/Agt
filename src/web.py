"""web.py —— Agent WebUI 后端（FastAPI + WebSocket）。

关键设计：
  - Agent 全局单例（断线重连不丢会话/状态/自主模式）
  - 事件缓冲（断线期间的事件保留，重连后回放最近 N 条）
  - WebSocket 心跳（防休眠断连）

跑法：python web.py  →  浏览器打开 http://127.0.0.1:8000
"""
from __future__ import annotations

import asyncio
from pathlib import Path
import contextlib
import io
import json
import threading
import time

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse

import chat as chatmod
import config
from agent import Agent
from agent_config import SKILL_TOOLS
from commands import CommandContext, build_default_registry, apply_config, read_config
from mcp_client import MCPManager, make_mcp_tools
from multiagent import make_subagent_tools
from plan_tools import make_plan_tools
from memory_tools import make_recall_tools
from background_tools import make_background_tools
from wiki import make_wiki_tools
from real_tools import REAL_TOOLS, WORKSPACE, make_autonomous_tools
from snapshots import SnapshotManager
from workflow import refresh_workflow_tools, make_workflow_mgmt_tools
from lsp_manager import make_lsp_tools
from rag import LocalRAG, set_rag as _rag_set, get_rag, make_rag_tools
from session import session_meta

app = FastAPI(title="Agt Agent WebUI")


@app.on_event("startup")
def _on_startup():
    """启动时播种默认 RAG 配置 + 按 config 建全局 RAG 实例（enabled 且模型路径有效时才真正建）。"""
    config.seed_rag_config(WORKSPACE)
    _rebuild_rag()


@app.on_event("shutdown")
def _on_shutdown():
    """进程退出时停后台服务（防孤儿进程）+ 停调度器。"""
    if _agent is not None:
        try:
            _agent.shutdown()
        except Exception:
            pass

# 全局 MCP（启动时连接一次）
_mcp = MCPManager()
_mcp.connect_from_config(str(WORKSPACE / ".mcp.json"))
_mcp.connect_from_config(str(Path.home() / ".agt" / "mcp.json"))   # 全局已装 LSP（ensure_lsp 装配的）
_MCP_TOOLS = _mcp.get_tools()
_snap = SnapshotManager(WORKSPACE)

_INDEX_HTML = (Path(__file__).resolve().parent / "static" / "index.html").read_text(encoding="utf-8")

# ===== 全局 Agent 单例 + 事件缓冲 + 多客户端广播 =====
_agent: Agent | None = None
_agent_busy: bool = False
_event_log: list[tuple[int, dict]] = []
_seq: int = 0
_clients: list[dict] = []     # [{ws, queue}]  所有活跃连接
_main_loop = None             # 主 event loop（_broadcast 在线程里用到）
_bg_drain_started = False     # 全局后台 drain 协程仅启动一次的标志
_rag_busy: bool = False          # RAG 建库锁（独立于 _agent_busy，建库不阻塞聊天）


def _broadcast(ev: dict):
    """记录事件到日志缓冲 + 广播给所有活跃客户端。"""
    global _seq, _main_loop
    _seq += 1
    _event_log.append((_seq, ev))
    if len(_event_log) > 500:
        _event_log.pop(0)
    loop = _main_loop or asyncio.get_event_loop()
    for c in _clients:
        try:
            loop.call_soon_threadsafe(c["queue"].put_nowait, ev)
        except Exception:
            pass


def _rebuild_rag():
    """按当前 .agent/rag.json 重建全局 LocalRAG 实例（配置保存/服务启动时调）。
    enabled 关或模型路径无效 → 实例为 None（rag_query 返回未建库提示）。"""
    try:
        inst = LocalRAG.from_config(WORKSPACE)
    except Exception as e:
        print(f"[rag] 加载失败：{e}")
        inst = None
    _rag_set(inst, str(WORKSPACE))


def _get_or_create_agent() -> Agent:
    """获取全局 Agent（首次创建，之后复用）。on_event 广播给所有客户端。"""
    global _agent
    if _agent is not None:
        return _agent
    agent = Agent(chatmod.SYSTEM, REAL_TOOLS, enable_thinking=True,
                  max_steps=50, token_budget=80000, verbose=False, on_event=_broadcast,
                  snapshot_manager=_snap)
    for t in _MCP_TOOLS:
        agent.tools.register(t)
    for t in make_subagent_tools(agent):
        agent.tools.register(t)
    for t in SKILL_TOOLS:
        agent.tools.register(t)
    for t in make_plan_tools(agent):
        agent.tools.register(t)
    for t in make_recall_tools(agent):
        agent.tools.register(t)
    for t in make_background_tools(agent):
        agent.tools.register(t)
    for t in make_wiki_tools(agent):
        agent.tools.register(t)
    for t in make_mcp_tools(_mcp, str(WORKSPACE / ".mcp.json")):
        agent.tools.register(t)
    for t in make_autonomous_tools(agent):
        agent.tools.register(t)
    for t in make_workflow_mgmt_tools(WORKSPACE):
        agent.tools.register(t)
    for t in make_lsp_tools(agent, _mcp):
        agent.tools.register(t)
    for t in make_rag_tools():
        agent.tools.register(t)
    refresh_workflow_tools(agent.tools, WORKSPACE, agent)
    # 加载持久化运行时设置
    saved = config.load_runtime_settings()
    if saved:
        apply_config(agent, saved)
    _agent = agent
    return agent


async def _send(ws: WebSocket, obj: dict):
    await ws.send_text(json.dumps(obj, ensure_ascii=False))


_WF_DIR = WORKSPACE / ".agent" / "workflows"
_EDITOR_HTML = (Path(__file__).resolve().parent / "static" / "workflow_editor.html").read_text(encoding="utf-8")
_RAG_HTML = (Path(__file__).resolve().parent / "static" / "rag.html").read_text(encoding="utf-8")


def _safe_wf_path(name: str) -> Path:
    """解析工作流文件名，防越界。自动补 .json 后缀。"""
    safe = Path(name).name
    if safe != name or not safe:
        raise ValueError(f"非法文件名: {name!r}")
    if not safe.endswith(".json"):
        safe = safe + ".json"
    return _WF_DIR / safe


@app.get("/")
async def index():
    return HTMLResponse(_INDEX_HTML)


@app.get("/editor")
async def workflow_editor():
    return HTMLResponse(_EDITOR_HTML)


@app.get("/wfdebug")
async def workflow_debug():
    """工作流调试页：只读渲染画布 + 流式执行 + 逐节点查看输出。"""
    html = (Path(__file__).resolve().parent / "static" / "workflow_debug.html").read_text(encoding="utf-8")
    return HTMLResponse(html)


@app.get("/rag")
async def rag_page():
    """RAG 文档库管理页：配置 + 建库 + 查询测试。"""
    return HTMLResponse(_RAG_HTML)


# ===== 工作流编辑器 REST API =====

@app.get("/api/wf/list")
async def api_wf_list():
    """列出所有工作流（名称+状态摘要）。"""
    from workflow import workflows_info
    items = []
    for it in workflows_info(WORKSPACE):
        items.append({"name": it["name"], "tool": it["tool"], "status": it["status"],
                       "detail": it["detail"], "description": it["description"], "coze_url": it["coze_url"]})
    return {"items": items}


@app.get("/api/tools")
async def api_tools():
    """返回工作流可调用的全部工具（内置 + MCP + 用户 py 工具），供编辑器生成工具节点。
    每个含 name/display/group/description/params/outputs，让插件节点能按 toolName 显示输入字段、
    工具面板按来源分组。outputs 优先用用户声明的 OUTPUT_SCHEMA。"""
    from real_tools import ALL_BUILTIN_TOOLS, infer_tool_outputs
    from workflow import load_user_tools
    user_tools, _ = load_user_tools(WORKSPACE)
    # 三类来源（顺序即优先级：内置 > MCP > 用户，同名去重保留前者）
    sources = [
        (list(ALL_BUILTIN_TOOLS), "内置"),
        (list(_MCP_TOOLS), None),         # MCP 的 group 按 server 动态生成
        (user_tools, "用户工具"),
    ]
    out, seen = [], set()
    for tools, default_group in sources:
        for t in tools:
            if t.name in seen:
                continue
            seen.add(t.name)
            s = t.schema["function"]
            props = s.get("parameters", {}).get("properties", {}) or {}
            params = [{"name": pn, "type": (ps.get("type") if isinstance(ps, dict) else "string") or "string"}
                      for pn, ps in props.items()]
            outputs = getattr(t, "user_outputs", None) or infer_tool_outputs(t)
            # 分组与显示名：MCP 工具名长（__mcp__server__tool），美化成 server.tool
            name = s["name"]
            if name.startswith("__mcp__"):
                server = getattr(t, "server", "") or ""
                orig = getattr(t, "orig_name", "") or name
                group = f"MCP · {server}" if server else "MCP"
                display = f"{server}.{orig}" if server else orig
            else:
                group = default_group or "其它"
                display = name
            out.append({"name": name, "display": display, "group": group,
                        "description": s.get("description", ""), "params": params, "outputs": outputs})
    return {"tools": out}


@app.get("/api/wf/{name}")
async def api_wf_get(name: str):
    """获取单个工作流画布 JSON + meta。优先 .json，否则 .xml（转 JSON）。"""
    import json as _j
    import re
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
        # XML：转 canvas，meta 从根属性（复用 scan 的解析）
        from workflow_xml import xml_to_canvas, WorkflowXmlError
        import xml.etree.ElementTree as ET
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
    """保存工作流画布 + meta。请求体: {canvas, meta, format?}。
    format='xml' 时转成 XML 写 .xml（meta 入根属性）；否则写 Coze JSON .json + .meta。
    切换格式时删除另一扩展名的旧文件，避免同名重复。"""
    import json as _j
    import re
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
        # 切换格式：删除同名 .json / .json.meta（meta 已入 XML 根属性）
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
        # 切换格式：删除同名 .xml
        xo = _WF_DIR / f"{safe}.xml"
        if xo.exists():
            try:
                xo.unlink()
            except OSError:
                pass
        saved_name = safe
    # 如果 Agent存在，刷新工作流工具
    global _agent
    if _agent is not None:
        try:
            refresh_workflow_tools(_agent.tools, WORKSPACE, _agent)
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
    import re
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


# ===== 模型配置 API =====

@app.get("/api/models")
async def api_models():
    """返回模型列表+默认模型名。"""
    return {"models": config.MODELS, "default": config.DEFAULT_MODEL}


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
    # 热更新：重新加载 MODELS/DEFAULT_MODEL
    m, d = config._load_models()
    config.MODELS.clear(); config.MODELS.update(m)
    config.DEFAULT_MODEL = d or config.DEFAULT_MODEL
    return {"ok": True, "default": config.DEFAULT_MODEL}


# ===== RAG 文档库 API =====

@app.get("/api/rag/config")
async def api_rag_config():
    """返回当前 RAG 配置（.agent/rag.json）。"""
    return config.load_rag_config(WORKSPACE)


@app.put("/api/rag/config")
async def api_rag_config_save(request: Request):
    """保存 RAG 配置并热重建实例。"""
    try:
        body = await request.json()
    except Exception:
        return {"error": "请求体需为 JSON"}
    if not isinstance(body, dict):
        return {"error": "配置需为对象"}
    config.save_rag_config(WORKSPACE, body)
    _rebuild_rag()
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


@app.websocket("/ws")
async def ws_endpoint(websocket: WebSocket):
    global _agent_busy
    global _main_loop
    await websocket.accept()
    _main_loop = asyncio.get_running_loop()  # 保存主循环供 _broadcast 线程用
    loop = _main_loop
    global _bg_drain_started
    if not _bg_drain_started:
        _bg_drain_started = True
        asyncio.create_task(_bg_drain())   # 全局后台 drain：inbox→触发 run（仅启动一次）
    queue: asyncio.Queue = asyncio.Queue()
    registry = build_default_registry()

    # 获取/创建 Agent（断线重连/多客户端复用）
    try:
        agent = _get_or_create_agent()
        print("[WS] agent ready", flush=True)
    except Exception as e:
        print(f"[WS] agent failed: {e}", flush=True)
        import traceback; traceback.print_exc()
        await websocket.close()
        return

    # 注册到客户端列表（广播目标）
    client = {"ws": websocket, "queue": queue}
    _clients.append(client)

    # 判断是首次连接还是重连
    is_reconnect = len(_event_log) > 0
    if is_reconnect:
        # 回放最近 40 条事件（断线期间错过的）
        replay = _event_log[-40:]
        await _send(websocket, {"type": "system",
                                "text": f"✅ 已重连到现有会话（回放最近 {len(replay)} 条事件）",
                                "models": [{"name": n, "desc": m.get("desc", "")} for n, m in config.MODELS.items()],
                                "current_model": agent.model_name})
        for _seq_num, ev in replay:
            await _send(websocket, ev)
        if _agent_busy:
            await _send(websocket, {"type": "system", "text": "⏳ Agent 正在执行任务，事件继续推送中…"})
    else:
        await _send(websocket, {
            "type": "system",
            "text": f"已连接。模型={agent.model_name}，工具 {len(list(agent.tools))} 个。直接对话，或输入 /help 看命令。",
            "models": [{"name": n, "desc": m.get("desc", "")} for n, m in config.MODELS.items()],
            "current_model": agent.model_name,
        })
    # 发送会话列表
    from session import list_sessions
    await _send(websocket, {"type": "sessions", "names": [session_meta(p) for p in list_sessions(workspace=WORKSPACE)]})
    # 发送工作流列表
    from workflow import workflows_info
    await _send(websocket, {"type": "workflows", "items": workflows_info(WORKSPACE)})

    # ===== 主循环：同时监听 WS 输入 + 队列事件 + 心跳 =====
    try:
        while True:
            # 用 select 模式：等 WS 输入 或 队列事件 或 心跳超时
            ws_task = asyncio.create_task(websocket.receive_text())
            queue_task = asyncio.create_task(queue.get())
            ping_task = asyncio.create_task(asyncio.sleep(30))

            done, pending = await asyncio.wait(
                [ws_task, queue_task, ping_task],
                return_when=asyncio.FIRST_COMPLETED
            )
            for t in pending:
                t.cancel()

            # ---- 心跳 ----
            if ping_task in done:
                try:
                    await websocket.send_json({"type": "_ping"})
                except Exception:
                    break  # WS 已断
                continue

            # ---- Agent 事件 ----
            if queue_task in done:
                ev = queue_task.result()
                try:
                    await _send(websocket, ev)
                except Exception:
                    pass  # WS 断了，事件留在 _event_log 里
                continue

            # ---- 用户输入 ----
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
    global _agent_busy

    # JSON action?
    try:
        _d = json.loads(raw) if raw.lstrip().startswith("{") else None
    except Exception:
        _d = None
    if isinstance(_d, dict) and _d.get("action") == "restore":
        try:
            _snap.restore(_d.get("sha", ""))
            target = agent.session.restore_to_snapshot(_d.get("sha", ""))
            await _send(ws, {"type": "restored", "target": target or ""})
        except Exception as e:
            await _send(ws, {"type": "system", "text": f"⚠️ 回溯失败：{type(e).__name__}: {e}"})
        return
    if isinstance(_d, dict) and _d.get("action") == "get_config":
        await _send(ws, {"type": "config", "values": read_config(agent)})
        return
    if isinstance(_d, dict) and _d.get("action") == "set_config":
        values = _d.get("values") or {}
        config.save_runtime_settings(values)  # 先存（apply_config 会 pop fallback_chain）
        lines = apply_config(agent, values)
        await _send(ws, {"type": "system", "text": "\n".join(lines) or "（无更改）"})
        return
    if isinstance(_d, dict) and _d.get("action") == "stop":
        agent._stop_flag = True
        await _send(ws, {"type": "system", "text": "⏹ 已请求停止…"})
        return
    if isinstance(_d, dict) and _d.get("action") == "list_sessions":
        from session import list_sessions
        await _send(ws, {"type": "sessions", "names": [session_meta(p) for p in list_sessions(workspace=WORKSPACE)]})
        return
    if isinstance(_d, dict) and _d.get("action") == "new_session":
        from session import Session
        agent.set_session(Session(agent.base_system, llm=agent.llm,
                                  recent_window_turns=agent.session.recent_window_turns))
        agent.plan = []                  # 新会话：清空计划与自主模式
        agent.exit_autonomous_mode()
        agent.goal_check_script = ""
        await _send(ws, {"type": "system", "text": "🔄 已创建新会话。"})
        return
    if isinstance(_d, dict) and _d.get("action") == "save_session":
        name = (_d.get("name") or "").strip() or None
        p = agent.session.save(name)
        await _send(ws, {"type": "saved", "name": p.stem})
        from session import list_sessions
        await _send(ws, {"type": "sessions", "names": [session_meta(s) for s in list_sessions(workspace=WORKSPACE)]})
        return
    if isinstance(_d, dict) and _d.get("action") == "load_session":
        from session import Session
        _ls_name = (_d.get("name") or "").strip()
        if not _ls_name:
            await _send(ws, {"type": "system", "text": "⚠️ 未指定要恢复的会话"})
            return
        try:
            new_session = Session.load(_ls_name, llm=agent.llm, workspace=WORKSPACE)
        except Exception as e:
            await _send(ws, {"type": "system", "text": f"❌ 恢复失败：{type(e).__name__}: {e}"})
            return
        agent.set_session(new_session)   # 切换 + 恢复 plan/自主模式状态
        # 把该 session 的完整历史原样推给前端渲染（user/工具调用/回答，不含思考过程）
        await _send(ws, {"type": "session_history", "name": agent.session.name or _ls_name,
                         "turns": agent.session.to_history()})
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
        await _send(ws, {"type": "workflows", "items": workflows_info(WORKSPACE)})
        return
    if isinstance(_d, dict) and _d.get("action") == "reload_workflows":
        from workflow import workflows_info
        ok, broken = refresh_workflow_tools(agent.tools, WORKSPACE, agent)
        await _send(ws, {"type": "workflows", "items": workflows_info(WORKSPACE)})
        await _send(ws, {"type": "system", "text":
                         f"🔄 已重载工作流：{len(ok)} 可用" + (f"，{len(broken)} 个失败" if broken else "")})
        return
    if isinstance(_d, dict) and _d.get("action") == "open_coze":
        from workflow import workflows_info
        name = _d.get("name")
        url = next((it["coze_url"] for it in workflows_info(WORKSPACE)
                    if it["name"] == name or it["tool"] == name), "") or "https://www.coze.com"
        await _send(ws, {"type": "coze_url", "url": url, "name": name})
        return
    if isinstance(_d, dict) and _d.get("action") == "debug_run":
        await _start_debug_run(ws, agent, _d.get("name", ""), _d.get("inputs") or {})
        return
    if isinstance(_d, dict) and _d.get("action") == "rag_build":
        await _start_rag_build(ws)
        return

    # 文本消息
    text, images = _parse_client_msg(raw)
    text = text.strip()
    if not text and not images:
        return

    # 斜杠命令
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

    # 普通对话（或自主模式下插消息）
    if agent.autonomous_mode and agent.is_autonomous_active():
        agent.queue_user_message(text)
        await _send(ws, {"type": "system", "text": f"✅ 消息已入队"})
        return

    # 跑 Agent
    if _agent_busy:
        await _send(ws, {"type": "system", "text": "⏳ Agent 正忙，请稍候或用停止按钮。"})
        return

    await _run_streaming(ws, agent, text, images, queue, loop)


def _parse_client_msg(raw: str):
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            return data.get("text", ""), data.get("images") or []
    except Exception:
        pass
    return raw, []


async def _run_streaming(ws, agent, msg, images, queue, loop):
    """跑 Agent，事件通过 _broadcast 广播到所有客户端；
    本连接从自己的 queue 消费，直到 _done。"""
    global _agent_busy
    _agent_busy = True
    try:
        def run_it():
            try:
                agent.run(msg, images=images)
            except Exception as e:
                _broadcast({"type": "error", "text": f"{type(e).__name__}: {e}"})
            finally:
                _broadcast({"type": "_done"})

        threading.Thread(target=run_it, daemon=True).start()

        # 本连接从自己的 queue 消费事件
        while True:
            ev = await queue.get()
            try:
                await _send(ws, ev)
            except Exception:
                pass  # WS 断了；事件留在 _event_log 里，重连时回放
            if ev.get("type") == "_done":
                break
    finally:
        _agent_busy = False


async def _bg_drain():
    """全局后台 drain：轮询 agent.inbox，Agent 空闲时触发一轮 run，事件 _broadcast 给所有客户端。
    占 _agent_busy 锁，与聊天/调试串行（run 非线程安全）。仅启动一次（_bg_drain_started）。"""
    global _agent_busy
    while True:
        await asyncio.sleep(0.5)
        agent = _agent
        if agent is None or _agent_busy:
            continue
        item = agent.pop_inbox()
        if not item:
            continue
        src, msg = item
        _agent_busy = True

        def run_it():
            global _agent_busy
            try:
                _broadcast({"type": "background_trigger", "source": src, "text": msg[:100]})
                agent.run(msg)
            except Exception as e:
                _broadcast({"type": "error", "text": f"后台触发失败：{type(e).__name__}: {e}"})
            finally:
                _broadcast({"type": "_done"})
                _agent_busy = False

        threading.Thread(target=run_it, daemon=True).start()


async def _start_debug_run(ws, agent, name, inputs):
    """启动工作流调试执行（后台线程跑 execute_debug），每个节点事件经 _broadcast 实时推流。
    占用 _agent_busy 锁（共用 agent.llm/tools，不能和聊天并发）。"""
    global _agent_busy
    # 读画布（复用 /api/wf/{name} 的 json/xml 读取逻辑）
    import re
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
    if _agent_busy:
        await _send(ws, {"type": "wf_debug_error", "text": "⏳ Agent 正忙，请稍候再试"})
        return
    _agent_busy = True
    await _send(ws, {"type": "wf_debug_start", "name": name})

    def run_it():
        global _agent_busy
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
            import traceback
            traceback.print_exc()
            _broadcast({"type": "wf_debug_error", "text": f"{type(e).__name__}: {e}"})
        finally:
            _agent_busy = False

    threading.Thread(target=run_it, daemon=True).start()


async def _start_rag_build(ws):
    """后台建库：threading.Thread 跑 index_dir，on_progress 经 _broadcast 推 rag_index_progress，
    末尾推 rag_index_done / rag_index_error。独立 _rag_busy 锁（建库不阻塞聊天）。"""
    global _rag_busy
    inst = get_rag()
    cfg = config.load_rag_config(WORKSPACE)
    if inst is None:
        await _send(ws, {"type": "rag_index_error", "text": "RAG 未启用或模型路径无效，请先保存有效配置"})
        return
    docs_dir = cfg.get("docs_dir", "")
    if not docs_dir or not Path(docs_dir).exists():
        await _send(ws, {"type": "rag_index_error", "text": f"docs_dir 不存在：{docs_dir}"})
        return
    if _rag_busy:
        await _send(ws, {"type": "rag_index_error", "text": "⏳ 正在建库，请稍候"})
        return
    _rag_busy = True
    await _send(ws, {"type": "rag_index_start"})

    def run_it():
        global _rag_busy
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
            import traceback; traceback.print_exc()
            _broadcast({"type": "rag_index_error", "text": f"{type(e).__name__}: {e}"})
        finally:
            _rag_busy = False

    threading.Thread(target=run_it, daemon=True).start()


def main():
    import uvicorn
    uvicorn.run("src.web:app", host="0.0.0.0", port=8000, reload=False)

if __name__ == "__main__":
    main()
