"""script_tools.py —— 外置脚本工具：扫描约定目录 → agt_register() 元信息 → 注册进 Toolbox。

目录约定（信任模型与 workflows/agents 一致：目录内 .py 会被 import 执行，勿放不受信脚本）：
  tools/            workspace 级（随仓库分发；builtin/ 子目录放原内置迁移件）
  .agent/tools/     用户/Agent 私有（实验性工具）
扫描顺序 tools/ → .agent/tools/，同名后者覆盖前者 → 再覆盖内置（后注册胜出，
同时成为用户定制覆写内置工具的机制）。

脚本约定：定义 agt_register() 返回描述符列表（每项一个工具）：
  {"name": "kw_score",
   "func": <可调用对象>,                # inline 模式（默认）：直接持有函数，零开销调用
   # 或 "mode": "subprocess" + "func": "run"（函数名，仅文档用途）—— 子进程隔离执行
   "description": "关键词命中比例评分 0~1",
   "params": {"keywords": "关键词数组", "text": "待评分文本"},   # 参数名→描述（inline 还会
                                                                # 叠加函数类型注解推断的 schema）
   "outputs": [{"name": "raw", "type": "number"}],              # 显式输出 schema（可省略）
   "hidden": False,                    # True=注册但不投影给 LLM（LIGHT_TOOLS 同款语义）
   "mode": "inline",                   # inline | subprocess（默认 inline）
   "group": "light",                   # 编辑器/文档分组标注
   "version": 1}                       # 元信息接口版本（未来兼容判定占位）

subprocess 模式协议（实验性/不受信脚本用；每次调用 +200~500ms 冷启动）：
  参数经 stdin 传一行 JSON：{"args": {...}}；
  脚本往 stdout 写 NDJSON（每行一个 JSON）：
    流式（可选多行）：{"type": "stream", "text": "..."}
    结束（最后一行）：{"type": "done", "result": <任意 JSON 值>}
  不遵循协议（纯文本输出）→ 整个 stdout 原样作为结果（优雅降级，兼容 run_script 老脚本）。
  超时/工作目录/PYTHONPATH/CREATE_NO_WINDOW 与 run_script 同款。

热加载：reload_script_tools(agent)（/reload tools 命令调）——mtime 缓存失效 + 重扫 +
摘除旧注册 + 重挂新工具。改脚本秒级生效，不再需要 /restart。
"""
from __future__ import annotations

import importlib.util
import logging
import sys
from pathlib import Path

from tools import Tool, Toolbox

_LOG = logging.getLogger("agt.script_tools")

# 模块缓存：abs_path -> {"mtime": float, "tools": [Tool]}
# mtime 未变直接复用（重复扫描零开销）；变了才重新 import（热加载）。
_CACHE: dict = {}
# 上次 attach 进目标 Toolbox 的脚本工具名（reload 时先摘除这些，避免改名/删除后残留）
_LAST: dict = {"names": set(), "failed": []}


