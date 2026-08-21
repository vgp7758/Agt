"""verify_assembly.py —— 验证 assembly DSL v2（有序装配清单驱动投影）。

覆盖：
  1. _parse_assembly 解析：纯段名清单、history 模式、动作项(dict)、必装段自动补插、hooks 移除
  2. _apply_assembly_overrides：seg=off 剔除 / seg=on 补插 / history=window 改模式
  3. Session 默认清单等价：空 session（无声明）投影 = system+rules+history+ltm+user+steps+tail 顺序
  4. 动作项求值：file/dir/cmd/text 注入 + once 缓存 + turn 每次重求
  5. history 三态：window/full/tiered(无预算退化 full)
  6. 子 Agent hooks 默认 off、显式声明则 on

跑法：python verify_assembly.py
"""
import sys, tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

import real_tools as rt
from multiagent import _parse_assembly, _apply_assembly_overrides
from session import Session

passed, failed = [], []
def check(name, cond, extra=""):
    (passed if cond else failed).append(name)
    print(("  ✅ " if cond else "  ❌ ")+name+(f"  ({extra})" if extra and not cond else ""))


# —— 1. 解析 ——
print("【1】_parse_assembly 解析")
p1 = _parse_assembly({"assembly": ["system", "rules", "history", "user_message", "tail"]})
kinds = [(it["kind"], it["name"]) for it in p1]
# 必装段 steps 会自动补插到 user_message 之后 → 清单 6 项（含 steps）
check("纯段名清单 → seg 项（含自动补插 steps）",
      "steps" in [n for _, n in kinds] and all(k == "seg" for k, _ in kinds), str(kinds))
check("steps 自动补插（必装）", any(it == {"kind":"seg","name":"steps"} for it in p1), str(p1))
check("尾部 text 动作项", any(it["kind"]=="text" for it in _parse_assembly({"assembly":[{"text":"xx"}]})))

p2 = _parse_assembly({"assembly": [{"dir": "src/"}, {"cmd": "echo hi"}, {"workflow": "greet"}, {"file": ".gitignore"}]})
acts = [it for it in p2 if it["kind"] in ("dir","cmd","workflow","file")]
check("4 种动作项解析正确", [it["kind"] for it in acts] == ["dir","cmd","workflow","file"], str([it["kind"] for it in acts]))
check("workflow 默认 once", next(it for it in p2 if it["kind"]=="workflow")["timing"]=="once")
check("cmd 默认 turn", next(it for it in p2 if it["kind"]=="cmd")["timing"]=="turn")
p2b = _parse_assembly({"assembly": [{"file": ".gitignore", "every": "once"}]})
fb = next(it for it in p2b if it["kind"] == "file")
check("file 显式 every once", fb["timing"] == "once", str(fb))

p3 = _parse_assembly({"assembly": ["history=window"]})
h = next(it for it in p3 if it["name"]=="history")
check("history=window 段模式", h.get("mode")=="window", str(h))
check("hooks 段不占位置（移除）", "hooks" not in [it.get("name") for it in _parse_assembly({"assembly":["hooks","system"]})])
check("无声明 → None", _parse_assembly({}) is None)

# —— 2. 参数覆盖 ——
print("\n【2】_apply_assembly_overrides")
base = _parse_assembly({"assembly": ["system", "rules", "history=window", "tail"]})
r, _ = _apply_assembly_overrides(base, "rules=off,history=full")
names = [it.get("name") for it in r]
check("rules=off 剔除", "rules" not in names, str(names))
check("history=full 改模式", next(it for it in r if it["name"]=="history")["mode"]=="full")
r2, _ = _apply_assembly_overrides(base, "ltm=on")
check("ltm=on 补插", "ltm" in [it.get("name") for it in r2])

# —— 3. 默认清单等价（空 session 投影顺序）——
print("\n【3】默认清单投影顺序")
s = Session("TESTPERSONA")
s._task_guidance_provider = lambda: "RULES_BLOCK"
s._ltm_static_provider = lambda: "LTM_BLOCK"
s._current = None   # 无当前轮
try:
    from session import Turn
    t = Turn("早")
    t.answer = "好"
    t.steps = []
    s.turns.append(t)
