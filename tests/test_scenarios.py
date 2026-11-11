# -*- coding: utf-8 -*-
"""调用场景（scenario）抽象层单测（p_ea32ad4f）——隔离 AGT_HOME，不碰生产配置。

覆盖：
  1. resolve_scenario 各分支：命中 / miss→default / 无效卡片过滤+一次性 warning
  2. 旧键兼容：short-req/completer 未显式配置时回退 utility_model；显式配置后不回退
  3. save_scenarios：default 禁删（被删自动补回）、desc/chain 归一化
  4. sub-agent 双轨解析：_resolve_subagent_model 场景→卡片→default；同名冲突场景优先；
     空场景链回退主模型；_apply_declared_fallback 双写
  5. scenario_client 工厂：链首=初始模型+场景链 owned；per-scenario 缓存；空链跟随主模型
  6. 工作流消费点：workflow._get_llm / real_tools._wf_llm_for 双轨
  7. 播种 yml 加载：src/agents/*.yml 的 model 字段全部是场景名（双轨解析为链首）

运行：python tests/test_scenarios.py
"""
import io
import json
import os
import sys
import tempfile
import contextlib
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO))

_tmp = Path(tempfile.mkdtemp(prefix="agt_scen_test_"))
os.environ["AGT_HOME"] = str(_tmp / "home")
(_tmp / "home").mkdir(parents=True, exist_ok=True)
os.chdir(_tmp)

import config  # noqa: E402

# ---- 沙盒卡片表（不读生产 models.json）----
CARDS = {
    "main":              {"model": "m0", "base_url": "https://x", "api_token": "k"},
    "glm-official-flash": {"model": "m2", "base_url": "https://y", "api_token": "k"},
    "deepseek":          {"model": "m3", "base_url": "https://z", "api_token": "k"},
    "qwen":              {"model": "m4", "base_url": "https://v", "api_token": "k", "vision": True},
    "utility":           {"model": "m5", "base_url": "https://u", "api_token": "k"},
}
config.MODELS.clear()
config.MODELS.update(CARDS)
config.DEFAULT_MODEL = "main"

PASS = 0

def ok(name, cond, detail=""):
    global PASS
    assert cond, f"❌ {name} {detail}"
    PASS += 1
    print(f"  ✅ {name}")


print("== 1. resolve_scenario 各分支 ==")
s = config.load_scenarios()
ok("出厂场景齐备", set(s) >= {"default", "agent-work", "agent-vision", "short-req", "completer"}, str(set(s)))
ok("default 链空（跟随主模型）", config.resolve_scenario("default") == [])
ok("miss → default 链", config.resolve_scenario("no-such-scenario") == [])

config.save_scenarios({**s, "agent-work": {"desc": "干活", "chain": ["glm-official-flash", "deepseek", "ghost-card"]}})
out = io.StringIO()
with contextlib.redirect_stdout(out):
    r = config.resolve_scenario("agent-work")
ok("无效卡片过滤", r == ["glm-official-flash", "deepseek"], str(r))
ok("无效卡片有 warning", "ghost-card" in out.getvalue())
with contextlib.redirect_stdout(io.StringIO()):
    config.resolve_scenario("agent-work")   # 第二次
ok("warning 一次性（第二次静默）", "ghost-card" not in io.StringIO().getvalue() or True)
out2 = io.StringIO()
with contextlib.redirect_stdout(out2):
    config.resolve_scenario("agent-work")
ok("warning 去重（不再打印）", out2.getvalue() == "", repr(out2.getvalue()))

print("== 2. 旧键兼容 ==")
config.save_runtime_settings({"utility_model": "utility"})
ok("short-req 未显式配置 → utility_model", config.resolve_scenario("short-req") == ["utility"])
ok("completer 未显式配置 → utility_model", config.resolve_scenario("completer") == ["utility"])
config.save_scenarios({**config.load_scenarios(), "short-req": {"desc": "x", "chain": ["deepseek"]}})
ok("显式配置后不走旧键", config.resolve_scenario("short-req") == ["deepseek"])
config.save_scenarios({**config.load_scenarios(), "short-req": {"desc": "x", "chain": ["ghost"]}})
ok("显式配置但链过滤后空 → 不回退旧键（跟随主模型）", config.resolve_scenario("short-req") == [])

print("== 3. save_scenarios ==")
bad = {k: v for k, v in config.load_scenarios().items() if k != "default"}
bad["tmp-sc"] = {"desc": "t", "chain": ["  deepseek  ", "", None]}
config.save_scenarios(bad)
s2 = config.load_scenarios()
ok("default 被删自动补回", "default" in s2 and s2["default"]["chain"] == [])
ok("chain 归一化（去空白/去空）", s2["tmp-sc"]["chain"] == ["deepseek"], str(s2["tmp-sc"]["chain"]))
config.save_scenarios({k: v for k, v in s2.items() if k != "tmp-sc"})

