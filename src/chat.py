"""chat.py —— 交互式 Agent（Step 8 强化版，AgenTank 比赛）。

在上一版基础上叠加：
  - 斜杠命令（/save /resume /list /config /tank …）
  - 分层上下文引擎（摘要 + 窗口融合，长会话不丢关键决策）
  - 长程自主（默认 50 步 + 软 token 预算，撑住"改→测→观→改"闭环）
  - AgenTank 原生工具（替代 curl 现写）

跑法：python chat.py
退出：quit / Ctrl+D ；运行中 Ctrl+C 第一次停当前任务（保留会话回到输入），第二次才退出。
"""
import queue
import threading
import time
from pathlib import Path

import config
from agent import Agent
from agent_config import SKILL_TOOLS, load_rules, skills_summary, agents_summary, seed_default_agents
from background_tools import make_background_tools
from plan_tools import make_plan_tools
from spec_tools import make_spec_tools
from survey_tools import make_survey_tools
from memory_tools import make_recall_tools
from session_tools import make_session_tools
from toollog import make_tool_log_tools
# wiki CRUD 十件套 + update_wiki 均不再内置：
#   - CRUD/章节四件套 → tools/builtin/wiki_tools.py（外置，真限界上下文）
#   - update_wiki 已删 → wiki_auto_maintenance 工作流直接 agent_prompt 派 wiki-updater
#     （reuse 复用 + caller=user 不唤醒主 Agent + wait_subagents 取报告）
from rag import LocalRAG, set_rag, ensure_rag, preload_async
from commands import CommandContext, build_default_registry, apply_config
from mcp_client import MCPManager, make_mcp_tools
from lsp_manager import make_lsp_tools
from multiagent import make_subagent_tools
from registry import AgentRegistry
from prompts import build_system
from real_tools import REAL_TOOLS, LIGHT_TOOLS, WORKSPACE, make_autonomous_tools
from updater import start_background_check
from workflow import refresh_workflow_tools, make_workflow_mgmt_tools
from snapshots import SnapshotManager

_MODELS_DESC = "；".join(f"{n}（{m.get('desc', '').strip()}）" for n, m in config.MODELS.items())


def _load_agent_md() -> str:
    """读取启动目录 (cwd) 中用户自编辑的 AGENTS.md（向后兼容 AGENT.md），作为领域任务指引拼进 SYSTEM。"""
    for name in ("AGENTS.md", "AGENT.md"):   # 优先 AGENTS.md（OpenAI 跨工具标准），兼容旧 AGENT.md
        p = WORKSPACE / name
        if p.exists():
            return p.read_text(encoding="utf-8").strip()
    return "(未找到 AGENTS.md，可在当前目录创建后重启生效)"


def _rules_and_skills_section(workspace=WORKSPACE) -> str:
    """读取 .agent/ 下的 rules(始终生效) 和 skills 摘要 (渐进式披露)。"""
    parts = []
    rules = load_rules(workspace)
    if rules:
        parts.append("=== 规则（.agent/rules/，始终生效）===\n" + rules)
    skills = skills_summary(workspace)
    if skills:
        parts.append("=== 可用技能（.agent/skills/）===\n"
                     "任务匹配某技能时，先 read_skill(name) 取详细 SOP 再按它执行：\n"
                     + skills + "\n（完成可复用任务后可用 save_skill 沉淀新技能）")
    agents = agents_summary(workspace)
    if agents:
        parts.append("=== 可用子 Agent（.agent/agents/，一次性）===\n"
                     "用 agent_prompt(name, 任务) 派给独立子 Agent 执行（实例即弃；过程回流本对话）：\n"
                     + agents)
    return ("\n\n" + "\n\n".join(parts)) if parts else ""


