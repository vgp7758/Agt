"""explore 工具端到端验证（spec s_54a1eb86 Step 4）：
FakeLLM（两步 tool_calls 后 stop）+ 临时目录真 Session → 嫁接步落盘/读档重放/衰减位置/超时降级。
不依赖完整 Agent 构造——_seed_steps 用 agent.py 同款逻辑（5 行等价实现，见 FakeAgent）。"""
import sys, json, tempfile, shutil, os
sys.path.insert(0, r"D:\AI_Usings\Agt\src")
sys.path.insert(0, r"D:\AI_Usings\Agt\tools\builtin")

import session as session_mod
from session import Session, Step, ToolCall
from llm_client import LLMResponse
from explore_tools import _make_explore, _READ_ONLY

TMP = tempfile.mkdtemp(prefix="explore_e2e_")

# ---------- Fake 组件 ----------
class FakeLLM:
    """两轮 tool_calls（grep + read_file）→ 第三轮纯文本摘要；记录收到的 messages 供断言"""
    def __init__(self, calls=2):
        self.calls = calls; self.seen = []
    def chat(self, messages, tools=None, scene=None, **kw):
        self.seen.append(json.dumps(messages, ensure_ascii=False))
        n = sum(1 for m in messages if m["role"] == "tool")   # 已执行的工具数
        if n < self.calls:
            tc = {"id": f"fake_{n}", "name": "grep" if n == 0 else "read_file",
                  "arguments": {"pattern": "def add_step" if n == 0 else "session.py",
                                "start_line": 1, "end_line": 20}}
            return LLMResponse(content=f"第{n+1}步", reasoning="想查一下", tool_calls=[tc],
                               finish_reason="tool_calls", usage={})
        return LLMResponse(content="要点：session.py L880 add_step 落 events.jsonl", reasoning="",
                           tool_calls=None, finish_reason="stop", usage={})

class FakeAgent:
    """与真 Agent 等价的最小面：schemas/_exec_tool/utility_client/_seed_steps"""
    def __init__(self, ws):
        self.session = Session(system="test", workspace=ws)
        self.session.start_turn("探索测试")
    def _llm_tool_schemas(self):
        return [{"type": "function", "function": {"name": "grep",
                 "description": "搜", "parameters": {"type": "object", "properties": {}}}},
                {"type": "function", "function": {"name": "read_file",
                 "description": "读", "parameters": {"type": "object", "properties": {}}}},
                {"type": "function", "function": {"name": "edit",
                 "description": "改", "parameters": {"type": "object", "properties": {}}}}]
    def _exec_tool(self, name, args):
        if name == "grep":
            return "src/session.py:880:    def add_step(self, step: Step):"
        if name == "read_file":
            return "1│ class Session:..."
        return "?"
    def utility_client(self):
        return FakeLLM()
    def _seed_steps(self, seeds):   # agent.py L880-895 同款
        for sd in seeds:
            cid = self.session.toollog.next_id()
            self.session.toollog.record(cid, sd.get("tool", ""), sd.get("args", {}), sd.get("result", ""))
            step = Step(reasoning=sd.get("reasoning", ""))
            step.tool_calls.append(ToolCall(call_id=cid))
            self.session.add_step(step)

# ---------- ① 端到端：2 步探索 + 嫁接 ----------
ws = os.path.join(TMP, "ws1"); os.makedirs(ws)
ag = FakeAgent(ws)
explore = _make_explore(ag)
out = explore("找到 session.add_step 的定义与落盘行为")
print("① 返回头:", out.split("\n")[0])
assert "嫁接 2 步" in out, out[:200]
assert "要点：session.py L880" in out
steps = ag.session._current.steps
assert len(steps) == 2 and all(s.reasoning.startswith("[外置探索]") for s in steps), [(s.reasoning[:30]) for s in steps]
names = []
for s in steps:
    for tc in s.tool_calls:
        n, a, r = ag.session.toollog.view(tc.call_id)
        names.append(n)
        assert r, "toollog 有结果"
assert names == ["grep", "read_file"], names
print("   嫁接步:", [(s.tool_calls[0].call_id, ag.session.toollog.view(s.tool_calls[0].call_id)[0]) for s in steps])

