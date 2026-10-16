"""技能装卸闭环（skill_equip / skill_unequip 生命周期，2026-10-16 用户裁定）单测。

覆盖（装配地基 apply_extras_to_session 之上的生命周期补全）：
- equip 登记+标记：插段/钩子项带 _skill_src 标记；session._equipped_skills 登记
  （services=自己拉起的声明服务 / item_keys=插段概览；纯内存不进持久层）；
- frontmatter services: 声明拉起（_ensure_agent_services 同款逻辑）：
  探测活→复用不拥有（不记 started）；死→拉起记入登记；unload 只停自己拉起的；
- unequip 撤净：assembly_plan/hook_specs 无该技能残留；其它技能/宿主项不动；
  自有服务停、复用跳过；登记清；
- replace 幂等：二次 equip 同名 = 撤旧（插段+自有服务）再装新——插段不重复；
- 纯函数层：_apply_extra_assemblies 标记透传 + _revoke_skill_items 多技能撤 A 保 B；
- 重启丢失容错：登记空时 unequip 尽力按标记撤 + 提示；未装备明确提示。

隔离：tmp_path 技能包 + FakeAgent/FakeSvc + monkeypatch
（Popen / _pipe_read_until / _RUNTIME_AGENT 全 mock——不打真子进程、不碰真服务池）。
"""
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import pytest

import multiagent as ma
import agent_config as ac
from session import Session


# ── 测试基建（FakeAgent/FakeSvc + 技能包构造，全 mock 无真进程） ──

class FakeSvc:
    """ServiceManager 假体：start/stop 记录 + 同名在跑拒绝（对齐真 ServiceManager 语义）。"""

    def __init__(self):
        self.started = []   # (name, cmd, cwd)
        self.stopped = []
        self._alive = set()

    def start(self, name, cmd, cwd="", **kw):
        if name in self._alive:
            return f"[已存在同名服务] {name}，先 stop_service 再启动"
        self._alive.add(name)
        self.started.append((name, cmd, cwd))
        return f"✅ 后台服务「{name}」已启动"

    def stop(self, name):
        self.stopped.append(name)
        self._alive.discard(name)
        return f"✅ 已停止「{name}」"


class FakeAgent:
    def __init__(self, session, services):
        self.agent_id = "_main_"
        self.session = session
        self.services = services


class FakeProc:
    """Popen 假体：skill_equip 只用 pid / poll / kill（stdout 只被 mock 掉的 _pipe_read_until 摸）。"""

    def __init__(self):
        self.pid = 4321
        self.returncode = None
        self.killed = 0
        self.stdout = None

    def poll(self):
        return self.returncode

    def kill(self):
        self.killed += 1


def _mk_equip_skill(ws_root, name, services_yaml="", assemblies_yaml="", server=True):
    d = ws_root / ".agent" / "skills" / name
    d.mkdir(parents=True, exist_ok=True)
    fm = f"name: {name}\ndescription: d\nwhen_to_use: w\n"
    if services_yaml:
        fm += f"services:\n{services_yaml}"
    if assemblies_yaml:
        fm += f"assemblies:\n{assemblies_yaml}"
    (d / "SKILL.md").write_text(f"---\n{fm}---\n\nSOP body\n", encoding="utf-8")
    if server:
        (d / "server.py").write_text("print('caps')\n", encoding="utf-8")
    return d


ASM_1 = '  - position: "after:steps"\n    ass:\n      - text: "技能段A"\n'
ASM_HOOK = '  - position: "hook:turn_end"\n    ass:\n      - "recap_wf | async"\n'
SVC_1 = '  - lc-svc: python -m svc_a\n'


@pytest.fixture
def rig(tmp_path, monkeypatch):
    """隔离装备环境：tmp workspace + FakeAgent(_RUNTIME_AGENT) + Popen/caps mock。
    同时把 real_tools.WORKSPACE 指向 tmp——否则 apply_extras_to_session 物化宿主
    旧式钩子（.agent/workflows/ meta.hook）时会读真 repo，测试受真实环境噪声干扰。"""
    import real_tools
    monkeypatch.setattr(ma, "WORKSPACE", tmp_path)
    monkeypatch.setattr(ac, "WORKSPACE", tmp_path)
    monkeypatch.setattr(real_tools, "WORKSPACE", tmp_path)
    sess = Session(system="t", workspace=str(tmp_path))
    svc = FakeSvc()
    agent = FakeAgent(sess, svc)
    monkeypatch.setattr(ac, "_RUNTIME_AGENT", agent)
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: FakeProc())
    monkeypatch.setattr(ac, "_pipe_read_until", lambda *a, **k: ("fake-svc 能力清单\nadd N", True))
    return tmp_path, sess, svc, agent


