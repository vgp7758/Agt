"""workflow.py —— Coze 工作流引擎（原生画布 JSON 执行 + 每轮扫描成工具）。

设计原则（忠实 Coze Studio 模型，见 coze-studio/backend/domain/workflow/）：
  - .agent/workflows/<名>.json   = Coze 原生画布 JSON（{nodes, edges, versions}），只读不改。
  - <名>.json.meta               = 旁车：name/description（Agent 工具元数据）/coze_url/可选 inputs 覆盖/enabled。
  - 每轮对话扫描该目录，把每个工作流注册成工具 wf_<name>，入参取自开始节点(100001)的 outputs。
  - 执行器解析画布建图，从开始节点出发按边前传；变量按 Coze 的 ref 表达式解析；分支节点按端口选路。

节点 type（精选；其余后续阶段补）：
  1=开始 2=结束 3=LLM 5=代码 8=选择器(分支) 15=文本处理 21=循环 22=意图识别
  28=批处理 32=变量聚合 40=变量赋值 45=HTTP 9=子工作流 58/59=JSON 序列化/解析

S1 实现：基座（解析/resolve_value/调度）+ Entry/Exit + LLM(3)。其余节点后续阶段加入 NODE_HANDLERS。
"""
from __future__ import annotations

import importlib.util
import inspect
import json
import logging
import re
from pathlib import Path

import config as _config
from llm_client import LLMClient
from real_tools import WORKSPACE
from tools import Tool, Toolbox

_LOG = logging.getLogger("agt.workflow")   # 0d852a0 引入 _LOG.warning 时漏了定义——NameError 让钩子链静默

# Coze 固定节点 ID
ENTRY_ID = "100001"   # type "1" 开始
EXIT_ID = "900001"    # type "2" 结束
WF_PREFIX = "wf_"

# debug_run 后缓存 ctx + 画布（供 server hotswap/rerun/list_node_outputs）
_debug_ctx: dict = {}   # {canvas, nodes, edges, ctx}

# ========== 工作流运行观测（run registry：对话中实时观测执行情况） ==========
# 每次 execute 可带 run_id（new_wf_run 注册）→ 节点事件写入 _WF_RUNS，观测页轮询读取。
# 线程安全：同步钩子(线程池)/async 钩子(后台线程)/主循环 wf_* 工具可能并发执行。
import threading as _threading
import time as _time
import uuid as _uuid

_WF_RUNS: dict = {}                     # run_id → {name, hook, status, started_at, finished_at, nodes[]}
_WF_RUNS_LOCK = _threading.Lock()
_WF_RUNS_MAX = 50                       # 内存上限：最近 50 次运行（旧的清掉）


def new_wf_run(name: str, hook: str = "", canvas: dict = None) -> str:
    """注册一次工作流运行，返回 run_id（观测页 URL 用）。
    canvas：可选，存入 run 供观测页"在调试页查看"按钮导入调试页。"""
    global _full_total
    rid = _uuid.uuid4().hex[:8]
    with _WF_RUNS_LOCK:
        # 容量控制（保留最近 N 个；全量输出预算相应扣减）
        while len(_WF_RUNS) >= _WF_RUNS_MAX:
            oldest = min(_WF_RUNS.items(), key=lambda kv: kv[1].get("started_at", 0))[0]
            old = _WF_RUNS.pop(oldest, None)
            if old:
                _full_total -= sum(len(n.get("full") or "") for n in old.get("nodes", []))
        _WF_RUNS[rid] = {"run_id": rid, "name": name, "hook": hook, "status": "running",
                         "started_at": _time.time(), "finished_at": None, "nodes": [],
                         "canvas": (json.loads(json.dumps(canvas, ensure_ascii=False, default=str))
                                    if isinstance(canvas, dict) else None)}
        # LLM 节点 model 溯源（2026-08-31·local-qwen 悬案收网）：注册时从 canvas 提取所有
        # llm 节点的 llmParam.model 值——观测页/API 直接可见每次执行【入口处】用的 model。
        # 与 canvas 快照/执行 warning 三方对照：llm_models=local-qwen → canvas 在 _run_one
        # 之前已被污染（_wf_canvas_index 缓存层）；=local-lfm 但 warning local-qwen → handler 层。
        try:
            _llm_models = []
            for _n in (canvas or {}).get("nodes", []):
                if str(_n.get("type")) == "3":
                    for _p in (((_n.get("data") or {}).get("inputs") or {}).get("llmParam") or []):
                        if isinstance(_p, dict) and _p.get("name") == "model":
                            _llm_models.append(str((((_p.get("input") or {}).get("value") or {}).get("content")) or ""))
            _WF_RUNS[rid]["llm_models"] = _llm_models
        except Exception:
            pass
    return rid


def _track_apply(nodes_list: list, ev: dict, store_full: bool = True):
    """对一个节点事件列表应用 start/end/error/meta 事件（run["nodes"] 与嵌套 children 共用）。
    store_full=False 用于嵌套子节点（只存 preview，全文与预算仍归顶层节点）。"""
    global _full_total
    kind = ev.get("ev")
    t = ev.get("t", _time.time())
    if kind == "node_start":
        nodes_list.append({"id": ev.get("id", ""), "title": ev.get("title", ""),
                           "ntype": ev.get("ntype", ""), "status": "running",
                           "t_start": t, "t_end": None, "dur_ms": None, "preview": ""})
    elif kind in ("node_end", "node_error"):
        for n in reversed(nodes_list):
            if n["id"] == ev.get("id") and n["status"] == "running":
                n["status"] = "done" if kind == "node_end" else "error"
                n["t_end"] = t
                try:
                    n["dur_ms"] = int((t - n["t_start"]) * 1000)
                except Exception:
                    pass
                n["preview"] = ev.get("preview", "")
                full = ev.get("full")
                if store_full and isinstance(full, str) and full and _full_total < _FULL_BUDGET:
                    n["full"] = full
                    _full_total += len(full)
                break
    elif kind == "node_meta":
        # 复合节点/子工作流的嵌套元数据：children（最后一轮/子执行轨迹）、rounds、childmeta
        for n in reversed(nodes_list):
            if n["id"] == ev.get("id"):
                for k in ("children", "rounds", "childmeta", "wf_name"):
                    if ev.get(k) is not None:
                        n[k] = ev[k]
                break


def _run_track(run_id: str, ev: dict):
    """写入顶层运行轨迹事件（节点 start/end/error/meta、run done/failed）。未知 run 静默忽略。"""
    global _full_total
    if not run_id:
        return
    with _WF_RUNS_LOCK:
        run = _WF_RUNS.get(run_id)
        if not run:
            return
        t = ev.get("t", _time.time())
        if ev.get("ev") in ("node_start", "node_end", "node_error", "node_meta"):
            _track_apply(run["nodes"], ev)
        elif ev.get("ev") == "run_done":
            run["status"] = "done"; run["finished_at"] = t
        elif ev.get("ev") == "run_failed":
            run["status"] = "failed"; run["finished_at"] = t
            run.setdefault("error", ev.get("error", ""))


def _track_dispatch(ctx, ev: dict):
    """节点事件分发：嵌套执行（复合体迭代内/子工作流内）→ 写 ctx.track_stack 栈顶容器；
    顶层 → _run_track。子节点全文不存（观测页展开看 preview；全文预算归顶层）。"""
    st = getattr(ctx, "track_stack", None)
    if st:
        _track_apply(st[-1], ev, store_full=False)
    else:
        _run_track(getattr(ctx, "run_id", None), ev)


def get_wf_run(run_id: str) -> dict | None:
    with _WF_RUNS_LOCK:
        r = _WF_RUNS.get(run_id)
        if not r:
            return None
        # 剥离 full（观测页 2s 轮询不能每次传几十万字符；全文走 /api/wf/runs/<id>/node/<nid>）
        # 与 canvas（"在调试页查看"按钮按需 ?canvas=1 单次拉取，不进轮询）
        nodes = []
        for n in r["nodes"]:
            n2 = {k: v for k, v in n.items() if k != "full"}
            n2["has_full"] = ("full" in n)
            nodes.append(n2)
        out = {k: v for k, v in r.items() if k not in ("full", "canvas")}
        out["has_canvas"] = r.get("canvas") is not None
        out["nodes"] = nodes
        return out


def get_wf_run_canvas(run_id: str) -> dict | None:
    """取 run 注册时快照的画布（观测页"在调试页查看"按钮用；未存返回 None）。"""
    with _WF_RUNS_LOCK:
        r = _WF_RUNS.get(run_id)
        return r.get("canvas") if r else None


def get_wf_node_full(run_id: str, node_id: str) -> str | None:
    """取某节点全量输出（观测页 text/plain 路由用）。run/node 不存在返回 None；
    节点存在但未记录全文（预算耗尽/running 中）返回 ''。"""
    with _WF_RUNS_LOCK:
        r = _WF_RUNS.get(run_id)
        if not r:
            return None
        for n in reversed(r["nodes"]):
            if n["id"] == node_id:
                return n.get("full", "")
    return None


def list_wf_runs() -> list:
    """最近运行摘要（倒序，不含 nodes 明细——列表页用）。"""
    with _WF_RUNS_LOCK:
        items = [{"run_id": r["run_id"], "name": r["name"], "hook": r["hook"], "status": r["status"],
                  "started_at": r["started_at"], "finished_at": r["finished_at"],
                  "node_count": len(r["nodes"])} for r in _WF_RUNS.values()]
    items.sort(key=lambda x: x["started_at"], reverse=True)
    return items


def _preview_str(v, cap=200) -> str:
    """输出预览：dict/list 转 JSON，截断。"""
    try:
        import json as _json
        s = _json.dumps(v, ensure_ascii=False, default=str) if isinstance(v, (dict, list)) else str(v)
    except Exception:
        s = str(v)
    s = s.replace("\n", " ").strip()
    return s[:cap] + ("…" if len(s) > cap else "")


_FULL_CAP = 200_000       # 单节点全量输出上限（字符；观测页纯文本路由用，超出截断标注）
_FULL_BUDGET = 20_000_000  # 全量输出总预算（字符；超预算后续节点只存预览，防观测功能吃爆内存）
_full_total = 0            # 已存全量字符计数（evict 时相应扣减）


def _full_str(v, cap=_FULL_CAP) -> str:
    """节点全量输出（保留换行/结构；观测页 text/plain 全文用）。超 cap 截断并标注。"""
    try:
        import json as _json
        s = _json.dumps(v, ensure_ascii=False, indent=None, default=str) if isinstance(v, (dict, list)) else str(v)
    except Exception:
        s = str(v)
    if len(s) > cap:
        return s[:cap] + f"\n\n…（全量输出超 {_FULL_CAP} 字符上限，已截断；完整值共 {len(s)} 字符）"
    return s

# Coze 变量类型 → Python 类型（生成工具参数签名用）
_TYPE_MAP = {
    "string": str, "str": str, "integer": int, "int": int, "long": int,
    "number": float, "float": float, "double": float,
    "boolean": bool, "bool": bool,
    "object": dict, "list": list, "array": list,
    "file": str, "time": str,
}


class WorkflowError(Exception):
    """工作流解析/执行错误（会被转成文本回传模型，不炸流程）。"""


# ========== 变量解析（Coze 的 literal / ref / object_ref）==========

def _dotted_get(obj, name: str):
    """按点号取子字段：'obj.field1' → obj['field1']['field1']...；支持 list 下标 + .length/.is_empty。
    .length 适用 list/str/dict（返回 len()）；.is_empty 适用 list/str（返回 bool）。"""
    if not name:
        return obj
    cur = obj
    for part in name.split("."):
        if cur is None:
            return None
        # .length / .is_empty 特殊属性（适用 list/str/dict）
        if part == "length":
            try:
                cur = len(cur)
            except TypeError:
                return None
            continue
        if part == "is_empty":
            try:
                cur = len(cur) == 0
            except TypeError:
                return None
            continue
        if isinstance(cur, dict):
            cur = cur.get(part)
        elif isinstance(cur, list):
            try:
                cur = cur[int(part)]
            except ValueError:
                # 非数字下标 = 字段名：逐项提取该字段组成数组（list of dict → list of values）——
                # 批处理节点的 all_outputs.score 这类"批内字段提取"引用场景
                out = []
                for it in cur:
                    if isinstance(it, dict):
                        out.append(it.get(part))
                    else:
                        out.append(None)
                cur = out
            except IndexError:
                return None
        else:
            return None
    return cur


def resolve_value(block_input, ctx) -> object:
    """解析一个 Coze BlockInput（{type, value:{type, content, rawMeta?}}）为 Python 值。
    literal → content（模板 {{}} 不在此渲染，由调用方按需 render_template）；
    ref     → 按 source 查上游节点输出或全局变量；
    object_ref → 按 schema 逐字段组装。rawMeta 忽略（前端专用）。"""
    if block_input is None:
        return None
    if not isinstance(block_input, dict):
        return block_input
    val = block_input.get("value", block_input)
    if not isinstance(val, dict):
        return val
    vt = val.get("type")
    content = val.get("content")
    if vt == "ref":
        return _resolve_ref(content or {}, ctx)
    if vt == "object_ref":
        return _resolve_object_ref(block_input, ctx)
    # literal 或未知 → 直接取 content
    return content


