#!/usr/bin/env python3
"""test_tier_shape_split.py —— meta.json 瘦身拆分 + 档位形态按 provider 建档（plan p_e7f7ebf6 · 步骤 8）。

运行：python -m pytest test/test_tier_shape_split.py -q（仓库根执行）

隔离：AGT_HOME 在 import config/session 之前指向临时目录（paths.AGT_DIR 解析唯一真源）；
session_dir 全部用临时目录——不碰真实 sessions/ 与 ~/.agt/。

覆盖（对应施工牌步骤 8 的 ①-⑦）：
① save 后产生 extra_state.json / system_ledger.json / tier_<p>.json；meta.json 无旧字段、有 provider
② 旧存档（meta 带旧字段、无旁车）load 兼容生效（三组双源回退）
③ tier 文件 provider 字段不匹配 / 损坏 → _read_tier_shape None（回退 recalc）
④ _decide_switch_shape 分支：adopt 命中 / 指纹不符 None / est 超窗 None / tier_policy=recalc None
⑤ adopt_tier_shape 越界钳制（边界/fc/sos；fc<=sos → sos 清零）
⑥ on_switch hook：LLMClient.switch_model 触发 + Agent._on_model_switch adopt/recalc 分流
   （invalidate_projection mock 计数）
⑦ 子 Agent _agent_meta：extra_state.json 写读往返 + meta.json 旧格式 fallback
"""
import json
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

# —— AGT_HOME 隔离（必须在 import config/session 之前）——
_AGT_HOME = tempfile.mkdtemp(prefix="agt_tier_split_home_")
os.environ["AGT_HOME"] = _AGT_HOME

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import config                                                        # noqa: E402
import session as session_mod                                        # noqa: E402
from session import Session, Turn                                    # noqa: E402

import pytest                                                        # noqa: E402


# ── 基建 ──────────────────────────────────────────────────────────────

class FakeLLM:
    """Session/Agent 用到的 llm 面：model_name / vision / 窗口 / 缓存经济学参数。"""

    def __init__(self, name="prov-a", win=100000, vision=False):
        self.model_name = name
        self.vision_supported = vision
        self.max_effective_context_window = win
        self.fold_target_ratio = None
        self.profile_detail_step = None
        self.on_switch = None


def mk_session(tmp: Path, llm=None, n_turns: int = 0) -> Session:
    llm = llm or FakeLLM()
    s = Session(system="测试人设", llm=llm, workspace=str(tmp),
                recent_window_turns=4, max_steps_per_turn=200,
                session_dir=str(tmp / "sess"))
    Path(s.session_dir).mkdir(parents=True, exist_ok=True)   # 旁车写盘需要目录就绪
    for i in range(n_turns):
        s.turns.append(Turn(user_message=f"问题{i}", answer=f"回答{i}"))
    return s


def write_events(sdir: Path, n: int) -> None:
    """手写 events.jsonl（turn_start/turn_end 对）——load 端重放出 n 轮 turns。"""
    lines = []
    for i in range(n):
        lines.append(json.dumps({"event": "turn_start", "user": f"问题{i}", "images": []},
                                ensure_ascii=False))
        lines.append(json.dumps({"event": "turn_end", "answer": f"回答{i}",
                                 "answer_reasoning": "", "summary": ""}, ensure_ascii=False))
    (Path(sdir) / "events.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_shape(s: Session, provider: str, fc=2, bs=None, fp=None, sos=0, sos_text="",
                gs="") -> None:
    """手写 provider 建档（_save_tier_shape 的等价物，便于构造各分支场景）。"""
    s._sidecar_write(s._tier_shape_path(provider), {
        "provider": provider,
        "profile_fp": fp if fp is not None else s._profile_fingerprint(),
        "tiering_mode": "",
        "saved_at": 1, "turns": len(s.turns),
        "tier_boundaries": bs if bs is not None else [0, 1],
        "fold_count": fc, "sos_count": sos, "sos_summary": sos_text,
        "global_summary": gs,
    })


# ── ① save 拆分落盘 ───────────────────────────────────────────────────

