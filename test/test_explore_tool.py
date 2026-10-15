"""explore 工具端到端验证（spec s_7edebd56 更新：嫁接改投影层内插 + 旁车 run_id + 断点续跑）：
FakeLLM（两步 tool_calls 后 stop）+ 临时目录真 Session → 旁车落盘（meta/assistant/tool/done）/
events 无嫁接 step/seeds 内插投影/下轮（历史轮）只剩调用+摘要/半截续跑（seq 接续）/损坏行截断/
pending 扫描/超时降级/白名单。不依赖完整 Agent 构造——explore_tools._new_llm 被 patch 成 Fake
（生产路径每次调用 new LLMClient；测试把这一步换成 FakeLLM 注入）。"""
import sys, json, tempfile, shutil, os
sys.path.insert(0, r"D:\AI_Usings\Agt\src")
sys.path.insert(0, r"D:\AI_Usings\Agt\.agent\tools")

import session as session_mod
from session import Session, Step, ToolCall
from llm_client import LLMResponse
import explore_tools
from explore_tools import _make_explore, _make_explore_resume, _make_explore_list

TMP = tempfile.mkdtemp(prefix="explore_e2e_")

# ---------- Fake 组件 ----------
class FakeLLM:
    """多轮 tool_calls（grep + read_file）→ 纯文本摘要；记录收到的 messages 供断言。
    calls=累计 tool 消息数阈值：见到的 tool 消息数 < calls 时继续发 tool_calls，否则总结。"""
    def __init__(self, calls=2):
        self.calls = calls; self.seen = []
    def chat(self, messages, tools=None, scene=None, **kw):
        self.seen.append(json.dumps(messages, ensure_ascii=False))
        n = sum(1 for m in messages if m["role"] == "tool")   # 已执行的工具数
        if n < self.calls:
            tc = {"id": f"fake_{n}", "name": "grep" if n % 2 == 0 else "read_file",
                  "arguments": {"pattern": "def add_step" if n % 2 == 0 else "session.py",
                                "start_line": 1, "end_line": 20}}
            return LLMResponse(content=f"第{n+1}步", reasoning="想查一下", tool_calls=[tc],
                               finish_reason="tool_calls", usage={})
        return LLMResponse(content="要点：session.py L880 add_step 落 events.jsonl", reasoning="",
                           tool_calls=None, finish_reason="stop", usage={})

def patch_llm(cls_or_factory):
    """把 explore_tools._new_llm 换成 Fake 注入（生产路径是独立 LLMClient，测试免网）。"""
    explore_tools._new_llm = (cls_or_factory if callable(cls_or_factory) and not isinstance(cls_or_factory, type)
                              else (lambda agent, model, _c=cls_or_factory: (_c(), "fake-model")))

class FakeAgent:
    """与真 Agent 等价的最小面：schemas/_exec_tool + 真 Session（_current/_explore_seeds/旁车目录）"""
    def __init__(self, ws):
        self.session = Session(system="test", workspace=ws)
        self.session.start_turn("探索测试")
        self.utility_model = ""
        self.model_name = "fake-model"
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

def sidecar_path(ag, run_id):
    return os.path.join(ag.session.session_dir, "explore", f"{run_id}.jsonl")

def read_jsonl(path):
    """读 jsonl，损坏行跳过（与生产 _read_sidecar 同语义——旁车损坏行会保留在文件里）。"""
    out = []
    with open(path, encoding="utf-8") as f:
        for l in f:
            if not l.strip():
                continue
            try:
                out.append(json.loads(l))
            except Exception:
                pass
    return out

# ---------- ① 端到端：2 步探索 → 旁车落盘 + seeds 内插 + events 零嫁接 ----------
patch_llm(FakeLLM)
ws = os.path.join(TMP, "ws1"); os.makedirs(ws)
ag = FakeAgent(ws)
explore = _make_explore(ag)
out = explore("找到 session.add_step 的定义与落盘行为")
head = out.split("\n")[0]
print("① 返回头:", head)
assert head.startswith("[外置探索·run:") and "·完成]" in head, head
run1 = head.split("run:")[1][:8]
assert len(run1) == 8, run1
assert "要点：session.py L880" in out

