# verify_loop_batch.py —— 循环/批处理/单节点批处理 + XML 往返 验证脚本（临时，不入库）
# 运行: python verify_loop_batch.py
import json
import sys
from copy import deepcopy
from types import SimpleNamespace

sys.path.insert(0, "src")
sys.stdout.reconfigure(encoding="utf-8")

import workflow
from workflow_xml import xml_to_canvas, canvas_to_xml


class MockLLM:
    def chat(self, msgs, **kw):
        user = [m for m in msgs if m.get("role") == "user"]
        last = user[-1]["content"] if user else ""
        return SimpleNamespace(content="LLM(" + last + ")")


class MockTools:
    def __contains__(self, name):
        return name in ("to_upper", "wrap")

    def call(self, name, args):
        if name == "to_upper":
            return str(args.get("text", "")).upper()
        if name == "wrap":
            return "《" + str(args.get("text", "")) + "》"
        raise workflow.WorkflowError("未知工具 " + name)


def run(canvas, inputs):
    return workflow.execute(canvas, inputs, tools=MockTools(), llm=MockLLM(),
                            return_exit_dict=True)


def ref(bid, name):
    return {"type": "ref", "content": {"source": "block-output", "blockID": bid, "name": name}}


def lit(v):
    return {"type": "literal", "content": v}


def in_param(name, typ, value):
    return {"name": name, "input": {"type": typ, "value": value}}


PASS, FAIL = [], []


def check(tag, cond, detail=""):
    (PASS if cond else FAIL).append(tag)
    print(("  ✅ " if cond else "  ❌ ") + tag + (("  -> " + str(detail)[:200]) if detail else ""))


# ============ T1: 存量 loop_greet（LLM→text→置变量 三块子画布）============
print("\n== T1 循环(loop_greet.json): 子画布 LLM→文本→置变量 ==")
canvas = json.load(open(".agent/workflows/loop_greet.json", encoding="utf-8"))
r = run(canvas, {"names": ["小明", "小红"]})
greet = r.get("result", "")
check("循环跑完且 greetings 非空", bool(greet), greet)
check("两轮都拼进了累加变量", greet.count("LLM(") == 2, greet)

# ============ T2: 存量 batch_double（批处理收集 xxx_list）============
print("\n== T2 批处理(batch_double.json): 单 code 块收集 doubled_list ==")
canvas = json.load(open(".agent/workflows/batch_double.json", encoding="utf-8"))
r = run(canvas, {"nums": [1, 2, 3]})
check("doubled_list == [2,4,6]", r.get("result") == [2, 4, 6], r.get("result"))

