"""workflow_debug_tools.py —— 工作流热调试工具（绑定到 Agent，Agent 自服务编排迭代）。

配合 ws debug 热调试后端（hotswap_node / rerun_node / list_node_outputs），Agent 可
自驱动：跑工作流 → 看指定节点输出 → 热替换节点配置 → 单节点重跑验证——无需人工手动。
"""
from __future__ import annotations

import json

from tools import Tool
from workflow_xml import parse_xml_fragment


def make_workflow_debug_tools(agent) -> list:
    workspace = getattr(agent.session, "workspace", None)

    def debug_workflow(name: str, inputs: str = "") -> str:
        """调试执行一个工作流，返回各节点的输出摘要。name 为工作流名(不含路径)；
        inputs 可选 JSON 字符串（如 {"user_message":"你好"}），对应开始节点的入参。
        执行完后可用 list_workflow_outputs(node_ids) 按节点 id 查看输出，用 hotswap_workflow_node(id,xml) 替换配置。"""
        from workflow import execute_debug
        _name = (name or "").strip()
        if not _name:
            return "[错误] 请提供工作流名"
        from real_tools import WORKSPACE
        wf_dir = (workspace or WORKSPACE) / ".agent" / "workflows"
        canvas = None
        for ext in (".json", ".xml"):
            p = wf_dir / f"{_name}{ext}"
            if p.exists():
                if ext == ".json":
                    canvas = json.loads(p.read_text(encoding="utf-8"))
                else:
                    from workflow_xml import xml_to_canvas
                    canvas = xml_to_canvas(p.read_text(encoding="utf-8"))
                break
        if canvas is None:
            return f"[错误] 找不到工作流 {_name!r}"
        try:
            inp = json.loads(inputs) if inputs and inputs.strip() else {}
        except json.JSONDecodeError:
            inp = {"user_message": inputs or ""}
        try:
            _llm = agent.utility_client() if getattr(agent, "utility_client", None) else agent.llm
            exit_dict, order, trace = execute_debug(
                canvas, inp, tools=agent.tools, llm=_llm, on_node=lambda e: None)
        except Exception as e:
            return f"[执行失败] {type(e).__name__}: {e}"
        lines = [f"工作流 {_name!r} 执行完成（{len(order)} 个节点）："]
        for nid in order:
            t = trace.get(nid, {})
            ks = list(t.keys())[:4]
            v = t.get("raw") or t.get("output") or t.get("result") or t.get("candidates") or ""
            preview = str(v)[:100].replace("\n", " ")
            lines.append(f"  {nid}: {ks} → {preview}")
        lines.append(f"\nlist_workflow_outputs('{','.join(order[:4])}') 看指定节点输出层；eval_node_output(id,script) 过滤/投影。")
        return "\n".join(lines)

    def list_workflow_outputs(node_ids: str = "") -> str:
        """列出上一次 debug_workflow 后指定节点的输出（每节点截断 300 字，防爆上下文）。
        node_ids 为逗号分隔的节点 id（如 '130001,160001'）；留空则列出全部节点 id 清单。
        支持【子画布节点】语法 '复合id/子节点id'（如 '300001/310002'——loop/batch 内部节点的
        最后一轮迭代输出，来自 ctx.sub_trace；复合节点需在最近一次 debug 中执行过）。"""
        from workflow import _debug_ctx
        ctx = _debug_ctx.get("ctx")
        nodes = _debug_ctx.get("nodes", {})
        if not ctx:
            return "（还没有跑过 debug_workflow——请先用 debug_workflow(name, inputs) 执行一次）"
        ids = [x.strip() for x in node_ids.replace("，", ",").split(",") if x.strip()] if node_ids else []
        if not ids:
            sub_ids = [f"{c}/{s}" for c, body in (getattr(ctx, "sub_trace", {}) or {}).items()
                       for s in body if not str(s).startswith("__")]
            return (f"可用节点 id（{len(ctx.node_outputs)} 个）：{', '.join(ctx.node_outputs.keys())}"
                    + (f"\n子画布节点（复合/子节点）：{', '.join(sub_ids)}" if sub_ids else "")
                    + "\n用 list_workflow_outputs('130001,160001') 按 id 查看输出；eval_node_output(id,script) 对单个节点输出作过滤/投影。")

        def _resolve(nid):
            """'comp/sub' → sub_trace[comp][sub]；普通 id → node_outputs[nid]。"""
            if "/" in nid:
                comp, sub = nid.split("/", 1)
                body = (getattr(ctx, "sub_trace", {}) or {}).get(comp.strip())
                return (body or {}).get(sub.strip()), True
            return ctx.node_outputs.get(nid), False

        items = []
        for nid in ids:
            outs, is_sub = _resolve(nid)
            if outs is None:
                items.append(f"  {nid}：（无输出缓存" + ("——复合节点未在最近 debug 中执行/非最后一轮可达" if is_sub else "") + "）")
                continue
            if is_sub:
                items.append(f"  {nid}: {str(outs)[:300]}" + ("…" if len(str(outs)) > 300 else ""))
                continue
            n = nodes.get(nid, {})
            title = (n.get("data", {}) or {}).get("nodeMeta", {}).get("title", "")
            val = str(outs)
            if len(val) > 300:
                val = val[:300] + f"…(共{len(val)}字)"
            items.append(f"  {nid}{'('+title+')' if title else ''}: {val}")
        return "\n".join(items) if items else f"节点 {ids} 无输出"

    def eval_node_output(node_id: str, script: str) -> str:
        """对某个节点的【完整输出】执行一段 Python 片段，返回处理结果。适用：输出很大，
        list_workflow_outputs 截断了 → 用 script 写过滤/投影/提取逻辑，只拿需要的数据。
        script 中可直接用变量 `output`（dict，该节点的完整输出），最后一条表达式的值作为返回值。
        例：eval_node_output('130001','[c[\"text\"] for c in output.get(\"candidates\",[])]') 取候选摘要列表。"""
        from workflow import _debug_ctx
        ctx = _debug_ctx.get("ctx")
        if not ctx:
            return "[错误] 请先 debug_workflow"
        nid = str(node_id).strip()
        # 子画布节点语法 '复合id/子节点id'（与 list_workflow_outputs 同款）
        if "/" in nid:
            comp, sub = nid.split("/", 1)
            body = (getattr(ctx, "sub_trace", {}) or {}).get(comp.strip())
            outs = (body or {}).get(sub.strip())
            if outs is None:
                return f"[错误] 子画布节点 {nid} 无输出缓存（复合节点未执行/非最后一轮可达）"
        else:
            outs = ctx.node_outputs.get(nid)
        if outs is None:
            return f"[错误] 节点 {nid} 无输出缓存"
        script = (script or "").strip()
        if not script:
            return f"[错误] 请提供 script（可直接用变量 output）：\n节点 {nid} 输出 keys={list(outs.keys())[:5]}"
        # 编译并执行
        try:
            code = compile(script, f"<eval_node_output:{nid}>", "eval")
        except SyntaxError:
            # 可能有多行语句 → 用 exec
            try:
                code = compile(script, f"<eval_node_output:{nid}>", "exec")
            except SyntaxError as e2:
                return f"[语法错误] {e2}"
        local_vars = {"output": outs}
        try:
            if isinstance(code, type(compile("1", "", "eval"))):
                # eval 模式
                result = eval(code, {"__builtins__": __builtins__}, local_vars)
            else:
                exec(code, {"__builtins__": __builtins__}, local_vars)
                result = local_vars.get("_", None)  # 执行模式下用 _ 显式返回
        except Exception as e:
            return f"[脚本执行错误] {type(e).__name__}: {e}"
        return str(result) if result is not None else "(空)"

    def hotswap_workflow_node(node_id: str, xml_fragment: str) -> str:
        """热替换某个节点的配置（只改配置，不重跑全流程）。node_id 如 '130001'；
        xml_fragment 为一段 XML 节点配置（如 <code language="3">...</code>），
        支持 <in>/<out>/<code>/<param>/<model>/<branch>/<intent> 等子元素。
        替换后自动重跑该节点（用缓存 ctx），返回新输出。"""
        from workflow import _debug_ctx, _run_node_with_batch, NODE_HANDLERS
        ctx = _debug_ctx.get("ctx")
        nodes = _debug_ctx.get("nodes", {})
        if not ctx or not nodes:
            return "[错误] 没有缓存的调试上下文——请先 debug_workflow(name, inputs)"
        nid = str(node_id).strip()
        old = nodes.get(nid)
        if old is None:
            return f"[错误] 节点 {nid!r} 不在当前工作流中"
        import xml.etree.ElementTree as ET
        try:
            root = ET.fromstring(f"<node id=\"{nid}\">{xml_fragment}</node>")
        except ET.ParseError as e:
            return f"[错误] XML 解析失败: {e}"
        ntype = old["type"]
        new_data = parse_xml_fragment(root, ntype)
        old["data"] = {**old["data"], **new_data}
        handler = NODE_HANDLERS.get(ntype)
        if handler:
            try:
                result = _run_node_with_batch(old, handler, ctx)
                outs = result.get("outputs") or {}
                ctx.node_outputs[nid] = outs
                return f"✅ 节点 {nid} 热替换 + 重跑完成。新输出: {list(outs.keys())[:4]} → {str(outs)[:200]}"
            except Exception as e:
                return f"[重跑失败] 配置已替换但执行报错: {type(e).__name__}: {e}"
        return f"✅ 节点 {nid} 配置已热替换（类型 {ntype} 不支持单节点重跑——可 debug_workflow 全流程验证）"

    return [Tool(debug_workflow), Tool(list_workflow_outputs), Tool(eval_node_output),
            Tool(hotswap_workflow_node)]
