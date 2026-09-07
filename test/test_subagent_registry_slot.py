# 子 Agent 抢注 _main_ registry 槽 → 完成通知路由错乱（用户实锤 2026-09-07）的修复验证
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from registry import AgentRegistry
from tools import Toolbox
from multiagent import SubAgent
from agent import Agent

reg = AgentRegistry()
ok = True
def check(name, cond, detail=""):
    global ok
    print(("✅" if cond else "❌"), name, detail)
    ok = ok and cond

# —— 1. 主 Agent 构造：注册 _main_ ——
main = Agent(system="t", tools=Toolbox(), model_name=None, verbose=False, registry=reg, agent_id="_main_")
e_main = reg.lookup("_main_")
check("主 Agent 注册 _main_ 槽（agent=自身）", e_main is not None and e_main.agent is main)
check("主 Agent role=main", e_main.role == "main")

# —— 2. 子 Agent 构造：注册正确 key（不抢 _main_）——
sub = SubAgent("vision", "local-lfm-vl", "你是看图助手", Toolbox(),
               session_dir=None, registry=reg, agent_id="vision_x", caller_id="_main_")
e_main2 = reg.lookup("_main_")
e_sub = reg.lookup("vision_x")
check("子 Agent 注册 vision_x 槽", e_sub is not None and e_sub.agent is sub.agent)
check("子 Agent role=subagent", e_sub.role == "subagent")
check("【核心】_main_ 槽仍指向主 Agent（未被抢注）", e_main2 is not None and e_main2.agent is main)
check("子 Agent 的 agent_id 正确", sub.agent.agent_id == "vision_x")

# —— 3. 主 Agent 重复构造（restart 场景）：不覆盖已有 live 主 Agent ——
main2 = Agent(system="t", tools=Toolbox(), model_name=None, verbose=False, registry=reg, agent_id="_main_")
check("第二个 _main_ 构造不覆盖原主 Agent（防覆盖生效）", reg.lookup("_main_").agent is main)

# —— 4. 子 Agent 无 agent_id（兜底用 name）→ 不落到 _main_ ——
sub2 = SubAgent("ghost", "x", "s", Toolbox(), registry=reg)   # agent_id=None
check("无 agent_id 的子 Agent 用 name 兜底（非 _main_）",
      reg.lookup("ghost") is not None and reg.lookup("_main_").agent is main)

print("\n" + ("ALL PASS ✅" if ok else "SOME FAILED ❌"))
sys.exit(0 if ok else 1)