# seeds 进当前轮旁车（_explore_seeds，运行时属性）
seeds = ag.session._current._explore_seeds
assert len(seeds) == 2, len(seeds)
assert [s["tool"] for s in seeds] == ["grep", "read_file"], seeds
assert seeds[0]["run"] == run1 and seeds[0]["seq"] == 0 and seeds[1]["seq"] == 1
assert seeds[0]["reasoning"].startswith("[外置探索]")

# 旁车文件：meta + 2×(assistant+tool) + done = 6 行
sp = sidecar_path(ag, run1)
assert os.path.isfile(sp), sp
lines = read_jsonl(sp)
print(f"   旁车 {len(lines)} 行:", [l.get("ev") for l in lines])
assert [l["ev"] for l in lines] == ["meta", "assistant", "tool", "assistant", "tool", "done"]
assert lines[0]["goal"] == "找到 session.add_step 的定义与落盘行为"
assert lines[-1]["stop_reason"] == "完成"
assert lines[1]["msg"]["tool_calls"][0]["function"]["name"] == "grep"   # assistant 消息本体（恢复拼回用）
assert lines[2]["msg"]["tool_call_id"] == lines[1]["msg"]["tool_calls"][0]["id"]

# events.jsonl 零嫁接 step（不再 _seed_steps——归档轮后验证）
ag.session.abort_current_turn("（测试归档）")
ev = os.path.join(ag.session.session_dir, "events.jsonl")
ev_lines = read_jsonl(ev)
step_evs = [l for l in ev_lines if l.get("event") == "step"]
print(f"   events.jsonl: {len(ev_lines)} 行，step 事件 {len(step_evs)} 条")
assert len(step_evs) == 0, [s.get("event") for s in ev_lines]

# 投影内插（当前轮）：手动塞 seeds → _seg_msgs_steps 含 assistant(tool_calls)+tool 组
ag.session.start_turn("内插验证轮")
ag.session._current._explore_seeds.append(
    {"tool": "grep", "args": {"pattern": "x"}, "result": "命中1行",
     "reasoning": "[外置探索] 想验证", "run": run1, "seq": 0})
msgs = ag.session._seg_msgs_steps()
a_msgs = [m for m in msgs if m.get("role") == "assistant" and m.get("tool_calls")]
t_msgs = [m for m in msgs if m.get("role") == "tool"]
assert len(a_msgs) == 1 and len(t_msgs) == 1, (len(a_msgs), len(t_msgs))
tc0 = a_msgs[0]["tool_calls"][0]
assert tc0["function"]["name"] == "grep" and json.loads(tc0["function"]["arguments"]) == {"pattern": "x"}
assert t_msgs[0]["tool_call_id"] == tc0["id"] == f"exp-{run1}-0"
assert t_msgs[0]["content"] == "命中1行"
assert a_msgs[0].get("reasoning_content", "").startswith("[外置探索]")
print("   内插投影:", tc0["id"], "✓")

# 历史轮（下轮语义）：归档轮渲染只剩调用+摘要两条，无内插消息组
old_turn = ag.session.turns[0]
hist = ag.session._render_turn_frozen(0)
hist_ids = [tc["id"] for m in hist if m.get("tool_calls") for tc in m["tool_calls"]]
assert not any(i.startswith("exp-") for i in hist_ids), hist_ids
assert len(old_turn.steps) == 0    # 旧轮 steps 也是空的（探索过程不落 Step）
print("   历史轮无内插、旧轮零 steps ✓")

