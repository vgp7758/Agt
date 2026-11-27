# -*- coding: utf-8 -*-
"""子 Agent 人设元信息（_agent_meta）持久化 + 重启恢复自愈 + agent_ask 磁盘替身。

背景（20048 实锤 2026-11-27）：extra_state 落盘是 provider 覆盖式重建，直写进去的 _agent_meta
被子 Agent 收工后的某次 save 冲掉 → 重启恢复人设退化成 agent_id → main 续派被人设锁拒绝、
agent_ask 又因实例未复活被拒，彻底失联。

覆盖：
① _capture_state 保留直写键（_ 前缀 / constr_*），provider 不输出的普通键仍清除（覆盖语义保住）；
② provider 抛异常时 extra_state 原样不动；
③ _restore_subagents 缺 _agent_meta 时按声明名推断人设并补写回存档（自愈）；
④ agent_ask 问磁盘恢复条目：走替身（加载它的会话 + 工具按替身重绑 + 声明场景 llm）；
⑤ agent_notify 对磁盘恢复条目给出可行路径的报错。"""
import json
from types import SimpleNamespace

import multiagent
import ask_consumer
from agent import Agent
from registry import AgentRegistry
from session import Session
from tools import Toolbox
import plan_tools as pt


class _FA:
    """最小主 Agent 替身载体（普通 class 实例，可弱引用）。"""


def _fake_main(tmp_path):
    m = _FA()
    m.agent_id = "_main_"
    m.session = SimpleNamespace(session_dir=tmp_path, workspace=tmp_path)
    m.active_plan = None
    m.active_plan_id = None
    m.plan = None
    m.active_spec = None
    m.active_spec_id = None
    m.on_event = None
    m.tool_groups = {}
    m.registry = AgentRegistry()
    m.llm = SimpleNamespace(model_name="stub-main")
    m._scene_llm = SimpleNamespace(model_name="stub-scene")
    m.scenario_client = lambda name, **kw: m._scene_llm
    m.tools = Toolbox()
    for t in pt.make_plan_tools(m):
        m.tools.register(t)
        m.tool_groups[t.name] = "计划"
    return m


# ---------- ①② _capture_state：直写键保留 ----------

def test_capture_state_keeps_direct_written_keys(tmp_path):
    s = Session(system="", llm=None, workspace=tmp_path)
    s.extra_state = {"plan_id": "p_old", "schedules": [{"name": "x"}],
                     "_agent_meta": {"name": "worker", "status": "done"},
                     "_pending_explores": [{"run": 1}],
                     "constr_start_idx": 7, "constr_plan_id": "p_1",
                     "stale_key": 1}
    s._state_provider = lambda: {"plan_id": "p_new", "background_tasks": {}}
    s._capture_state()
    assert s.extra_state["plan_id"] == "p_new"
    assert "schedules" not in s.extra_state        # provider 不输出 = 已清空（覆盖语义保住）
    assert "stale_key" not in s.extra_state         # 非直写键以 provider 为准
    assert s.extra_state["_agent_meta"] == {"name": "worker", "status": "done"}
    assert s.extra_state["_pending_explores"] == [{"run": 1}]
    assert s.extra_state["constr_start_idx"] == 7 and s.extra_state["constr_plan_id"] == "p_1"


def test_capture_state_provider_exception_leaves_state(tmp_path):
    s = Session(system="", llm=None, workspace=tmp_path)
    s.extra_state = {"plan_id": "p_keep", "_agent_meta": {"name": "worker"}}

    def boom():
        raise RuntimeError("provider 挂了")

    s._state_provider = boom
    s._capture_state()
    assert s.extra_state == {"plan_id": "p_keep", "_agent_meta": {"name": "worker"}}


# ---------- ③ 重启恢复：人设推断 + 自愈回写 ----------

