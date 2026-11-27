# -*- coding: utf-8 -*-
"""agent_bound 工具族重建 + 装配 contextvar + plan 读改写锁 + 没收中断 的单测（p_c0eebb19 步 8）。

mock 最小对象、不依赖 LLM。覆盖：
① rebind 后子 Agent 的 update_plan 报「还没有活动计划」，main 的活动计划不受影响；
② 子 Agent 线程里 agent_config._active_plan_dict() 读到子 Agent 自己的计划（contextvar）；
③ update_plan 读文件→改→写：main 内存副本落后于文件时不覆盖其它步骤状态；
④ 没收：_exec_tool fail-closed / sleep 提前返回 / agent_revoke 置旗标清插话 / 仅 main 持有。
"""
import json
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

import agent_config as ac
import plan_tools as pt
from agent import Agent
from tools import Toolbox


class _FA:
    """最小 Agent 替身的载体：普通 class 实例（可弱引用——SimpleNamespace 不可弱引用，
    会话 owner 登记需要 weakref.ref(agent)）。"""


def _fake_agent(workspace, agent_id):
    """最小 Agent 替身：plan/spec 工厂与 session 槽位需要的全部属性。
    （p_c6cde42a：替身不持有计划副本——只记 active_plan_id，读端按 id 查 store。）"""
    a = _FA()
    a.session = SimpleNamespace(workspace=workspace)
    a.agent_id = agent_id
    a.active_plan_id = None
    a.active_spec = None
    a.active_spec_id = None
    a.on_event = None
    a.tool_groups = {}
    a._revoked = False
    a._revoked_reason = ""
    a.pending_messages = []
    a.registry = None
    return a


def _tools_map(agent):
    return {t.name: t.func for t in pt.make_plan_tools(agent)}


# ---------- ① rebind：子 Agent 工具族重建，不借 main 的闭包 ----------

def test_rebind_rebuilds_bound_groups_and_updates_plan_is_isolated(tmp_path):
    from agent_bound_tools import rebind_agent_tools
    main = _fake_agent(tmp_path, "_main_")
    ts = _tools_map(main)
    ts["create_plan"]("主计划", ["主步1"])
    main.tools = Toolbox()
    for t in pt.make_plan_tools(main):
        main.tools.register(t)
        main.tool_groups[t.name] = "计划"

    sub = _fake_agent(tmp_path, "worker-x")
    sub.tools = Toolbox(*list(main.tools))          # 复刻 _resolve_tools：共享同一批 Tool 对象
    _tb = sub.tools
    old_update_plan = sub.tools._tools["update_plan"]
    replaced = rebind_agent_tools(sub, main)
    assert "update_plan" in replaced
    assert sub.tools._tools["update_plan"] is not old_update_plan   # 原地替换成 sub 闭包
    assert sub.tools is _tb                                          # Toolbox 对象身份不变（原地替换）

    # 子 Agent 的 update_plan：自己名下无计划 → 明确报错，不写到 main 的计划
    msg = _tools_map(sub)["update_plan"](1, "completed")
    assert "还没有活动计划" in msg
    assert pt.PLAN_STORE.get(tmp_path, main.active_plan_id)["steps"][0]["status"] == "pending"   # main 不受影响
    # join 之后 worker 的标进度落在共享仓库上（p_c6cde42a：同一进程一份快照，main 查即见）
    worker_ts = _tools_map(sub)
    assert "已加入计划" in worker_ts["join_plan"](main.active_plan_id)
    assert "▶" in worker_ts["update_plan"](1, "in_progress")
    assert pt.PLAN_STORE.get(tmp_path, main.active_plan_id)["steps"][0]["status"] == "in_progress"


# ---------- ② contextvar：子线程装配读自己的活动计划 ----------

