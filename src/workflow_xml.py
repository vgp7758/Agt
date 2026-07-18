"""workflow_xml.py —— 工作流 XML 序列化（模型友好）→ Coze 画布 JSON。

为什么用 XML：模型直接写 Coze 画布 JSON 时，代码节点的 code、LLM 的 prompt 等
字段是 JSON 字符串里的字符串，里面的双引号/花括号/换行/JSON 块要层层转义，
极易出错（JSON 套 JSON）。XML 用标签 + CDATA 包裹代码/模板块，内部无需转义：

    <node id="500001" type="code">
      <in name="x" ref="100001.x"/>
      <code><![CDATA[
        async def main(args):
            return {"y": args.params["x"] * 2}   # 引号花括号随便写
      ]]></code>
      <out name="y" type="number"/>
    </node>

落地策略（XML 写作 + JSON 执行）：模型/用户写 .xml，扫描时转成 Coze JSON，
现有执行器/编辑器/Coze 互导能力全部保留。本模块只做 XML → JSON 单向转换。

节点 type 用可读名字（start/llm/code/...），也兼容数字。
"""
from __future__ import annotations

import re
from xml.etree import ElementTree as ET

# 节点 type 可读名 ↔ Coze 数字
TYPE_NAME_TO_NUM = {
    "start": "1", "end": "2", "llm": "3", "code": "5", "selector": "8",
    "text": "15", "loop": "21", "batch": "28", "intent": "22",
    "aggregator": "32", "assigner": "40", "http": "45", "subworkflow": "9",
    "plugin": "4", "tojson": "58", "fromjson": "59", "output": "13",
}
TYPE_NUM_TO_NAME = {v: k for k, v in TYPE_NAME_TO_NUM.items()}


class WorkflowXmlError(Exception):
    """XML 工作流解析错误。"""


def _type_num(t: str) -> str:
    if not t:
        raise WorkflowXmlError("节点缺少 type")
    return TYPE_NAME_TO_NUM.get(t.strip().lower(), t)


def _ref_input(ref: str) -> dict:
    """'NODEID.field' → BlockInput 的 value（ref）"""
    node, _, name = (ref or "").partition(".")
    return {"type": "ref", "content": {"source": "block-output", "blockID": node, "name": name}}


def _parse_val(s, type_hint="string"):
    """字面量按类型转换：integer→int, number→float, boolean→bool，其余 str。"""
    if s is None:
        return ""
    if type_hint in ("integer", "int", "long"):
        try:
            return int(s)
        except (TypeError, ValueError):
            return s
    if type_hint in ("number", "float", "double"):
        try:
            return float(s)
        except (TypeError, ValueError):
            return s
    if type_hint in ("boolean", "bool"):
        return str(s).strip().lower() in ("true", "1", "yes")
    return s


def _val_of_in(el) -> dict:
    """<in ref="..."/> 或 <in literal="..." type="..."/> → BlockInput 的 value 部分 {type, content}"""
    ref = el.get("ref")
    if ref:
        return _ref_input(ref)
    return {"type": "literal", "content": _parse_val(el.get("literal", ""), el.get("type", "string"))}


def _in_param(el) -> dict:
    """<in> → inputParameters 项 {name, input:{type, value}}"""
    return {"name": el.get("name"), "input": {"type": el.get("type", "string"), "value": _val_of_in(el)}}


def _text_block(el) -> str:
    """取元素文本（CDATA 内容原样，保留换行/引号/花括号）。"""
    if el is None:
        return ""
    # ElementTree 把 CDATA 合并进 text，原样保留内部字符
    return el.text or ""