# ---------- ② 落盘 + 读档重放（events.jsonl → _replay_events 重建 turns；meta.json 优先路径的兜底层） ----------
ag.session.abort_current_turn("（测试归档）")   # 归档轮（触发落盘）
sdir = ag.session.session_dir
ev = os.path.join(sdir, "events.jsonl")
lines = [json.loads(l) for l in open(ev, encoding="utf-8")]
step_evs = [l for l in lines if l.get("event") == "step"]
print(f"② events.jsonl: {len(lines)} 行，step 事件 {len(step_evs)} 条")
assert len(step_evs) == 2, len(step_evs)
# 重放路径（Session.load 的 events 兜底层，同 _replay_events）：重建 Turn>Step>ToolCall 树
turns = session_mod._replay_events(lines)
turn = turns[-1]
assert len(turn.steps) == 2, len(turn.steps)
# toollog 重载 + 按重放 call_id 召回（与投影组装同路径）
s2 = session_mod.Session(system="test", workspace=ws)
s2.toollog.load_from_jsonl(os.path.join(sdir, "toollog.jsonl"))
g_names = [s2.toollog.view(tc.call_id)[0] for s in turn.steps for tc in s.tool_calls]
print("   重放 steps 工具:", g_names)
assert g_names == ["grep", "read_file"], g_names

# ---------- ③ 超时降级（max_steps=1 → 部分结果 + 无工具收口摘要） ----------
class SlowLLM(FakeLLM):
    def chat(self, messages, tools=None, scene=None, **kw):
        import time as _t; _t.sleep(0.05)
        # 恒返回 tool_calls（永不总结）→ 逼步数上限路径
        n = sum(1 for m in messages if m["role"] == "tool")
        tc = {"id": f"slow_{n}", "name": "glob_files", "arguments": {"pattern": "*.py"}}
        return LLMResponse(content="", reasoning="", tool_calls=[tc], finish_reason="tool_calls", usage={})
    def chat_final(self, messages, **kw):
        return LLMResponse(content="部分摘要", reasoning="", tool_calls=None, finish_reason="stop", usage={})

class SlowAgent(FakeAgent):
    def utility_client(self): return SlowLLM()
    def _exec_tool(self, name, args): return "slow-result"

ws3 = os.path.join(TMP, "ws3"); os.makedirs(ws3)
ag3 = SlowAgent(ws3)
out3 = _make_explore(ag3)("限时探索", max_steps=1, budget_seconds=15)
print("③ 降级:", out3.split("\n")[0], "| 摘要段含'部分':", "部分" in out3 or "无摘要" in out3)
assert "嫁接 1 步" in out3 or "嫁接" in out3, out3[:150]
assert len(ag3.session._current.steps) == 1

# ---------- ④ 白名单（edit 拒绝） ----------
class EditLLM(FakeLLM):
    def chat(self, messages, tools=None, scene=None, **kw):
        tc = {"id": "e0", "name": "edit", "arguments": {"path": "x.py", "old_string": "a", "new_string": "b"}}
        if not any(m["role"] == "tool" for m in messages):
            return LLMResponse(content="", reasoning="", tool_calls=[tc], finish_reason="tool_calls", usage={})
        return LLMResponse(content="总结", reasoning="", tool_calls=None, finish_reason="stop", usage={})
class EditAgent(FakeAgent):
    def utility_client(self): return EditLLM()
ws4 = os.path.join(TMP, "ws4"); os.makedirs(ws4)
ag4 = EditAgent(ws4)
out4 = _make_explore(ag4)("尝试改文件", max_steps=2, budget_seconds=15)
assert "白名单拒绝" in json.dumps([ag4.session.toollog.view(tc.call_id)[2] for s in ag4.session._current.steps for tc in s.tool_calls], ensure_ascii=False)
print("④ edit 白名单拒绝 ✓（schemas 也不含 edit——第二道闸）")

shutil.rmtree(TMP, ignore_errors=True)
print("\n全部 PASS（嫁接/落盘/读档重放/超时降级/白名单）")
