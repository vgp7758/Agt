# 任务指引（AGENTS.md）— 通用示例

> 本文件由你（用户）维护，放在**启动目录**（即 workspace / cwd）中，启动时会被读取并拼进 Agent 的 SYSTEM。
> 这是框架自带的**通用示例**——把它复制到你的任务目录，按实际任务改写即可。改这里就能调整 Agent 的领域行为/策略，无需改代码，重启生效。
> 文件名遵循 OpenAI 跨工具标准 `AGENTS.md`；旧名 `AGENT.md` 仍兼容（优先读 `AGENTS.md`）。

## 你是谁/在做什么
（在这里用一两句写清当前任务的领域与目标。例：「你是一个帮助我分析数据并产出报告的助手」「你正在维护 XXX 项目」。）

## 可用工具
- **内置工具**：run_python（写并运行 Python）/ read_file / write_file / edit（精确替换）/ grep（内容搜索）/ list_dir / web_search / run_shell（慎用）。
- **MCP 工具**：由任务目录 `.mcp.json` 声明的 MCP server 提供，名字带 `__mcp__<server>__<tool>` 前缀，按描述选用。
- **多 Agent**：可用 create_agent(name, model, system, tools) 创建带工具的子 Agent，agent_prompt(name, prompt) 派任务，并行可同时进行的子任务。
- **技能**：见 `.agent/skills/`（SYSTEM 里有摘要；匹配时先 read_skill(name) 取详细 SOP）。

## 工作原则（按需改写）
- 先理解需求再动手；复杂任务拆成小步，每步用合适的工具。
- 改文件优先用 edit（精确替换），避免整体覆盖；改动前先读懂现有内容。
- 执行有副作用或不可逆的操作（删除、发布、联网提交等）前，先向用户确认。
- 优先简单稳健的方案；遇到错误如实上报并尝试修正。
- 文件操作（read/write/edit/grep/list_dir）和 run_python 都在**当前目录(cwd)**下进行。

## 当前上下文（可选）
（写一些模型不知道、但任务需要的背景：当前日期、项目状态、已知约束、用户偏好等。）