# ============ T3: 循环子画布多工具组合（code→plugin→text→置变量，item 为 object）============
print("\n== T3 循环子画布多工具组合: code→plugin→text→置变量, item=object, 用 loop-item/loop-index ==")
LOOP = "200001"
loop_multi = {
    "nodes": [
        {"id": "100001", "type": "1", "data": {"outputs": [
            {"name": "items", "type": "list", "schema": [
                {"name": "word", "type": "string"}], "required": True}], "trigger_parameters": []}},
        {"id": LOOP, "type": "21", "data": {
            "inputs": {
                "loopType": "array",
                "inputParameters": [in_param("input", "list", ref("100001", "items"))],
                "variableParameters": [in_param("acc", "string", lit(""))]},
            "outputs": [{"name": "result", "input": {"type": "string", "value": ref(LOOP, "acc")}}]},
         "blocks": [
            # c1: code 把 item.word 加上序号（input.word 点号 + loop-index）
            {"id": "200011", "type": "5", "data": {
                "inputs": {
                    "inputParameters": [
                        in_param("w", "string", ref(LOOP, "input.word")),
                        {"name": "idx", "input": {"type": "integer",
                            "value": {"type": "ref", "content": {"source": "loop-index"}}}}],
                    "code": "async def main(args):\n    return {'shouted': args.params['w'] + '#' + str(args.params['idx'])}",
                    "language": 3},
                "outputs": [{"name": "shouted", "type": "string"}]}},
            # p1: plugin to_upper（loop-item 点号取 word）
            {"id": "200012", "type": "4", "data": {
                "toolName": "to_upper",
                "inputs": {"inputParameters": [
                    in_param("text", "string",
                             {"type": "ref", "content": {"source": "loop-item", "name": "word"}})]},
                "outputs": [{"name": "raw", "type": "string"}]}},
            # t1: text 拼接 acc + code结果 + plugin结果
            {"id": "200013", "type": "15", "data": {
                "inputs": {
                    "method": "concat",
                    "inputParameters": [
                        in_param("acc", "string", ref(LOOP, "acc")),
                        in_param("a", "string", ref("200011", "shouted")),
                        in_param("b", "string", ref("200012", "raw"))],
                    "concatParams": [{"name": "concatResult", "input": {
                        "type": "string", "value": lit("{{acc}}|{{a}}|{{b}}")}}]},
                "outputs": [{"name": "output", "type": "string"}]}},
            # s1: 置变量 acc = t1.output
            {"id": "200014", "type": "20", "data": {
                "inputs": {"inputParameters": [{
                    "name": "acc",
                    "left": {"type": "string", "value": ref(LOOP, "acc")},
                    "right": {"type": "string", "value": ref("200013", "output")}}]}}},
         ],
         "edges": [
            {"sourceNodeID": LOOP, "targetNodeID": "200011", "sourcePortID": "loop-function-inline-output"},
            {"sourceNodeID": "200011", "targetNodeID": "200012", "sourcePortID": ""},
            {"sourceNodeID": "200012", "targetNodeID": "200013", "sourcePortID": ""},
            {"sourceNodeID": "200013", "targetNodeID": "200014", "sourcePortID": ""},
            {"sourceNodeID": "200014", "targetNodeID": LOOP, "sourcePortID": "", "targetPortID": "loop-function-inline-input"},
         ]},
        {"id": "900001", "type": "2", "data": {"inputs": {"terminatePlan": "returnVariables",
            "inputParameters": [in_param("result", "string", ref(LOOP, "result"))]}}},
    ],
    "edges": [
        {"sourceNodeID": "100001", "targetNodeID": LOOP, "sourcePortID": ""},
        {"sourceNodeID": LOOP, "targetNodeID": "900001", "sourcePortID": ""},
    ],
}
r = run(loop_multi, {"items": [{"word": "ab"}, {"word": "cd"}]})
res = r.get("result", "")
check("循环多工具链跑完", bool(res), res)
check("code 的 index 注入正确 (#0/#1)", "#0" in res and "#1" in res, res)
check("plugin 大写生效 (AB/CD)", "AB" in res and "CD" in res, res)
check("累加变量跨轮累积 (两轮分隔符|)", res.count("|") >= 4, res)

# ============ T4: 批处理子画布多工具组合（code→plugin→text，收集 result_list）============
print("\n== T4 批处理子画布多工具组合: code→plugin→text, 收集 result_list ==")
BAT = "210001"
batch_multi = deepcopy(loop_multi)
# 改循环为批处理
bat_node = None
for n in batch_multi["nodes"]:
    if n["id"] == LOOP:
        bat_node = n
bat_node["id"] = BAT
bat_node["type"] = "28"
bat_node["data"]["inputs"] = {
    "batchSize": {"type": "integer", "value": lit(10)},
    "concurrentSize": {"type": "integer", "value": lit(1)},
    "inputParameters": [in_param("input", "list", ref("100001", "items"))],
}
bat_node["data"]["outputs"] = [
    {"name": "result_list", "type": "list", "schema": {"type": "string"},
     "input": {"type": "string", "value": ref("200013", "output")}}]
for b in bat_node["blocks"]:
    for p in (b.get("data", {}).get("inputs", {}) or {}).get("inputParameters", []):
        v = (p.get("input") or {}).get("value", {})
        c = v.get("content") if isinstance(v, dict) else None
        if isinstance(c, dict) and c.get("blockID") == LOOP:
            c["blockID"] = BAT