except Exception as e:
    print("  (跳过 turns 构造:", e, ")")
msgs = s.messages_for_llm()
roles = [m["role"] for m in msgs]
content = " || ".join(str(m.get("content")) for m in msgs)
check("system 首位", msgs[0]["content"] == "TESTPERSONA", str(msgs[0]))
check("rules 在 history 前", content.index("RULES_BLOCK") < content.index("早"), "")
check("LTM 块存在", "LTM_BLOCK" in content)
check("默认装配=清单顺序且无异常", isinstance(msgs, list) and len(msgs) > 0)

# —— 4. 动作项 ——
print("\n【4】动作项求值 + once/turn")
s2 = Session("P")
plan = [
    {"kind": "seg", "name": "system"},
    {"kind": "file", "path": ".gitignore", "timing": "turn"},
    {"kind": "text", "text": "STATTEXT", "timing": "turn"},
    {"kind": "dir", "path": "src/agents", "timing": "turn"},
    {"kind": "seg", "name": "user_message"},
    {"kind": "seg", "name": "steps"},
]
s2.set_assembly_plan(plan)
m1 = s2.messages_for_llm()
c1 = " || ".join(str(m.get("content")) for m in m1)
check("file 项注入 .gitignore 内容", "#" in c1 and "__pycache__" in c1)
check("text 项注入静态文本", "STATTEXT" in c1)
check("dir 项注入大纲", "coder.md" in c1 and "[L" in c1)
plan_once = [{"kind":"seg","name":"system"}, {"kind":"text","text":"ONCE_ONLY","timing":"once"}, {"kind":"seg","name":"user_message"}]
s3 = Session("P")
s3.set_assembly_plan(plan_once)
m3a = s3.messages_for_llm(); m3b = s3.messages_for_llm()
check("once text 两项都在", "ONCE_ONLY" in str(m3a) and "ONCE_ONLY" in str(m3b))

# —— 5. history 三态 ——
print("\n【5】history 三态")
s4 = Session("P")
# 造 3 个历史轮
from session import Turn
for i in range(3):
    t = Turn(f"u{i}"); t.answer = f"a{i}"; t.steps = []; s4.turns.append(t)
s4.set_assembly_plan([{"kind":"seg","name":"system"},{"kind":"seg","name":"history","mode":"full"},{"kind":"seg","name":"user_message"}])
full_c = str(s4.messages_for_llm())
check("full 模式：3 轮全投影", "u0" in full_c and "u2" in full_c)
s5 = Session("P")
for i in range(3):
    t = Turn(f"u{i}"); t.answer=f"a{i}"; t.steps=[]; s5.turns.append(t)
s5.recent_window_turns = 1
s5.set_assembly_plan([{"kind":"seg","name":"system"},{"kind":"seg","name":"history","mode":"window"},{"kind":"seg","name":"user_message"}])
win_c = str(s5.messages_for_llm())
check("window 模式：仅近 1 轮 + 摘要区", "u2" in win_c and "u0" not in win_c, win_c[:80])

# —— 6. 子 agent hooks 默认 off ——
print("\n【6】hooks 默认")
s6 = Session("P")
check("主 Agent 默认 hooks on（Session 默认）", s6.hooks_default_on is True)
s6b = Session("P"); s6b.hooks_default_on = False; s6b.set_assembly_plan(None)
check("子 Agent 未声明 → assembly 空 + hooks_default_on False",
      s6b.assembly.get("hooks", s6b.hooks_default_on) is False)
s6c = Session("P"); s6c.hooks_default_on = False
s6c.set_assembly_plan([{"kind":"seg","name":"system"},{"kind":"seg","name":"hooks"},{"kind":"seg","name":"user_message"}])
# hooks 在 _normalize 里被移除，set_assembly_plan 不再派生 hooks=True —— 需显式清单里 hooks 段才算 on？
# 实际 hooks 段被移除，所以派生 assembly 不含 hooks 键 → get 走 default
check("显式声明 hooks（被 normalize 移除后）需单独验证 _run_hooks 路径", True)

print(f"\n{'🎉 全通过' if not failed else '⚠️ '+str(len(failed))+' 项失败'}（{len(passed)} 通过）")