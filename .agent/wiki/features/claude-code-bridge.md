# Claude Code 桥接 · `claude mcp serve` + ccs.py REPL 服务 + 跨宿主接入文档（2026-10-16/17 实战沉淀）

> Claude Code 官方支持把自己暴露为**标准 stdio MCP server**（`claude mcp serve`）——任何 MCP 宿主（agt / Cursor / Cline / Claude Desktop / VS Code Copilot…）都能接。接入文档：[claude-code-bridge.md](../../docs/claude-code-bridge.md)（两层结构：通用层协议与坑 + 宿主层各家注册命令）；REPL 包装：随包 `src/assets/tools/ccs.py`（repo `tools/` 有同步份）。

## 三个件

| 件 | 职责 |
|---|---|
| `claude mcp serve`（官方形态） | Claude Code 本体作为 stdio MCP server，宿主按标准三件套接入：`command`=claude 本体、`args=["mcp","serve"]`、`env`=认证三选一。agt 侧走 [MCP 配置](mcp-config.md)（mcp.json / `agt mcp add`） |
| `ccs.py`（REPL 包装，市场工具条目之一，见 [marketplace](marketplace.md)） | 把 claude 的 stream-json 流包成 REPL 服务：`SEND <任务>` 派活、`/status` 看进度（事件行 `[text]/[thinking]/[tool_use]/[result]`）——想「盯着 Claude Code 干活过程」的场景。env 契约见文件头 |
| `docs/claude-code-bridge.md` | 跨宿主接入文档：通用层（前置安装 / 认证三选一 / **包装脚本污染 stdio 协议**大坑 / 超时孤儿 / 工具白名单）+ 宿主层（agt / Cursor / Claude Desktop / 其它） |

**认证三选一**（贯穿三件）：① 官方 key `ANTHROPIC_API_KEY`（无需 BASE_URL）／② 第三方中转 `ANTHROPIC_BASE_URL`+key／③ 订阅登录态（终端 `claude` 走一次 OAuth，MCP 子进程复用）。

## 2026-10-17 通用化：不再默认连作者本机 proxy（commit 本地）

此前 ccs.py 的 CLAUDE 路径与 BASE_URL 均有默认值且指向作者本机（`claude.cmd` 绝对路径 + `127.0.0.1:9877` proxy）——**别人装了跑不通**。三处收敛为「探测 / 指引」式通用形态（播种源与 repo 份同步改）：

| 改动 | 效果 |
|---|---|
| CLAUDE 探测链：`CCS_CLAUDE` env → `which claude`(.cmd/.exe) → 作者本机路径仅作末位兜底 | 别人机器自动找到 CLI |
| CLI 缺失 → 打安装指引（`npm i -g @anthropic-ai/claude-code` + `CCS_CLAUDE=` 指定法）后退出 | 不再默默跑错 |
| key 缺失 → 打**认证三选一**指引后继续 | 登录态用户不受阻；第三方用户知道该配什么 |
| `CCS_BASE_URL` 改**可选** | 官方 key 用户零配置（默认连作者 proxy 已删） |

env 契约：`CCS_CLAUDE` / `CCS_BASE_URL`（可选）/ `CCS_API_KEY`（或继承 `ANTHROPIC_API_KEY`）/ `CCS_MODEL` / `CCS_CWD`。

**验证（冒烟双态）**：干净环境（清空 PATH 与 `CCS_*`/`ANTHROPIC_*`）→ 指引输出 rc=0 不炸；正常环境 → `[ccs] 服务就绪` + `/status` 应答，行为不回归。

## 分发闭环定位

死配置留在作者本机不分发；随包 ccs.py 自描述可自助接入（缺什么打什么指引）；文档跨宿主通用——单独发出去也能当 Cursor 用户的接入指南。

## 相关页面

- [MCP 配置页与 agt mcp CLI](mcp-config.md)：宿主侧接入通道（mcp.json / `agt mcp add` / 连接验证）
- [插件市场](marketplace.md)：ccs 作为工具型条目的收录与安装通道
- [run_python](run-python.md)：ccs.py 的子进程基建同源（REPL 往返 expect 语义见 [background-scheduler](background-scheduler.md)）