def _resolve_ref(content: dict, ctx) -> object:
    source = content.get("source")
    if source == "block-output":
        block_id = str(content.get("blockID", ""))
        name = content.get("name", "")
        return _dotted_get(ctx.node_outputs.get(block_id, {}), name)
    if source == "loop-item":
        # 批处理模式：取当前 item（name 空=整个 item，name=字段名取子字段）
        item = getattr(ctx, "batch_item", None)
        if item is None:
            return None
        name = content.get("name", "")
        return item if not name else _dotted_get(item, name)
    if source == "loop-index":
        return getattr(ctx, "batch_index", None)
    if source in ("global_variable_app", "global_variable_system", "global_variable_user"):
        path = content.get("path") or []
        return _dotted_get(ctx.global_vars, ".".join(str(p) for p in path))
    return None


def _resolve_object_ref(block_input: dict, ctx) -> dict:
    """object_ref：content 省略，子字段在 schema[] 里各自带 input.value。"""
    schema = block_input.get("schema")
    if schema is None:
        val = block_input.get("value") or {}
        schema = val.get("schema") if isinstance(val, dict) else None
    out = {}
    for field in schema or []:
        fname = field.get("name")
        if fname is None:
            continue
        out[fname] = resolve_value(field.get("input"), ctx)
    return out


def render_template(text: str, params: dict) -> str:
    """把 {{name}} / ${name} / {{a.b}} / ${a.b} 替换为 params 中的值；
    dict/list 转 JSON，None 转空串。同时支持 {{}} 与 ${} 两种占位语法。"""
    def _repl(m):
        val = _dotted_get(params, m.group(1).strip())
        if val is None:
            return ""
        if isinstance(val, (dict, list)):
            return json.dumps(val, ensure_ascii=False)
        return str(val)
    # 先 ${...}，再 {{...}}（两种风格都支持）
    out = re.sub(r"\$\{([^}]+)\}", _repl, text or "")
    return re.sub(r"\{\{([^}]+)\}\}", _repl, out)


def _resolve_input_params(params_list: list, ctx) -> dict:
    """解析节点 inputParameters：[{name, input:BlockInput}] → {name: value}。"""
    out = {}
    for p in params_list or []:
        name = p.get("name")
        if name is None:
            continue
        out[name] = resolve_value(p.get("input"), ctx)
    return out


# ========== 节点处理器（S1：LLM；其余后续阶段补）==========

def _get_llm(ctx, model_name: str = ""):
    """按模型名获取 LLMClient；空名返回 ctx.llm（None=无上下文，调用方自行处理）。
    指定名字 → per-model 独立 client 缓存——ctx.llm 为 None 也可按名建（独立测试/
    agent=None 场景；此前 ctx.llm.model_name 在 None 时直接 AttributeError）。"""
    if not model_name:
        return ctx.llm
    if ctx.llm is not None and model_name == ctx.llm.model_name:
        return ctx.llm
    cache = getattr(ctx, "_llm_cache", None)
    if cache is None:
        ctx._llm_cache = cache = {}
    if model_name not in cache:
        try:
            profile = _config.get_profile(model_name)
            _c = LLMClient(profile=profile, model_name=model_name)
            # 复制主 client 的调用记录器（llm_calls 写入）：_record_call 对 recorder=None
            # 静默跳过——独立 client 不挂 recorder 则其调用不进 llm_calls（2026-08-31
            # 17:42 实测：local-lfm 真实执行成功、日志面板有记录，但 llm_calls 零写入
            # 的根因）。recorder 写主 session 的流水文件——钩子执行本属主 agent 的 session ✓
            _c.call_recorder = getattr(ctx.llm, "call_recorder", None) if ctx.llm is not None else None
            cache[model_name] = _c
        except KeyError as e:
            # 静默 fallback 是排障黑洞：LLM 节点选了 X 却悄悄用 ctx.llm 跑——
            # llm_calls 里 model 对不上，得靠这条日志定位（recap_gen 三轮排查的教训）
            _LOG.warning("LLM 节点模型 '%s' 未找到（%s）——回退 ctx.llm=%s。检查 models.json 键名/是否需 /reload models",
                         model_name, e, getattr(ctx.llm, "model_name", None))
            return ctx.llm
    return cache[model_name]


# （_handle_llm 已外置为节点插件：src/assets/nodes_builtin/）

def _outputs_to_json_schema(outputs: list) -> dict:
    """把 Coze 节点 outputs 字段定义转成 JSON Schema（object）。
    object 字段展开 properties；list 字段按 schema 取 items。"""
    def _var_to_schema(var: dict) -> dict:
        t = var.get("type", "string")
        sch = var.get("input", {}).get("schema") if isinstance(var.get("input"), dict) else None
        if sch is None:
            sch = var.get("schema")
        if t in ("object",) or (isinstance(sch, list)):
            props, req = {}, []
            for sub in (sch if isinstance(sch, list) else []):
                props[sub.get("name", "")] = _var_to_schema(sub)
                if sub.get("required"):
                    req.append(sub.get("name", ""))
            s = {"type": "object", "properties": props}
            if req:
                s["required"] = req
        elif t in ("list", "array"):
            if isinstance(sch, dict):
                s = {"type": "array", "items": _type_to_schema(sch.get("type", "string"), sch)}
            elif isinstance(sch, list):
                s = {"type": "array", "items": {"type": "object", "properties": {sub.get("name", ""): _var_to_schema(sub) for sub in sch}}}
            else:
                s = {"type": "array", "items": {}}
        else:
            s = {"type": t}
        if var.get("description"):   # 字段描述并入 schema，让模型理解字段含义
            s["description"] = var.get("description")
        return s

    def _type_to_schema(t, sch=None):
        if t in ("object",) and isinstance(sch, list):
            return _var_to_schema({"type": "object", "schema": sch})
        return {"type": t}

    # outputs 是多字段 → 包装成 object
    props, req = {}, []
    for o in outputs:
        nm = o.get("name", "")
        props[nm] = _var_to_schema(o)
    return {"type": "object", "properties": props, "required": list(props.keys())}


def _needs_structured_parse(outputs: list) -> bool:
    """outputs 里只要有一个字段不是 (name=="output" 且 type==string)，就视为结构化输出需解析。
    单个 output:string（最常见、纯文本 LLM 节点）→ 不解析，保持 {output: 文本} 旧行为。"""
    for o in outputs:
        if o.get("name") != "output" or o.get("type", "string") not in ("string", None):
            return True
    return False


def _coerce_field(v, t: str):
    """按声明的 output type 强转 LLM 解析出的 JSON 值，保证下游 selector/引用按强类型消费
    （_cmp 的 Equal 不做宽化，全靠上游给对类型；boolean 尤其要转成 Python bool）。"""
    try:
        if t in ("boolean", "bool"):
            if isinstance(v, bool):
                return v
            if isinstance(v, str):
                return v.strip().lower() in ("1", "true", "yes")
            return bool(v)
        if t in ("integer", "int"):
            return int(v)
        if t in ("number", "float"):
            return float(v)
    except (ValueError, TypeError):
        return v
    return v


def _parse_structured_output(content: str, outputs: list):
    """LLM 声明了结构化 outputs 时，尝试把模型输出的 JSON 解析成具名字段（按声明 type 强转）。
    容错：截取首个 '{' 到末个 '}' 之间的子串（抗 markdown 围栏/多余解释）。
    返回 {字段名: 值}；失败（非对象 / 截取不到 / JSON 解析失败）→ None，由调用方降级。"""
    s = (content or "").strip()
    i, j = s.find("{"), s.rfind("}")
    if i < 0 or j <= i:
        return None
    try:
        d = json.loads(s[i:j + 1])
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(d, dict):
        return None
    out = {}
    for o in outputs:
        nm = o.get("name")
        if not nm:
            continue
        t = o.get("type", "string")
        val = d.get(nm)
        if t == "object" and isinstance(val, dict) and isinstance(o.get("schema"), list):
            # 递归强转 object 子字段（如 found:"true"→True），保证下游 selector 按强类型判断
            row = {}
            for sf in o["schema"]:
                if not sf.get("name"): continue
                sfnm, sft = sf["name"], sf.get("type", "string")
                sfv = val.get(sfnm)
                if sft in ("list", "array") and sfv is not None and not isinstance(sfv, list):
                    sfv = [sfv]   # LLM 输出单个对象/标量 → 包成数组
                row[sfnm] = _coerce_field(sfv, sft)
            out[nm] = row
        elif t in ("list", "array"):
            # LLM 可能输出单个对象/标量而非数组 → 包成 list
            if val is not None and not isinstance(val, list):
                val = [val]
            out[nm] = val
        else:
            out[nm] = _coerce_field(val, t)
    return out


# （_handle_code 已外置为节点插件：src/assets/nodes_builtin/）

# （_handle_aggregator 已外置为节点插件：src/assets/nodes_builtin/）

# （_handle_assigner 已外置为节点插件：src/assets/nodes_builtin/）

def _to_num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _is_empty(x):
    return x is None or x == "" or x == [] or x == {}


def _cmp(op: int, l, r) -> bool:
    """Coze OperatorType：1=Equal 2=NotEqual 3-6=Length 系列 7=Contain 8=NotContain
    9=Empty 10=NotEmpty 11=True 12=False 13-16=数值 大于/大于等于/小于/小于等于。
    比较前按类型宽化：integer→int, number/float→float, boolean→bool。"""
    try:
        # 类型宽化（左右同时转）
        def _coerce(v, to_type):
            if v is None: return None
            try:
                if to_type in ("integer", "int"): return int(v)
                if to_type in ("number", "float"): return float(v)
                if to_type in ("boolean", "bool"): return str(v).lower() in ("1","true","yes")
                return v
            except (ValueError, TypeError): return v
        if op == 1:
            return l == r
        if op == 2:
            return l != r
        if op == 7:  # contain（object 退化为 contain_key）
            if isinstance(l, dict):
                return str(r) in l
            return str(r) in str(l) if l is not None else False
        if op == 8:
            if isinstance(l, dict):
                return str(r) not in l
            return str(r) not in str(l) if l is not None else True
        if op == 9:
            return _is_empty(l)
        if op == 10:
            return not _is_empty(l)
        if op == 11:
            return bool(l)
        if op == 12:
            return not bool(l)
        if op in (3, 4, 5, 6):  # 长度比较
            n = len(l) if l is not None else 0
            rn = _to_num(r)
            return {3: n > rn, 4: n >= rn, 5: n < rn, 6: n <= rn}[op]
        # 13-16 数值比较
        ln, rn = _to_num(l), _to_num(r)
        if ln is None or rn is None:
            return False
        return {13: ln > rn, 14: ln >= rn, 15: ln < rn, 16: ln <= rn}[op]
    except Exception:
        return False


def _eval_condition(condition: dict, ctx) -> bool:
    """求值一个分支条件：logic(1=OR/2=AND，默认AND) 组合多个 condition。"""
    conds = condition.get("conditions", [])
    logic = condition.get("logic", 2)
    results = []
    for c in conds:
        left_input = (c.get("left") or {}).get("input") or {}
        right_input = (c.get("right") or {}).get("input") or {}
        l = resolve_value(left_input, ctx)
        r = resolve_value(right_input, ctx) if "right" in c else None
        # 按输入声明的类型做值转换
        lt = left_input.get("type")
        rt = right_input.get("type") if "right" in c else None
        if lt in ("integer", "int"):
            try: l = int(l) if l is not None else 0
            except (ValueError, TypeError): pass
        elif lt in ("number", "float"):
            try: l = float(l) if l is not None else 0
            except (ValueError, TypeError): pass
        if rt in ("integer", "int"):
            try: r = int(r) if r is not None else 0
            except (ValueError, TypeError): pass
        elif rt in ("number", "float"):
            try: r = float(r) if r is not None else 0
            except (ValueError, TypeError): pass
        results.append(_cmp(c.get("operator"), l, r))
    if not results:
        return False
    return any(results) if logic == 1 else all(results)


# （_handle_selector 已外置为节点插件：src/assets/nodes_builtin/）

_INLINE_OUT_SUFFIX = "-function-inline-output"   # 复合节点体内迭代入口边的源端口后缀
_MAX_LOOP_ITERS = 10000   # 单次循环迭代上限（防失控；infinite 靠 Break 退出）


def _find_body_entry(edges: list, composite_id: str) -> str | None:
    """找子画布迭代入口节点 id（-function-inline-output 边的目标）。无则 None（旧格式）。"""
    for e in edges:
        if str(e.get("sourceNodeID")) == composite_id and \
           str(e.get("sourcePortID", "")).endswith(_INLINE_OUT_SUFFIX):
            return str(e.get("targetNodeID"))
    return None


