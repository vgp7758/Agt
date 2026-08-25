"""AND 逻辑节点插件（type AND）：多组条件全部满足 → result=true。
条件结构与 selector 同构（branches[].condition.conditions[]，组内 logic 1=AND/2=OR），
逐组求值输出 results（每组 bool）+ result（AND 聚合）。配合 OR 节点做组合逻辑判定。
未设置的条件（编辑器占位）恒真——eval_condition_lenient（与批处理 filter 裁定一致）。
"""
from workflow_node_api import eval_condition_lenient


def _handle_and(node: dict, ctx) -> dict:
    branches = (node.get("data", {}).get("inputs", {}) or {}).get("branches") or []
    results = [bool(eval_condition_lenient((br.get("condition") or {}), ctx)) for br in branches]
    return {"outputs": {"result": all(results), "results": results}, "port": None}


def agt_node():
    return {"type": "AND", "label": "AND", "handler": _handle_and, "catalog": _CATALOG}


PARAMS = [
    {"key": "branches", "type": "object", "required": True,
     "desc": "条件组列表（与 selector 同构：每组含 logic 与 conditions），全部满足 → true"},
]

# ===== 节点目录条目（list_workflow_nodes / query_workflow_node 动态聚合自插件声明）=====
_CATALOG = {"name": "AND 逻辑与", "desc": "多组条件全部满足 → true。条件组与 selector 同构（左值/运算符/右值，支持引用上游字段），输出 result:boolean 与逐组结果列表", "xml": "<!-- AND：多组条件全部满足才走 true 支路 -->\n<node id=\"160001\" type=\"AND\" title=\"全部满足?\">\n  <branch>\n    <cond op=\"1\" left=\"110001.score\" right=\"0.8\"/>\n    <cond op=\"1\" left=\"110001.hits\" right=\"3\"/>\n  </branch>\n  <out name=\"result\" type=\"boolean\"/>\n  <out name=\"results\" type=\"list<boolean>\"/>\n</node>"}
