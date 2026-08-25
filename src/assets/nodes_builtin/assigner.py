"""Assigner 变量赋值节点插件（type 40）：把 input 值写入 left 指向的全局变量。"""
from workflow_node_api import resolve_value


def _handle_assigner(node: dict, ctx) -> dict:
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


def agt_node():
    return {"type": "40", "label": "Assigner", "handler": _handle_assigner, "catalog": _CATALOG}

# ===== 节点目录条目（list_workflow_nodes / query_workflow_node 动态聚合自插件声明）=====
_CATALOG = {"name": "赋值 (Assigner)", "desc": "修改全局变量或工作流变量的值，left 指向变量路径，input 是新值", "xml": "<!-- 赋值节点：修改变量值 -->\n<node id=\"190001\" type=\"assigner\">\n  <!-- inputParameters 声明左值（变量路径）和右值（新值） -->\n  <in name=\"counter\" left=\"global_variable_app.counter\">\n    <!-- input 是新值：可 ref 上游或 literal 字面量 -->\n    <value ref=\"150001.sum\"/>\n  </in>\n  <in name=\"username\" left=\"global_variable_app.username\">\n    <value literal=\"Alice\"/>\n  </in>\n\n  <out name=\"isSuccess\" type=\"boolean\"/>\n</node>\n<!--\n  left 路径：global_variable_app.<变量名>（全局变量）\n  输出：isSuccess（赋值是否成功）\n-->"}
