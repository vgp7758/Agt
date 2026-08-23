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
    return {"type": "59", "label": "FromJSON", "handler": _fromjson}
