# Agt — AI Agent 框架

> 多模型 ReAct 引擎 + MCP 工具 + Coze 工作流 + WebUI + 可视化编辑器。不依赖 LangChain / LlamaIndex / AutoGen，每个模块手写。

```bash
pip install agt-agent
agt          # CLI 对话
agt-web      # WebUI → http://localhost:8000
```

---

## 功能

| 模块 | 说明 |
|---|---|
| **ReAct 引擎** | 思考 → 工具调用 → 观察 → 循环，最大步数 + Token 预算 + Ctrl+C 中断 |
| **多模型** | DeepSeek / Qwen / GLM / MiniMax / StepFun / 任意 OpenAI 兼容，热切换 + 回退链 |
| **MCP** | Model Context Protocol 客户端，自动发现工具 |
| **工作流** | Coze 原生画布 JSON 执行器，13 类节点，每轮扫描注册为 Agent 工具 |
| **可视化编辑器** | SVG 画布 + 节点拖拽 + 流程线/变量线(彩色) + 属性面板，零依赖 |
| **多 Agent** | SubAgent 并行调度，工具继承，防递归 |
| **WebUI** | FastAPI + WebSocket，多客户端广播，断线续连 |
| **会话** | 分层上下文（全局摘要 + 最近窗口融合）+ 存档/恢复 |
| **自主模式** | 定时/目标驱动，消息队列 |
| **Wiki** | `.agent/wiki/` 知识库 CRUD + 子 Agent 自动维护 |
| **技能** | `.agent/skills/` YAML 渐进式披露 |
| **Agentic RAG** | 工作流设 `auto:true`，消息自动预取注入 |
| **批处理** | 任意节点对数组逐元素执行，输出 all/filtered/nth |

---

## 快速开始

### 1. 安装

```bash
pip install agt-agent
```

### 2. 配置模型

启动后点 ⚙ 设置 → 添加模型（base_url / api_token / model id），保存到 `~/.agt/models.json`。

或手动：复制 `models.example.py` → `models.py`，填 token。

### 3. 启动

```bash
agt          # CLI
agt-web      # WebUI（浏览器打开 http://localhost:8000）
```

---

## 工作流

1. 打开 `http://localhost:8000/editor`（或 WebUI 点 ✏ 编辑器）
2. 左侧调色板拖放节点 → 连线 → 右侧面板编辑属性 → Ctrl+S 保存
3. 文件存入 `.agent/workflows/<名>.json`
4. Agent 每轮自动扫描 → 注册为 `wf_<名>` 工具，模型可调用

**自动工作流（Agentic RAG）**：编辑器勾选 ✅ 自动 + 填写参数名，保存后 `meta` 写入 `{"auto":true,"auto_param":"query"}`。之后每次发消息，Agent 先跑该工作流取上下文再处理。

### 节点类型（13 类）

开始/结束 · LLM · 代码 · 选择器(分支) · 循环 · 批处理 · 意图识别 · JSON 解析/构造 · 文本处理 · HTTP 请求 · 子工作流 · 插件 · 变量聚合/赋值

---

## 项目结构

```
├── pyproject.toml       # pip install 配置（entry: agt, agt-web）
├── src/                 # 源代码
│   ├── chat.py          # CLI
│   ├── web.py           # WebUI + API
│   ├── agent.py         # ReAct 引擎
│   ├── workflow.py      # Coze 工作流执行器
│   ├── tools.py         # Tool/Toolbox
│   ├── real_tools.py    # 内置工具（Python/文件读写/grep/搜索/shell）
│   ├── llm_client.py    # 多模型客户端
│   ├── session.py       # 分层上下文会话
│   ├── mcp_client.py    # MCP 协议
│   ├── multiagent.py    # 多 Agent
│   └── ...
├── static/              # WebUI 前端
│   ├── index.html       # 聊天界面
│   └── workflow_editor.html  # 工作流编辑器
├── .agent/              # 工作区（workflows/rules/skills/wiki）
└── examples/            # step0-7 教程
```

---

## 开发

```bash
git clone https://github.com/vgp7758/Agt.git
cd Agt
pip install -e .
```

运行测试：`python examples/step*.py`

---

## 卸载

```bash
pip uninstall agt-agent
rm -rf ~/.agt
```

## License

MIT