def _run_composite_body(blocks_by_id: dict, edges: list, composite_id: str,
                        body_outputs: dict, ctx, max_steps: int = 5000):
    """执行复合节点内部子图一轮。返回 (信号, 本轮输出值)。
    信号 'done'/'break'/'continue'。本轮输出值：Continue 节点(2/29) 唯一 result 字段
    经连线 ref 解析出的值（裸值，不包 dict）。Break(19)/done 不产出（None）。
    数据完全由 continue 节点的 result 连线决定——不再兜底取最后产出值节点。"""
    start = None
    for e in edges:
        if str(e.get("sourceNodeID")) == composite_id and str(e.get("sourcePortID", "")).endswith(_INLINE_OUT_SUFFIX):
            start = str(e.get("targetNodeID"))
            break
    saved = ctx.node_outputs
    ctx.node_outputs = body_outputs

    try:
        current = start
        for _ in range(max_steps):
            if current is None or current == composite_id:
                return "done", None
            node = blocks_by_id.get(current)
            if node is None:
                return "done", None
            ntype = str(node.get("type"))
            if ntype == "19":
                # Break 节点：与 Continue 同款解析唯一 result 字段——break 携带值退出
                # （await 语义：就绪轮的最终结果进 all_outputs 末位；未连 result → None 不占位）
                for p in node.get("data", {}).get("inputs", {}).get("inputParameters", []):
                    if p.get("name") == "result":
                        return "break", resolve_value(p.get("input"), ctx)
                return "break", None
            if ntype in ("29", "2", "yield"):
                # Continue / Yield 节点：固定取唯一 result 字段经连线解析的值（裸值，不包 dict）。
                # yield ≡ continue(result)——"本轮产出该值"的语义化一等节点（用户提案：
                # 比"在 continue 上放输出端口"更自然；break 保持纯退出或带终值）。
                # result.type 与复合节点 nth_output.type 联动（编辑器约定），即 all/filtered 的 itemType。
                for p in node.get("data", {}).get("inputs", {}).get("inputParameters", []):
                    if p.get("name") == "result":
                        return "continue", resolve_value(p.get("input"), ctx)
                return "continue", None   # result 字段缺失（容错旧数据）→ 本轮空值
            if ntype == "1":
                # 迭代入口标记：跳过（保留预置的迭代上下文输出）
                current = _next_node(edges, current, None)
                continue
            handler = NODE_HANDLERS.get(ntype)
            if handler is None:
                raise WorkflowError(f"复合节点体内未支持的节点类型 {ntype}（节点 {current}）")
            # 观测：子画布内节点事件经 _track_dispatch（嵌套时写轮容器栈顶，顶层时进 run registry）
            _do_track = getattr(ctx, "run_id", None) is not None
            if _do_track:
                _track_dispatch(ctx, {"ev": "node_start", "id": current,
                                      "title": _node_title(node), "ntype": ntype})
            # 走 _run_node_with_batch：子画布内节点同样支持节点级批处理
            # （此前直接 handler(node, ctx)——体内 batch.enabled=true 被静默忽略，
            #   loop-item 引用解析到外层 batch_item=None → 工具参数全 None）
            result = _run_node_with_batch(node, handler, ctx)
            ctx.node_outputs[current] = result.get("outputs") or {}
            if _do_track:
                _track_dispatch(ctx, {"ev": "node_end", "id": current,
                                      "preview": _preview_str(ctx.node_outputs[current]),
                                      "full": _full_str(ctx.node_outputs[current])})
            current = _next_node(edges, current, result.get("port"))
        return "done", None
    finally:
        # debug：记录本复合节点最后一轮迭代的子画布输出（list_workflow_outputs('comp/sub') 读）
        if getattr(ctx, "record_sub", False):
            ctx.sub_trace[composite_id] = {k: dict(v) if isinstance(v, dict) else v
                                           for k, v in body_outputs.items()}
        ctx.node_outputs = saved


def _collect_composite_outputs(decl: list, round_items: list, batch: dict):
    """编辑器约定的复合节点输出收集（all_outputs/filtered_outputs/nth_output）：
    逐轮收集 round_items（continue 捕获的本轮输出），套用 inputs.batch.filter + nth，产出三组。
    decl 只传无 input 引用的输出定义（调用方已拆分原生/约定两组）。"""
    all_outputs = list(round_items or [])
    filt = (batch or {}).get("filter")
    filtered = []
    for o in all_outputs:
        if _is_null_output(o):   # 裸值/空 dict 判空统一走 _is_null_output（避免 o=0/False 被 not 误杀）
            continue
        if filt and not _eval_batch_filter(filt, o):
            continue
        filtered.append(o)
    nth = (batch or {}).get("nth", 0)
    try:
        nth = int(nth)
    except (TypeError, ValueError):
        nth = 0
    if not filtered:
        nth_output = None
    elif nth < 0 or nth >= len(filtered):
        nth_output = filtered[-1]
    else:
        nth_output = filtered[nth]
    return {"all_outputs": all_outputs, "filtered_outputs": filtered, "nth_output": nth_output}


def _setvar_left_name(left) -> str | None:
    """SetVar(type20) 的 left（目标变量名）多形态解析：
    - 标准 canvas 结构 {value:{content:{name}}}（编辑器路线）；
    - XML 简写字符串：'__entry__.keywords' / '1275951.keywords' / 裸 'keywords'
      （点号路径 → 取最后段；__entry__ 前缀即循环变量本名）；
    - 字符串字面量名（编辑器 round-trip 的 {input:{value:{content:'name'}}} 变体）。
    解析失败返回 None（调用方静默跳过——不炸循环体）。"""
    if isinstance(left, dict):
        lv = left.get("value", left)
        if isinstance(lv, dict):
            content = lv.get("content")
            if isinstance(content, dict) and content.get("name"):
                return str(content["name"])
            if isinstance(content, str) and content.strip():
                s = content.strip()
                return s.split(".")[-1] if "." in s else s
        elif isinstance(lv, str) and lv.strip():
            s = lv.strip()
            return s.split(".")[-1] if "." in s else s
    elif isinstance(left, str) and left.strip():
        s = left.strip()
        return s.split(".")[-1] if "." in s else s
    return None


def _setvar_right_value(right, ctx):
    """SetVar(type20) 的 right（新值）多形态解析：
    - 标准 canvas 结构（p['right'] 或 p['input'] 的 BlockInput）→ resolve_value；
    - XML 简写字符串：'ref:节点.字段' → 按点号路径解析上游输出；裸值 → 字面量。"""
    if isinstance(right, str) and right.strip().startswith("ref:"):
        path = right.strip()[4:]
        bid, _, fname = path.partition(".")
        src = ctx.node_outputs.get(bid, {})
        # __entry__ / 复合节点 id 同样在 node_outputs（迭代上下文已注入）
        return _dotted_get(src, fname) if fname else src
    return resolve_value(right, ctx)


def _handle_loop_setvar(node: dict, ctx) -> dict:
    """type 20：循环内设置变量。left 指向循环变量名(blockID=复合节点/__entry__)，right 为新值；写 ctx.loop_vars。
    left/right 均兼容编辑器结构化与 XML 简写（'__entry__.keywords' / 'ref:节点.字段'）。"""
    for p in node.get("data", {}).get("inputs", {}).get("inputParameters", []):
        var_name = _setvar_left_name(p.get("left"))
        if var_name is None:
            continue   # left 未设置：跳过该参数（不炸循环体）
        new_val = _setvar_right_value(p.get("right", p.get("input")), ctx)
        if getattr(ctx, "loop_vars", None) is not None:
            ctx.loop_vars[var_name] = new_val
    return {"outputs": {}, "port": None}


def _handle_loop(node: dict, ctx) -> dict:
    """type 21 循环：array(遍历 list)/count(固定次数)/infinite(直到 Break)。
    list 型 inputParameter 在每轮绑定为当前元素；variableParameters 为累加变量初值。"""
    inputs = node.get("data", {}).get("inputs", {})
    loop_type = inputs.get("loopType", "array")
    composite_id = str(node["id"])
    blocks_by_id = {str(b["id"]): b for b in node.get("blocks", [])}
    edges = node.get("edges", [])

    other_inputs, elements, elem_name = {}, None, None
    for p in inputs.get("inputParameters", []):
        val = resolve_value(p.get("input"), ctx)
        # 字面量JSON字符串→尝试解析为list
        if isinstance(val, str) and isinstance(p.get("input", {}).get("schema"), dict):
            try: val = json.loads(val)
            except (json.JSONDecodeError, TypeError): pass
        if loop_type == "array" and isinstance(val, list) and elements is None:
            elements, elem_name = val, p.get("name")
        else:
            other_inputs[p.get("name")] = val
    if loop_type == "array":
        items = elements or []
    elif loop_type == "count":
        try:
            items = [None] * int(resolve_value(inputs.get("loopCount"), ctx))
        except (TypeError, ValueError):
            items = []
    else:  # infinite
        items = [None] * _MAX_LOOP_ITERS

    loop_vars = {}
    for vp in inputs.get("variableParameters", []):
        loop_vars[vp.get("name")] = resolve_value(vp.get("input"), ctx)
    ctx.loop_vars = loop_vars

    outer = ctx.node_outputs
    saved_item, saved_idx = ctx.batch_item, ctx.batch_index
    last_exposed, last_body = {}, {}
    entry_id = _find_body_entry(edges, composite_id)   # 迭代入口节点（新模型）；None=旧格式
    round_items = []   # 编辑器约定：每轮最后一个产出值节点的输出 dict
    _track = getattr(ctx, "run_id", None) is not None   # 观测：子画布节点轨迹进轮容器
    _childmeta = {bid: {"title": _node_title(b), "ntype": str(b.get("type"))}
                  for bid, b in blocks_by_id.items()}
    for idx, elem in enumerate(items[:_MAX_LOOP_ITERS]):
        ctx.batch_item = elem
        ctx.batch_index = idx
        exposed = dict(other_inputs)
        if elem_name is not None:
            exposed[elem_name] = elem
        exposed["index"] = idx
        exposed.update(loop_vars)
        body_outputs = dict(outer)
        body_outputs[composite_id] = exposed
        if entry_id:
            # 迭代入口节点：暴露 item/index/其它输入/循环变量（与 batch 对齐——
            # other_inputs（复合节点输入如 tools）此前漏合并，ref="__entry__.xxx" 恒 None）
            entry_exp = {"index": idx}
            if elem_name is not None:
                entry_exp["item"] = elem
            entry_exp.update(other_inputs)
            entry_exp.update(loop_vars)
            body_outputs[entry_id] = entry_exp
        _round_children = []
        if _track:
            ctx.track_stack.append(_round_children)   # 本轮子画布节点事件写入轮容器
        try:
            signal, round_out = _run_composite_body(blocks_by_id, edges, composite_id, body_outputs, ctx)
        finally:
            if _track:
                ctx.track_stack.pop()
        # 观测：每轮尾部实时更新 node_meta（栈已恢复外层——children 挂到本复合节点 entry；
        # 运行中展开观测页即可看到最后一轮轨迹逐轮刷新）
        if _track:
            _track_dispatch(ctx, {"ev": "node_meta", "id": composite_id,
                                  "children": _round_children, "rounds": idx + 1, "childmeta": _childmeta})
        last_exposed, last_body = exposed, body_outputs
        # 本轮输出完全由 continue 节点 result 连线决定：仅 continue 信号产出（裸值）；
        # break/done 不产出（中断/旧格式无 continue 时本轮无 item）
        if signal == "continue":
            round_items.append(round_out)
            if ctx.on_round:   # 调试页逐轮刷新：每轮迭代完成即回调（不等整个循环跑完）
                try:
                    ctx.on_round(composite_id, idx, round_out)
                except Exception:
                    pass
        if signal == "break":
            # break 轮携带值时同样计入 all_outputs（await 语义：就绪轮的最终结果）；
            # 未带值（result 未连线）不占位——保持纯退出语义
            if round_out is not None:
                round_items.append(round_out)
                if ctx.on_round:
                    try:
                        ctx.on_round(composite_id, idx, round_out)
                    except Exception:
                        pass
            break

    decl = node.get("data", {}).get("outputs", [])
    conv_outs = [o for o in decl if not o.get("input")]   # 编辑器约定（all/filtered/nth）
    native_outs = [o for o in decl if o.get("input")]      # 原生（带 input 引用）
    outputs = {}
    # 编辑器约定输出：continue 捕获本轮输出 → all/filtered/nth
    if conv_outs:
        outputs.update(_collect_composite_outputs(conv_outs, round_items, inputs.get("batch")))
    # 循环变量终值【无条件并入】：'复合节点.变量名' 是一等输出引用面（__entry__ 的变量端口
    # 在编辑器本来就暴露）——此前只在声明了原生输出(native_outs)时才 merge，漏声明时
    # 下游 ref '循环.变量' 恒 None（extract_keywords 的 keywords 输出 null 的根因）。
    # setdefault：约定输出（all/filtered/nth）与显式原生输出优先，不覆盖。
    for _vk, _vv in loop_vars.items():
        outputs.setdefault(_vk, _vv)
    # 原生输出：用最后一轮 body + 累加终值解析
    if native_outs:
        merged = dict(last_body) if last_body else dict(outer)
        merged[composite_id] = {**(last_exposed or {}), **loop_vars}
        saved = ctx.node_outputs
        ctx.node_outputs = merged
        try:
            for o in native_outs:
                outputs[o.get("name")] = resolve_value(o.get("input"), ctx)
        finally:
            ctx.node_outputs = saved
    ctx.loop_vars = None
    ctx.batch_item, ctx.batch_index = saved_item, saved_idx
    return {"outputs": outputs, "port": None}


