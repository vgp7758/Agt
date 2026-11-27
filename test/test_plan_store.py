# -*- coding: utf-8 -*-
"""进程级计划仓库 PlanStore（p_c6cde42a，2026-11-28）回归网。

不变量逐条落成断言（取代 test_plan_view_sync.py——视图校验由 PlanStore 按文件戳自动完成，
场景并入于此）：
① 进程内同一计划只有一份快照：main/worker/任意读方 get 到同一对象（get 不拷贝，保身份）；
② mutate=深拷贝→改→原子落盘→换快照：旧快照引用不被就地改、文件同步、双方可见；
③ 写盘成功后全部订阅者收到 plan 事件（含从未读过该计划的空闲 main）；
④ 文件戳兜底外部手改；⑤ 文件损坏/被删保留现有快照；
⑥ 并发 mutate 各标不同步不丢更新（锁内读→改→写串行化）；
⑦ exit 后不再收 store 通知；⑧ 无活动计划返回 None / NO_PLAN_ERR；
⑨ Agent 上计划状态只剩 active_plan_id：active_plan/plan 为派生 property，setter 兼容旧赋值；
⑩ 多 workspace 同 id 计划互不串（key=文件绝对路径）；
⑪ 替身（SimpleNamespace 不可弱引用）watch 兜底跳过不炸，工具链照常可跑；
⑫ 读端 agent_config 装配函数经属性自动读到 worker 最新标步（零显式校验）。"""
import json
import threading
from types import SimpleNamespace

import pytest

import agent_config as ac
import plan_tools as pt


# ---------- 替身与工具 ----------

class _FA:
    """最小替身（普通 class 实例可进 WeakSet——收 store 写盘通知）。
    新模型下只持有 active_plan_id；active_plan/plan 与真实 Agent 同语义的派生 property
    （按 id 查 store）——agent_config 读端走 getattr(a,'active_plan',None)（生产路径），
    替身不带 property 会拿 None，绑上才与真实 Agent 行为一致。"""

    def __init__(self, ws, aid):
        self.agent_id = aid
        self.session = SimpleNamespace(workspace=str(ws))
        self.active_plan_id = None
        self.events = []
        self.on_event = self.events.append

    @property
    def active_plan(self):
        ws = getattr(getattr(self, "session", None), "workspace", None)
        pid = getattr(self, "active_plan_id", None)
        if ws is None or not pid:
            return None
        try:
            return pt.PLAN_STORE.get(ws, pid)
        except Exception:
            return None

    @property
    def plan(self):
        return [dict(s) for s in (self.active_plan or {}).get("steps", [])]


def _mk(ws, aid):
    return _FA(ws, aid)


def _funcs(a):
    """{工具名: 可调用}——直接取 Tool.func（绕过 Toolbox 的 dict 入参）。"""
    return {t.name: t.func for t in pt.make_plan_tools(a)}


def _setup(main, worker):
    """main create + worker join，返回 pid。"""
    pid = _funcs(main)["create_plan"]("标题", ["第一步", "第二步"]).split("已创建计划 ")[1].split("（")[0]
    assert "已加入计划" in _funcs(worker)["join_plan"](pid)
    return pid


@pytest.fixture(autouse=True)
def _fresh_store():
    """PlanStore 是进程级单例：每个测试前后清空条目与订阅，防跨用例串扰。"""
    pt.PLAN_STORE._reset()
    yield
    pt.PLAN_STORE._reset()


# ---------- ① 同一快照身份 ----------

def test_same_snapshot_identity(tmp_path):
    main, worker = _mk(tmp_path, "_main_"), _mk(tmp_path, "worker-1")
    pid = _setup(main, worker)
    s1 = pt.PLAN_STORE.get(tmp_path, pid)
    s2 = pt.PLAN_STORE.get(tmp_path, pid)
    assert s1 is s2                                  # get 不拷贝：同进程内同一快照对象
    assert main.active_plan_id == worker.active_plan_id == pid
    s3 = pt.PLAN_STORE.get(tmp_path, pid)            # 第三个读方（直接 store）
    assert s3 is s1


# ---------- ② mutate 双方可见 + 文件同步 + 不可变快照 ----------