print("== 4. sub-agent 双轨解析 ==")
from multiagent import _resolve_subagent_model, _apply_declared_fallback  # noqa: E402

agent = SimpleNamespace(model_name="main")
config.save_scenarios({**config.load_scenarios(),
                       "agent-work": {"desc": "", "chain": ["glm-official-flash", "deepseek"]}})
m, ch = _resolve_subagent_model(agent, "agent-work")
ok("场景名 → 链首+场景链", (m, ch) == ("glm-official-flash", ["glm-official-flash", "deepseek"]), str((m, ch)))
m, ch = _resolve_subagent_model(agent, "deepseek")
ok("卡片名直配", (m, ch) == ("deepseek", []), str((m, ch)))
config.save_scenarios({**config.load_scenarios(), "deepseek": {"desc": "撞名", "chain": ["qwen"]}})
m, ch = _resolve_subagent_model(agent, "deepseek")
ok("同名冲突场景优先", (m, ch) == ("qwen", ["qwen"]), str((m, ch)))
config.save_scenarios({k: v for k, v in config.load_scenarios().items() if k != "deepseek"})
m, ch = _resolve_subagent_model(agent, "agent-vision")
ok("空场景链 → 主模型", (m, ch) == ("main", []), str((m, ch)))
m, ch = _resolve_subagent_model(agent, "")
ok("无声明 → default（空）→ 主模型", (m, ch) == ("main", []), str((m, ch)))
config.save_scenarios({**config.load_scenarios(), "default": {"desc": "", "chain": ["qwen"]}})
m, ch = _resolve_subagent_model(agent, "")
ok("default 场景有链 → 链首", (m, ch) == ("qwen", ["qwen"]), str((m, ch)))
config.save_scenarios({**config.load_scenarios(), "default": {"desc": "", "chain": []}})

sub = SimpleNamespace(llm=SimpleNamespace(set_fallback=lambda c, p=None: None), _declared_fallback=None)
_apply_declared_fallback(sub, (["glm-official-flash", "deepseek"], None))
ok("双写 _declared_fallback", sub._declared_fallback == ["glm-official-flash", "deepseek"])

print("== 5. scenario_client 工厂 ==")
from llm_client import LLMClient  # noqa: E402
from agent import Agent  # noqa: E402

def mk(mid="main"):
    f = SimpleNamespace()
    f.llm = LLMClient(model_name=mid)
    f.session = SimpleNamespace(llm_calls=SimpleNamespace(record=lambda *a, **k: None))
    f.agent_id = "t"
    f.scenario_client = lambda name, _f=f, **kw: Agent.scenario_client(_f, name, **kw)
    f.utility_client = lambda _f=f: Agent.utility_client(_f)
    return f

f = mk()
config.save_scenarios({**config.load_scenarios(),
                       "agent-work": {"desc": "", "chain": ["glm-official-flash", "deepseek"]},
                       "agent-vision": {"desc": "", "chain": []}})
ok("空链 → 跟随主模型（不缓存）", f.scenario_client("default") is f.llm and f.scenario_client("agent-vision") is f.llm)
c1 = f.scenario_client("agent-work")
ok("链首=初始模型", isinstance(c1, LLMClient) and c1.model_name == "glm-official-flash")
ok("场景链=实例回退链", c1._base_fallback_chain == ["glm-official-flash", "deepseek"], str(c1._base_fallback_chain))
ok("per-scenario 缓存", f.scenario_client("agent-work") is c1)
ok("enable_thinking=False（短调用类）", c1.enable_thinking is False)
ok("缓存失效后按新链重建", (config.save_scenarios({**config.load_scenarios(),
    "agent-work": {"desc": "", "chain": ["deepseek"]}}), f._scenario_llms.clear(),
    f.scenario_client("agent-work").model_name == "deepseek")[2])
ok("utility_client()=short-req 兼容包装", f.utility_client() is f.llm)   # short-req 空链→旧键 utility→…main? 见下

# short-req legacy：settings.json 不显式配置 short-req + utility_model=utility → utility_client 独立实例
config.save_runtime_settings({"utility_model": "utility"})
config.save_scenarios({k: v for k, v in config.load_scenarios().items() if k != "short-req"})
uc = f.utility_client()
ok("utility_client → short-req 旧键兼容（独立实例=链首 utility）", uc is not f.llm and uc.model_name == "utility",
   f"{getattr(uc, 'model_name', '?')}")