def test_save_creates_sidecars_and_slim_meta(tmp_path):
    llm = FakeLLM("prov-a", win=100000)
    s = mk_session(tmp_path, llm, n_turns=3)
    s.extra_state = {"plan": {"id": "p1"}}
    s._tier_boundaries = [0, 1]
    s._planned_fold = 1
    s._last_fold_count = 1
    s._system_ledger = {"last_text": "快照", "appends": [], "dirty": False}
    s.global_summary = "汇总"
    s.save("拆分测试")

    sdir = Path(s.session_dir)
    meta = json.loads((sdir / "meta.json").read_text(encoding="utf-8"))
    for k in ("extra_state", "system_ledger", "global_summary", "tier_boundaries",
              "fold_count", "sos_count", "sos_summary"):
        assert k not in meta, f"meta.json 不应再含旧字段 {k}"
    assert meta["provider"] == "prov-a"
    assert meta.get("profile_fp")
    assert meta.get("name") == "拆分测试"

    # 旁车三件
    es = json.loads((sdir / "extra_state.json").read_text(encoding="utf-8"))
    assert es == {"plan": {"id": "p1"}}
    sl = json.loads((sdir / "system_ledger.json").read_text(encoding="utf-8"))
    assert sl["last_text"] == "快照" and sl["dirty"] is False

    tiers = list(sdir.glob("tier_*.json"))
    assert len(tiers) == 1
    tier = json.loads(tiers[0].read_text(encoding="utf-8"))
    assert tier["provider"] == "prov-a"
    assert tier["tier_boundaries"] == [0, 1]
    assert tier["fold_count"] == 1
    assert tier["turns"] == 3
    assert tier["global_summary"] == "汇总"
    assert tier["profile_fp"] == s._profile_fingerprint()


# ── ② 旧存档 load 兼容（meta 旧字段双源回退） ──────────────────────────

def test_load_old_meta_compat(tmp_path):
    sdir = tmp_path / "old_sess"
    sdir.mkdir()
    (sdir / "meta.json").write_text(json.dumps({
        "name": "旧会话", "created_at": 1700000000.0, "system": "旧人设",
        "global_summary": "旧汇总", "recent_window_turns": 4, "max_steps_per_turn": 200,
        "extra_state": {"plan": {"id": "old"}},
        "tier_boundaries": [], "fold_count": 0,
        "system_ledger": {"last_text": "旧账本", "appends": [], "dirty": False},
        "profile_fp": "",            # 空 → 不触发指纹比对
        "sos_count": 0, "sos_summary": "",
        "saved_at": 1700000000,
    }, ensure_ascii=False), encoding="utf-8")
    s = Session.load(str(sdir), llm=FakeLLM("prov-a", win=100000), workspace=tmp_path)
    assert s.extra_state == {"plan": {"id": "old"}}          # 旁车缺失 → 回退 meta 旧字段
    assert s._system_ledger.get("last_text") == "旧账本"
    assert s.global_summary == "旧汇总"
    assert not getattr(s, "_simple_pending", False)             # 无指纹差异不触发重算


# ── ③ provider 不匹配 / 损坏 → None ──────────────────────────────────

def test_read_tier_shape_provider_mismatch(tmp_path):
    s = mk_session(tmp_path)
    Path(s.session_dir).mkdir(parents=True, exist_ok=True)
    p = s._tier_shape_path("prov-a")
    p.write_text(json.dumps({"provider": "prov-b", "tier_boundaries": []}), encoding="utf-8")
    assert s._read_tier_shape("prov-a") is None               # sanitize 撞名防护
    p.write_text("{bad json", encoding="utf-8")
    assert s._read_tier_shape("prov-a") is None               # 损坏
    assert s._read_tier_shape("") is None                     # 空 provider


# ── ④ _decide_switch_shape 分支 ──────────────────────────────────────

def test_decide_switch_shape_branches(tmp_path, monkeypatch):
    llm = FakeLLM("prov-a", win=100000)
    s = mk_session(tmp_path, llm, n_turns=5)
    monkeypatch.setattr(config, "get_profile", lambda n: {})

    # adopt 命中（指纹一致 + est 未超窗）
    write_shape(s, "prov-a", fc=2, bs=[0, 1])
    shape = s._decide_switch_shape("prov-a")
    assert shape is not None and shape["fold_count"] == 2

    # 指纹不符 → None
    write_shape(s, "prov-a", fc=2, fp="stale|False|1")
    assert s._decide_switch_shape("prov-a") is None

    # est 超窗 → None（大文本 + 小窗口）
    write_shape(s, "prov-a", fc=0, bs=[])
    s.turns[0].user_message = "x" * 50000
    llm.max_effective_context_window = 100
    assert s._decide_switch_shape("prov-a") is None

    # tier_policy=recalc → None（即使有完好建档）
    llm.max_effective_context_window = 100000
    s.turns[0].user_message = "小"
    monkeypatch.setattr(config, "get_profile", lambda n: {"tier_policy": "recalc"})
    write_shape(s, "prov-a", fc=2, bs=[0, 1])
    assert s._decide_switch_shape("prov-a") is None
    # 未知 provider（get_profile 抛 KeyError）→ 缺省 adopt，有建档即命中
    def _boom(n):
        raise KeyError(n)
    monkeypatch.setattr(config, "get_profile", _boom)
    assert s._decide_switch_shape("prov-a") is not None


