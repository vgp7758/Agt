"""web.py —— Agent WebUI 后端（FastAPI + WebSocket）。

浏览器 → WS /ws → 每个连接一个独立 Agent（自带会话）→ 在线程里跑 Agent.run，
事件经 asyncio.Queue 桥接到 WS 协程实时推给前端。斜杠命令复用 CommandRegistry
（用 redirect_stdout 捕获其打印输出作为 system 事件回传）。

跑法：python web.py  →  浏览器打开 http://127.0.0.1:8000
"""
from __future__ import annotations

import asyncio
import contextlib
import io
import json
import threading
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

# 全局 MCP（启动时连接一次，所有 WS 连接共享）
_mcp = MCPManager()
_mcp.connect_from_config(str(WORKSPACE / ".mcp.json"))
_MCP_TOOLS = _mcp.get_tools()
_snap = SnapshotManager(WORKSPACE)  # 工作区检查点快照/回溯（独立 git 仓库）

_INDEX_HTML = (Path(__file__).resolve().parent / "static" / "index.html").read_text(encoding="utf-8")


def _new_agent(on_event) -> Agent:
    """每个 WS 连接建一个独立 Agent（独立会话），注册全部工具。"""
    agent = Agent(chatmod.SYSTEM, REAL_TOOLS, enable_thinking=True,
                  max_steps=50, token_budget=80000, verbose=False, on_event=on_event,
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
    return agent


async def _send(ws: WebSocket, obj: dict):
    await ws.send_text(json.dumps(obj, ensure_ascii=False))


@app.get("/")
async def index():
    return HTMLResponse(_INDEX_HTML)


@app.websocket("/ws")
async def ws_endpoint(websocket: WebSocket):
    await websocket.accept()
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue()
    registry = build_default_registry()

    def on_event(ev: dict):
        # Agent 在工作线程里调用；跨线程把事件投到本连接的队列
        loop.call_soon_threadsafe(queue.put_nowait, ev)

    agent = _new_agent(on_event)
    await _send(websocket, {
        "type": "system",
        "text": f"已连接。模型={agent.model_name}，工具 {len(list(agent.tools))} 个。直接对话，或输入 /help 看命令。",
        "models": [{"name": n, "desc": m.get("desc", "")} for n, m in config.MODELS.items()],
        "current_model": agent.model_name,
    })

    try:
        while True:
            raw = await websocket.receive_text()
            # 检查点回溯请求 {action:"restore", sha}
            try:
                _d = json.loads(raw) if raw.lstrip().startswith("{") else None
            except Exception:
                _d = None
            if isinstance(_d, dict) and _d.get("action") == "restore":
                sha = _d.get("sha", "")
                try:
                    _snap.restore(sha)
                    target = agent.session.restore_to_snapshot(sha)
                    await _send(websocket, {"type": "restored", "target": target or ""})
                except Exception as e:
                    await _send(websocket, {"type": "system", "text": f"⚠️ 回溯失败：{type(e).__name__}: {e}"})
                continue
            if isinstance(_d, dict) and _d.get("action") == "get_config":
                await _send(websocket, {"type": "config", "values": read_config(agent)})
                continue
            if isinstance(_d, dict) and _d.get("action") == "set_config":
                lines = apply_config(agent, _d.get("values") or {})
                await _send(websocket, {"type": "system", "text": "\n".join(lines) or "（无更改）"})
                continue
            if isinstance(_d, dict) and _d.get("action") == "stop":
                agent._stop_flag = True
                await _send(websocket, {"type": "system", "text": "⏹ 已请求停止，当前步完成后 Agent 会停下来。"})
                continue
            if isinstance(_d, dict) and _d.get("action") == "list_sessions":
                from session import list_sessions
                sessions = list_sessions(workspace=WORKSPACE)
                await _send(websocket, {"type": "sessions", "names": [p.stem for p in sessions]})
                continue
            if isinstance(_d, dict) and _d.get("action") == "new_session":
                from session import Session
                agent.session = Session(agent.base_system, llm=agent.llm,
                                        recent_window_turns=agent.session.recent_window_turns)
                await _send(websocket, {"type": "system", "text": "🔄 已创建新会话。"})
                continue
            if isinstance(_d, dict) and _d.get("action") == "save_session":
                name = (_d.get("name") or "").strip() or None
                p = agent.session.save(name)
                await _send(websocket, {"type": "saved", "name": p.stem})
                from session import list_sessions
                sessions = list_sessions(workspace=WORKSPACE)
                await _send(websocket, {"type": "sessions", "names": [s.stem for s in sessions]})
                continue
            # 纯自主模式下插入新消息（即使 busy 也可以发送）
            if isinstance(_d, dict) and _d.get("action") == "insert_message":
                text = _d.get("text", "").strip()
                if not text:
                    continue
                if agent.autonomous_mode and agent.is_autonomous_active():
                    agent.queue_user_message(text)
                    await _send(websocket, {"type": "system",
                                            "text": f"✅ 消息已加入队列（当前队列：{len(agent.pending_messages)} 条），将在当前任务完成后处理"})
                else:
                    await _send(websocket, {"type": "system",
                                            "text": "⚠️ 纯自主模式未开启，无法插入消息。先用 /autonomous on <时间> 开启"})
                continue
            text, images = _parse_client_msg(raw)
            text = text.strip()
            if not text and not images:
                continue
            # 斜杠命令：捕获其 stdout 作为 system 事件（忽略图片）；命令异常也不崩连接
            if text.startswith("/"):
                buf = io.StringIO()
                try:
                    with contextlib.redirect_stdout(buf):
                        registry.dispatch(text, CommandContext(agent=agent))
                    out = buf.getvalue().strip()
                except Exception as e:
                    out = f"⚠️ 命令执行出错：{type(e).__name__}: {e}"
                if out:
                    await _send(websocket, {"type": "system", "text": out})
                continue
            # 普通对话：线程跑 Agent 并流式推送事件（用户气泡由前端本地渲染，可含图片）
            await _run_streaming(websocket, agent, text, images, queue, loop)
    except WebSocketDisconnect:
        pass


def _parse_client_msg(raw: str):
    """解析客户端消息：优先 JSON {text, images}，否则当作纯文本。"""
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            return data.get("text", ""), data.get("images") or []
    except Exception:
        pass
    return raw, []


async def _run_streaming(ws: WebSocket, agent: Agent, msg: str, images: list, queue: asyncio.Queue, loop):
    """在工作线程跑 agent.run，主协程从队列消费事件推给前端，直到 _done。"""

    def run_it():
        try:
            agent.run(msg, images=images)
        except Exception as e:
            loop.call_soon_threadsafe(queue.put_nowait,
                                      {"type": "error", "text": f"{type(e).__name__}: {e}"})
        finally:
            loop.call_soon_threadsafe(queue.put_nowait, {"type": "_done"})

    threading.Thread(target=run_it, daemon=True).start()
    while True:
        ev = await queue.get()
        await _send(ws, ev)  # 含 _done：转发给前端（前端据此重新启用输入）
        if ev.get("type") == "_done":
            break


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("web:app", host="127.0.0.1", port=8000, reload=False)