def test_mutate_visible_to_both_and_file_synced(tmp_path):
    main, worker = _mk(tmp_path, "_main_"), _mk(tmp_path, "worker-1")
    pid = _setup(main, worker)
    assert "✅ 1." in _funcs(worker)["update_plan"](1, "completed")
    onfile = json.loads(pt._plan_path(tmp_path, pid).read_text(encoding="utf-8"))
    assert onfile["steps"][0]["status"] == "completed"      # 文件同步

    s_old = pt.PLAN_STORE.get(tmp_path, pid)                # mutate 前拿到的旧快照引用
    _funcs(worker)["update_plan"](2, "in_progress")
    s_new = pt.PLAN_STORE.get(tmp_path, pid)
    assert s_new is not s_old                               # 原子换快照
    assert s_old["steps"][1]["status"] == "pending"         # 旧快照不被就地改（不可变）
    assert s_new["steps"][1]["status"] == "in_progress"     # 新快照可见
    assert s_new["steps"][0]["status"] == "completed"       # 前一次标记不丢


def test_mutator_abort_leaves_file_and_snapshot_untouched(tmp_path):
    main, worker = _mk(tmp_path, "_main_"), _mk(tmp_path, "worker-1")
    pid = _setup(main, worker)
    s0 = pt.PLAN_STORE.get(tmp_path, pid)

    def bad_mut(plan):
        plan["steps"][0]["status"] = "completed"            # 先改再中止
        return "[错误] 中止"

    assert pt.PLAN_STORE.mutate(tmp_path, pid, bad_mut) == "[错误] 中止"
    assert pt.PLAN_STORE.get(tmp_path, pid) is s0           # 快照未换
    onfile = json.loads(pt._plan_path(tmp_path, pid).read_text(encoding="utf-8"))
    assert onfile["steps"][0]["status"] == "pending"        # 文件未写


# ---------- ③ 订阅者收 plan 事件（含从未读过该计划的空闲 main）----------

def test_subscribers_notified_even_idle_main(tmp_path):
    main, worker = _mk(tmp_path, "_main_"), _mk(tmp_path, "worker-1")
    pid = _setup(main, worker)
    n0 = len(main.events)                    # create 的显式 emit 之后（main 此后从未读过该计划）
    _funcs(worker)["update_plan"](1, "completed")
    plan_ev = [e for e in main.events[n0:] if e.get("type") == "plan"]
    assert len(plan_ev) == 1, "空闲 main（只订阅未读）也应在写盘瞬间收到 store 通知"
    assert plan_ev[0]["plan_id"] == pid
    assert [s["status"] for s in plan_ev[0]["plan"]] == ["completed", "pending"]


# ---------- ④ 外部手改文件可见 ----------

def test_external_edit_visible(tmp_path):
    main, worker = _mk(tmp_path, "_main_"), _mk(tmp_path, "worker-1")
    pid = _setup(main, worker)
    f = pt._plan_path(tmp_path, pid)
    d = json.loads(f.read_text(encoding="utf-8"))
    d["title"] = "外部手改"
    d["steps"][1]["status"] = "in_progress"
    f.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    s = pt.PLAN_STORE.get(tmp_path, pid)                     # 戳变 → 重读换快照
    assert s["title"] == "外部手改" and s["steps"][1]["status"] == "in_progress"
    block = pt._format_plan_block(main)                      # 装配视图同样跟上
    assert "外部手改" in block and "▶ 2." in block


# ---------- ⑤ 文件损坏/被删保留现有快照 ----------

def test_corrupt_or_missing_file_keeps_snapshot(tmp_path):
    main, worker = _mk(tmp_path, "_main_"), _mk(tmp_path, "worker-1")
    pid = _setup(main, worker)
    s0 = pt.PLAN_STORE.get(tmp_path, pid)
    f = pt._plan_path(tmp_path, pid)
    f.write_text("{not json", encoding="utf-8")              # 损坏
    assert pt.PLAN_STORE.get(tmp_path, pid) is s0
    f.unlink()                                               # 被删
    assert pt.PLAN_STORE.get(tmp_path, pid) is s0


