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
_clients: list[dict] = []       # [{ws, queue, target}]  target=该客户端正在交互的 agent_id（默认 _main_）
_event_log: list[tuple[int, dict]] = []
_seq: int = 0
_main_loop = None               # uvicorn 线程的 asyncio loop（broadcast 跨线程推送用）

_STATIC_DIR = Path(__file__).resolve().parent / "static"
_WF_DIR = WORKSPACE / ".agent" / "workflows"


def _inject_node_plugins(html: str) -> str:
    """把节点插件的 .js 注入页面（</body> 前，带 sourceURL 保 devtools 定位）。
    页面无 EdFW（如 debug 页）时先注入最小 shim——仅登记 TYPE_LABEL（画布渲染走各页内置逻辑）。
    注入失败/无插件 → 原样返回（编辑器离线可用性不受影响）。"""
    try:
        from node_plugins import node_js_payload
        payload = node_js_payload()
    except Exception:
        return html
    if not payload:
        return html
    blocks = ['<script>/* 节点插件注入 shim */if(!window.EdFW){window.EdFW={register:function(d){'
              'if(window.TYPE_LABEL&&d&&d.label)TYPE_LABEL[d.type]=d.label;}};}</script>']
    for item in payload:
        blocks.append(f'<script>\n{item["source"]}\n//# sourceURL=nodes/{item["name"]}.js\n</script>')
    return html.replace("</body>", "\n".join(blocks) + "\n</body>", 1)


# 静态 HTML 热更新（2026-09-01·用户报告：改 HTML 后 Ctrl+Shift+R 不生效须 /restart——
# 启动时读进模块级常量的内存快照是根因）：mtime 缓存——文件没变用缓存（stat 零开销），
# 变了重读（transform 一并重跑——节点插件注入也热生效）。此后改 HTML 硬刷新即生效。
_HTML_CACHE: dict = {}

def _serve_html(filename: str, transform=None) -> str:
    try:
        mtime = (_STATIC_DIR / filename).stat().st_mtime_ns
    except OSError:
        mtime = 0
    ent = _HTML_CACHE.get(filename)
    if ent is None or ent["mtime"] != mtime:
        text = (_STATIC_DIR / filename).read_text(encoding="utf-8")
        if transform:
            text = transform(text)
        _HTML_CACHE[filename] = {"mtime": mtime, "text": text}
        return text
    return ent["text"]


