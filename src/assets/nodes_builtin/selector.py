"""Selector 选择器节点插件（type 8）：按分支顺序求值，第 i 个(0起)成立的分支 →
端口 'true'(i=0) / 'true_{i}'(i>0)；都不成立 → 'false'。
"""
from workflow_node_api import eval_condition


def _handle_selector(node: dict, ctx) -> dict:
    branches = node.get("data", {}).get("inputs", {}).get("branches", [])
    for i, br in enumerate(branches):
        if eval_condition(br.get("condition", {}), ctx):
            return {"outputs": {}, "port": "true" if i == 0 else f"true_{i}"}
    return {"outputs": {}, "port": "false"}


def agt_node():
    return {"type": "8", "label": "Selector", "handler": _handle_selector}
