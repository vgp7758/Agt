"""mcp_client.py —— MCP client：从 .mcp.json 连接 MCP server，发现工具供 Agent 使用。

设计要点：
  - MCP SDK 是异步的，我们的 Agent 循环是同步的 → 用【后台线程跑一个 asyncio 事件循环】
    做桥，对外暴露同步接口（connect_from_config / call_tool_sync / get_tools / shutdown）。
  - 用 AsyncExitStack 长期持有 stdio_client + ClientSession（连接建立后一直开着）。
  - 工具名按 MCP 惯例加 server 命名空间：__mcp__<server>__<tool>，防撞名、标来源；
    MCPTool 内部记 server + 原始工具名，调用时按原始名路由到对应 session。
"""
from __future__ import annotations

import asyncio
import json
import threading
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Optional

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

# mcp_client.py 自身目录（保留备查）。注意：server 子进程的默认 cwd 现在跟随 chat.py
# 的启动目录(cwd)，而不再锚定在这里——这样 .mcp.json 的查找目录(WORKSPACE=cwd)与 server
# 执行目录一致：从哪个目录启动就用哪个目录的脚本/.env。.mcp.json 里仍可用 "cwd" 字段覆盖。
PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _extract_text(result) -> str:
    """从 CallToolResult.content（一组内容块）拼出文本；isError 时加前缀。"""
    parts = []
    for block in (getattr(result, "content", None) or []):
        text = getattr(block, "text", None)
        parts.append(text if text is not None else str(block))
    out = "\n".join(parts).strip() or "(空结果)"
    if getattr(result, "isError", False):
        out = "[MCP 工具错误] " + out
    return out


class MCPTool:
    """与 tools.Tool 同接口（.name/.schema/.run），供 Toolbox 透明使用。"""

    def __init__(self, manager: "MCPManager", server: str, mcp_tool):
        self.manager = manager
        self.server = server
        self.orig_name = mcp_tool.name
        self.name = f"__mcp__{server}__{mcp_tool.name}"   # 带命名空间的全名
        self.schema = {
            "type": "function",
            "function": {
                "name": self.name,
                "description": mcp_tool.description or "",
                "parameters": mcp_tool.inputSchema or {"type": "object", "properties": {}},
            },
        }

    def run(self, **kwargs) -> str:
        """调用对应 MCP 工具（用原始名），返回文本；出错也返回文本而非抛异常。"""
        try:
            return self.manager.call_tool_sync(self.server, self.orig_name, kwargs)
        except Exception as e:
            return f"[MCP 调用出错] {type(e).__name__}: {e}"

    def __repr__(self):
        return f"MCPTool({self.name})"


