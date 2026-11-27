"""/pending 的两种模式（用户裁定 2026-11-27：带参数，按需逐条或合并）。

· merge（默认，source=pending_user）：后台泵弹出 → worker 批合并 → 轮末拼成一条开一轮（原行为）
· each（-each，source=pending_each）：不走后台泵；run 轮末逐条弹出开下一轮（原文）；
  agent 空闲时泵投 pending_each 触发项 → worker 取一条开一轮
worker 的分支与批合并行为由隔离实例实测覆盖（L2），这里测纯逻辑。
"""
import collections
import queue
import sys
import threading
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import chat  # noqa: E402
import commands  # noqa: E402
from agent import Agent  # noqa: E402


def _agent():
    ag = Agent.__new__(Agent)
    ag.inbox = collections.deque()
    ag._inbox_lock = threading.RLock()
    ag._notices = collections.deque()
    ag.rewrites = 0
    ag._inbox_persist_rewrite = lambda: setattr(ag, "rewrites", ag.rewrites + 1)
    return ag


def test_queue_mode_maps_to_source():
    ag = _agent()
    seen = []
    ag.push_message = lambda text, source=None, **kw: seen.append(source)
    ag.queue_pending_user_message("默认")
    ag.queue_pending_user_message("逐条", mode="each")
    ag.queue_pending_user_message("显式合并", mode="merge")
    ag.queue_pending_user_message("未知按合并", mode="whatever")
    assert seen == ["pending_user", "pending_each", "pending_user", "pending_user"]


def test_pop_inbox_skip_leaves_each_in_queue():
    ag = _agent()
    for it in (("pending_each", "A", None), ("schedule", "S", None),
               ("pending_each", "B", None), ("pending_user", "M", None)):
        ag.inbox.append(it)
    # 后台泵：跳过逐条型
    assert ag.pop_inbox(skip={"pending_each"})[1] == "S"
    assert ag.pop_inbox(skip={"pending_each"})[1] == "M"
    assert ag.pop_inbox(skip={"pending_each"}) is None          # 只剩逐条型 → 不弹
    assert [it[1] for it in ag.inbox] == ["A", "B"]              # 逐条型原样留在队列
    # run 轮末：不过滤 → 按序逐条弹出
    assert ag.pop_inbox()[1] == "A"
    assert ag.pop_inbox()[1] == "B"
    assert ag.pop_inbox() is None
    assert ag.rewrites == 4                                     # 每次成功弹出都重写持久化


def test_pop_pending_each_and_count():
    ag = _agent()
    for it in (("schedule", "S", None), ("pending_each", "A", None), ("pending_each", "B", None)):
        ag.inbox.append(it)
    assert ag.count_pending_each() == 2
    assert ag.pop_pending_each()[1] == "A"                      # 只取逐条型，队首的 schedule 不动
    assert ag.count_pending_each() == 1
    assert [it[1] for it in ag.inbox] == ["S", "B"]
    assert ag.pop_pending_each()[1] == "B"
    assert ag.pop_pending_each() is None
    assert [it[1] for it in ag.inbox] == ["S"]


def test_views_and_clear_cover_both_modes():
    ag = _agent()
    for it in (("pending_each", "A", None), ("pending_user", "M", None), ("schedule", "S", None)):
        ag.inbox.append(it)
    assert ag.pending_user_queue() == ["A", "M"]
    assert ag.pending_user_items() == [("each", "A"), ("merge", "M")]
    assert ag.pending_user_clear() == 2                         # 两种模式一起清，其它来源保留
    assert [it[1] for it in ag.inbox] == ["S"]


def _ctx(ag, busy):
    return types.SimpleNamespace(agent=ag, state={"busy": busy, "answered": False})


def test_cmd_pending_parses_mode(capsys):
    ag = _agent()
    queued = []
    ag.queue_pending_user_message = lambda text, mode="merge": queued.append((mode, text))
    ag.pending_user_queue = lambda: [t for _, t in queued]
    ag.pending_user_items = lambda: list(queued)
    commands._cmd_pending(_ctx(ag, True), ["-each", "修复A", "页面"])
    commands._cmd_pending(_ctx(ag, True), ["默认合并", "需求"])
    commands._cmd_pending(_ctx(ag, True), ["-merge", "显式合并"])
    assert queued == [("each", "修复A 页面"), ("merge", "默认合并 需求"), ("merge", "显式合并")]
    out = capsys.readouterr().out
    assert "-each 逐条" in out and "逐条成轮（每条独立一轮）" in out
    assert "与其它排队项合并成一轮" in out
    # 只给开关不给内容 → 用法提示，不入队
    commands._cmd_pending(_ctx(ag, False), ["-each"])
    assert len(queued) == 3
    assert "用法" in capsys.readouterr().out
    # list 标注模式
    commands._cmd_pending(_ctx(ag, False), ["list"])
    out = capsys.readouterr().out
    assert "[逐条] 修复A 页面" in out and "[合并] 默认合并 需求" in out


def test_pump_skips_each_and_triggers_only_when_idle():
    calls = {"skip": [], "sleep": 0}
    stops = {"n": 0}

    class _Stop(BaseException):
        pass

    _fa = types.SimpleNamespace()
    _fa.pop_inbox = lambda skip=None, only=None: (calls["skip"].append(skip), None)[1]
    _fa.count_pending_each = lambda: 1

    class _Clock:
        @staticmethod
        def sleep(_s):
            calls["sleep"] += 1
            if calls["sleep"] >= 4:
                raise _Stop

    _q = queue.Queue()
    _state = {"busy": True}
    pump = chat._make_inbox_pump(_fa, _q, _state)

    def _run():
        try:
            pump()
        except _Stop:
            stops["n"] += 1

    import chat as _chat_mod
    real_time = _chat_mod.time
    _chat_mod.time = _Clock
    try:
        t = threading.Thread(target=_run)
        t.start()
        t.join(5)
    finally:
        _chat_mod.time = real_time
    assert stops["n"] == 1 and not t.is_alive()
    assert all(s == frozenset({"pending_each"}) for s in calls["skip"])   # 泵始终跳过逐条型
    # 置空闲后：投一个触发项——模拟 worker 取走并置 busy → 之后不再重复投
    _state["busy"] = False
    calls["sleep"] = 0
    _q2 = queue.Queue()
    _real_put = _q2.put

    def _put(item):
        _real_put(item)
        _state["busy"] = True        # 模拟 worker 取走触发项并置 busy

    _q2.put = _put
    pump2 = chat._make_inbox_pump(_fa, _q2, _state)
    _chat_mod.time = _Clock
    try:
        t2 = threading.Thread(target=lambda: _swallow(pump2))
        t2.start()
        t2.join(5)
    finally:
        _chat_mod.time = real_time
    assert not t2.is_alive()
    assert _q2.qsize() == 1 and _q2.get_nowait()[0] == "pending_each"   # 投且仅投一次
    assert len(calls["skip"]) == 8 and all(s == frozenset({"pending_each"}) for s in calls["skip"])


def _swallow(fn):
    try:
        fn()
    except BaseException:
        return True
    return False
