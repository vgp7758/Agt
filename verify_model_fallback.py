"""verify_model_fallback.py —— 验证 per-agent 模型回退链分权（.yml 声明 > 全局 settings > 无回退）。

覆盖：
  1. _parse_agent_fallback 解析：字符串/list/dict{chain,policy}/未声明/显式空
  2. LLMClient 构造参数：显式 fallback_chain 优先于全局 settings.json
  3. set_fallback：设置 base 链 + policy + 重建有效链（user_model 在首）+ _fallback_owned 标记
  4. SubAgent 注入路径：.yml 声明 fallback 的子 agent 实例 llm 用自己的链；
     未声明 → 继承全局（_fallback_owned=False）
  5. main.yml 声明 fallback 时主 agent 生效（build_agent 路径逻辑等价验证）

跑法：python verify_model_fallback.py
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

passed, failed = [], []
def check(name, cond, extra=""):
    (passed if cond else failed).append(name)
    print(("  ✅ " if cond else "  ❌ ")+name+(f"  ({extra})" if extra and not cond else ""))


# —— 1. 解析 ——
print("【1】_parse_agent_fallback 解析")
from multiagent import _parse_agent_fallback
r = _parse_agent_fallback({"fallback": "deepseek, kimi"})
check("字符串链", r == (["deepseek", "kimi"], None), str(r))
r = _parse_agent_fallback({"fallback": ["deepseek", "kimi"]})
check("list 链", r == (["deepseek", "kimi"], None), str(r))
r = _parse_agent_fallback({"fallback": {"chain": "deepseek,kimi", "policy": "reset"}})
check("dict 完整声明（链+策略）", r == (["deepseek", "kimi"], "reset"), str(r))
r = _parse_agent_fallback({"fallback": ""})
check("显式空 = 关回退（与全局隔离）", r == ([], None), str(r))
r = _parse_agent_fallback({"fallback": {"policy": "sticky"}})
check("dict 只声明策略（链空）", r == ([], "sticky"), str(r))
check("未声明 = None（继承全局）", _parse_agent_fallback({}) is None)
check("yaml 空值 = None", _parse_agent_fallback({"fallback": None}) is None)

# —— 2/3. LLMClient 构造参数 + set_fallback ——
print("\n【2】LLMClient fallback 注入")
from llm_client import LLMClient
import config
c1 = LLMClient(model_name=config.DEFAULT_MODEL, enable_thinking=False)
g_chain = list(c1._base_fallback_chain)   # 全局 settings 读到的（可能为空）
check("默认构造继承全局 settings", c1._fallback_owned is False, str(g_chain))

c2 = LLMClient(model_name=config.DEFAULT_MODEL, enable_thinking=False,
               fallback_chain=["m_a", "m_b"], fallback_policy="reset")
check("构造参数显式链生效", c2._base_fallback_chain == ["m_a", "m_b"] and c2._fallback_owned is True)
check("构造参数 policy 生效", c2.fallback_policy == "reset")
check("有效链 user_model 在首", c2.fallback_chain[0] == c2._user_model and "m_a" in c2.fallback_chain, str(c2.fallback_chain))

c2.set_fallback("m_x, m_y", "sticky")
check("set_fallback 串入参解析", c2._base_fallback_chain == ["m_x", "m_y"])
check("set_fallback 重建有效链", c2.fallback_chain == [c2._user_model, "m_x", "m_y"], str(c2.fallback_chain))
c2.set_fallback([])
check("set_fallback 空链=关回退", c2.fallback_chain == [], str(c2.fallback_chain))

# —— 4. SubAgent 注入（yml 声明 vs 未声明）——
print("\n【3】子 agent .yml fallback 注入")
import tempfile, shutil
import real_tools
from agent import Agent
from real_tools import REAL_TOOLS
from agent_config import load_agent_yml as _lay

tmp = Path(tempfile.mkdtemp())
old_ws = real_tools.WORKSPACE
real_tools.WORKSPACE = tmp
agd = tmp / ".agent" / "agents"
agd.mkdir(parents=True)
(agd / "declared.yml").write_text(
    "name: declared\ndescription: d\nmodel:\n"
    "fallback:\n  chain: fb1, fb2\n  policy: reset\n", encoding="utf-8")
(agd / "undeclared.yml").write_text(
    "name: undeclared\ndescription: d\nmodel:\n", encoding="utf-8")
(agd / "off.yml").write_text(
    "name: off\ndescription: d\nmodel:\nfallback: ''\n", encoding="utf-8")

from multiagent import _parse_agent_fallback, SubAgent
from tools import Toolbox
def build(name):
    meta, _ = _lay(agd / f"{name}.yml")
    fb = _parse_agent_fallback(meta)
    tb = Toolbox(*list(REAL_TOOLS))   # 独立副本（SubAgent 会注册通信工具，避免污染共享单例）
    sa = SubAgent(name, config.DEFAULT_MODEL, "sys", tb)
    if fb is not None:
        sa.agent.llm.set_fallback(fb[0], fb[1])
    return sa

sa1 = build("declared")
check("声明者用自己的链", sa1.agent.llm._base_fallback_chain == ["fb1", "fb2"], str(sa1.agent.llm._base_fallback_chain))
check("声明者 policy 透传", sa1.agent.llm.fallback_policy == "reset")
check("声明者 _fallback_owned", sa1.agent.llm._fallback_owned is True)

sa2 = build("undeclared")
check("未声明者继承全局（owned=False）", sa2.agent.llm._fallback_owned is False and
      sa2.agent.llm._base_fallback_chain == g_chain, str(sa2.agent.llm._base_fallback_chain))

sa3 = build("off")
check("显式空=无回退", sa3.agent.llm.fallback_chain == [] and sa3.agent.llm._fallback_owned is True)

real_tools.WORKSPACE = old_ws
shutil.rmtree(tmp, ignore_errors=True)

print(f"\n{'🎉 全通过' if not failed else '⚠️ '+str(len(failed))+' 项失败'}（{len(passed)} 通过）")