def _cond(el) -> dict:
    """<cond op="13" left="NODE.field" right="60"/> → selector 条件项"""
    op = int(el.get("op", "1"))
    left_ref = el.get("left", "")
    left_input = {"type": "string", "value": _ref_input(left_ref)} if left_ref \
        else {"type": "string", "value": {"type": "literal", "content": ""}}
    rv = el.get("right", "")
    if rv.startswith("ref:"):
        right_input = {"type": "string", "value": _ref_input(rv[4:])}
    else:
        val = rv[8:] if rv.startswith("literal:") else rv
        right_input = {"type": "string", "value": {"type": "literal", "content": val}}
    return {"operator": op, "left": {"input": left_input}, "right": {"input": right_input}}


def xml_to_canvas(xml_str: str) -> dict:
    """把工作流 XML 字符串转成 Coze 画布 JSON {nodes, edges, versions}。"""
    try:
        root = ET.fromstring(xml_str)
    except ET.ParseError as e:
        raise WorkflowXmlError(f"XML 解析失败：{e}")
    nodes, edges = [], []
    for nd in root.findall("node"):
        n = _node_to_json(nd)
        if n:
            nodes.append(n)
    for ed in root.findall("edge"):
        edges.append({"sourceNodeID": ed.get("from"), "targetNodeID": ed.get("to"),
                      "sourcePortID": ed.get("port", "") or ""})
    canvas = {"nodes": nodes, "edges": edges, "versions": {}}
    _validate(canvas)
    return canvas


def _validate(canvas: dict) -> None:
    if not any(str(n["id"]) == "100001" for n in canvas["nodes"]):
        raise WorkflowXmlError("缺少开始节点（id=100001, type=start）")