# ---------- 文件没动：零重读 ----------

def test_get_skips_reread_when_stamp_unchanged(tmp_path, monkeypatch):
    main, worker = _mk(tmp_path, "_main_"), _mk(tmp_path, "worker-1")
    pid = _setup(main, worker)
    s0 = pt.PLAN_STORE.get(tmp_path, pid)
    calls = []
    real = json.loads

    def counting(*a, **k):
        calls.append(1)
        return real(*a, **k)

    monkeypatch.setattr(pt.json, "loads", counting)
    for _ in range(3):
        assert pt.PLAN_STORE.get(tmp_path, pid) is s0        # 每次恰好一次 os.stat，零读盘
    assert calls == []                                       # 文件没动：不重读


# ---------- ⑥ 并发 mutate 不丢更新 ----------

def test_concurrent_mutate_no_lost_update(tmp_path):
    main, worker = _mk(tmp_path, "_main_"), _mk(tmp_path, "worker-1")
    pid = _setup(main, worker)
    barrier = threading.Barrier(2)
    errs = []

    def mark(idx, status):
        def mut(plan):
            plan["steps"][idx]["status"] = status
            return None
        barrier.wait()                       # 同时开跑，逼出碰撞
        err = pt.PLAN_STORE.mutate(tmp_path, pid, mut)
        if err:
            errs.append(err)

    ts = [threading.Thread(target=mark, args=(0, "completed")),
          threading.Thread(target=mark, args=(1, "in_progress"))]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert errs == []
    onfile = json.loads(pt._plan_path(tmp_path, pid).read_text(encoding="utf-8"))
    assert onfile["steps"][0]["status"] == "completed"       # 两个标记都落盘（不互相覆盖）
    assert onfile["steps"][1]["status"] == "in_progress"


# ---------- ⑦ exit 后不再收通知 ----------

def test_exit_stops_notifications(tmp_path):
    main, worker = _mk(tmp_path, "_main_"), _mk(tmp_path, "worker-1")
    pid = _setup(main, worker)
    assert "已退出" in _funcs(worker)["exit_plan"]()
    n_main, n_worker = len(main.events), len(worker.events)   # worker 的 exit 显式 emit 已发生
    _funcs(main)["update_plan"](1, "completed")
    plan_ev_main = [e for e in main.events[n_main:] if e.get("type") == "plan"]
    assert len(plan_ev_main) == 1                             # main 仍订阅：收到 store 通知
    assert len(worker.events) == n_worker                     # worker 已退出：零新增通知


# ---------- ⑧ 无活动计划 → None / NO_PLAN_ERR ----------

def test_no_active_plan(tmp_path):
    a = _mk(tmp_path, "_main_")
    assert pt.PLAN_STORE.get(tmp_path, None) is None
    assert pt.PLAN_STORE.get(tmp_path, "p_absent") is None
    assert _funcs(a)["update_plan"](1, "completed") == pt.PLAN_STORE.NO_PLAN_ERR
    assert pt._format_plan_block(a) == ""
    assert pt._plan_text(a) == "(空计划)"


# ---------- ⑨ Agent property：真实 Agent 类 + 最小构造 ----------

def test_agent_property_setter_compat(tmp_path):
    import agent as A
    inst = object.__new__(A.Agent)               # 最小构造：不跑 __init__
    inst.session = SimpleNamespace(workspace=str(tmp_path))
    inst.active_plan_id = None
    assert inst.active_plan is None              # 无 id → None
    assert inst.plan == []

    plan = {"id": "p_x1", "title": "t", "design": "",
            "steps": [{"description": "a", "status": "pending"}]}
    pt.PLAN_STORE.put(str(tmp_path), dict(plan))            # 先入库（put 落盘+入快照）

    inst.active_plan = plan                     # setter：dict → 只记 id
    assert inst.active_plan_id == "p_x1"
    assert inst.active_plan is pt.PLAN_STORE.get(str(tmp_path), "p_x1")   # getter 走 store（同快照）
    assert [d["description"] for d in inst.plan] == ["a"]                 # plan getter=steps 拷贝
    inst.plan = ["ignored"]                     # plan setter no-op
    assert [d["description"] for d in inst.plan] == ["a"]
    inst.active_plan = None                     # setter：None → 清 id
    assert inst.active_plan_id is None and inst.active_plan is None


