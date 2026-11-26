"""复用子 Agent 实例的链刷新决策（用户实锤 2026-11-26）。

背景：wiki-updater 的 yml 写 model: spark——本机既非场景也非卡片（悬空名），创建时按
default 场景链兜底；旧逻辑复用路径只对"场景名"刷新链，悬空名永不刷新 → 长寿命实例一直
带着创建当时的旧链（[moss]），settings 里改了 default 链也不跟随，moss 欠费后仍只试 moss。
_reuse_refresh_chain：场景名 → 该场景最新链；悬空名/空 → default 最新链；卡片名 → None。
"""
import ast
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import config  # noqa: E402
from multiagent import _reuse_refresh_chain  # noqa: E402

_SCEN = {"default": ["glm-official", "basalt", "proxy"], "short-req": ["utility"], "empty": []}
_CARDS = {"moss": {}, "glm-official": {}, "utility": {}}


def _setup(monkeypatch, resolve_raises=False):
    def _resolve(name):
        if resolve_raises:
            raise RuntimeError("settings 损坏")
        return list(_SCEN.get(name, []))
    # multiagent 里 `import config` 与这里 import 的是同一模块对象——patch 模块属性即生效
    monkeypatch.setattr(config, "load_scenarios", lambda: dict(_SCEN))
    monkeypatch.setattr(config, "resolve_scenario", _resolve)
    monkeypatch.setattr(config, "MODELS", dict(_CARDS))


def test_scenario_name_refreshes_with_its_own_chain(monkeypatch):
    _setup(monkeypatch)
    assert _reuse_refresh_chain("short-req") == ["utility"]
    assert _reuse_refresh_chain(" default ") == ["glm-official", "basalt", "proxy"]


def test_dangling_name_follows_latest_default_chain(monkeypatch):
    _setup(monkeypatch)
    assert _reuse_refresh_chain("spark") == ["glm-official", "basalt", "proxy"]   # 用户实锤场景
    # default 链在 settings 里被改 → 下次派活即跟随（不再带着创建当时的旧链）
    monkeypatch.setattr(config, "resolve_scenario",
                        lambda n: ["moss", "utility"] if n == "default" else list(_SCEN.get(n, [])))
    assert _reuse_refresh_chain("spark") == ["moss", "utility"]


def test_empty_declared_model_follows_default_chain(monkeypatch):
    _setup(monkeypatch)
    assert _reuse_refresh_chain("") == ["glm-official", "basalt", "proxy"]
    assert _reuse_refresh_chain(None) == ["glm-official", "basalt", "proxy"]


def test_card_name_keeps_unchanged(monkeypatch):
    _setup(monkeypatch)
    assert _reuse_refresh_chain("moss") is None      # 卡片直配：回退链跟随全局 settings，不动


def test_empty_resolved_chain_means_no_refresh(monkeypatch):
    _setup(monkeypatch)
    assert _reuse_refresh_chain("empty") == []       # falsy → 调用点不应用（与旧行为一致）


def test_resolution_error_keeps_old_chain(monkeypatch):
    _setup(monkeypatch, resolve_raises=True)
    assert _reuse_refresh_chain("spark") is None


def test_reuse_call_site_uses_helper():
    """结构回归：复用路径必须走 _reuse_refresh_chain（旧的内联场景名判定不得回流）。"""
    src = (Path(__file__).resolve().parent.parent / "src" / "multiagent.py").read_text(encoding="utf-8")
    assert '_reuse_refresh_chain((_meta or {}).get("model"))' in src
    assert "if _scen in config.load_scenarios():" not in src
    ast.parse(src)   # 顺带保证可解析
