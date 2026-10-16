# Claude Code 桥接指南（当 MCP server / 子 Agent 用）

Claude Code 官方支持把自己暴露为**标准 stdio MCP server**（`claude mcp serve`）——
任何 MCP 宿主（agt / Cursor / Cline / Continue / Claude Desktop / VS Code Copilot…）
都能接。本文分两层：**通用层**（协议与坑，所有宿主一样）+ **宿主层**（各家注册命令）。

> 实战沉淀自 agt 接入（2026-10-16/17）：`claude --version` 2.1.288 实测三形态全通。

## 通用层（先读这个——坑都在这）

### 前置

```bash
npm i -g @anthropic-ai/claude-code     # 安装 Claude Code CLI
claude --version                        # 验证可用
```

### 认证三选一

| 路线 | 配置 |
|---|---|
| ① 官方 API key | 环境变量 `ANTHROPIC_API_KEY=sk-ant-...`（无需 BASE_URL） |
| ② 第三方后端/中转 | `ANTHROPIC_BASE_URL=<中转地址>` + `ANTHROPIC_API_KEY=<对应key>`（可选 `ANTHROPIC_DEFAULT_OPUS_MODEL=<模型名>` 路由默认模型） |
| ③ Claude 订阅（Pro/Max） | 终端跑一次 `claude` 完成 OAuth 登录，MCP 子进程复用登录态 |

### 最大的坑：包装脚本污染 stdio 协议 ⚠️

如果你平时用 `.bat`/`.ps1` 包装脚本启动 claude（挂代理、设 env 等），**不要**把包装脚本
配成 MCP 的 command——包装脚本里的 `Write-Host`/`echo` 横幅会混进 stdout，**污染 stdio
MCP 协议**导致握手失败。正确做法是 **env 直灌裸命令**：command 直接指向 claude 本体，
把包装脚本里的环境变量逐项写进 MCP 配置的 `env` 字段。

### 次要提示

- **超时孤儿**：宿主调用 MCP 工具超时后，claude 子进程可能继续跑成孤儿——宿主侧最好有
  超时转后台/杀树策略（agt 已内置）；无此机制的宿主注意长任务别设过短超时。
- **工具白名单**：MCP 配置里可过滤暴露哪些工具（如只留 Agent/WebSearch），减少面。

## 宿主层（各家的注册方式）

### agt（本框架）

mcp.json 加条目（全局 `~/.agt/mcp.json` 或 repo `.mcp.json`）：

```json
"claude": {
  "command": "claude",
  "args": ["mcp", "serve"],
  "env": {
    "ANTHROPIC_BASE_URL": "http://你的中转地址/v1",
    "ANTHROPIC_API_KEY": "你的key",
    "ANTHROPIC_DEFAULT_OPUS_MODEL": "glm-5.2"
  },
  "tools": ["Agent", "WebSearch", "WebFetch"]
}
```

或 CLI 一条命令（env 三项按需删减）：

```bash
agt mcp add --transport stdio claude "claude mcp serve" \
  --env ANTHROPIC_API_KEY=sk-ant-xxx --env ANTHROPIC_DEFAULT_OPUS_MODEL=glm-5.2
```

官方 key 用户去掉 BASE_URL/MODEL 两项即可。Windows 下 `command` 用 claude.cmd 的绝对
路径（如 `C:\Users\<你>\AppData\Roaming\npm\claude.cmd`）更稳。

**进阶（可选）**：随包 `ccs.py` 把 claude 的 stream-json 流包成 REPL 服务
（`SEND <任务>` / `/status` 看进度——事件行 [text]/[thinking]/[tool_use]/[result]），
适合想"盯着 Claude Code 干活过程"的场景：`python tools/ccs.py`，env 契约见其文件头。

### Cursor

`.cursor/mcp.json`（项目根）或全局配置——与 agt 同构：

```json
{ "mcpServers": { "claude": { "command": "claude", "args": ["mcp", "serve"],
  "env": { "ANTHROPIC_API_KEY": "sk-ant-xxx" } } } }
```

### Claude Desktop

`claude_desktop_config.json`（设置 → 开发者 → 编辑配置）：

```json
{ "mcpServers": { "claude-code": { "command": "claude", "args": ["mcp", "serve"] } } }
```

### 其它任何 MCP 宿主

标准三件套：`command` = claude 本体、`args` = `["mcp", "serve"]`、`env` = 你的认证
三选一。认准这三个字段，宿主叫什么都能接。
