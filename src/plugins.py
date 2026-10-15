"""plugins.py —— 引擎级插件系统（P0+P1，设计 docs/plugin-sdk-design.md，2026-10-14 用户提案）。

一个插件 = 一个目录 + manifest.yml：
    <repo>/.agent/plugins/<name>/   repo 级（随仓库走；同名覆盖全局）
    ~/.agt/plugins/<name>/          全局级（本机个人）
        manifest.yml    必需：name/version/description/backend/frontend/exposed_to_llm
        backend.py      可选：后端入口，约定 setup(api)（PluginAPI 注册工具/命令/路由/钩子）
        frontend.js     可选：前端入口（浏览器 AgtPlugin SDK：addButton/addPanel/addChatCard）
        tools/ workflows/ agents/ skills/  可选静态资产（未来 provides 扩展点，当前未扫）

信任模型（同 .agent/tools/、Obsidian）：目录内代码会被 import 执行，勿装不受信插件。

生命周期（可逆副作用，DSH 同款语义）：setup 时每次注册都记 undo → unload/reload
按插件粒度撤销全部注册（工具摘除/命令删表/路由摘挂/钩子摘链）。单个插件 setup 抛异常
→ warn 日志 + 跳过，绝不炸启动。

挂载点（引擎侧三处）：
    chat.build_agent   → load_plugins(agent)         装配期加载
    agent._exec_tool   → run_pre_hooks/run_post_hooks 工具执行管线钩子（零插件零开销）
    commands.build_default_registry → append_commands 每次构建追加插件命令（WS/CLI 两入口自动生效）
    server.start_server → attach_routes(app)          插件 HTTP 路由动态挂载
"""
from __future__ import annotations

import json
import logging
import re
import sys
import threading
from dataclasses import dataclass, field
from fastapi import Request as _FastAPIRequest   # 模块级：future-annotations 字符串化注解的
# resolve 依赖模块 globals（函数体内 import 的名字 get_type_hints 找不到——插件路由 422
# 实锤 2026-10-15：request 注解被当 query 参数）
from pathlib import Path

_LOG = logging.getLogger("agt.plugins")

# ===================== manifest =====================

@dataclass
class PluginManifest:
    """manifest.yml 的解析结果。name 是全局唯一键（repo 级覆盖全局级同名）。"""
    name: str
    version: str = "0.0.0"
    description: str = ""
    backend: str = ""          # 后端入口文件名（相对插件根；空=无后端）
    frontend: str = ""         # 前端入口文件名（空=无前端）
    exposed_to_llm: bool = True  # False=register_tool 的工具不投影给 LLM（hidden）
    dir: Path = None           # 插件根目录
    scope: str = "repo"        # repo | global（发现时定，同名 repo 赢）


def _parse_manifest(dir_path: Path) -> PluginManifest:
    """解析 manifest.yml（宽松：缺字段用默认值；缺 name 用目录名）。"""
    import yaml
    meta = {}
    try:
        meta = yaml.safe_load((dir_path / "manifest.yml").read_text(encoding="utf-8")) or {}
    except Exception as e:
        raise ValueError(f"manifest.yml 解析失败：{e}")
    if not isinstance(meta, dict):
        raise ValueError("manifest.yml 顶层须为映射")
    name = str(meta.get("name") or "").strip() or dir_path.name
    if not re.fullmatch(r"[A-Za-z0-9_\-]+", name):
        raise ValueError(f"插件名 {name!r} 仅允许字母/数字/_/-")
    return PluginManifest(
        name=name,
        version=str(meta.get("version") or "0.0.0").strip(),
        description=str(meta.get("description") or "").strip(),
        backend=str(meta.get("backend") or "").strip(),
        frontend=str(meta.get("frontend") or "").strip(),
        exposed_to_llm=bool(meta.get("exposed_to_llm", True)),
        dir=dir_path,
    )


# ===================== 发现 + 启用状态 =====================

def _state_file(workspace: Path) -> Path:
    return workspace / ".agent" / "plugins_state.json"


def _load_state(workspace: Path) -> dict:
    """启用状态：{name: {"enabled": bool}}。默认全部启用。"""
    try:
        return json.loads(_state_file(workspace).read_text(encoding="utf-8"))
    except Exception:
        return {}