def _handle_batch(node: dict, ctx) -> dict:
    """type 28 批处理：对 list 每个元素跑子图，把声明为 list 的 body 输出聚合成列表。
    v1 顺序执行（concurrentSize 并发留待后续）。"""
    inputs = node.get("data", {}).get("inputs", {})
    composite_id = str(node["id"])
    blocks_by_id = {str(b["id"]): b for b in node.get("blocks", [])}
    edges = node.get("edges", [])

    elements, elem_name, other_inputs = [], None, {}
    for p in inputs.get("inputParameters", []):
        val = resolve_value(p.get("input"), ctx)
        # 字面量JSON字符串→尝试解析为list
        if isinstance(val, str) and isinstance(p.get("input", {}).get("schema"), dict):
            try: val = json.loads(val)
            except (json.JSONDecodeError, TypeError): pass
        if isinstance(val, list) and elements == []:
            elements, elem_name = val, p.get("name")
        else:
            other_inputs[p.get("name")] = val

    outer = ctx.node_outputs
    saved_item, saved_idx = ctx.batch_item, ctx.batch_index
    decl = node.get("data", {}).get("outputs", [])
    native = any(o.get("input") for o in decl)
    collected = {o.get("name"): [] for o in decl}
    last_body = {}
    entry_id = _find_body_entry(edges, composite_id)   # 迭代入口节点（新模型）；None=旧格式
    round_items = []   # 编辑器约定：每轮最后一个产出值节点的输出 dict
    _track = getattr(ctx, "run_id", None) is not None   # 观测：子画布节点轨迹进轮容器
    _last_round_children = None
    _rounds = 0
    for idx, elem in enumerate(elements[:_MAX_LOOP_ITERS]):
        ctx.batch_item = elem
        ctx.batch_index = idx
        exposed = dict(other_inputs)
        if elem_name is not None:
            exposed[elem_name] = elem
        exposed["index"] = idx
        body_outputs = dict(outer)
        body_outputs[composite_id] = exposed
        if entry_id:
            # 迭代入口节点：暴露 item/index/other_inputs（batch 总是 array 模式）
            entry_exp = {"item": elem, "index": idx}
            entry_exp.update(other_inputs)
            body_outputs[entry_id] = entry_exp
        _round_children = []
        if _track:
            ctx.track_stack.append(_round_children)
        try:
            signal, round_out = _run_composite_body(blocks_by_id, edges, composite_id, body_outputs, ctx)
        finally:
            if _track:
                ctx.track_stack.pop()
        _rounds = idx + 1
        _last_round_children = _round_children
        last_body = body_outputs
        # 本轮输出完全由 continue 节点 result 连线决定：仅 continue 信号产出（裸值）
        if signal == "continue":
            round_items.append(round_out)
            if ctx.on_round:   # 调试页逐轮刷新
                try:
                    ctx.on_round(composite_id, idx, round_out)
                except Exception:
                    pass
        if signal == "break":
            # 批处理体内 Break：停止迭代（此前 break 信号被静默忽略——继续跑完剩余元素）；
            # 携带值时同样计入约定输出
            if round_out is not None:
                round_items.append(round_out)
            break
        if native:
            saved = ctx.node_outputs
            ctx.node_outputs = body_outputs
            try:
                for o in decl:
                    if o.get("type") == "list" or (o.get("name") or "").endswith("_list"):
                        collected[o.get("name")].append(resolve_value(o.get("input"), ctx))
            finally:
                ctx.node_outputs = saved

    conv_outs = [o for o in decl if not o.get("input")]   # 编辑器约定（all/filtered/nth）
    outputs = {}
    if conv_outs:
        outputs.update(_collect_composite_outputs(conv_outs, round_items, inputs.get("batch")))
    # 原生输出（带 input 引用）：list/xxx_list 逐轮收集，其余从最后一轮解析
    for o in decl:
        if not o.get("input"):
            continue
        nm = o.get("name")
        if o.get("type") == "list" or (nm or "").endswith("_list"):
            outputs[nm] = collected.get(nm, [])
        else:
            saved = ctx.node_outputs
            ctx.node_outputs = last_body or outer
            try:
                outputs[nm] = resolve_value(o.get("input"), ctx)
            finally:
                ctx.node_outputs = saved
    ctx.batch_item, ctx.batch_index = saved_item, saved_idx
    # 观测：批处理节点的子画布轨迹（最后一轮 children + 轮数 + 子节点标题映射）
    if _track and _last_round_children is not None:
        _track_dispatch(ctx, {"ev": "node_meta", "id": composite_id,
                              "children": _last_round_children, "rounds": _rounds,
                              "childmeta": {bid: {"title": _node_title(b), "ntype": str(b.get("type"))}
                                            for bid, b in blocks_by_id.items()}})
    return {"outputs": outputs, "port": None}


# ----- 意图识别(22) / HTTP(45) / 子工作流(9) / 插件(4) -----

def _try_parse(s):
    """尝试把字符串解析成 Python 对象（dict/list/标量）；失败返回 None。
    先标准 JSON，再 Python 字面量（单引号 dict 风格，模型/工具常误输出这种）。
    返回 None 让调用方回退到原始字符串。"""
    if isinstance(s, str):
        # 标准 JSON
        try:
            return json.loads(s)
        except Exception:
            pass
        # Python repr 风格（单引号 dict/list）
        try:
            import ast
            return ast.literal_eval(s)
        except Exception:
            return None
    return s if isinstance(s, (dict, list)) else None


# http 节点 url/body 模板统一用 render_template 引用 inputParameters 变量（{{变量名}}）：
# 上游值通过 <in name="x" ref="节点.字段"> 桥接为变量 x，URL 写 {{x}}（与 text/llm 一致）。


def _find_local_workflow(ctx, wf_id: str):
    """按 workflowId 在 .agent/workflows/ 找本地工作流（匹配 meta.name 或文件名）。
    支持 .json 与 .xml（XML 读入时转 JSON）。"""
    d = ctx.workspace / ".agent" / "workflows"
    if not d.exists():
        return None
    # 收集候选：{path, stem, name}
    cands = []
    for jf in sorted(d.glob("*.json")):
        if jf.name.endswith(".meta"):
            continue
        name = _read_meta_name(jf, jf.stem)
        cands.append((jf, jf.stem, name))
    for xf in sorted(d.glob("*.xml")):
        if xf.name.endswith(".meta"):
            continue
        name = _read_meta_name(xf, xf.stem)
        cands.append((xf, xf.stem, name))
    for path, stem, name in cands:
        if wf_id in (stem, name):
            return _load_canvas(path)
    return None


def _read_meta_name(path: Path, default: str) -> str:
    """从 path.meta 或 XML 根属性读 name；失败返回 default。"""
    meta_p = path.with_name(path.name + ".meta")
    if meta_p.exists():
        try:
            return (json.loads(meta_p.read_text(encoding="utf-8")) or {}).get("name", default)
        except Exception:
            pass
    if path.suffix.lower() == ".xml":
        try:
            import xml.etree.ElementTree as ET
            root = ET.fromstring(path.read_text(encoding="utf-8"))
            return root.get("name") or default
        except Exception:
            pass
    return default


def _load_canvas(path: Path):
    """读 .json（直接）或 .xml（转 JSON）。"""
    try:
        text = path.read_text(encoding="utf-8")
        if path.suffix.lower() == ".xml":
            from workflow_xml import xml_to_canvas
            return xml_to_canvas(text)
        return json.loads(text)
    except Exception:
        return None



# （_handle_intent 已外置为节点插件：src/assets/nodes_builtin/）

# （_handle_http 已外置为节点插件：src/assets/nodes_builtin/）

def _handle_subworkflow(node: dict, ctx) -> dict:
    """type 9 子工作流：workflowId 按本地 .agent/workflows/<名> 匹配并执行（我们的约定：
    手写工作流时把 workflowId 写成目标工作流的 name/文件名）。"""
    inputs = node.get("data", {}).get("inputs", {})
    wf_id = str(inputs.get("workflowId", "")).strip()
    params = _resolve_input_params(inputs.get("inputParameters", []), ctx)
    canvas = _find_local_workflow(ctx, wf_id)
    if canvas is None:
        raise WorkflowError(f"子工作流未找到：{wf_id!r}（本地按 .agent/workflows/<名>.json 的 name/文件名匹配）")
    _track = getattr(ctx, "run_id", None) is not None   # 观测：子执行轨迹挂到本节点 entry
    _container = []
    if _track:
        # 嵌套观测容器：子 execute 的节点事件写本容器（不发 run_done——栈非空由 execute 判断）
        ctx.track_stack.append(_container)
    try:
        result = execute(canvas, params, tools=ctx.tools, llm=ctx.llm, emit=ctx.emit,
                         workspace=ctx.workspace, return_exit_dict=True,
                         run_id=ctx.run_id, track_stack=ctx.track_stack)
    finally:
        if _track:
            ctx.track_stack.pop()
    if _track:
        _track_dispatch(ctx, {"ev": "node_meta", "id": str(node["id"]),
                              "children": _container, "wf_name": wf_id,
                              "childmeta": {str(n.get("id")): {"title": _node_title(n), "ntype": str(n.get("type"))}
                                            for n in canvas.get("nodes", [])}})
    # 子工作流输出保留 end 字段结构：output=整个 end dict（可 .field 引用），字段同时平铺
    outputs = {"output": result, **(result if isinstance(result, dict) else {})}
    return {"outputs": outputs, "port": None}


def _handle_plugin(node: dict, ctx) -> dict:
    """type 4 插件/工具节点：按 toolName（或 apiName）匹配 Agent 工具箱中的工具并调用。
    输入参数取自 inputParameters；输出默认 raw（工具原始返回），若用户编辑了 outputs 字段，
    则尝试从 raw（先 JSON 解析，再支持 a.b 点号取值）按字段名解析填充。"""
    inputs = node.get("data", {}).get("inputs", {})
    tool_name = node.get("data", {}).get("toolName") or inputs.get("toolName")
    if not tool_name:
        for p in inputs.get("apiParam", []) or []:
            if p.get("name") == "apiName":
                tool_name = resolve_value(p.get("input"), ctx)
    args = _resolve_input_params(inputs.get("inputParameters", []), ctx)
    if not tool_name:
        raise WorkflowError("工具节点缺少 toolName")
    if ctx.tools is None:
        raise WorkflowError("工具节点需要工具上下文(tools)")
    # 优先 agent.tools，找不到再查内置轻量工具（workflow.py 内延迟导入防循环）
    actual_tools = ctx.tools
    if tool_name not in actual_tools:
        from real_tools import LIGHT_TOOLS
        if tool_name in LIGHT_TOOLS:
            actual_tools = LIGHT_TOOLS
        else:
            raise WorkflowError(f"工具 {tool_name!r} 未在工具箱中找到")
    # ReAct 原语三件套（llm_call/get_tool_schemas/call_tool）需要执行上下文的 llm/tools：
    # 调用前注入 real_tools._WF_CTX（保存/恢复，嵌套子工作流安全），其余工具不受影响
    if tool_name in ("llm_call", "get_tool_schemas", "call_tool"):
        import real_tools as _rt
        _saved = (_rt._WF_CTX.get("llm"), _rt._WF_CTX.get("tools"))
        _rt._WF_CTX.update(llm=ctx.llm, tools=ctx.tools)
        try:
            raw = actual_tools.call(tool_name, args)
        finally:
            _rt._WF_CTX.update(llm=_saved[0], tools=_saved[1])
    else:
        raw = actual_tools.call(tool_name, args)          # Tool.run 统一返回 str
    # raw 可能是 JSON/Python-repr 字符串（list/dict 等），尝试解析回 Python 对象；
    # 解析成功则 outputs 里存解析后的对象（下游代码节点引用时直接拿 list/dict）
    parsed = _try_parse(raw)
    outputs = {"raw": parsed if parsed is not None else raw}
    # 按用户声明的 outputs 字段填充
    declared = node.get("data", {}).get("outputs", []) or []
    for o in declared:
        nm = o.get("name")
        if not nm or nm == "raw":
            continue
        val = _extract_field(parsed if parsed is not None else raw, nm, o)
        # 纯文本返回的工具（如 web_search），JSON 解析失败拿不到字段名，
        # 若声明类型为 string 则直接兜底透传全文
        if val is None and o.get("type", "string") == "string":
            val = raw
        outputs[nm] = val
    return {"outputs": outputs, "port": None}


def _extract_field(data, name: str, var: dict):
    """从工具返回里抽取某字段：先直接取键，再点号路径，再按 description 提示取，失败返回 None。"""
    if isinstance(data, dict):
        if name in data:
            return data[name]
        if "." in name:
            v = _dotted_get(data, name)
            if v is not None:
                return v
        # 模糊：按 description 里写的键名
        desc = (var.get("description") or "").strip()
        if desc and desc in data:
            return data[desc]
    return None


