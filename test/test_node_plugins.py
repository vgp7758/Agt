#!/usr/bin/env python3
"""test_node_plugins.py —— 节点插件化（node_plugins.py）全场景验证。
运行：python test/test_node_plugins.py（仓库根执行）

七场景（spec s_029e4453 第 5 步）：
① 扫描注册：builtin 3（15/58/59）+ .agent/nodes 的 N1，py/js 配对齐全无警告
② 对拍执行：三迁移节点 handler 行为与原内置一致（concat/split/tojson/fromjson 往返）
③ XML 往返：type N1 + 字段保留（未知类型走通用序列化）
④ server 注入：编辑器 + debug 两页含 shim/插件 js/sourceURL/defaults，顺序在主 script 后
⑤ 热加载：reload 摘旧重挂；mtime 缓存变更生效
⑥ 交叉校验：js 无配对 py 拒绝注入告警 / py 无 js 告警但 handler 照注册
⑦ 核心类型保护：CORE_TYPES 覆盖尝试被拒
"""
import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

# run_python 可能同进程——清旧缓存确保用最新源码
for _m in ("node_plugins", "workflow", "server", "workflow_xml", "workflow_node_api"):
    sys.modules.pop(_m, None)

PASS = 0
def check(name, ok, extra=""):
    global PASS
    print(("✅" if ok else "❌") + f" {name}" + (f" {extra}" if extra and not ok else ""))
    if ok:
        PASS += 1
    else:
        sys.exit(1)


# ===== ① 扫描注册 =====
import node_plugins as NP

res = NP.scan_node_plugins()
hs = res["handlers"]
SECOND = {"3", "5", "8", "15", "22", "32", "40", "45", "58", "59"}   # 第二批迁移
check("① 注册 11 类（第一批 3 + 第二批 7 + N1）", set(hs) >= (SECOND | {"N1"}), str(sorted(hs)))
check("① py/js 配对齐全（无警告）",
      not [w for w in res["warnings"] if "无配对" in w], str(res["warnings"]))
check("① N1 handler 可调用", callable(hs["N1"]["handler"]))
check("① 第二批全部 py+js", SECOND <= {i["type"] for i in res["js"]},
      str(sorted({i["type"] for i in res["js"]})))

# ===== ② 对拍执行（三迁移节点 + N1）=====
import workflow as W