def set_enabled(workspace: Path, name: str, enabled: bool) -> str:
    """启用/禁用某插件（写状态文件）。返回人话结果。"""
    manifests = discover(workspace)
    m = next((x for x in manifests if x.name == name), None)
    if m is None:
        return f"❌ 插件 {name} 不存在（/plugins 看清单）"
    st = _load_state(workspace)
    st.setdefault(name, {})["enabled"] = bool(enabled)
    st[name]["scope"] = m.scope
    _state_file(workspace).parent.mkdir(parents=True, exist_ok=True)
    _state_file(workspace).write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")
    return f"✅ 插件 {name} 已{'启用' if enabled else '禁用'}（reload 后生效：/reload plugins 或 /restart）"


def discover(workspace: Path) -> list[PluginManifest]:
    found: dict[str, PluginManifest] = {}
    global_dir = Path.home() / ".agt" / "plugins"
    repo_dir = Path(workspace) / ".agent" / "plugins"
    for scope, base in (("global", global_dir), ("repo", repo_dir)):   # repo 后扫=覆盖
        if not base.is_dir():
            continue
        for d in sorted(base.iterdir()):
            if not d.is_dir() or not (d / "manifest.yml").is_file():
                continue
            try:
                m = _parse_manifest(d)
            except Exception as e:
                _LOG.warning("插件 %s manifest 无效，跳过：%s", d.name, e)
                continue
            m.scope = scope
            found[m.name] = m
    return list(found.values())


def enabled_view(workspace: Path) -> list[PluginManifest]:
    """发现结果 ∩ 启用状态（无记录=默认启用）。"""
    st = _load_state(workspace)
    out = []
    for m in discover(workspace):
        ent = st.get(m.name) or {}
        if ent.get("enabled", True):
            out.append(m)
    return out


# ===================== 运行时注册表（进程级单例） =====================

@dataclass
class _Loaded:
    """一个已加载插件的运行时态：manifest + 撤销列表。"""
    manifest: PluginManifest
    undo: list = field(default_factory=list)   # [fn...] unload 时倒序执行


class _Runtime:
    """进程级运行时：已加载插件/命令表/路由表/钩子链。reload 时整体重建。"""
    def __init__(self):
        self.loaded: dict[str, _Loaded] = {}
        self.commands: dict[str, tuple] = {}     # name -> (handler, help, detail)
        self.routes: list[dict] = []             # [{"method","path","fn","plugin"}]
        self.pre_hooks: list[tuple] = []         # [(plugin, fn(name, args) -> None|{"deny":str})]
        self.post_hooks: list[tuple] = []        # [(plugin, fn(name, args, result) -> result|新)]
        self.agent = None
        self.workspace = None
        self.attached_app = None                 # 已挂过路由的 FastAPI app（防重复挂）


_RT = _Runtime()
_LOCK = threading.Lock()


# ===================== PluginAPI（backend.py 的 setup(api) 入参） =====================