# ── ⑤ adopt_tier_shape 越界钳制 ──────────────────────────────────────

def test_adopt_tier_shape_clamps(tmp_path):
    # 场景 A：fc<=sos → sos 清零；越界边界剔除；fc 死重剔除
    s = mk_session(tmp_path, n_turns=3)
    s._frozen_renders[0] = (1, [{"role": "x"}])
    s.adopt_tier_shape({"provider": "prov-a",
                        "tier_boundaries": [0, 1, 2, 5, 9],
                        "fold_count": 7, "sos_count": 9, "sos_summary": "旧sos",
                        "global_summary": "汇总", "tiering_mode": ""})
    assert s._planned_fold == 3                                # fc 钳到 len(turns)
    assert s._tier_boundaries == []                            # 全部 < fc（死重剔除）或越界
    assert s._sos_count == 0 and s._sos_text == ""             # fc<=sos → sos 清零
    assert s.global_summary == "汇总"
    assert not s._frozen_renders                               # 冻结渲染清空
    assert s._system_ledger["dirty"] is True                   # 账本置 dirty

    # 场景 B：正常区间
    s2 = mk_session(tmp_path / "b", n_turns=10)
    s2.adopt_tier_shape({"provider": "prov-a",
                         "tier_boundaries": [0, 2, 5, 12],
                         "fold_count": 4, "sos_count": 2, "sos_summary": "叙事",
                         "global_summary": "", "tiering_mode": ""})
    assert s2._tier_boundaries == [5]                          # 0 剔除 / 12 越界 / <fc(4) 死重剔除
    assert s2._planned_fold == 4 and s2._last_fold_count == 4
    assert s2._sos_count == 2 and s2._sos_text == "叙事"        # fc>sos → sos 保留
    assert s2._planned_graduates == 0


# ── ⑥ on_switch hook ─────────────────────────────────────────────────

def test_llm_switch_fires_on_switch(monkeypatch):
    from llm_client import LLMClient
    calls = []
    cli = object.__new__(LLMClient)                            # 绕过 __init__（不建网络客户端）
    cli.on_switch = lambda name, ui, prev=None: calls.append((name, ui,
                                                              (prev or {}).get("model_name")))
    cli.model_name = "old-a"
    cli.vision_supported = False
    cli._user_model = "old-a"
    cli._user_switch_epoch = 0
    cli._apply_profile = lambda p: None
    cli._rebuild_chain = lambda: None
    monkeypatch.setattr(config, "get_profile", lambda n: {"model": n})
    cli.switch_model("new-b", _user_initiated=True)
    assert calls == [("new-b", True, "old-a")]                 # 用户切换触发 + prev 带旧名
    cli.switch_model("new-c", _user_initiated=False)
    assert calls[-1] == ("new-c", False, "new-b")              # 回退路径同样触发


