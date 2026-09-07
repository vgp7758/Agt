# recent_file 段式化验证（用户提案 2026-09-07·第四版）：
#   快照不再内嵌 tool result 尾部 → 独立装配段 <recent-file><file path version>…</file></recent-file>
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from session import Session, Step, ToolCall, RF_MAX_CHARS

s = Session(system="t", llm=None)
ok = True
def check(name, cond, detail=""):
    global ok
    print(("✅" if cond else "❌"), name, detail)
    ok = ok and cond

# —— 构造当前轮：两次工具调用带文件快照（小文件 + 超大文件） ——
s.start_turn("改两个文件")
big_text = "x = 1\n" * 30_000                      # 180K 字符 > RF_MAX_CHARS
s._current.steps.append(Step(
    tool_calls=[ToolCall(call_id="c1", changed=[{"file": "a.py", "change": "modified"}]),
                ToolCall(call_id="c2", changed=[{"file": "big.py", "change": "modified"}])],
    file_snapshots={"c1": {"path": "a.py", "version": "v1a", "text": "import os\nimport sys\nx = 1"},
                    "c2": {"path": "big.py", "version": "v1b", "text": big_text}}))

# —— 1. 段渲染：结构 + 行号 + version ——
msgs = s._seg_msgs_recent_file()
check("段渲染：非空（1 条消息）", len(msgs) == 1)
b = msgs[0]["content"]
check("结构：<recent-file> 包裹", b.startswith("<recent-file>\n<file ") and b.rstrip().endswith("</recent-file>"))
check("小文件：<file path version 属性", '<file path="a.py" version="v1a">' in b)
check("小文件：行号化全文（宽度自适应）", " 1| import os\n 2| import sys\n 3| x = 1" in b)
check("大文件：size 属性 = 字符数", f'size="{len(big_text)}"' in b)
check("大文件：<overview> 块", "<overview>" in b and "class/def 结构或大纲" not in b)  # py→ast 结构（此处 x=1 无定义→"(无顶层定义)")
check("大文件：<content note= 省略提示", f'<content note="文件过大（{len(big_text):,} 字符' in b)
check("大文件：不投全文（体积可控）", len(b) < len(big_text) // 2)

# —— 2. steps 消息不再内嵌（内嵌移除验证） ——
s.toollog.record("c1", "edit", {"path": "a.py"}, "done")
s.toollog.record("c2", "edit", {"path": "big.py"}, "done")
steps_msgs = s._seg_msgs_steps()
rf_in_tool = [m for m in steps_msgs if m.get("role") == "tool" and "<recent-file" in str(m.get("content"))]
check("steps 的 tool result 不再含 <recent-file（内嵌已移除）", not rf_in_tool)

# —— 3. walk_plan 集成：段进 reminder 桶（merge 到末条 content，含 <system-reminder> 包裹） ——
s.set_assembly_plan(None)   # 默认清单（含 recent_file 段）
proj, secs = [], []
s._walk_plan(proj, secs)
sec_names = [x["name"] for x in secs]
check("projection_breakdown 含 recent_file 段（并入末条）", any("recent_file" in n for n in sec_names))
_last = str(proj[-1].get("content") or "")
check("末条 content 含 <recent-file> 块（reminder 桶 merge）", "<recent-file>" in _last and "<system-reminder" in _last)

# —— 4. 空映射（无快照轮）零噪声 ——
s2 = Session(system="t", llm=None)
s2.start_turn("没改文件的轮")
check("空映射 → 空段", s2._seg_msgs_recent_file() == [])

# —— 5. 同文件多次 edit：只有最新快照 ——
s3 = Session(system="t", llm=None)
s3.start_turn("同文件两次改")
s3._current.steps.append(Step(
    tool_calls=[ToolCall(call_id="c1"), ToolCall(call_id="c2")],
    file_snapshots={"c1": {"path": "a.py", "version": "v1", "text": "old"},
                    "c2": {"path": "a.py", "version": "v2", "text": "newest"}}))
b3 = s3._seg_msgs_recent_file()[0]["content"]
check("同文件多改：仅最新（v2）一份", 'version="v2"' in b3 and 'version="v1"' not in b3 and "newest" in b3)

print("\n" + ("ALL PASS ✅" if ok else "SOME FAILED ❌"))
sys.exit(0 if ok else 1)
