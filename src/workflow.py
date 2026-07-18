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
import re
from pathlib import Path

from real_tools import WORKSPACE
from tools import Tool

# Coze 固定节点 ID
ENTRY_ID = "100001"   # type "1" 开始
EXIT_ID = "900001"    # type "2" 结束
WF_PREFIX = "wf_"

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
    """按点号取子字段：'obj.field1' → obj['field1']['field1']...；支持 list 下标。"""
    if not name:
        return obj
    cur = obj
    for part in name.split("."):
        if cur is None:
            return None
        if isinstance(cur, dict):
            cur = cur.get(part)
        elif isinstance(cur, list):
            try:
                cur = cur[int(part)]
            except (ValueError, IndexError):
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

def _handle_llm(node: dict, ctx) -> dict:
    """type 3：渲染 prompt/systemPrompt，调用 ctx.llm，输出 {output: 文本}。
    本节点声明的 outputs 结构会作为 JSON Schema 自动并入 systemPrompt，约束模型输出格式。"""
    inputs = node.get("data", {}).get("inputs", {})
    params = _resolve_input_params(inputs.get("inputParameters", []), ctx)

    cfg = {}
    for p in inputs.get("llmParam", []):
        cfg[p.get("name")] = resolve_value(p.get("input"), ctx)

    prompt = render_template(str(cfg.get("prompt", "")), params)
    system = render_template(str(cfg.get("systemPrompt", "")), params).strip()

    # 把节点声明的输出结构转成 JSON Schema，并入系统提示词
    outputs = node.get("data", {}).get("outputs", []) or []
    if outputs:
        schema = _outputs_to_json_schema(outputs)
        schema_hint = ("\n\n【输出要求】请严格按照以下 JSON Schema 输出（纯 JSON，不要 markdown 代码块，不要多余解释）：\n"
                       + json.dumps(schema, ensure_ascii=False, indent=2))
        system = (system + schema_hint) if system else schema_hint.strip()

    msgs = []
    if system:
        msgs.append({"role": "system", "content": system})
    msgs.append({"role": "user", "content": prompt or "（空提示）"})

    overrides = {}
    if cfg.get("temperature") is not None:
        try:
            overrides["temperature"] = float(cfg["temperature"])
        except (TypeError, ValueError):
            pass
    resp = ctx.llm.chat(msgs, **overrides)
    return {"outputs": {"output": getattr(resp, "content", "") or ""}, "port": None}


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
            return s
        if t in ("list", "array"):
            if isinstance(sch, dict):
                return {"type": "array", "items": _type_to_schema(sch.get("type", "string"), sch)}
            if isinstance(sch, list):
                return {"type": "array", "items": {"type": "object", "properties": {s.get("name", ""): _var_to_schema(s) for s in sch}}}
            return {"type": "array", "items": {}}
        return {"type": t}

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


def _handle_code(node: dict, ctx) -> dict:
    """type 5：沙箱执行 Python（Coze language=3）。code 形如 `async def main(args)->Output`，
    args.params 取 inputParameters；return 的 dict 作为节点输出。"""
    import os
    import subprocess
    import sys
    import tempfile

    inputs = node.get("data", {}).get("inputs", {})
    language = inputs.get("language", 3)
    if language != 3:
        raise WorkflowError(f"代码节点仅支持 Python3(language=3)，收到 language={language}")
    params = _resolve_input_params(inputs.get("inputParameters", []), ctx)
    code = inputs.get("code", "") or ""

    runner = (
        "import json, os, asyncio, inspect\n"
        "class _Args:\n"
        "    def __init__(self, p): self.params = p\n"
        "Output = dict\n"
        "args = _Args(json.loads(os.environ['WF_PARAMS']))\n"
        + code +
        "\n_r = None\n"
        "if 'main' in dir():\n"
        "    _m = main\n"
        "    if inspect.iscoroutinefunction(_m):\n"
        "        _r = asyncio.get_event_loop().run_until_complete(_m(args))\n"
        "    else:\n"
        "        _r = _m(args)\n"
        "print('__WF_RESULT__', json.dumps(_r, ensure_ascii=False, default=str))\n"
    )
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False, encoding="utf-8") as f:
        f.write(runner)
        tmp = f.name
    env = dict(os.environ)
    env["WF_PARAMS"] = json.dumps(params, ensure_ascii=False, default=str)
    try:
        proc = subprocess.run([sys.executable, tmp], capture_output=True, text=True,
                              timeout=30, encoding="utf-8", errors="replace", env=env)
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass

    marker = None
    for line in (proc.stdout or "").splitlines():
        if line.startswith("__WF_RESULT__ "):
            marker = line[len("__WF_RESULT__ "):]
    if marker is None:
        tail = (proc.stderr or proc.stdout or "")[-500:]
        raise WorkflowError(f"代码节点未产出结果（可能抛错）：{tail}")
    try:
        ret = json.loads(marker)
    except json.JSONDecodeError:
        ret = {"output": marker}
    if not isinstance(ret, dict):
        ret = {"output": ret}
    return {"outputs": ret, "port": None}