# SYSTEM = 默认角色 + 内置工具 + 框架能力（代码拥有）+ 工作区 AGENTS.md（用户自编辑）
SYSTEM = build_system(
    persona="默认助手",
    with_date=False,   # 日期/时间改由 tail 每步注入（实时、利于 Agent 感知时段）；persona 保持纯净稳定
    extra=(
        "你是一个强大的自主 Agent。用户用自然语言布置任务，你自主决定用哪些工具、分几步完成。\n"
        "内置工具：run_python(运行 Python：内联 code 或 .py 文件；跑已保存的脚本传 file=) / read_file / write_file / "
        "edit(精确替换 old_string→new_string，外科手术式小改、自带位置校验) / replace_lines(按行号整段替换，重写整个函数/大段代码用它，比 edit 省 token) / "
        "insert(按行号插入) / delete(按行号删行) / move(搬代码块) / grep(内容搜索) / "
        "list_dir(workspace 内) / web_search / open_url(抓网页提取正文) / run_shell(慎用)。"
        "其它工具由 MCP server 动态提供，名字带 __mcp__ 前缀（按描述选用）。\n"
        "复杂任务（涉及多处修改/跨文件/需要先探索）建议先用 explore_subagent 派探索子 Agent 摸清相关模块，"
        "再用 create_spec(title, steps, design) 制定施工方案（每步含 file/action/anchor/content/rationale），"
        "然后用 commit_spec 提交供用户批阅；用户「通过」则自动建 plan 开始施工，「返工」则据反馈 regenerate_spec 重新生成。\n"
        "简单任务直接用 create_plan(steps) 拆成步骤清单，每完成一步用 update_plan(step, status) 标记进度。\n"
         "接手不熟悉的任务前可用 wiki_search/wiki_read 查 .agent/wiki/ 里的仓库知识；"
         "wiki 由 before_answer 钩子（wiki_auto_maintenance）在你回答后自动维护——值得记录时自动派 wiki-updater 子 Agent 更新，无需手动调用。\n"
        + "\n\n【长期记忆·跨 session】你有一个 per-repo 长期记忆库（~/.agt/repos/<hash>/memories/，semantic/episodic/procedural 三类）：\n"
        "- semantic（事实/偏好，如用户背景、项目约定）每轮【始终注入】——少而稳定，像背景知识。\n"
        "- procedural（流程经验/how-to）system 里只列标题，需要时调 read_procedure(id) 取详情。\n"
        "- episodic（过往情境经历）按本轮问题【自动召回】注入，不相关则自动忽略。\n"
        "当你判断本轮出现【值得跨 session 记住】的经验，主动调 add_memory(type,title,content,tags) 记一笔——"
        "典型场景：踩坑及解法、用户偏好/背景、重要决策及原因、可复用流程。"
        "需要时用 search_memory 检索；同 type+title 会自动更新而非重复。用户可用 /memory 命令查看管理。\n"
        + "\n\n【随包资产】随包附带的工作流等资产可用 list_downloadable 查看（名称/类型/描述/是否已在本地），"
        "需要时 download_asset(name) 下载（用户也可用 /download 命令）。默认工作流已自动播种，这里用于显式取用或下载到指定目录。\n"
        + "\n\n【工具结果·按步距衰减】当前步的工具调用(入参+结果)【始终完整披露、不摘要】（你刚调用、需完整反馈）；"
        "历史工具调用按【距当前步的距离】差异化摘要(每远一步 −15 字、下限 20 字)，被截断的结果末尾标注 id(如 c7)。"
        "需要历史步骤的完整内容(完整 traceback、run_python 全部输出、edit 的完整 old/new)时调 get_tool_detail 拉取——"
        "可传单个 id 或多个(逗号分隔，如 get_tool_detail(\"c7,c8\"))一次取回多条；不确定有哪些 id 时先 list_tool_logs。\n"
        + "多 Agent 协作（全异步 + 自动 caller 绑定）：子 Agent 声明在 .agent/agents/<name>.md，下方【可用子 Agent】清单已为你投影——"
        "匹配某子 Agent 时直接 agent_prompt(name, 任务) 派活：它读声明建临时实例在后台自主跑，立即返回（不阻塞你）。"
        "完成后结果自动入队到你的 inbox——你下一步边界就能看到（跟用户插话效果一样），你也可以结束本轮等它汇报回来时自动激活下一轮。"
        "需要立即要结果才能继续时，调 wait_subagents(agent_ids) 显式阻塞等待。"
        "（多次 prompt 同名 = 独立实例，不共享状态；过程输出会回流到本对话）。"
        "需要新角色时 create_agent(name, description, system, tools, model) 写一条声明"
        "（description=一句话作用+何时调用，会投影进你的 SYSTEM；system=角色定义；tools 留空=继承全部(除管理工具)，或逗号分隔工具名；model 留空=用你当前模型）；"
        "不再需要时 kill_agent(name) 删声明；list_agents() 查看全部。"
        "复杂任务可拆分派给不同角色/模型的子 Agent 再综合——全异步并行，互不阻塞。"
        "高频反复派活（如反复看图/检查）时用 agent_prompt(name, 任务, reuse=true)：复用同名活实例，"
        "其上下文投影只含当前轮（token 不随复用次数增长），避免实例越建越多。"
        "可用模型：" + _MODELS_DESC + "。"
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
        "【本地脚本】写完的 Python 处理脚本（放 tools/ 或 .agent/workflows/tools/ 均可）用内置 run_script 工具执行："
        "run_script(script, payload) —— script 是脚本路径，payload 是 JSON 负载（脚本从环境变量 PAYLOAD 读取）。\n"
        "  工作流里：前置 ToJSON 节点把若干输入字段组装成 JSON → output 接 run_script 节点的 payload；"
        "脚本约定 `import os,json; data=json.loads(os.environ['PAYLOAD'])` 取参、print 输出（可再接 FromJSON 解析）。\n"
        "  脚本不必注册成工具——run_script 节点直接按文件名执行，适合复用较重的处理逻辑（比代码节点内联更清晰）。\n"
        "每轮对话结束时 .agent/workflows/ 下的工作流会被自动扫描注册为 wf_* 工具。\n"
        + "\n\n【语义代码导航 / LSP】处理 Python(.py)、C#(.cs) 等代码工程时，grep 找引用/定义在重载/泛型/分部类/扩展方法前会失效。"
          "处理某语言代码前先调 ensure_lsp('python') 或 ensure_lsp('csharp') 装上对应语义工具"
          "（首次自动 copy 脚本+装依赖到 ~/.agt/lsp/，当轮即可用；装一次后重启也会自动连），"
          "再用 py_def/py_ref/py_syms（Python）或 cs_def/cs_ref/cs_wsym/cs_hover/cs_diag（C#）替代 grep 做定义跳转/引用查找/符号搜索。改完 .cs 用 cs_diag 看 OmniSharp 的红线报错（改→查→改闭环，比 dotnet build 快）。grep 只用于纯文本/字面量。\n"
        + "\n\n【后台服务 + 定时调度】start_service(name, command) 后台启动长服务（如你写的后端 `python app.py` 或 `python -m http.server 8000`）做前后端联调——其状态会自动显示在每轮系统提示里，无需自己查；service_logs 看输出、stop_service 停止、list_services 总览。"
          "add_schedule(name, every_seconds=N, message='...') 每 N 秒自动推送一条消息触发你跑一轮（repeat 控制循环）；add_schedule(name, at='2026-07-20T17:30:00', tool='web_search', tool_args={...}) 到点执行工具拿结果触发（动态消息）；cancel_schedule/list_schedules 管理。"
          "适合：长联调时定时自检、到点搜集信息并处理、周期性监控与续作。被后台触发的那一轮，你能从上下文里看到 [后台触发·任务名] 标记。"
    ),
)


# ===== 可复用装配层（web/CLI 共用，消除两边装配漂移） =====

def init_rag(workspace):
    """同步强制重建 RAG 实例（/rag 页面保存配置后的热重建路径——server.py 调用）。
    启动期装载已改为【后台预热】（build_agent → rag.preload_async，模型加载秒级~十秒级
    不再阻塞启动）；本函数仅配置变更时同步用（force=True 无视 attempted 标志）。"""
    return ensure_rag(workspace, force=True)