class MCPManager:
    def __init__(self):
        self.loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._stack = AsyncExitStack()
        self.sessions: dict[str, dict] = {}   # server -> {session, tools}
        self._thread.start()

    def _run_loop(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def _run_coro(self, coro):
        """把协程提交到后台 loop 执行，阻塞等待结果。"""
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result()

    # —— 连接 ——
    def connect_from_config(self, path: str) -> None:
        """读取 .mcp.json，连接其中所有 stdio server，发现工具。失败只告警不中断。"""
        cfg_path = Path(path)
        if not cfg_path.exists():
            print(f"[MCP] 未找到 {path}，跳过（Agent 将不带 MCP 工具）")
            return
        config = json.loads(cfg_path.read_text(encoding="utf-8"))
        servers = config.get("mcpServers", {})
        for name, cfg in servers.items():
            try:
                self._run_coro(self._connect_one(name, cfg))
            except Exception as e:
                print(f"[MCP] 连接 server '{name}' 失败：{type(e).__name__}: {e}")

    async def _connect_one(self, name: str, cfg: dict):
        params = StdioServerParameters(
            command=cfg["command"],
            args=cfg.get("args", []),
            env=cfg.get("env"),
            cwd=cfg.get("cwd", str(Path.cwd())),
        )
        read, write = await self._stack.enter_async_context(stdio_client(params))
        session = await self._stack.enter_async_context(ClientSession(read, write))
        await session.initialize()
        tools_resp = await session.list_tools()
        self.sessions[name] = {"session": session, "tools": tools_resp.tools}
        print(f"[MCP] 已连接 '{name}'，发现 {len(tools_resp.tools)} 个工具")

    # —— 重连 ——
    def reconnect_from_config_one(self, path: str, name: str) -> None:
        """断开并重连 .mcp.json 中指定的 server。旧进程待 shutdown 时清理。"""
        cfg_path = Path(path)
        if not cfg_path.exists():
            raise RuntimeError(f".mcp.json 不存在: {path}")
        config = json.loads(cfg_path.read_text(encoding="utf-8"))
        cfg = config.get("mcpServers", {}).get(name)
        if not cfg:
            raise RuntimeError(f"在 .mcp.json 中未找到 server '{name}'")
        self.sessions.pop(name, None)   # 断开旧会话（旧进程不主动杀，等 stack 一起清理）
        self._run_coro(self._connect_one(name, cfg))
        print(f"[MCP] 已重连 '{name}'，发现 {len(self.sessions[name]['tools'])} 个工具")

    # —— 运行时直接连新 server（不依赖 .mcp.json，供 ensure_lsp 动态装配用）——
    def connect_one(self, name: str, cfg: dict) -> None:
        """运行时连一个新 MCP server（直接吃 cfg dict：command/args/env/cwd）。
        同名已存在则先断开旧会话再连。供 ensure_lsp 等按需装配，无需重启 Agent。"""
        self.sessions.pop(name, None)
        self._run_coro(self._connect_one(name, cfg))
        print(f"[MCP] 已动态连接 '{name}'，发现 {len(self.sessions[name]['tools'])} 个工具")

    def sync_to_toolbox(self, toolbox) -> list:
        """把当前所有 server 的 MCPTool 同步注册进 toolbox（register_or_replace 幂等）。
        返回【本次新加入】的工具全名列表，供调用方提示 Agent。"""
        try:
            existing = set((getattr(toolbox, "_tools", {}) or {}).keys())
        except Exception:
            existing = set()
        added = []
        for t in self.get_tools():
            if t.name not in existing:
                added.append(t.name)
            toolbox.register_or_replace(t)
        return added

    # —— 调用 ——
    def call_tool_sync(self, server: str, name: str, args: dict) -> str:
        if server not in self.sessions:
            return f"[MCP] 未知 server '{server}'"
        return self._run_coro(self._call(server, name, args))

    async def _call(self, server: str, name: str, args: dict) -> str:
        session = self.sessions[server]["session"]
        result = await session.call_tool(name, args)
        return _extract_text(result)

    # —— 工具列表 ——
    def get_tools(self) -> list:
        """所有 server 的工具拍平成 MCPTool 列表（带命名空间名）。"""
        tools = []
        for server, info in self.sessions.items():
            for t in info["tools"]:
                tools.append(MCPTool(self, server, t))
        return tools

    # —— 关闭 ——
    def shutdown(self):
        # 不显式 aclose 上下文（anyio cancel scope 有任务亲和性，跨任务 aclose 会报错）。
        # 直接停 loop；进程退出时 server 子进程的 stdin 被 EOF，自行退出。
        self.loop.call_soon_threadsafe(self.loop.stop)


def make_mcp_tools(mcp_mgr, config_path: str) -> list:
    """生成 reload_mcp_server 工具（闭包绑定 MCPManager + .mcp.json 路径）。"""
    from tools import Tool

    def reload_mcp_server(name: str) -> str:
        """断开并重连指定 MCP server。当该 server 的代码被修改后调用，无需重启 Agent。
        name: .mcp.json 中 mcpServers 下的键名（如 'agentank'）。"""
        try:
            mcp_mgr.reconnect_from_config_one(config_path, name)
            return f"✅ MCP server '{name}' 已重新连接"
        except Exception as e:
            return f"[重连失败] {type(e).__name__}: {e}"

    return [Tool(reload_mcp_server)]
