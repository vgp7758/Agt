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
# ★ SDK 导出名是 streamablehttp_client（一个词，无下划线）——此前 import streamable_http_client
#   恒 ImportError → None → 所有 HTTP 型 MCP 报"当前 mcp SDK 不支持 streamable_http 传输"，
#   从未真正工作过（2026-09-26 连 videoclipper@18180 时实测抓出）
try:
    from mcp.client.streamable_http import streamablehttp_client as streamable_http_client
except ImportError:
    try:   # 极旧 SDK 的别名兜底
        from mcp.client.streamable_http import streamable_http_client
    except ImportError:
        streamable_http_client = None
try:
    from mcp.client.sse import sse_client
except ImportError:
    sse_client = None

# mcp_client.py 自身目录（保留备查）。注意：server 子进程的默认 cwd 现在跟随 chat.py
# 的启动目录(cwd)，而不再锚定在这里——这样 .mcp.json 的查找目录(WORKSPACE=cwd)与 server
# 执行目录一致：从哪个目录启动就用哪个目录的脚本/.env。.mcp.json 里仍可用 "cwd" 字段覆盖。
PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _extract_text(result) -> str:
    """从 CallToolResult.content（一组内容块）拼出文本；isError 时加前缀。
    图片块(ImageContent)转成标准 data URL 文本（而非 str(block) 乱码），
    交由 agent._materialize_tool_result 统一落盘成 <img> 标签。"""
    parts = []
    for block in (getattr(result, "content", None) or []):
        if getattr(block, "type", None) == "image":
            mt = getattr(block, "mimeType", None) or "image/png"
            data = getattr(block, "data", "")
            parts.append(f"data:{mt};base64,{data}")
            continue
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
                "description": getattr(mcp_tool, "description", None) or "",
                # mcp SDK 版本兼容：1.18- 字段名 inputSchema（camelCase），更新版
                # 改为 input_schema（snake_case，pydantic AttributeError 提示同名）。
                # CI 装最新 mcp / 本机旧版 / 用户任意版本都要能跑（CI 实测炸点）。
                "parameters": (getattr(mcp_tool, "inputSchema", None)
                               or getattr(mcp_tool, "input_schema", None)
                               or {"type": "object", "properties": {}}),
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

    def _run_coro(self, coro, timeout=None):
        """把协程提交到后台 loop 执行，阻塞等待结果。
        timeout：仅连接握手场景传（防单个 server 挂起无限拖死装配）；
        工具调用不传——长任务工具可能跑很久，不能误杀。超时取消协程并抛 TimeoutError。"""
        fut = asyncio.run_coroutine_threadsafe(coro, self.loop)
        try:
            return fut.result(timeout)
        except TimeoutError:
            fut.cancel()
            raise

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
                self._run_coro(self._connect_one(name, cfg), timeout=60)
            except Exception as e:
                print(f"[MCP] 连接 server '{name}' 失败：{type(e).__name__}: {e}")

    async def _connect_one(self, name: str, cfg: dict):
        if cfg.get("url"):
            # HTTP/SSE 传输（远程 server）：transport 默认 http，可显式 sse
            url = cfg["url"]
            transport = (cfg.get("transport") or ("http" if streamable_http_client else "sse")).lower()
            headers = cfg.get("headers")
            if transport == "sse":
                if sse_client is None:
                    raise RuntimeError("当前 mcp SDK 不支持 sse 传输")
                ctx = sse_client(url, headers=headers) if headers else sse_client(url)
            else:
                if streamable_http_client is None:
                    raise RuntimeError("当前 mcp SDK 不支持 streamable_http 传输")
                ctx = streamable_http_client(url, headers=headers) if headers else streamable_http_client(url)
            transport_label = transport
        else:
            # stdio 传输（本地进程）
            params = StdioServerParameters(
                command=cfg["command"],
                args=cfg.get("args", []),
                env=cfg.get("env"),
                cwd=cfg.get("cwd") or str(Path.cwd()),   # 空串/None 回退当前目录（Win 上 cwd="" 会 WinError 123）
            )
            ctx = stdio_client(params)
            transport_label = "stdio"
        res = await self._stack.enter_async_context(ctx)
        read, write = res[0], res[1]   # http/sse 可能返回 (read, write, resp)，取前两个兼容
        session = await self._stack.enter_async_context(ClientSession(read, write))
        await session.initialize()
        tools_resp = await session.list_tools()
        # server 级工具白名单（可选）：tools: ["A", "B"] 只注册列出的工具——
        # 有些 server（如 claude mcp serve）一次吐 20+ 个工具，与本地工具语义撞车，按需收窄。
        allow = cfg.get("tools")
        if allow:
            allow_set = set(allow)
            before = len(tools_resp.tools)
            tools_resp.tools = [t for t in tools_resp.tools if t.name in allow_set]
            skipped = before - len(tools_resp.tools)
        else:
            skipped = 0
        self.sessions[name] = {"session": session, "tools": tools_resp.tools}
        extra = f"（白名单收窄：{len(tools_resp.tools)}/{before}，跳过 {skipped}）" if skipped else ""
        print(f"[MCP] 已连接 '{name}'（{transport_label}），发现 {len(tools_resp.tools)} 个工具{extra}")

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
        self._run_coro(self._connect_one(name, cfg), timeout=60)
        print(f"[MCP] 已重连 '{name}'，发现 {len(self.sessions[name]['tools'])} 个工具")

    # —— 运行时直接连新 server（不依赖 .mcp.json，供 ensure_lsp 动态装配用）——
    def connect_one(self, name: str, cfg: dict) -> None:
        """运行时连一个新 MCP server（直接吃 cfg dict：command/args/env/cwd）。
        同名已存在则先断开旧会话再连。供 ensure_lsp 等按需装配，无需重启 Agent。"""
        self.sessions.pop(name, None)
        self._run_coro(self._connect_one(name, cfg), timeout=60)
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
        # 600s 兜底（2026-10-06·50052 实锤：MCP server 侧 hang 时 future.result() 无限等，
        # react worker 卡死 35min+ 只能杀进程——超长任务应转后台而非靠 MCP 无限等）
        return self._run_coro(self._call(server, name, args), timeout=600)

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


