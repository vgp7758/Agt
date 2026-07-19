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
    "break": "19", "continue": "29", "setvar": "20",
}
TYPE_NUM_TO_NAME = {v: k for k, v in TYPE_NAME_TO_NUM.items()}


class WorkflowXmlError(Exception):
    """XML 工作流解析错误。"""


def _type_num(t: str) -> str:
    if not t:
        raise WorkflowXmlError("节点缺少 type")
    return TYPE_NAME_TO_NUM.get(t.strip().lower(), t)


def _ref_input(ref: str) -> dict:
    """ref 字符串 → BlockInput 的 value（ref）。
    编码：'NODEID.field'=block-output / 'loop-item'[.field] / 'loop-index' / 'global:path'"""
    ref = ref or ""
    if ref == "loop-index":
        return {"type": "ref", "content": {"source": "loop-index"}}
    if ref == "loop-item" or ref.startswith("loop-item."):
        name = ref[10:] if ref.startswith("loop-item.") else ""   # 取 . 后的字段名
        return {"type": "ref", "content": {"source": "loop-item", "name": name}}
    if ref.startswith("global:"):
        path = [p for p in ref[7:].split(".") if p != ""]
        return {"type": "ref", "content": {"source": "global_variable_app", "path": path or [""]}}
    node, _, name = ref.partition(".")
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
    """<in ref="..."/> 或 <in literal="..." type="..."/> 或 <in>inner text</in> → BlockInput 的 value 部分 {type, content}"""
    ref = el.get("ref")
    if ref:
        return _ref_input(ref)
    lit = el.get("literal")
    if lit is not None:
        return {"type": "literal", "content": _parse_val(lit, el.get("type", "string"))}
    # 兜底：标签内文本作为字面量（如 <in name="x" type="string">hello</in>）
    text = (el.text or "").strip()
    return {"type": "literal", "content": _parse_val(text, el.get("type", "string"))}


def _in_param(el) -> dict:
    """<in> → inputParameters 项 {name, input:{type, schema?, value}}（list 可带 item schema）"""
    res = {"name": el.get("name"), "input": {"type": el.get("type", "string"), "value": _val_of_in(el)}}
    fields = el.findall("field")
    if fields:
        res["input"]["schema"] = [_field_to_schema(f) for f in fields]
    elif el.get("itemType"):
        res["input"]["schema"] = {"type": el.get("itemType")}
    return res


def _text_block(el) -> str:
    """取元素文本（CDATA 内容原样，保留换行/引号/花括号）。"""
    if el is None:
        return ""
    # ElementTree 把 CDATA 合并进 text，原样保留内部字符
    return el.text or ""


def _cond(el) -> dict:
    """<cond op="13" left="NODE.field" right="60" left_type="integer"/> → selector 条件项"""
    op = int(el.get("op", "1"))
    left_ref = el.get("left", "")
    lt = el.get("left_type", "string")
    left_input = {"type": lt, "value": _ref_input(left_ref)} if left_ref \
        else {"type": lt, "value": {"type": "literal", "content": ""}}
    rv = el.get("right", "")
    rt = el.get("right_type", "string")
    if rv.startswith("ref:"):
        right_input = {"type": rt, "value": _ref_input(rv[4:])}
    else:
        val = rv[8:] if rv.startswith("literal:") else rv
        right_input = {"type": rt, "value": {"type": "literal", "content": _parse_val(val, rt)}}
    return {"operator": op, "left": {"input": left_input}, "right": {"input": right_input}}


# ----- 复合节点(21/28) 子画布 blocks/edges 与 节点级 batch 的 XML 往返 -----

