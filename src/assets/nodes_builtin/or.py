"""OR 逻辑节点插件（type OR）：多组条件任一满足 → result=true。
条件结构与 selector 同构（branches[].condition.conditions[]，组内 logic 1=AND/2=OR），
逐组求值输出 results（每组 bool）+ result（OR 聚合）。
未设置的条件（编辑器占位）恒真——eval_condition_lenient。
"""
from workflow_node_api import eval_condition_lenient


def _handle_or(node: dict, ctx) -> dict:
    branches = (node.get("data", {}).get("inputs", {}) or {}).get("branches") or []
    results = [bool(eval_condition_lenient((br.get("condition") or {}), ctx)) for br in branches]
    return {"outputs": {"result": any(results), "results": results}, "port": None}


def agt_node():
    return {"type": "OR", "label": "OR", "handler": _handle_or}


PARAMS = [
    {"key": "branches", "type": "object", "required": True,
     "desc": "条件组列表（与 selector 同构），任一满足 → true"},
]
