# 施工期 recent-file 内嵌形态验证（用户裁定 2026-09-13·步骤5）：
#   施工模式（活动 plan 有未完成步）下，快照不再走独立段（防双份），而是回内嵌——
#   该次写调用【当时】的快照贴在该次 tool result 尾部，不去重（同文件多改各挂各的）、不限数量。
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from session import Session, Step, ToolCall
import agent_config

s = Session(system="t", llm=None)
ok = True
def check(name, cond, detail=""):
    global ok
    print(("✅" if cond else "❌"), name, detail)
    ok = ok and cond

def set_construction(sess, on: bool):
    agent_config._RUNTIME_AGENT = (types_mod.SimpleNamespace(
        session=sess, active_plan={"steps": [{"status": "pending"}, {"status": "completed"}]})
        if on else None)

import types as types_mod

# —— 构造当前轮：同文件两次 edit（c1→v1 旧版 / c2→v2 新版）+ 另一文件一次（c3） ——
s.start_turn("施工轮：同文件两次改 + 另一文件一次")
s._current.steps.append(Step(
    tool_calls=[ToolCall(call_id="c1", changed=[{"file": "a.py", "change": "modified"}])],
    file_snapshots={"c1": {"path": "a.py", "version": "v1", "text": "old line"}}))
s._current.steps.append(Step(
    tool_calls=[ToolCall(call_id="c2", changed=[{"file": "a.py", "change": "modified"}]),
                ToolCall(call_id="c3", changed=[{"file": "b.py", "change": "modified"}])],
    file_snapshots={"c2": {"path": "a.py", "version": "v2", "text": "newest line"},
                    "c3": {"path": "b.py", "version": "vB", "text": "b content"}}))
for cid in ("c1", "c2", "c3"):
    s.toollog.record(cid, "edit", {"path": "x"}, "done")

# —— 1. 施工模式判定（mock _RUNTIME_AGENT：session 同一 + 有未完成步） ——
set_construction(s, True)
check("施工模式判定：True", s._construction_mode() is True)

# —— 2. 段式防双份：施工期独立段返回空（即使有快照） ——
check("段式防双份：施工期 _seg_msgs_recent_file 为空", s._seg_msgs_recent_file() == [])

# —— 3. 内嵌命中 + 不去重 + 不限数量：三次写调用各挂【当时】的快照 ——
msgs = s._seg_msgs_steps()
tool_by_cid = {m.get("tool_call_id"): str(m.get("content") or "") for m in msgs if m.get("role") == "tool"}
check("内嵌：c1 result 尾含 rf 块（当时版本 v1）",
      'file="a.py" version="v1"' in tool_by_cid.get("c1", "") and "old line" in tool_by_cid["c1"])
check("不去重：c2 result 尾含 rf 块（当时版本 v2，与 c1 各挂各的）",
      'file="a.py" version="v2"' in tool_by_cid.get("c2", "") and "newest line" in tool_by_cid["c2"])
check("不限数量：c3 也各挂各的",
      'file="b.py" version="vB"' in tool_by_cid.get("c3", "") and "b content" in tool_by_cid["c3"])
check("内嵌块在 result 尾部（\n 前缀起头）", "\n<recent-file " in tool_by_cid["c1"])
check("快照行号化全文（read_file 口径）", "1| old line" in tool_by_cid["c1"])
check("共 3 个 rf 块（不去重 × 不限数量）",
      sum(c.count("<recent-file ") for c in tool_by_cid.values()) == 3)

# —— 4. 剥离/诊断口径全量扩展（施工命中集合 = 全部写调用 cid） ——
stripped = s._rf_stripped(msgs)
check("_rf_stripped：3 块全剥（估算免疫不漏）",
      not any("<recent-file" in str(m.get("content") or "") for m in stripped))
check("_rf_in_msgs：计 3 块总量 > 0", s._rf_in_msgs(msgs) > 0)

# —— 5. 非施工回归：段式照旧（同文件仅最新 v2 一份）、内嵌消失 ——
set_construction(s, False)
check("非施工判定：False", s._construction_mode() is False)
msgs2 = s._seg_msgs_steps()
check("非施工：tool result 不含 rf（内嵌仅施工期）",
      not any("<recent-file" in str(m.get("content") or "") for m in msgs2 if m.get("role") == "tool"))
b = s._seg_msgs_recent_file()
check("非施工：段式照旧（同文件仅最新 v2 一份）",
      len(b) == 1 and 'version="v2"' in b[0]["content"]
      and 'version="v1"' not in b[0]["content"] and 'version="vB"' in b[0]["content"])

agent_config._RUNTIME_AGENT = None
print("\n" + ("ALL PASS ✅" if ok else "SOME FAILED ❌"))
sys.exit(0 if ok else 1)
