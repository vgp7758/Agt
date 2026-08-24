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
    return {"type": "AND", "label": "AND", "handler": _handle_and}


PARAMS = [
    {"key": "branches", "type": "object", "required": True,
     "desc": "条件组列表（与 selector 同构：每组含 logic 与 conditions），全部满足 → true"},
]