def build_agent(mcp_mgr, *, on_event=None, snapshot_manager=None, verbose=True, workspace=WORKSPACE):
    """装配一个完整 Agent（web 与 CLI 共用）。
    mcp_mgr 须已连接（web 传模块级单例 / chat main 自建并连接后传入）；
    on_event/snapshot_manager 可注入；verbose 控制装配期打印。返回装配好的 agent。
    主 agent 元信息来自 ~/.agt/main.yml（assembly 清单 + hooks 声明 + model 覆盖），
    读不到时回退内置 SYSTEM 字符串 + 默认装配清单（保底不炸）。"""
    # RAG 后台预热（替代原同步 init_rag：模型加载秒级~十秒级不再阻塞启动）。
    # 外置件 tools/builtin/rag_tools.py 的 agt_register 也会触发 preload（幂等——
    # ensure 内部锁 + attempted 标志，双触发不重复加载）；这里显式调一次兜底外置件缺失的场景。
    preload_async(workspace)
    # 旧 .md 声明先迁移到 .yml（幂等；用户改过的 .md 优先成 .yml，不被随包模板覆盖）
    from agent_config import migrate_agents_md_to_yml
    migrate_agents_md_to_yml(workspace)
    # 播种默认子 Agent 模板（.agent/agents/，目标存在则跳过——迁移产物即存在）
    seed_default_agents(workspace)
    # 主 agent 元信息：~/.agt/main.yml（首次播种随包 src/assets/main.yml；用户可编辑）
    from agent_config import seed_main_agent, load_agent_yml
    main_yml = seed_main_agent(workspace)
    asm_plan = None
    hook_specs = None
    model_name = None
    main_fb = None
    try:
        meta, _ = load_agent_yml(main_yml)
        if meta:
            from multiagent import _parse_assembly, _parse_hooks, _parse_agent_fallback
            asm_plan = _parse_assembly(meta)
            hook_specs = _parse_hooks(meta)
            model_name = (meta.get("model") or "").strip() or None
            main_fb = _parse_agent_fallback(meta)
    except Exception as e:
        if verbose:
            print(f"[main.yml] 读取失败（{e}），回退内置 SYSTEM + 默认装配")
    # 快照管理器（默认装；web 可传自己的）
    snap = snapshot_manager or SnapshotManager(workspace)
    # Agent 注册表（多 Agent 协作通信的寻址基础）
    agent_registry = AgentRegistry()

    if asm_plan is not None:
        # v2：persona 在 assembly text: 项里，system 参数传空（_seg_msgs_system 空则跳过）
        agent = Agent(system="", tools=REAL_TOOLS,
                      model_name=model_name if model_name in (config.MODELS or {}) else None,
                      enable_thinking=True, max_steps=50, token_budget=80000,
                      verbose=verbose, on_event=on_event, snapshot_manager=snap,
                      registry=agent_registry)
        agent.session.set_assembly_plan(asm_plan)
        agent.session.hook_specs = hook_specs if hook_specs is not None else {}
    else:
        agent = Agent(system=SYSTEM, tools=REAL_TOOLS,
                      enable_thinking=True, max_steps=50, token_budget=80000,
                      verbose=verbose, on_event=on_event, snapshot_manager=snap,
                      registry=agent_registry)
    # main.yml 声明了 fallback 时覆盖全局 settings（未声明=走 /model、WebUI 配的全局链）
    if main_fb is not None:
        agent.llm.set_fallback(main_fb[0], main_fb[1])
    # 绑 mcp_mgr / workspace 到 agent，供 /web 等命令复用
    agent.mcp_mgr = mcp_mgr
    agent.workspace = workspace

    # session 语义召回层：配了 embed 模型就建 SessionVectorStore 并注入 session；
    # 没配（from_config 返回 None）→ session.vec_store 保持 None，recall 自动退回子串。
    # 【后台线程装载】：先 ensure_rag 等 RAG 预热完成（_build_embedder 共享其 embedder，
    # 省一份模型内存——此前 LocalRAG 与 session_vec 各建一份 bge 双倍内存）；启动不阻塞。
    # 预热完成前的最初几轮 recall 走子串匹配（vec_store=None 的既有降级路径），随后自动就绪。
    def _init_session_vec():
        try:
            from config import load_rag_config
            cfg = load_rag_config(workspace)
            if not (cfg.get("session_index_enabled") or cfg.get("enabled")):
                return   # session_index_enabled 显式开，或文档 RAG 已 enabled（隐含配了 embed）才建
            ensure_rag(workspace)   # 等预热（幂等；RAG 未启用时快速返回 None → 下方自建）
            from session_vec import SessionVectorStore
            sv = SessionVectorStore.from_config(workspace, cfg)
            if sv is not None:
                agent.session.vec_store = sv
                st = sv.stats()
                print(f"[session_vec] 已启用语义召回：{st['turns']} 轮 / {st['sessions']} 会话已索引"
                      f"（embedder {'共享自 RAG' if getattr(sv.embedder, '__class__', None).__name__ == '_CachedEmbedder' else '独立'}）")
        except Exception as e:
            print(f"[session_vec] 未启用（{e}）— recall 将用子串匹配")
    threading.Thread(target=_init_session_vec, daemon=True, name="session-vec-init").start()

    # 任务指引 provider：每轮从磁盘重读 AGENTS.md/rules/skills/子Agent（不再创建时烤进 SYSTEM）。
    # 用户改这些文件 → 任意 session、当轮即生效；SYSTEM 只留稳定框架，task-guidance 紧随其后由 provider 注入。
    def _task_guidance():
        sections = []
        rs = _rules_and_skills_section(workspace)
        if rs:
            sections.append(rs)
        for _name in ("AGENTS.md", "AGENT.md"):
            p = workspace / _name
            if p.exists():
                sections.append("=== 任务指引（当前目录 AGENTS.md，用户可自行编辑）===\n"
                                + p.read_text(encoding="utf-8").strip())
                break
        return "\n\n".join(sections) if sections else None
    agent._task_guidance_provider_fn = _task_guidance
    agent.session._task_guidance_provider = _task_guidance

    agent.tool_groups = {t.name: "内置" for t in REAL_TOOLS}  # 内置工具(real+light)标注来源

    def _reg(tools, group):
        """注册一组工具并标注来源模块（已注册的同名只更新 group，不重复注册）。"""
        for t in tools:
            if t.name not in agent.tools:
                agent.tools.register(t)
            agent.tool_groups[t.name] = group

    _reg(LIGHT_TOOLS, "内置")                       # 轻量工具补注册（REAL_TOOLS 已在构造时注册）
    # 外置脚本工具（tools/ 与 .agent/tools/ 的 agt_register() 声明）：register_or_replace——
    # 同名覆盖内置（用户定制覆写机制）；改脚本用 /reload tools 热加载，不需要 /restart
    from script_tools import attach_script_tools, make_hot_reload_tools
    _stb = attach_script_tools(agent.tools)
    for t in _stb:
        agent.tool_groups[t.name] = getattr(t, "group", "") or "脚本"   # 描述符 group 优先（如 wiki）
    _reg(make_hot_reload_tools(agent), "脚本")   # reload_hot：钩子工作流/Agent 改插件后热生效
    _reg(mcp_mgr.get_tools(), "MCP")
    _reg(make_subagent_tools(agent), "子Agent")
    _reg(SKILL_TOOLS, "技能")
    _reg(make_plan_tools(agent), "计划")
    _reg(make_spec_tools(agent), "施工方案")
    _reg(make_survey_tools(agent), "用户交互")
    _reg(make_recall_tools(agent), "记忆召回")
    _reg(make_session_tools(agent), "会话")  # get_session_history: hidden，工作流节点用
    # ltm 五件套与 download 两件套由外置件提供（tools/builtin/ltm_tools.py / download_tools.py，
    # attach_script_tools 注册；ltm 工具经 ensure_ltm 单例与注入 provider 共享实例）
    _reg(make_tool_log_tools(agent), "工具日志")
    _reg(make_background_tools(agent), "后台/调度")
    # wiki CRUD/章节四件套由外置件提供（attach_script_tools 已注册，group=wiki）；
    # update_wiki 已删除——wiki_auto_maintenance 工作流用 agent_prompt 直接派 wiki-updater
    # rag_query 由外置件 tools/builtin/rag_tools.py 提供（attach_script_tools 已注册，
    # group=rag）；其 agt_register 触发模型后台预热
    _reg(make_autonomous_tools(agent), "自主模式")
    _reg(make_workflow_mgmt_tools(workspace), "工作流管理")
    ok, broken = refresh_workflow_tools(agent.tools, workspace, agent)
    for t in agent.tools:                         # refresh 注册的 wf_* 标"工作流"
        if t.name.startswith("wf_"):
            agent.tool_groups[t.name] = "工作流"
    _reg(make_mcp_tools(mcp_mgr, str(workspace / ".mcp.json")), "MCP管理")
    _reg(make_lsp_tools(agent, mcp_mgr), "LSP")
    from workflow_debug_tools import make_workflow_debug_tools
    _reg(make_workflow_debug_tools(agent), "工作流调试")
    from restart_tools import make_restart_tools
    _reg(make_restart_tools(agent), "生命周期")
    if verbose:
        if ok:
            print(f"已加载工作流 {len(ok)} 个：{', '.join(ok)}")
        if broken:
            print(f"⚠️ {len(broken)} 个工作流加载失败：{broken}")

    # 加载持久化运行时设置（回退链/策略/max_steps/temperature/enable_thinking 等；~/.agt/settings.json）
    saved = config.load_runtime_settings()
    if saved:
        for line in apply_config(agent, saved):
            if verbose:
                print(line)
    # 统一辅助模型（utility_model 未配=主模型）：retrieval_llm 指向 utility_client
    # （RAG 检索抽关键字/精排用；recap/工作流 LLM 默认也走同一个 utility_client）
    try:
        agent.retrieval_llm = agent.utility_client()
    except Exception:
        agent.retrieval_llm = agent.llm
    return agent