def _read_composite_body(nd, node):
    """从 <blocks><node.../></blocks> 与 <edges><edge.../></edges> 读回子图（节点级 blocks/edges）。"""
    blocks = []
    bel = nd.find("blocks")
    if bel is not None:
        for b in bel.findall("node"):
            blocks.append(_node_to_json(b))
    node["blocks"] = blocks
    edges = []
    eel = nd.find("edges")
    if eel is not None:
        for e in eel.findall("edge"):
            ed = {"sourceNodeID": e.get("source", ""), "targetNodeID": e.get("target", "")}
            if e.get("sourcePort"):
                ed["sourcePortID"] = e.get("sourcePort")
            if e.get("targetPort"):
                ed["targetPortID"] = e.get("targetPort")
            edges.append(ed)
    node["edges"] = edges


def _read_batch_el(nd):
    """读 <batch enabled nth itemType><input ref/><filter><cond/></filter></batch>。无则 None。"""
    bel = nd.find("batch")
    if bel is None:
        return None
    b = {"enabled": bel.get("enabled", "false") == "true"}
    if bel.get("nth") is not None:
        b["nth"] = _parse_val(bel.get("nth"), "integer")
    if bel.get("itemType"):
        b["itemType"] = bel.get("itemType")
    ie = bel.find("input")
    if ie is not None:
        b["input"] = {"type": ie.get("type", "list"),
                      "value": _ref_input(ie.get("ref")) if ie.get("ref")
                               else {"type": "literal", "content": _parse_val(ie.get("literal", ""), ie.get("type", "string"))}}
    fel = bel.find("filter")
    if fel is not None:
        b["filter"] = {"logic": int(fel.get("logic", "2")),
                       "conditions": [_cond(c) for c in fel.findall("cond")]}
    return b


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


def _field_to_schema(f):
    """<field name type>[<field.../] → schema 项 {name, type, schema?}（递归嵌套 object）"""
    res = {"name": f.get("name", ""), "type": f.get("type", "string")}
    if f.get("description"):
        res["description"] = f.get("description")
    sub = f.findall("field")
    if sub:
        res["schema"] = [_field_to_schema(s) for s in sub]
    return res


def _out_to_json(o):
    """<out name type required default>[<field.../] → outputs 项（含 object 子字段 schema）
    default 值可从 default= 属性或标签内文本读取（如 <out name="x" type="integer">10</out>）"""
    res = {"name": o.get("name"), "type": o.get("type", "string")}
    if o.get("description"):
        res["description"] = o.get("description")
    if o.get("required") == "true":
        res["required"] = True
    default_attr = o.get("default")
    if default_attr is not None:
        res["defaultValue"] = _parse_val(default_attr, res["type"])
    elif o.text and o.text.strip():
        res["defaultValue"] = _parse_val((o.text or "").strip(), res["type"])
    fields = o.findall("field")
    if fields:
        res["schema"] = [_field_to_schema(f) for f in fields]
    ref = o.get("ref")
    if ref:
        # 复合节点(21/28) 输出可带 input 引用（指向循环变量/body 节点字段）
        res["input"] = {"type": res.get("type", "string"), "value": _ref_input(ref)}
    return res