def _handle_text(node: dict, ctx) -> dict:
    """type 15 文本处理：concat（渲染 concatResult 模板）/ split（按分隔符切分）。"""
    inputs = node.get("data", {}).get("inputs", {})
    method = inputs.get("method", "concat")
    params = _resolve_input_params(inputs.get("inputParameters", []), ctx)
    if method == "split":
        sep = ","
        for p in inputs.get("splitParams", []):
            if "char" in (p.get("name") or "").lower() or "sep" in (p.get("name") or "").lower():
                sep = str(resolve_value(p.get("input"), ctx))
        val = next((v for v in params.values() if v is not None), "")
        return {"outputs": {"output": str(val).split(sep)}, "port": None}
    # concat
    result = ""
    for p in inputs.get("concatParams", []):
        if p.get("name") == "concatResult":
            tmpl = resolve_value(p.get("input"), ctx)
            result = render_template(str(tmpl), params)
    return {"outputs": {"output": result}, "port": None}


def _handle_tojson(node: dict, ctx) -> dict:
    """type 58：把 input 变量序列化成 JSON 字符串。"""
    inputs = node.get("data", {}).get("inputs", {})
    val = None
    for p in inputs.get("inputParameters", []):
        if p.get("name") == "input":
            val = resolve_value(p.get("input"), ctx)
    return {"outputs": {"output": json.dumps(val, ensure_ascii=False, default=str)}, "port": None}


def _handle_fromjson(node: dict, ctx) -> dict:
    """type 59：把 JSON 字符串解析成对象。"""
    inputs = node.get("data", {}).get("inputs", {})
    raw = None
    for p in inputs.get("inputParameters", []):
        if p.get("name") == "input":
            raw = resolve_value(p.get("input"), ctx)
    try:
        parsed = json.loads(raw) if isinstance(raw, str) else raw
    except (json.JSONDecodeError, TypeError) as e:
        raise WorkflowError(f"FromJSON 解析失败：{e}（原文：{str(raw)[:120]}）")
    return {"outputs": {"output": parsed}, "port": None}


def _handle_aggregator(node: dict, ctx) -> dict:
    """type 32 变量聚合：多个分支汇合，取"实际执行到的那个"上游输出。"""
    groups = node.get("data", {}).get("inputs", {}).get("mergeGroups", [])
    out = {}
    for g in groups:
        gname = g.get("name")
        chosen = None
        for bi in g.get("variables", []):
            val = bi.get("value", bi) if isinstance(bi, dict) else {}
            content = val.get("content") if isinstance(val, dict) else None
            if isinstance(content, dict) and content.get("source") == "block-output":
                if str(content.get("blockID", "")) in ctx.node_outputs:  # 该分支执行过
                    chosen = resolve_value(bi, ctx)
                    break
            else:
                v = resolve_value(bi, ctx)  # 字面量/全局变量：取非空者
                if v is not None:
                    chosen = v
                    break
        out[gname] = chosen
    return {"outputs": out, "port": None}


def _handle_assigner(node: dict, ctx) -> dict:
    """type 40 变量赋值：把 input 值写入 left 指向的全局变量。"""
    inputs = node.get("data", {}).get("inputs", {})
    for p in inputs.get("inputParameters", []):
        val = resolve_value(p.get("input"), ctx)
        left = p.get("left", {})
        lv = left.get("value", left) if isinstance(left, dict) else {}
        content = lv.get("content") if isinstance(lv, dict) else None
        path = (content or {}).get("path") if isinstance(content, dict) else None
        if path:
            ctx.global_vars[str(path[0])] = val
    return {"outputs": {"isSuccess": True}, "port": None}


# ----- Selector(8) 条件分支 -----

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