# 批处理没有 variableParameters/acc：把 t1 的 acc 入参换成字面量
t1 = next(b for b in bat_node["blocks"] if b["id"] == "200013")
t1["data"]["inputs"]["inputParameters"][0]["input"]["value"] = lit("")
t1["data"]["inputs"]["concatParams"][0]["input"]["value"] = lit("{{a}}~{{b}}")
# 批处理体去掉置变量块，t1 直接回到 composite
bat_node["blocks"] = [b for b in bat_node["blocks"] if b["id"] != "200014"]
bat_node["edges"] = [
    {"sourceNodeID": BAT, "targetNodeID": "200011", "sourcePortID": "batch-function-inline-output"},
    {"sourceNodeID": "200011", "targetNodeID": "200012", "sourcePortID": ""},
    {"sourceNodeID": "200012", "targetNodeID": "200013", "sourcePortID": ""},
    {"sourceNodeID": "200013", "targetNodeID": BAT, "sourcePortID": "", "targetPortID": "batch-function-inline-input"},
]
for n in batch_multi["nodes"]:
    if n["id"] == "900001":
        n["data"]["inputs"]["inputParameters"][0]["input"]["value"] = ref(BAT, "result_list")
for e in batch_multi["edges"]:
    if e["sourceNodeID"] == LOOP:
        e["sourceNodeID"] = BAT
    if e["targetNodeID"] == LOOP:
        e["targetNodeID"] = BAT
r = run(batch_multi, {"items": [{"word": "ab"}, {"word": "cd"}, {"word": "ef"}]})
rl = r.get("result")
check("批处理收集 3 轮结果", isinstance(rl, list) and len(rl) == 3, rl)
check("每轮都是 code→plugin→text 链产物", rl == ["ab#0~AB", "cd#1~CD", "ef#2~EF"], rl)

# ============ T5: 单节点级批处理（code 节点 batch.enabled + filter + nth）============
print("\n== T5 单节点级批处理: code 节点逐元素执行 + 筛选 + nth ==")
code_batch = {
    "nodes": [
        {"id": "100001", "type": "1", "data": {"outputs": [
            {"name": "nums", "type": "list", "schema": {"type": "integer"}, "required": True}],
            "trigger_parameters": []}},
        {"id": "220001", "type": "5", "data": {
            "inputs": {
                "inputParameters": [
                    {"name": "n", "input": {"type": "integer",
                        "value": {"type": "ref", "content": {"source": "loop-item", "name": ""}}}}],
                "code": "async def main(args):\n    return {'doubled': args.params['n'] * 10}",
                "language": 3,
                "batch": {
                    "enabled": True,
                    "input": {"type": "list", "value": ref("100001", "nums")},
                    "itemType": "integer",
                    "filter": {"logic": 2, "conditions": [{
                        "operator": 14,
                        "left": {"input": {"type": "integer", "value": {
                            "type": "ref", "content": {"source": "block-output",
                                                       "blockID": "__batch_output__", "name": "doubled"}}}},
                        "right": {"input": {"type": "integer", "value": lit(30)}}}]},
                    "nth": 0}},
            "outputs": [
                {"name": "all_outputs", "type": "list"},
                {"name": "filtered_outputs", "type": "list"},
                {"name": "nth_output", "type": "object"}]}},
        {"id": "900001", "type": "2", "data": {"inputs": {"terminatePlan": "returnVariables",
            "inputParameters": [
                in_param("all", "list", ref("220001", "all_outputs")),
                in_param("filtered", "list", ref("220001", "filtered_outputs")),
                in_param("nth", "object", ref("220001", "nth_output"))]}}},
    ],
    "edges": [
        {"sourceNodeID": "100001", "targetNodeID": "220001", "sourcePortID": ""},
        {"sourceNodeID": "220001", "targetNodeID": "900001", "sourcePortID": ""},
    ],
}
r = run(code_batch, {"nums": [1, 2, 3, 4]})
check("all_outputs 4 轮 [10,20,30,40]",
      [o.get("doubled") for o in r.get("all", [])] == [10, 20, 30, 40], r.get("all"))