def make_mcp_tools(mcp_mgr, config_path, agent=None) -> list:
    """生成 reload_mcp_server 工具（闭包绑定 MCPManager + 配置路径）。

    config_path: 单个路径或路径列表——server 可能注册在 repo 的 .mcp.json，也可能只在
    全局 ~/.agt/mcp.json（如 scnet），逐个配置找同名 server 重连。
    agent: 传入后，重连会**同步工具到工具箱**（摘旧 `__mcp__{server}__*` + 注册新发现的）
    ——否则给 MCP server 新增工具必须 /restart 才可见（2026-09-14 用户实测缺口）。"""
    from tools import Tool

    paths = [config_path] if isinstance(config_path, (str, Path)) else list(config_path)

    def reload_mcp_server(name: str = "") -> str:
        """断开并重连 MCP server，并把工具同步进工具箱（新增/删除的工具立即生效，无需 /restart）。
        name: mcp.json（repo 或全局）中 mcpServers 下的键名（如 'agentank' / 'scnet'）；
        【留空 = 全量重载】（2026-09-18·用户提案）：断开全部、重读两级配置
        （repo .mcp.json + 全局 ~/.agt/mcp.json，后连覆盖先连——与启动语义一致）、
        重建全部连接并全量同步工具——新增/删除的 server 立即生效。"""
        if not (name or "").strip():
            # —— 全量重载 ——
            old_servers = set(mcp_mgr.sessions.keys())
            mcp_mgr.sessions.clear()          # 断开全部（旧进程待 shutdown 一起清理，同单连口径）
            loaded, errs = [], []
            for p in paths:
                try:
                    before = set(mcp_mgr.sessions.keys())
                    mcp_mgr.connect_from_config(str(p))   # 该配置里全部 server（同名后连覆盖）
                    loaded.extend(f"{s}（{Path(p).name}）" for s in set(mcp_mgr.sessions.keys()) - before)
                except Exception as e:
                    errs.append(f"  · {p}: {type(e).__name__}: {e}")
            sync = ""
            if agent is not None:
                dropped = 0
                for s in old_servers - set(mcp_mgr.sessions.keys()):   # 消失的 server：摘其工具
                    dropped += agent.tools.drop(f"__mcp__{s}__")
                for k in [k for k in list(getattr(agent, "tool_groups", {}))
                          if k.startswith("__mcp__") and k not in agent.tools]:
                    del agent.tool_groups[k]
                added = mcp_mgr.sync_to_toolbox(agent.tools)
                for t in mcp_mgr.get_tools():
                    if t.name.startswith("__mcp__"):
                        agent.tool_groups[t.name] = "MCP"
                sync = f"；工具同步：摘除 {dropped} / 新增 {len(added)} / 现存 {len(mcp_mgr.get_tools())}"
            body = f"✅ MCP 全量重载：{len(mcp_mgr.sessions)} 个 server 在线 [{', '.join(mcp_mgr.sessions)}]{sync}"
            if errs:
                body += "\n失败配置：\n" + "\n".join(errs)
            return body
        errs = []
        for p in paths:
            try:
                mcp_mgr.reconnect_from_config_one(str(p), name)
                sync = ""
                if agent is not None:
                    prefix = f"__mcp__{name}__"
                    dropped = agent.tools.drop(prefix)          # 摘掉该 server 的旧工具
                    for k in [k for k in list(getattr(agent, "tool_groups", {}))
                              if k.startswith(prefix) and k not in agent.tools]:
                        del agent.tool_groups[k]                # 清理悬空 group 映射
                    added = mcp_mgr.sync_to_toolbox(agent.tools)   # 复用既有同步器（幂等注册，返回新加入）
                    for t in mcp_mgr.get_tools():
                        if t.name.startswith(prefix):
                            agent.tool_groups[t.name] = "MCP"
                    sync = f"，工具已同步（摘除 {dropped} 个 / 新增 {len(added)} 个）"
                return f"✅ MCP server '{name}' 已重新连接（配置：{p}）{sync}"
            except Exception as e:
                errs.append(f"  · {p}: {type(e).__name__}: {e}")
        return f"[重连失败] 未在任何配置中找到 server '{name}'\n" + "\n".join(errs)

    return [Tool(reload_mcp_server)]


