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
    return {"type": "15", "label": "文本", "handler": _text, "catalog": _CATALOG}

# ===== 节点目录条目（list_workflow_nodes / query_workflow_node 动态聚合自插件声明）=====
_CATALOG = {"name": "文本处理 (Text)", "desc": "文本拼接(concat)或分割(split)，concat 多输入拼成一个字符串，split 按分隔符切分成列表", "xml": "<!-- 文本处理节点 -->\n<!-- 模式1：concat —— 拼接多个输入 -->\n<node id=\"220001\" type=\"text\" method=\"concat\">\n  <in name=\"part1\" ref=\"130001.output\"/>\n  <in name=\"part2\" literal=\" — \"/>\n  <in name=\"part3\" ref=\"140001.result\"/>\n  <out name=\"string\" type=\"string\"/>\n</node>\n\n<!-- 模式2：split —— 按分隔符切割 -->\n<node id=\"220002\" type=\"text\" method=\"split\">\n  <in name=\"text\" ref=\"130001.output\"/>\n  <param name=\"separator\" literal=\",\">,</param>\n  <out name=\"list\" type=\"list\"/>\n</node>\n<!--\n  concat 输出：string（拼接后的文本）\n  split 输出：list（切割后的字符串数组）\n  separator 默认是逗号\n-->"}