check("filtered_outputs 只留 ≥30", [o.get("doubled") for o in r.get("filtered", [])] == [30, 40],
      r.get("filtered"))
check("nth_output = 第一个 filtered", (r.get("nth") or {}).get("doubled") == 30, r.get("nth"))

# ============ T6: 编辑器模板形态的 loop（all/filtered/nth 无 input 引用）============
print("\n== T6 编辑器模板形态 loop（输出无 input 引用）——验证执行器行为 ==")
editor_loop = deepcopy(loop_multi)
for n in editor_loop["nodes"]:
    if n["id"] == LOOP:
        n["data"]["outputs"] = [
            {"name": "all_outputs", "type": "list"},
            {"name": "filtered_outputs", "type": "list"},
            {"name": "nth_output", "type": "object"}]
        n["data"]["inputs"]["batch"] = {"nth": 0}
    if n["id"] == "900001":
        n["data"]["inputs"]["inputParameters"] = [
            in_param("all", "list", ref(LOOP, "all_outputs")),
            in_param("nth", "object", ref(LOOP, "nth_output"))]
r = run(editor_loop, {"items": [{"word": "ab"}, {"word": "cd"}]})
print("  编辑器形态 loop 实际输出:", json.dumps(r, ensure_ascii=False)[:200])
check("【已知差异】all_outputs 非空才算编辑器形态可用", bool(r.get("all")), r.get("all"))

# ============ T7: XML 往返 ============
print("\n== T7 XML 序列化/反序列化往返 ==")
for tag, path in [("循环 loop_greet", ".agent/workflows/loop_greet.json"),
                  ("批处理 batch_double", ".agent/workflows/batch_double.json")]:
    canvas = json.load(open(path, encoding="utf-8"))
    comp = next(n for n in canvas["nodes"] if str(n["type"]) in ("21", "28"))
    xml = canvas_to_xml(canvas, {"name": tag, "description": ""})
    back = xml_to_canvas(xml)
    comp2 = next((n for n in back["nodes"] if n["id"] == comp["id"]), None)
    b1, b2 = len(comp.get("blocks") or []), len((comp2 or {}).get("blocks") or [])
    e1, e2 = len(comp.get("edges") or []), len((comp2 or {}).get("edges") or [])
    vp1 = len((comp.get("data", {}).get("inputs") or {}).get("variableParameters") or [])
    vp2 = len(((comp2 or {}).get("data", {}).get("inputs") or {}).get("variableParameters") or [])
    lt1 = (comp.get("data", {}).get("inputs") or {}).get("loopType")
    lt2 = ((comp2 or {}).get("data", {}).get("inputs") or {}).get("loopType")
    print(f"  [{tag}] blocks {b1}→{b2}, edges {e1}→{e2}, variableParameters {vp1}→{vp2}, loopType {lt1!r}→{lt2!r}")
    check(f"{tag}: 子画布 blocks/edges 往返保留", (b1, e1) == (b2, e2), f"{b1},{e1} → {b2},{e2}")
    check(f"{tag}: variableParameters/loopType 保留", vp1 == vp2 and lt1 == lt2, f"vp {vp1}→{vp2}, loopType {lt1}→{lt2}")

# 单节点批处理配置的 XML 往返
xml = canvas_to_xml(code_batch, {"name": "node_batch", "description": ""})
back = xml_to_canvas(xml)
nb = next((n for n in back["nodes"] if n["id"] == "220001"), None)
b_cfg = ((nb or {}).get("data", {}).get("inputs") or {}).get("batch") or {}
check("单节点批处理: batch.enabled 往返保留", b_cfg.get("enabled") is True, b_cfg)
check("单节点批处理: batch.filter/nth 保留", bool(b_cfg.get("filter")) and "nth" in b_cfg,
      {"filter": bool(b_cfg.get("filter")), "nth": b_cfg.get("nth")})

# ============ 汇总 ============
print("\n======== 汇总 ========")
print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
if FAIL:
    print("失败项:", FAIL)