def _handle_output_emitter(node: dict, ctx) -> dict:
    """type 13 输出消息：中途向用户输出一段内容（经 ctx.emit 推 workflow_message 事件）。"""
    inputs = node.get("data", {}).get("inputs", {})
    params = _resolve_input_params(inputs.get("inputParameters", []), ctx)
    text = render_template(str(resolve_value(inputs.get("content"), ctx)), params)
    if ctx.emit:
        try:
            ctx.emit({"type": "workflow_message", "text": text})
        except Exception:
            pass
    return {"outputs": {"output": text}, "port": None}


def _handle_input_receiver(node: dict, ctx) -> dict:
    """type 30 索取输入：需"暂停工作流等用户输入"，同步工具执行下做不到 → 明确报错。"""
    raise WorkflowError("InputReceiver(30) 需要交互式用户输入，工具模式下不支持（仅 chatflow 场景）")


# type → 处理器。Entry(1)/Exit(2) 在顶层调度器里特判；Break(19)/Continue(29) 在复合体调度里判类型；31=注释忽略。
# 子画布内可能有 type 1/2 作为视觉标记——通过处理（不报错）。
def _passthrough(node, ctx): return {"outputs": {}, "port": None}
NODE_HANDLERS = {
    # 核心（调度器协议，不容插件覆写）：start/end/loop/batch/loop-setvar/subworkflow/plugin/output
    "1": _passthrough,
    "2": _passthrough,
    "20": _handle_loop_setvar,
    "21": _handle_loop,
    "28": _handle_batch,
    "9": _handle_subworkflow,
    "4": _handle_plugin,
    "13": _handle_output_emitter,
    # 3/5/8/22/32/40/45/15/58/59 由节点插件注入（node_plugins.attach——三级目录同名 type 可覆盖）
}
# 节点插件装配（assets/nodes_builtin → nodes/ → .agent/nodes/；同 type 覆盖=定制机制）：
# 15/58/59（text/tojson/fromjson）已迁 src/assets/nodes_builtin/——首批插件化节点
try:
    from node_plugins import attach_node_plugins
    attach_node_plugins(NODE_HANDLERS)
except Exception as _np_e:   # 插件层故障不阻断引擎（内置 handler 缺失才会在执行时报节点类型未支持）
    import logging as _np_log
    _np_log.getLogger("agt.workflow").warning("节点插件装配失败：%s", _np_e)


# ========== 调度器 ==========

class _Ctx:
    """运行时上下文：各节点输出、全局变量、循环变量、workspace、以及 tools/llm 引用。"""
    def __init__(self, *, tools, llm, emit=None, workspace=None):
        self.node_outputs: dict[str, dict] = {}
        self.on_round = None   # 每轮迭代完成回调（调试页逐轮刷新）：fn(node_id, round_idx, outputs_snapshot)。None=无观察者
        self.global_vars: dict = {}
        self.loop_vars: dict | None = None   # 当前循环的累加变量（LoopSetVariable 读写）
        self.batch_item = None               # 当前批处理的 item（loop-item source 用）
        self.batch_index = None              # 当前批处理的 index（loop-index source 用）
        self.tools = tools
        self.llm = llm
        self.emit = emit
        self.workspace = workspace or WORKSPACE
        # 观测（run registry）：run_id 非空时节点事件写 _WF_RUNS（观测页轮询）；
        # track_stack 非空 = 嵌套执行（复合体迭代内/子工作流内）→ 事件写栈顶容器而非顶层 run
        self.run_id: str | None = None
        self.track_stack: list = []
        # 子画布节点输出追踪（debug 用）：record_sub=True 时 _run_composite_body 把每轮迭代的
        # body_outputs 存到 sub_trace[复合节点id]——debug 工具用 "复合id/子节点id" 语法读取。
        self.record_sub = False
        self.sub_trace: dict[str, dict] = {}


def _bind_entry(entry: dict, inputs: dict) -> dict:
    """开始节点：把外部入参按其 outputs 声明绑定（缺必填报错，有 defaultValue 回退）。"""
    bound = {}
    for var in entry.get("data", {}).get("outputs", []) or []:
        name = var.get("name")
        if name in inputs:
            bound[name] = inputs[name]
        elif "defaultValue" in var:
            bound[name] = var["defaultValue"]
        elif var.get("required"):
            raise WorkflowError(f"缺少必填工作流入参：{name}")
        else:
            bound[name] = None
    # 透传未声明但已传入的参数（宽松）
    for k, v in inputs.items():
        bound.setdefault(k, v)
    return bound


def _exit_result(node: dict, ctx) -> dict:
    """结束节点的结果 dict（结构化，不 stringify）。
    returnVariables → {字段名: 值}；useAnswerContent → {output: 渲染文本}。
    子工作流用此拿结构化输出（保持 end 字段结构）。"""
    inputs = node.get("data", {}).get("inputs", {})
    plan = inputs.get("terminatePlan", "returnVariables")
    if plan == "useAnswerContent":
        params = _resolve_input_params(inputs.get("inputParameters", []), ctx)
        text = resolve_value(inputs.get("content"), ctx)
        return {"output": render_template(str(text), params)}
    return {p.get("name"): resolve_value(p.get("input"), ctx)
            for p in inputs.get("inputParameters", [])}


def _handle_exit(node: dict, ctx) -> str:
    """结束节点：返回 _stringify_result（工具/wf_* 用，单键取值保持简洁）。"""
    return _stringify_result(_exit_result(node, ctx))


def _stringify_result(result) -> str:
    """工具返回字符串：单键 dict 取值，多键转 JSON。"""
    if isinstance(result, dict) and len(result) == 1:
        only = next(iter(result.values()))
        return str(only)
    if isinstance(result, (dict, list)):
        return json.dumps(result, ensure_ascii=False, indent=2)
    return str(result)


def _next_node(edges: list, node_id: str, port) -> str | None:
    """找 node_id 的后继：有 port 时匹配 sourcePortID，否则优先空端口、再取第一个。
    保留给复合节点（loop/batch）内部的线性子图调度用——主流程已改 DAG 拓扑调度。"""
    outs = [e for e in edges if str(e.get("sourceNodeID")) == node_id]
    if not outs:
        return None
    if port:
        for e in outs:
            if e.get("sourcePortID") == port:
                return str(e["targetNodeID"])
    for e in outs:                       # 优先无端口的线性边
        if not e.get("sourcePortID"):
            return str(e["targetNodeID"])
    return str(outs[0]["targetNodeID"])


# 聚合节点（OR 汇聚）：任一前驱完成即可继续，不等所有前驱
_AGGREGATOR_TYPES = {"32"}


def _build_dag(nodes: dict, edges: list) -> tuple[dict, dict]:
    """构建 DAG 调度所需的拓扑索引：
    - out_edges: sourceNodeID → [(targetNodeID, sourcePortID)] 扇出索引
    - pending_in: node_id → 未完成前驱数（aggregator 节点 OR 语义初值=1）
    entry 节点不算前驱（它由 _bind_entry 处理，视为已完成）。"""
    out_edges: dict[str, list] = {}
    in_count: dict[str, int] = {}
    for e in edges:
        src = str(e.get("sourceNodeID"))
        tid = str(e.get("targetNodeID"))
        # 悬空边（端点节点不存在，手写画布/编辑器删节点漏清理残留）跳过：
        # 照常计入度会让目标节点的 pending_in 永远减不到 0——分支静默卡死且无任何报错
        if src not in nodes or tid not in nodes:
            continue
        out_edges.setdefault(src, []).append((tid, e.get("sourcePortID", "")))
        in_count[tid] = in_count.get(tid, 0) + 1
    pending_in: dict[str, int] = {}
    for nid, node in nodes.items():
        ntype = str(node.get("type"))
        ic = in_count.get(nid, 0)
        # aggregator（type 32）OR 语义：任一前驱完成即可，初值=1
        # exit（type 2）终点：任一路径到达即结束，初值=1（多分支汇聚到 exit）
        if ntype in _AGGREGATOR_TYPES or ntype == "2":
            pending_in[nid] = 1 if ic > 0 else ic
        else:
            pending_in[nid] = ic
    # entry 视为已完成：其所有后继 pending_in -1
    for tid, _ in out_edges.get(ENTRY_ID, []):
        pending_in[tid] = pending_in.get(tid, 0) - 1
    return out_edges, pending_in


def _run_node_with_batch(node: dict, handler, ctx):
    """执行一个节点；若其 data.inputs.batch.enabled，则对数组逐元素执行，输出三组结果。
    返回与普通 handler 一致的 {outputs, port}。"""
    batch = (node.get("data", {}).get("inputs", {}) or {}).get("batch") or {}
    if not batch.get("enabled"):
        return handler(node, ctx)

    # 解析批处理数据源（array）
    arr = resolve_value(batch.get("input"), ctx)
    if isinstance(arr, str):
        try:
            arr = json.loads(arr)
        except json.JSONDecodeError:
            arr = [arr]
    if not isinstance(arr, list):
        arr = [arr] if arr is not None else []

    # 逐元素执行：注入 batch_item/batch_index，调用 handler
    # handler 应看到原始输出声明（all/filtered/nth 是批处理包装层产物，不能让 handler 去提取）
    _BATCH_OUT_NAMES = ("all_outputs", "filtered_outputs", "nth_output")
    data = node.get("data", {})
    saved_outputs = data.get("outputs")
    # nth_output 装填定义（fill）：迭代结果从单次输出的哪个字段提取（如 "raw" → output.raw）。
    # 留空 = 整个 output dict（默认，all_outputs=[{raw:x},...]）；fill 后元素即该字段值（[x,y,...]）
    _nth_decl = next((o for o in (saved_outputs or []) if o.get("name") == "nth_output"), None)
    fill_path = str((_nth_decl or {}).get("fill") or "").strip()
    # 对象组装模式：nth_output 的 schema 子字段带 fill 声明（如 name=output.raw / class=input.class /
    # no=loop.index）→ 每轮迭代结果为按声明组装的 object（至少一个子字段有 fill 才激活；字面量字段取 defaultValue）
    _assemble = None
    _nth_schema = (_nth_decl or {}).get("schema")
    if not fill_path and isinstance(_nth_schema, list):
        _fmap = [(s.get("name"), str(s.get("fill") or "").strip(), s.get("defaultValue"))
                 for s in _nth_schema if isinstance(s, dict) and s.get("name")]
        if any(src for _, src, _ in _fmap):
            _assemble = _fmap
    # 组装来源 input.xxx 需要【节点级输入的已解析值】（循环外一次；批处理数组源字段解析为整个数组——
    # 引用它没意义但也不炸）。标量 fill 用 input. 前缀时同样需要。
    _inputs_resolved = None
    _need_inputs = bool(_assemble) or (fill_path.startswith("input.") if fill_path else False)
    if _need_inputs:
        _inputs_resolved = {}
        for p in (node.get("data", {}).get("inputs", {}) or {}).get("inputParameters", []) or []:
            _pn = p.get("name")
            if _pn:
                try:
                    _inputs_resolved[_pn] = resolve_value(p.get("input"), ctx)
                except Exception:
                    _inputs_resolved[_pn] = None

    def _fill_one(out, idx, item):
        """单轮迭代结果的装填：标量 fill 路径 > 对象组装 > 整个 output dict。
        标量 fill 的路径前缀与对象组装统一：output.xxx（或裸路径=output 相对）/ input.xxx /
        loop.index / loop.item（裸路径保持旧语义——"raw" ≡ "output.raw"，向后兼容）。"""
        if fill_path:
            if fill_path == "loop.index":
                return idx
            if fill_path == "loop.item":
                return item
            if fill_path.startswith("input."):
                return (_inputs_resolved or {}).get(fill_path[6:])
            return _dotted_get(out, fill_path[7:] if fill_path.startswith("output.") else fill_path)
        if _assemble:
            obj = {}
            for fname, src, dv in _assemble:
                if not src:
                    obj[fname] = dv            # 字面量（defaultValue；未设为 None）
                elif src.startswith("output."):
                    obj[fname] = _dotted_get(out, src[7:])
                elif src.startswith("input."):
                    obj[fname] = (_inputs_resolved or {}).get(src[6:])
                elif src == "loop.index":
                    obj[fname] = idx
                elif src == "loop.item":
                    obj[fname] = item
                else:
                    obj[fname] = _dotted_get(out, src)   # 兼容裸路径（视作 output 相对）
            return obj
        return out

    if saved_outputs:
        data["outputs"] = [o for o in saved_outputs if o.get("name") not in _BATCH_OUT_NAMES]
    all_outputs = []
    saved_item, saved_idx = ctx.batch_item, ctx.batch_index
    _nid = str(node.get("id", ""))
    try:
        for idx, item in enumerate(arr[:_MAX_LOOP_ITERS]):
            ctx.batch_item = item
            ctx.batch_index = idx
            try:
                r = handler(node, ctx)
                out = r.get("outputs") or {}
                all_outputs.append(_fill_one(out, idx, item))
            except WorkflowError as e:
                all_outputs.append(None if (fill_path or _assemble) else {"_error": str(e)})  # 单次失败不中断（装填模式 None 会被过滤）
            except Exception as e:
                all_outputs.append(None if (fill_path or _assemble) else {"_error": f"{type(e).__name__}: {e}"})
            if ctx.on_round:   # 调试页逐轮刷新：节点级批处理每轮迭代完成即回调
                try:
                    ctx.on_round(_nid, idx, all_outputs[-1])
                except Exception:
                    pass
    finally:
        if saved_outputs is not None:
            data["outputs"] = saved_outputs
        ctx.batch_item, ctx.batch_index = saved_item, saved_idx

    # 组2：非 null/空 且满足 filter 条件
    filt = batch.get("filter")
    filtered = []
    for out in all_outputs:
        if fill_path:
            if out is None or out == "":
                continue
        else:
            if not out or (len(out) == 1 and "_error" in out):
                continue
            if _is_null_output(out):
                continue
        if filt and not _eval_batch_filter(filt, out):
            continue
        filtered.append(out)

    # 组3：filtered 的第 nth 个
    nth = batch.get("nth", 0)
    try:
        nth = int(nth)
    except (TypeError, ValueError):
        nth = 0
    if not filtered:
        nth_output = None
    elif nth < 0 or nth >= len(filtered):
        nth_output = filtered[-1]
    else:
        nth_output = filtered[nth]

    return {"outputs": {
        "all_outputs": all_outputs,
        "filtered_outputs": filtered,
        "nth_output": nth_output,
    }, "port": None}


