# -*- coding: utf-8 -*-
"""临时验证脚本（施工牌 p_2c19626d · 消费点对账治双消费）：
L3 单测五场景 + 接线静态断言；L2 隔离实跑由外层 run_shell 另起直跑实例覆盖。

场景：
  1. 正常后台消息不受影响（其它 source / 未登记 subagent 源恒不拦截）
  2. work_q 副本过滤（真调 chat._worker：已消费副本被拦截丢弃，正常项照常进 run）
  3. 轮末跳过续取（真 pop_inbox + 真 is_consumed_subagent，按 run() 轮末循环语义复刻）
  4. 重复登记幂等
  5. 容量淘汰一致性（501 个 aid 灌入，deque/set 同步淘汰）
外加：并发 smoke、multiagent 登记点接线断言。
"""
import sys, os, threading, time, collections, queue, inspect

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from agent import Agent
import chat as chat_mod
import multiagent as ma_mod

PASS, FAIL = [], []

def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("✅" if cond else "❌") + f" {name}" + (f"  [{detail}]" if detail and not cond else ""))

def make_fake_agent():
    """绕过 Agent.__init__（重依赖），手动按真实 __init__ 命名装配对账所需属性——
    绑定的 mark/is_consumed/pop 是【真方法真代码】。"""
    fa = type("FA", (), {})()
    fa._inbox_lock = threading.Lock()
    fa.inbox = collections.deque()
    fa._consumed_subagent = collections.deque(maxlen=500)
    fa._consumed_subagent_set = set()
    fa._inbox_persist_rewrite = lambda: None
    fa.mark_subagent_consumed = Agent.mark_subagent_consumed.__get__(fa)
    fa.is_consumed_subagent = Agent.is_consumed_subagent.__get__(fa)
    fa.pop_inbox = Agent.pop_inbox.__get__(fa)
    return fa

# ── 场景 1：正常后台消息不受影响 ─────────────────────────────
fa1 = make_fake_agent()
check("S1 其它 source（schedule）不拦截", fa1.is_consumed_subagent("schedule:jobs") is False)
check("S1 其它 source（service_exit）不拦截", fa1.is_consumed_subagent("service_exit:web") is False)
check("S1 非 subagent 前缀（user）不拦截", fa1.is_consumed_subagent("user") is False)
check("S1 None source 安全", fa1.is_consumed_subagent(None) is False)
check("S1 未登记 aid 不拦截", fa1.is_consumed_subagent("subagent:never-registered") is False)
fa1.inbox.append(("schedule:t1", "正常调度", None))
it = fa1.pop_inbox()
check("S1 未登记项 pop 照常返回", it == ("schedule:t1", "正常调度", None), repr(it))

# ── 场景 2：work_q 副本过滤（真调 chat._worker）──────────────
fa2 = make_fake_agent()
fa2.run_calls = []
def _fake_run(self, msg, **kw):
    self.run_calls.append(msg)
    return "ok"
fa2.run = _fake_run.__get__(fa2)
fa2.mark_subagent_consumed("a1")
wq = queue.Queue()
wq.put(("background", ("subagent:a1", "已交付回执副本", None)))   # 应被过滤
wq.put(("background", ("schedule:t9", "正常调度消息", None)))     # 应进 run
wq.put(None)                                                     # 退出哨兵
state = {"busy": False}
chat_mod._worker(fa2, wq, None, state)   # registry=None（run 分支不 dispatch，不会用到）
check("S2 混合批：副本被过滤、正常项进 run",
      len(fa2.run_calls) == 1 and "schedule:t9" in fa2.run_calls[0] and "subagent:a1" not in fa2.run_calls[0],
      repr(fa2.run_calls))

fa2b = make_fake_agent()
fa2b.run_calls = []
def _fake_run2(self, msg, **kw):
    self.run_calls.append(msg)
    return "ok"
fa2b.run = _fake_run2.__get__(fa2b)
fa2b.mark_subagent_consumed("a1")
wq2 = queue.Queue()
wq2.put(("background", ("subagent:a1", "已交付回执副本", None)))  # 纯副本批 → 全滤空 → 不空跑轮
wq2.put(None)
chat_mod._worker(fa2b, wq2, None, {"busy": False})
check("S2 纯副本批：不空跑 agent.run（worker 正常退出）", len(fa2b.run_calls) == 0, repr(fa2b.run_calls))

