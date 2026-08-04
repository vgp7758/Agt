"""multiagent.py —— 多 Agent 协作（主 Agent 派任务给一次性子 Agent）。

子 Agent 是【声明式 + 按需实例化 + 一次性】：声明存 .agent/agents/<name>.md
（frontmatter: name/description/tools/model + body: systemPrompt）。harness 每轮把
可用子 agent 清单投影进主 Agent SYSTEM（见 agent_config.agents_summary）。agent_prompt
按需读 md 建临时实例跑完即弃——多次 agent_prompt 同名 = 多个独立实例（无共享状态）。

工具：
  create_agent(name, description, system, tools, model)  写 .agent/agents/<name>.md（不建实例）
  agent_prompt(name, prompt)               读 md 建临时实例，跑完返回报告后销毁
  kill_agent(name)                         删 .agent/agents/<name>.md
  list_agents()                            扫 .agent/agents/ 列出
"""
from __future__ import annotations

import yaml

import config
from agent import Agent
from agent_config import _AGENT_DIR, _NAME_RE, _split_frontmatter, load_agents_index
from real_tools import WORKSPACE
from tools import Tool, Toolbox

# 子 Agent 绝不能继承的工具（防止递归生子 Agent、互相操控；计划工具绑定主 Agent）
_AGENT_TOOL_NAMES = {"create_agent", "agent_prompt", "kill_agent", "list_agents",
                     "create_plan", "update_plan", "update_wiki"}


class SubAgent:
    """一次性子 Agent：建实例 → prompt 跑一次 → 丢弃（不存任何 dict）。
    on_event 接主 Agent 的事件流，输出经主线程 _render_loop 渲染（CLI）/ broadcast（Web）。"""

    def __init__(self, name: str, model_name: str, system: str, tools: Toolbox,
                 on_event=None, max_steps: int = 15, token_budget: int = 30000):
        self.name = name
        self.model_name = model_name
        self.agent = Agent(system, tools, model_name=model_name,
                           enable_thinking=True, max_steps=max_steps,
                           token_budget=token_budget, verbose=False, on_event=on_event)

    def prompt(self, text: str) -> str:
        """派一个任务，子 Agent 自主用工具完成，返回最终回复。过程事件经 on_event 回流。"""
        if self.agent.on_event:
            self.agent.on_event({"type": "system",
                                 "text": f"▸ [子 Agent '{self.name}' ({self.model_name}) 开始工作]"})
        self.agent.cumulative_tokens = 0   # 一次性实例，本就是 0；保留防御
        result = self.agent.run(text)
        if self.agent.on_event:
            self.agent.on_event({"type": "system", "text": f"▸ [子 Agent '{self.name}' 完成]"})
        return result or "(空回复)"


def _resolve_tools(agent, tools_str: str):
    """把 tools 字符串解析成 Toolbox：留空/all/* = 继承主 Agent 全部（排除子 Agent 管理工具）；
    否则逗号分隔只注册这些（仍排除管理工具）。返回 (toolbox, 说明)。"""
    all_main_tools = list(agent.tools)
    if not tools_str or tools_str.strip().lower() in ("all", "*", "default", "继承", "全部"):
        chosen = [t for t in all_main_tools if t.name not in _AGENT_TOOL_NAMES]
        return Toolbox(*chosen), f"继承全部({len(chosen)}个)"
    wanted = [w.strip() for w in tools_str.split(",") if w.strip()]
    chosen = [t for t in all_main_tools if t.name in wanted and t.name not in _AGENT_TOOL_NAMES]
    found = {t.name for t in chosen}
    missing = [w for w in wanted if w not in found and w not in _AGENT_TOOL_NAMES]
    note = f"仅{len(chosen)}个" + (f"，未找到:{missing}" if missing else "")
    return Toolbox(*chosen), note


def _agent_md_path(name: str):
    """.agent/agents/<name>.md 路径；名字非法返回 None。"""
    if not _NAME_RE.match(name or ""):
        return None
    return WORKSPACE / _AGENT_DIR / "agents" / f"{name}.md"