def _import_fresh(path: Path):
    """以独立模块名加载脚本（sys.modules 里摘掉旧实例再 import——reload 语义）。
    模块名带路径 hash：同 stem 不同目录不冲突。"""
    modname = f"agt_script_{path.stem}_{abs(hash(str(path))) % 100000}"
    sys.modules.pop(modname, None)
    spec = importlib.util.spec_from_file_location(modname, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[modname] = mod      # 脚本内自引用/数据类需要
    spec.loader.exec_module(mod)
    return mod


def _tool_from_desc(desc: dict, path: Path) -> Tool:
    """描述符 → Tool。inline：包装真实函数（类型注解自动推断 schema + 描述符覆盖
    name/description/参数描述）；subprocess：shim 函数 + 描述符手搓 schema。"""
    if not isinstance(desc, dict):
        raise ValueError(f"描述符需为 dict，收到 {type(desc).__name__}")
    name = str(desc.get("name") or "").strip()
    if not name:
        raise ValueError("描述符缺 name")
    description = str(desc.get("description") or "").strip() or name
    outputs = desc.get("outputs")
    hidden = bool(desc.get("hidden"))
    # params 简单形态：{参数名: "描述"}；subprocess 也接受完整 schema dict（{"type":..., "description":...}）
    pd = {k: v for k, v in (desc.get("params") or {}).items() if isinstance(v, str)}

    mode = str(desc.get("mode") or "inline").strip().lower()
    if mode == "subprocess":
        script_path = str(path)

        def _shim(**kwargs):
            return _run_subprocess(script_path, name, kwargs)

        _shim.__doc__ = description
        t = Tool(_shim, outputs=outputs, hidden=hidden)
        # 覆盖 schema：shim 的 **kwargs 推断不出参数表，按描述符手搓
        props, required = {}, []
        for pname, pdef in (desc.get("params") or {}).items():
            sch = dict(pdef) if isinstance(pdef, dict) else {"type": "string", "description": str(pdef)}
            sch.setdefault("type", "string")
            if sch.pop("required", None):
                required.append(pname)
            props[pname] = sch
        t.schema["function"]["parameters"] = {"type": "object", "properties": props, "required": required}
        t.mode = "subprocess"
        t.script_path = script_path
        t.script_func = str(desc.get("func") or "")
    else:
        func = desc.get("func")
        if not callable(func):
            raise ValueError("inline 模式需要 func=可调用对象")
        # Tool 要求 docstring 作描述；描述符优先（函数自己的 docstring 可省）
        if not (getattr(func, "__doc__", "") or "").strip():
            func.__doc__ = description
        t = Tool(func, outputs=outputs, hidden=hidden, param_descriptions=pd)
        t.mode = "inline"

    # 描述符的 name/description 覆盖函数推断值（schema 同步改）
    if name != t.name:
        t.name = name
    t.description = description
    t.schema["function"]["name"] = t.name
    t.schema["function"]["description"] = description
    t.group = str(desc.get("group") or "")
    t.source = str(path)
    return t


def _run_subprocess(script_path: str, tool_name: str, kwargs: dict) -> str:
    """subprocess 模式执行：stdin 传 {"args":...}，stdout NDJSON 协议解析。"""
    import json as _json
    import subprocess as _sp
    from real_tools import WORKSPACE, TOOL_TIMEOUT, _tool_emit
    import os

    env = dict(os.environ)
    pp = str(WORKSPACE)
    if env.get("PYTHONPATH"):
        pp = pp + os.pathsep + env["PYTHONPATH"]
    env["PYTHONPATH"] = pp
    payload = _json.dumps({"args": kwargs}, ensure_ascii=False, default=str)
    try:
        proc = _sp.run([sys.executable, script_path], input=payload, capture_output=True,
                       text=True, timeout=TOOL_TIMEOUT, env=env, cwd=str(WORKSPACE),
                       encoding="utf-8", errors="replace",
                       creationflags=(_sp.CREATE_NO_WINDOW if os.name == "nt" else 0))
    except _sp.TimeoutExpired:
        return f"[脚本工具执行超时（>{TOOL_TIMEOUT}s，可 set_tool_timeout 调大）]"
    except Exception as e:
        return f"[执行失败] {type(e).__name__}: {e}"
    if proc.returncode != 0:
        err = (proc.stderr or "").strip()
        return f"[脚本工具出错 rc={proc.returncode}]\nstderr: {err[-500:]}"
    out = (proc.stdout or "").strip()
    if not out:
        return "(无输出)"
    # NDJSON 协议解析：done 行取 result；stream 行转发 _tool_emit（观测页可见）
    result, protocol = None, False
    for ln in out.splitlines():
        ln = ln.strip()
        if not ln.startswith("{"):
            continue
        try:
            obj = _json.loads(ln)
        except Exception:
            continue
        if not isinstance(obj, dict):
            continue
        if obj.get("type") == "done":
            result = obj.get("result")
            protocol = True
        elif obj.get("type") == "stream" and _tool_emit:
            _tool_emit({"type": "tool_stream", "name": tool_name, "text": str(obj.get("text", ""))})
    if protocol:
        if isinstance(result, (dict, list)):
            return _json.dumps(result, ensure_ascii=False, default=str)
        return str(result)
    return out   # 非协议输出 → 原样返回（兼容纯文本 print 的老脚本）


def default_dirs() -> list:
    """约定扫描目录：tools/ → .agent/tools/。"""
    from real_tools import WORKSPACE
    return [WORKSPACE / "tools", WORKSPACE / ".agent" / "tools"]


def scan_script_tools(dirs=None) -> Toolbox:
    """扫描目录 → Toolbox（同名后扫覆盖先扫；坏脚本跳过并记 _scan_failed，不炸主程序）。"""
    if dirs is None:
        dirs = default_dirs()
    tb = Toolbox()
    failed = []
    for d in dirs:
        d = Path(d)
        if not d.is_dir():
            continue
        for py in sorted(d.rglob("*.py")):   # rglob：支持 tools/builtin/ 等子目录组织
            if py.name.startswith("_"):
                continue
            abs_p = str(py.resolve())
            try:
                mtime = py.stat().st_mtime
                ent = _CACHE.get(abs_p)
                if ent and ent["mtime"] == mtime:
                    tools = ent["tools"]
                else:
                    mod = _import_fresh(py)
                    reg = getattr(mod, "agt_register", None)
                    if reg is None:
                        _CACHE[abs_p] = {"mtime": mtime, "tools": []}
                        continue    # 普通脚本（无 agt_register）静默跳过
                    tools = [_tool_from_desc(dd, py) for dd in (reg() or [])]
                    _CACHE[abs_p] = {"mtime": mtime, "tools": tools}
                for t in tools:
                    tb.register_or_replace(t)
            except Exception as e:
                failed.append(f"{py.name}: {type(e).__name__}: {e}")
                _LOG.warning("脚本工具 %s 注册失败：%s", py.name, e)
    tb._scan_failed = failed
    return tb


def attach_script_tools(tb: Toolbox, dirs=None) -> Toolbox:
    """把脚本工具注册进 tb（同名覆盖——后注册胜出即外置覆盖内置）；记录名字供 reload 摘除。
    返回扫描出的脚本工具 Toolbox（含 _scan_failed 供命令输出）。"""
    stb = scan_script_tools(dirs)
    for t in stb:
        tb.register_or_replace(t)
    _LAST["names"] = {t.name for t in stb}
    _LAST["failed"] = list(getattr(stb, "_scan_failed", []))
    return stb


def reload_script_tools(agent, dirs=None) -> str:
    """热加载（/reload tools）：mtime 失效重扫 + 摘除旧注册 + 重挂新工具。返回摘要文本。
    dirs：默认 None=约定目录（tools/ + .agent/tools/）；测试/自定义场景可传目录列表。"""
    # 摘旧（改名/删除的工具不留残尸）
    gone = 0
    for nm in list(_LAST["names"]):
        if agent.tools.unregister(nm):
            gone += 1
    _CACHE.clear()   # 强制全量重扫（不只是 mtime 变了的——目录增删文件也要看到）
    stb = attach_script_tools(agent.tools, dirs=dirs)
    n = len(_LAST["names"])
    failed = _LAST["failed"]
    head = f"✅ 脚本工具已重载：注册 {n} 个（摘除旧 {gone} 个）"
    if failed:
        head += f"；失败 {len(failed)} 个：\n  " + "\n  ".join(failed)
    return head