class PluginAPI:
    """插件后端 SDK。所有 register_* 即时生效并记 undo；同插件注册项在 unload 时全部撤销。"""

    def __init__(self, loaded: _Loaded):
        self._loaded = loaded
        self._plugin = loaded.manifest.name

    # —— 注册型 seam ——

    def register_tool(self, name: str, func, description: str = "", *, hidden: bool = None):
        """注册一个工具进主 Agent 工具箱（LLM 可见；exposed_to_llm=False 或 hidden=True 则仅
        工作流 plugin 节点/编辑器可用，不投影给 LLM）。func 需类型注解（schema 自动推断）；
        docstring 缺省时用 description 兜底。同名覆盖现有工具（用户定制覆写机制同 script_tools）。"""
        from tools import Tool
        if not callable(func):
            raise ValueError("register_tool 需要 callable")
        if not (getattr(func, "__doc__", "") or "").strip():
            func.__doc__ = description or name
        t = Tool(func)
        t.name = name
        t.description = description or t.description
        t.schema["function"]["name"] = name
        t.schema["function"]["description"] = t.description
        hid = (not _RT.loaded[self._plugin].manifest.exposed_to_llm) if hidden is None else bool(hidden)
        t.hidden = bool(hid)
        t.group = f"插件·{self._plugin}"
        t.source = str(_RT.loaded[self._plugin].manifest.dir / "backend.py")
        agent = _RT.agent
        if agent is not None:
            agent.tools.register_or_replace(t)
            if getattr(agent, "tool_groups", None) is not None:
                agent.tool_groups[name] = t.group
        def _undo():
            a = _RT.agent
            if a is not None:
                a.tools.unregister(name)
                if getattr(a, "tool_groups", None) is not None:
                    a.tool_groups.pop(name, None)
        self._loaded.undo.append(_undo)

    def register_command(self, name: str, handler, help_text: str = "", detail: str = ""):
        """注册斜杠命令（/name）：CLI 与 WebUI 输入框两入口自动可用。
        handler(ctx, args)：ctx 为 CommandContext（agent/work_q/state），args 为参数词列表。"""
        if name.startswith("/"):
            name = name[1:]
        if not callable(handler):
            raise ValueError("register_command 需要 callable")
        _RT.commands[name] = (handler, help_text or f"插件 {self._plugin} 提供的命令", detail)
        self._loaded.undo.append(lambda: _RT.commands.pop(name, None))

    def register_route(self, method: str, path: str, fn):
        """注册 HTTP 路由（如 POST /api/my/echo）：server 启动/本调用时动态挂载 FastAPI。
        path 必须以 / 开头；fn(request) -> (status, body_dict|str) 同内置路由约定。
        CLI 模式（无 server）注册挂起，起服务时统一挂。"""
        method = method.upper()
        path = path if path.startswith("/") else "/" + path
        _RT.routes.append({"method": method, "path": path, "fn": fn, "plugin": self._plugin})
        if _RT.attached_app is not None:
            _attach_one(_RT.attached_app, _RT.routes[-1])

        def _undo():
            for r in [x for x in _RT.routes if x["plugin"] == self._plugin
                      and x["path"] == path and x["method"] == method]:
                _RT.routes.remove(r)
        self._loaded.undo.append(_undo)

    # —— 事件型 seam（工具执行管线，waterfall）——

    def on_tool_pre_execute(self, fn):
        """挂工具执行前钩子：fn(name, arguments) -> None（放行）| {"deny": "原因"}（拒绝）。
        拒绝时工具不执行，结果为 [插件拒绝] 前缀文本（模型可见）。"""
        _RT.pre_hooks.append((self._plugin, fn))
        self._loaded.undo.append(lambda: _RT.pre_hooks.remove((self._plugin, fn)))

    def on_tool_post_execute(self, fn):
        """挂工具执行后钩子：fn(name, arguments, result) -> 返回新 result 替换（或原样返回观察）。"""
        _RT.post_hooks.append((self._plugin, fn))
        self._loaded.undo.append(lambda: _RT.post_hooks.remove((self._plugin, fn)))

    # —— 输出 ——

    def emit(self, event: dict):
        """广播一条 WS 事件（前端 AgtPlugin.ws.on / addChatCard 可接）。建议 type 唯一命名。"""
        agent = _RT.agent
        if agent is not None and isinstance(event, dict):
            ev = dict(event)
            ev.setdefault("type", "plugin_event")
            ev.setdefault("plugin", self._plugin)
            try:
                agent._emit(ev)
            except Exception as e:   # emit 失败不能炸工具管线
                _LOG.warning("插件 %s emit 失败：%s", self._plugin, e)

    def log(self, text: str):
        """打一条插件日志（引擎日志流，不进会话）。"""
        _LOG.info("[%s] %s", self._plugin, text)

    # —— 只读引擎访问 ——

    @property
    def agent(self):
        """主 Agent（session/tools 等引擎态只读访问；勿持有跨 reload）。"""
        return _RT.agent

    @property
    def workspace(self) -> Path:
        return _RT.workspace


# ===================== 加载 / 卸载 / 重载 =====================

def _probe_guard(backend_py: Path) -> bool:
    """任务脚本防御（同 script_tools._needs_probe）：模块级 while/sleep/input 拒绝 import。"""
    try:
        src = backend_py.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return False
    return not bool(re.search(r"^(while |time\.sleep\(|input\()", src, re.M))


