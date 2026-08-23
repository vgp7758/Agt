"""ToJSON 节点插件（type 58）：input 变量 → JSON 字符串。"""
import json

from workflow_node_api import resolve_value


def _tojson(node: dict, ctx) -> dict:
    inputs = node.get("data", {}).get("inputs", {})
    val = None
    for p in inputs.get("inputParameters", []):
        if p.get("name") == "input":
            val = resolve_value(p.get("input"), ctx)
    return {"outputs": {"output": json.dumps(val, ensure_ascii=False, default=str)}, "port": None}


def agt_node():
    return {"type": "58", "label": "ToJSON", "handler": _tojson}