def test_current_agent_contextvar_isolation():
    main = _fake_agent(Path("."), "_main_")
    main.active_plan = {"id": "p_main", "steps": [{"description": "a", "status": "pending"}]}
    ac._RUNTIME_AGENT = main
    try:
        assert ac._active_plan_dict()["id"] == "p_main"   # 默认读 main
        sub = _fake_agent(Path("."), "worker-x")
        sub.active_plan = {"id": "p_sub", "steps": [{"description": "b", "status": "pending"}]}
        got = {}
        def _worker():
            ac.set_current_agent(sub)
            got["plan"] = ac._active_plan_dict()
            got["agent"] = ac.current_agent().agent_id
        t = threading.Thread(target=_worker)
        t.start(); t.join()
        assert got["plan"]["id"] == "p_sub"
        assert got["agent"] == "worker-x"
        assert ac._active_plan_dict()["id"] == "p_main"   # 线程外不受影响
    finally:
        ac._RUNTIME_AGENT = None
        ac._CURRENT_AGENT.set(None)


# ---------- ③ 读文件→改→写：落后内存不覆盖文件里其它步骤 ----------

def test_update_plan_reads_file_writes_back_under_lock(tmp_path):
    main = _fake_agent(tmp_path, "_main_")
    ts = _tools_map(main)
    ts["create_plan"]("锁测", ["步1", "步2", "步3"])
    pid = main.active_plan_id

    # worker join 同一计划并标步 2 完成（文件前进）
    worker = _fake_agent(tmp_path, "worker-x")
    wts = _tools_map(worker)
    wts["join_plan"](pid)
    wts["update_plan"](2, "completed")

    # main 视角（store 快照）与文件一致：worker 标的步 2 已可见（p_c6cde42a：不再有「落后内存副本」）
    assert pt.PLAN_STORE.get(tmp_path, pid)["steps"][1]["status"] == "completed"
    ts["update_plan"](3, "completed")

    # 文件终态：步2（worker 标）未被覆盖回 pending，步3（main 标）已写入
    plan = pt._load_plan(tmp_path, pid)
    assert [s["status"] for s in plan["steps"]] == ["pending", "completed", "completed"]
    # main 视图=store 快照=文件最新（同一份；辅助读法两条路都通）
    assert [s["status"] for s in pt.PLAN_STORE.get(tmp_path, pid)["steps"]] == ["pending", "completed", "completed"]
    assert [s["status"] for s in pt._plan_steps_of(main)] == ["pending", "completed", "completed"]
    # 校验错误分支原样保留
    assert "越界" in ts["update_plan"](9, "completed")
    assert "至少提供" in ts["update_plan"](1)


# ---------- ④ 没收与中断 ----------

def _fake_entry(agent):
    return SimpleNamespace(agent=agent, agent_id=agent.agent_id)


class _FakeReg:
    def __init__(self):
        self._m = {}
    def lookup(self, tid):
        return self._m.get(tid)
    def update_status(self, aid, status):
        pass


def test_revoke_fail_closed_and_sleep_interrupt(tmp_path):
    from multiagent import make_communication_tools
    main = _fake_agent(tmp_path, "_main_")
    main.registry = _FakeReg()
    sub = _fake_agent(tmp_path, "worker-x")
    main.registry._m["worker-x"] = _fake_entry(sub)
    main.registry._m["_main_"] = _fake_entry(main)

    tools = {t.name: t.func for t in make_communication_tools(main)}
    # 仅 main 有 agent_revoke
    sub_tools = {t.name for t in make_communication_tools(sub)}
    assert "agent_revoke" not in sub_tools

    # 没收：置旗标 + 清 pending + 回执
    sub.pending_messages.append("插话残留")
    ret = tools["agent_revoke"]("worker-x", "乱写文件")
    assert sub._revoked and sub._revoked_reason == "乱写文件"
    assert not sub.pending_messages
    assert "已没收" in ret
    # 不能没收自己
    assert "不能没收自己" in tools["agent_revoke"]("_main_", "")

    # _exec_tool fail-closed：任何工具直接返回固定文案
    r = Agent._exec_tool(sub, "write_file", {"path": "x", "content": "y"})
    assert r.startswith("[工具已没收]") and "乱写文件" in r

    # sleep 提前返回：被没收后第一片检查即中断（<0.1s 真睡）
    import time as _time
    t0 = _time.time()
    from real_tools import set_bg_owner
    set_bg_owner(sub)
    import assets.tools_builtin.misc_tools as _mt
    out = _mt.sleep(30)
    assert "已中断" in out and "没收" in out
    assert _time.time() - t0 < 2.0
    set_bg_owner(None)      # 复位线程局部 owner（不污染后续用例）