def load_plugins(agent, workspace=None) -> str:
    """装配期入口：发现启用插件 → import backend → setup(api)。失败隔离（单插件异常不炸启动）。
    返回人话摘要（build_agent 打印）。重复调用=先全卸再装（reload 语义）。"""
    global _RT
    with _LOCK:
        if workspace is None:
            workspace = getattr(agent, "workspace", None) or Path.cwd()
        unload_all()
        _RT = _Runtime()
        _RT.agent = agent
        _RT.workspace = Path(workspace)
        ok, failed = [], []
        for m in enabled_view(Path(workspace)):
            if not m.backend:
                ok.append(m.name)   # 纯前端插件（只供 frontend.js）也算加载成功
                continue
            bp = m.dir / m.backend
            if not bp.is_file():
                failed.append(f"{m.name}: backend 入口 {m.backend} 不存在")
                continue
            if not _probe_guard(bp):
                failed.append(f"{m.name}: backend 疑似任务脚本（模块级循环/阻塞），拒绝加载")
                continue
            try:
                mod = _import_backend(bp)
                setup = getattr(mod, "setup", None)
                loaded = _Loaded(manifest=m)
                _RT.loaded[m.name] = loaded
                if callable(setup):
                    setup(PluginAPI(loaded))
                ok.append(m.name)
            except Exception as e:
                _RT.loaded.pop(m.name, None)
                failed.append(f"{m.name}: {type(e).__name__}: {e}")
                _LOG.warning("插件 %s 加载失败：%s", m.name, e, exc_info=True)
        # 命令注册进 agent 级记忆（build_default_registry 每次构建时读 _RT.commands）
        parts = [f"插件 {len(ok)} 个加载" + (f"：{', '.join(ok)}" if ok else "")]
        if failed:
            parts.append(f"⚠️ {len(failed)} 个失败：\n  " + "\n  ".join(failed))
        return "\n".join(parts)


def _import_backend(bp: Path):
    """独立模块名 import（reload 语义，同 script_tools._import_fresh）。"""
    import importlib.util
    modname = f"agt_plugin_{bp.stem}_{abs(hash(str(bp))) % 100000}"
    sys.modules.pop(modname, None)
    spec = importlib.util.spec_from_file_location(modname, bp)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[modname] = mod
    spec.loader.exec_module(mod)
    return mod


def unload_all():
    """按插件粒度倒序撤销全部注册（工具/命令/路由/钩子）。"""
    for loaded in reversed(list(_RT.loaded.values())):
        for undo in reversed(loaded.undo):
            try:
                undo()
            except Exception:
                pass
    _RT.loaded.clear()
    _RT.commands.clear()
    _RT.pre_hooks.clear()
    _RT.post_hooks.clear()
    # routes 不清空：attached_app 上的 FastAPI 路由无法摘除（挂一次永久）——
    # reload 时同名路由重新挂会 409 冲突，由 _attach_one 幂等处理（已存在同 path 跳过）


# ===================== 引擎钩子（agent._exec_tool 调用；零插件零开销） =====================

def run_pre_hooks(name: str, arguments: dict):
    """工具执行前：任一钩子返回 deny 即拒绝。返回 None=放行 / {"deny": str}=拒绝。"""
    hooks = _RT.pre_hooks
    if not hooks:
        return None
    for _plugin, fn in list(hooks):
        try:
            r = fn(name, arguments)
        except Exception as e:
            _LOG.warning("插件 %s pre_execute 钩子异常（放行）：%s", _plugin, e)
            continue
        if isinstance(r, dict) and r.get("deny"):
            return {"deny": str(r["deny"]), "plugin": _plugin}
    return None


def run_post_hooks(name: str, arguments: dict, result):
    """工具执行后：钩子可改写 result（返回非 None 替换）。异常不炸管线（原样返回）。"""
    hooks = _RT.post_hooks
    if not hooks:
        return result
    cur = result
    for _plugin, fn in list(hooks):
        try:
            r = fn(name, arguments, cur)
            if r is not None:
                cur = r
        except Exception as e:
            _LOG.warning("插件 %s post_execute 钩子异常（跳过）：%s", _plugin, e)
    return cur


# ===================== 引擎挂载接口 =====================

def append_commands(reg) -> None:
    """build_default_registry 末尾调用：插件命令追加进 registry（三处构建点自动生效）。"""
    for name, (handler, help_text, detail) in list(_RT.commands.items()):
        try:
            reg.register(name, handler, help_text, detail)
        except Exception:
            pass