def _node_to_json(nd) -> dict:
    nid = nd.get("id")
    if not nid:
        raise WorkflowXmlError("节点缺少 id")
    ntype = _type_num(nd.get("type"))
    title = nd.get("title") or TYPE_NUM_TO_NAME.get(ntype, ntype)
    node = {"id": nid, "type": ntype,
            "data": {"nodeMeta": {"title": title}, "inputs": {}, "outputs": []}}
    if nd.get("x") is not None:
        node["x"] = _parse_val(nd.get("x"), "number")
    if nd.get("y") is not None:
        node["y"] = _parse_val(nd.get("y"), "number")
    inp = node["data"]["inputs"]
    out = node["data"]["outputs"]

    if ntype == "1":        # start：data.outputs = 工作流入参
        node["data"]["trigger_parameters"] = []
        out.extend(_out_to_json(o) for o in nd.findall("out"))
    elif ntype == "2":      # end：<out ref> = 返回变量
        inp["terminatePlan"] = "returnVariables"
        inp["inputParameters"] = [
            {"name": o.get("name"),
             "input": {"type": o.get("type", "string"),
                       "value": _ref_input(o.get("ref")) if o.get("ref")
                                else {"type": "literal",
                                      "content": _parse_val(o.get("literal") if o.get("literal") is not None
                                                            else (o.text or "").strip(), o.get("type", "string"))}}}
            for o in nd.findall("out")
        ]
    elif ntype == "3":      # llm：inputParameters + llmParam(prompt/systemPrompt/...)
        inp["inputParameters"] = [_in_param(i) for i in nd.findall("in")]
        inp["llmParam"] = [{"name": p.get("name"),
                            "input": {"type": p.get("type", "string"),
                                      "value": {"type": "literal", "content": _text_block(p)}}}
                           for p in nd.findall("param")]
        out.extend(_out_to_json(o) for o in nd.findall("out"))
    elif ntype == "5":      # code：inputParameters + code(CDATA) + outputs
        inp["inputParameters"] = [_in_param(i) for i in nd.findall("in")]
        code_el = nd.find("code")
        inp["code"] = _text_block(code_el)
        inp["language"] = int(code_el.get("language", "3")) if code_el is not None else 3
        out.extend(_out_to_json(o) for o in nd.findall("out"))
    elif ntype == "4":      # plugin：toolName + inputParameters + outputs
        node["data"]["toolName"] = nd.get("toolName")
        inp["inputParameters"] = [_in_param(i) for i in nd.findall("in")]
        out.extend(_out_to_json(o) for o in nd.findall("out"))
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
        out.extend(_out_to_json(o) for o in nd.findall("out"))
        if not out:
            out.append({"name": "output", "type": "string"})   # 默认 output（执行返回 {output}）
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
    elif ntype in ("58", "59"):  # tojson / fromjson：单 input + output（可带 object schema）
        inp["inputParameters"] = [_in_param(i) for i in nd.findall("in")]
        outs = [_out_to_json(o) for o in nd.findall("out")]
        if not outs:
            outs = [{"name": "output", "type": "object" if ntype == "59" else "string"}]
        out.extend(outs)
    elif ntype == "13":     # output emitter
        inp["inputParameters"] = [_in_param(i) for i in nd.findall("in")]
        c = nd.find("content")
        if c is not None:
            inp["content"] = {"type": "string", "value": {"type": "literal", "content": _text_block(c)}}
        out.append({"name": "output", "type": "string"})
    elif ntype == "20":     # LoopSetVariable：inputParameters[{name,left(ref循环变量),right(ref|literal)}]
        ips = []
        for i in nd.findall("in"):
            lt = i.get("left_type", "string")
            rt = i.get("right_type", "string")
            lv = i.get("left", "")
            rv = i.get("right", "")
            if rv.startswith("ref:"):
                rval = {"type": rt, "value": _ref_input(rv[4:])}
            else:
                rval = {"type": rt, "value": {"type": "literal", "content": _parse_val(rv, rt)}}
            ips.append({"name": i.get("name", "var"),
                        "left": {"type": lt, "value": _ref_input(lv)},
                        "right": rval})
        inp["inputParameters"] = ips
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
    elif ntype == "21":     # loop：loopType/loopCount/variableParameters/子图/batch(nth/filter)
        inp["inputParameters"] = [_in_param(i) for i in nd.findall("in")]
        for p in nd.findall("param"):
            nm = p.get("name")
            if nm == "loopType":
                inp["loopType"] = _text_block(p) or "array"
            elif nm == "loopCount":
                inp["loopCount"] = {"type": "integer", "value": {"type": "literal",
                                    "content": _parse_val(_text_block(p), "integer")}}
        inp["variableParameters"] = [
            {"name": v.get("name", ""), "input": {"type": v.get("type", "string"),
                "value": _val_of_in(v)}} for v in nd.findall("var")]
        out.extend(_out_to_json(o) for o in nd.findall("out"))
        _read_composite_body(nd, node)
    elif ntype == "28":     # batch：batchSize/concurrentSize/子图/batch(nth/filter)
        inp["inputParameters"] = [_in_param(i) for i in nd.findall("in")]
        for p in nd.findall("param"):
            nm = p.get("name")
            if nm in ("batchSize", "concurrentSize"):
                inp[nm] = {"type": "integer", "value": {"type": "literal",
                           "content": _parse_val(_text_block(p), "integer")}}
        out.extend(_out_to_json(o) for o in nd.findall("out"))
        _read_composite_body(nd, node)
    else:
        # 通用兜底：in→inputParameters, out→outputs, param→inputs[name]
        inp["inputParameters"] = [_in_param(i) for i in nd.findall("in")]
        out.extend({"name": o.get("name"), "type": o.get("type", "string")} for o in nd.findall("out"))
        for p in nd.findall("param"):
            inp[p.get("name")] = _text_block(p)
    # 节点级批处理配置（任意节点都可能带 batch）
    b = _read_batch_el(nd)
    if b:
        inp["batch"] = b
    return node


