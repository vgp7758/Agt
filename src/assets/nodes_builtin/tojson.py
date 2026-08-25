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
    return {"type": "58", "label": "ToJSON", "handler": _tojson, "catalog": _CATALOG}

# ===== 节点目录条目（list_workflow_nodes / query_workflow_node 动态聚合自插件声明）=====
_CATALOG = {"name": "ToJson", "desc": "将上游多个字段组装成 JSON 字符串，输入字段一一映射到 JSON 对象的 key", "xml": "<!-- ToJson 节点：多个输入字段 → JSON 字符串 -->\n<node id=\"230001\" type=\"tojson\">\n  <in name=\"name\" ref=\"130001.output\"/>\n  <in name=\"age\" ref=\"140001.result\"/>\n  <in name=\"scores\" ref=\"150001.filtered_outputs\"/>\n  <out name=\"output\" type=\"string\"/>\n</node>\n<!--\n  输入：任意多个字段，每个 in 的 name 成为 JSON key，值成为 JSON value\n  输出：output（JSON 字符串，如 {\"name\":\"Alice\",\"age\":\"25\",\"scores\":[...]}）\n  典型用法：组装数据 → HTTP 请求的 body，或传给 run_script 的 payload\n-->"}
