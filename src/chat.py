"""chat.py —— 交互式 Agent（Step 8 强化版，AgenTank 比赛）。

在上一版基础上叠加：
  - 斜杠命令（/save /resume /list /config /tank …）
  - 分层上下文引擎（摘要 + 窗口融合，长会话不丢关键决策）
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
from mcp_client import MCPManager, make_mcp_tools
from multiagent import make_subagent_tools
from prompts import build_system
from real_tools import REAL_TOOLS, WORKSPACE, make_autonomous_tools
from workflow import refresh_workflow_tools, make_workflow_mgmt_tools

_MODELS_DESC = "；".join(f"{n}（{m.get('desc', '').strip()}）" for n, m in config.MODELS.items())


def _load_agent_md() -> str:
    """读取启动目录 (cwd) 中用户自编辑的 AGENT.md，作为领域任务指引拼进 SYSTEM。"""
    p = WORKSPACE / "AGENT.md"
    if not p.exists():
        return "(未找到 AGENT.md，可在当前目录创建后重启生效)"
    return p.read_text(encoding="utf-8").strip()


def _rules_and_skills_section() -> str:
    """读取 .agent/ 下的 rules(始终生效) 和 skills 摘要 (渐进式披露)。"""
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
        "list_dir(workspace 内) / web_search / open_url(抓网页提取正文) / run_shell(慎用)。"
        "其它工具由 MCP server 动态提供，名字带 __mcp__ 前缀（按描述选用）。\n"
        "复杂任务建议先 create_plan(steps) 拆成步骤清单，每完成一步用 update_plan(step, status) 标记进度。\n"
        "接手不熟悉的任务前可用 wiki_search/wiki_read 查 .agent/wiki/ 里的仓库知识；"
        "完成重要功能或修改后调用 update_wiki(改动摘要)，由子 Agent 自动更新 repo-wiki。\n"
        "多 Agent 协作：create_agent(name, model, system, tools) 创建【带工具的自主子 Agent】——"
        "tools 留空=继承你的全部工具 (自动排除子 Agent 管理工具防递归)，或传逗号分隔工具名只注册这些；"
        "agent_prompt(name, prompt) 派任务，子 Agent 自主用工具完成再回复；kill_agent(name) 销毁；list_agents() 查看。"
        "复杂任务可拆分派给不同角色/模型的子 Agent 再综合。"
        "【并行】可同时进行的子任务，请在【同一步】里发起多个 agent_prompt 调用，并行更快；只有存在依赖关系时才分步。"
        "创建子 Agent 时从下列可用模型里选合适的：" + _MODELS_DESC + "。"
        + "\n\n【工作流编排】【推荐用 XML 写工作流】在 .agent/workflows/ 创建 .xml 文件（系统自动转 Coze JSON 执行）。"
        "XML 用标签+CDATA 包裹代码/提示词，内部双引号/花括号/换行/JSON 块都【无需转义】，远比手写 JSON 不易出错：\n"
        "  <workflow name=\"xx\" description=\"xx\">\n"
        "    <node id=\"100001\" type=\"start\"><out name=\"x\" type=\"number\" required=\"true\"/></node>\n"
        "    <node id=\"500001\" type=\"code\">\n"
        "      <in name=\"x\" ref=\"100001.x\"/>\n"
        "      <code><![CDATA[ async def main(args): return {\"y\": args.params[\"x\"]*2} ]]></code>\n"
        "      <out name=\"y\" type=\"number\"/>\n"
        "    </node>\n"
        "    <node id=\"900001\" type=\"end\"><out name=\"result\" ref=\"500001.y\"/></node>\n"
        "    <edge from=\"100001\" to=\"500001\"/><edge from=\"500001\" to=\"900001\"/>\n"
        "  </workflow>\n"
        "  节点 type 用名字：start/end/llm(用<param name=\"prompt\">+CDATA)/code/plugin(toolName=)/"
        "selector(<branch><cond op=\"13\" left=\"NODE.field\" right=\"60\"/>)/text(<result>+CDATA)/"
        "intent/aggregator/http/subworkflow。引用上游用 ref=\"节点id.字段名\"。meta(name/description/coze_url/auto)放<workflow>根属性。\n"
        "也支持直接写 .json（Coze 原生画布）。【写前先 read_workflow_spec() 读规范】，完整规范见 "
        "https://github.com/vgp7758/Agt/blob/main/docs/workflow-spec.md 。\n"
        "节点 type 速查：1=开始(入参在其 data.outputs) / 2=结束(出参在 data.inputs.inputParameters) / "
        "3=LLM(prompt/systemPrompt 在 llmParam) / 5=代码(自包含 Python，写 `async def main(args)->Output`，args.params 取输入) / "
        "8=选择器(分支) / 15=文本 / 21=循环 / 28=批处理 / 22=意图 / 45=HTTP / 9=子工作流 / "
        "4=插件(调工具箱里的工具) / 58/59=JSON 序列化/解析 / 32=聚合 / 40=赋值。\n"
        "【关键坑】① 插件节点(type 4)调的是工具箱里【已注册的工具】(toolName=工具名)，"
        "不是外部 py 文件；② 代码节点(type 5)是自包含沙箱代码，不要 import workspace 里的文件；"
        "③ 变量引用用 ref：{type:ref, content:{source:'block-output', blockID:'节点id', name:'输出字段名'}}。\n"
        "【自定义工具】你可以在 .agent/workflows/tools/*.py 里写顶层函数（带中文 docstring），"
        "它会自动注册成工具，工作流插件节点 toolName=函数名 即可调用。这比在代码节点里重复实现更可复用。\n"
        "  工具参数类型两种方式：① 简单类型直接加注解（dict→object / list→array / int→integer）；\n"
        "    ② 参数是 object/array 等需明确结构、或 schema 较复杂时，在 py 顶部【显式声明】模块级变量更清晰：\n"
        "    INPUT_SCHEMA = {'参数名':'object|array|integer|number|boolean|string', ...}\n"
        "    OUTPUT_SCHEMA = [{'name':'字段','type':'object','description':'...'}, ...]\n"
        "    （INPUT_SCHEMA 优先于注解；两者都无的参数回退 string）。main 函数会被跳过不注册。\n"
        "每轮对话结束时 .agent/workflows/ 下的工作流会被自动扫描注册为 wf_* 工具，tools/*.py 的函数也会自动注册。\n"
        + "\n\n=== 任务指引（当前目录 AGENT.md，用户可自行编辑）===\n"
        + _load_agent_md()
        + _rules_and_skills_section()
    ),
)

def main():
    print("=" * 64)
    print("🤖 交互式 Agent · AgenTank 比赛版")
    print("=" * 64)
    print("工具：get_tank/simulate/publish_code/challenge/... + run_python/文件/搜索/shell")
    print("命令：/save /resume /list /show /reset /config /budget /tank /model /autonomous /help")
    print("退出：quit / Ctrl+C / Ctrl+D  (运行中 Ctrl+C 打断但保留会话)")
    print("=" * 64)

    agent = Agent(system=SYSTEM, tools=REAL_TOOLS,
                  enable_thinking=True, max_steps=50, token_budget=80000)
    print(f"当前模型：{agent.model_name}  (输入 /model 切换)")

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
    # 注册纯自主模式工具
    for t in make_autonomous_tools(agent):
        agent.tools.register(t)
    # 注册工作流管理工具 + 首次扫描 .agent/workflows/ 注册工作流（之后每轮自动刷新）
    for t in make_workflow_mgmt_tools(WORKSPACE):
        agent.tools.register(t)
    ok, broken = refresh_workflow_tools(agent.tools, WORKSPACE, agent)
    for t in make_mcp_tools(mcp_mgr, str(WORKSPACE / ".mcp.json")):
        agent.tools.register(t)
    if ok:
        print(f"已加载工作流 {len(ok)} 个：{', '.join(ok)}")
    if broken:
        print(f"⚠️ {len(broken)} 个工作流加载失败：{broken}")
    registry = build_default_registry()

    try:
        while True:
            try:
                user = input("\n🧑 你：").strip()
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
                print(f"\n⚠️ 执行出错：{e}")
    finally:
        mcp_mgr.shutdown()


if __name__ == "__main__":
    main()
