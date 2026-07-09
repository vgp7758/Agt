"""web.py —— Agent WebUI 后端（FastAPI + WebSocket）。

关键设计：
  - Agent 全局单例（断线重连不丢会话/状态/自主模式）
  - 事件缓冲（断线期间的事件保留，重连后回放最近 N 条）
  - WebSocket 心跳（防休眠断连）

跑法：python web.py  →  浏览器打开 http://127.0.0.1:8000
"""
from __future__ import annotations

import asyncio
import contextlib
import io
import json
import threading
import time
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse

import chat as chatmod
import config
from agent import Agent
from agent_config import SKILL_TOOLS
from commands import CommandContext, build_default_registry, apply_config, read_config
from mcp_client import MCPManager, make_mcp_tools
from multiagent import make_subagent_tools
from plan_tools import make_plan_tools
from wiki import make_wiki_tools
from real_tools import REAL_TOOLS, WORKSPACE, make_autonomous_tools
from snapshots import SnapshotManager

app = FastAPI(title="Agt Agent WebUI")

# 全局 MCP（启动时连接一次）
_mcp = MCPManager()
_mcp.connect_from_config(str(WORKSPACE / ".mcp.json"))
_MCP_TOOLS = _mcp.get_tools()
_snap = SnapshotManager(WORKSPACE)

_INDEX_HTML = (Path(__file__).resolve().parent / "static" / "index.html").read_text(encoding="utf-8")

# ===== 全局 Agent 单例 + 事件缓冲 + 多客户端广播 =====
_agent: Agent | None = None
_agent_busy: bool = False
_event_log: list[tuple[int, dict]] = []
_seq: int = 0
_clients: list[dict] = []     # [{ws, queue}]  所有活跃连接


def _broadcast(ev: dict):
    """记录事件到日志缓冲 + 广播给所有活跃客户端。"""
    global _seq
    _seq += 1
    _event_log.append((_seq, ev))
    if len(_event_log) > 500:
        _event_log.pop(0)
    for c in _clients:
        try:
            asyncio.get_event_loop().call_soon_threadsafe(c["queue"].put_nowait, ev)
        except Exception:
            pass


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
    for t in make_wiki_tools(agent):
        agent.tools.register(t)
    for t in make_mcp_tools(_mcp, str(WORKSPACE / ".mcp.json")):
        agent.tools.register(t)
    for t in make_autonomous_tools(agent):
        agent.tools.register(t)
    _agent = agent
    return agent


async def _send(ws: WebSocket, obj: dict):
    await ws.send_text(json.dumps(obj, ensure_ascii=False))


@app.get("/")
async def index():
    return HTMLResponse(_INDEX_HTML)


@app.websocket("/ws")
async def ws_endpoint(websocket: WebSocket):
    global _agent_busy
    await websocket.accept()
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue()
    registry = build_default_registry()

    # 获取/创建 Agent（断线重连/多客户端复用）
    agent = _get_or_create_agent()

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
    await _send(websocket, {"type": "sessions", "names": [p.stem for p in list_sessions(workspace=WORKSPACE)]})

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
        lines = apply_config(agent, _d.get("values") or {})
        await _send(ws, {"type": "system", "text": "\n".join(lines) or "（无更改）"})
        return
    if isinstance(_d, dict) and _d.get("action") == "stop":
        agent._stop_flag = True
        await _send(ws, {"type": "system", "text": "⏹ 已请求停止…"})
        return
    if isinstance(_d, dict) and _d.get("action") == "list_sessions":
        from session import list_sessions
        await _send(ws, {"type": "sessions", "names": [p.stem for p in list_sessions(workspace=WORKSPACE)]})
        return
    if isinstance(_d, dict) and _d.get("action") == "new_session":
        from session import Session
        agent.session = Session(agent.base_system, llm=agent.llm,
                                recent_window_turns=agent.session.recent_window_turns)
        await _send(ws, {"type": "system", "text": "🔄 已创建新会话。"})
        return
    if isinstance(_d, dict) and _d.get("action") == "save_session":
        name = (_d.get("name") or "").strip() or None
        p = agent.session.save(name)
        await _send(ws, {"type": "saved", "name": p.stem})
        from session import list_sessions
        await _send(ws, {"type": "sessions", "names": [s.stem for s in list_sessions(workspace=WORKSPACE)]})
        return
    if isinstance(_d, dict) and _d.get("action") == "insert_message":
        text = (_d.get("text") or "").strip()
        if text and agent.autonomous_mode and agent.is_autonomous_active():
            agent.queue_user_message(text)
            await _send(ws, {"type": "system", "text": f"✅ 消息已入队（队列：{len(agent.pending_messages)} 条）"})
        else:
            await _send(ws, {"type": "system", "text": "⚠️ 自主模式未开启"})
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


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("web:app", host="0.0.0.0", port=8000, reload=False)
