"""chat.py —— 交互式 Agent（Step 8 强化版，AgenTank 比赛）。

在上一版基础上叠加：
  - 斜杠命令（/save /resume /list /config /tank …）
  - 分层上下文引擎（摘要+窗口融合，长会话不丢关键决策）
  - 长程自主（默认 50 步 + 软 token 预算，撑住"改→测→观→改"闭环）
  - AgenTank 原生工具（替代 curl 现写）

跑法：python chat.py
退出：quit / Ctrl+C / Ctrl+D ；运行中 Ctrl+C 可打断当前任务但保留会话。
"""
from pathlib import Path

import config
from agent import Agent
from agent_config import SKILL_TOOLS, load_rules, skills_summary
from plan_tools import make_plan_tools
from wiki import make_wiki_tools
from commands import CommandContext, build_default_registry
from mcp_client import MCPManager
from multiagent import make_subagent_tools
from prompts import build_system
from real_tools import REAL_TOOLS, WORKSPACE

_MODELS_DESC = "；".join(f"{n}（{m.get('desc', '').strip()}）" for n, m in config.MODELS.items())


def _load_agent_md() -> str:
    """读取启动目录(cwd)中用户自编辑的 AGENT.md，作为领域任务指引拼进 SYSTEM。"""
    p = WORKSPACE / "AGENT.md"
    if not p.exists():
        return "(未找到 AGENT.md，可在当前目录创建后重启生效)"
    return p.read_text(encoding="utf-8").strip()


def _rules_and_skills_section() -> str:
    """读取 .agent/ 下的 rules(始终生效) 和 skills 摘要(渐进式披露)。"""
    parts = []
    rules = load_rules(WORKSPACE)
    if rules:
        parts.append("=== 规则（.agent/rules/，始终生效）===\n" + rules)
    skills = skills_summary(WORKSPACE)
    if skills:
        parts.append("=== 可用技能（.agent/skills/）===\n"
                     "任务匹配某技能时，先 read_skill(name) 取详细 SOP 再按它执行：\n"
                     + skills + "\n（完成可复用任务后可用 save_skill 沉淀新技能）")
    return ("\n\n" + "\n\n".join(parts)) if parts else ""


# SYSTEM = 默认角色 + 内置工具 + 框架能力（代码拥有）+ 工作区 AGENT.md（用户自编辑）
SYSTEM = build_system(
    persona="默认助手",
    extra=(
        "你是一个强大的自主 Agent。用户用自然语言布置任务，你自主决定用哪些工具、分几步完成。\n"
        "内置工具：run_python(写并运行 Python) / read_file / write_file / edit(精确替换) / grep(内容搜索) / "
        "list_dir(workspace 内) / web_search / run_shell(慎用)。其它工具由 MCP server 动态提供，名字带 __mcp__ 前缀（按描述选用）。\n"
        "复杂任务建议先 create_plan(steps) 拆成步骤清单，每完成一步用 update_plan(step, status) 标记进度。\n"
        "接手不熟悉的任务前可用 wiki_search/wiki_read 查 .agent/wiki/ 里的仓库知识；"
        "完成重要功能或修改后调用 update_wiki(改动摘要)，由子 Agent 自动更新 repo-wiki。\n"
        "多 Agent 协作：create_agent(name, model, system, tools) 创建【带工具的自主子 Agent】——"
        "tools 留空=继承你的全部工具(自动排除子 Agent 管理工具防递归)，或传逗号分隔工具名只注册这些；"
        "agent_prompt(name, prompt) 派任务，子 Agent 自主用工具完成再回复；kill_agent(name) 销毁；list_agents() 查看。"
        "复杂任务可拆分派给不同角色/模型的子 Agent 再综合。"
        "【并行】可同时进行的子任务，请在【同一步】里发起多个 agent_prompt 调用，并行更快；只有存在依赖关系时才分步。"
        "创建子 Agent 时从下列可用模型里选合适的：" + _MODELS_DESC + "。"
        + "\n\n=== 任务指引（当前目录 AGENT.md，用户可自行编辑）===\n"
        + _load_agent_md()
        + _rules_and_skills_section()
    ),
)

def main():
    print("=" * 64)
    print("🤖 交互式 Agent · AgenTank 比赛版")
    print("=" * 64)
    print("工具: get_tank/simulate/publish_code/challenge/... + run_python/文件/搜索/shell")
    print("命令: /save /resume /list /show /reset /config /budget /tank /model /help")
    print("退出: quit / Ctrl+C / Ctrl+D  (运行中 Ctrl+C 打断但保留会话)")
    print("=" * 64)

    agent = Agent(system=SYSTEM, tools=REAL_TOOLS,
                  enable_thinking=True, max_steps=50, token_budget=80000)
    print(f"当前模型: {agent.model_name}  (输入 /model 切换)")

    # 连接 MCP server（读 workspace/.mcp.json），发现并注册 AgenTank 等工具
    mcp_mgr = MCPManager()
    mcp_mgr.connect_from_config(str(WORKSPACE / ".mcp.json"))
    for t in mcp_mgr.get_tools():
        agent.tools.register(t)
    print(f"已注册工具 {len(list(agent.tools))} 个（含 MCP 发现的）")

    # 注册绑定到本 Agent 的子 Agent 管理工具（多 Agent 协作）
    for t in make_subagent_tools(agent):
        agent.tools.register(t)
    # 注册技能工具（.agent/skills 渐进式披露 + 自动沉淀）
    for t in SKILL_TOOLS:
        agent.tools.register(t)
    # 注册计划工具（create_plan / update_plan）
    for t in make_plan_tools(agent):
        agent.tools.register(t)
    # 注册 wiki 工具（.agent/wiki 知识库 CRUD + update_wiki 子 Agent 维护）
    for t in make_wiki_tools(agent):
        agent.tools.register(t)
    registry = build_default_registry()

    try:
        while True:
            try:
                user = input("\n🧑 你: ").strip()
            except (EOFError, KeyboardInterrupt):
                print("\n再见！")
                break
            if not user:
                continue
            if user.lower() in {"quit", "exit", "q", "退出"}:
                print("再见！")
                break

            # 斜杠命令优先分发
            if registry.dispatch(user, CommandContext(agent=agent)):
                continue

            # 普通对话交给 Agent
            try:
                agent.run(user)
            except Exception as e:
                print(f"\n⚠️ 执行出错: {e}")
    finally:
        mcp_mgr.shutdown()


if __name__ == "__main__":
    main()
