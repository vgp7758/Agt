"""团队看板「本机 Agent」按近期活跃倒序（用户提案 2026-11-27）。

AgentEntry.last_active：事件流经 server._broadcast 时刷新（放在「无 WS 客户端即返回」之前——
没开浏览器时也在打点）；派活注册/交互 touch 时置当前；磁盘唤醒的已结束条目=0（未知，排后）。
/api/dash 的 team 条目带 last_active、载荷带 now（前端算相对时间，免浏览器时钟偏差）。
"""
import asyncio
import sys
import time
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import remote_tools  # noqa: E402
import server  # noqa: E402
from registry import AgentRegistry  # noqa: E402


def test_register_running_marks_active_done_is_unknown():
    reg = AgentRegistry()
    e1 = reg.register("a", "w", "subagent", "m", None)                    # 默认 status=running（派活）
    assert abs(e1.last_active - time.time()) < 5
    e2 = reg.register("b", "w", "subagent", "m", None, status="done")     # 磁盘唤醒的已结束条目
    assert e2.last_active == 0.0
    # 重新派活复用 id → 新条目重新打点
    e1.last_active = 0.0
    e3 = reg.register("a", "w", "subagent", "m", None)
    assert e3 is not e1 and e3.last_active > 0


def test_touch_and_mark_active_update_last_active():
    reg = AgentRegistry()
    e = reg.register("a", "w", "subagent", "m", None, status="done")
    assert e.last_active == 0.0
    reg.touch("a")
    assert e.last_active > 0
    e.last_active = 0.0
    reg.mark_active("a")
    assert e.last_active > 0
    reg.mark_active("nope")                    # 未知 id：静默不炸
    e.last_active = 0.0
    reg.mark_active("a", ts=123.5)
    assert e.last_active == 123.5


def test_broadcast_marks_active_even_without_clients(monkeypatch):
    reg = AgentRegistry()
    e = reg.register("a", "w", "subagent", "m", None, status="done")
    monkeypatch.setattr(server, "_agent", types.SimpleNamespace(registry=reg))
    monkeypatch.setattr(server, "_clients", {})            # 没开浏览器——仍要打点
    monkeypatch.setattr(server, "_main_loop", None)
    before = time.time()
    server._broadcast({"type": "step", "agent_id": "a"})
    assert e.last_active >= before
    server._broadcast({"type": "system"})                  # 无 agent_id
    server._broadcast({"type": "step", "agent_id": "ghost"})   # 未注册 id
    server._broadcast({"type": "step", "agent_id": "a", "text": "x" * 10})


def test_broadcast_no_registry_no_crash(monkeypatch):
    monkeypatch.setattr(server, "_agent", None)
    monkeypatch.setattr(server, "_clients", {})
    server._broadcast({"type": "step", "agent_id": "a"})   # 无主 Agent（服务未接入）


def test_api_dash_emits_last_active_and_now(monkeypatch, tmp_path):
    reg = AgentRegistry()
    e = reg.register("x", "worker", "subagent", "m", None)
    fake = types.SimpleNamespace(registry=reg, recap_observe=lambda k, v: None, _recap_seen={},
                                 services=None, scheduler=None, session=None)
    monkeypatch.setattr(server, "_agent", fake)
    monkeypatch.setattr(remote_tools, "REMOTE_SERVERS", {})   # 不探测真实远端实例
    monkeypatch.chdir(tmp_path)                                # 不读真实 .agent/projects.json（其成员探测会打远端）
    out = asyncio.run(server.api_dash())
    assert "now" in out and abs(out["now"] - time.time()) < 5
    row = next(r for r in out["team"] if r["agent_id"] == "x")
    assert row["last_active"] == e.last_active