# ========== 反向：Coze JSON → XML（保存时用）==========
from xml.sax.saxutils import quoteattr, escape as _xml_escape


def _qa(s):
    return quoteattr("" if s is None else str(s))


def _cdata(text):
    # CDATA 内若含 ]]> 需拆分（极罕见），这里简单处理
    return "<![CDATA[" + ("" if text is None else str(text)) + "]]>"


def _schema_to_xml(schema):
    """object 输出字段的 schema → <field name type>[<field.../]（递归嵌套 object）"""
    parts = []
    for s in schema or []:
        if not isinstance(s, dict):
            continue
        sa = f'name={_qa(s.get("name", ""))} type={_qa(s.get("type", "string"))}'
        sub = s.get("schema")
        if s.get("type") == "object" and isinstance(sub, list) and sub:
            parts.append(f'<field {sa}>{_schema_to_xml(sub)}</field>')
        else:
            parts.append(f'<field {sa}/>')
    return "".join(parts)


def _ref_of(block_input):
    """BlockInput → ref 字符串（block-output/loop-item/loop-index/global），否则空串"""
    v = block_input.get("value", block_input) if isinstance(block_input, dict) else {}
    if isinstance(v, dict) and v.get("type") == "ref":
        c = v.get("content", {}) or {}
        src = c.get("source")
        if src == "loop-item":
            return "loop-item" + (("." + c.get("name", "")) if c.get("name") else "")
        if src == "loop-index":
            return "loop-index"
        if src in ("global_variable_app", "global_variable_system", "global_variable_user"):
            path = c.get("path") or []
            return "global:" + ".".join(str(p) for p in path)
        return f'{c.get("blockID", "")}.{c.get("name", "")}'.strip(".")
    return ""


def _lit_of(block_input):
    v = block_input.get("value", block_input) if isinstance(block_input, dict) else {}
    return v.get("content", "") if isinstance(v, dict) else ""


def _in_to_xml(p):
    name = p.get("name", "")
    inp = p.get("input", {}) or {}
    typ = inp.get("type", "string")
    ref = _ref_of(inp)
    sch = inp.get("schema")
    attrs = f'name={_qa(name)} type={_qa(typ)}'
    if ref:
        attrs += f' ref={_qa(ref)}'
        if isinstance(sch, list) and sch:
            return f'<in {attrs}>{_schema_to_xml(sch)}</in>'
        if isinstance(sch, dict) and sch.get("type"):
            return f'<in {attrs} itemType={_qa(sch["type"])}/>'
        return f'<in {attrs}/>'
    # 字面量：有 schema 子元素时仍用 literal= 属性；简单值用标签内文本（更干净）
    lit_val = _lit_of(inp)
    if isinstance(sch, list) and sch:
        return f'<in {attrs} literal={_qa(lit_val)}>{_schema_to_xml(sch)}</in>'
    if isinstance(sch, dict) and sch.get("type"):
        return f'<in {attrs} literal={_qa(lit_val)} itemType={_qa(sch["type"])}/>'
    return f'<in {attrs}>{_xml_escape(str(lit_val))}</in>'