def _broadcast(ev: dict):
    """记录事件到日志缓冲 + 按 agent 交互目标分发。
    事件带 agent_id（Agent._emit 自动打标：主=_main_、子 Agent=各自 id）→ 只发给
    target 匹配的客户端（多页签各与不同 Agent 交互时互不串台）；无 agent_id
    （系统级：sessions/workflows/config/wf_debug/命令回显）→ 广播全部。
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
    aid = str(ev.get("agent_id") or "")
    for c in _clients:
        if aid and c.get("target", "_main_") != aid:
            # answer 特例：同步工具型子 Agent（explore_subagent 等）的回应需要进主视图的
            # answer 分页——主 Agent 页签（target=_main_）额外放行其它 Agent 的 answer
            if not (aid != "_main_" and ev.get("type") in ("answer", "wrap_answer")
                    and c.get("target", "_main_") == "_main_"):
                continue   # Agent 专属事件：只发给与该 Agent 交互的客户端
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
    return HTMLResponse(_serve_html("index.html"))


@app.get("/editor")
async def workflow_editor():
    return HTMLResponse(_serve_html("workflow_editor.html", _inject_node_plugins))


@app.get("/wfdebug")
async def workflow_debug():
    """工作流调试页：只读渲染画布 + 流式执行 + 逐节点查看输出。"""
    return HTMLResponse(_serve_html("workflow_debug.html", _inject_node_plugins))


@app.get("/rag")
async def rag_page():
    """RAG 文档库管理页：配置 + 建库 + 查询测试。"""
    return HTMLResponse(_serve_html("rag.html"))


@app.get("/memory")
async def memory_page():
    """长期记忆管理页：查看/编辑/删除三类记忆。"""
    return HTMLResponse(_serve_html("memory.html"))


@app.get("/stats")
async def stats_page():
    """LLM 调用统计页：缓存命中率折线图 + 各模型调用概览表。"""
    return HTMLResponse(_serve_html("stats.html"))


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

@app.get("/api/wf/nodes")
async def api_wf_nodes():
    """节点类型目录（type → 描述）——编辑器画布 hover tooltip / props 面板描述段用。
    来自 real_tools._node_catalog()（核心节点 + node_plugins.catalog_entries 动态聚合的
    插件目录声明——改插件 desc 跟实现走，此处自动跟上）。"""
    try:
        from real_tools import _node_catalog
        return {"nodes": {n["type"]: n["desc"] for n in _node_catalog() if n.get("desc")}}
    except Exception as e:
        return {"nodes": {}, "error": str(e)[:200]}


@app.get("/api/wf/list")
async def api_wf_list():
    """列出所有工作流（名称+状态摘要）。"""
    from workflow import workflows_info
    items = []
    for it in workflows_info(_workspace):
        items.append({"name": it["name"], "tool": it["tool"], "status": it["status"],
                       "detail": it["detail"], "description": it["description"], "coze_url": it["coze_url"]})
    return {"items": items}


@app.get("/api/wf/runs")
async def api_wf_runs():
    """最近工作流运行列表（观测页首页用，倒序摘要）。
    ⚠ 必须注册在 /api/wf/{name} 之前——FastAPI 按注册顺序匹配路径参数，
    否则 GET /api/wf/runs 被 {name} 路由吞掉（"工作流 'runs' 不存在"）。"""
    from workflow import list_wf_runs
    return {"runs": list_wf_runs()}


@app.get("/api/wf/runs/{run_id}")
async def api_wf_run(run_id: str, canvas: str = ""):
    """单次工作流运行的完整轨迹（节点时间线 + 输出预览）。
    ?canvas=1 时附带 run 注册时快照的画布（观测页"在调试页查看"按钮单次拉取）。"""
    from workflow import get_wf_run, get_wf_run_canvas
    r = get_wf_run(run_id)
    if r is None:
        return {"error": f"运行 {run_id} 不存在（可能已被清理，仅保留最近 50 次）"}
    if canvas in ("1", "true"):
        c = get_wf_run_canvas(run_id)
        if c is not None:
            r["canvas"] = c
    return r


@app.get("/api/wf/runs/{run_id}/node/{node_id}")
async def api_wf_run_node(run_id: str, node_id: str):
    """某节点全量输出（text/plain 纯文本页——观测页点击节点打开，浏览器原生渲染无样式）。"""
    from fastapi.responses import PlainTextResponse
    from workflow import get_wf_node_full
    full = get_wf_node_full(run_id, node_id)
    if full is None:
        return PlainTextResponse(f"[不存在] 运行 {run_id} 或节点 {node_id} 未找到（运行仅保留最近 50 次）",
                                 status_code=404)
    if not full:
        return PlainTextResponse(f"[无全文] 节点 {node_id} 未记录全量输出（执行中 / 全量预算耗尽只存预览）")
    return PlainTextResponse(full, media_type="text/plain; charset=utf-8")


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
        name = s["name"]   # 提前（params 的 enum 判断要用，否则首轮 NameError/串到上个工具名）
        props = s.get("parameters", {}).get("properties", {}) or {}
        # schema 无 type（如 pass_through 的 Any 参数）→ "any"：类型不确定的标记，
        # 编辑器据此不锁死该字段的类型编辑（用户可改成 object 逐字段连线组装）
        params = []
        for pn, ps in props.items():
            pm = {"name": pn, "type": (ps.get("type") if isinstance(ps, dict) else None) or "any"}
            # enum 透传（通用）：工具 schema 自带 enum 的参数（如 agent_prompt.name 的子 Agent 名单、
            # agent_prompt.caller、llm_call.model）——编辑器检测 enum 渲染下拉控件 + LLM 调用时的合法值约束
            if isinstance(ps, dict) and isinstance(ps.get("enum"), list) and ps["enum"]:
                pm["enum"] = ps["enum"]
            # llm_call 的 model 参数：API 侧附加（models.json 的 provider 列表 + 空=跟随）
            # ——schema 不自带（llm_call 在 LIGHT_TOOLS 构造时 enum 无法静态声明），保持原有路径
            if name == "llm_call" and pn == "model":
                pm["enum"] = [""] + sorted(config.MODELS.keys())
            params.append(pm)
        outputs = getattr(t, "user_outputs", None) or infer_tool_outputs(t)
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
            # hidden 默认 true（与 workflow._scan_xml_workflows 同语义）：只有显式 hidden="false" 才注册为工具
            meta["hidden"] = root.get("hidden", "true") != "false"
            if root.get("async") is not None:
                meta["async"] = root.get("async") == "true"
            if root.get("recap") is not None:
                meta["recap"] = root.get("recap") == "true"
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
        # 钩子根属性保底：编辑器 UI 已不管理 hook/async/recap/enabled（钩子挂载统一走 /agents 声明面），
        # 保存请求缺这些字段时从磁盘现有 XML 根属性合并——防编辑器每次保存逐步丢光
        # （实际发生过：22 个 XML 里只剩 2 个还带 hook 标志，播种面新装机钩子全死）
        try:
            _old_head = xf.read_text(encoding="utf-8")[:900]
            for _k in ("hook", "async", "recap", "enabled"):
                if _k not in meta:
                    _mm = re.search(rf'\b{_k}="([^"]*)"', _old_head)
                    if _mm and _mm.group(1):
                        meta[_k] = _mm.group(1)
        except OSError:
            pass
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
async def api_models(scope: str = "auto"):
    """返回模型列表+默认模型名。scope=local/global 显式读某一份（UI 全局/本地切换，用户裁定 2026-08-31）。
    preset（spec s_d4241d58）：随包预设条目（下拉框开箱可选 + onboarding 引导）。用户条目按
    base_url+model 命中预设时该条目标 configured（provider 组内直接可选，"已配置"组不重复显示）；
    未命中的选中走 /api/models/onboard 落地。"""
    if scope in ("local", "global"):
        d = config.read_models_scoped(scope)
        return {"models": d["models"], "default": d["default"], "exists": d["exists"],
                "path": d["path"], "scope": scope,
                "active": config.active_scope("models.json"),
                "preset": config.preset_models_view()}
    return {"models": config.MODELS, "default": config.DEFAULT_MODEL,
            "active": config.active_scope("models.json"),
            "preset": config.preset_models_view()}


@app.get("/api/model-list")
async def api_model_list():
    """返回模型名→显示名映射（不含敏感信息），供工作流编辑器选模型用。"""
    return {"models": {name: p.get("display", name) for name, p in config.MODELS.items()},
            "default": config.DEFAULT_MODEL}


@app.put("/api/models")
async def api_models_save(request: Request):
    """保存模型配置。body.scope=local/global 写指定份；写非生效份只落盘不热应用（存档备用），
    写生效份（或未指定 scope）走现状通道（save_user_models + reload + 实例层热应用）。"""
    try:
        body = await request.json()
    except Exception:
        return {"error": "请求体需为 JSON"}
    models = body.get("models") or {}
    default = body.get("default") or ""
    _scope = (body.get("scope") or "auto").strip()
    if _scope in ("local", "global") and _scope != config.active_scope("models.json"):
        # 非生效份：scoped 落盘（不 reload、不热应用——当前进程仍跑生效份）
        r = config.save_models_scoped(models, default, _scope)
        return {"ok": True, "path": r["path"], "scope": _scope, "reloaded": False,
                "note": f"已写非生效份（当前生效：{config.active_scope('models.json')}）——存档备用，不热应用"}
    config.save_user_models(models, default)
    # 实例层热应用：主 llm 同名 profile 重应用（token/model id 刷新）+ utility 通道重建
    # （save_user_models 已自动 reload_models 刷新 config.MODELS；LLMClient 实例 profile 是
    #  创建时固化的，这里补上实例层——否则保存后当前进程跑的还是旧配置）
    if _agent is not None:
        try:
            cur = _agent.llm.model_name
            if cur in config.MODELS:
                _agent.llm._apply_profile(config.get_profile(cur))
                # session 窗口副本同步（switch_model/_cmd_reload 同款）：窗口随新 profile 生效，
                # 折叠计划/detail_base 不再按旧窗口算
                try:
                    _agent.session.max_effective_context_window = getattr(
                        _agent.llm, "max_effective_context_window", None)
                    _agent.session.fold_target_ratio = getattr(_agent.llm, "fold_target_ratio", None)
                    _agent.session.profile_detail_step = getattr(_agent.llm, "profile_detail_step", None)
                    _agent.session.invalidate_detail_base()
                except Exception:
                    pass
            _agent._utility_llm = None
            _agent.retrieval_llm = _agent.utility_client()
        except Exception:
            pass
    return {"ok": True, "default": config.DEFAULT_MODEL}


@app.post("/api/models/onboard")
async def api_models_onboard(request: Request):
    """预设条目 onboarding 落地（spec s_d4241d58）：预设 provider 参数 + 用户 token
    → 完整条目写入 models.json（写生效份 + reload + 实例层热应用）→ 前端刷新下拉即可切换。
    body: {name: 预设模型名, api_keys: "key1,key2"（逗号分隔多 key）}"""
    try:
        body = await request.json()
    except Exception:
        return {"error": "请求体需为 JSON"}
    name = (body.get("name") or "").strip()
    keys_raw = (body.get("api_keys") or "").strip()
    if not name or not keys_raw:
        return {"error": "缺少 name 或 api_keys"}
    pe = config.preset_entry(name)
    if not pe:
        return {"error": f"预设条目 '{name}' 不存在"}
    if pe.get("configured"):
        return {"error": f"'{name}' 已配置（条目「{pe.get('config_name')}」）——下拉框直接选择即可，参数在设置页编辑"}
    toks = [t.strip() for t in keys_raw.replace("，", ",").split(",") if t.strip()]
    if not toks:
        return {"error": "api_keys 无有效条目"}
    entry = {"base_url": pe.get("base_url", ""),
             "api_token": toks[0] if len(toks) == 1 else toks,
             "model": pe.get("model", ""),
             "thinking": bool(pe.get("thinking", False))}
    if pe.get("vision"):
        entry["vision"] = True
    if pe.get("model_desc"):
        entry["desc"] = pe.get("model_desc")
    models = dict(config.MODELS)
    models[name] = entry
    config.save_user_models(models, config.DEFAULT_MODEL)
    # 实例层热应用（与 PUT /api/models 同款——主 llm 同名刷新 + utility 通道重建）
    if _agent is not None:
        try:
            cur = _agent.llm.model_name
            if cur in config.MODELS:
                _agent.llm._apply_profile(config.get_profile(cur))
            _agent._utility_llm = None
            _agent.retrieval_llm = _agent.utility_client()
        except Exception:
            pass
    return {"ok": True, "name": name, "reload": True}


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


@app.get("/api/mcp/status")
async def api_mcp_status():
    """MCP server 状态概览（night_tasks #3）：配置的 server × 连接态 × 工具清单。
    数据源：.mcp.json 配置 ∪ agent.mcp_mgr.sessions（已连接会话——含动态注入不在配置里的）。"""
    import json as _j
    p = _workspace / ".mcp.json"
    configured = {}
    if p.exists():
        try:
            configured = _j.loads(p.read_text(encoding="utf-8")).get("mcpServers", {})
        except Exception:
            pass
    mgr = getattr(_agent, "mcp_mgr", None) if _agent is not None else None
    sessions = getattr(mgr, "sessions", {}) or {}
    def _tnames(sess):
        # 兼容两种形态（2026-09-02·tool_count=0 修复）：mcp SDK 的 Tool 对象（.name 属性——
        # sessions[name]["tools"] 存的就是它）与 dict（{"name": ...}）
        try:
            out = []
            for t in (sess.get("tools") or []):
                n = t.get("name") if isinstance(t, dict) else getattr(t, "name", "")
                if n:
                    out.append(str(n))
            return out
        except Exception:
            return []
    out = {}
    for name, c in configured.items():
        sess = sessions.get(name)
        tools = _tnames(sess) if sess else []
        out[name] = {"connected": bool(sess), "tool_count": len(tools), "tools": tools[:60],
                     "command": c.get("command", ""), "args": c.get("args", []),
                     "transport": "stdio"}
    for name, sess in sessions.items():   # 已连接但不在配置（动态注入）
        if name not in out:
            tools = _tnames(sess)
            out[name] = {"connected": True, "tool_count": len(tools), "tools": tools[:60],
                         "command": "", "args": [], "transport": "stdio(dynamic)"}
    return {"servers": out}


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


@app.post("/api/tool/exec")
async def api_tool_exec(request: Request):
    """跨实例工具直执行（server_id 路由的远程侧）：{name, arguments} → 本地工具箱执行 → {ok, result}。
    异步壳 + run_in_threadpool（工具执行可能长：run_python 等，不占事件循环）。
    不进 agent.run/不碰 session（纯"手"，与 WS 消息驱动的"脑"互补）；
    file_version 乐观锁 / py_auto_diag 等钩子在工具执行路径上自然生效。
    信任模型与 /api/status 一致（局域网；WS 本就能驱动任意行为，无增量风险）。"""
    from fastapi.concurrency import run_in_threadpool
    if _agent is None:
        return {"ok": False, "error": "Agent 未就绪（服务未注入或未启动）"}
    try:
        body = await request.json()
    except Exception as e:
        return {"ok": False, "error": f"请求体不是合法 JSON：{e}"}
    name = str(body.get("name") or "").strip()
    args = body.get("arguments") or {}
    if not isinstance(args, dict):
        return {"ok": False, "error": f"arguments 须为对象（dict），收到 {type(args).__name__}"}
    if not name:
        return {"ok": False, "error": "缺少 name（要执行的工具名）"}
    if name not in _agent.tools:
        return {"ok": False, "error": f"未知工具 '{name}'（本实例工具箱共 {len(list(_agent.tools))} 个）"}
    try:
        result = await run_in_threadpool(_agent.tools.call, name, args)
        return {"ok": True, "result": str(result)}
    except Exception as e:
        return {"ok": False, "error": f"工具 {name} 执行失败：{type(e).__name__}: {e}"}


@app.post("/api/status")
async def api_status(request: Request):
    """实例运行时状态快照（POST，供跨实例/外部诊断用）。
    返回：agent 就绪/模型/工具数/session 名/turns 数/busy/work_q 深度/inbox 深度/
    registry 团队列表（agent_id/name/role/model/status/recap/caller_id）/
    后台任务列表/服务端口/MCP servers/钩子工作流状态。
    无 Agent 时返回 ready=False。"""
    if _agent is None:
        return {"ready": False, "error": "Agent 未就绪（服务未注入或未启动）"}

    agent = _agent
    # —— 基本态 ——
    st = {
        "ready": True,
        "model": agent.llm.model_name,
        "model_id": agent.llm.model,
        "tools_count": len(list(agent.tools)),
        "session_name": agent.session.name or "(未命名)",
        "session_turns": len(agent.session.turns),
        "current_turn_active": agent.session._current is not None,
        "busy": bool(_state and _state.get("busy")),
        "work_q_size": _work_q.qsize() if _work_q is not None else -1,
        "inbox_size": len(agent.inbox) if hasattr(agent, "inbox") else -1,
        "pending_messages": len(getattr(agent, "pending_messages", [])),
        "active_target": getattr(agent, "_active_target", "_main_"),
        "ws_clients": [{"target": c.get("target", "_main_")} for c in _clients],   # 各 WS 客户端的交互目标
        "autonomous_mode": getattr(agent, "autonomous_mode", False),
        "utility_model": getattr(agent, "utility_model", ""),
        "server": server_status(),
    }

    # —— registry 团队 ——
    reg = getattr(agent, "registry", None)
    if reg:
        team = []
        with reg._lock:
            for e in reg._agents.values():
                team.append({
                    "agent_id": e.agent_id,
                    "name": e.name,
                    "role": e.role,
                    "model": e.model,
                    "status": e.status,
                    "caller_id": e.caller_id,
                    "recap": (e.recap or "")[:80],
                    "has_agent": e.agent is not None,
                })
        st["registry"] = team
    else:
        st["registry"] = []

    # —— 后台任务 ——
    bt = getattr(agent, "background_tasks", {})
    st["background_tasks"] = [
        {"id": aid, "name": t.get("name", ""), "status": t.get("status", "?"),
         "started_at": t.get("started_at"), "finished_at": t.get("finished_at"),
         "result_preview": str(t.get("result", ""))[:80]}
        for aid, t in bt.items()
    ]

    # —— MCP servers ——
    if _mcp_mgr:
        st["mcp_servers"] = [
            {"name": name, "tools_count": len(info.get("tools", []))}
            for name, info in _mcp_mgr.sessions.items()
        ]
    else:
        st["mcp_servers"] = []

    # —— 钩子工作流 ——
    try:
        from workflow import get_hook_workflows
        from real_tools import WORKSPACE as _ws
        hooks = []
        for hook_pos in ("before_turn", "after_tool", "before_answer", "turn_end"):
            for hw in get_hook_workflows(_ws, hook_pos):
                m = hw.get("meta") or {}
                hooks.append({
                    "name": hw.get("name", ""),
                    "hook": hook_pos,
                    "enabled": m.get("enabled", True),
                    "async": m.get("async", False),
                    "hidden": m.get("hidden", True),
                })
        st["hooks"] = hooks
    except Exception:
        st["hooks"] = []

    return st


@app.get("/wf/monitor")
async def wf_monitor_page(run: str = ""):
    """工作流运行观测页：?run=<run_id> 实时轮询单次运行节点轨迹；无参=最近运行列表。"""
    return HTMLResponse(_serve_html("wf_monitor.html"))



@app.get("/api/stats")
async def api_stats(scope: str = "current"):
    """LLM 调用统计·原始流水（按 ts 升序），前端按选中的调用序号窗口本地聚合。scope=current/all。
    records 每条：{ts, model(provider名), resp_model(回包实际模型), outcome, elapsed,
                  cached(归一化缓存命中tokens), prompt, tokens, completer, err}。
    端点 = model/resp_model（resp_model 空或与 model 同则不拼），由前端计算。"""
    from llm_call_log import cached_tokens_of, load_all_calls
    from session import _repo_sessions_dir
    if _agent is None:
        return {"scope": scope, "calls": 0, "records": []}
    if scope == "all":
        records = load_all_calls(_repo_sessions_dir(_workspace))
    else:
        records = _agent.session.llm_calls.all_records()
    recs = []
    for r in records or []:
        u = r.get("usage") or {}
        err_raw = r.get("error") or ""
        recs.append({
            "ts": r.get("ts") or 0,
            "model": r.get("model") or "?",
            "resp_model": (r.get("resp_model") or "").strip(),
            "scene": r.get("scene") or "",   # 调用时机（react/hook:before_turn/recap/debug/completer；老记录空）
            "turn": r.get("turn"),           # react 调用的轮号（对上 projections/t{N} 文件名；老记录 None）
            "step": r.get("step"),           # react 调用的步号（对上 projections/_s{M}）
            "outcome": r.get("outcome") or "",
            "elapsed": r.get("elapsed") or 0,
            "cached": cached_tokens_of(r),
            "prompt": u.get("prompt_tokens") or 0,
            "completion": u.get("completion_tokens") or 0,
            # reasoning 子项（归一化后 GLM/DeepSeek 都在 completion_tokens_details.reasoning_tokens；
            # 思考模型的"思考占产出的比例"观测用——completion 的一部分，不与上面重复计数）
            "reasoning": (u.get("completion_tokens_details") or {}).get("reasoning_tokens") or 0,
            "tokens": u.get("total_tokens")
                     or ((u.get("prompt_tokens") or 0) + (u.get("completion_tokens") or 0)),
            "completer": bool(r.get("completer")),
            "err": (err_raw.split(":")[0][:32] if err_raw else ""),
        })
    recs.sort(key=lambda x: x["ts"])
    return {"scope": scope, "calls": len(recs), "records": recs}


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


# ===================== 子 Agent 声明管理 API =====================

@app.get("/agents")
async def agents_page():
    """子 Agent 声明管理页：列表 + 参数编辑（名称/描述/模型/工具/assembly/hooks）。"""
    return HTMLResponse(_serve_html("agents.html"))


@app.get("/agents/{agent_id}")
async def agent_chat_page(agent_id: str):
    """Agent 专属对话页：/agents/<agent_id> 与裸 / 同一页面（index.html 读 URL 初始化交互目标）。
    URL 直接编码目标 Agent——刷新/分享/收藏自动落在对应视图（如 /agents/wiki-updater_3）；
    裸 / 默认主 Agent。与 /agents（无 id，声明管理页）按路径形态区分，互不冲突。"""
    return HTMLResponse(_serve_html("index.html"))


def _agent_safe_name(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "_", Path(name).name).strip("_")


@app.get("/api/agents")
async def api_agents_list():
    """列出全部 Agent 声明：_main_（~/.agt/main.yml）置顶 + 子 Agent（.agent/agents/）。"""
    from agent_config import load_agents_index
    out = []
    # —— 主 Agent（全局声明，非 .agent/agents/ 成员）——
    try:
        from agent_config import seed_main_agent, load_agent_yml
        mp = seed_main_agent(_workspace)
        mmeta, _ = load_agent_yml(mp)
        masm = mmeta.get("assembly")
        mhooks = mmeta.get("hooks")
        out.append({
            "name": "_main_",
            "description": mmeta.get("description") or "主 Agent 全局声明（assembly 装配清单 + hooks + model 覆盖）",
            "model": mmeta.get("model") or "",
            "tools": "",
            "assembly_count": len(masm) if isinstance(masm, list) else 0,
            "hooks_positions": sorted(mhooks.keys()) if isinstance(mhooks, dict) else [],
            "file": str(mp),
            "is_main": True,
        })
    except Exception:
        out.append({"name": "_main_", "description": "主 Agent（main.yml 读取失败）", "model": "",
                    "tools": "", "assembly_count": 0, "hooks_positions": [], "file": "", "is_main": True})
    for it in load_agents_index(_workspace):
        meta = {}
        try:
            from agent_config import load_agent_yml
            meta, _ = load_agent_yml(Path(it["path"]))   # path 是 str——必须转 Path（load_agent_yml 调 read_text，str 抛 AttributeError 被吞 → 恒空 meta）
        except Exception:
            pass
        asm = meta.get("assembly")
        hooks = meta.get("hooks")
        out.append({
            "name": it.get("name", ""),
            "description": it.get("description", ""),
            "model": it.get("model") or "",
            "tools": it.get("tools") or "",
            "assembly_count": len(asm) if isinstance(asm, list) else 0,
            "hooks_positions": sorted(hooks.keys()) if isinstance(hooks, dict) else [],
            "file": str(it.get("path", "")),
        })
    try:
        from agent_config import FUNC_REGISTRY
        _funcs = sorted(FUNC_REGISTRY.keys())   # assembly 编辑器 func 下拉的选项源（agents.html）
        # func 下拉 tooltips（用户提案 2026-09-02）：各函数 docstring 首个非空行（FUNC_REGISTRY 的
        # docstring 首句即用途摘要；全文多行不适 tooltip），超 160 字截断
        _fd = {}
        for _n, _fn in FUNC_REGISTRY.items():
            _lines = [ln.strip() for ln in (_fn.__doc__ or "").splitlines() if ln.strip()]
            _fd[_n] = (_lines[0][:160] if _lines else "")
    except Exception:
        _funcs, _fd = [], {}
    return {"items": out, "funcs": _funcs, "func_docs": _fd}


def _read_persona_md(rel_path: str) -> str:
    """读 persona md（file: 首项指向的文件）。剥 frontmatter——存量旧格式声明 md 带
    frontmatter（yml 存在时 md 已不再是声明，作纯 persona 载体）；新写的本来就是纯正文。
    路径基于 server._workspace（与 _dump_agent_yml 写盘同源，读写对称）。"""
    from multiagent import _split_frontmatter
    try:
        p = (_workspace / rel_path).resolve()
        p.relative_to(_workspace.resolve())   # 沙箱：不许越界
        if not p.exists():
            return ""
        _, body = _split_frontmatter(p.read_text(encoding="utf-8", errors="ignore"))
        return (body or "").strip()
    except Exception:
        return ""


def _persona_from_decl(meta: dict, system: str) -> str:
    """声明 → persona 文本，双形态兼容：
    ① assembly 首项 file:（v2.1，persona 独立 md）→ 读文件；
    ② 首项 text:（v2 迁移产物，内嵌）→ text 内容；
    ③ 兜底 load_agent_yml 的 system。"""
    asm = meta.get("assembly")
    if isinstance(asm, list) and asm and isinstance(asm[0], dict):
        if asm[0].get("file"):
            p = _read_persona_md(str(asm[0]["file"]))
            if p:
                return p
        if asm[0].get("text"):
            return str(asm[0]["text"])
    return system or ""


def _fb_of(meta: dict):
    """fallback 声明 → (chain 逗号串, policy)。三形态（串/list/{chain,policy}）归一，与
    multiagent._parse_agent_fallback 的解析形态对齐；未声明 → ('', '')（前端留空=继承全局）"""
    raw = meta.get("fallback")
    if raw is None:
        return "", ""
    policy = ""
    chain = raw
    if isinstance(raw, dict):
        chain = raw.get("chain", [])
        policy = str(raw.get("policy") or "").strip()
    if isinstance(chain, str):
        chain = [m.strip() for m in chain.split(",") if m.strip()]
    else:
        chain = [str(m).strip() for m in (chain or []) if str(m).strip()]
    return ",".join(chain), policy


def _fb_yaml_value(body: dict):
    """提交的 fallback 串 + policy → yml 值（未声明返回 None=不写键=继承全局）。
    串非空 → list 或 {chain, policy}（policy 指定时）；与 _parse_agent_fallback 读形态对齐。"""
    chain = [m.strip() for m in str(body.get("fallback") or "").split(",") if m.strip()]
    if not chain:
        return None
    policy = str(body.get("fallback_policy") or "").strip()
    if policy in ("sticky", "reset"):
        return {"chain": chain, "policy": policy}
    return chain


@app.get("/api/agents/{name}")
async def api_agents_get(name: str):
    """单个 Agent 完整声明。_main_ 特判（~/.agt/main.yml，is_main=True）。"""
    from agent_config import load_agent_yml
    from multiagent import _agent_def_path
    if name == "_main_":
        # 主 Agent：persona 不是单块正文（人设分多个 text 项与动作交错——正是 assembly DSL 的完整配方），
        # 编辑走原始 assembly 清单；不提供 persona/tools 字段
        from agent_config import seed_main_agent
        p = seed_main_agent(_workspace)
        try:
            meta, _ = load_agent_yml(p)
        except Exception as e:
            return {"error": f"main.yml 解析失败：{type(e).__name__}: {e}"}
        asm_raw = meta.get("assembly")
        _fb, _fbp = _fb_of(meta)
        return {
            "name": "_main_", "is_main": True,
            "description": meta.get("description", ""),
            "model": meta.get("model") or "",
            "tools": "", "persona": "",
            "fallback": _fb, "fallback_policy": _fbp,
            "assembly": asm_raw if isinstance(asm_raw, list) else None,
            "hooks": meta.get("hooks") if isinstance(meta.get("hooks"), dict) else {},
            "file": str(p),
        }
    safe = _agent_safe_name(name)
    p = _agent_def_path(safe)
    if p is None or not p.exists():
        return {"error": f"子 Agent {name!r} 不存在"}
    try:
        meta, system = load_agent_yml(p)
    except Exception as e:
        return {"error": f"解析失败：{type(e).__name__}: {e}"}
    asm_raw = meta.get("assembly")
    persona = _persona_from_decl(meta, system)
    _fb, _fbp = _fb_of(meta)
    persona_file = ""
    if isinstance(asm_raw, list) and asm_raw and isinstance(asm_raw[0], dict) and asm_raw[0].get("file"):
        persona_file = str(asm_raw[0]["file"])
    return {
        "name": meta.get("name") or safe,
        "description": meta.get("description", ""),
        "model": meta.get("model") or "",
        "tools": meta.get("tools") or "",
        "fallback": _fb, "fallback_policy": _fbp,   # 声明级回退链（引擎 _parse_agent_fallback 消费：新建/复用/复活三路径均应用）
        "persona": persona,
        "persona_file": persona_file,   # 非空=persona 走独立 md（file: 引用形态）
        "assembly": asm_raw if isinstance(asm_raw, list) else None,
        "hooks": meta.get("hooks") if isinstance(meta.get("hooks"), dict) else {},
        "file": str(p),
    }


def _dump_agent_yml(body: dict, safe: str) -> Path:
    """把编辑器提交的声明写成 .agent/agents/<safe>.yml（v2.1 格式）。
    persona 走独立 md（用户设计）：assembly 首项 {file: .agent/agents/<safe>.md}
    引用同名 .md（每次投影重读——编辑 md 即时生效；yml 保持纯配置不臃肿），
    persona 正文写该 md。同名 .md 与旧格式声明不冲突（yml 存在时 .md 被声明扫描跳过）。"""
    import yaml
    d = _workspace / ".agent" / "agents"
    d.mkdir(parents=True, exist_ok=True)
    asm = body.get("assembly")
    if not (isinstance(asm, list) and asm):
        asm = []
    persona = (body.get("persona") or "").strip()
    # 首项统一为 file: 引用（读侧对 text: 内嵌兼容，写侧统一新形态）
    rel = f".agent/agents/{safe}.md"
    if asm and isinstance(asm[0], dict) and ("text" in asm[0] or "file" in asm[0]):
        asm[0] = {"file": rel}
    else:
        asm.insert(0, {"file": rel})
    data = {
        "name": safe,
        "description": body.get("description", ""),
        "tools": body.get("tools", ""),
        "model": body.get("model") or None,
        "assembly": asm,
    }
    hooks = body.get("hooks")
    if isinstance(hooks, dict) and hooks:
        data["hooks"] = hooks
    # 声明级回退链：非空才写（list / {chain,policy}——与 _parse_agent_fallback 读形态对齐）；
    # 留空不写键 = 继承全局 settings 配置（管理页语义；「显式关回退」手写 yml 空串实现）
    _fbv = _fb_yaml_value(body)
    if _fbv is not None:
        data["fallback"] = _fbv
    p = d / f"{safe}.yml"
    p.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
    (d / f"{safe}.md").write_text(persona + ("\n" if persona else ""), encoding="utf-8")
    return p


@app.put("/api/agents/{name}")
async def api_agents_save(name: str, request: Request):
    """保存子 Agent 声明（写 .yml + 删同名 .md 避免 yml 优先遮蔽）。"""
    try:
        body = await request.json()
    except Exception:
        return {"error": "请求体需为 JSON"}
    if name == "_main_":
        # 主 Agent 保存：写 config_file 解析的 main.yml（repo 级覆盖：<cwd>/.agent/main.yml
        # 存在则读写本地——多实例组网的角色实例独立主声明；否则全局 ~/.agt/main.yml）——
        # assembly 原样保留（多 text 段与动作交错是完整配方，不做 persona 拆分），
        # 保留未识别字段；不写 .md。生效需 /restart（启动时装配）。
        import yaml
        from agent_config import seed_main_agent
        p = seed_main_agent(_workspace)
        try:
            base = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        except Exception:
            base = {}
        if "description" in body:
            base["description"] = body.get("description", "")
        if "model" in body:
            base["model"] = (body.get("model") or "").strip() or None
        asm = body.get("assembly")
        if isinstance(asm, list) and asm:
            base["assembly"] = asm
        _fbv = _fb_yaml_value(body)   # 主 Agent 同样支持声明级回退链（留空=删键继承全局）
        if _fbv is not None:
            base["fallback"] = _fbv
        elif "fallback" in body:
            base.pop("fallback", None)
        hooks = body.get("hooks")
        if isinstance(hooks, dict):
            if hooks:
                base["hooks"] = hooks
            else:
                base.pop("hooks", None)
        p.write_text(yaml.safe_dump(base, allow_unicode=True, sort_keys=False), encoding="utf-8")
        return {"ok": True, "name": "_main_", "file": str(p),
                "note": f"已写 {p}；/restart 后生效（主 Agent 装配在启动时读取）"}
    safe = _agent_safe_name(name)
    if not safe:
        return {"error": "name 非法"}
    # 这里不再做 text: 同步——双形态读取在 api_agents_get 的 _persona_from_decl。
    # 注意：不删同名 .md（旧逻辑防旧格式声明遮蔽；现在它是 persona 载体，删了就丢内容）
    asm = body.get("assembly")
    p = _dump_agent_yml(body, safe)
    return {"ok": True, "name": safe, "file": str(p),
            "persona_file": f".agent/agents/{safe}.md"}


@app.post("/api/agents")
async def api_agents_create(request: Request):
    """新建子 Agent 声明（模板）。"""
    try:
        body = await request.json()
    except Exception:
        return {"error": "请求体需为 JSON"}
    safe = _agent_safe_name(body.get("name") or "")
    if not safe:
        return {"error": "name 不能为空"}
    if (body.get("name") or "").strip() in ("_main_", "main"):
        return {"error": "'_main_' 是主 Agent 保留名，不能用作子 Agent"}
    d = _workspace / ".agent" / "agents"
    if (d / f"{safe}.yml").exists() or (d / f"{safe}.md").exists():
        return {"error": f"已存在同名声明 '{safe}'"}
    p = _dump_agent_yml({
        "description": "（新子 Agent：一句话作用 + 何时调用）",
        "tools": "", "model": "", "persona": "你是 xxx，一个……的子 Agent。规则：\n- …",
        "assembly": [{"text": "你是 xxx，一个……的子 Agent。规则：\n- …"}],
        "hooks": {},
    }, safe)
    return {"ok": True, "name": safe}


@app.delete("/api/agents/{name}")
async def api_agents_delete(name: str):
    """删除子 Agent 声明（.yml 与 .md 一并清理）。"""
    if name == "_main_":
        return {"error": "主 Agent 声明（~/.agt/main.yml）不可删除"}
    safe = _agent_safe_name(name)
    d = _workspace / ".agent" / "agents"
    gone = False
    for suf in (".yml", ".md"):
        p = d / f"{safe}{suf}"
        if p.exists():
            try:
                p.unlink()
                gone = True
            except OSError:
                pass
    return {"ok": gone} if gone else {"error": f"没有名为 '{safe}' 的子 Agent"}


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
    client = {"ws": websocket, "queue": queue, "target": "_main_"}   # target=本客户端正在交互的 agent_id
    _clients.append(client)

    is_reconnect = len(_event_log) > 0
    if is_reconnect:
        await _send(websocket, {"type": "system",
                                "text": "✅ 已重连（前端会自动请求当前对话历史）",
                                "models": [{"name": n, "desc": m.get("desc", "")} for n, m in config.MODELS.items()],
                                "preset": config.preset_models_view(),
                                "current_model": agent.model_name})
    else:
        await _send(websocket, {
            "type": "system",
            "text": f"已连接。模型={agent.model_name}，工具 {len(list(agent.tools))} 个。直接对话，或输入 /help 看命令。",
            "models": [{"name": n, "desc": m.get("desc", "")} for n, m in config.MODELS.items()],
            "preset": config.preset_models_view(),
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
                await _handle_user_input(websocket, agent, raw, queue, loop, registry, client)
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        if client in _clients:
            _clients.remove(client)


def _history_event(agent, name_override: str = "") -> dict:
    """构造 session_history 事件：默认只渲染【当前档位】的轮次（最后边界之后），
    并附 expand_from（当前渲染起点）/ total_turns——前端据此显示"展开更早"蓝字，
    点击请求 expand_history 再往前展开一档。无分档（无边界）时全量（行为同旧版）。"""
    s = agent.session
    try:
        bounds = sorted(getattr(s, "_tier_boundaries", None) or [])
        start = bounds[-1] if bounds else 0
        total = len(s.turns)
    except Exception:
        start, total = 0, 0
    return {"type": "session_history",
            "name": name_override or s.name or "(当前会话)",
            "turns": s.to_history(start_turn=start),
            "expand_from": start, "total_turns": total}


def _current_turn_event(agent):
    """构造 current_turn 事件：正在进行的轮（session._current）——新连接的客户端
    在历史渲染完后补发当前轮已完成的步骤（用户提案 2026-09-02）。格式对齐 to_history()
    的 steps 结构（name/arguments/result/call_id），无 answer（还在跑）。"""
    s = agent.session
    cur = getattr(s, "_current", None)
    if cur is None or not (cur.user_message or "").strip():
        return None
    steps = []
    for st in cur.steps:
        tcs = []
        for tc in st.tool_calls:
            try:
                n, a, r = s.toollog.view(tc.call_id)
            except Exception:
                n, a, r = tc.call_id, {}, ""
            tcs.append({"name": n, "arguments": a, "result": (r or "")[:500],
                        "call_id": tc.call_id,
                        "changed": list(getattr(tc, "changed", None) or [])})
        if tcs:
            steps.append({"tool_calls": tcs, "reasoning": st.reasoning or ""})
    return {"type": "current_turn",
            "turn": len(s.turns) + 1,
            "user": cur.user_message,
            "steps": steps,
            "agent_id": getattr(agent, "agent_id", "_main_")}



    """广播主 Agent 的 session 历史——带 agent_id="_main_"（只刷与主 Agent 交互的客户端；
    其它页签正与子 Agent 交互时不会被主 session 的历史冲掉视图）。"""
    ev = _history_event(agent, name_override)
    ev["agent_id"] = "_main_"
    _broadcast(ev)


def broadcast_session_state(agent):
    """广播完整视图态（session_history + team_list + pending spec）——会话切换/重启恢复后调用。
    供 server 的 load_session 与 chat._recover_restart_env 共用：重启后早连的页签
    拿到的是恢复前的空 session（大 session 重放慢于页面连接的竞态），此后无任何推送
    告知"已恢复"→ 一直显示 (当前对话) 直到手动刷新；广播一发全治。"""
    _broadcast_history(agent)
    reg = getattr(agent, "registry", None)
    if reg:
        _broadcast({"type": "team_list",
                    "team": reg.format_team(exclude_id=""),
                    "current_target": getattr(agent, "_active_target", "_main_")})
    try:
        from spec_tools import check_pending_spec, _spec_event_payload
        if not check_pending_spec(agent):
            if getattr(agent, "active_spec", None):
                _broadcast(_spec_event_payload(agent))
    except Exception:
        pass


async def _handle_user_input(ws, agent, raw, queue, loop, registry, client=None):
    """处理一条用户输入（文本/命令/action）。client=来源客户端 dict（含 target——
    文本按其交互目标路由：主 Agent 走 work_q；子 Agent 按忙闲插话/task 直达）。"""
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
            _broadcast({"type": "restored", "target": target or "", "agent_id": "_main_"})
            _broadcast_history(agent)
        _work_q.put(("task", _do_restore))
        await _send(ws, {"type": "system", "text": "⏮ 回溯中…"})
        return
    if isinstance(_d, dict) and _d.get("action") == "get_config":
        # scope：auto=生效份（现状）| local/global=显式查看某一份（UI 全局/本地切换，用户裁定 2026-08-31）
        _scope = (_d.get("scope") or "auto").strip()
        if _scope in ("local", "global"):
            await _send(ws, {"type": "config_scoped", "scope": _scope,
                             "models": config.read_models_scoped(_scope),
                             "settings": config.read_settings_scoped(_scope),
                             "active": {"models": config.active_scope("models.json"),
                                        "settings": config.active_scope("settings.json")}})
        else:
            # auto：现状（运行时视角）+ 附生效 scope（前端显示"本地覆盖生效中"徽章；旧前端忽略新字段）
            await _send(ws, {"type": "config", "values": read_config(agent),
                             "active": {"models": config.active_scope("models.json"),
                                        "settings": config.active_scope("settings.json")}})
        return
    if isinstance(_d, dict) and _d.get("action") == "set_config":
        values = _d.get("values") or {}
        _scope = (_d.get("scope") or "auto").strip()
        if _scope in ("local", "global"):
            _act = config.active_scope("settings.json")
            note = ""
            if _scope == _act:
                # 写的就是生效份 → 现状通道（save_runtime_settings 写生效份 + apply_config 热应用）
                config.save_runtime_settings(values)
                lines = apply_config(agent, values)
            else:
                _rs = config.save_settings_scoped(values, _scope)
                lines = []
                note = (f"💾 已写 {_rs.get('path')}\n"
                        f"ℹ️ 非生效份（当前生效：{'📦 本地' if _act == 'local' else '🌐 全局'}）——存档备用，不热应用")
            if isinstance(_d.get("modelsData"), dict):
                _rm = config.save_models_scoped(_d["modelsData"], _d.get("modelsDefault") or "", _scope)
                if _rm.get("reloaded"):
                    note += "\n🔄 模型配置已重载（生效份）"
            await _send(ws, {"type": "system", "text": ("\n".join(lines) + "\n" + note).strip()})
            return
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
        # 重连后前端请求：返回当前内存中 session 的历史（不从磁盘重载；默认当前档，可展开）。
        # target 参数：前端 sessionStorage 记住的交互目标——重连/刷新后恢复该客户端的
        # agent 视图（校验存在性；running 的目标退回 _main_ 防卡在忙实例上）。
        rt = (_d.get("target") or "").strip() if isinstance(_d, dict) else ""
        if rt and rt != "_main_" and client is not None:
            reg0 = getattr(agent, "registry", None)
            e0 = reg0.lookup(rt) if reg0 else None
            if e0 is not None:   # running 也允许——busy 实例页面正是观测价值最大的时刻
                client["target"] = rt      # （历史正常浏览；事件流按 agent_id 分发实时可见；
                agent._active_target = rt  #   向它发消息走插话队列。旧"防卡忙实例"防御已撤）
            else:
                rt = ""
        cur = (client or {}).get("target", "_main_")
        if cur != "_main_":
            reg0 = getattr(agent, "registry", None)
            e0 = reg0.lookup(cur) if reg0 else None
            if e0 is not None and e0.agent is not None:
                await _send(ws, {"type": "session_history",
                                 "name": f"{e0.name} [{cur}]", "agent_id": cur,
                                 "turns": e0.agent.session.to_history()})
                # 补发子 Agent 进行中的轮（同主 Agent——观测正在跑的子 Agent 是切换页的核心场景）
                cur_ev0 = _current_turn_event(e0.agent)
                if cur_ev0:
                    cur_ev0["agent_id"] = cur
                    await _send(ws, cur_ev0)
                return
            # 历史子 Agent（agent=None，磁盘恢复条目）：从磁盘加载历史（与 switch_agent 同款）——
            # /agents/<id> URL 直达时不再被复位回主视图（此前 agent=None 走"失效"分支直接回主，
            # 专属页路由打开显示的却是主 Agent 历史）
            if e0 is not None:
                try:
                    sdir = getattr(agent.session, "session_dir", None)
                    sub_meta = Path(sdir) / "agents" / cur / "meta.json" if sdir else None
                    if sub_meta and sub_meta.exists():
                        from session import Session
                        sub_session = Session.load(str(sub_meta), llm=agent.llm,
                                                   workspace=agent.session.workspace)
                        await _send(ws, {"type": "session_history",
                                         "name": f"{e0.name} [{cur}]", "agent_id": cur,
                                         "turns": sub_session.to_history()})
                        return
                except Exception:
                    pass
            # 真失效（registry 无此条目且无存档）：退回主 session（client target 一并复位）
            if client is not None:
                client["target"] = "_main_"
        await _send(ws, _history_event(agent))
        # 补发进行中的轮（用户提案 2026-09-02）：新连接的客户端看到当前正在跑的步骤
        cur_ev = _current_turn_event(agent)
        if cur_ev:
            await _send(ws, cur_ev)
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
            _broadcast_history(agent, "(新会话)")
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
            broadcast_session_state(agent)
        _work_q.put(("task", _sync_loaded))
        await _send(ws, {"type": "system", "text": f"🔄 恢复「{_ls_name}」中…"})
        return
    if isinstance(_d, dict) and _d.get("action") == "expand_history":
        # 前端点"展开更早"：往回展开一个固定批量（15 轮 ≈ 一档）。
        # 不按 boundary 退一步——长会话滚动毕业时边界间隔常为 1（每轮毕业一次），
        # 退一个边界 = 只展开 1 轮，不符合"点一次展开一档"的预期。
        try:
            cur = int(_d.get("from") or 0)
        except (TypeError, ValueError):
            cur = 0
        # 按客户端交互目标取对应 session（页签 A 在子 Agent 视图展开时不该拿到主 session 的轮次）
        tgt = (client or {}).get("target", "_main_")
        s = agent.session
        if tgt != "_main_":
            reg0 = getattr(agent, "registry", None)
            e0 = reg0.lookup(tgt) if reg0 else None
            if e0 is not None and e0.agent is not None:
                s = e0.agent.session
        new_start = max(0, cur - 15)
        turns = s.to_history(start_turn=new_start, end_turn=cur)
        await _send(ws, {"type": "history_expand", "turns": turns,
                         "expand_from": new_start, "total_turns": len(s.turns)})
        return
    if isinstance(_d, dict) and _d.get("action") == "resume_interrupted":
        # 中断轮续跑：恢复 _current（不新增 user_message）→ 直接续 ReAct 循环。
        # 走 work_q 的 task 通道——worker 串行执行，天然与 agent.run 互斥（busy 时排队）。
        def _do_resume():
            err = agent.resume_interrupted()
            if err:
                agent.on_event({"type": "system", "text": f"⚠️ 无法恢复：{err}", "transient": True})
                return
            # 广播 turn_resume：前端据此清掉中断轮"继续"按钮、复用原容器继续（不新建轮）
            _broadcast({"type": "turn_resume", "agent_id": "_main_"})
            agent.run("", _resume_current=True)
        _work_q.put(("task", _do_resume))
        await _send(ws, {"type": "system", "text": "▶ 恢复中断轮，从断点继续…", "transient": True})
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
            team = reg.format_team(exclude_id="")
            cur = (client or {}).get("target", "_main_")   # 本客户端自己的交互目标
            await _send(ws, {"type": "team_list", "team": team, "current_target": cur})
        return
    if isinstance(_d, dict) and _d.get("action") == "switch_agent":
        target_id = (_d.get("target_id") or "").strip()
        reg = getattr(agent, "registry", None)
        if not reg:
            await _send(ws, {"type": "system", "text": "(多 Agent 通信未启用：无 registry)"})
            return
        if target_id in ("_main_", "main", "back", ""):
            target_id = "_main_"
        else:
            entry = reg.lookup(target_id)
            if entry is None:
                await _send(ws, {"type": "system", "text": f"❌ agent_id='{target_id}' 不在注册表中"})
                return
            _busy = entry.status == "running"   # 允许切换 busy 实例——观测正在跑的过程正是价值所在；
            #   向它发文本走插话队列（_handle_user_input 已有路径），不会并发 run
        # —— 客户端级切换：只改本客户端的 target（多页签各与不同 Agent 交互互不干扰）——
        # agent._active_target 保留为"最后被切换的目标"（CLI 输入路由 / /api/status 全局视角用）
        if client is not None:
            client["target"] = target_id
        agent._active_target = target_id
        # —— 响应只发给切换的这个客户端；session_history 单发（其它页签视图不动）——
        await _send(ws, {"type": "system",
                         "text": "✅ 已切回主 Agent" if target_id == "_main_"
                                 else f"✅ 已切换到与 '{target_id}' 直接交互"
                                      + ("（正在执行任务：消息将排队注入其当前轮）" if _busy else "")})
        if target_id == "_main_":
            await _send(ws, _history_event(agent))
        else:
            entry = reg.lookup(target_id)
            if entry and entry.agent:
                await _send(ws, {"type": "session_history",
                                 "name": f"{entry.name} [{target_id}]",
                                 "agent_id": target_id,
                                 "turns": entry.agent.session.to_history()})
            else:
                # 历史子 Agent（从磁盘恢复，agent=None）：磁盘加载历史
                try:
                    sdir = getattr(agent.session, "session_dir", None)
                    sub_meta = Path(sdir) / "agents" / target_id / "meta.json" if sdir else None
                    if sub_meta and sub_meta.exists():
                        from session import Session
                        sub_session = Session.load(str(sub_meta), llm=agent.llm,
                                                   workspace=agent.session.workspace)
                        await _send(ws, {"type": "session_history",
                                         "name": f"{entry.name} [{target_id}]",
                                         "agent_id": target_id,
                                         "turns": sub_session.to_history()})
                    else:
                        await _send(ws, {"type": "system", "text": f"⚠️ 子 Agent '{target_id}' 的存档不存在"})
                except Exception as e:
                    await _send(ws, {"type": "system", "text": f"⚠️ 加载子 Agent 历史失败：{type(e).__name__}: {e}"})
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

    # —— 按客户端交互目标路由：本客户端切到了某个子 Agent → 文本直达该 Agent ——
    # （与 CLI /agent 切换后的直连语义对齐；多页签互不影响：其它页签仍走主 Agent work_q）
    _tgt = (client or {}).get("target", "_main_")
    if _tgt != "_main_" and not text.startswith("/"):
        reg0 = getattr(agent, "registry", None)
        e0 = reg0.lookup(_tgt) if reg0 else None
        if e0 is None or e0.agent is None:
            # 目标已失效（进程重启后 registry 重建）：复位回主 Agent 并提示
            if client is not None:
                client["target"] = "_main_"
            await _send(ws, {"type": "system", "text": f"⚠️ '{_tgt}' 已不在线，本客户端已切回主 Agent；消息将发给主 Agent"})
            _tgt = "_main_"
        elif e0.status == "running" or getattr(e0.agent, "busy", False):
            # 子 Agent 正在跑（异步任务中）：走插话队列（下一步边界注入），不并发 run
            e0.agent.queue_user_message(text)
            await _send(ws, {"type": "system", "transient": True,
                             "text": f"📥 已排队并将在下一步注入 '{_tgt}' 的当前任务"})
            return
        else:
            # 空闲：task 进 work_q（与主 Agent run 同 worker 串行——agent.run 非线程安全）。
            # 交互期间临时接通事件流（on_event=broadcast 带 agent_id → 只推给与该 Agent
            # 交互的客户端），平时异步任务保持无事件流。
            _sub = e0.agent
            def _run_sub(_a=_sub, _t=text):
                _old = getattr(_a, "on_event", None)
                _a.on_event = agent.on_event
                try:
                    _a.run(_t)
                finally:
                    _a.on_event = _old
            if _work_q is not None:
                _work_q.put(("task", _run_sub))
                await _send(ws, {"type": "system", "transient": True, "text": f"✅ 已发送给 '{_tgt}'，处理中…"})
            else:
                await _send(ws, {"type": "system", "text": "⚠️ 服务未接入主循环（work_q 缺失）"})
            return
    # _tgt == "_main_"（或已复位）：走原有主 Agent 路径

    # 斜杠命令（即时处理，不进 work_q）
    if text.startswith("/"):
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf):
                registry.dispatch(text, CommandContext(agent=agent, work_q=_work_q, state=_state))
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
        await _send(ws, {"type": "system", "transient": True,
                         "text": f"📥 已排队并将在下一步注入当前任务（队列 {len(agent.pending_messages)} 条）"})
    else:
        _work_q.put(("user", text))
        # transient=True：前端走右下角 toast（2s 消失）而非永久气泡——瞬时状态不该留在消息流里
        await _send(ws, {"type": "system", "transient": True, "text": "✅ 已接收，处理中…"})


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
                if phase == "round":
                    # 逐轮事件：批处理/循环节点每轮迭代完成（调试页白框实时增长 + 轮次下拉）
                    _broadcast({"type": "wf_debug_round", "id": ev.get("id"),
                                "round": ev.get("round"), "outputs": ev.get("outputs")})
                    return
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
