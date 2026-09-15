"""verify_recent_file.py —— 验证 recent-file 快照采集 + 渲染。

A) _collect_file_snapshots：针对文件的多 tool_call 收集快照(同文件覆盖、最多3个、>4000行截断)
B) _steps_to_messages 渲染 <recent-file> 在 tool result 后
"""
import shutil, sys, types
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent/"src"))
import agent as agent_mod
from session import Session, Step, ToolCall

TMP = Path(__file__).resolve().parent / "_verify_rf"
passed, failed = [], []
def check(n,c,e=""): (passed if c else failed).append(n); print(("  ✅ " if c else "  ❌ ")+n+(f"  ({e})" if e and not c else ""))

if TMP.exists(): shutil.rmtree(TMP)
TMP.mkdir()
(TMP/"a.js").write_text("// file a\nvar x=1;\n", encoding="utf-8")
(TMP/"b.js").write_text("// file b\nvar y=2;\n", encoding="utf-8")

# FakeLLM + Agent
class _Resp:
    def __init__(s): s.content="ok"; s.tool_calls=[]; s.reasoning=""; s.usage={"prompt_tokens":1,"completion_tokens":1,"total_tokens":2}
class _FakeLLM:
    def __init__(s,*a,**k): s.model_name="fake"; s.call_recorder=None; s.switch_model=lambda n:None
    def chat(s,msgs,tools=None): return _Resp()

import real_tools as rt; rt.WORKSPACE = TMP
orig = agent_mod.LLMClient; agent_mod.LLMClient = _FakeLLM
try:
    from tools import Toolbox
    ag = agent_mod.Agent(system="sys", tools=Toolbox(), verbose=False)
    # 构造一个 step：两工具改 a.js + 一个改 b.js
    ag.session.toollog.record("c1","edit",  {"path":"a.js","old_string":"x","new_string":"X"},"ok")
    ag.session.toollog.record("c2","insert",{"path":"a.js","entries":[{"line":1,"content":"top"}]},"ok")
    ag.session.toollog.record("c3","edit",  {"path":"b.js","old_string":"y","new_string":"Y"},"ok")
    step = Step(reasoning="r", tool_calls=[ToolCall(call_id="c1"),ToolCall(call_id="c2"),ToolCall(call_id="c3")])
    snaps = ag._collect_file_snapshots(step)
    # 同文件 a.js → 最后一次 c2 持有快照（覆盖 c1）；b.js → c3
    check("同文件后面覆盖(c1无快照)", "c1" not in snaps, str(list(snaps.keys())))
    check("a.js 快照归 c2", "c2" in snaps and "a.js" in snaps["c2"]["path"])
    check("b.js 快照归 c3", "c3" in snaps and "b.js" in snaps["c3"]["path"])
    check("最多 2 个不同文件", len(snaps) == 2)

    # 渲染：验证 <recent-file> 出现在 tool result 后
    step.file_snapshots = snaps
    ag.session._current = ag.session.start_turn("hi") or ag.session._current
    ag.session._current.steps.append(step)
    msgs = ag.session.messages_for_llm()
    rf_msgs = [m for m in msgs if "<recent-file" in (m.get("content") or "")]
    check("渲染出 recent-file 块", len(rf_msgs) >= 2, str(len(rf_msgs)))
    check("块紧跟 tool result(tool_call_id 匹配)", any("tool_call_id" in str(msgs[i-1]) if i>0 else "" for i,m in enumerate(msgs) if "<recent-file" in (m.get("content") or "")), "ok")

except Exception as e:
    check(f"异常 {type(e).__name__}", False, str(e)[:200])
finally:
    agent_mod.LLMClient = orig
    shutil.rmtree(TMP, ignore_errors=True)

print(f"\n通过 {len(passed)} / 失败 {len(failed)}")
if failed: print("失败：", failed); sys.exit(1)
print("全部通过 ✅")