# ---------- ② 模拟中断：半截旁车 → explore_resume 断点续跑（seq 接续） ----------
ws2 = os.path.join(TMP, "ws2"); os.makedirs(ws2)
ag2 = FakeAgent(ws2)
run2 = "deadbeef"
ag2.session._ensure_session_dir()   # 新会话未落盘过，先确保 session 目录（生产路径由 _sidecar_dir 兜）
os.makedirs(os.path.join(ag2.session.session_dir, "explore"), exist_ok=True)
half = [{"ev": "meta", "goal": "续跑目标", "model": "", "max_steps": 8, "budget": 120, "ts": 0.0}]
for i in range(3):
    half.append({"ev": "assistant", "msg": {"role": "assistant", "content": None,
                "tool_calls": [{"id": f"t{i}", "type": "function",
                                "function": {"name": "grep", "arguments": "{\"pattern\": \"p\"}"}}]}})
    half.append({"ev": "tool", "msg": {"role": "tool", "tool_call_id": f"t{i}", "content": f"结果{i}"}})
with open(sidecar_path(ag2, run2), "w", encoding="utf-8") as f:
    for o in half:
        f.write(json.dumps(o, ensure_ascii=False) + "\n")

patch_llm(lambda agent, model: (FakeLLM(calls=5), "fake-model"))   # 已有 3 tool → 续跑 2 步后总结
out2 = _make_explore_resume(ag2)(run2)
head2 = out2.split("\n")[0]
print("② 续跑返回头:", head2)
assert head2.startswith("[外置探索·续跑·run:deadbeef·完成]"), head2
assert "已在 3 步基础上续跑 2 步" in head2, head2
assert "要点：session.py L880" in out2
# done 终行追加（旁车共 7 + 续跑 2 步×2 行 + done = 12 行）
lines2 = read_jsonl(sidecar_path(ag2, run2))
assert len(lines2) == 12 and lines2[-1]["ev"] == "done", len(lines2)
# 新 seeds 进当前轮旁车，seq 接续（3, 4）——tool_call_id 不与断点前冲突
seeds2 = ag2.session._current._explore_seeds
assert len(seeds2) == 2 and [s["seq"] for s in seeds2] == [3, 4], [s["seq"] for s in seeds2]
assert seeds2[0]["run"] == run2
# 续跑后重复 resume 拒绝（已有 done）
out2b = _make_explore_resume(ag2)(run2)
assert "已完成" in out2b, out2b
print("   旁车 12 行、seq 接续 [3,4]、重复 resume 拒绝 ✓")

# ---------- ③ explore_list + pending 扫描 + 损坏行截断 ----------
patch_llm(FakeLLM)
lst = _make_explore_list(ag2)()
print("③ explore_list:")
print("   " + lst.replace("\n", "\n   "))
assert f"run:{run2}" in lst and "完成" in lst
# 再造一个半截（带损坏行）→ list 标半截；scan → pending；resume 截断续跑成功
run3 = "cafe1234"
with open(sidecar_path(ag2, run3), "w", encoding="utf-8") as f:
    f.write(json.dumps({"ev": "meta", "goal": "损坏截断目标", "model": "", "max_steps": 8,
                        "budget": 120, "ts": 0.0}, ensure_ascii=False) + "\n")
    f.write(json.dumps({"ev": "assistant", "msg": {"role": "assistant", "content": None,
            "tool_calls": [{"id": "t0", "type": "function",
                            "function": {"name": "grep", "arguments": "{}"}}]}}, ensure_ascii=False) + "\n")
    f.write(json.dumps({"ev": "tool", "msg": {"role": "tool", "tool_call_id": "t0",
            "content": "结果0"}}, ensure_ascii=False) + "\n")
    f.write("{broken json !!!\n")   # 损坏行
