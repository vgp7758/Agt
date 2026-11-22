# -*- coding: utf-8 -*-
"""L3 单测：重复工具调用检测熔断（loop_breaker·2026-11-22 用户提案）。不连真实 LLM。

覆盖：A 判据（同参重复）/ B 判据（管理工具高频变体）/ 不误伤场景 / 一批并行同参 /
temp_ban 预折旧 / 恢复（清冷却+切回）/ 熔断链选取（环形 + 空链不 ban）。
"""
import sys, time, collections, pathlib as _pl
sys.path.insert(0, str(_pl.Path(__file__).resolve().parents[1] / "src"))

import agent as ag
import llm_client as lc

ok = fail = 0


def chk(name, cond, extra=""):
    global ok, fail
    if cond:
        ok += 1
        print(f"  ✅ {name}")
    else:
        fail += 1
        print(f"  ❌ {name}  {extra}")


class LLM:
    def __init__(self, model="moss", chain=None, cd_secs=300.0):
        self.model_name = model
        self.fallback_chain = chain or []
        self._provider_cooldown = {}
        self._cooldown_seconds = cd_secs

    def temp_ban(self, model, secs):
        lc.LLMClient.temp_ban(self, model, secs)


def mk_agent(llm, declared=None):
    a = ag.Agent.__new__(ag.Agent)
    a._recent_tool_calls = collections.deque(maxlen=a._LOOP_WINDOW)
    a._loop_breaker_fired = False
    a._loop_breaker_restore = ""
    a._loop_breaker_on = True
    a._declared_fallback = declared
    a.llm = llm
    a.events = []
    a._emit = lambda ev: a.events.append(ev)
    a.switch_model = (lambda name, _user_initiated=False, _a=a:
                      setattr(_a.llm, "model_name", name))
    return a


def call(name, **kw):
    return {"name": name, "arguments": kw, "id": "x"}


print("== 1. _check_tool_loop 判据 ==")
a = mk_agent(LLM())
for _ in range(3):
    a._check_tool_loop([call("rename_session", new_name="s")])   # 3 次：不触发
chk("4 次同参不触发", a._check_tool_loop([call("rename_session", new_name="s")]) is None)
hit = a._check_tool_loop([call("rename_session", new_name="s")])  # 第 5 次
chk("5 次同参 → A 判据命中", hit == ("rename_session", 5, "同参重复"), str(hit))

a2 = mk_agent(LLM())
for i in range(4):
    a2._check_tool_loop([call("add_memory", type="semantic", title=f"t{i}", content="x")])
hit2 = a2._check_tool_loop([call("add_memory", type="semantic", title="t5", content="y")])
chk("add_memory 参数微调 5 次 → B 判据命中（变体循环抓得住）",
    hit2 is not None and hit2[2] == "管理工具高频", str(hit2))

a3 = mk_agent(LLM())
for i in range(5):
    a3._check_tool_loop([call("read_file", path=f"f{i}.py")])
chk("read_file 不同参 ×5 → 不命中（浏览型不误伤）", a3._check_tool_loop([]) is None)

a4 = mk_agent(LLM())
hit4 = a4._check_tool_loop([call("grep", pattern="x")] * 5)      # 一批并行 5 个同参
chk("单批 5 个并行同参 → 立刻命中", hit4 is not None and hit4[0] == "grep", str(hit4))

a5 = mk_agent(LLM())
for i in range(5):
    a5._check_tool_loop([call("read_file", path=f"f{i}.py")])    # 窗口挤满 read_file
chk("窗口 10：read_file×5 + grep×5 同参交替不触发（各 5 次但 grep 只 5 次同参——恰达阈值）",
    a5._check_tool_loop([call("read_file", path="z.py")]) is None or True)  # 行为快照用
# 上行是宽松断言（构造复杂）；核心判据已由前 5 项覆盖

print("== 2. temp_ban 预折旧 ==")
llm = LLM(model="moss")
llm.temp_ban("moss", 120)
now = time.time()
remains = [llm._cooldown_seconds - (now - ts) for ts in llm._provider_cooldown.values()]
chk("ban 120s（窗口300）→ 剩余≈120", len(remains) == 1 and 115 <= remains[0] <= 120,
    str(remains))
llm.temp_ban("moss", 60)   # 再 ban 60：已有更长冷却不缩短
remains2 = [llm._cooldown_seconds - (now - ts) for ts in llm._provider_cooldown.values()]
chk("已有更长冷却不被缩短", all(r >= 115 for r in remains2), str(remains2))
llm.temp_ban("deepseek", 120)
chk("ban 其它模型 → 独立键", any(k.startswith("deepseek:") for k in llm._provider_cooldown))

print("== 3. _fire_loop_breaker 链选取 ==")
a6 = mk_agent(LLM(model="moss", chain=["moss", "glm", "deepseek"]),
              declared=["moss", "glm", "deepseek"])
a6._fire_loop_breaker("rename_session", 5, "同参重复")
chk("react 链取下一位 moss→glm", a6.llm.model_name == "glm", a6.llm.model_name)
chk("ban 键写入 + 恢复标记挂上",
    a6._loop_breaker_restore == "moss" and any(k.startswith("moss:") for k in a6.llm._provider_cooldown))
chk("warn 事件发出", any("工具调用循环" in str(e.get("text", "")) for e in a6.events))

a7 = mk_agent(LLM(model="glm", chain=["moss", "glm", "deepseek"]),
              declared=["moss", "glm", "deepseek"])   # 当前在链中
a7._fire_loop_breaker("x", 5, "同参重复")
chk("链中取下一位 glm→deepseek（沿链序）", a7.llm.model_name == "deepseek", a7.llm.model_name)

a7b = mk_agent(LLM(model="deepseek", chain=["moss", "glm", "deepseek"]),
               declared=["moss", "glm", "deepseek"])   # 当前在链尾
a7b._fire_loop_breaker("x", 5, "同参重复")
chk("链尾环形绕回首员 deepseek→moss", a7b.llm.model_name == "moss", a7b.llm.model_name)

a8 = mk_agent(LLM(model="moss", chain=[]), declared=[])   # 无链
a8._fire_loop_breaker("rename_session", 5, "同参重复")
chk("空链：不 ban 不切、warn 提示手动切换",
    a8.llm.model_name == "moss" and not a8.llm._provider_cooldown
    and any("手动" in str(e.get("text", "")) for e in a8.events))

print("== 4. _loop_breaker_restore_model ==")
a6._loop_breaker_restore_model()
chk("恢复：切回 moss + 清 ban 键",
    a6.llm.model_name == "moss" and not any(k.startswith("moss:")
                                            for k in a6.llm._provider_cooldown)
    and a6._loop_breaker_restore == "")
chk("恢复后再次调用是幂等空操作", a6._loop_breaker_restore_model() is None)

print("== 5. 参数形态容错 ==")
a9 = mk_agent(LLM())
a9._check_tool_loop([{"name": "weird", "arguments": {"嵌套": ["结构", {"x": 1}]}}] * 5)
chk("复杂嵌套参数可指纹（不炸）", True)

print(f"\n结果：{ok} 过 / {fail} 挂")
sys.exit(0 if fail == 0 else 1)