def _handle_selector(node: dict, ctx) -> dict:
    """type 8 选择器：按分支顺序求值，第 i 个(0起)成立的分支 → 端口 'true'(i=0) / 'true_{i}'(i>0)；
    都不成立 → 'false'。"""
    branches = node.get("data", {}).get("inputs", {}).get("branches", [])
    for i, br in enumerate(branches):
        if _eval_condition(br.get("condition", {}), ctx):
            return {"outputs": {}, "port": "true" if i == 0 else f"true_{i}"}
    return {"outputs": {}, "port": "false"}


# ----- 复合节点：Loop(21) / Batch(28) + LoopSetVariable(20) + Break(19)/Continue(29) -----

_INLINE_OUT_SUFFIX = "-function-inline-output"
_MAX_LOOP_ITERS = 10000   # 单次循环迭代上限（防失控；infinite 靠 Break 退出）


def _run_composite_body(blocks_by_id: dict, edges: list, composite_id: str,
                        body_outputs: dict, ctx, max_steps: int = 5000) -> str:
    """执行复合节点内部子图一轮：从 <prefix>-function-inline-output 入口跑到回到 composite
    （或死路）。返回信号 'done' / 'break' / 'continue'。Break(19)/Continue(29) 直接判类型。"""
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
                return "done"
            node = blocks_by_id.get(current)
            if node is None:
                return "done"
            ntype = str(node.get("type"))
            if ntype == "19":
                return "break"
            if ntype == "29":
                return "continue"
            handler = NODE_HANDLERS.get(ntype)
            if handler is None:
                raise WorkflowError(f"复合节点体内未支持的节点类型 {ntype}（节点 {current}）")
            result = handler(node, ctx)
            ctx.node_outputs[current] = result.get("outputs") or {}
            current = _next_node(edges, current, result.get("port"))
        return "done"
    finally:
        ctx.node_outputs = saved


def _handle_loop_setvar(node: dict, ctx) -> dict:
    """type 20：循环内设置变量。left 指向循环变量名(blockID=复合节点)，right 为新值；写 ctx.loop_vars。"""
    for p in node.get("data", {}).get("inputs", {}).get("inputParameters", []):
        left = p.get("left", {})
        lv = left.get("value", left) if isinstance(left, dict) else {}
        content = lv.get("content") if isinstance(lv, dict) else None
        var_name = content.get("name") if isinstance(content, dict) else None
        new_val = resolve_value(p.get("right", p.get("input")), ctx)
        if var_name and getattr(ctx, "loop_vars", None) is not None:
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
        signal = _run_composite_body(blocks_by_id, edges, composite_id, body_outputs, ctx)
        last_exposed, last_body = exposed, body_outputs
        if signal == "break":
            break

    # 解析输出：用最后一轮 body_outputs，且把复合节点映射更新为最新 loop_vars（取累加终值）
    merged = dict(last_body) if last_body else dict(outer)
    merged[composite_id] = {**(last_exposed or {}), **loop_vars}
    saved = ctx.node_outputs
    ctx.node_outputs = merged
    try:
        outputs = {o.get("name"): resolve_value(o.get("input"), ctx)
                   for o in node.get("data", {}).get("outputs", [])}
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
    collected = {o.get("name"): [] for o in decl}
    last_body = {}
    for idx, elem in enumerate(elements[:_MAX_LOOP_ITERS]):
        ctx.batch_item = elem
        ctx.batch_index = idx
        exposed = dict(other_inputs)
        if elem_name is not None:
            exposed[elem_name] = elem
        exposed["index"] = idx
        body_outputs = dict(outer)
        body_outputs[composite_id] = exposed
        _run_composite_body(blocks_by_id, edges, composite_id, body_outputs, ctx)
        last_body = body_outputs
        saved = ctx.node_outputs
        ctx.node_outputs = body_outputs
        try:
            for o in decl:
                if o.get("type") == "list" or (o.get("name") or "").endswith("_list"):
                    collected[o.get("name")].append(resolve_value(o.get("input"), ctx))
        finally:
            ctx.node_outputs = saved

    outputs = {}
    for o in decl:
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
    return {"outputs": outputs, "port": None}


# ----- 意图识别(22) / HTTP(45) / 子工作流(9) / 插件(4) -----

def _try_parse(s) -> dict:
    """尝试把字符串解析成 dict；失败返回 {}（用于把工具/子工作流的文本结果当结构化用）。"""
    try:
        v = json.loads(s) if isinstance(s, str) else s
        return v if isinstance(v, dict) else {}
    except Exception:
        return {}


