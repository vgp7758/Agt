"""node_plugins.py —— 节点插件扫描装配（前后端配对：同目录同名 .py + .js）。

目录约定（信任模型与 tools/workflows 一致；同名 type 后扫覆盖先扫）：
  src/assets/nodes_builtin/   随包核心插件（最低优先级）
  nodes/                      workspace 级
  .agent/nodes/               用户/Agent 私有（扩展面：写两个文件即得全新节点类型）

.py 约定：def agt_node() -> 描述符：
  {"type": "58",                 # Coze type code（新类型自编 "N1","N2"... 段）
   "label": "ToJSON",            # 编辑器显示名
   "handler": async fn,          # (node, ctx) -> {"outputs": {...}, "port": str|None}
   "xml": {"to_canvas": fn?, "to_xml": fn?}}   # 可选序列化钩子；缺省通用 <in>/<out>
配对校验：扫描后交叉核对——.py 无配对 .js → handler 照常注册但告警（编辑器旧渲染兜底）；
.js 无配对 .py → 拒绝注入 + 告警（前端渲染将无处执行）。
核心节点（1/2/21/28/19/29 + 调度器）永远内置，插件不可覆盖。

热加载：reload_node_plugins()（/reload nodes）——后端 handler 秒级生效；前端 .js 需 Ctrl+F5。
"""
from __future__ import annotations

import importlib.util
import logging
import sys
from pathlib import Path

_LOG = logging.getLogger("agt.node_plugins")

# 核心内置 type（插件覆盖这些 → 拒绝并告警：调度器协议不容覆写）
CORE_TYPES = {"1", "2", "21", "28", "19", "29", "13"}

# 模块缓存：abs_path(.py) -> {"mtime", "desc"}；js 文件内容缓存 abs_path -> {"mtime", "source"}
_CACHE_PY: dict = {}
_CACHE_JS: dict = {}
# 上次装配状态（reload 摘除用 + js payload）
_LAST: dict = {"types": set(), "warnings": [], "js": []}


def _import_fresh(path: Path):
    """以独立模块名加载插件 .py（绕过 importlib 的 .pyc 缓存——exec+compile 直读源码，
    mtime 变了必生效）。模块名含 mtime_ns：同 mtime（未改）→ 同名 → sys.modules 命中（零开销）；
    mtime 变了 → 不同名 → 新模块 → 强制重执行。"""
    import types
    mtime_ns = int(path.stat().st_mtime_ns)
    modname = f"agt_node_{path.stem}_{mtime_ns % 100000000}"
    mod = sys.modules.get(modname)
    if mod is not None:
        return mod
    mod = types.ModuleType(modname)
    mod.__file__ = str(path)
    sys.modules[modname] = mod      # 插件内自引用/数据类需要
    source = path.read_text(encoding="utf-8")
    exec(compile(source, str(path), "exec"), mod.__dict__)
    return mod


def default_dirs() -> list:
    from real_tools import WORKSPACE
    pkg = Path(__file__).parent / "assets" / "nodes_builtin"
    return [pkg, WORKSPACE / "nodes", WORKSPACE / ".agent" / "nodes"]


def _read_js(path: Path) -> str:
    key = str(path.resolve())
    mtime = path.stat().st_mtime
    ent = _CACHE_JS.get(key)
    if ent and ent["mtime"] == mtime:
        return ent["source"]
    src = path.read_text(encoding="utf-8")
    _CACHE_JS[key] = {"mtime": mtime, "source": src}
    return src


