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
    return {"type": "40", "label": "Assigner", "handler": _handle_assigner}