mini = {"nodes": [
  {"id": "100001", "type": "1", "data": {"outputs": [{"name": "n"}]}},
  {"id": "150001", "type": "15", "data": {"inputs": {"method": "concat",
      "inputParameters": [{"name": "n", "input": {"type": "ref", "value": {"type": "ref", "content": {"source": "block-output", "blockID": "100001", "name": "n"}}}}],
      "concatParams": [{"name": "concatResult", "input": {"type": "string", "value": {"type": "literal", "content": "hi {{n}}"}}}]},
      "outputs": [{"name": "output", "type": "string"}]}},
  {"id": "200001", "type": "N1", "data": {"inputs": {"inputParameters": []},
      "outputs": [{"name": "output", "type": "string"}]}},
  {"id": "900001", "type": "2", "data": {"inputs": {"inputParameters": [
      {"name": "a", "input": {"type": "ref", "value": {"type": "ref", "content": {"source": "block-output", "blockID": "150001", "name": "output"}}}},
      {"name": "b", "input": {"type": "ref", "value": {"type": "ref", "content": {"source": "block-output", "blockID": "200001", "name": "output"}}}}]}}},
], "edges": [
  {"sourceNodeID": "100001", "targetNodeID": "150001"},
  {"sourceNodeID": "150001", "targetNodeID": "900001"},
  {"sourceNodeID": "200001", "targetNodeID": "900001"},
]}
out = W.execute(mini, {"n": "插件"}, tools=None, llm=None, return_exit_dict=True)
check("② text concat 走插件 handler", out["a"] == "hi 插件", str(out))
check("② N1 输出时间串", bool(re.match(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", out["b"])), out["b"])

tjf = {"nodes": [
  {"id": "100001", "type": "1", "data": {"outputs": [{"name": "x"}]}},
  {"id": "580001", "type": "58", "data": {"inputs": {"inputParameters": [
      {"name": "input", "input": {"type": "ref", "value": {"type": "ref", "content": {"source": "block-output", "blockID": "100001", "name": "x"}}}}]},
      "outputs": [{"name": "output", "type": "string"}]}},
  {"id": "590001", "type": "59", "data": {"inputs": {"inputParameters": [
      {"name": "input", "input": {"type": "ref", "value": {"type": "ref", "content": {"source": "block-output", "blockID": "580001", "name": "output"}}}}]},
      "outputs": [{"name": "output", "type": "object"}]}},
  {"id": "900001", "type": "2", "data": {"inputs": {"inputParameters": [
      {"name": "o", "input": {"type": "ref", "value": {"type": "ref", "content": {"source": "block-output", "blockID": "590001", "name": "output"}}}}]}}},
], "edges": [
  {"sourceNodeID": "100001", "targetNodeID": "580001"},
  {"sourceNodeID": "580001", "targetNodeID": "590001"},
  {"sourceNodeID": "590001", "targetNodeID": "900001"},
]}
out2 = W.execute(tjf, {"x": {"k": [1, None]}}, tools=None, llm=None, return_exit_dict=True)
check("② tojson→fromjson 往返", out2 == {"o": {"k": [1, None]}}, str(out2))

# ===== ②b 第二批七节点 mock 回归（串联：code→selector→aggregator→assigner；intent/llm 单测）=====
class _MockResp:
    content = '{"ok": 1}'; reasoning = ""

class _MockLLM:
    model_name = "mock"
    def chat(self, msgs, **kw):
        return _MockResp()


def _cond(op, lref, rlit, rtype="number"):
    return {"logic": 2, "conditions": [{"operator": op,
        "left": {"input": lref},
        "right": {"input": {"type": rtype, "value": {"type": "literal", "content": rlit}}}}]}


chain = {"nodes": [
    {"id": "100001", "type": "1", "data": {"outputs": [{"name": "x", "type": "number"}]}},
    {"id": "500001", "type": "5", "data": {
        "inputs": {"language": 3, "inputParameters": [
            {"name": "x", "input": {"type": "number", "value": {"type": "ref", "content": {"source": "block-output", "blockID": "100001", "name": "x"}}}}],
            "code": "async def main(args):\n    return {'y': args.params['x'] * 2}"},
        "outputs": [{"name": "y", "type": "number"}]}},
    {"id": "800001", "type": "8", "data": {"inputs": {"branches": [
        {"condition": _cond(13, {"type": "number", "value": {"type": "ref", "content": {"source": "block-output", "blockID": "500001", "name": "y"}}}, 5)}]}}},
    {"id": "320001", "type": "32", "data": {"inputs": {"mergeGroups": [
        {"name": "pick", "variables": [
            {"value": {"type": "ref", "content": {"source": "block-output", "blockID": "500001", "name": "y"}}},
            {"value": {"type": "literal", "content": -1}}]}]},
        "outputs": [{"name": "pick", "type": "number"}]}},
    {"id": "400001", "type": "40", "data": {"inputs": {"inputParameters": [
        {"name": "input", "input": {"type": "number", "value": {"type": "ref", "content": {"source": "block-output", "blockID": "320001", "name": "pick"}}},
         "left": {"value": {"content": {"path": ["g_pick"]}}}}]}}},
    {"id": "900001", "type": "2", "data": {"inputs": {"inputParameters": [
        {"name": "out", "input": {"type": "number", "value": {"type": "ref", "content": {"source": "block-output", "blockID": "320001", "name": "pick"}}}}]}}},
], "edges": [
    {"sourceNodeID": "100001", "targetNodeID": "500001"},
    {"sourceNodeID": "500001", "targetNodeID": "800001"},
    {"sourceNodeID": "800001", "targetNodeID": "320001", "sourcePortID": "true"},
    {"sourceNodeID": "320001", "targetNodeID": "400001"},
    {"sourceNodeID": "400001", "targetNodeID": "900001"},
]}
o3 = W.execute(chain, {"x": 4}, tools=None, llm=_MockLLM(), return_exit_dict=True)
check("②b 串联 code→selector→aggregator→assigner", o3 == {"out": 8}, str(o3))

intnt = {"nodes": [
    {"id": "100001", "type": "1", "data": {"outputs": [{"name": "q", "type": "string"}]}},
    {"id": "220001", "type": "22", "data": {"inputs": {
        "inputParameters": [{"name": "query", "input": {"type": "string", "value": {"type": "ref", "content": {"source": "block-output", "blockID": "100001", "name": "q"}}}}],
        "intents": [{"name": "退款"}, {"name": "咨询"}]}}},
    {"id": "900001", "type": "2", "data": {"inputs": {"inputParameters": [
        {"name": "r", "input": {"type": "string", "value": {"type": "literal", "content": "is-refund"}}}]}}},
], "edges": [
    {"sourceNodeID": "100001", "targetNodeID": "220001"},
    {"sourceNodeID": "220001", "targetNodeID": "900001", "sourcePortID": "branch_0"},
]}
o4 = W.execute(intnt, {"q": "退款"}, tools=None, llm=_MockLLM(), return_exit_dict=True)
check("②b intent branch_0 路由", o4 == {"r": "is-refund"}, str(o4))

llmn = {"nodes": [
    {"id": "100001", "type": "1", "data": {"outputs": [{"name": "q", "type": "string"}]}},
    {"id": "300001", "type": "3", "data": {
        "inputs": {
            "inputParameters": [{"name": "q", "input": {"type": "string", "value": {"type": "ref", "content": {"source": "block-output", "blockID": "100001", "name": "q"}}}}],
            "llmParam": [{"name": "prompt", "input": {"type": "string", "value": {"type": "literal", "content": "问题：{{q}}"}}}]},
        "outputs": [{"name": "output", "type": "string"}]}},
    {"id": "900001", "type": "2", "data": {"inputs": {"inputParameters": [
        {"name": "out", "input": {"type": "string", "value": {"type": "ref", "content": {"source": "block-output", "blockID": "300001", "name": "output"}}}}]}}},
], "edges": [
    {"sourceNodeID": "100001", "targetNodeID": "300001"},
    {"sourceNodeID": "300001", "targetNodeID": "900001"},
]}
o5 = W.execute(llmn, {"q": "hi"}, tools=None, llm=_MockLLM(), return_exit_dict=True)
check("②b llm prompt 渲染+透传", o5 == {"out": '{"ok": 1}'}, str(o5))

# ===== ③ XML 往返（未知类型 N1 通用序列化）=====
from workflow_xml import canvas_to_xml, xml_to_canvas
xml = canvas_to_xml(mini, {"name": "t", "description": ""})
back = xml_to_canvas(xml)
n1 = next(n for n in back["nodes"] if n["id"] == "200001")
check("③ XML 往返：N1 type + output 保留",
      str(n1["type"]) == "N1" and n1["data"]["outputs"][0]["name"] == "output")

# ===== ④ server 两页注入 =====
import server as S
for html, tag in ((S._EDITOR_HTML, "editor"), (S._WF_DEBUG_HTML, "debug")):
    ok = ("节点插件注入 shim" in html and "sourceURL=nodes/timestamp.js" in html
          and html.find("EdFW.register") > html.find("function renderAll" if tag == "editor" else "<body"))
    check(f"④ {tag} 页注入（shim+js+sourceURL+顺序）", ok)

# ===== ⑤ 热加载（mtime 失效 + 摘旧）=====
with tempfile.TemporaryDirectory() as td:
    tdp = Path(td)
    (tdp / "tmpnode.py").write_text(
        'def _h(node, ctx):\n    return {"outputs": {"v": 1}, "port": None}\n\n'
        'def agt_node():\n    return {"type": "T9", "label": "tmp", "handler": _h}\n', encoding="utf-8")
    (tdp / "tmpnode.js").write_text('EdFW.register({type:"T9",label:"tmp",icon:"t"});', encoding="utf-8")
    r1 = NP.reload_node_plugins(W.NODE_HANDLERS, dirs=[tdp])   # 换目录：builtin 全摘（含 N1）
    check("⑤ reload 换目录摘旧", "T9" in W.NODE_HANDLERS and "N1" not in W.NODE_HANDLERS, r1)
    # 改文件内容 → mtime 失效 → 新 handler 生效
    import time as _t; _t.sleep(0.05)
    (tdp / "tmpnode.py").write_text(
        'def _h(node, ctx):\n    return {"outputs": {"v": 2}, "port": None}\n\n'
        'def agt_node():\n    return {"type": "T9", "label": "tmp", "handler": _h}\n', encoding="utf-8")
    NP.reload_node_plugins(W.NODE_HANDLERS, dirs=[tdp])
    r2 = W.NODE_HANDLERS["T9"]({"data": {}}, None)
    check("⑤ mtime 变更热生效（v=2）", r2["outputs"]["v"] == 2, str(r2))
    # 还原：重挂默认目录
    NP.reload_node_plugins(W.NODE_HANDLERS)
    check("⑤ 还原默认目录（N1 回归）", "N1" in W.NODE_HANDLERS and "15" in W.NODE_HANDLERS)

# ===== ⑥ 交叉校验 =====
with tempfile.TemporaryDirectory() as td:
    tdp = Path(td)
    (tdp / "onlyjs.js").write_text('EdFW.register({type:"Z9"});', encoding="utf-8")
    (tdp / "onlypy.py").write_text(
        'def _h(node, ctx):\n    return {"outputs": {}, "port": None}\n\n'
        'def agt_node():\n    return {"type": "Z8", "label": "op", "handler": _h}\n', encoding="utf-8")
    r3 = NP.scan_node_plugins(dirs=[tdp])
    warn_txt = " ".join(r3["warnings"])
    check("⑥ js 无 py → 拒绝注入告警", "onlyjs.js" in warn_txt and "拒绝注入" in warn_txt)
    check("⑥ py 无 js → handler 照注册 + 告警", "Z8" in r3["handlers"] and "onlypy.py" in warn_txt)
    check("⑥ 独立 js 不进 payload", not any(i["type"] == "Z9" for i in r3["js"]))

# ===== ⑦ 核心类型保护 =====
with tempfile.TemporaryDirectory() as td:
    tdp = Path(td)
    (tdp / "evil.py").write_text(
        'def _h(node, ctx):\n    return {"outputs": {}, "port": None}\n\n'
        'def agt_node():\n    return {"type": "21", "label": "x", "handler": _h}\n', encoding="utf-8")
    (tdp / "evil.js").write_text('EdFW.register({type:"21"});', encoding="utf-8")
    r4 = NP.scan_node_plugins(dirs=[tdp])
    check("⑦ 覆盖核心 type 21 被拒", "21" not in r4["handlers"] and "核心节点" in " ".join(r4["warnings"]))

print(f"\n全部通过 ✅（{PASS} 项）")
