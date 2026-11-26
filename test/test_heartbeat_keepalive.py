"""白名单长等工具执行期间的心跳续期（用户实锤 2026-11-26）。

sleep(1800) 期间心跳不动 → 巡检（agent_watchdog.stuck_candidates）按 stuck_secs（默认 1200s）
判静默 → 20 分钟拉 doctor。_HeartbeatKeeper 在白名单工具执行期间按间隔刷 agent._hb_ts；
工具返回即停；累计超 _HB_KEEPALIVE_MAX 停止续期（真卡死留给出巡检）。
"""
import sys
import time
import types
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "src"))
sys.path.insert(0, str(_ROOT / "src" / "assets" / "tools_builtin"))

import agent as agent_mod  # noqa: E402
import misc_tools  # noqa: E402
from agent import Agent, _HeartbeatKeeper  # noqa: E402
from agent_watchdog import stuck_candidates  # noqa: E402


def _fast(monkeypatch, interval=0.02, max_secs=60.0):
    """把续期间隔/上限缩到测试尺度（生产 30s / 3600s）。"""
    monkeypatch.setattr(agent_mod, "_HB_KEEPALIVE_INTERVAL", interval)
    monkeypatch.setattr(agent_mod, "_HB_KEEPALIVE_MAX", max_secs)


def test_keeper_refreshes_heartbeat_while_active(monkeypatch):
    _fast(monkeypatch, interval=0.02)
    ag = types.SimpleNamespace(_hb_ts=0.0)
    with _HeartbeatKeeper(ag, "sleep"):
        assert ag._hb_ts > 0                 # 进入即刷
        t1 = ag._hb_ts
        time.sleep(0.07)
        assert ag._hb_ts > t1                # 执行期间持续续期
    t2 = ag._hb_ts
    time.sleep(0.06)
    assert ag._hb_ts == t2                   # 退出即停


def test_keeper_stops_after_max(monkeypatch):
    _fast(monkeypatch, interval=0.02, max_secs=0.08)
    ag = types.SimpleNamespace(_hb_ts=0.0)
    with _HeartbeatKeeper(ag, "stuck-tool"):
        time.sleep(0.30)                     # 远超上限：续期线程应已自行退出
    t = ag._hb_ts
    time.sleep(0.06)
    assert ag._hb_ts == t, "超上限后不应再续期"


def test_watchdog_no_longer_flags_sleeping_agent(monkeypatch):
    """端到端语义（纯函数拼装）：续期期间 stuck_candidates 判不出卡死；续期上限过后恢复判定。"""
    _fast(monkeypatch, interval=0.02, max_secs=0.5)
    stuck = 0.12                             # "卡死阈值"（测试缩小；生产 1200s）
    ag = types.SimpleNamespace(_hb_ts=0.0, _react_running=True)
    entry = types.SimpleNamespace(agent=ag, status="running", agent_id="a1")

    ag._hb_ts = time.time() - 0.3
    assert len(stuck_candidates([entry], time.time(), stuck, {})) == 1        # 对照：无续期 → 判卡死

    with _HeartbeatKeeper(ag, "sleep"):
        time.sleep(0.3)                                                       # 远超阈值，但仍在续期
        assert stuck_candidates([entry], time.time(), stuck, {}) == []         # 续期期间不判
        time.sleep(0.45)                                                      # 越过续期上限
        assert len(stuck_candidates([entry], time.time(), stuck, {})) == 1     # 上限后恢复判定（真卡死仍可查出）


def test_guarded_whitelist_path_uses_keeper(monkeypatch):
    """_exec_tool_guarded：白名单分支包续期器（进入即刷、返回时心跳新鲜）；非白名单仍走线程池限时。"""
    _fast(monkeypatch, interval=0.02, max_secs=0.5)
    fake = types.SimpleNamespace(_hb_ts=0.0, _TOOL_GUARD_EX=Agent._TOOL_GUARD_EX)
    fake._exec_tool = lambda name, args: (misc_tools.sleep(args.get("seconds", 0)) if name == "sleep"
                                          else f"ran {name}")
    monkeypatch.setattr(agent_mod, "_TOOL_TIMEOUT_WHITELIST", frozenset({"sleep"}))

    r = Agent._exec_tool_guarded(fake, "sleep", {"seconds": 0.12})            # 真 sleep（短）
    assert r == "已等待 0.12 秒"
    assert time.time() - fake._hb_ts < 0.1                                    # 工具返回时心跳仍新鲜

    assert Agent._exec_tool_guarded(fake, "other", {}) == "ran other"         # 非白名单正常路径不变

    fake._exec_tool = lambda name, args: time.sleep(2.5) or "late"            # 非白名单卡死 → 既有硬超时注入
    r3 = Agent._exec_tool_guarded(fake, "other2", {}, timeout=1)
    assert r3.startswith("[看门狗] 工具 other2 执行超")