def _attach_one(app, route: dict):
    """把一条插件路由挂上 FastAPI（幂等：同 method+path 已存在则跳过——reload 防冲突）。"""
    paths = {getattr(r, "path", None) for r in app.routes}
    if route["path"] in paths:
        return
    fn = route["fn"]

    # 注解修复（2026-10-15 隔离实例实锤 422）：request 参数必须带 fastapi.Request 注解——
    # 无注解时 FastAPI 把它当必填 query 参数，POST 一律 422。插件侧 fn(request) 无注解照写
    # （SDK 契约不要求懂 FastAPI），引擎包装层统一补。注解引用模块级 _FastAPIRequest
    # （本文件有 from __future__ import annotations：字符串注解按模块 globals resolve）。
    async def _handler(request: "_FastAPIRequest"):
        try:
            status, body = await fn(request) if _is_coro(fn) else fn(request)
        except Exception as e:
            return {"error": f"{type(e).__name__}: {e}"}
        from fastapi.responses import JSONResponse
        if isinstance(body, (dict, list)):
            return JSONResponse(body, status_code=int(status or 200))
        return JSONResponse({"text": str(body)}, status_code=int(status or 200))

    try:
        app.add_api_route(route["path"], _handler, methods=[route["method"]], name=f"plugin_{route['plugin']}_{len(app.routes)}")
    except Exception as e:
        _LOG.warning("插件路由 %s %s 挂载失败：%s", route["method"], route["path"], e)


def _is_coro(fn) -> bool:
    import inspect
    return inspect.iscoroutinefunction(fn)


def attach_routes(app) -> int:
    """server 启动时调用：把注册表里的全部路由挂上（并标记 app，后续 register_route 即时挂）。"""
    _RT.attached_app = app
    before = len(getattr(app, "routes", []))
    for r in list(_RT.routes):
        _attach_one(app, r)
    return len(getattr(app, "routes", [])) - before


def plugins_view(workspace: Path) -> list[dict]:
    """GET /api/plugins 数据：发现清单 + 启用态 + frontend 入口（前端注入器消费）。"""
    st = _load_state(workspace)
    out = []
    for m in discover(workspace):
        ent = st.get(m.name) or {}
        enabled = bool(ent.get("enabled", True))
        frontend_js = ""
        if enabled and m.frontend and (m.dir / m.frontend).is_file():
            frontend_js = f"/plugins-assets/{m.name}/{m.frontend}"
        out.append({"name": m.name, "version": m.version, "description": m.description,
                    "scope": m.scope, "enabled": enabled, "frontend_js": frontend_js,
                    "loaded": m.name in _RT.loaded})
    return out


def plugin_asset(workspace: Path, name: str, rel: str) -> Path | None:
    """插件静态资产安全解析（/plugins-assets/{name}/{file}）：resolve 后必须仍在该插件目录内。"""
    for m in discover(workspace):
        if m.name == name:
            target = (m.dir / rel).resolve()
            try:
                target.relative_to(m.dir.resolve())
            except ValueError:
                return None
            return target if target.is_file() else None
    return None


def reload_plugins(agent, workspace=None) -> str:
    """/reload plugins 入口：全卸重装。返回人话摘要。"""
    return load_plugins(agent, workspace)


def seed_default_plugins(workspace: Path) -> int:
    """随包示例插件播种（用户提案 2026-10-15：open-folder / feedback / example-hello 作为
    插件 SDK 实用示例随包分发）：src/assets/plugins/<名>/ → .agent/plugins/<名>/。
    以 manifest.yml 判定存在——已装则整插件跳过（用户改动永不覆盖）。返回播种数。
    播种时写 seed_state 基线（key plugin/<名>/<rel>——/update-assets 三方 hash 纳管）。"""
    try:
        bundled = Path(__file__).resolve().parent / "assets" / "plugins"
        if not bundled.is_dir():
            return 0
        from asset_sync import _sha, _load_state, _save_state
        st = _load_state(workspace)
        n = 0
        for src in sorted(bundled.iterdir()):
            if not src.is_dir() or not (src / "manifest.yml").is_file():
                continue
            dst = Path(workspace) / ".agent" / "plugins" / src.name
            if (dst / "manifest.yml").exists():
                continue                      # 已装同名插件：不覆盖
            dst.mkdir(parents=True, exist_ok=True)
            try:
                for f in sorted(src.rglob("*")):
                    if not f.is_file() or "__pycache__" in f.parts:
                        continue
                    rel = f.relative_to(src)
                    tf = dst / rel
                    tf.parent.mkdir(parents=True, exist_ok=True)
                    tf.write_bytes(f.read_bytes())
                    st[f"plugin/{src.name}/{rel.as_posix()}"] = _sha(f)
                n += 1
            except Exception:
                pass
        if n:
            _save_state(workspace, st)
        return n
    except Exception:
        return 0


