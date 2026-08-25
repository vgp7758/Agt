"""FromJSON 节点插件（type 59）：JSON 字符串 → 对象。解析失败降级返回原文（不崩溃工作流）。"""
import json

from workflow_node_api import resolve_value


def _fromjson(node: dict, ctx) -> dict:
    inputs = node.get("data", {}).get("inputs", {})
    raw = None
    for p in inputs.get("inputParameters", []):
        if p.get("name") == "input":
            raw = resolve_value(p.get("input"), ctx)
    try:
        parsed = json.loads(raw) if isinstance(raw, str) else raw
    except (json.JSONDecodeError, TypeError):
        # 降级：返回原文，避免单次解析失败（LLM 轻微畸形 JSON / HTTP 错误体）整条工作流崩溃
        try:
            ctx.emit({"type": "workflow_message",
                      "text": f"⚠️ FromJSON 解析失败，已降级返回原文：{str(raw)[:80]}"})
        except Exception:
            pass
        parsed = raw
    return {"outputs": {"output": parsed}, "port": None}


def agt_node():
    return {"type": "59", "label": "FromJSON", "handler": _fromjson, "catalog": _CATALOG}

# ===== 节点目录条目（list_workflow_nodes / query_workflow_node 动态聚合自插件声明）=====
_CATALOG = {"name": "FromJson", "desc": "将 JSON 字符串解析为结构化字段，输入一个 JSON 字符串，输出按声明的字段名提取", "xml": "<!-- FromJson 节点：JSON 字符串 → 结构化字段 -->\n<node id=\"240001\" type=\"fromjson\">\n  <!-- 输入：JSON 字符串 -->\n  <in name=\"input\" ref=\"230001.output\"/>\n\n  <!-- 输出：按需声明要从 JSON 中提取的字段 -->\n  <out name=\"name\" type=\"string\"/>\n  <out name=\"age\" type=\"integer\"/>\n  <out name=\"scores\" type=\"list\"/>\n</node>\n<!--\n  输入：input（JSON 字符串，通常来自 HTTP 响应 body 或 ToJson 输出）\n  输出：按 out 声明的字段名从 JSON 中提取对应值\n  解析失败时降级返回原始字符串，不中断工作流\n-->"}
