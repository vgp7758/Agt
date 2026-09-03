# -*- coding: utf-8 -*-
"""tool_briefs.py —— 全部内置工具的【用户视角一句话简介】。

与 description（给 LLM 的 docstring 首行，面向"模型判断该不该调"）不同：
brief 面向【在 WebUI 工具箱里挑工具的人类用户】——回答"我为什么要选这个工具"。

规则：
  - 一句话，≤30 字左右，动词开头，说清楚"用它干什么"而非"它是什么"
  - 只维护自有工具；MCP 工具（__mcp__ 前缀）由其自带 description 首句兜底
  - Tool(brief=...) 显式传参优先于本字典；都没有则取 description 首句截断兜底
  - 新增工具不写 brief 不会报错，只是工具箱里简介退化为 docstring 首句
"""

TOOL_BRIEFS = {
    # ===== 内置：代码执行 / 文件编辑 =====
    "run_python": "跑一段 Python 拿结果：算数据、批处理、验证逻辑一步到位",
    "run_shell": "执行系统命令（pip/git/进程），需要碰系统层面时用",
    "run_script": "运行已保存的脚本文件，适合反复跑的处理脚本",
    "read_file": "读文件内容的统一入口：Word/Excel/PDF/图片自动提取",
    "write_file": "新建或整体覆盖一个文件，放长内容的首选",
    "edit": "只替换文件里一小段文本，小改不必重写全文件",
    "insert": "按行号在指定位置插入一段或多段文本，一次原子写入",
    "delete": "按行号删掉一段连续的行，外科手术式删行",
    "move": "把一段代码整体搬到新位置，重构挪块专用",
    "replace_lines": "按行号整段替换，重写整个函数/大段代码比 edit 省 token",
    "diff_files": "两个文件逐行对比出差异，审计改动/版本对比用",
    "git_commit": "一键 add+commit+push，标准提交通道",
    "list_dir": "看目录下有哪些文件和子目录",
    "glob_files": "按通配符（** 递归）找文件名，找文件不搜内容",
    "grep": "全库搜内容返回带行号匹配，定位代码在哪最快",
    "find_function": "直接看某个函数/方法的完整定义，比 grep 更省事",

    # ===== 内置：网络 =====
    "web_search": "联网搜索：查资料、查文档、查报错",
    "open_url": "抓网页正文来读，不用自己开浏览器",

    # ===== 内置：工作流参考 =====
    "read_workflow_spec": "写工作流前先读规范，节点/字段/引用规则全在这",
    "read_workflow_demo": "拿官方工作流示例照着写，或看示例清单",
    "list_workflow_nodes": "所有可用节点类型的速查表",
    "query_workflow_node": "查某个节点的完整写法和字段说明",
    "list_workflows": "列出全部工作流及加载状态",
    "debug_workflow": "调试执行一个工作流，逐节点看输出",
    "list_workflow_outputs": "看上次调试中各节点的输出摘要",
    "eval_node_output": "对某节点的完整输出跑一段 Python 再加工",
    "hotswap_workflow_node": "热替换某个节点的配置，不用重跑全流程",
    "llm_call": "原生 LLM 调用原语：搭工作流版 ReAct/批量问答用",
    "get_tool_schemas": "拿全部工具 schema，喂给 llm_call 搭子 Agent",
    "call_tool": "按名字执行任意工具（ReAct 循环的执行侧原语）",

    # ===== 内置：其它 =====
    "set_tool_timeout": "调大长任务的执行超时秒数",
    "get_tool_timeout": "查当前执行超时设置",
    "dir_outline": "目录大纲树（每个文件带符号大纲），快速摸清模块",
    "concat_files": "按 glob 把多个文件拼接一次读完",
    "cache_breakpoint": "定位两次 LLM 调用间缓存断在哪，排查命中率下跌",
    "restart_agent": "重启进程让代码改动生效（看门狗自动恢复）",
    "ensure_lsp": "按需装配某语言的 LSP 导航工具（定义/引用/诊断）",
    "reload_mcp_server": "重连指定 MCP server，改完它的代码免重启",

    # ===== light（工作流轻量原语）=====
    "diff_lines": "两个文本块直接对比（不落盘），临时比对用",
    "kv_cache_read": "读跨轮共享的 KV 缓存",
    "kv_cache_write": "写跨轮共享的 KV 缓存",
    "list_append": "往列表追加元素返回新列表，循环收集用",
    "get_list_item": "取列表第 N 个元素（负数从尾部数）",
    "get_list_items": "按下标批量取列表元素",
    "pass_through": "原样透传/组装任意结构，配合编辑器组对象",
    "add": "两个数相加",
    "subtract": "两个数相减",
    "multiply": "两个数相乘",
    "divide": "两个数相除",
    "sleep": "等待 N 秒：轮询间隔/限速用",
    "kw_score": "关键词命中率评分，无 embedding 时的降级重排",
    "cosine_sim": "两段文本的语义相似度（embedding 余弦）",
    "emb_probe": "探测 embedding 模型当前是否可用",
    "contains": "判断文本是否包含某关键词",
    "starts_with": "判断文本是否以某前缀开头（按前缀分流）",
    "ends_with": "判断文本是否以某后缀结尾（按扩展名分流）",
    "to_ascii": "非 ASCII 字符转义，生成 ASCII 安全文本",
    "join": "用分隔符把列表拼成一个字符串",
    "split": "按分隔符把字符串切成列表",
    "length": "取字符串/列表/字典的长度",
    "to_uppercase": "字符串转大写",
    "to_lowercase": "字符串转小写",

    # ===== 资产下载 =====
    "list_downloadable": "看随包有哪些可下载资产（工作流/脚本/mcp）",
    "download_asset": "下载某个随包资产到本地",

    # ===== 长期记忆 =====
    "add_memory": "记一笔值得跨 session 记住的经验",
    "search_memory": "在长期记忆库里按关键词检索",
    "read_procedure": "取某条记忆的完整内容详情",
    "update_memory": "更新一条已有的长期记忆",
    "delete_memory": "按 id 删除一条长期记忆",

    # ===== RAG =====
    "rag_query": "本地文档库语义搜索，问项目设计时自动用",

    # ===== wiki =====
    "wiki_read": "读 wiki 里某个页面",
    "wiki_list": "列 wiki 子目录的页面（各页带标题大纲）",
    "wiki_tree": "整个 wiki 的页面树总览",
    "wiki_search": "wiki 全文搜索",
    "wiki_write": "整页写入/覆盖一个 wiki 页面",
    "wiki_delete": "删除一个 wiki 页面",
    "wiki_add_chapter": "向页面新增一个章节，不动其余部分",
    "wiki_update_chapter": "替换某个章节的正文（子章节一并换）",
    "wiki_remove_chapter": "整棵删除一个章节及其子章节",
    "wiki_move_chapter": "把一个章节整棵挪到新位置调顺序",

    # ===== 团队（多实例）=====
    "team_up": "按清单拉起整个团队：启动+组网+恢复 session",
    "team_status": "团队各成员在线/忙闲/模型总览",
    "remote_connect": "连接另一台机器上的 agt 实例",
    "remote_disconnect": "断开并移除一个远程实例连接",
    "remote_list": "看已连接的远程实例列表",
    "remote_message": "给远程实例异步发条消息，送达即返回",
    "remote_ask": "向远程实例提问并等它回答（带它自己的上下文）",

    # ===== 子 Agent =====
    "create_agent": "声明一个新的子 Agent 角色",
    "kill_agent": "删除一个子 Agent 声明",
    "agent_prompt": "给子 Agent 派活：后台自主跑，立即返回",
    "list_agents": "列出所有已声明的子 Agent",
    "wait_subagents": "等异步子 Agent 干完活拿结果",
    "list_team": "看当前所有活跃 Agent 的状态",
    "agent_ask": "向另一 Agent 无状态询问，不打扰它干活",
    "agent_notify": "向另一 Agent 插话，消息进它的队列",
    "agent_query_events": "看另一个 Agent 最近在干嘛",
    "agent_query_tool_detail": "查另一个 Agent 某次工具调用的完整详情",
    "explore_subagent": "派一次性探索子 Agent 去摸清一个模块",
    "hook_write": "钩子工作流写回副作用（如 set_recap）",

    # ===== 技能 =====
    "read_skill": "读某个技能的详细 SOP 按步骤执行",
    "save_skill": "把可复用任务沉淀成技能",

    # ===== 计划 =====
    "create_plan": "新建计划拆成步骤清单",
    "update_plan": "标记某步进度或改写步骤描述",
    "add_step": "给当前计划追加一步",
    "edit_plan": "改计划的标题或设计",
    "join_plan": "加入一个已有计划",
    "list_plans": "列出全部计划及进度",
    "exit_plan": "退出当前活动计划",

    # ===== 施工方案 =====
    "create_spec": "新建一个施工方案（含文件级步骤）",
    "commit_spec": "提交 spec 给你批阅，阻塞等回应",
    "regenerate_spec": "按你的反馈重新生成一版 spec",
    "list_specs": "列出全部施工方案及批阅状态",
    "recall_spec": "看某个 spec 的完整内容",

    # ===== 用户交互 / 记忆召回 =====
    "ask_user": "向你发问卷收集决定（阻塞等答完）",
    "recall_turn": "召回早期某轮对话的原文",
    "rename_session": "给当前会话改个贴切的名字",
    "get_session_history": "拿当前会话全量结构化历史（结果不截断）",
    "semantic_search_history": "语义检索历史轮次，换种说法也能命中",

    # ===== 工具日志 =====
    "get_tool_detail": "拉某次工具调用的完整详情（入参+结果）",
    "list_tool_logs": "列本会话全部工具调用的 id 清单",

    # ===== 后台 / 调度 =====
    "start_service": "后台起一个长运行服务做联调",
    "stop_service": "停掉指定的后台服务",
    "list_services": "看所有后台服务的运行状态",
    "service_logs": "看某个后台服务最近的输出日志",
    "send_to_service": "给 REPL 型后台服务的 stdin 发一行指令",
    "add_schedule": "加定时/到点任务，到时自动触发一轮",
    "cancel_schedule": "取消一个定时任务",
    "list_schedules": "列出所有定时任务",

    # ===== 自主模式 =====
    "set_autonomous_mode": "开启纯自主模式持续干活直到目标达成",
    "exit_autonomous_mode": "退出纯自主模式",
    "autonomous_status": "看自主模式当前状态",
    "set_goal_check": "设置目标达成验证脚本",
    "check_goal": "手动跑一次目标验证",

    # ===== 其它 =====
    "reload_hot": "改了工具/节点插件后热重载，免重启",
}