# ── 场景 3：轮末跳过续取（真 pop_inbox + 真 is_consumed，循环复刻 run() 源码语义）──
fa3 = make_fake_agent()
fa3.inbox.append(("subagent:a1", "副本", None))
fa3.inbox.append(("schedule:t1", "正常项", None))
fa3.mark_subagent_consumed("a1")
item = None
while True:                       # 与 run() 轮末 while 逐行同构
    item = fa3.pop_inbox()
    if item is None:
        break
    if fa3.is_consumed_subagent(item[0]):
        continue
    break
check("S3 轮末跳过已消费副本续取下一项", item == ("schedule:t1", "正常项", None), repr(item))
check("S3 跳过后 inbox 清空", len(fa3.inbox) == 0)
# 全是副本时轮末循环应落到 None（不开新一轮）
fa3b = make_fake_agent()
fa3b.inbox.append(("subagent:b1", "副本", None))
fa3b.mark_subagent_consumed("b1")
item3b = None
while True:
    item3b = fa3b.pop_inbox()
    if item3b is None:
        break
    if fa3b.is_consumed_subagent(item3b[0]):
        continue
    break
check("S3 纯副本时轮末取到 None", item3b is None, repr(item3b))

# ── 接线静态断言（改动真的接进了 run() / _worker / wait_subagents）──
# 注：inspect.getsource 对 run() 这类超大函数会截断返回（实测仅 1484 字符），
# 故接线断言一律直接读源文件文本，不经过 inspect。
import agent as _agent_mod
check("import 来源是 src/agent.py",
      getattr(_agent_mod, "__file__", "").replace("\\", "/").endswith("src/agent.py"),
      str(_agent_mod.__file__))
def _src(name):
    with open(os.path.join(ROOT, "src", name), encoding="utf-8") as f:
        return f.read()
ag_src = _src("agent.py")
ma_src = _src("multiagent.py")
chat_src = _src("chat.py")
check("接线 run() 轮末含对账 while+拦截", "is_consumed_subagent(item[0])" in ag_src and "消费点对账(run轮末)" in ag_src)
check("接线 _worker 含 batch 过滤", "agent.is_consumed_subagent" in chat_src and "if not batch:" in chat_src)
check("接线 wait_subagents 含登记点", "mark_subagent_consumed" in ma_src and "delivered_ids" in ma_src)
check("接线 __init__ 含登记表容器", "_consumed_subagent" in ag_src)
check("deliver_notice docstring 已修正（不再宣称步边界注入）", "并非旧注释说的" in ma_src)

# ── 场景 4：重复登记幂等 ────────────────────────────────────
fa4 = make_fake_agent()
fa4.mark_subagent_consumed("dup-1")
fa4.mark_subagent_consumed("dup-1")
fa4.mark_subagent_consumed("dup-1")
check("S4 重复登记幂等（deque=1）", len(fa4._consumed_subagent) == 1)
check("S4 重复登记幂等（set=1）", len(fa4._consumed_subagent_set) == 1)
check("S4 登记后可查询", fa4.is_consumed_subagent("subagent:dup-1") is True)

# ── 场景 5：容量淘汰一致性 ──────────────────────────────────
fa5 = make_fake_agent()
for i in range(501):
    fa5.mark_subagent_consumed(f"w-{i}")
check("S5 容量封顶 500（deque）", len(fa5._consumed_subagent) == 500)
check("S5 容量封顶 500（set）", len(fa5._consumed_subagent_set) == 500)
check("S5 最旧被淘汰", fa5.is_consumed_subagent("subagent:w-0") is False)
check("S5 最新仍可查", fa5.is_consumed_subagent("subagent:w-500") is True)
check("S5 deque/set 元素集合一致", set(fa5._consumed_subagent) == fa5._consumed_subagent_set)

# ── 并发 smoke：mark + is_consumed 多线程无死锁无异常 ─────────
fa6 = make_fake_agent()
errs = []
def _w(i):
    try:
        for j in range(50):
            fa6.mark_subagent_consumed(f"c-{i}-{j}")
            fa6.is_consumed_subagent(f"subagent:c-{i}-{j}")
    except Exception as e:
        errs.append(e)
ts = [threading.Thread(target=_w, args=(i,)) for i in range(8)]
[t.start() for t in ts]; [t.join(timeout=10) for t in ts]
check("并发 mark/query 无异常无死锁", not errs and all(not t.is_alive() for t in ts), str(errs[:3]))

print("\n" + "=" * 60)
print(f"L3 结果：{len(PASS)} 通过 / {len(FAIL)} 失败")
if FAIL:
    print("失败项：", FAIL)
    sys.exit(1)