def get_snapshot_list(session):
    """收集 session 里所有快照点（供 /snapshot list 与未来 web UI）。
    同 sha 去重（留首次出现），返回 [{idx, sha, user_message}]。"""
    seen = set()
    items = []
    for i, t in enumerate(session.turns):
        sha = getattr(t, "snapshot_sha", "") or ""
        if not sha or sha in seen:
            continue
        seen.add(sha)
        items.append({"idx": i, "sha": sha, "user_message": getattr(t, "user_message", "")})
    return items


def restore_snapshot(agent, sha):
    """检查点回溯：还原工作区文件树 + 截断对话。
    返回被截那轮的 user_message；snapshot_manager 未装或 sha 不存在返回 None。"""
    if agent.snapshot_manager is None:
        return None
    agent.snapshot_manager.restore(sha)
    return agent.session.restore_to_snapshot(sha)


def _install_signal_handlers(agent, work_q):
    """注册信号兜底：收到 SIGTERM/SIGHUP/SIGBREAK（kill / 关终端）时，让正在跑的
    agent.run 中断 + work_q 喂 None，把异常退出也导流到 finally，从而清理 start_service
    启的后台子进程，防孤儿。SIGINT(Ctrl+C) 保持默认 KeyboardInterrupt（本身就走 finally）。"""
    import signal

    def _on_sig(signum, frame):
        try:
            agent._stop_flag = True    # 让正在跑的 agent.run 尽快中断
        except Exception:
            pass
        work_q.put(None)               # 让 _run_loop 退出 → finally 清理

    sigs = [signal.SIGTERM]
    for name in ("SIGHUP", "SIGBREAK"):
        s = getattr(signal, name, None)
        if s is not None:
            sigs.append(s)
    for s in sigs:
        try:
            signal.signal(s, _on_sig)
        except (ValueError, OSError):
            pass   # 必须主线程注册；非主线程/不支持则跳过


