"""Text 节点插件（type 15）：concat（模板渲染）/ split（按分隔符切分）。"""
from workflow_node_api import resolve_value, resolve_input_params, render_template


def _text(node: dict, ctx) -> dict:
    inputs = node.get("data", {}).get("inputs", {})
    method = inputs.get("method", "concat")
    params = resolve_input_params(inputs.get("inputParameters", []), ctx)
    if method == "split":
        sep = ","
        for p in inputs.get("splitParams", []):
            if "char" in (p.get("name") or "").lower() or "sep" in (p.get("name") or "").lower():
                sep = str(resolve_value(p.get("input"), ctx))
        val = next((v for v in params.values() if v is not None), "")
        return {"outputs": {"output": str(val).split(sep)}, "port": None}
    # concat
    result = ""
    for p in inputs.get("concatParams", []):
        if p.get("name") == "concatResult":
            tmpl = resolve_value(p.get("input"), ctx)
            result = render_template(str(tmpl), params)
    return {"outputs": {"output": result}, "port": None}


def agt_node():
    return {"type": "15", "label": "文本", "handler": _text}