def test_restore_infers_persona_and_heals_archive(tmp_path, monkeypatch):
    agents = tmp_path / "agents"
    d1 = agents / "worker-p_abc12345"
    d1.mkdir(parents=True)
    (d1 / "extra_state.json").write_text(json.dumps({"plan_id": None, "remote_servers": {}}),
                                         encoding="utf-8")
    (d1 / "meta.json").write_text(json.dumps({"system": "", "provider": "glm-official-flash"}),
                                  encoding="utf-8")
    d2 = agents / "wiki-updater_4"
    d2.mkdir()
    (d2 / "extra_state.json").write_text("{}", encoding="utf-8")
    d3 = agents / "mystery"                      # 对不上任何声明 → 退回 agent_id（旧行为）
    d3.mkdir()
    declared = {"worker": tmp_path / "worker.yml", "wiki-updater": tmp_path / "wiki-updater.yml"}
    for p in declared.values():
        p.write_text("name: x", encoding="utf-8")
    monkeypatch.setattr(multiagent, "_agent_def_path",
                        lambda n: declared.get(n) or (tmp_path / "nope.yml"))

    fake = SimpleNamespace(registry=AgentRegistry(), agent_id="_main_",
                           session=SimpleNamespace(session_dir=tmp_path))
    fake._heal_agent_meta = Agent._heal_agent_meta          # 真实 Agent 上是 staticmethod
    fake._infer_persona_name = Agent._infer_persona_name
    Agent._restore_subagents(fake)

    reg = fake.registry
    e1 = reg.lookup("worker-p_abc12345")
    assert e1.name == "worker" and e1.status == "done" and e1.model == "glm-official-flash"
    assert e1.agent is None
    assert reg.lookup("wiki-updater_4").name == "wiki-updater"
    assert reg.lookup("mystery").name == "mystery"
    es = json.loads((d1 / "extra_state.json").read_text(encoding="utf-8"))
    assert es["_agent_meta"]["name"] == "worker" and es["_agent_meta"]["model"] == "glm-official-flash"
    assert es["remote_servers"] == {}             # 原有键不动
    assert es["plan_id"] is None


def test_infer_persona_prefers_longest_declared_prefix(tmp_path, monkeypatch):
    declared = {n: tmp_path / f"{n}.yml" for n in ("worker", "worker-p")}
    for p in declared.values():
        p.write_text("name: x", encoding="utf-8")
    monkeypatch.setattr(multiagent, "_agent_def_path",
                        lambda n: declared.get(n) or (tmp_path / "nope.yml"))
    assert Agent._infer_persona_name("worker-p_7dffae8b") == "worker-p"   # 最长命中
    assert Agent._infer_persona_name("worker-s185") == "worker"
    assert Agent._infer_persona_name("nobody-xyz") == "nobody-xyz"         # 无声明退回 agent_id


# ---------- ④ agent_ask 问磁盘恢复条目：替身 + 工具重绑 ----------

def _make_sub_archive(tmp_path, aid="worker-p_test"):
    d = tmp_path / "agents" / aid
    d.mkdir(parents=True)
    (d / "meta.json").write_text(json.dumps({"system": "", "name": "t", "created_at": "0"}),
                                 encoding="utf-8")
    (d / "events.jsonl").write_text("", encoding="utf-8")
    return d


def test_agent_ask_on_disk_restored_entry_uses_standin(tmp_path, monkeypatch):
    m = _fake_main(tmp_path)
    assert "已创建计划" in m.tools.call("create_plan", {"title": "主计划", "steps": ["a"]})
    m.registry.register("worker-p_test", "worker", "subagent", "glm-official-flash", agent=None,
                        task="(历史任务)", status="done")
    _make_sub_archive(tmp_path)
    seen = {}

    def fake_answer(target, question, asker=""):
        seen["target"] = target
        # 替身名下的计划工具：读它自己（无活动计划）——不是 main 的计划
        return target.tools.call("update_plan", {"step": 1, "status": "completed"})

    monkeypatch.setattr(ask_consumer, "answer_ask", fake_answer)
    ask = next(t for t in multiagent.make_communication_tools(m) if t.name == "agent_ask").func
    out = ask("worker-p_test", "你的收口报告？")
    assert out.startswith("[worker-p_test 回答] ")
    assert "还没有活动计划" in out                       # 计划工具已按替身重绑
    assert m.active_plan["steps"][0]["status"] == "pending"   # main 的计划没被动
    t = seen["target"]
    assert t.agent_id == "worker-p_test"
    assert isinstance(t.session, Session) and t.session.session_dir.name == "worker-p_test"
    assert t.llm is m._scene_llm                          # 声明场景（worker.yml model: agent-work）的 client


def test_agent_ask_disk_entry_without_archive_is_rejected(tmp_path, monkeypatch):
    m = _fake_main(tmp_path)
    m.registry.register("worker-p_ghost", "worker", "subagent", "glm-official-flash", agent=None,
                        status="done")
    ask = next(t for t in multiagent.make_communication_tools(m) if t.name == "agent_ask").func
    out = ask("worker-p_ghost", "在吗？")
    assert out.startswith("[错误]") and "agent_prompt" in out and "磁盘也无存档" in out


# ---------- ⑤ agent_notify 对磁盘恢复条目的报错 ----------

def test_agent_notify_on_disk_restored_entry_gives_a_path(tmp_path):
    m = _fake_main(tmp_path)
    m.registry.register("worker-p_test", "worker", "subagent", "glm-official-flash", agent=None,
                        status="done")
    notify = next(t for t in multiagent.make_communication_tools(m) if t.name == "agent_notify").func
    out = notify("worker-p_test", "在吗？")
    assert out.startswith("[错误]") and "agent_prompt(name='worker'" in out and "agent_ask" in out