def _worker(agent, work_q, registry, state):
    """后台 worker：串行消费 work_q，跑 agent.run / task / background。
    维护 state(busy/started/desc) 供主线程心跳。agent verbose=False（事件经 on_event
    入 event_q，由主线程 _render_loop 渲染）；本线程只发命令/提示类 print——与主线程
    事件渲染基本不并发（跑命令时 agent 未在跑、无事件；agent.run 期间本线程不 print）。"""
    while True:
        item = work_q.get()
        if item is None:
            break
        kind, payload = item
        state["kind"] = kind
        state["started"] = time.time()
        state["busy"] = True
        try:
            if kind == "task":
                # task（工作流调试/RAG 建库）单独跑，不合并
                state["desc"] = "工作流调试/RAG 建库"
                payload()
            elif kind == "user" and payload.startswith("/"):
                # 斜杠命令：即时分发，不合并、不进 agent.run
                state["desc"] = payload[:40]
                registry.dispatch(payload, CommandContext(agent=agent, work_q=work_q, state=state))
            else:
                # user(普通文本) / background：drain 期间累积的同类项，合并成一批一次 agent.run
                batch = [(kind, payload)]
                while True:
                    try:
                        nxt = work_q.get_nowait()
                    except queue.Empty:
                        break
                    if nxt is None:
                        work_q.put(None)   # 退出哨兵放回，本轮处理后退出
                        break
                    nk, np_ = nxt
                    # task / 斜杠命令不能合并进 agent.run：放回队尾停止 drain
                    if nk == "task" or (nk == "user" and np_.startswith("/")):
                        work_q.put(nxt)
                        break
                    batch.append(nxt)
                user_msg, seeds = _merge_batch(batch)
                if not user_msg and not seeds:
                    continue
                state["desc"] = user_msg[:40] or "(后台事件)"
                agent.run(user_msg, _seeds=seeds or None)
        except Exception as e:
            print(f"\n⚠️ 执行出错：{e}")
        finally:
            state["busy"] = False
            try:
                agent.on_event({"type": "_done"})   # 通知前端 agent 空闲（解除 busy）；CLI _render_loop 收到打印输入提示
            except Exception:
                pass


def _merge_batch(batch):
    """把 drain 出的一批 (kind, payload) 合并成 (user_message, seeds)，一次 agent.run 处理。
    background 标注来源（[后台通知·<source>]），让 agent 识别是哪个调度任务/进程发的；
    user 原样。多条用 --- 分隔。background 携带的 seed（后台服务退出等合成工具记录）收集进
    seeds，由 agent.run 经 _seeds 预置成 Step。"""
    parts = []
    seeds = []
    for k, p in batch:
        if k == "background":
            src, msg, seed = p
            print(f"\n⏰ [后台触发·{src}] {msg[:120]}")
            parts.append(f"[后台通知·{src}] {msg}")
            if seed:
                seeds.append(seed)
        else:  # user
            parts.append(p)
    user_msg = parts[0] if len(parts) == 1 else "\n\n---\n".join(parts)
    return user_msg, seeds


def _render_loop(agent, event_q, worker, state, work_q, threshold=10.0, interval=3.0,
                 interactive=True, quit_check=None, echo_pending=None):
    """主线程：消费 event_q 调 agent._print_event 渲染 agent 事件 + 长任务心跳 + 检测 worker 退出。
    所有 print 都在本线程 → 与 worker 的命令/提示 print 基本不并发（worker 串行 + 心跳仅长任务）。
    长任务（agent.run 超过 threshold 秒）才开始报心跳——心跳用 ANSI escape 原地刷新（不刷屏），
    有实际事件输出时自动隐去、下个 tick 再现。quit_check：CLI 两段式 Ctrl+C 退出用。
    echo_pending：CLI 本地输入对账表（user 事件到达时逐条匹配移除——input() 已回显的不重复打印；
    web 模式传 None：stdin 无回显，全部 user 事件渲染为日志行）。"""
    import sys, shutil, itertools
    _status_active = False
    spinner = itertools.cycle("⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏")
    last_report = 0.0

    def _clear_status():
        nonlocal _status_active
        if _status_active:
            sys.stdout.write("\033[A\033[2K\r")   # 光标上移一行 + 清行 + 回行首
            sys.stdout.flush()
            _status_active = False

    def _show_status(text):
        nonlocal _status_active
        cols = shutil.get_terminal_size().columns or 80
        if len(text) >= cols:
            text = text[:cols - 4] + "…"          # 截断防换行（CJK 宽度近似，够用）
        if _status_active:
            sys.stdout.write("\033[A\033[2K\r")   # 已有状态行：上移覆盖
        sys.stdout.write(text + "\n")
        sys.stdout.flush()
        _status_active = True

    while True:
        try:
            e = event_q.get(timeout=min(interval, 5))
        except queue.Empty:
            # 无事件：检查 worker 是否退出 / 是否请求退出 / 是否该报心跳
            if quit_check and quit_check():
                _clear_status()
                break
            if not worker.is_alive() and event_q.empty():
                _clear_status()
                break   # worker 已退出且事件排空 → 主线程退出
            if state.get("busy") and state.get("started"):
                elapsed = time.time() - state["started"]
                if elapsed > threshold and time.time() - last_report > interval:
                    qsize = work_q.qsize()
                    spin = next(spinner)
                    _show_status(f"{spin} 处理中「{state.get('desc', '')}」· {int(elapsed)}s"
                                 f" · 队列 {qsize}（Ctrl+C 停止）")
                    last_report = time.time()
            continue
        try:
            etype = e.get("type")
            if etype == "_quit":
                _clear_status()
                break   # CLI 第二次 Ctrl+C：SIGINT 处理器塞入的退出事件 → 立即退出
            _clear_status()   # 有实际输出：先隐去状态行，避免穿插
            if etype == "user":
                # user 事件回显（多端同步）：与 CLI 交互的同一 Agent 收到来自其它客户端（WebUI/
                # 手机/stdin 驱动）的消息时打印出来——否则 CLI 只见回答不见问题，日志缺半边。
                _aid = str(e.get("agent_id") or "_main_")
                _cur = str(getattr(agent, "_active_target", "_main_") or "_main_")
                if _aid != _cur:
                    continue   # 其它 Agent 的 user 事件（同步子 Agent 的任务输入/wiki-updater 的批量任务）不渲染
                _txt = e.get("text") or ""
                if echo_pending is not None:
                    # 本 CLI 刚输入的（input() 已回显）——对账移除，防双打印。
                    # 批量合并形态（worker drain 合并 "a\n\n---\nb"）也逐段对账。
                    _parts = _txt.split("\n\n---\n")
                    if _txt in echo_pending:
                        echo_pending.remove(_txt)
                        continue
                    if len(_parts) > 1 and all(p in echo_pending for p in _parts):
                        for p in _parts:
                            echo_pending.remove(p)
                        continue
                if _txt.startswith("[后台通知·") or _txt.startswith("[后台触发·"):
                    continue   # 后台触发轮：_merge_batch 已打 ⏰ 行，不重复渲染为用户消息
                _ic = e.get("image_count") or 0
                print(f"\n🧑 你（来自其它客户端）：{_txt[:500]}"
                      + (f"（+{_ic}图）" if _ic else ""))
                continue
            if etype == "spec_pending":
                # commit_spec 阻塞等待用户裁定：CLI 打印 spec 详情 + 提示
                spec = e.get("spec", [])
                title = e.get("spec_title", "")
                design = e.get("spec_design", "")
                sid = e.get("spec_id", "")
                print(f"\n📐 施工方案待批阅：{title}（{sid}）")
                if design:
                    print(f"   设计：{design[:300]}")
                for i, s in enumerate(spec):
                    act = s.get("action", "review")
                    f = s.get("file", "") or ""
                    print(f"   {i+1}. [{act}] {f}")
                print(f"\n   输入 /approve 通过，或 /reject <反馈> 返工")
                continue
            if etype == "survey_pending":
                # ask_user 阻塞等待用户回答：CLI 打印题目 + 选项
                questions = e.get("questions", [])
                print(f"\n📋 用户问卷（共 {len(questions)} 题）：")
                for qi, q in enumerate(questions):
                    qid = q.get("id", f"q{qi+1}")
                    title = q.get("title", "")
                    opts = q.get("options", [])
                    multi = q.get("multi_select", False)
                    custom = q.get("allow_custom", False)
                    tag = "多选" if multi else "单选"
                    print(f"\n  {qi+1}. [{tag}] {title}")
                    for oi, opt in enumerate(opts):
                        print(f"     {oi+1}) {opt}")
                    if custom:
                        print(f"     自定义：直接输入文字")
                    print(f"     → 回答格式：{'逗号分隔序号' if multi else '序号'}"
                          f"{'+自定义文字' if custom else ''}，如：{('1,3') if multi else '2'}"
                          f"{(' 链路追踪') if custom else ''}")
                print(f"\n   输入 /survey <第1题答案> | <第2题答案> | ... 提交（| 分隔各题）")
                continue
            if etype == "system":
                print(e.get("text", ""))   # 子 Agent 边界 / agent system 提示（_print_event 不处理 system）
            else:
                agent._print_event(e)      # 复用 agent 的事件格式化渲染（主线程单线程 print）
            # _done = 一个 work_q 项处理完（agent 回答 / task / background）→ 打印输入提示（仅 CLI）
            if interactive and etype == "_done":
                print("\n🧑 你：", end="", flush=True)
        except Exception:
            pass
    _clear_status()
    print("\n再见！")