def _render_http_template(text: str, ctx) -> str:
    """HTTP 节点的 {{block_output_<id>.<field>}} 模板（区别于普通 {{inputParam}}）。"""
    def _repl(m):
        parts = m.group(1).split(".", 1)
        val = _dotted_get(ctx.node_outputs.get(parts[0], {}), parts[1] if len(parts) > 1 else "")
        if val is None:
            return ""
        if isinstance(val, (dict, list)):
            return json.dumps(val, ensure_ascii=False)
        return str(val)
    return re.sub(r"\{\{block_output_([^}]+)\}\}", _repl, text or "")


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



def _handle_intent(node: dict, ctx) -> dict:
    """type 22 意图识别：LLM 把 query 分到预设意图之一。命中第 i 个 → 端口 branch_{i}，否则 default。
    意图选项自动并入提示词；若节点声明了 systemPrompt 则一并送入。"""
    inputs = node.get("data", {}).get("inputs", {})
    params = _resolve_input_params(inputs.get("inputParameters", []), ctx)
    intents = [i.get("name", "") for i in inputs.get("intents", [])]
    query = params.get("query") or next((v for v in params.values() if v), "")

    # 意图列表自动并入提示词（带编号，便于模型返回）
    list_str = "\n".join(f"{i+1}. {n}" for i, n in enumerate(intents) if n) or "(无意图)"
    prompt = (f"判断用户输入属于下列哪个意图，只回复对应编号（数字），不要任何解释。\n"
              f"可选意图：\n{list_str}\n\n用户输入：{query}\n\n"
              f"若无任何匹配，回复 0。")
    msgs = []
    sys_input = next((p for p in inputs.get("llmParam", []) if p.get("name") == "systemPrompt"), None)
    sys_text = resolve_value(sys_input.get("input"), ctx) if sys_input else ""
    if sys_text:
        msgs.append({"role": "system", "content": str(sys_text)})
    msgs.append({"role": "user", "content": prompt})
    resp = ctx.llm.chat(msgs)
    answer = (getattr(resp, "content", "") or "").strip()

    # 优先按编号解析，其次按意图名匹配
    idx = None
    digits = "".join(ch for ch in answer if ch.isdigit())
    if digits:
        n = int(digits)
        if 1 <= n <= len(intents):
            idx = n - 1
    if idx is None:
        for i, name in enumerate(intents):
            if name and (name == answer or name in answer):
                idx = i
                break
    if idx is None:
        return {"outputs": {}, "port": "default"}
    return {"outputs": {}, "port": f"branch_{idx}"}


def _handle_http(node: dict, ctx) -> dict:
    """type 45 HTTP 请求：method/url/headers/params/body/auth。URL/JSON 体支持 {{block_output_*}} 模板。"""
    import urllib.parse
    import urllib.request
    import urllib.error

    inputs = node.get("data", {}).get("inputs", {})
    api = inputs.get("apiInfo", {}) or {}
    method = (api.get("method") or "GET").upper()
    url = _render_http_template(api.get("url", ""), ctx)

    kv = {}
    for p in inputs.get("params", []) or []:
        kv[p.get("name")] = resolve_value(p.get("input"), ctx)
    if kv:
        url += ("&" if "?" in url else "?") + urllib.parse.urlencode(kv, doseq=True)

    headers = {}
    for p in inputs.get("headers", []) or []:
        headers[p.get("name")] = str(resolve_value(p.get("input"), ctx))

    auth = inputs.get("auth") or {}
    if auth.get("authOpen") and auth.get("authType") == "bearer":
        for p in ((auth.get("authData") or {}).get("bearerTokenData") or []):
            if p.get("name") == "token":
                headers["Authorization"] = "Bearer " + str(resolve_value(p.get("input"), ctx))

    body = inputs.get("body") or {}
    bt = body.get("bodyType")
    bd = body.get("bodyData") or {}
    data = None
    if bt == "JSON":
        data = _render_http_template(bd.get("json", ""), ctx).encode("utf-8")
        headers.setdefault("Content-Type", "application/json")
    elif bt == "RAW_TEXT":
        data = _render_http_template(bd.get("rawText", ""), ctx).encode("utf-8")
        headers.setdefault("Content-Type", "text/plain")
    elif bt in ("FORM_DATA", "FORM_URLENCODED"):
        fields = bd.get("formURLEncoded") or (bd.get("formData", {}) or {}).get("data") or []
        form = {p.get("name"): str(resolve_value(p.get("input"), ctx)) for p in fields}
        data = urllib.parse.urlencode(form).encode("utf-8")
        headers.setdefault("Content-Type", "application/x-www-form-urlencoded")

    setting = inputs.get("setting") or {}
    timeout = int(setting.get("timeout") or 15)
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            code = resp.getcode()
            hdrs = json.dumps(dict(resp.headers.items()), ensure_ascii=False)
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace") if hasattr(e, "read") else str(e)
        code = e.code
        hdrs = "{}"
    except Exception as e:
        return {"outputs": {"body": f"[HTTP 失败] {type(e).__name__}: {e}", "statusCode": 0, "headers": "{}"},
                "port": None}
    return {"outputs": {"body": raw, "statusCode": code, "headers": hdrs}, "port": None}


