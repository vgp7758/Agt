"""回退链默认按 default 场景处理（用户裁定 2026-11-27：「所有调用场景 fallback 默认都按 default 处理，选了就用选的」）。

三处单一真源消费点：
- LLMClient 未选链分支（fallback_chain=None）→ default 场景链（此前读旧全局键 settings.fallback_chain，
  与 default 分叉：本机实锤旧键 proxy,deepseek ≠ default glm-official,proxy,basalt）
- Agent._react_chain：yml 未声明 fallback → default 场景链（此前 None = react 无回退）
- 子 Agent 卡片直配：_card_chain = [卡片] + default（此前卡片直配 react 无回退）
显式选链（yml fallback / 场景名 / 卡片 / 显式空链隔离）一律「选了就用选的」，不变。
"""
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import agent as agent_mod  # noqa: E402
import config  # noqa: E402
import llm_client  # noqa: E402
import multiagent  # noqa: E402

_DEF = ["glm-official", "proxy", "basalt"]
_SCEN = {"default": list(_DEF), "short-req": ["utility"]}
_CARDS = {"glm-official": {}, "moss": {}, "deepseek": {}}


def _setup(monkeypatch, *, default=None, legacy="proxy,deepseek"):
    scen = dict(_SCEN)
    if default is not None:
        scen["default"] = list(default)
    monkeypatch.setattr(config, "load_scenarios", lambda *a, **k: dict(scen))
    monkeypatch.setattr(config, "resolve_scenario", lambda n: list(scen.get(n) or scen["default"]))
    monkeypatch.setattr(config, "MODELS", dict(_CARDS))
    monkeypatch.setattr(config, "load_runtime_settings",
                        lambda *a, **k: {"fallback_chain": legacy, "fallback_policy": "reset"})
    monkeypatch.setattr(config, "get_profile",
                        lambda n: {"base_url": "http://127.0.0.1:1/v1", "api_key": "k",
                                   "api_tokens": ["k"], "model": n, "vision": False})


# ---------- LLMClient：未选链 → default 场景链 ----------

def test_llm_client_unselected_uses_default_scene(monkeypatch):
    _setup(monkeypatch)
    c = llm_client.LLMClient(model_name="glm-official")
    assert c._base_fallback_chain == _DEF                  # default 场景链——不是旧键 proxy,deepseek
    c._rebuild_chain()
    assert c.fallback_chain == ["glm-official", "proxy", "basalt"]


def test_llm_client_unselected_legacy_only_when_default_empty(monkeypatch):
    _setup(monkeypatch, default=[], legacy="proxy,deepseek")
    c = llm_client.LLMClient(model_name="glm-official")
    assert c._base_fallback_chain == ["proxy", "deepseek"]   # default 空链 → 旧键兼容


def test_llm_client_explicit_chain_wins(monkeypatch):
    _setup(monkeypatch)
    c = llm_client.LLMClient(model_name="glm-official", fallback_chain=["kimi"])
    assert c._base_fallback_chain == ["kimi"] and c._fallback_owned is True


def test_llm_client_explicit_empty_chain_isolates(monkeypatch):
    _setup(monkeypatch)
    c = llm_client.LLMClient(model_name="glm-official", fallback_chain=[])
    c._rebuild_chain()
    assert c.fallback_chain == []                            # 显式空链=隔离（doctor/代看临时 client 语义不变）


# ---------- Agent._react_chain ----------

def test_react_chain_undeclared_uses_default(monkeypatch):
    _setup(monkeypatch)
    fake = types.SimpleNamespace(_declared_fallback=None)
    assert agent_mod.Agent._react_chain(fake) == _DEF        # 此前为 None（react 无回退）


def test_react_chain_declared_wins(monkeypatch):
    _setup(monkeypatch)
    fake = types.SimpleNamespace(_declared_fallback=["kimi"])
    assert agent_mod.Agent._react_chain(fake) == ["kimi"]    # 选了就用选的


def test_react_chain_empty_default_returns_none(monkeypatch):
    _setup(monkeypatch, default=[])
    fake = types.SimpleNamespace(_declared_fallback=None)
    assert agent_mod.Agent._react_chain(fake) is None        # 出厂空链 → 跟随主模型（语义不变）


# ---------- 子 Agent：卡片直配 / 场景 / 悬空 / 复用刷新 ----------

def test_card_direct_gets_default_fallback(monkeypatch):
    _setup(monkeypatch)
    ag = types.SimpleNamespace(model_name="glm-official")
    m, chain = multiagent._resolve_subagent_model(ag, "moss")
    assert m == "moss" and chain == ["moss", "glm-official", "proxy", "basalt"]
    assert multiagent._card_chain("deepseek") == ["deepseek", "glm-official", "proxy", "basalt"]


def test_scene_and_dangling_unchanged(monkeypatch):
    _setup(monkeypatch)
    ag = types.SimpleNamespace(model_name="glm-official")
    assert multiagent._resolve_subagent_model(ag, "short-req") == ("utility", ["utility"])   # 选了场景
    m2, c2 = multiagent._resolve_subagent_model(ag, "spark")                                 # 悬空名
    assert m2 == "glm-official" and c2 == _DEF


def test_reuse_card_refreshes_to_card_plus_default(monkeypatch):
    _setup(monkeypatch)
    assert multiagent._reuse_refresh_chain("moss") == ["moss", "glm-official", "proxy", "basalt"]
    assert multiagent._reuse_refresh_chain("default") == _DEF