lst3 = _make_explore_list(ag2)()
assert f"run:{run3}" in lst3 and "半截" in lst3
ag2.session._scan_pending_explores()
pend = ag2.session.extra_state.get("_pending_explores") or []
pend_runs = {p["run"] for p in pend}
print("   pending 扫描:", pend_runs)
assert pend_runs == {run3}, pend_runs          # run2 已 done 不在 pending；run3 半截在
assert pend[0]["goal"] == "损坏截断目标" and pend[0]["steps"] == 1
patch_llm(lambda agent, model: (FakeLLM(calls=2), "fake-model"))
out3 = _make_explore_resume(ag2)(run3)
print("   损坏截断续跑:", out3.split("\n")[0])
assert "损坏行" in out3.split("\n")[0], out3.split("\n")[0]
assert "已在 1 步基础上续跑" in out3
lines3 = read_jsonl(sidecar_path(ag2, run3))
assert lines3[-1]["ev"] == "done"
# pending 消费语义：scan 后 run3 有 done → 再扫不再 pending
ag2.session._scan_pending_explores()
assert not (ag2.session.extra_state.get("_pending_explores")), "done 后不应再 pending"
print("   list/scan/损坏截断 ✓")

# ---------- ④ 超时降级（max_steps=1 → 部分结果 + 无工具收口摘要） ----------
class SlowLLM(FakeLLM):
    def chat(self, messages, tools=None, scene=None, **kw):
        import time as _t; _t.sleep(0.05)
        if tools is None:   # 收口请求（_react 预算耗尽后的无工具总结）→ 部分摘要
            return LLMResponse(content="部分摘要", reasoning="", tool_calls=None, finish_reason="stop", usage={})
        # 恒返回 tool_calls（永不总结）→ 逼步数上限路径
        n = sum(1 for m in messages if m["role"] == "tool")
        tc = {"id": f"slow_{n}", "name": "glob_files", "arguments": {"pattern": "*.py"}}
        return LLMResponse(content="", reasoning="", tool_calls=[tc], finish_reason="tool_calls", usage={})

class SlowAgent(FakeAgent):
    def _exec_tool(self, name, args): return "slow-result"

patch_llm(lambda agent, model: (SlowLLM(), "fake-model"))
ws4 = os.path.join(TMP, "ws4"); os.makedirs(ws4)
ag4 = SlowAgent(ws4)
out4 = _make_explore(ag4)("限时探索", max_steps=1, budget_seconds=15)
head4 = out4.split("\n")[0]
print("④ 降级:", head4, "| 摘要段:", "部分摘要" in out4 or "无摘要" in out4)
assert "·步数上限]" in head4, head4
assert len(ag4.session._current._explore_seeds) == 1   # 1 步 seed 进旁车
run4 = head4.split("run:")[1][:8]
lines4 = read_jsonl(sidecar_path(ag4, run4))
assert lines4[-1]["ev"] == "done" and lines4[-1]["stop_reason"] == "步数上限"
# 收口摘要进 done 行（旁车可追溯部分结果）
assert "部分摘要" in lines4[-1]["summary"]

# ---------- ⑤ 白名单（edit 拒绝） ----------
class EditLLM(FakeLLM):
    def chat(self, messages, tools=None, scene=None, **kw):
        tc = {"id": "e0", "name": "edit", "arguments": {"path": "x.py", "old_string": "a", "new_string": "b"}}
        if not any(m["role"] == "tool" for m in messages):
            return LLMResponse(content="", reasoning="", tool_calls=[tc], finish_reason="tool_calls", usage={})
        return LLMResponse(content="总结", reasoning="", tool_calls=None, finish_reason="stop", usage={})

patch_llm(lambda agent, model: (EditLLM(), "fake-model"))
ws5 = os.path.join(TMP, "ws5"); os.makedirs(ws5)
ag5 = FakeAgent(ws5)
out5 = _make_explore(ag5)("尝试改文件", max_steps=2, budget_seconds=15)
s0 = ag5.session._current._explore_seeds[0]
assert "白名单拒绝" in s0["result"], s0["result"][:100]
print("⑤ edit 白名单拒绝 ✓（schemas 也不含 edit——第二道闸）")

shutil.rmtree(TMP, ignore_errors=True)
print("\n全部 PASS（旁车/内插/零嫁接/续跑seq接续/损坏截断/扫描/降级/白名单）")