def _handle_subworkflow(node: dict, ctx) -> dict:
    """type 9 子工作流：workflowId 按本地 .agent/workflows/<名> 匹配并执行（我们的约定：
    手写工作流时把 workflowId 写成目标工作流的 name/文件名）。"""
    inputs = node.get("data", {}).get("inputs", {})
    wf_id = str(inputs.get("workflowId", "")).strip()
    params = _resolve_input_params(inputs.get("inputParameters", []), ctx)
    canvas = _find_local_workflow(ctx, wf_id)
    if canvas is None:
        raise WorkflowError(f"子工作流未找到：{wf_id!r}（本地按 .agent/workflows/<名>.json 的 name/文件名匹配）")
    result = execute(canvas, params, tools=ctx.tools, llm=ctx.llm, emit=ctx.emit,
                     workspace=ctx.workspace, return_exit_dict=True)
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
    raw = actual_tools.call(tool_name, args)
    outputs = {"raw": raw}
    # 尝试解析 raw 为结构化，按用户声明的 outputs 字段填充
    parsed = _try_parse(raw)
    declared = node.get("data", {}).get("outputs", []) or []
    for o in declared:
        nm = o.get("name")
        if not nm or nm == "raw":
            continue
        outputs[nm] = _extract_field(parsed if parsed else raw, nm, o)
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
    "1": _passthrough,
    "2": _passthrough,
    "3": _handle_llm,
    "5": _handle_code,
    "15": _handle_text,
    "58": _handle_tojson,
    "59": _handle_fromjson,
    "32": _handle_aggregator,
    "40": _handle_assigner,
    "8": _handle_selector,
    "20": _handle_loop_setvar,
    "21": _handle_loop,
    "28": _handle_batch,
    "22": _handle_intent,
    "45": _handle_http,
    "9": _handle_subworkflow,
    "4": _handle_plugin,
    "13": _handle_output_emitter,
}


# ========== 调度器 ==========

class _Ctx:
    """运行时上下文：各节点输出、全局变量、循环变量、workspace、以及 tools/llm 引用。"""
    def __init__(self, *, tools, llm, emit=None, workspace=None):
        self.node_outputs: dict[str, dict] = {}
        self.global_vars: dict = {}
        self.loop_vars: dict | None = None   # 当前循环的累加变量（LoopSetVariable 读写）
        self.batch_item = None               # 当前批处理的 item（loop-item source 用）
        self.batch_index = None              # 当前批处理的 index（loop-index source 用）
        self.tools = tools
        self.llm = llm
        self.emit = emit
        self.workspace = workspace or WORKSPACE


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
    """找 node_id 的后继：有 port 时匹配 sourcePortID，否则优先空端口、再取第一个。"""
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
    all_outputs = []
    saved_item, saved_idx = ctx.batch_item, ctx.batch_index
    try:
        for idx, item in enumerate(arr[:_MAX_LOOP_ITERS]):
            ctx.batch_item = item
            ctx.batch_index = idx
            try:
                r = handler(node, ctx)
                all_outputs.append(r.get("outputs") or {})
            except WorkflowError as e:
                all_outputs.append({"_error": str(e)})  # 单次失败不中断
            except Exception as e:
                all_outputs.append({"_error": f"{type(e).__name__}: {e}"})
    finally:
        ctx.batch_item, ctx.batch_index = saved_item, saved_idx

    # 组2：非 null/空 且满足 filter 条件
    filt = batch.get("filter")
    filtered = []
    for out in all_outputs:
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


def _is_null_output(out: dict) -> bool:
    """判断单次输出是否算 null（全空值）。"""
    vals = [v for k, v in out.items() if k != "_error"]
    if not vals:
        return True
    return all(v is None or v == "" or v == [] or v == {} for v in vals)