def test_on_model_switch_adopt_vs_recalc(tmp_path, monkeypatch):
    from agent import Agent
    emitted, inval = [], []

    llm = FakeLLM("prov-a", win=100000)
    s = mk_session(tmp_path, llm, n_turns=5)
    monkeypatch.setattr(s, "invalidate_projection",
                        lambda reason, async_rebuild=False: inval.append(reason))

    # 预写 prov-b 建档（指纹按「已在 prov-b 档」算：model_name=prov-b + 同窗口）
    llm.model_name = "prov-b"
    write_shape(s, "prov-b", fc=2, bs=[0, 1], gs="B汇总")
    llm.model_name = "prov-a"                                  # 当前在 prov-a

    fake = SimpleNamespace(model_name="prov-a", llm=llm, session=s,
                           _emit=lambda ev: emitted.append(ev))
    monkeypatch.setattr(config, "get_profile", lambda n: {})

    # adopt 分支：切到 prov-b（llm 已 _apply_profile 新档 → model_name 已是 prov-b）
    llm.model_name = "prov-b"
    Agent._on_model_switch(fake, "prov-b", True, prev={"model_name": "prov-a",
                                                       "vision_supported": False})
    assert inval == []                                          # adopt 不触发重算
    assert s._planned_fold == 2 and s.global_summary == "B汇总"
    assert s._system_ledger["dirty"] is True
    assert any("已采用" in str(e.get("text", "")) for e in emitted)
    # flush 保真旧 provider：tier_prov-a.json 应以旧名+旧指纹写入（内存 shape）
    old_shape = s._read_tier_shape("prov-a")
    assert old_shape and old_shape["profile_fp"] == "prov-a|False|100000"

    # recalc 分支：切到无建档的 prov-c
    llm.model_name = "prov-c"
    Agent._on_model_switch(fake, "prov-c", False, prev={"model_name": "prov-b",
                                                        "vision_supported": False})
    assert len(inval) == 1 and "prov-c" in inval[0]
    assert s._planned_fold == 2                                 # 未被 adopt 改写


# ── ⑦ 子 Agent _agent_meta 写读往返 + meta 旧格式 fallback ─────────────

def test_subagent_meta_roundtrip(tmp_path):
    ad = tmp_path / "agents" / "vision"
    ad.mkdir(parents=True)
    # 新格式：extra_state.json 顶层 _agent_meta
    (ad / "extra_state.json").write_text(json.dumps({
        "_agent_meta": {"agent_id": "vision", "name": "vision", "model": "prov-a",
                        "task": "看图", "caller_id": "_main_", "recap": "", "status": "done"}}),
        encoding="utf-8")
    from agent import Agent
    # _restore_subagents 读端：走真实方法路径太重（要 registry）——直接验证读逻辑同构：
    es = json.loads((ad / "extra_state.json").read_text(encoding="utf-8"))
    am = es.get("_agent_meta") or {}
    assert am["status"] == "done" and am["task"] == "看图"
    # 老格式 fallback：只有 meta.json（extra_state._agent_meta）
    ad2 = tmp_path / "agents" / "old"
    ad2.mkdir(parents=True)
    (ad2 / "meta.json").write_text(json.dumps({
        "name": "old", "extra_state": {"_agent_meta": {"agent_id": "old", "status": "done"}}}),
        encoding="utf-8")
    meta = json.loads((ad2 / "meta.json").read_text(encoding="utf-8"))
    am2 = (meta.get("extra_state") or {}).get("_agent_meta") or {}
    assert am2["agent_id"] == "old"
    # 无 extra_state.json → am 为空（读端回退 meta）
    assert not (ad2 / "extra_state.json").exists()


def test_subagent_restore_prefers_extra_state_json(tmp_path):
    """_restore_subagents 读端双源：extra_state.json 优先，缺失回退 meta.json 旧字段。
    直接构造最小 registry 环境跑真实 _restore_subagents（不建 Agent 实例）。"""
    import threading
    from agent import Agent

    class _Reg:
        def __init__(self):
            self._lock = threading.Lock()
            self._agents = {"_main_": SimpleNamespace(agent_id="_main_")}
            self.registered = []

        def lookup(self, aid):
            return self._agents.get(aid)

        def register(self, aid, name, kind, model, agent=None, task="",
                     status="", caller_id="", recap="", **kw):
            self.registered.append({"agent_id": aid, "name": name, "task": task, "status": status})
            return True

    sdir = tmp_path / "sess"
    (sdir / "agents" / "sub1").mkdir(parents=True)
    (sdir / "agents" / "sub2").mkdir(parents=True)
    # sub1：新格式 extra_state.json；sub2：老格式 meta.json
    (sdir / "agents" / "sub1" / "extra_state.json").write_text(json.dumps(
        {"_agent_meta": {"agent_id": "sub1", "name": "sub1", "model": "prov-a",
                         "task": "t1", "caller_id": "_main_", "recap": "", "status": "done"}}),
        encoding="utf-8")
    (sdir / "agents" / "sub2" / "meta.json").write_text(json.dumps(
        {"extra_state": {"_agent_meta": {"agent_id": "sub2", "name": "sub2", "model": "prov-a",
                                         "task": "t2", "caller_id": "_main_", "recap": "",
                                         "status": "done"}}}),
        encoding="utf-8")
    reg = _Reg()
    sess = SimpleNamespace(session_dir=sdir)
    # 直接调 Unbound method：Agent._restore_subagents(fake_self)
    fake = SimpleNamespace(registry=reg, session=sess, agent_id="_main_")
    Agent._restore_subagents(fake)
    got = {r["agent_id"]: r for r in reg.registered}
    assert "sub1" in got and "sub2" in got
    assert got["sub1"]["task"] == "t1"                            # 新格式：extra_state.json
    assert got["sub2"]["task"] == "t2"                            # 老格式：meta.json fallback


