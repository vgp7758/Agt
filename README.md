# Agt · 从零搭建的 AI Agent 框架

> 一个**不依赖 LangChain / LlamaIndex / AutoGen**、每个模块都手写、面向"吃透原理"的多模型自主 Agent 框架。
> 用 OpenAI 兼容协议接入任意大模型，支持 ReAct 自主循环、分层上下文工程、多 Agent 协作（并行）、MCP 工具生态、可编辑技能系统、WebUI，可在任意目录以 cwd 为工作区运行。

本项目最初是一个**边学边造**的过程：从一个最简单的 LLM API 调用起步，逐步加上记忆、人格、工具调用、Agent 循环、真实工具、多 Agent、MCP、技能系统、WebUI……最终得到一个结构清晰、可读、可改的完整 Agent。**它不追求取代成熟框架，而追求把"Agent 到底怎么转起来的"讲明白**——也因此适合作为学习、面试展示与二次开发的起点。

> 实战：用它参加 [AgenTank](https://agentank.ai)（agent-first 坦克编程对抗赛），自主跑通"读状态 → 改代码 → 模拟 → 分析回放 → 发布"全闭环。比赛相关代码在独立任务目录，**本框架本身是通用的**。

---

## ✨ 核心特性

- **多模型字典 + 运行时热切换**：`models.py` 用一个字典集中管理所有 provider（base_url / api_token / model / desc），`/model <name>` 随时切换，无需重启。
- **ReAct 自主推理循环**：思考 → 工具调用 → 观察结果 → 继续，直到任务完成；支持**长程任务**（默认 50 步、可配）+ **软 token 预算**（80% 提醒 / 100% 收尾）+ `Ctrl+C` 优雅打断保留会话。
- **分层上下文工程**：`Turn > Step > ToolCall` 三级结构；喂给 LLM 的是「**全局摘要 + 近期窗口**」的融合——旧轮自动压成摘要，近期轮保留逐 step 原文，长会话也不丢关键决策、不爆上下文。
- **结构化持久化**：会话结构化存盘，`/save` `/resume` 跨进程恢复，记忆完整接续。
- **Function Calling 工具体系**：从 Python 函数签名 + docstring **自动生成 JSON Schema**；内置代码执行 / 文件读写 / `edit`（精确替换）/ `grep`（内容搜索）/ 目录浏览 / 联网搜索 / shell，含**超时 + 目录沙箱 + 限流重试**安全设计。
- **多 Agent 协作**：主 Agent 调度**带工具的自主子 Agent**（继承工具、可选模型、有记忆）；单步内多个工具调用**并行执行**（线程池）。
- **MCP 客户端**：`mcp_client.py` 读任务目录的 `.mcp.json`，连接任意 MCP server（后台 asyncio 桥接、`__mcp__<server>__<tool>` 命名空间）——**任何 MCP server 插上即用**，框架不绑定具体服务。
- **可编辑技能系统（`.agent/`）**：`rules/` 始终生效；`skills/<名>/SKILL.md`（YAML frontmatter + SOP）采用**渐进式披露**——只把摘要放进上下文，匹配时再 `read_skill` 读完整流程；还能 `save_skill` 自主沉淀新技能。
- **WebUI**：`web.py`（FastAPI + WebSocket）+ `static/index.html`——浏览器聊天界面，实时展示思考/工具调用/结果，支持模型下拉、指令按钮、**图片粘贴/上传**（多模态）。
- **结构化输出**：Pydantic 模型 + JSON Schema 约束 + 校验失败自动重试。
- **可观测**：每步打印思考过程（节选）、工具调用与结果、累计 token。
- **检查点回溯**：每条用户指令前自动给工作区打 git 快照（独立仓库，不碰用户的 `.git`）；WebUI 每条消息带"↩ 回溯"按钮，一键把工作区文件 + 对话回滚到该指令之前。

---

## 🧱 架构

```
┌───────────────────────────────────────────────────────────────┐
│  chat.py (REPL) / web.py (WebUI)   入口 · 斜杠命令 · 注册工具          │
├───────────────────────────────────────────────────────────────┤
│  Agent (agent.py)                                              │
│    ReAct 主循环 · 长程自主 · 单步并行工具 · 软token预算 · 事件化输出   │
├────────────────┬──────────────────┬───────────────────────────┤
│   Session      │   Multi-Agent    │   Structured              │
│  分层上下文引擎  │  子 Agent 协作/并行 │  Pydantic 结构化输出       │
│ (Turn>Step>Tool)│ (multiagent.py)  │ (structured.py)           │
├────────────────┴──────────────────┴───────────────────────────┤
│  Tools:  built-in(real_tools) · MCP(mcp_client) · skills(agent_config) │
├───────────────────────────────────────────────────────────────┤
│  LLMClient  多模型 · 空响应重试退避 · 推理(reasoning)处理 · 工具解析 · 流式 │
├───────────────────────────────────────────────────────────────┤
│  models.py(模型字典)   ·   config.py(读 .env)   ·   prompts.py(人设)    │
└───────────────────────────────────────────────────────────────┘
```

### 模块说明

| 文件 | 职责 |
|---|---|
| `chat.py` | 交互式 REPL 入口；斜杠命令分发；SYSTEM = 默认角色 + 内置工具 + 框架能力 + 工作区 `AGENT.md` + `.agent/` 规则/技能 |
| `web.py` | **WebUI 后端**（FastAPI + WebSocket）：每连接一个独立 Agent，线程跑 `run` + 队列桥接实时推流；斜杠命令 stdout 捕获；支持图片多模态 |
| `static/index.html` | 单文件聊天前端（vanilla JS + WebSocket）：对话区 + 实时过程 + 模型下拉 + 指令按钮 + 图片粘贴/上传 |
| `agent.py` | Agent 核心：ReAct 循环、长程边界（max_steps + 软预算）、单步并行工具、`Ctrl+C` 打断、**事件化输出**（`_emit`，CLI 打印 / Web 推流） |
| `session.py` | 分层上下文引擎（Turn/Step/ToolCall）；`messages_for_llm` 融合「全局摘要 + 近期窗口」；惰性摘要压缩；多模态(图片)消息；save/load |
| `llm_client.py` | 统一 LLM 调用：多模型 profile、`switch_model` 热切换、空响应指数退避重试、`reasoning_content` 处理、`tool_calls` 解析、流式 |
| `tools.py` | `Tool`/`Toolbox`：由函数签名 + docstring 自动生成 JSON Schema；按名派发执行 |
| `real_tools.py` | 内置工具（run_python/read_file/write_file/edit/grep/list_dir/web_search/run_shell）；`WORKSPACE = cwd`，沙箱边界即启动目录 |
| `snapshots.py` | 检查点快照/回溯（独立 git 仓库于 `.agt/snapshots`，与用户 `.git` 隔离） |
| `mcp_client.py` | `MCPManager`（后台 asyncio 线程 + AsyncExitStack 长连 + 同步桥，从任务目录拉起 server）+ `MCPTool`（`__mcp__server__tool` 命名空间） |
| `multiagent.py` | `SubAgent`（带工具的自主子 Agent）+ 子 Agent 管理工具（create_agent/agent_prompt/kill_agent/list_agents） |
| `agent_config.py` | `.agent/` rules + skills 加载；`read_skill` / `save_skill` 工具 |
| `structured.py` | Pydantic 结构化输出 + 校验失败重试 |
| `commands.py` | 斜杠命令：`/save /resume /list /show /reset /config /budget /model /help` |
| `prompts.py` | 人设模板 + `build_system`（动态注入日期等上下文） |
| `models.py` / `models.example.py` | 模型字典（**含 token，已 gitignore**；`.example.py` 为模板） |
| `step0~7_demo.py` | 从最简 API 调用到完整 Agent 的渐进式演示，对应搭建的每一步 |

---

## 🚀 快速开始

```bash
# 1. 安装依赖
pip install -r requirements.txt

# 2. 配置模型（复制模板，填入你自己的 key）
cp models.example.py models.py
#   然后编辑 models.py，填入 deepseek / qwen 等的 api_token

# 3. 在任意任务目录启动 —— 当前目录(cwd)即工作区
cd /your/task/dir
python /path/to/chat.py     # CLI 模式
python /path/to/web.py      # WebUI 模式 → 浏览器打开 http://127.0.0.1:8000
```

启动后：
- `/help` 看命令；`/model` 切模型；`/save` `/resume` 管理会话；`/budget` 看 token。
- 直接用自然语言下任务，Agent 自主决定用哪些工具、分几步完成。

### 在任务目录里放配置（可选，按需）

```
your-task-dir/
├── AGENT.md          # 该任务的领域指引（拼进 SYSTEM）
├── .mcp.json         # 要连接的 MCP server（你在任务目录提供）
├── .agent/
│   ├── rules/        # 始终生效的规则
│   └── skills/       # 技能（每个一个子目录 + SKILL.md）
└── （你的 MCP server 代码、任务文件等）
```

> 框架本身是**通用的**，不含任何领域（如 AgenTank）专属代码。领域能力通过任务目录的 `AGENT.md` / `.mcp.json` / `.agent/` 注入。仓库根目录的 `AGENT.md` 与 `.agent/` 是**通用示例**。

---

## 🆚 与主流框架的对比

本项目从零手写，不依赖下列任何框架——**用框架是工程效率，懂原理是架构能力**。

| 框架 | 定位 | 本项目的对应实现 / 取舍 |
|---|---|---|
| **LangChain** | 最流行，Chain/Agent/Tool 链式编排 | 从零实现了其 Agent/Tool/Memory 核心机制，更透明、更易排查与定制 |
| **LlamaIndex** | 偏 RAG / 数据连接 | 上下文工程（分层摘要 + 窗口）与 RAG 检索增强原理相通 |
| **AutoGen / CrewAI** | 多 Agent 对话 / 角色协作 | 主从调度 + 并行派发，思路类似但更轻量、可控 |
| **LangGraph** | 图状态机式复杂 Agent | ReAct 是简化的状态流转，足够覆盖大多数场景；复杂分支可借鉴 |

---

## 📁 项目结构

```
Agt/
├── chat.py                  # CLI 入口
├── web.py                   # WebUI 后端（FastAPI + WebSocket）
├── static/index.html        # WebUI 前端（单文件）
├── agent.py                 # ReAct 核心（事件化输出）
├── session.py               # 分层上下文引擎
├── llm_client.py            # 多模型 LLM 客户端
├── tools.py                 # Tool / Toolbox（自动 schema）
├── real_tools.py            # 内置工具（cwd 沙箱）
├── snapshots.py             # 检查点快照/回溯
├── mcp_client.py            # MCP client（异步桥 + 命名空间）
├── multiagent.py            # 子 Agent 协作
├── agent_config.py          # .agent/ rules + skills
├── structured.py            # Pydantic 结构化输出
├── commands.py              # 斜杠命令
├── prompts.py               # 人设 + 动态上下文
├── config.py                # 配置（读项目根 .env）
├── models.py                # 模型字典（gitignore，含 token）
├── models.example.py        # 模型字典模板
├── step0_hello.py … step7_demo.py   # 渐进式演示
├── AGENT.md                 # 通用示例任务指引
├── .agent/                  # 通用示例 rules + skills
└── requirements.txt
```

---

## 🗺️ 路线图

- [x] 多模型字典 + 热切换
- [x] ReAct 主循环 + 长程自主 + 软预算
- [x] 分层上下文工程（摘要 + 窗口融合）
- [x] 内置工具（含 grep / edit）+ 沙箱
- [x] 多 Agent 协作 + 并行派发
- [x] MCP client（接入任意 MCP server）
- [x] `.agent/` rules + skills（渐进披露 + 自动沉淀）
- [x] WebUI（聊天界面 + 实时过程 + 图片多模态）
- [x] 检查点回溯（每条指令前的 git 快照 + 一键回滚）
- [ ] `.agent/agents/` 子 Agent 模板（按 frontmatter 定义可复用专家）
- [ ] 可观测性：每步思考 / 工具 / token 落盘日志
- [ ] 子 Agent 内部步骤也流式到 WebUI

---

## 📄 许可证

MIT（可自由使用、修改、分发）。

---

> 作者：马建强 · [GitHub @vgp7758](https://github.com/vgp7758)
> 本项目用于学习 AI Agent 架构与原理。欢迎 Issue / PR。