def _eval_batch_filter(condition: dict, output: dict) -> bool:
    """批处理筛选：复用 Selector 的 condition 结构，left 引用本次输出字段。
    left 的 ref 用特殊 blockID='__batch_output__' 指向本次 output。"""
    class _Proxy:
        node_outputs = {"__batch_output__": output}
        global_vars = {}
    # 临时让 _eval_condition / _cmp 的 left ref 能解析到本次 output
    conds = condition.get("conditions", [])
    logic = condition.get("logic", 2)
    results = []
    for c in conds:
        left_input = (c.get("left") or {}).get("input")
        right_input = (c.get("right") or {}).get("input") if "right" in c else None
        # 把 left 的 ref blockID 重定向到本次 output
        l = _redirect_ref(left_input, output)
        r = _resolve_filter_value(right_input, output)
        results.append(_cmp(c.get("operator"), l, r))
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


def execute(canvas: dict, inputs: dict, *, tools, llm, emit=None, workspace=None, max_steps: int = 1000, return_exit_dict: bool = False):
    """执行一个 Coze 画布，返回结束节点的输出（字符串）。"""
    ctx = _Ctx(tools=tools, llm=llm, emit=emit, workspace=workspace)
    nodes = {str(n["id"]): n for n in canvas.get("nodes", [])}
    edges = canvas.get("edges", [])

    entry = nodes.get(ENTRY_ID)
    if entry is None:
        raise WorkflowError("画布缺少开始节点（id=100001, type=1）")
    ctx.node_outputs[ENTRY_ID] = _bind_entry(entry, inputs or {})

    current = _next_node(edges, ENTRY_ID, None)
    if current is None:
        return _stringify_result({})     # 空工作流

    for _ in range(max_steps):
        if current == EXIT_ID:
            raw = _exit_result(nodes[EXIT_ID], ctx)
            return raw if return_exit_dict else _stringify_result(raw)
        node = nodes.get(current)
        if node is None:
            raise WorkflowError(f"节点 {current} 不存在（边指向了不存在的节点）")
        ntype = str(node.get("type"))
        handler = NODE_HANDLERS.get(ntype)
        if handler is None:
            raise WorkflowError(f"未支持的节点类型 {ntype}（节点 {current}）——该节点类型将在后续阶段支持")
        result = _run_node_with_batch(node, handler, ctx)
        ctx.node_outputs[current] = result.get("outputs") or {}
        nxt = _next_node(edges, current, result.get("port"))
        if nxt is None:
            return _stringify_result(ctx.node_outputs.get(current, {}))  # 隐式结束
        current = nxt
    raise WorkflowError(f"执行步数超限({max_steps})，疑似死循环")


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
            return execute(canvas, kwargs, tools=agent.tools, llm=agent.llm,
                           workspace=WORKSPACE, emit=getattr(agent, "_emit", None))
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
_SUPPORTED_TYPES = set(NODE_HANDLERS.keys()) | {"1", "2", "19", "29", "31"}


def validate_canvas_detailed(canvas: dict) -> list[str]:
    """不执行地扫描画布（含复合节点 blocks），报告未支持的节点类型。返回问题字符串列表。"""
    issues = []
    def _walk(nodes):
        for n in nodes or []:
            t = str(n.get("type"))
            if t not in _SUPPORTED_TYPES:
                issues.append(f"节点 {n.get('id')} 类型 {t} 暂未支持")
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


def get_auto_workflows(workspace: Path = None) -> list[dict]:
    """返回所有 auto:true 的工作流 [{name, canvas, meta, auto_param}]。agent.run() 调用。"""
    out = []
    for it in scan_workflows(workspace):
        meta = it.get("meta") or {}
        if meta.get("auto") and it.get("canvas") and not it.get("error"):
            out.append({
                "name": it["name"],
                "canvas": it["canvas"],
                "meta": meta,
                "auto_param": meta.get("auto_param", "query"),  # 默认参数名
            })
    return out


def refresh_workflow_tools(toolbox, workspace: Path = None, agent=None) -> tuple[list, list]:
    """每轮调用：清掉旧 wf_* 工具，按当前 .agent/workflows/ 重新注册。返回 (ok_names, broken)。
    本地脚本不再自动注册为工具——改用内置 run_script(script, payload) 工具执行（见 real_tools）。"""
    workspace = workspace or WORKSPACE
    toolbox.drop(WF_PREFIX)
    ok, broken = [], []
    for item in scan_workflows(workspace):
        meta = item["meta"] or {}
        if meta.get("enabled") is False:
            continue
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