def _is_null_output(out) -> bool:
    """判断单次输出是否算 null（全空值）。
    复合节点 continue-result 解包后元素是裸值（str/list/object-dict/number/None）；
    单节点批处理 _run_node_with_batch 仍产出 dict（{raw:x, ...}）。两种形态都要能判。"""
    if isinstance(out, dict):
        vals = [v for k, v in out.items() if k != "_error"]
        if not vals:
            return True
        return all(v is None or v == "" or v == [] or v == {} for v in vals)
    # 裸值（str/list/number/None/bool）
    return out is None or out == "" or out == [] or out == {}


# 不需要右值的运算符（为空/非空/True/False）——右侧残留的未设置值不应影响判断
_RIGHTLESS_OPS = {9, 10, 11, 12}


def _filter_input_unset(block_input) -> bool:
    """筛选条件的左/右值是否处于「未设置」状态：input 缺失 / 空 ref（blockID 与 name 均空，
    即编辑器下拉的「左值…」「右值…」占位未选）/ 空 literal。
    半成品条件不拦截数据——_eval_batch_filter 对未设置侧按恒真处理。"""
    if not isinstance(block_input, dict):
        return True
    val = block_input.get("value")
    if not isinstance(val, dict):
        return True
    vt = val.get("type")
    if vt == "ref":
        c = val.get("content") or {}
        if c.get("source") in ("loop-item", "loop-index"):
            return False                      # loop 引用恒有意义
        return not (str(c.get("blockID", "")).strip() or str(c.get("name", "")).strip())
    if vt == "literal":
        return val.get("content") in (None, "")
    return False


def _eval_batch_filter(condition: dict, output: dict) -> bool:
    """批处理筛选：复用 Selector 的 condition 结构，left 引用本次输出字段。
    left 的 ref 用特殊 blockID='__batch_output__' 指向本次 output。
    左/右值未设置（编辑器占位未选）→ 该条件恒真——半成品条件不拦截数据。"""
    class _Proxy:
        node_outputs = {"__batch_output__": output}
        global_vars = {}
    # 临时让 _eval_condition / _cmp 的 left ref 能解析到本次 output
    conds = condition.get("conditions", [])
    logic = condition.get("logic", 2)
    results = []
    for c in conds:
        left_input = (c.get("left") or {}).get("input")
        op = c.get("operator")
        # 右值仅在"用到右值的运算符"（1-8/13-16）下参与未设置判定——9-12（为空/非空/True/False）
        # 不需要右值，编辑器隐藏右值控件后残留的空值不应让条件恒真跳过左值检查
        has_right = "right" in c and op not in _RIGHTLESS_OPS
        right_input = (c.get("right") or {}).get("input") if has_right else None
        if _filter_input_unset(left_input) or (has_right and _filter_input_unset(right_input)):
            results.append(True)
            continue
        # 把 left 的 ref blockID 重定向到本次 output
        l = _redirect_ref(left_input, output)
        r = _resolve_filter_value(right_input, output)
        results.append(_cmp(op, l, r))
    if not results:
        return True
    return any(results) if logic == 1 else all(results)


def _redirect_ref(block_input, output):
    """若 block_input 是 ref(block-output)，重定向到本次 batch output；否则解析字面量。"""
    if block_input is None:
        return None
    val = block_input.get("value", block_input) if isinstance(block_input, dict) else None
    if isinstance(val, dict) and val.get("type") == "ref":
        name = (val.get("content") or {}).get("name", "")
        return _dotted_get(output, name)
    if isinstance(val, dict) and val.get("type") == "literal":
        return val.get("content")
    return None


def _resolve_filter_value(block_input, output):
    if block_input is None:
        return None
    return _redirect_ref(block_input, output)


def _node_title(n: dict) -> str:
    """节点标题（观测页显示用）。"""
    return ((n.get("data", {}).get("nodeMeta", {}) or {}).get("title") or f"节点{n.get('id')}")


def execute(canvas: dict, inputs: dict, *, tools, llm, emit=None, workspace=None, max_steps: int = 1000, return_exit_dict: bool = False, run_id: str = None, track_stack: list = None):
    """执行一个 Coze 画布，返回结束节点的输出（字符串）。
    run_id：传 new_wf_run() 的 id 时，节点执行事件写入 _WF_RUNS（观测页 /wf/monitor 实时轮询）。
    track_stack：嵌套观测容器栈（子工作流执行时由 _handle_subworkflow 传入——子节点事件写栈顶
    容器而非顶层 run；栈非空时本 execute 不发 run_done，整体结束态归最外层）。"""
    ctx = _Ctx(tools=tools, llm=llm, emit=emit, workspace=workspace)
    ctx.run_id = run_id
    ctx.track_stack = list(track_stack or [])
    _nested = bool(ctx.track_stack)
    nodes = {str(n["id"]): n for n in canvas.get("nodes", [])}
    edges = canvas.get("edges", [])

    entry = nodes.get(ENTRY_ID)
    if entry is None:
        raise WorkflowError("画布缺少开始节点（id=100001, type=1）")
    ctx.node_outputs[ENTRY_ID] = _bind_entry(entry, inputs or {})
    if run_id:
        _track_dispatch(ctx, {"ev": "node_start", "id": ENTRY_ID, "title": _node_title(entry),
                              "ntype": str(entry.get("type"))})
        _track_dispatch(ctx, {"ev": "node_end", "id": ENTRY_ID, "preview": _preview_str(ctx.node_outputs[ENTRY_ID]),
                              "full": _full_str(ctx.node_outputs[ENTRY_ID])})

    # —— DAG 拓扑调度（扇出 + 汇聚 + 端口分支）——
    from collections import deque
    out_edges, pending_in = _build_dag(nodes, edges)
    # 初始就绪：所有 pending_in<=0 的节点（entry 后继 + 无入边孤立节点，ComfyUI 风格）。
    # 孤立 end（type=2 且非 entry 后继——手写画布可能当视觉标记）排除；entry 的直接后继
    # （含 start→end 直连的 exit）不排除——此前无条件排除 type 2，start→end 直连的
    # 子工作流 exit 永远不进 ready → 隐式结束返回 {}（execute_debug 无此排除故一直正常）
    _entry_succs = {tid for tid, _ in out_edges.get(ENTRY_ID, [])}
    ready = deque(nid for nid in nodes if nid != ENTRY_ID and pending_in.get(nid, 0) <= 0
                  and not (str(nodes[nid].get("type")) == "2" and nid not in _entry_succs))
    executed: set[str] = set()

    try:
        for _ in range(max_steps):
            if not ready:
                break   # 所有路径走完
            current = ready.popleft()
            if current in executed:
                continue   # 防重复
            executed.add(current)
            node = nodes.get(current)
            # 到达结束节点即返回：EXIT_ID(900001) 之外的多个 Exit 同样生效（多出口画布的
            # 常规模式：不同分支各挂一个 end，各自 ref 不同来源——单 id 特判会漏掉第二个
            # 之后的 end，落入"隐式结束"返回上游节点输出）
            if current == EXIT_ID or (node is not None and str(node.get("type")) == "2"):
                raw = _exit_result(node if node is not None else nodes[EXIT_ID], ctx)
                if run_id:
                    _track_dispatch(ctx, {"ev": "node_start", "id": current,
                                          "title": _node_title(node), "ntype": "2"})
                    _track_dispatch(ctx, {"ev": "node_end", "id": current, "preview": _preview_str(raw),
                                          "full": _full_str(raw)})
                return raw if return_exit_dict else _stringify_result(raw)
            if node is None:
                raise WorkflowError(f"节点 {current} 不存在")
            ntype = str(node.get("type"))
            handler = NODE_HANDLERS.get(ntype)
            if handler is None:
                raise WorkflowError(f"未支持的节点类型 {ntype}（节点 {current}）")
            if run_id:
                _track_dispatch(ctx, {"ev": "node_start", "id": current,
                                      "title": _node_title(node), "ntype": ntype})
            try:
                result = _run_node_with_batch(node, handler, ctx)
                ctx.node_outputs[current] = result.get("outputs") or {}
                port = result.get("port")   # selector/intent 分支端口
                if run_id:
                    _track_dispatch(ctx, {"ev": "node_end", "id": current,
                                          "preview": _preview_str(ctx.node_outputs[current]),
                                          "full": _full_str(ctx.node_outputs[current])})
            except Exception as e:
                # 节点报错：默认 error 输出端口（{node_id}_error），工作流可从此端口拉边做错误处理
                # 未声明 error 边时该分支静默终止（不阻塞并行分支），整个工作流不崩
                ctx.node_outputs[current] = {"_error": f"{type(e).__name__}: {e}"}
                port = f"{current}_error"   # 每个节点默认 error 端口名
                if run_id:
                    _track_dispatch(ctx, {"ev": "node_error", "id": current,
                                          "preview": _preview_str(ctx.node_outputs[current]),
                                          "full": _full_str(ctx.node_outputs[current])})

            # 扇出 + port 匹配：遍历当前节点的所有出边
            for tid, src_port in out_edges.get(current, []):
                # 有 port 时严格匹配：只激活 src_port==port 的边（error/"true"/"false"/"branch_0"）
                # error 端口兼容两种写法：{node_id}_error 或统一 "error"
                if port:
                    if src_port != port and not (port.endswith("_error") and src_port == "error"):
                        continue
                elif src_port and (src_port == "error" or str(src_port).endswith("_error")):
                    # 成功（port=None）：错误处理边不激活——
                    # 此前无条件放行所有出边，画了 error 边的节点一成功就把 end（OR 语义）拉进
                    # 就绪队列，整条工作流被提前终止（before_turn_retrieval 7 节点早退的根因）
                    continue
                pending_in[tid] -= 1
                if pending_in[tid] <= 0 and tid not in executed:
                    ready.append(tid)
        # 循环结束：走完所有路径但无 exit（隐式结束）→ 返回最后一个执行的节点输出
        if executed:
            last = next((nid for nid in reversed(list(executed)) if nid != EXIT_ID), None)
            if last and last in ctx.node_outputs:
                return (ctx.node_outputs[last] if return_exit_dict
                        else _stringify_result(ctx.node_outputs[last]))
        return {} if return_exit_dict else _stringify_result({})
    finally:
        if run_id and not _nested:
            # 整体结束态：正常返回=done；异常抛出=failed（finally 里判断——except 重抛前记录）。
            # 嵌套执行（track_stack 非空）不发——整体结束态归最外层 execute
            _run_track(run_id, {"ev": "run_done"})