def stats() -> dict:
    """运行时统计（诊断用）。"""
    return {"loaded": sorted(_RT.loaded), "commands": sorted(_RT.commands),
            "routes": len(_RT.routes), "pre_hooks": len(_RT.pre_hooks),
            "post_hooks": len(_RT.post_hooks)}


# ===================== CLI（agt plugin <sub>，chat.main 分发） =====================

def run_plugin_cli(argv: list) -> int:
    """agt plugin list|enable|disable|remove|install <src> —— 命令行插件管理（免起实例）。
    返回退出码。install：本地目录复制 / git URL 克隆 → ~/.agt/plugins/<名>/。"""
    import shutil
    import subprocess
    ws = Path.cwd()
    sub = argv[0] if argv else "list"
    arg = argv[1] if len(argv) > 1 else ""

    if sub == "list":
        view = plugins_view(ws)
        if not view:
            print("(未发现插件。目录：<repo>/.agent/plugins/<名>/ 或 ~/.agt/plugins/<名>/，含 manifest.yml)")
        for p in view:
            print(f"  {'✅' if p['enabled'] else '⏸'} {p['name']} v{p['version']} [{p['scope']}]"
                  + (" +前端" if p["frontend_js"] else "") + f"  {p['description']}")
        return 0

    if sub in ("enable", "disable") and arg:
        print(set_enabled(ws, arg, sub == "enable"))
        return 0

    if sub == "remove" and arg:
        m = next((x for x in discover(ws) if x.name == arg), None)
        if m is None:
            print(f"❌ 插件 {arg} 不存在")
            return 1
        if m.scope == "repo":
            print(f"❌ {arg} 是 repo 级插件（{m.dir}）——随仓库管理，不走 CLI remove（手动 git rm）")
            return 1
        shutil.rmtree(m.dir, ignore_errors=True)
        print(f"🗑️ 已删除全局插件 {arg}（{m.dir}）")
        return 0

    if sub == "install" and arg:
        dest_root = Path.home() / ".agt" / "plugins"
        dest_root.mkdir(parents=True, exist_ok=True)
        src = Path(arg)
        if src.is_dir():
            name = src.name
            dest = dest_root / name
            if dest.exists():
                print(f"❌ 已存在 {dest}（先 agt plugin remove {name}）")
                return 1
            shutil.copytree(src, dest)
        elif arg.startswith(("http://", "https://", "git@", "ssh://")):
            name = arg.rstrip("/").split("/")[-1].removesuffix(".git") or "plugin"
            dest = dest_root / name
            if dest.exists():
                print(f"❌ 已存在 {dest}（先 agt plugin remove {name}）")
                return 1
            r = subprocess.run(["git", "clone", "--depth", "1", arg, str(dest)],
                               capture_output=True, text=True, timeout=300)
            if r.returncode != 0:
                print(f"❌ git clone 失败：{(r.stderr or '').strip()[-300:]}")
                shutil.rmtree(dest, ignore_errors=True)
                return 1
        else:
            print(f"❌ 源不存在或不是目录/git URL：{arg}")
            return 1
        if not (dest / "manifest.yml").is_file():
            print(f"⚠️ {dest} 缺 manifest.yml——插件不会被识别（补一个再 /plugins reload）")
        print(f"✅ 已安装到 {dest}（重启或会话内 /reload plugins 生效）")
        return 0

    print("用法：agt plugin list                    列出全部插件")
    print("      agt plugin enable <名>             启用（重启/reload 后生效）")
    print("      agt plugin disable <名>            禁用")
    print("      agt plugin install <目录|git-url>  安装到 ~/.agt/plugins/")
    print("      agt plugin remove <名>             删除全局插件（repo 级随仓库管理）")
    return 0 if sub in ("list", "help", "-h") else 1