def _ensure_utf8_stdout():
    """把 stdout/stderr 切到 UTF-8 + errors=replace：防 LLM 回答含当前 codepage 编不出的
    码点时 print 抛 UnicodeEncodeError（会把一整次回答崩在最后一行）。box-drawing 在
    cp936 本就有映射不会炸，真正风险是 LLM 文本里任意 Unicode 码点。"""
    import sys
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def _recover_restart_env(agent, work_q, registry, state):
    """/restart 或 restart_agent 看门狗重启后的自动恢复：消费 AGT_RESTART_* 环境变量。
    AGT_RESTART_SESSION → /resume 恢复会话；AGT_RESTART_MESSAGE → 作为第一条消息进 work_q。
    pop 掉避免污染后续 spawn 的子进程环境。"""
    import os
    sess = os.environ.pop("AGT_RESTART_SESSION", "")
    msg = os.environ.pop("AGT_RESTART_MESSAGE", "")
    if not sess and not msg:
        return
    print("🔁 检测到重启恢复指令…")
    if sess:
        try:
            registry.dispatch(f"/resume {sess}", CommandContext(agent=agent, work_q=work_q, state=state))
        except Exception as e:
            print(f"⚠️ 恢复会话「{sess}」失败：{e}（可用 /resume 手动恢复）")
    if msg:
        print(f"📨 重启后首条消息：{msg[:80]}")
        work_q.put(("user", msg))