def execute_debug(canvas: dict, inputs: dict, *, tools, llm, on_node,
                  emit=None, workspace=None, max_steps: int = 1000):
    """调试执行：和 execute() 一样同步单路径跑完，但每个节点执行【前后】回调 on_node(event)，
    供调试页实时点亮节点、查看每步输出。

    event 形如：
      {"phase":"start","id":"100001","title":"开始","ntype":"1"}
      {"phase":"end",  "id":"100001","outputs":{...}}
    entry(100001)/exit(900001) 在调度器里特判（不走 NODE_HANDLERS），这里手动补发事件。
    复合节点(loop/batch/子工作流)只在【外层节点】触发 start/end——内部每轮细节不展开（v1 边界）。

    返回 (exit_dict, order, trace)：
      exit_dict = exit 节点的结构化结果（_exit_result 原始 dict）
      order     = 实际执行过的节点 id 顺序（含 entry/exit）
      trace     = {节点id: outputs} 全量输出快照
    on_node 抛异常会被吞掉（前端断了不应中断工作流）。
    """
    def _title(n: dict) -> str:
        return ((n.get("data", {}).get("nodeMeta", {}) or {}).get("title")
                or f"节点{n.get('id')}")

    def _safe_emit(ev: dict):
        try:
            on_node(ev)
        except Exception:
            pass

    ctx = _Ctx(tools=tools, llm=llm, emit=emit, workspace=workspace)
    ctx.record_sub = True   # debug 场景：记录复合节点子画布最后一轮输出（list_workflow_outputs('comp/sub') 可读）
    # 逐轮刷新：loop/batch 复合节点与节点级批处理每轮迭代完成即发 round 事件（调试页白框实时增长）
    ctx.on_round = lambda nid, ridx, outs: _safe_emit(
        {"phase": "round", "id": nid, "round": ridx, "outputs": json.loads(json.dumps(outs, ensure_ascii=False, default=str))
         if isinstance(outs, (dict, list)) else outs})
    nodes = {str(n["id"]): n for n in canvas.get("nodes", [])}
    edges = canvas.get("edges", [])
    # 缓存 ctx + 画布到模块级（供 server hotswap/rerun/list_outputs）
    _debug_ctx.update({"canvas": canvas, "nodes": nodes, "edges": edges, "ctx": ctx})
    order: list[str] = []
    trace: dict[str, dict] = {}

    entry = nodes.get(ENTRY_ID)
    if entry is None:
        raise WorkflowError("画布缺少开始节点（id=100001, type=1）")

    # —— 开始节点：手动补发事件，outputs = 绑定后的入参值 ——
    _safe_emit({"phase": "start", "id": ENTRY_ID, "title": _title(entry), "ntype": str(entry.get("type"))})
    bound = _bind_entry(entry, inputs or {})
    ctx.node_outputs[ENTRY_ID] = bound
    order.append(ENTRY_ID)
    trace[ENTRY_ID] = bound
    _safe_emit({"phase": "end", "id": ENTRY_ID, "outputs": bound})

    # —— DAG 拓扑调度（扇出 + 汇聚 + 端口分支），和 execute() 一致但带 on_node 回调 ——
    from collections import deque
    out_edges, pending_in = _build_dag(nodes, edges)
    ready = deque(nid for nid in nodes if nid != ENTRY_ID and pending_in.get(nid, 0) <= 0)
    executed: set[str] = set()

    for _ in range(max_steps):
        if not ready:
            break
        current = ready.popleft()
        if current in executed:
            continue
        executed.add(current)
        # —— 结束节点：手动补发事件，outputs = 结构化最终结果 ——
        if current == EXIT_ID:
            exit_node = nodes[EXIT_ID]
            exit_dict = _exit_result(exit_node, ctx)
            _safe_emit({"phase": "start", "id": EXIT_ID, "title": _title(exit_node), "ntype": str(exit_node.get("type"))})
            order.append(EXIT_ID)
            trace[EXIT_ID] = exit_dict
            _safe_emit({"phase": "end", "id": EXIT_ID, "outputs": exit_dict})
            return (exit_dict, order, trace)
        node = nodes.get(current)
        if node is None:
            raise WorkflowError(f"节点 {current} 不存在")
        ntype = str(node.get("type"))
        handler = NODE_HANDLERS.get(ntype)
        if handler is None:
            raise WorkflowError(f"未支持的节点类型 {ntype}（节点 {current}）")
        _safe_emit({"phase": "start", "id": current, "title": _title(node), "ntype": ntype})
        try:
            result = _run_node_with_batch(node, handler, ctx)
            outs = result.get("outputs") or {}
        except Exception as e:
            # 节点报错：默认 error 输出端口（{node_id}_error），工作流可从此端口拉边做错误处理
            outs = {"_error": f"{type(e).__name__}: {e}"}
            result = {"outputs": outs, "port": f"{current}_error"}
        ctx.node_outputs[current] = outs
        order.append(current)
        trace[current] = outs
        _safe_emit({"phase": "end", "id": current, "outputs": outs})
        port = result.get("port")

        for tid, src_port in out_edges.get(current, []):
            # 有 port 时严格匹配；error 端口兼容 {node_id}_error 和统一 "error" 两种写法
            if port:
                if src_port != port and not (port.endswith("_error") and src_port == "error"):
                    continue
            elif src_port and (src_port == "error" or str(src_port).endswith("_error")):
                # 成功（port=None）：错误处理边不激活（与 execute() 同款修复）
                continue
            pending_in[tid] -= 1
            if pending_in[tid] <= 0 and tid not in executed:
                ready.append(tid)
    # 走完所有路径但无 exit（隐式结束）
    last = next((nid for nid in reversed(list(executed)) if nid != EXIT_ID), None)
    last_outs = ctx.node_outputs.get(last, {}) if last else {}
    return (last_outs, order, trace)


# ========== 用户工具：.agent/workflows/tools/*.py 自动注册 ==========
# 把用户写在 tools/ 下的 Python 脚本里的【顶层函数】注册成工具，
# 供工作流插件节点(toolName=函数名)调用，也可被主 Agent 直接调用。
# 这让"写个 py 工具脚本供工作流用"从误解变成正确用法。
_LOADED_USER_TOOLS: set[str] = set()   # 记录已注册的用户工具名（每轮刷新前清理）


# schema 类型字符串 → Python 类型（INPUT_SCHEMA 里 "object"/"array" 等映射）
_SCHEMA_TYPE_MAP = {
    "string": str, "str": str, "integer": int, "int": int, "long": int,
    "number": float, "float": float, "double": float,
    "boolean": bool, "bool": bool, "object": dict, "dict": dict,
    "array": list, "list": list, "file": str, "time": str,
}


def _make_tool_from_func(func, input_schema: dict = None, output_schema: list = None) -> Tool | None:
    """把一个普通函数包成 Tool。参数类型来源优先级：模块 INPUT_SCHEMA > 函数注解 > str 兜底。
    output_schema（模块 OUTPUT_SCHEMA）若有，挂到 Tool.user_outputs 供编辑器/前端覆盖推断。
    无 docstring 或类型不可识别则返回 None（跳过）。"""
    try:
        hints = dict(getattr(func, "__annotations__", {}) or {})
        sig = inspect.signature(func)
    except (TypeError, ValueError):
        return None
    # INPUT_SCHEMA 覆盖：参数名 → 类型（object/array 等得以正确识别，不再误判 string）
    if isinstance(input_schema, dict):
        for p in sig.parameters:
            if p in input_schema:
                t = _SCHEMA_TYPE_MAP.get(str(input_schema[p]).strip().lower())
                if t is not None:
                    hints[p] = t
    need_fix = [p for p in sig.parameters if p not in hints]
    if need_fix:
        # 仍无类型的参数补 str。
        hints = {**hints, **{p: str for p in need_fix}}
    # 无条件写回 func.__annotations__（INPUT_SCHEMA 覆盖 / 补 str 都要让 Tool 看到）。
    # 直接赋给运行时函数对象（每次刷新重新 import，不影响磁盘 py）。
    try:
        func.__annotations__ = hints
    except Exception:
        return None
    try:
        t = Tool(func)
    except Exception:
        return None
    if isinstance(output_schema, list) and output_schema:
        t.user_outputs = output_schema   # 编辑器/api 优先用它作为 outputs
    return t


def load_user_tools(workspace: Path = None) -> tuple[list[Tool], list[tuple[str, str]]]:
    """扫描 .agent/workflows/tools/*.py，把每个文件里【本模块定义】的顶层函数注册成工具。
    跳过私有(_开头)、main、以及 import 进来的函数。

    支持模块级类型声明（解决 object/array 参数被误判 string）：
      INPUT_SCHEMA  = {"参数名": "object|array|integer|...", ...}   # 参数名→类型
      OUTPUT_SCHEMA = [{"name":"字段","type":"object","description":"..."}, ...]
    有则优先于注解；都没有的参数回退 str。返回 (tools, [(文件, 载入错误)])。"""
    d = (workspace or WORKSPACE) / ".agent" / "workflows" / "tools"
    if not d.exists():
        return [], []
    out, errors = [], []
    for py in sorted(d.glob("*.py")):
        mod_name = f"_wf_user_tools_{py.stem}"
        try:
            spec = importlib.util.spec_from_file_location(mod_name, py)
            if spec is None or spec.loader is None:
                continue
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
        except Exception as e:
            errors.append((py.name, f"{type(e).__name__}: {e}"))
            continue
        input_schema = getattr(mod, "INPUT_SCHEMA", None)
        output_schema = getattr(mod, "OUTPUT_SCHEMA", None)
        for nm, obj in inspect.getmembers(mod, inspect.isfunction):
            if nm.startswith("_") or nm == "main":
                continue
            if getattr(obj, "__module__", "") != mod_name:
                continue  # 只收本模块定义的（过滤 import 进来的 json.loads 等）
            t = _make_tool_from_func(obj, input_schema, output_schema)
            if t is not None:
                out.append(t)
    return out, errors




def _find_node(canvas: dict, node_id: str):
    for n in canvas.get("nodes", []):
        if str(n.get("id")) == node_id:
            return n
    return None


def _entry_input_schema(canvas: dict) -> list:
    """工作流入参 = 开始节点(100001)的 data.outputs。"""
    entry = _find_node(canvas, ENTRY_ID)
    if not entry:
        return []
    return entry.get("data", {}).get("outputs", []) or []


def _validate_canvas(canvas: dict) -> None:
    """轻量校验：必须有开始节点。"""
    if not isinstance(canvas, dict) or "nodes" not in canvas:
        raise WorkflowError("不是合法画布（缺 nodes 字段）")
    if _find_node(canvas, ENTRY_ID) is None:
        raise WorkflowError("缺少开始节点（id=100001）")


def _safe_name(name: str) -> str:
    s = re.sub(r"[^A-Za-z0-9_]", "_", name or "").strip("_")
    return s or "workflow"


def make_workflow_tool(meta: dict, canvas: dict, path: Path, agent) -> Tool:
    """把一个工作流封装成 Tool：入参来自开始节点 outputs（或 meta.inputs 覆盖），
    描述取自 meta.description。调用时执行画布。"""
    name = WF_PREFIX + _safe_name(meta.get("name") or path.stem)
    desc = (meta.get("description") or f"工作流：{meta.get('name')}").strip()
    schema = meta.get("inputs") or _entry_input_schema(canvas)

    params = []
    for spec in schema:
        pname = spec.get("name")
        if not pname:
            continue
        ptype = _TYPE_MAP.get(spec.get("type", "string"), str)
        if "defaultValue" in spec:
            default = spec["defaultValue"]
        elif spec.get("required"):
            default = inspect.Parameter.empty
        else:
            default = None
        params.append(inspect.Parameter(pname, inspect.Parameter.KEYWORD_ONLY,
                                        default=default, annotation=ptype))

    def _run(**kwargs):
        try:
            # 工作流 LLM/意图节点默认走统一辅助模型（utility_model 未配=主模型）；
            # agent=None（测试/独立注册场景）时 llm 传 None——纯工具型工作流（diff_snapshots 等）可跑
            if agent is not None and getattr(agent, "utility_client", None):
                _llm = agent.utility_client()
            else:
                _llm = getattr(agent, "llm", None) if agent is not None else None
            # 观测注册：Agent 场景注册 run（观测页可实时看节点轨迹）；agent=None（测试）不注册
            rid = new_wf_run(name, "tool", canvas=canvas) if agent is not None else None
            if rid and hasattr(agent, "_emit"):
                try:
                    agent._emit({"type": "auto_wf_start", "name": name, "hook": "tool",
                                 "run_id": rid, "text": str(kwargs)[:80]})
                except Exception:
                    pass
            ret = execute(canvas, kwargs, tools=agent.tools if agent is not None else Toolbox(),
                          llm=_llm, run_id=rid,
                          workspace=WORKSPACE, emit=getattr(agent, "_emit", None))
            if rid and hasattr(agent, "_emit"):
                try:
                    agent._emit({"type": "auto_wf", "name": name, "hook": "tool",
                                 "run_id": rid, "text": str(ret)[:120]})
                except Exception:
                    pass
            return ret
        except WorkflowError as e:
            return f"[工作流 {name} 执行失败] {e}"
        except Exception as e:  # 任何意外都转文本，不炸 Agent 主循环
            return f"[工作流 {name} 出错] {type(e).__name__}: {e}"

    _run.__signature__ = inspect.Signature(params)
    _run.__annotations__ = {p.name: p.annotation for p in params}
    _run.__name__ = name
    _run.__doc__ = desc
    return Tool(_run)


