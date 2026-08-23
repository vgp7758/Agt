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
check("① 注册 4 类（15/58/59/N1）", set(hs) >= {"15", "58", "59", "N1"}, str(sorted(hs)))
check("① py/js 配对齐全（无警告）",
      not [w for w in res["warnings"] if "无配对" in w], str(res["warnings"]))
check("① N1 handler 可调用", callable(hs["N1"]["handler"]))

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