def _cond_to_xml(c):
    op = c.get("operator", "")
    left = (c.get("left") or {}).get("input", {}) or {}
    right = (c.get("right") or {}).get("input", {}) or {}
    lref = _ref_of(left)
    rref = _ref_of(right)
    lt = left.get("type") or "string"
    rt = right.get("type") or "string"
    r = f"ref:{rref}" if rref else str(_lit_of(right))
    a = f'op={_qa(op)} left={_qa(lref)} right={_qa(r)}'
    if lt != "string":
        a += f' left_type={_qa(lt)}'
    if rt != "string":
        a += f' right_type={_qa(rt)}'
    return f'<cond {a}/>'


def _composite_body_xml(n):
    """复合节点(21/28) 的子图 → ['<blocks>...</blocks>', '<edges>...</edges>']（递归 _node_to_xml）。"""
    parts = []
    blocks = n.get("blocks") or []
    edges = n.get("edges") or []
    if blocks:
        parts.append("<blocks>")
        for b in blocks:
            parts.append(_node_to_xml(b))
        parts.append("</blocks>")
    if edges:
        parts.append("<edges>")
        for e in edges:
            attrs = f"source={_qa(str(e.get('sourceNodeID', '')))} target={_qa(str(e.get('targetNodeID', '')))}"
            if e.get("sourcePortID"):
                attrs += f" sourcePort={_qa(str(e['sourcePortID']))}"
            if e.get("targetPortID"):
                attrs += f" targetPort={_qa(str(e['targetPortID']))}"
            parts.append(f"<edge {attrs}/>")
        parts.append("</edges>")
    return parts


def _batch_to_xml(b):
    """节点级/复合节点 batch 配置 → <batch enabled nth itemType><input/><filter/></batch>。"""
    if not isinstance(b, dict):
        return ""
    head = f'<batch enabled={_qa("true" if b.get("enabled") else "false")}'
    if "nth" in b:
        head += f" nth={_qa(str(b.get('nth')))}"
    if b.get("itemType"):
        head += f" itemType={_qa(b['itemType'])}"
    inner = []
    bi = b.get("input")
    if bi:
        inner.append(f'<input ref={_qa(_ref_of(bi))} type={_qa((bi or {}).get("type", "list"))}/>')
    f = b.get("filter")
    if f:
        inner.append(f'<filter logic="{int(f.get("logic", 2))}">')
        inner.extend(_cond_to_xml(c) for c in f.get("conditions", []))
        inner.append("</filter>")
    if inner:
        return head + ">" + "".join(inner) + "</batch>"
    return head + "/>"