def scan_workflows(workspace: Path = None) -> list[dict]:
    """扫描 .agent/workflows/ 下 *.json 与 *.xml，返回 [{name, path, meta_path, meta, canvas, error}]。
    .xml（模型友好格式，代码块用 CDATA 免转义）在扫描时转成 Coze JSON canvas。"""
    d = (workspace or WORKSPACE) / ".agent" / "workflows"
    if not d.exists():
        return []
    out = []
    # JSON 工作流
    for jf in sorted(d.glob("*.json")):
        if jf.name.endswith(".meta"):
            continue
        meta_path = jf.with_name(jf.name + ".meta")
        item = {"name": jf.stem, "path": jf, "meta_path": meta_path,
                "meta": None, "canvas": None, "error": None, "warnings": []}
        try:
            item["canvas"] = json.loads(jf.read_text(encoding="utf-8"))
        except Exception as e:
            item["error"] = f"画布 JSON 解析失败：{e}"
            out.append(item)
            continue
        meta = {}
        if meta_path.exists():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8")) or {}
            except Exception:
                meta = {}
        meta.setdefault("name", jf.stem)
        item["meta"] = meta
        try:
            _validate_canvas(item["canvas"])
            item["warnings"] = validate_canvas_detailed(item["canvas"])
        except WorkflowError as e:
            item["error"] = str(e)
        out.append(item)
    # XML 工作流（转 JSON；meta 从根属性读，可被 .xml.meta 覆盖）
    out.extend(_scan_xml_workflows(d))
    return out


def _scan_xml_workflows(d: Path) -> list[dict]:
    """扫描 *.xml（排除 .meta），转成 Coze JSON canvas。meta 优先根属性，.xml.meta 可覆盖。"""
    import xml.etree.ElementTree as ET
    from workflow_xml import xml_to_canvas, WorkflowXmlError
    out = []
    for xf in sorted(d.glob("*.xml")):
        if xf.name.endswith(".meta"):
            continue
        meta_path = xf.with_name(xf.name + ".meta")
        item = {"name": xf.stem, "path": xf, "meta_path": meta_path,
                "meta": None, "canvas": None, "error": None, "warnings": []}
        try:
            xml_text = xf.read_text(encoding="utf-8")
            root = ET.fromstring(xml_text)
            meta = {"name": root.get("name") or xf.stem,
                    "description": root.get("description", ""),
                    "coze_url": root.get("coze_url", ""),
                    "enabled": root.get("enabled", "true") != "false"}
            if root.get("auto"):
                meta["auto"] = root.get("auto") == "true"
            if root.get("auto_param"):
                meta["auto_param"] = root.get("auto_param")
            if root.get("hook"):
                meta["hook"] = root.get("hook")
            # hidden 默认 true（未设置=不注册成 Agent 工具）：只有显式 hidden="false" 才注册为 wf_* 工具。
            # 大多数工作流是钩子/子工作流/demo——进工具箱反而是 schema 噪声；想给 Agent 用的显式取消勾选。
            meta["hidden"] = root.get("hidden", "true") != "false"
            if root.get("async") is not None:
                meta["async"] = root.get("async") == "true"
            if root.get("recap") is not None:
                meta["recap"] = root.get("recap") == "true"   # recap 工作流：结果写回 agent._recap（队友可见）
            if meta_path.exists():
                try:
                    meta = {**meta, **(json.loads(meta_path.read_text(encoding="utf-8")) or {})}
                except Exception:
                    pass
            item["meta"] = meta
            item["canvas"] = xml_to_canvas(xml_text)
            item["warnings"] = validate_canvas_detailed(item["canvas"])
        except (WorkflowXmlError, ET.ParseError) as e:
            item["error"] = f"XML 解析失败：{e}"
        except Exception as e:
            item["error"] = f"{type(e).__name__}: {e}"
        out.append(item)
    return out


# 执行器支持的所有节点 type（含调度器特判的 entry/exit/break/continue/注释）
_SUPPORTED_TYPES = set(NODE_HANDLERS.keys()) | {"1", "2", "19", "29", "31", "yield"}


def validate_canvas_detailed(canvas: dict) -> list[str]:
    """不执行地扫描画布（含复合节点 blocks），报告未支持的节点类型。返回问题字符串列表。
    含 LLM 节点 model 校验（2026-08-31·间歇性 local-qwen 排查）：llmParam 声明的模型名
    不在 MODELS 时告警——把「执行时才爆」的模型路由问题提前到「每次扫描」可见
    （workflows_info 的 warn 状态——观测网从执行时扩大到扫描时）。"""
    issues = []
    try:
        import config as _cfg
        _known = set(getattr(_cfg, "MODELS", {}) or {})
    except Exception:
        _known = None
    def _walk(nodes):
        for n in nodes or []:
            t = str(n.get("type"))
            if t not in _SUPPORTED_TYPES:
                issues.append(f"节点 {n.get('id')} 类型 {t} 暂未支持")
            if t == "3" and _known is not None:
                for p in (((n.get("data") or {}).get("inputs") or {}).get("llmParam") or []):
                    if isinstance(p, dict) and p.get("name") == "model":
                        mv = str((((p.get("input") or {}).get("value") or {}).get("content")) or "")
                        if mv and mv not in _known:
                            issues.append(f"LLM 节点 {n.get('id')} 模型 '{mv}' 不在 models.json（执行将回退 ctx.llm）")
            _walk(n.get("blocks"))
    try:
        _walk(canvas.get("nodes", []))
    except Exception as e:
        issues.append(f"扫描异常：{e}")
    return issues


def workflows_info(workspace=None) -> list[dict]:
    """供 UI/命令用的工作流摘要：[{name, tool, status, detail, description, coze_url}]。
    status ∈ ok / warn / error / disabled。"""
    out = []
    for it in scan_workflows(workspace):
        meta = it["meta"] or {}
        if it["error"]:
            status, detail = "error", it["error"]
        elif meta.get("enabled") is False:
            status, detail = "disabled", ""
        elif it.get("warnings"):
            status, detail = "warn", "；".join(it["warnings"])
        else:
            status, detail = "ok", ""
        out.append({
            "name": it["name"],
            "tool": WF_PREFIX + _safe_name(meta.get("name") or it["name"]),
            "status": status,
            "detail": detail,
            "description": meta.get("description", ""),
            "coze_url": meta.get("coze_url", ""),
        })
    return out


def get_hook_workflows(workspace: Path = None, hook: str = "") -> list[dict]:
    """返回所有声明在某 hook 位置触发的工作流 [{name, canvas, meta}]。

    hook 取值：before_turn / before_tool / after_tool / before_answer。
    向后兼容：meta.auto:true 且未显式设 hook 的工作流，视为 before_turn。
    未传 hook（空串）则返回所有带 hook（或 auto）的工作流，每项带解析后的 hook 字段。
    """
    out = []
    for it in scan_workflows(workspace):
        meta = it.get("meta") or {}
        if not it.get("canvas") or it.get("error"):
            continue
        if meta.get("enabled") is False:         # 显式禁用的钩子不触发（开关机制）
            continue
        h = meta.get("hook")
        if h is None and meta.get("auto"):       # 旧式 auto:true ≡ before_turn
            h = "before_turn"
        if not h:
            continue
        if hook and h != hook:
            continue
        item = {"name": it["name"], "canvas": it["canvas"], "meta": meta, "hook": h}
        if h == "before_turn":
            item["auto_param"] = meta.get("auto_param", "query")   # 兼容旧 auto_param 名
        out.append(item)
    return out


def get_auto_workflows(workspace: Path = None) -> list[dict]:
    """[兼容] 返回所有 before_turn 钩子工作流（含旧 auto:true）。等价 get_hook_workflows(hook='before_turn')。"""
    return get_hook_workflows(workspace, "before_turn")


def run_hook(canvas: dict, context: dict, *, tools, llm, workspace=None, run_id: str = None) -> tuple:
    """执行一个钩子工作流，返回 (inject: bool, result: str, message: str)。
    run_id：观测注册表 id（_run_hooks 生成），透传给 execute。

    结束节点约定返回 {inject: bool, result: str, message: str}：
      - inject=true + result → 作 system 旁注喂主 LLM（注入语义）；
      - message（无论 inject）→ 发 workflow_message 事件到 UI，【不进主 LLM】
        （用于"静默执行+系统通知"类钩子，如 wiki 自动维护）。
    解析规则——
      - 显式 end 返回 dict：inject 缺省按 result 非空推断；result 兜底 output；
      - dict 无 inject（旧式 {output:x} / 引用未解析 {result:None}）：取唯一值，None/空 → 不注入；
      - 隐式 end/纯文本 → 尝试 JSON 解析；失败则整体当 result，inject=True（非空即注入）。
    inject 可能以字符串 'false'/'true' 形式传来，按布尔语义归一化；message 始终为字符串。
    """
    def _to_bool(v):
        if v is None:
            return False
        if isinstance(v, bool):
            return v
        if isinstance(v, (int, float)):
            return v != 0
        return str(v).strip().lower() not in ("false", "0", "no", "off", "none", "")
    def _msg(d):
        m = d.get("message")
        return "" if m is None else str(m)

    ret = execute(canvas, context, tools=tools, llm=llm, workspace=workspace,
                  return_exit_dict=True, run_id=run_id)
    if isinstance(ret, dict):
        msg = _msg(ret)
        if "inject" in ret:
            return _to_bool(ret.get("inject")), str(ret.get("result") or ret.get("output") or ""), msg
        # 无 inject 键（旧式 {output:x} / 引用未解析的 {result:None}）：取唯一值，None/空 → 不注入
        v = next((ret.get(k) for k in ("result", "output") if k in ret), None)
        if v is None and ret:
            v = next(iter(ret.values()))
        vs = "" if v is None else str(v)
        return (bool(vs), vs, msg)
    s = str(ret).strip()
    if s.startswith("{"):
        try:
            d = json.loads(s)
            if isinstance(d, dict) and "inject" in d:
                return _to_bool(d.get("inject")), str(d.get("result") or d.get("output") or ""), _msg(d)
        except Exception:
            pass
    return (bool(s), s, "")


def seed_default_workflows(workspace: Path = None) -> int:
    """把随包附带的默认工作流（src/workflows/*.xml）播种到 workspace/.agent/workflows/。
    仅在目标不存在时拷贝（用户改动过的同名文件不会被覆盖）。返回播种数量。
    用于让"默认行为类"工作流（如 cs_auto_diag 自动诊断）对 pip 安装的用户也开箱即用。
    播种时写 seed_state 基线（asset_sync 的 /update-assets 依赖三方 hash 判定可否安全更新）。"""
    workspace = workspace or WORKSPACE
    try:
        bundled_dir = Path(__file__).resolve().parent / "workflows"
        if not bundled_dir.is_dir():
            return 0
        target_dir = workspace / ".agent" / "workflows"
        target_dir.mkdir(parents=True, exist_ok=True)
        from asset_sync import _sha, _load_state, _save_state
        st = _load_state(workspace)
        n = 0
        for src in sorted(bundled_dir.glob("*.xml")):
            dst = target_dir / src.name
            if not dst.exists():
                try:
                    dst.write_bytes(src.read_bytes())   # 字节级：write_text 行尾转换会让 /update-assets 的 hash 对不上
                    st[f"workflow/{src.name}"] = _sha(src)   # 基线：随包 hash
                    n += 1
                except Exception:
                    pass
        if n:
            _save_state(workspace, st)
        return n
    except Exception:
        return 0


def refresh_workflow_tools(toolbox, workspace: Path = None, agent=None) -> tuple[list, list]:
    """每轮调用：清掉旧 wf_* 工具，按当前 .agent/workflows/ 重新注册。返回 (ok_names, broken)。
    本地脚本不再自动注册为工具——改用内置 run_script(script, payload) 工具执行（见 real_tools）。"""
    workspace = workspace or WORKSPACE
    seed_default_workflows(workspace)   # 确保默认工作流存在（存在则不覆盖），再扫描
    toolbox.drop(WF_PREFIX)
    ok, broken = [], []
    for item in scan_workflows(workspace):
        meta = item["meta"] or {}
        if meta.get("enabled") is False:
            continue
        if meta.get("hidden") is not False:
            continue   # 默认 hidden（未设置=true）：不注册成 Agent 工具。显式 hidden=False（管理页/编辑器取消勾选保存）才注册
        if item["error"] or item["canvas"] is None:
            broken.append((item["name"], item["error"]))
            continue
        try:
            t = make_workflow_tool(meta, item["canvas"], item["path"], agent)
            toolbox.register_or_replace(t)
            ok.append(t.name)
        except Exception as e:
            broken.append((item["name"], f"工具生成失败：{type(e).__name__}: {e}"))
    return ok, broken


# ========== 管理工具（list_workflows，供 Agent/用户查看）==========

def make_workflow_mgmt_tools(workspace: Path = None):
    """工作流管理工具：列出当前扫描到的工作流（含状态）。"""
    workspace = workspace or WORKSPACE

    def list_workflows() -> str:
        """列出 .agent/workflows/ 下所有工作流及其加载状态（✅可用 / ⚠️有误 / ⏸已禁用）。"""
        items = scan_workflows(workspace)
        if not items:
            return "（.agent/workflows/ 为空或不存在）"
        lines = []
        for it in items:
            meta = it["meta"] or {}
            if meta.get("enabled") is False:
                mark = "⏸已禁用"
            elif it["error"]:
                mark = f"⚠️{it['error']}"
            else:
                mark = "✅可用"
            desc = (meta.get("description") or "").strip()
            lines.append(f"- wf_{_safe_name(it['name'])}：{mark}" + (f"（{desc}）" if desc else ""))
        return f"共 {len(items)} 个工作流：\n" + "\n".join(lines)

    return [Tool(list_workflows)]