# ── L4 不变量：tier 建档 save/load 往返一致（防折叠螺旋的关键不变量） ────

def test_tier_shape_save_load_roundtrip(tmp_path):
    llm = FakeLLM("prov-a", win=100000)
    s = mk_session(tmp_path, llm, n_turns=6)
    s._tier_boundaries = [0, 2, 5]
    s._planned_fold = 3
    s._last_fold_count = 3
    s._sos_count = 1
    s._sos_text = "叙事摘要"
    s.global_summary = "全局"
    s._system_ledger = {"last_text": "快照", "appends": [], "dirty": False}
    s.save("往返")
    sdir = Path(s.session_dir)
    tier = json.loads((sdir / s._tier_shape_path("prov-a").name).read_text(encoding="utf-8"))
    assert tier["tier_boundaries"] == [0, 2, 5]
    assert tier["fold_count"] == 3 and tier["sos_count"] == 1
    assert tier["sos_summary"] == "叙事摘要" and tier["global_summary"] == "全局"
    write_events(sdir, 6)                                       # load 端重放出 6 轮
    # 重启（新 Session 同 provider 同窗口）→ load 决策树分支①：采用文件形态
    s2 = Session.load(str(sdir), llm=FakeLLM("prov-a", win=100000), workspace=tmp_path)
    assert s2._tier_boundaries == [2, 5]                        # 0 被既有钳制语义剔除
    assert s2._planned_fold == 3 and s2._sos_count == 1
    assert s2._sos_text == "叙事摘要" and s2.global_summary == "全局"
    assert not getattr(s2, "_simple_pending", False)            # 同 provider 不重算
    assert not s2._system_ledger.get("dirty")                   # 指纹一致 → 账本延续
    assert len(s2.turns) == 6


def test_load_provider_mismatch_falls_to_switch_decision(tmp_path, monkeypatch):
    """meta.provider != 当前模型（重启期间换默认模型）：
    A. tier_<cur>.json 存在且指纹一致 → 建档直接采用（分支①优先）；
    B. tier_<cur>.json 缺失 → 分支③ decide 未命中 → _simple_pending → apply_simple_tiering 重算。"""
    llm = FakeLLM("prov-b", win=100000)
    s = mk_session(tmp_path, llm, n_turns=4)
    s.save("换默认")
    sdir = Path(s.session_dir)
    write_events(sdir, 4)
    # 模拟「存档时在 prov-a，重启后默认模型=prov-b」：改 meta.provider
    meta = json.loads((sdir / "meta.json").read_text(encoding="utf-8"))
    meta["provider"] = "prov-a"
    (sdir / "meta.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    (sdir / "tier_prov-b.json").unlink(missing_ok=True)        # 先清掉 save 落的档，走 B 场景
    # 观察 apply_simple_tiering 是否被消化段触发（_simple_pending 在 load 内被消化复位，不可直读）
    ran = []
    monkeypatch.setattr(session_mod.Session, "apply_simple_tiering",
                        lambda self: (ran.append("simple"), 0)[1])
    # B：无 prov-b 建档 → decide None → _simple_pending → 现状重算（apply_simple_tiering）
    s2 = Session.load(str(sdir), llm=FakeLLM("prov-b", win=100000), workspace=tmp_path)
    assert ran == ["simple"]
    # A：手写 prov-b 建档（指纹按当前 prov-b 档算）→ load 采用建档，不重算
    write_shape(s, "prov-b", fc=2, bs=[0, 1], gs="B档")
    s3 = Session.load(str(sdir), llm=FakeLLM("prov-b", win=100000), workspace=tmp_path)
    assert ran == ["simple"]                                   # 未追加——分支①直接采用
    assert s3._planned_fold == 2 and s3.global_summary == "B档"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