def web_main(port=None):
    """agt-web 入口：装配 Agent + 自动起 Web 服务 + 开浏览器，跑主循环服务 WS/后台（非交互 REPL）。
    端口可由命令行参数指定：`agt-web` → 8000，`agt-web 9000` → 9000。"""
    import sys
    _ensure_utf8_stdout()
    if port is None:
        for a in sys.argv[1:]:
            if a.isdigit():
                port = int(a)
                break
    port = port or 8000
    # 首次安装引导：无模型配置时，提示用户在 WebUI 设置页添加第一个模型
    if not config.MODELS:
        print("=" * 64)
        print("⚠️  尚未配置任何模型，Agent 可以启动但无法执行 LLM 任务。")
        print("    浏览器打开后 → 右上角「设置」→ 添加第一个模型（base_url / api_token / model）")
        print("    或编辑 ~/.agt/models.json 手动添加，然后刷新页面即可使用。")
        print("=" * 64)
    start_background_check()   # 后台查 PyPI 新版（24h 节流；editable 跳过；失败静默）
    from server import broadcast, start_server, open_browser, lan_urls, stop_server_if_running
    mcp_mgr = MCPManager()
    mcp_mgr.connect_from_config(str(WORKSPACE / ".mcp.json"))
    mcp_mgr.connect_from_config(str(Path.home() / ".agt" / "mcp.json"))
    event_q: "queue.Queue" = queue.Queue()
    def _on_event(e):
        broadcast(e); event_q.put(e)
    agent = build_agent(mcp_mgr, on_event=_on_event, verbose=False, workspace=WORKSPACE)
    print(f"当前模型：{agent.model_name}  (工具 {len(list(agent.tools))} 个)")
    work_q: "queue.Queue" = queue.Queue()
    registry = build_default_registry()
    state: dict = {"busy": False, "started": 0.0, "desc": "", "kind": None}
    _install_signal_handlers(agent, work_q)   # 关终端/kill 兜底清理，防后台服务孤儿

    ok, msg = start_server(agent=agent, work_q=work_q, mcp_mgr=mcp_mgr, workspace=WORKSPACE,
                           port=port, state=state)
    if not ok:
        print(f"❌ {msg}")
        return
    print(f"✅ {msg}")
    print(f"  本机:   http://127.0.0.1:{port}/")
    print(f"  局域网: {', '.join(lan_urls(port))}")
    print("  （局域网内任何设备可连并驱动 Agent，仅在可信网络使用；Ctrl+C 退出）")
    open_browser(port)

    def _inbox_thread():
        import logging as _lg
        _il = _lg.getLogger("agt.chat")
        while True:
            item = agent.pop_inbox()
            if item:
                _il.info("inbox_thread: (%s) %s → work_q", item[0], (item[1] or "")[:60])
                work_q.put(("background", item))
            else:
                time.sleep(0.2)
    threading.Thread(target=_inbox_thread, daemon=True).start()

    # worker 线程：串行消费 work_q 跑 agent.run（长任务后台化）
    worker = threading.Thread(target=_worker, args=(agent, work_q, registry, state), daemon=True)
    worker.start()
    agent._work_q = work_q   # restart_agent 工具需要（触发优雅退出）
    _recover_restart_env(agent, work_q, registry, state)   # 看门狗重启恢复：/resume + 首条消息

    def _stdin_thread():
        """stdin 消费线程：仅当 stdin 非 tty（被 start_service 以管道启动 / 输入重定向）时启动。
        每读一行作为 user 消息进 work_q（与 WS 文本同流串行）——外部 Agent 可用
        send_to_service 驱动本实例（发任务 / 发 /restart 等命令）。tty（用户手动跑）不启动，
        终端保持只读日志，原行为不变。"""
        import sys as _sys
        try:
            if _sys.stdin is None or _sys.stdin.isatty():
                return
        except Exception:
            return
        while True:
            try:
                line = _sys.stdin.readline()
            except Exception:
                return   # stdin 关闭（父进程退出）
            if not line:
                return   # EOF
            line = line.strip()
            if line:
                work_q.put(("user", line))
    threading.Thread(target=_stdin_thread, daemon=True).start()

    print("（Web 模式：浏览器交互；Ctrl+C 退出）")
    try:
        _render_loop(agent, event_q, worker, state, work_q, interactive=False)
    except KeyboardInterrupt:
        print("\n⏹ 已请求停止，等当前步完成…")
        agent._stop_flag = True
        work_q.put(None)
        worker.join(timeout=120)
    finally:
        work_q.put(None)
        if worker.is_alive():
            worker.join(timeout=5)
        stop_server_if_running()
        agent.shutdown()
        mcp_mgr.shutdown()


