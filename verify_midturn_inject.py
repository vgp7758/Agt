"""verify_midturn_inject.py —— 验证"中途插话"改为 user 角色 + 锚步渲染。

A) 锚到 Step.preceding_hint：_steps_to_messages 把它作为 user 消息(带标签)渲染在该步 assistant 之前。
B) 本步 pending（_pending_step_hint，还没锚定）：渲染在所有 tool 结果之后（跟下一组请求一起发）。
C) 无 pending / 无锚定时不多注入。
直接在 Session 层测（不需跑 Agent/LLM）。
"""
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from session import Session, Step, ToolCall, _MIDTURN_TAG  # noqa: E402

TMP = Path(__file__).resolve().parent / "_verify_mid"
if TMP.exists():
    shutil.rmtree(TMP)
TMP.mkdir()

passed, failed = [], []


def check(n, c, e=""):
    (passed if c else failed).append(n)
    print(("  ✅ " if c else "  ❌ ") + n + (f"  ({e})" if e and not c else ""))


s = Session("SYS", llm=None, workspace=TMP)
s.start_turn("做任务")
s.toollog.record("c1", "fake_tool", {"x": 1}, "结果1")
s.toollog.record("c2", "fake_tool", {"x": 2}, "结果2")
# 步1 带 preceding_hint；步2 不带
s._current.steps.append(Step(reasoning="r1", tool_calls=[ToolCall(call_id="c1")],
                             preceding_hint="改用方案B"))
s._current.steps.append(Step(reasoning="r2", tool_calls=[ToolCall(call_id="c2")]))

msgs = s.messages_for_llm()
hint_idx = next((i for i, m in enumerate(msgs)
                 if m.get("role") == "user" and "改用方案B" in m.get("content", "")), None)
asst_idx = next((i for i, m in enumerate(msgs)
                 if m.get("role") == "assistant" and m.get("tool_calls")), None)
check("锚定 hint 渲染为 user 角色", hint_idx is not None and msgs[hint_idx]["role"] == "user")
check("锚定 hint 带特殊标签",
      hint_idx is not None and _MIDTURN_TAG.strip() in msgs[hint_idx]["content"])
check("hint 在该步 assistant 之前",
      hint_idx is not None and asst_idx is not None and hint_idx < asst_idx, f"{hint_idx}/{asst_idx}")

# 本步 pending（还没锚定）：渲染在所有 tool 结果之后
s._current._pending_step_hint = "tail补充"
msgs2 = s.messages_for_llm()
tail_idx = next((i for i, m in enumerate(msgs2)
                 if m.get("role") == "user" and "tail补充" in m.get("content", "")), None)
last_tool_idx = max((i for i, m in enumerate(msgs2) if m.get("role") == "tool"), default=-1)
check("pending hint 在所有 tool 结果之后",
      tail_idx is not None and tail_idx > last_tool_idx, f"{tail_idx}/{last_tool_idx}")
check("pending hint 也是 user + 标签",
      tail_idx is not None and _MIDTURN_TAG.strip() in msgs2[tail_idx]["content"])

# 无 pending 时不注入尾部
s._current._pending_step_hint = None
msgs3 = s.messages_for_llm()
check("无 pending 时尾部不注入", not any("tail补充" in (m.get("content") or "") for m in msgs3))

shutil.rmtree(TMP, ignore_errors=True)
print(f"\n{'='*40}\n通过 {len(passed)} / 失败 {len(failed)}")
if failed:
    print("失败：", failed)
    sys.exit(1)
print("全部通过 ✅")
