"""multiagent.py —— 多 Agent 协作（主 Agent 调度带工具的自主子 Agent）。

子 Agent 是【带工具的自主 worker】：默认继承主 Agent 的全部工具（自动排除子 Agent
管理工具以防递归），或由主 Agent 指定只注册哪些工具。agent_prompt 派任务后，子 Agent
自主跑 ReAct 循环（调用工具）完成，再回复。

工具：
  create_agent(name, model, system, tools)  建子 Agent；tools 留空=继承全部(除管理工具)，
                                            或逗号分隔工具名只注册这些
  agent_prompt(name, prompt)                派任务，子 Agent 自主用工具完成后回复
  kill_agent(name)                          销毁
  list_agents()                             列出
"""
from __future__ import annotations

import config
from agent import Agent
from tools import Tool, Toolbox

# 子 Agent 绝不能继承的工具（防止递归生子 Agent、互相操控；计划工具绑定主 Agent）
_AGENT_TOOL_NAMES = {"create_agent", "agent_prompt", "kill_agent", "list_agents", "create_plan", "update_plan"}


class SubAgent:
    """带工具的自主子 Agent：内含一个 Agent，prompt() 跑自主 ReAct 循环。"""

    def __init__(self, name: str, model_name: str, system: str, tools: Toolbox,
                 max_steps: int = 15, token_budget: int = 30000):
        self.name = name
        self.model_name = model_name
        self.agent = Agent(system, tools, model_name=model_name,
                           enable_thinking=True, max_steps=max_steps,
                           token_budget=token_budget, verbose=True)

    def prompt(self, text: str) -> str:
        """派一个任务，子 Agent 自主用工具完成，返回最终回复。"""
        print(f"\n   ▸ [子 Agent '{self.name}' ({self.model_name}) 开始工作]")
        self.agent.cumulative_tokens = 0  # 每次派任务重置预算
        result = self.agent.run(text)
        print(f"   ▸ [子 Agent '{self.name}' 完成]")
        return result or "(空回复)"

    def info(self) -> str:
        return (f"- {self.name} (模型={self.model_name}, 工具={len(list(self.agent.tools))}个, "
                f"轮数={len(self.agent.session.turns)}): {self.agent.base_system[:40]}")


def make_subagent_tools(agent) -> list:
    """生成绑定到指定主 Agent 的子 Agent 管理工具。"""

    def create_agent(name: str, model: str, system: str, tools: str = None) -> str:
        """创建一个带工具的自主子 Agent。
        name: 唯一名字；model: 从可用模型里选；system: 子 Agent 的角色/任务定义。
        tools: 留空(或 all/*)= 继承主 Agent 全部工具(自动排除子 Agent 管理工具，防递归)；
               或传逗号分隔的工具名(如 'run_python,write_file')只注册这些。"""
        if name in agent.sub_agents:
            return f"[已存在] 名为 '{name}' 的子 Agent 已存在，换名或先 kill_agent"
        if model not in config.MODELS:
            return f"[未知模型] '{model}'，可用：{list(config.MODELS)}"

        all_main_tools = list(agent.tools)
        if not tools or tools.strip().lower() in ("all", "*", "default", "继承", "全部"):
            chosen = [t for t in all_main_tools if t.name not in _AGENT_TOOL_NAMES]
            note = f"继承全部({len(chosen)}个)"
        else:
            wanted = [w.strip() for w in tools.split(",") if w.strip()]
            chosen = [t for t in all_main_tools
                      if t.name in wanted and t.name not in _AGENT_TOOL_NAMES]
            found = {t.name for t in chosen}
            missing = [w for w in wanted if w not in found and w not in _AGENT_TOOL_NAMES]
            note = f"仅{len(chosen)}个" + (f"，未找到:{missing}" if missing else "")
        toolbox = Toolbox(*chosen)

        agent.sub_agents[name] = SubAgent(name, model, system, toolbox)
        return f"✅ 已创建子 Agent '{name}' (模型={model}, 工具{note})：{system[:50]}"

    def kill_agent(name: str) -> str:
        """销毁指定子 Agent。"""
        if name not in agent.sub_agents:
            return f"[不存在] 没有名为 '{name}' 的子 Agent"
        del agent.sub_agents[name]
        return f"✅ 已销毁子 Agent '{name}'"

    def agent_prompt(name: str, prompt: str) -> str:
        """向指定子 Agent 派任务，它自主用工具完成后回复。"""
        sub = agent.sub_agents.get(name)
        if sub is None:
            return f"[不存在] 没有名为 '{name}' 的子 Agent，先 create_agent"
        try:
            return sub.prompt(prompt)
        except Exception as e:
            return f"[子 Agent 调用出错] {type(e).__name__}: {e}"

    def list_agents() -> str:
        """列出当前所有子 Agent（名字/模型/工具数/轮数/角色）。"""
        if not agent.sub_agents:
            return "(暂无子 Agent)"
        return "\n".join(s.info() for s in agent.sub_agents.values())

    return [Tool(create_agent), Tool(kill_agent), Tool(agent_prompt), Tool(list_agents)]