def main():
    _ensure_utf8_stdout()
    print("=" * 64)
    print("🤖 交互式 Agent")
    print("=" * 64)
    print("命令：/save /rename /resume /list /show /reset /config /budget /stats /status /model /autonomous /memory /logs /download /web /snapshot /rewind /rag /tools /update /reload /restart /debug /agent /help")
    print("退出：quit / Ctrl+D  (运行中 Ctrl+C 第一次=停当前任务回到输入，第二次=退出)")
    print("=" * 64)

    start_background_check()   # 后台查 PyPI 新版（24h 节流；editable 跳过；失败静默）

    # 连接 MCP server（workspace/.mcp.json + 全局 ~/.agt/mcp.json）
    mcp_mgr = MCPManager()
    mcp_mgr.connect_from_config(str(WORKSPACE / ".mcp.json"))
    mcp_mgr.connect_from_config(str(Path.home() / ".agt" / "mcp.json"))   # 全局已装 LSP（ensure_lsp 装配的）

    # 装配 Agent（web 与 CLI 共用 build_agent，消除装配漂移）。
    # on_event=broadcast：无 Web 客户端时 no-op，纯 CLI 零开销；/web 起服务、有客户端连上后才推送。
    from server import broadcast, stop_server_if_running
    event_q: "queue.Queue" = queue.Queue()
    def _on_event(e):
        broadcast(e)          # 推 WS（无客户端 no-op）
        event_q.put(e)        # 喂主线程渲染（CLI）
    agent = build_agent(mcp_mgr, on_event=_on_event, verbose=False, workspace=WORKSPACE)
    if agent.model_name:
        print(f"当前模型：{agent.model_name}  (输入 /model 切换)")
    else:
        print("⚠️  当前无模型配置。输入 /web 启动 WebUI，在设置页添加第一个模型后即可使用。")
    print(f"已注册工具 {len(list(agent.tools))} 个（含 MCP 发现的）")

    # REPL 单消费者队列驱动：input 线程 + inbox 轮询线程 都往 work_q 喂，
    # 主线程串行消费——保证任何时候只有一个 agent.run 在跑（run 非线程安全）。
    # Web 客户端（/web 起的服务）的文本消息也喂进这个 work_q，与 CLI 输入同流串行。
    work_q: "queue.Queue" = queue.Queue()
    registry = build_default_registry()   # work_q 通过 dispatch 时的 CommandContext 注入
    state: dict = {"busy": False, "started": 0.0, "desc": "", "kind": None}
    _install_signal_handlers(agent, work_q)   # 关终端/kill 兜底清理，防后台服务孤儿

    # CLI 两段式 Ctrl+C：第 1 次 = 停当前任务、保留 REPL；第 2 次 = 退出。
    # 默认 SIGINT→KeyboardInterrupt 会直接走 finally 退出整个程序，与"打断但保留会话"的承诺不符。
    import signal
    _quit_state = {"count": 0, "quit": False}
    def _on_sigint(signum, frame):
        _quit_state["count"] += 1
        if _quit_state["count"] == 1:
            agent._stop_flag = True
            print("\n⏹ 已请求停止当前任务（等当前步返回后回到输入）。再按一次 Ctrl+C 退出。", flush=True)
        else:
            _quit_state["quit"] = True
            event_q.put({"type": "_quit"})   # 立即唤醒 _render_loop 退出
    try:
        signal.signal(signal.SIGINT, _on_sigint)
    except (ValueError, OSError):
        pass   # 非主线程注册失败 → 退回默认 KeyboardInterrupt 行为（兜底退出）

    _cli_echo = []   # CLI 本地输入对账表（_input_thread 记录 → _render_loop 的 user 事件逐条匹配移除）

    def _input_thread():
        _echo = _cli_echo   # 本地输入对账表（user 事件到达时匹配移除——input() 已回显的不重复打印）

        def _record(t):
            _echo.append(t)
            if len(_echo) > 10:
                _echo.pop(0)

        while True:
            try:
                user = input("").strip()   # 提示由主线程 _render_loop 在 agent 完成后打印
            except (EOFError, KeyboardInterrupt):
                work_q.put(None)   # 哨兵：通知主线程退出
                return
            # quit 仅在 CLI 输入触发退出；Web 客户端发的 "quit" 只当普通消息（不会关掉服务）
            if user.lower() in {"quit", "exit", "q", "退出"}:
                work_q.put(None)
                return
            # spec 批阅：commit_spec 阻塞等待时，直接解除（不进 work_q，因为 worker 被阻塞）
            if getattr(agent, "_spec_decision_event", None):
                if user == "/approve" or user.startswith("/approve"):
                    from spec_tools import resolve_spec_decision
                    resolve_spec_decision(agent, "approve")
                    print("✅ 已批准 spec，继续...", flush=True)
                    continue
                if user == "/reject" or user.startswith("/reject"):
                    from spec_tools import resolve_spec_decision
                    fb = user[7:].strip()  # remove "/reject"
                    resolve_spec_decision(agent, "reject", fb)
                    print(f"❌ 已返工" + (f"：{fb}" if fb else ""), flush=True)
                    continue
            # survey 回答：ask_user 阻塞等待时，直接解除
            if getattr(agent, "_survey_decision_event", None):
                if user.startswith("/survey"):
                    from survey_tools import resolve_survey
                    raw_answers = user[7:].strip()  # remove "/survey"
                    # 解析答案：| 分隔各题，每题是序号或文字
                    questions = agent.session.extra_state.get("_pending_survey", [])
                    parts = [p.strip() for p in raw_answers.split("|")]
                    answers = {}
                    for qi, q in enumerate(questions):
                        qid = q.get("id", f"q{qi+1}")
                        ans_text = parts[qi] if qi < len(parts) else ""
                        opts = q.get("options", [])
                        multi = q.get("multi_select", False)
                        # 尝试解析序号
                        if "," in ans_text or ans_text.isdigit():
                            indices = [int(x.strip()) - 1 for x in ans_text.split(",") if x.strip().isdigit()]
                            selected = [opts[i] for i in indices if 0 <= i < len(opts)]
                            # 检查是否有附加文字（如 "1,3 链路追踪"）
                            text_parts = [x.strip() for x in ans_text.split() if not x.strip().isdigit() and x.strip() != ","]
                            if text_parts:
                                selected.append(" ".join(text_parts))
                            answers[qid] = selected if (multi or len(selected) > 1) else (selected[0] if selected else "")
                        elif ans_text:
                            answers[qid] = ans_text
                        else:
                            answers[qid] = "(未答)"
                    resolve_survey(agent, answers)
                    print("✅ 已提交问卷答案，继续...", flush=True)
                    continue
            # /agent 命令切换了交互目标 → 用户输入直接路由到目标 agent
            target = getattr(agent, "_active_target", "_main_")
            if target != "_main_":
                # 用户在与子 Agent 直接交互：直接调目标 agent.run，结果经 on_event 渲染
                entry = agent.registry.lookup(target) if agent.registry else None
                if entry and entry.agent:
                    try:
                        _record(user)   # CLI 已回显：user 事件回来时对账跳过（防双打印）
                        entry.agent.run(user)
                        print("\n🧑 你：", end="", flush=True)
                        continue
                    except Exception as e:
                        print(f"\n❌ 与 '{target}' 交互失败：{type(e).__name__}: {e}")
                        agent._active_target = "_main_"
                        print("（已切回主 Agent）")
                        print("\n🧑 你：", end="", flush=True)
                        continue
                else:
                    print(f"\n⚠️ 找不到目标 '{target}'，已切回主 Agent")
                    agent._active_target = "_main_"
            _record(user)   # CLI 已回显：user 事件回来时对账跳过（防双打印）
            work_q.put(("user", user))
            if state.get("busy"):
                print(f"\n📥 已排队（当前任务结束后处理）：{user[:40]!r}", flush=True)

    def _inbox_thread():
        while True:
            item = agent.pop_inbox()
            if item:
                work_q.put(("background", item))
            else:
                time.sleep(0.2)

    threading.Thread(target=_input_thread, daemon=True).start()
    threading.Thread(target=_inbox_thread, daemon=True).start()

    # worker 线程：串行消费 work_q 跑 agent.run（长任务后台化，不卡主线程）
    worker = threading.Thread(target=_worker, args=(agent, work_q, registry, state), daemon=True)
    worker.start()
    agent._work_q = work_q   # restart_agent 工具需要（触发优雅退出）
    _recover_restart_env(agent, work_q, registry, state)   # 看门狗重启恢复：/resume + 首条消息
    print("\n🧑 你：", end="", flush=True)   # 首次输入提示（之后 _render_loop 在 agent 完成后打印）

    try:
        _render_loop(agent, event_q, worker, state, work_q, interactive=True,
                     quit_check=lambda: _quit_state["quit"], echo_pending=_cli_echo)
    except KeyboardInterrupt:
        # 兜底：SIGINT 处理器注册失败时才走到这（默认 KeyboardInterrupt 行为）→ 退出
        print("\n⏹ 已请求停止，等当前步完成…")
        agent._stop_flag = True
        work_q.put(None)
        worker.join(timeout=120)
    finally:
        work_q.put(None)   # 确保 worker 退出
        if worker.is_alive():
            worker.join(timeout=5)
        stop_server_if_running()   # 若 /web 起过服务，退出时一并停掉释放端口
        agent.shutdown()    # 停后台服务（防孤儿进程）+ 停调度器
        mcp_mgr.shutdown()


if __name__ == "__main__":
    main()