def scan_node_plugins(dirs=None) -> dict:
    """扫描 → {"handlers": {type: desc}, "js": [{name,type,source}], "warnings": [...]}。
    同 type 后扫覆盖先扫；坏脚本/缺配对记 warnings 不炸主程序。"""
    if dirs is None:
        dirs = default_dirs()
    handlers: dict = {}
    js_payload: list = []
    warnings: list = []
    for d in dirs:
        d = Path(d)
        if not d.is_dir():
            continue
        for py in sorted(d.rglob("*.py")):
            if py.name.startswith("_"):
                continue
            try:
                abs_p = str(py.resolve())
                mtime = py.stat().st_mtime
                ent = _CACHE_PY.get(abs_p)
                if ent and ent["mtime"] == mtime:
                    desc = ent["desc"]
                else:
                    mod = _import_fresh(py)
                    reg = getattr(mod, "agt_node", None)
                    if reg is None:
                        _CACHE_PY[abs_p] = {"mtime": mtime, "desc": None}
                        continue     # 普通脚本静默跳过
                    desc = reg()
                    _CACHE_PY[abs_p] = {"mtime": mtime, "desc": desc}
                if not isinstance(desc, dict) or not desc.get("type") or not callable(desc.get("handler")):
                    warnings.append(f"{py.name}: 描述符需含 type 与可调用 handler")
                    continue
                t = str(desc["type"]).strip()
                if t in CORE_TYPES:
                    warnings.append(f"{py.name}: type {t} 是核心节点，禁止插件覆盖")
                    continue
                # 配对校验：同目录同名 .js
                js_path = py.with_suffix(".js")
                if not js_path.exists():
                    warnings.append(f"{py.name}: 无配对 {py.stem}.js（handler 已注册，编辑器用旧渲染）")
                    desc["_js_ok"] = False
                else:
                    desc = dict(desc)
                    desc["_js_ok"] = True
                    desc["_source"] = abs_p
                    js_payload.append({"name": py.stem, "type": t,
                                       "source": _read_js(js_path)})
                handlers[t] = desc
            except Exception as e:
                warnings.append(f"{py.name}: {type(e).__name__}: {e}")
                _LOG.warning("节点插件 %s 注册失败：%s", py.name, e)
        # 独立 .js（无配对 .py）→ 拒绝注入
        for js in sorted(d.rglob("*.js")):
            if js.with_suffix(".py").exists():
                continue
            if not js.name.startswith("_"):
                warnings.append(f"{js.name}: 无配对 {js.stem}.py，已拒绝注入（前端渲染将无处执行）")
    # js payload 按 type 去重（后扫的覆盖先扫的——目录优先级语义）
    seen = {}
    for item in js_payload:
        seen[item["type"]] = item
    return {"handlers": handlers, "js": list(seen.values()), "warnings": warnings}


def attach_node_plugins(handlers_table: dict, dirs=None) -> dict:
    """把插件 handler 注册进 NODE_HANDLERS（同 type 覆盖=定制机制）；记录状态供 reload。
    返回扫描结果（含 warnings 供命令输出）。"""
    res = scan_node_plugins(dirs)
    for t, desc in res["handlers"].items():
        handlers_table[t] = desc["handler"]
    _LAST["types"] = set(res["handlers"].keys())
    _LAST["warnings"] = res["warnings"]
    _LAST["js"] = res["js"]
    for w in res["warnings"]:
        _LOG.warning("节点插件：%s", w)
    return res


def reload_node_plugins(handlers_table: dict, dirs=None) -> str:
    """/reload nodes：摘旧 + 全量重扫 + 重挂。返回摘要文本。"""
    gone = 0
    for t in list(_LAST["types"]):
        if handlers_table.pop(t, None) is not None:
            gone += 1
    _CACHE_PY.clear()
    _CACHE_JS.clear()
    res = attach_node_plugins(handlers_table, dirs=dirs)
    head = f"✅ 节点插件已重载：{len(res['handlers'])} 类（摘旧 {gone} 个）"
    if res["warnings"]:
        head += f"；警告 {len(res['warnings'])} 条：\n  " + "\n  ".join(res["warnings"])
    return head


def node_js_payload() -> list:
    """当前装配的前端插件 js（server 注入编辑器/debug 页用）。
    _LAST 为空（server 先于 workflow 导入、attach 尚未跑）时自举扫描一次。"""
    if not _LAST.get("js"):
        _LAST["js"] = scan_node_plugins()["js"]
    return list(_LAST["js"])


def catalog_entries() -> list:
    """插件节点目录条目（list_workflow_nodes / query_workflow_node 动态聚合用）。
    每项 {"type", "name", "desc", "xml"}——来自各插件 agt_node() 声明的 catalog 字段
    （元信息跟实现走：改插件 handler/示例同文件维护；用户级 .agent/nodes/ 的目录同样生效）。
    _LAST 为空时自举扫描一次（与 node_js_payload 同模式）。"""
    if not _LAST.get("handlers"):
        _LAST["handlers"] = scan_node_plugins()["handlers"]
    out = []
    for t, desc in _LAST["handlers"].items():
        cat = (desc or {}).get("catalog") if isinstance(desc, dict) else None
        if not isinstance(cat, dict):
            continue   # 未声明 catalog 的插件不进目录（如实验节点）——按需声明
        out.append({
            "type": t,
            "name": str(cat.get("name") or desc.get("label") or t),
            "desc": str(cat.get("desc") or ""),
            "xml": str(cat.get("xml") or ""),
        })
    return out