# ================= CLI 子命令（用户提案 2026-10-11，Claude Code 风格对齐）=================
# agt mcp add|list|remove|get —— 无需手编 mcp.json 的快捷通道。
# 配置写 workspace/.mcp.json（repo 级，跟随 agt 启动目录；--scope user 写 ~/.agt/mcp.json），
# 合并写（保留现有 servers）；add 后尝试真实连接一次并打印发现的工具数（失败不回滚——
# 配置已就位，可 /reload_mcp 或重启后再试）。完整能力（两级配置/编辑器/热重载）在 WebUI 设置。

def _mcp_cli_paths(workspace):
    """两级 mcp.json 路径：(project 路径, user 路径)。"""
    from pathlib import Path as _P
    try:
        from config import _AGT_DIR
        user = _AGT_DIR / "mcp.json"
    except Exception:
        user = _P.home() / ".agt" / "mcp.json"
    return _P(workspace or _P.cwd()) / ".mcp.json", user


def _mcp_cli_usage() -> str:
    return (
        "用法：\n"
        "  agt mcp add [--transport http|sse|stdio] [--env K=V] [--header K=V] [--scope project|user] "
        "<name> <commandOrUrl> [args...]\n"
        "  agt mcp list                              两级配置合并视图\n"
        "  agt mcp get <name>                        查看某 server 的配置 JSON\n"
        "  agt mcp remove <name> [--scope project|user]   删除（缺 scope 时自动找有配置的那份）\n\n"
        "示例：\n"
        "  agt mcp add ping python D:/AI/mcp/ping.py\n"
        "  agt mcp add --transport sse test http://xxx/mcp --header Authorization=Bearer_sk-x\n"
        "  agt mcp add --env KEY=v worker node D:/AI/mcp/worker.js\n\n"
        "说明：commandOrUrl 以 http(s):// 开头 → 远程传输（默认 http）；否则 stdio 本地进程。\n"
        "远程鉴权用 --header（如 Authorization=Bearer_xxx）；--env 是 stdio 子进程的环境变量。\n"
        "写入后自动尝试连接一次并打印发现的工具数（失败不回滚——配置已就位，可再试）。")