def test_restore_active_plan_puts_and_watches(tmp_path):
    main = _mk(tmp_path, "_main_")
    pt.restore_active_plan(main, {"plan_id": "p_r1"})       # 文件不存在 → 清空
    assert main.active_plan_id is None

    plan = {"id": "p_r1", "title": "恢复", "design": "",
            "steps": [{"description": "s", "status": "pending"}]}
    pt._save_plan(tmp_path, plan)
    pt.restore_active_plan(main, {"plan_id": "p_r1"})       # 按 id 入库（懒加载）+订阅
    assert main.active_plan_id == "p_r1"
    assert pt.PLAN_STORE.get(tmp_path, "p_r1")["title"] == "恢复"

    n0 = len(main.events)                                   # 订阅生效：mutate 通知到它
    pt.PLAN_STORE.mutate(tmp_path, "p_r1", lambda p: (p["steps"][0].__setitem__("status", "completed"), None)[1])
    assert len([e for e in main.events[n0:] if e.get("type") == "plan"]) == 1

    pt.restore_active_plan(main, {"plan": [{"description": "旧", "status": "pending"}]})   # legacy 迁移
    assert main.active_plan_id and main.active_plan_id != "p_r1"
    assert pt.PLAN_STORE.get(tmp_path, main.active_plan_id)["steps"][0]["description"] == "旧"


# ---------- ⑩ 多 workspace 同 id 计划互不串 ----------

def test_same_plan_id_in_two_workspaces_isolated(tmp_path):
    ws1, ws2 = str(tmp_path / "ws1"), str(tmp_path / "ws2")
    for ws, title in ((ws1, "甲仓库"), (ws2, "乙仓库")):
        pt._save_plan(ws, {"id": "p_same", "title": title, "design": "",
                           "steps": [{"description": "s", "status": "pending"}]})
    s1 = pt.PLAN_STORE.get(ws1, "p_same")
    s2 = pt.PLAN_STORE.get(ws2, "p_same")
    assert s1["title"] == "甲仓库" and s2["title"] == "乙仓库"
    assert s1 is not s2                                     # key=文件绝对路径，条目隔离


# ---------- ⑪ 不可弱引用替身：watch 兜底跳过，工具链照常 ----------

def test_watch_skips_non_weakref_agent(tmp_path):
    a = SimpleNamespace(agent_id="ns", session=SimpleNamespace(workspace=str(tmp_path)),
                        active_plan_id=None)                # SimpleNamespace 不可弱引用
    tools = _funcs(a)
    pid = tools["create_plan"]("t", ["s"]).split("已创建计划 ")[1].split("（")[0]
    assert pid == a.active_plan_id                          # _set_active 照常（watch 兜底跳过）
    assert "✅ 1." in tools["update_plan"](1, "completed")  # mutate 不受影响
    assert pt.PLAN_STORE.get(tmp_path, pid)["steps"][0]["status"] == "completed"


# ---------- ⑫ 读端装配函数经属性自动读到最新标步 ----------

def test_agent_config_reads_store(tmp_path, monkeypatch):
    main, worker = _mk(tmp_path, "_main_"), _mk(tmp_path, "worker-1")
    pid = _setup(main, worker)
    old_ctx = ac._CURRENT_AGENT.get()
    monkeypatch.setattr(ac, "_RUNTIME_AGENT", main)
    ac._CURRENT_AGENT.set(None)
    try:
        assert "☐ 1. 第一步" in ac._func_plan_steps()       # 初始进度
        _funcs(worker)["update_plan"](1, "completed")       # worker 标步（main 未动任何计划工具）
        steps2 = ac._func_plan_steps()
        assert "✅" in steps2 and "已完成 1" in steps2       # main 装配实时跟上
        assert ac._active_plan_dict()["id"] == pid
        content = ac._func_plan_content()
        assert "第一步" in content                          # 图纸段仍完整
    finally:
        ac._CURRENT_AGENT.set(old_ctx)