# ── ① equip：登记 + 标记 + services 拉起 ─────────────────────

def test_equip_registers_and_marks(rig):
    ws, sess, svc, agent = rig
    _mk_equip_skill(ws, "lc-a", services_yaml=SVC_1, assemblies_yaml=ASM_1 + ASM_HOOK)
    out = ac.skill_equip("lc-a")
    assert "已装备" in out and "已登记" in out
    # 插段/钩子项带 _skill_src 标记
    assert any(it.get("_skill_src") == "lc-a" for it in sess.assembly_plan)
    ours = [it for it in sess.hook_specs["turn_end"] if it.get("_skill_src") == "lc-a"]
    assert len(ours) == 1 and ours[0]["value"] == "recap_wf"
    # 装备登记：services=自己拉起的声明服务；item_keys=插段概览
    reg = sess._equipped_skills["lc-a"]
    assert reg["services"] == ["lc-svc"]
    assert reg["item_keys"] and any("after:steps" in k for k in reg["item_keys"])
    assert ("lc-svc", "python -m svc_a", str(ws)) in svc.started   # 真拉起（cwd=workspace）


def test_equip_service_reuse_not_owned(rig, monkeypatch):
    """services 带 reuse=URL：探测活→复用（不记 started 不停）；死→拉起（记 started 会被停）。"""
    ws, sess, svc, agent = rig
    _mk_equip_skill(ws, "lc-r", services_yaml='  - frame-svc: python -m frame | reuse=http://127.0.0.1:9\n')
    monkeypatch.setattr(ma, "_probe_alive", lambda url, timeout=2.0: (True, "HTTP 200 image/*"))
    out1 = ac.skill_equip("lc-r")
    assert "复用已活" in out1                                       # 探测活 → 复用不动
    assert sess._equipped_skills["lc-r"]["services"] == []          # 复用——不拥有
    assert svc.started == [] and svc.stopped == []
    # 探测死 → 拉起并登记
    monkeypatch.setattr(ma, "_probe_alive", lambda url, timeout=2.0: (False, "连接拒绝"))
    ac.skill_equip("lc-r")     # replace 分支：旧登记（复用，services=[]）撤旧无停
    assert sess._equipped_skills["lc-r"]["services"] == ["frame-svc"]
    assert len(svc.started) == 1


# ── ② unequip：撤净 + 其它来源不碰 + 服务停 ─────────────────

def test_unequip_revokes_cleanly_keeps_others(rig):
    ws, sess, svc, agent = rig
    _mk_equip_skill(ws, "lc-a", services_yaml=SVC_1, assemblies_yaml=ASM_1 + ASM_HOOK)
    _mk_equip_skill(ws, "lc-b", assemblies_yaml=ASM_1.replace("技能段A", "技能段B"))
    ac.skill_equip("lc-a")
    ac.skill_equip("lc-b")
    # 手工补一个宿主原有风格项（无标记）——验证不吞
    sess.assembly_plan.append({"kind": "text", "text": "宿主项"})
    n_a_plan = sum(1 for it in sess.assembly_plan if it.get("_skill_src") == "lc-a")
    assert n_a_plan >= 1

    out = ac.skill_unequip("lc-a")
    assert "已卸载" in out and "撤销插段 1 项 / 钩子 1 项" in out
    assert "停自有服务 lc-svc" in out and "lc-svc" in svc.stopped
    # 撤净：lc-a 无残留；lc-b 与宿主项保留
    assert not any(it.get("_skill_src") == "lc-a" for it in sess.assembly_plan)
    assert not any(it.get("_skill_src") == "lc-a" for pos in sess.hook_specs for it in sess.hook_specs[pos])
    assert any(it.get("_skill_src") == "lc-b" for it in sess.assembly_plan)
    assert any(it.get("text") == "宿主项" for it in sess.assembly_plan)
    assert "lc-a" not in sess._equipped_skills and "lc-b" in sess._equipped_skills


def test_unequip_not_equipped(rig):
    ws, sess, svc, agent = rig
    out = ac.skill_unequip("no-such")
    assert out.startswith("[未装备]")
    # 装了再卸，二次卸也提示未装备
    _mk_equip_skill(ws, "lc-a", assemblies_yaml=ASM_1)
    ac.skill_equip("lc-a")
    ac.skill_unequip("lc-a")
    assert ac.skill_unequip("lc-a").startswith("[未装备]")


# ── ③ replace 幂等：二次 equip 不重复插段、旧自有服务停 ─────