def _mcp_cli_load(path) -> dict:
    from pathlib import Path as _P
    p = _P(path)
    if not p.exists():
        return {"mcpServers": {}}
    try:
        return json.loads(p.read_text(encoding="utf-8")) or {"mcpServers": {}}
    except Exception:
        return {"mcpServers": {}}


def _mcp_cli_save(path, data: dict) -> None:
    from pathlib import Path as _P
    p = _P(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


async def _mcp_cli_verify_one(name: str, cfg: dict):
    """独立连接验证（不动 MCPManager 全局状态）：返回 (ok, 描述, 工具名清单)。"""
    try:
        from contextlib import AsyncExitStack
        stack = AsyncExitStack()
        if cfg.get("url"):
            transport = (cfg.get("transport") or "http").lower()
            if transport == "sse":
                if sse_client is None:
                    return False, "mcp 库缺 sse 支持", []
                ctx = sse_client(cfg["url"], headers=cfg.get("headers"))
            else:
                if streamable_http_client is None:
                    return False, "mcp 库缺 streamable_http 支持", []
                ctx = streamable_http_client(cfg["url"], headers=cfg.get("headers"))
            transport_label = transport
        else:
            params = StdioServerParameters(
                command=cfg["command"], args=cfg.get("args", []),
                env=cfg.get("env"), cwd=cfg.get("cwd") or None)
            ctx = stdio_client(params)
            transport_label = "stdio"
        read, write = (await stack.enter_async_context(ctx))[:2]
        session = await stack.enter_async_context(ClientSession(read, write))
        await session.initialize()
        tools = (await session.list_tools()).tools
        names = [t.name for t in tools]
        await stack.aclose()
        return True, f"{transport_label} · {len(tools)} 个工具", names
    except Exception as e:
        return False, f"{type(e).__name__}: {str(e)[:140]}", []


def run_mcp_cli(argv, workspace=None) -> int:
    """agt mcp <add|list|remove|get> 子命令入口（chat.main 拦截分发）。返回进程退出码。"""
    from pathlib import Path as _P
    sub = (argv[0] if argv else "").lower()
    if sub in ("", "-h", "--help", "help"):
        print(_mcp_cli_usage())
        return 0
    ws = _P(workspace or _P.cwd())
    proj, user = _mcp_cli_paths(ws)

    if sub == "add":
        pos = []
        env, headers = {}, {}
        transport = None
        scope = "project"
        i = 1                                  # argv[0]="add"，从 1 起（完整索引，无切片/推进叠加错位）
        n = len(argv)
        while i < n:
            a = argv[i]
            if a in ("--transport", "--scope"):
                if i + 1 >= n:
                    print(f"❌ {a} 缺值"); return 2
                if a == "--transport": transport = argv[i + 1]
                else: scope = argv[i + 1]
                i += 2
            elif a in ("--env", "--header"):
                if i + 1 >= n:
                    print(f"❌ {a} 缺值"); return 2
                k, _, v = argv[i + 1].partition("=")
                if not k.strip():
                    print(f"❌ {a} 值须为 K=V 形式"); return 2
                (env if a == "--env" else headers)[k.strip()] = v
                i += 2
            elif a.startswith("--"):
                print(f"❌ 未知选项 {a}"); print(_mcp_cli_usage()); return 2
            else:
                pos.append(a)
                i += 1
        if len(pos) < 2:
            print("❌ 缺 name / commandOrUrl"); print(_mcp_cli_usage()); return 2
        name, first = pos[0], pos[1]
        if first.startswith(("http://", "https://")):
            cfg = {"url": first, "transport": (transport or "http").lower()}
            if headers:
                cfg["headers"] = headers
        else:
            cfg = {"command": first, "args": pos[2:]}
            if env:
                cfg["env"] = env
        path = user if scope == "user" else proj
        data = _mcp_cli_load(path)
        existed = name in data.get("mcpServers", {})
        data.setdefault("mcpServers", {})[name] = cfg
        _mcp_cli_save(path, data)
        print(f"✅ 已{'覆盖更新' if existed else '添加'} server '{name}' → {path}")
        print(f"   {json.dumps(cfg, ensure_ascii=False)[:160]}")
        ok, desc, names = asyncio.run(_mcp_cli_verify_one(name, cfg))
        print((f"{'✅' if ok else '❌'} 连接验证：{desc}") if ok
              else (f"❌ 连接验证失败：{desc}\n   （配置已写入；服务未就绪/网络不通时先放着，" 
                    "agent 启动或 /reload_mcp 时会再连）"))
        if names:
            print("   工具：" + "、".join(names[:10]) + (f" …共 {len(names)} 个" if len(names) > 10 else ""))
        return 0

    if sub == "list":
        merged, src_of = {}, {}
        for src, p in (("project", proj), ("user", user)):
            for name, cfg in (_mcp_cli_load(p).get("mcpServers") or {}).items():
                if name in src_of and src == "user":
                    continue   # project（repo）优先：与 connect_from_config 路径顺序一致，同名 user 让位
                merged[name] = cfg; src_of[name] = src
        # 修正优先级：project 覆盖 user（与 connect_from_config 的路径顺序一致：repo 在前）
        if not merged:
            print("（两级配置均无 mcpServers）")
            return 0
        print(f"{'名称':<16} {'来源':<9} 传输/命令")
        for name, cfg in merged.items():
            desc = cfg.get("url") or " ".join([cfg.get("command", "")] + cfg.get("args", []))
            print(f"  {name:<14} {src_of.get(name, ''):<8} {desc[:70]}")
        return 0

    if sub == "get":
        name = argv[1] if len(argv) > 1 else ""
        for src, p in (("project", proj), ("user", user)):
            cfg = (_mcp_cli_load(p).get("mcpServers") or {}).get(name)
            if cfg is not None:
                print(f"[{src}] {p}")
                print(json.dumps(cfg, ensure_ascii=False, indent=2))
                return 0
        print(f"[无此 server] {name}")
        return 1

    if sub in ("remove", "rm"):
        name = argv[1] if len(argv) > 1 else ""
        scope = "auto"
        if "--scope" in argv:
            scope = argv[argv.index("--scope") + 1]
        removed = False
        for src, p in (("project", proj), ("user", user)):
            if scope != "auto" and src != scope:
                continue
            servers = _mcp_cli_load(p).get("mcpServers") or {}
            if name in servers:
                del servers[name]
                _mcp_cli_save(p, {"mcpServers": servers})
                print(f"✅ 已从 {src} 配置删除 '{name}'（{p}）")
                removed = True
        if not removed:
            print(f"[未找到] {name}")
            return 1
        return 0

    print(f"❌ 未知子命令 {sub}")
    print(_mcp_cli_usage())
    return 2