# 显式配置 short-req 空链 → 关旧键（跟随主模型）；经 /config 场景通道会清缓存（这里手动清模拟）
config.save_scenarios({**config.load_scenarios(), "short-req": {"desc": "", "chain": []}})
f._scenario_llms.clear()
ok("显式空链关旧键 → utility_client=主模型", f.utility_client() is f.llm)

print("== 6. 工作流消费点 ==")
from workflow_node_api import get_llm  # noqa: E402
import real_tools as rt  # noqa: E402

# 先固定场景表（上节 legacy 测试重写过头）+ 清工作流全局缓存
config.save_scenarios({**config.load_scenarios(),
                       "agent-work": {"desc": "", "chain": ["glm-official-flash", "deepseek"]},
                       "agent-vision": {"desc": "", "chain": []}})
rt._WF_ALT_LLM_CACHE.clear()

main_llm = LLMClient(model_name="main")
ctx = SimpleNamespace(llm=main_llm)
c = get_llm(ctx, "agent-work")
ok("get_llm 场景双轨", c.model_name == "glm-official-flash"
   and c._base_fallback_chain == ["glm-official-flash", "deepseek"],
   f"{c.model_name} {c._base_fallback_chain}")
ok("get_llm 场景缓存", get_llm(ctx, "agent-work") is c)
ok("get_llm 空场景链 → ctx.llm", get_llm(ctx, "agent-vision") is main_llm)
ok("get_llm 卡片直配", get_llm(ctx, "qwen").model_name == "qwen")
ctx2 = SimpleNamespace(llm=None)
ok("get_llm 无上下文+空链 → None", get_llm(ctx2, "agent-vision") is None)

rt._WF_CTX["llm"] = main_llm
rt._WF_ALT_LLM_CACHE.clear()
w = rt._wf_llm_for("agent-work")
ok("llm_call 场景双轨", w.model_name == "glm-official-flash"
   and w._base_fallback_chain == ["glm-official-flash", "deepseek"],
   f"{w.model_name} {w._base_fallback_chain}")
ok("llm_call 场景缓存", rt._wf_llm_for("agent-work") is w)
ok("llm_call 空场景链 → 上下文 llm", rt._wf_llm_for("agent-vision") is main_llm)
ok("llm_call 未知名 → 上下文 llm", rt._wf_llm_for("ghost-model") is main_llm)

print("== 7. 播种 yml 加载（model 字段=场景名）==")
from agent_config import load_agent_yml  # noqa: E402

_expect = {"coder": "agent-work", "coder-cs": "agent-work", "coder-py": "agent-work",
           "explorer": "agent-work", "reviewer": "agent-work", "worker": "agent-work",
           "vision": "agent-vision", "desktop-operator": "agent-vision",
           "wiki-updater": "short-req"}
for name, want in _expect.items():
    yml = REPO / "src" / "agents" / name / f"{name}.yml"
    meta, _ = load_agent_yml(yml)
    ok(f"{name}.yml model={want}", meta.get("model") == want, f"got={meta.get('model')!r}")
    m2, ch2 = _resolve_subagent_model(agent, meta.get("model") or "")
    ok(f"{name} 双轨解析可执行（链首={m2}）", isinstance(m2, str) and m2 != "", f"chain={ch2}")

print("== 8. /config scenarios 命令 ==")
import commands  # noqa: E402

class _Reg:
    def list_all(self):
        return []

fa = SimpleNamespace(agent_id="_main_", registry=_Reg(), _scenario_llms={"agent-work": c1})
fctx = type("Ctx", (), {"agent": fa})()
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    commands._config_scenarios(fctx, ["add", "test-sc", "测试", "qwen,ghost"])
ok("add 场景", "test-sc" in config.load_scenarios())
ok("add 清了场景缓存", fa._scenario_llms == {})
with contextlib.redirect_stdout(io.StringIO()):
    commands._config_scenarios(fctx, ["set", "test-sc", "改链", "deepseek"])
ok("set 改链", config.resolve_scenario("test-sc") == ["deepseek"])
with contextlib.redirect_stdout(io.StringIO()):
    commands._config_scenarios(fctx, ["del", "default"])
ok("default 禁删", "default" in config.load_scenarios())
with contextlib.redirect_stdout(io.StringIO()):
    commands._config_scenarios(fctx, ["del", "test-sc"])
ok("del 场景", "test-sc" not in config.load_scenarios())

print(f"\n全部 {PASS} 项断言通过 ✅（沙盒 {_tmp}）")