def test_replace_idempotent(rig):
    ws, sess, svc, agent = rig
    _mk_equip_skill(ws, "lc-a", services_yaml=SVC_1, assemblies_yaml=ASM_1 + ASM_HOOK)
    ac.skill_equip("lc-a")
    n1 = sum(1 for it in sess.assembly_plan if it.get("_skill_src") == "lc-a")
    n1_hook = len(sess.hook_specs["turn_end"])
    out = ac.skill_equip("lc-a")     # 二次装备 = replace
    assert "replace" in out
    n2 = sum(1 for it in sess.assembly_plan if it.get("_skill_src") == "lc-a")
    assert n2 == n1 and len(sess.hook_specs["turn_end"]) == n1_hook   # 不重复插段
    assert svc.stopped == ["lc-svc"]                                  # 旧自有服务被 replace 停掉
    assert sess._equipped_skills["lc-a"]["services"] == ["lc-svc"]    # 新登记指向新实例
    assert len([s for s in svc.started if s[0] == "lc-svc"]) == 2     # 停旧→拉新


# ── ④ 纯函数层：标记透传 + 撤 A 保 B ─────────────────────────

def test_apply_marks_and_revoke_pure():
    def ex(name, pos):
        return {"position": pos, "action": "after", "anchor": "steps",
                "_skill_src": name, "items": [{"kind": "text", "text": name}]}
    base = [{"kind": "seg", "name": "steps"}, {"kind": "seg", "name": "tail"}]
    plan, hooks, errs = ma._apply_extra_assemblies(
        base, None, [ex("A", "after:steps"), ex("B", "after:steps"),
                     {"position": "hook:turn_end", "action": "hook", "anchor": "turn_end",
                      "_skill_src": "A", "items": [{"kind": "workflow", "value": "wf_a"}]}])
    assert errs == []
    assert [it["text"] for it in plan if it.get("kind") == "text"] == ["A", "B"]   # 稳定追加
    plan2, hooks2, n_seg, n_hook = ma._revoke_skill_items(plan, hooks, "A")
    assert (n_seg, n_hook) == (1, 1)
    assert [it["text"] for it in plan2 if it.get("kind") == "text"] == ["B"]       # 撤 A 保 B
    assert hooks2 == {}                                                            # 钩子位撤空删位
    assert len(plan2) == len(plan) - 1


def test_revoke_none_and_no_match():
    plan, hooks, n_seg, n_hook = ma._revoke_skill_items(None, None, "X")   # 宿主未物化快照
    assert plan is None and hooks is None and (n_seg, n_hook) == (0, 0)
    p2, h2, n2s, n2h = ma._revoke_skill_items(
        [{"kind": "seg", "name": "steps"}], {"turn_end": [{"kind": "workflow", "value": "w"}]}, "X")
    assert n2s == 0 and n2h == 0 and h2 == {"turn_end": [{"kind": "workflow", "value": "w"}]}


# ── ⑤ 重启丢失容错：登记空 → 尽力按标记撤 + 提示 ────────────

def test_lost_registry_best_effort(rig):
    ws, sess, svc, agent = rig
    _mk_equip_skill(ws, "lc-a", services_yaml=SVC_1, assemblies_yaml=ASM_1)
    ac.skill_equip("lc-a")
    sess._equipped_skills = {}    # 模拟重启：登记丢失，快照仍带标记
    out = ac.skill_unequip("lc-a")
    assert "登记丢失" in out and "尽力" in out
    assert not any(it.get("_skill_src") == "lc-a" for it in sess.assembly_plan)   # 撤净
    assert svc.stopped == []     # 声明服务无从追溯——不停（提示已给出）


def test_ensure_agent_services_started_reused_semantics(tmp_path, monkeypatch):
    """_ensure_agent_services 返回语义：started=真拉起；reused=复用；同名在跑/异常不记。"""
    class A:
        class session:
            workspace = str(tmp_path)
        services = FakeSvc()
    monkeypatch.setattr(ma, "_probe_alive", lambda url, timeout=2.0: (True, "alive"))
    r = ma._ensure_agent_services({"services": [{"s1": "cmd1 | reuse=http://x"}]}, A)
    assert r == {"started": [], "reused": ["s1"]}
    monkeypatch.setattr(ma, "_probe_alive", lambda url, timeout=2.0: (False, "dead"))
    r2 = ma._ensure_agent_services({"services": [{"s1": "cmd1"}]}, A)
    assert r2 == {"started": ["s1"], "reused": []}
    A.services._alive.add("s1")   # 同名在跑 → start 拒绝 → 不记 started（不拥有）
    r3 = ma._ensure_agent_services({"services": [{"s1": "cmd1"}]}, A)
    assert r3 == {"started": [], "reused": []}