# ---------- ⑤ 复核补强：非借来的同名工具不动 / 没收终态不被 done 覆盖 / 会话 owner 归属 ----------

def test_rebind_skips_non_borrowed_same_name_tools(tmp_path):
    from agent_bound_tools import rebind_agent_tools
    from tools import Tool

    main = _fake_agent(tmp_path, "_main_")
    main.tools = Toolbox()
    for t in pt.make_plan_tools(main):
        main.tools.register(t)
        main.tool_groups[t.name] = "计划"
    sub = _fake_agent(tmp_path, "worker-x")
    sub.tools = Toolbox(*list(main.tools))

    def update_plan():          # 子 Agent 专属工具恰好与计划工具同名（覆盖注册，不是借来的对象）
        """子 Agent 专属同名工具（测试替身）。"""
        return "own"
    own = Tool(update_plan)
    sub.tools.register_or_replace(own)

    replaced = rebind_agent_tools(sub, main)
    assert "update_plan" not in replaced
    assert sub.tools._tools["update_plan"] is own        # 专属实现保住
    assert "join_plan" in replaced                        # 借来的照常重绑


def test_final_status_keeps_revoked():
    from multiagent import _final_status

    rv = SimpleNamespace(_revoked=True)
    ok = SimpleNamespace(_revoked=False)
    assert _final_status(rv, "done") == "revoked"
    assert _final_status(rv, "failed") == "revoked"
    assert _final_status(ok, "failed") == "failed"
    assert _final_status(ok, "done") == "done"
    assert _final_status(object(), "done") == "done"     # 无旗标属性的旧对象不炸


def test_session_owner_binding_sets_context_for_background_thread():
    main = _fake_agent(Path("."), "_main_")
    main.active_plan = {"id": "p_main", "steps": [{"description": "a", "status": "pending"}]}
    ac._RUNTIME_AGENT = main
    try:
        sub = _fake_agent(Path("."), "worker-x")
        sub.active_plan = {"id": "p_sub", "steps": [{"description": "b", "status": "pending"}]}
        sess = type("_Sess", (), {})()      # 普通 class 实例：可弱引用可哈希（SimpleNamespace 不可弱引用）
        ac.bind_session_owner(sess, sub)
        ac._CURRENT_AGENT.set(None)
        assert ac.adopt_session_owner(sess) is sub          # 后台线程入口按 session 反查 owner
        assert ac._active_plan_dict()["id"] == "p_sub"
        # main 的会话：归属不设（contextvar 留空 → 落回 _RUNTIME_AGENT）
        ac._CURRENT_AGENT.set(None)
        m_sess = type("_SessM", (), {})()
        ac.bind_session_owner(m_sess, main)
        assert ac.adopt_session_owner(m_sess) is main
        assert ac.current_agent() is main
    finally:
        ac._RUNTIME_AGENT = None
        ac._CURRENT_AGENT.set(None)


def test_update_plan_rejects_non_numeric_step(tmp_path):
    main = _fake_agent(tmp_path, "_main_")
    ts = _tools_map(main)
    ts["create_plan"]("步号", ["步1"])
    assert "整数序号" in ts["update_plan"]("abc", "completed")
    assert "▶" in ts["update_plan"]("1", "in_progress")   # 字符串数字宽容
