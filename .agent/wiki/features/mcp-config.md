# MCP 配置页 · 配置展示 + 连接状态（night_tasks #3，2026-09-02，commit 8498c39）

> WebUI 的 MCP Server 配置此前只有「添加 server」和「保存 MCP」两个按钮——缺各 server 的信息展示与连接状态。本次补齐：后端 `GET /api/mcp` + `GET /api/mcp/status` 两端点，前端卡片状态徽章 + 「🔄 状态」刷新按钮。

## 职责

- 展示 workspace/.mcp.json 里配置的 MCP server 清单（command 等）
- 实时展示每个 server 的连接状态：🟢 已连接（含工具数）/ 🔴 未连接 / ⚪ 未知
- 手动刷新连接状态（无需重启）

## 数据源

| 层 | 内容 |
|---|---|
| 配置 | `workspace/.mcp.json`（MCPManager 的配置来源之一；chat.py 连接两处：`WORKSPACE/.mcp.json` + `config.config_file("mcp.json")` repo 覆盖，见 [config-and-models · 配置文件解析](../guides/config-and-models.md)） |
| 会话 | `agent.mcp_mgr.sessions`——**已连接会话**（含动态注入、不在配置里的 server） |

## 端点

| 端点 | 语义 |
|---|---|
| `GET /api/mcp` | 读 workspace/.mcp.json（不存在 / 解析失败 → `{"mcpServers": {}}`，不报错） |
| `GET /api/mcp/status` | 配置 ∪ 已连接会话 → 每 server 的 `{connected, tool_count, tools, command}` |

## 前端（index.html）

- `loadMcpConfig()` 并行拉 `/api/mcp` + `/api/mcp/status` → `renderMcpList()` 卡片渲染
- 卡片头部状态徽章：**🟢 已连接 · N 工具**（悬停看工具名清单）/ 🔴 未连接 / ⚪ 未知
- 「💾 保存 MCP」旁新增 **「🔄 状态」**按钮即时刷新（refreshMcpStatus）

## 与其他模块的关系

- mcp_client.py：MCPManager 连接 stdio server、发现工具（失败只告警不中断）
- chat.py：`mcp_mgr.connect_from_config` 两处连接（workspace + repo 覆盖）
- 工具外置判别 [运行时管理器的替代边界](../architecture/tool-externalization-criteria.md)：MCP 管理器是引擎侧 runtime 管理器，与 background/lsp/reload_hot 同族（不外置）

## 注意事项

- 状态徽章基于 mcp_mgr 当前会话快照——「未连接」可能是配置了但未启动/连接失败，点「🔄 状态」刷新
- 生效：后端新端点需 `/restart`；前端 **Ctrl+F5 强刷**（浏览器缓存旧 HTML 是看不到新 UI 的最常见原因，见 [onboarding 实测](../guides/config-and-models.md)）

## 相关页面

- [配置体系与模型调优](../guides/config-and-models.md)（mcp.json 在四份配置 repo 覆盖里）
- [多实例组网](../architecture/multi-instance.md)（每角色实例各持自己的 .agent/mcp.json）