def _node_to_json(nd) -> dict:
    nid = nd.get("id")
    if not nid:
        raise WorkflowXmlError("节点缺少 id")
    ntype = _type_num(nd.get("type"))
    title = nd.get("title") or TYPE_NUM_TO_NAME.get(ntype, ntype)
    node = {"id": nid, "type": ntype,
            "data": {"nodeMeta": {"title": title}, "inputs": {}, "outputs": []}}
    inp = node["data"]["inputs"]
    out = node["data"]["outputs"]

    if ntype == "1":        # start：data.outputs = 工作流入参
        node["data"]["trigger_parameters"] = []
        for o in nd.findall("out"):
            v = {"name": o.get("name"), "type": o.get("type", "string")}
            if o.get("required") == "true":
                v["required"] = True
            if o.get("default") is not None:
                v["defaultValue"] = _parse_val(o.get("default"), v["type"])
            out.append(v)
    elif ntype == "2":      # end：<out ref> = 返回变量
        inp["terminatePlan"] = "returnVariables"
        inp["inputParameters"] = [
            {"name": o.get("name"),
             "input": {"type": o.get("type", "string"),
                       "value": _ref_input(o.get("ref")) if o.get("ref")
                                else {"type": "literal", "content": _parse_val(o.get("literal", ""))}}}
            for o in nd.findall("out")
        ]
    elif ntype == "3":      # llm：inputParameters + llmParam(prompt/systemPrompt/...)
        inp["inputParameters"] = [_in_param(i) for i in nd.findall("in")]
        inp["llmParam"] = [{"name": p.get("name"),
                            "input": {"type": p.get("type", "string"),
                                      "value": {"type": "literal", "content": _text_block(p)}}}
                           for p in nd.findall("param")]
        out.extend({"name": o.get("name"), "type": o.get("type", "string")} for o in nd.findall("out"))
    elif ntype == "5":      # code：inputParameters + code(CDATA) + outputs
        inp["inputParameters"] = [_in_param(i) for i in nd.findall("in")]
        code_el = nd.find("code")
        inp["code"] = _text_block(code_el)
        inp["language"] = int(code_el.get("language", "3")) if code_el is not None else 3
        out.extend({"name": o.get("name"), "type": o.get("type", "string")} for o in nd.findall("out"))
    elif ntype == "4":      # plugin：toolName + inputParameters + outputs
        node["data"]["toolName"] = nd.get("toolName")
        inp["inputParameters"] = [_in_param(i) for i in nd.findall("in")]
        out.extend({"name": o.get("name"), "type": o.get("type", "string")} for o in nd.findall("out"))
    elif ntype == "15":     # text：method + inputParameters + concatParams(<result>)
        inp["method"] = nd.get("method", "concat")
        inp["inputParameters"] = [_in_param(i) for i in nd.findall("in")]
        res = nd.find("result")
        if res is not None:
            inp["concatParams"] = [{"name": "concatResult",
                                    "input": {"type": "string",
                                              "value": {"type": "literal", "content": _text_block(res)}}}]
        out.append({"name": "output", "type": "string"})
    elif ntype == "8":      # selector：<branch><cond/>
        branches = []
        for br in nd.findall("branch"):
            conds = [_cond(c) for c in br.findall("cond")]
            branches.append({"condition": {"logic": int(br.get("logic", "2")), "conditions": conds}})
        inp["branches"] = branches
    elif ntype == "32":     # aggregator：<group><var ref/>
        mg = []
        for g in nd.findall("group"):
            gname = g.get("name")
            mg.append({"name": gname,
                       "variables": [{"value": _ref_input(v.get("ref"))} for v in g.findall("var")]})
            out.append({"name": gname, "type": g.get("type", "string")})
        inp["mergeGroups"] = mg
    elif ntype == "22":     # intent：<in query/> + <intent name/>
        inp["inputParameters"] = [_in_param(i) for i in nd.findall("in")]
        inp["intents"] = [{"name": it.get("name")} for it in nd.findall("intent")]
        inp["mode"] = "all"
    elif ntype == "9":      # subworkflow
        inp["workflowId"] = nd.get("workflowId", "")
        inp["inputParameters"] = [_in_param(i) for i in nd.findall("in")]
    elif ntype == "45":     # http
        inp["apiInfo"] = {"method": (nd.findtext("method") or "GET").upper(),
                          "url": nd.findtext("url") or ""}
        inp["headers"] = [{"name": h.get("name"),
                           "input": {"type": "string",
                                     "value": {"type": "literal", "content": h.get("value", "")}}}
                          for h in nd.findall("header")]
        body = nd.find("body")
        if body is not None:
            inp["body"] = {"bodyType": body.get("type", "JSON"),
                           "bodyData": {"json": _text_block(body)}}
        else:
            inp["body"] = {"bodyType": "EMPTY", "bodyData": {}}
        inp["setting"] = {"timeout": 15}
        out.extend([{"name": "body", "type": "string"}, {"name": "statusCode", "type": "integer"}])
    elif ntype in ("58", "59"):  # tojson / fromjson：单 input
        inp["inputParameters"] = [_in_param(i) for i in nd.findall("in")]
        out.append({"name": "output", "type": "object" if ntype == "59" else "string"})
    elif ntype == "13":     # output emitter
        inp["inputParameters"] = [_in_param(i) for i in nd.findall("in")]
        c = nd.find("content")
        if c is not None:
            inp["content"] = {"type": "string", "value": {"type": "literal", "content": _text_block(c)}}
        out.append({"name": "output", "type": "string"})
    elif ntype == "40":     # assigner：inputParameters[{name,left,input}]
        ips = []
        for i in nd.findall("in"):
            left_path = i.get("path") or i.get("left", "")
            ips.append({"name": i.get("name", "var"),
                        "left": {"type": "string",
                                 "value": {"type": "ref",
                                           "content": {"source": "global_variable_app",
                                                       "path": [left_path]}}},
                        "input": {"type": i.get("type", "string"), "value": _val_of_in(i)}})
        inp["inputParameters"] = ips
        inp["variableTypeMap"] = {}
        out.append({"name": "isSuccess", "type": "boolean"})
    else:
        # 通用兜底：in→inputParameters, out→outputs, param→inputs[name]
        inp["inputParameters"] = [_in_param(i) for i in nd.findall("in")]
        out.extend({"name": o.get("name"), "type": o.get("type", "string")} for o in nd.findall("out"))
        for p in nd.findall("param"):
            inp[p.get("name")] = _text_block(p)
    return node