def make_subagent_tools(agent) -> list:
    """生成绑定到指定主 Agent 的子 Agent 管理工具（声明式 + 一次性）。"""

    def create_agent(name: str, description: str, system: str, tools: str = "", model: str = "") -> str:
        """声明一个子 Agent（写 .agent/agents/<name>.md，不建实例）。
        name: 唯一名；description: 一句话作用 + 何时调用（投影给主 Agent 决定何时派活）；
        system: 子 Agent 的角色/任务定义（systemPrompt）；tools: 留空/all=继承主 Agent 全部
               (除管理工具)，或逗号分隔工具名只注册这些；model: 指定模型，留空=主 Agent 当前模型。
        声明后下一轮主 Agent SYSTEM 就会列出它，可用 agent_prompt 派活。"""
        p = _agent_md_path(name)
        if p is None:
            return f"[非法名称] '{name}'，只能含字母数字、下划线、连字符"
        if model and model not in config.MODELS:
            return f"[未知模型] '{model}'，可用：{list(config.MODELS)}"
        p.parent.mkdir(parents=True, exist_ok=True)
        meta = yaml.safe_dump(
            {"name": name, "description": description, "tools": tools, "model": model},
            allow_unicode=True, sort_keys=False,
        ).strip()
        p.write_text(f"---\n{meta}\n---\n\n{system.strip()}\n", encoding="utf-8")
        return f"✅ 已声明子 Agent '{name}' -> {p.relative_to(WORKSPACE)}（下一轮 SYSTEM 可见）"

    def kill_agent(name: str) -> str:
        """删除子 Agent 声明（.agent/agents/<name>.md）。"""
        p = _agent_md_path(name)
        if p is None or not p.exists():
            return f"[不存在] 没有名为 '{name}' 的子 Agent"
        p.unlink()
        return f"✅ 已删除子 Agent '{name}'"

    def agent_prompt(name: str, prompt: str, tools: str = "") -> str:
        """向子 Agent <name> 派任务：读它的声明 md 建临时实例，自主用工具完成后回复，实例即弃。
        多次派同名 = 多个独立实例（无共享状态）。
        tools: 临时指定本次子 Agent 可用的工具（留空=用 .md 里配置的；all/*=继承主 Agent 全部除
               管理工具；逗号分隔=只注册这些，如 'read_file,edit,write_file'）——主 agent 可借此
               临时出借部分工具给子 agent 操作，覆盖其默认工具集。"""
        p = _agent_md_path(name)
        if p is None or not p.exists():
            return f"[不存在] 没有名为 '{name}' 的子 Agent，先 create_agent"
        try:
            meta, system = _split_frontmatter(p.read_text(encoding="utf-8"))
        except Exception as e:
            return f"[读取失败] {type(e).__name__}: {e}"
        system = (system or "").strip() or "你是一个自主子 Agent，用工具完成任务。"
        toolbox, _ = _resolve_tools(agent, tools or meta.get("tools", ""))
        model_name = meta.get("model") or agent.model_name
        if model_name not in config.MODELS:
            model_name = agent.model_name
        try:
            sub = SubAgent(name, model_name, system, toolbox, on_event=agent.on_event)
            return sub.prompt(prompt)
        except Exception as e:
            return f"[子 Agent 调用出错] {type(e).__name__}: {e}"

    def list_agents() -> str:
        """列出所有已声明的子 Agent（扫 .agent/agents/）。"""
        idx = load_agents_index(WORKSPACE)
        if not idx:
            return "(暂无子 Agent)"
        return "\n".join(
            f"- {a['name']} (模型={a['model'] or '默认'}, 工具={a['tools'] or '全部'}): {a['description']}"
            for a in idx
        )

    return [Tool(create_agent), Tool(kill_agent), Tool(agent_prompt), Tool(list_agents)]