def _node_to_xml(n):
    nid = str(n.get("id", ""))
    ntype = str(n.get("type", ""))
    name = TYPE_NUM_TO_NAME.get(ntype, ntype)
    data = n.get("data", {}) or {}
    inp = data.get("inputs", {}) or {}
    out = data.get("outputs", []) or []
    title = (data.get("nodeMeta") or {}).get("title", name)
    attrs = f'id={_qa(nid)} type={_qa(name)} title={_qa(title)}'
    if n.get("x") is not None:
        attrs += f' x={_qa(n["x"])}'
    if n.get("y") is not None:
        attrs += f' y={_qa(n["y"])}'
    inner = []

    def out_el(o):
        attrs = f'name={_qa(o.get("name",""))} type={_qa(o.get("type","string"))}'
        if o.get("required"):
            attrs += ' required="true"'
        if isinstance(o.get("input"), dict):
            r = _ref_of(o["input"])
            if r:
                attrs += f' ref={_qa(r)}'
        sch = o.get("schema")
        has_default = "defaultValue" in o
        if o.get("type") == "object" and isinstance(sch, list) and sch:
            inner = _schema_to_xml(sch)
            if has_default:
                return f'<out {attrs} default={_qa(o["defaultValue"])}>{inner}</out>'
            return f'<out {attrs}>{inner}</out>'
        if has_default:
            return f'<out {attrs}>{_xml_escape(str(o["defaultValue"]))}</out>'
        return f'<out {attrs}/>'

    if ntype == "1":
        inner.extend(out_el(o) for o in out)
    elif ntype == "2":
        for p in inp.get("inputParameters", []):
            pi = p.get("input", {}) or {}
            ref = _ref_of(pi)
            typ = pi.get("type", "string")
            if ref:
                inner.append(f'<out name={_qa(p.get("name",""))} ref={_qa(ref)} type={_qa(typ)}/>')
            else:
                lit = _lit_of(pi)
                inner.append(f'<out name={_qa(p.get("name",""))} type={_qa(typ)}>{_xml_escape(str(lit))}</out>')
    elif ntype == "3":
        inner.extend(_in_to_xml(p) for p in inp.get("inputParameters", []))
        for p in inp.get("llmParam", []):
            pi = p.get("input", {}) or {}
            inner.append(f'<param name={_qa(p.get("name",""))} type={_qa(pi.get("type","string"))}>{_cdata(_lit_of(pi))}</param>')
        inner.extend(out_el(o) for o in out)
    elif ntype == "5":
        inner.extend(_in_to_xml(p) for p in inp.get("inputParameters", []))
        inner.append(f'<code language="{inp.get("language", 3)}">{_cdata(inp.get("code", ""))}</code>')
        inner.extend(out_el(o) for o in out)
    elif ntype == "4":
        attrs += f' toolName={_qa(data.get("toolName", ""))}'
        inner.extend(_in_to_xml(p) for p in inp.get("inputParameters", []))
        inner.extend(out_el(o) for o in out)
    elif ntype == "15":
        inner.extend(_in_to_xml(p) for p in inp.get("inputParameters", []))
        cr = next((p for p in inp.get("concatParams", []) if p.get("name") == "concatResult"), None)
        if cr:
            inner.append(f'<result>{_cdata(_lit_of(cr.get("input", {})))}</result>')
    elif ntype == "8":
        for br in inp.get("branches", []):
            cs = (br.get("condition") or {}).get("conditions", [])
            inner.append("<branch>" + "".join(_cond_to_xml(c) for c in cs) + "</branch>" if cs else "<branch/>")
    elif ntype == "32":
        for g in inp.get("mergeGroups", []):
            vs = "".join(f'<var ref={_qa(_ref_of(v.get("value", v)))}/>' for v in g.get("variables", []))
            inner.append(f'<group name={_qa(g.get("name",""))}>{vs}</group>')
    elif ntype == "22":
        inner.extend(_in_to_xml(p) for p in inp.get("inputParameters", []))
        inner.extend(f'<intent name={_qa(it.get("name",""))}/>' for it in inp.get("intents", []))
    elif ntype == "9":
        attrs += f' workflowId={_qa(inp.get("workflowId", ""))}'
        inner.extend(_in_to_xml(p) for p in inp.get("inputParameters", []))
        inner.extend(out_el(o) for o in out)
    elif ntype == "45":
        api = inp.get("apiInfo", {}) or {}
        inner.append(f'<method>{_xml_escape(api.get("method", "GET"))}</method>')
        inner.append(f'<url>{_cdata(api.get("url", ""))}</url>')
        for h in inp.get("headers", []):
            inner.append(f'<header name={_qa(h.get("name",""))} value={_qa(_lit_of(h.get("input", {})))}/>')
        body = inp.get("body", {}) or {}
        if body.get("bodyType") and body.get("bodyType") != "EMPTY":
            inner.append(f'<body type={_qa(body.get("bodyType","JSON"))}>{_cdata((body.get("bodyData") or {}).get("json",""))}</body>')
    elif ntype in ("58", "59"):
        inner.extend(_in_to_xml(p) for p in inp.get("inputParameters", []))
        inner.extend(out_el(o) for o in out)
    elif ntype == "13":
        c = inp.get("content")
        if c:
            inner.append(f'<content>{_cdata(_lit_of(c))}</content>')
    elif ntype == "40":
        for p in inp.get("inputParameters", []):
            path = ((p.get("left") or {}).get("value", {}).get("content", {}).get("path") or [""])[0]
            inner.append(f'<in name={_qa(p.get("name","var"))} path={_qa(path)} literal={_qa(_lit_of(p.get("input", {})))}/>')
    elif ntype == "20":   # LoopSetVariable：<in name left left_type right right_type/>
        for p in inp.get("inputParameters", []):
            l_in = p.get("left", {}) or {}
            r_in = p.get("right", {}) or {}
            lref = _ref_of(l_in)
            rref = _ref_of(r_in)
            r = ("ref:" + rref) if rref else _lit_of(r_in)
            inner.append(f'<in name={_qa(p.get("name","var"))} left={_qa(lref)} left_type={_qa(l_in.get("type","string"))} right={_qa(r)} right_type={_qa(r_in.get("type","string"))}/>')
    elif ntype == "21":   # loop
        if inp.get("loopType") is not None:
            inner.append(f'<param name="loopType">{_cdata(str(inp.get("loopType", "array")))}</param>')
        if "loopCount" in inp:
            inner.append(f'<param name="loopCount" type="integer">{_cdata(_lit_of(inp["loopCount"]))}</param>')
        for vp in inp.get("variableParameters", []):
            pi = vp.get("input", {}) or {}
            inner.append(f'<var name={_qa(vp.get("name", ""))} type={_qa(pi.get("type", "string"))} literal={_qa(_lit_of(pi))}/>')
        inner.extend(_in_to_xml(p) for p in inp.get("inputParameters", []))
        inner.extend(out_el(o) for o in out)
        inner.extend(_composite_body_xml(n))
        if isinstance(inp.get("batch"), dict):
            inner.append(_batch_to_xml(inp["batch"]))
    elif ntype == "28":   # batch
        if "batchSize" in inp:
            inner.append(f'<param name="batchSize" type="integer">{_cdata(_lit_of(inp["batchSize"]))}</param>')
        if "concurrentSize" in inp:
            inner.append(f'<param name="concurrentSize" type="integer">{_cdata(_lit_of(inp["concurrentSize"]))}</param>')
        inner.extend(_in_to_xml(p) for p in inp.get("inputParameters", []))
        inner.extend(out_el(o) for o in out)
        inner.extend(_composite_body_xml(n))
        if isinstance(inp.get("batch"), dict):
            inner.append(_batch_to_xml(inp["batch"]))
    else:
        inner.extend(_in_to_xml(p) for p in inp.get("inputParameters", []))
        inner.extend(out_el(o) for o in out)

    # 节点级批处理（任意普通节点）：即使 inner 为空也输出 batch
    if ntype not in ("21", "28") and isinstance(inp.get("batch"), dict):
        inner.append(_batch_to_xml(inp["batch"]))

    if not inner:
        return f"  <node {attrs}/>"
    body = "\n    ".join(inner)
    return f"  <node {attrs}>\n    {body}\n  </node>"


def canvas_to_xml(canvas: dict, meta: dict = None) -> str:
    """Coze 画布 JSON → XML 字符串。meta 放 <workflow> 根属性。"""
    meta = meta or {}
    attrs = f'name={_qa(meta.get("name", ""))} description={_qa(meta.get("description", ""))}'
    if meta.get("coze_url"):
        attrs += f' coze_url={_qa(meta["coze_url"])}'
    if meta.get("auto"):
        attrs += ' auto="true"'
        if meta.get("auto_param"):
            attrs += f' auto_param={_qa(meta["auto_param"])}'
    lines = [f"<workflow {attrs}>"]
    for n in canvas.get("nodes", []):
        lines.append(_node_to_xml(n))
    for e in canvas.get("edges", []):
        port = e.get("sourcePortID", "") or ""
        ea = f'from={_qa(str(e.get("sourceNodeID","")))} to={_qa(str(e.get("targetNodeID","")))}'
        if port:
            ea += f' port={_qa(port)}'
        lines.append(f"  <edge {ea}/>")
    lines.append("</workflow>")
    return "\n".join(lines) + "\n"

