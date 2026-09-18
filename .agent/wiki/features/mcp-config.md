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

## reload_mcp 热重连工具（2026-09-14：配置路径列表化）

`src/mcp_client.py` 的 `make_mcp_tools` 生成 `reload_mcp_server` 工具——断开并重连指定 MCP server（server 代码改后免 `/restart`）。此前闭包只绑定**单一** `.mcp.json` 路径，导致**只注册在全局 `~/.agt/mcp.json` 的 server 无法热重连**（如 SCNet 凭证修复后 `reload_mcp_server('scnet')` 被拒）。

本次修复（用户改 SCNet 用户名后发现）：

- `make_mcp_tools` 入参由单路径改为**路径列表**，`reload_mcp_server(name)` 逐个配置找同名 server 重连（单字符串入参向后兼容）
- `src/chat.py` 注册时同时传 `workspace/.mcp.json` 与 `config.config_file("mcp.json")`（repo 级优先、缺省全局）
- `src/commands.py` 的 `/reload_mcp` CLI 帮助文案同步（「repo .mcp.json 与全局 ~/.agt/mcp.json 都查」）

单元验证 4 场景全过：repo 内的 server / 只在全局的 server / 不存在的 server 报错清单 / 单路径兼容。

用法：工具调用 `reload_mcp_server(name)`，或 CLI `/reload_mcp <name>`（name 为 `mcpServers` 键名）。实测场景见 [SCNet 凭证用户名变更](../guides/scnet.md)。

## 全量重载：/reload_mcp 无参数 = 重读两级配置重建全部（2026-09-18，commit c3554c9，用户提案）

`reload_mcp_server(name="")` **留空 = 全量重载**（此前必须逐个点名，commit c3554c9）；CLI `/reload_mcp` 无参数走同款，带参数仍单个重连（原行为不变）。全量流程四步：

1. **断开全部** sessions（旧进程待 shutdown 一起清理，与单连口径一致）
2. **重读两级配置**：repo `.mcp.json` + 全局 `~/.agt/mcp.json`（按 paths 顺序连，同名**后连覆盖先连**——与启动语义一致）
3. **工具全量同步**：配置里消失的 server → 摘除其 `__mcp__{server}__*` 工具前缀 + 清悬空 tool_groups；现存全部 → `sync_to_toolbox` 幂等注册
4. 摘要输出：`✅ MCP 全量重载：N 个 server 在线 [...]；工具同步：摘除 X / 新增 Y / 现存 Z`

**验证（mock 两级配置，四断言全绿）**：全量 5 server 在线 ✓；重连顺序 `['.mcp.json', 'mcp.json']` ✓；模拟 ghost server 从配置删除 → 其工具前缀被摘除 ✓；单 server 分支代码未动（原路径回归无虞）✓。

价值闭环：新增/删除 MCP server（改 mcp.json）后 `/reload_mcp` 一发生效，**不再需要 `/restart`**（正好接住 zai 迁独立 MCP repo 后的运维场景）。

## 注意事项

- 状态徽章基于 mcp_mgr 当前会话快照——「未连接」可能是配置了但未启动/连接失败，点「🔄 状态」刷新
- 生效：后端新端点需 `/restart`；前端 **Ctrl+F5 强刷**（浏览器缓存旧 HTML 是看不到新 UI 的最常见原因，见 [onboarding 实测](../guides/config-and-models.md)）

## 相关页面

- [配置体系与模型调优](../guides/config-and-models.md)（mcp.json 在四份配置 repo 覆盖里）
- [多实例组网](../architecture/multi-instance.md)（每角色实例各持自己的 .agent/mcp.json）
## 缺口闭环：重连后工具同步进工具箱（2026-09-14 实测 → 2026-09-18 修复）

`reload_mcp_server` 的旧语义是「**断开并重连**」——`MCPManager.reconnect_from_config_one(path, name)` 只做 `sessions.pop(name)` + `_connect_one(name, cfg)`，刷新的是 `mcp_mgr.sessions[name]["tools"]`（server 侧工具清单），**不碰 `agent.tools`（工具箱）**。装配期的注册在 `src/chat.py` 的 `build_agent` 里一次性完成（`make_mcp_tools` / `MCPTool` 注册，L326 附近），此后新增的 server 工具不会自动进工具箱。

**实测后果（2026-09-14，SCNet 场景，两次踩到）**：给 `~/.agt/mcp/scnet/scnet_mcp.py` 新增工具（先是 `scnet_notebook`，后是 `scnet_monitor`）后，即使 `reload_mcp_server('scnet')` 成功重连（session 里能看到新工具），Agent 调用仍报「工具箱里没有」——当时只能 `/restart`。

**后记（2026-09-18，commit c3554c9，缺口闭环）**：工具箱同步已收编为 `reload_mcp_server` 的核心语义（docstring：「断开并重连 MCP server，并把工具同步进工具箱——新增/删除的工具立即生效，无需 /restart」）。全量重载（name 留空）路径完整实现：现存 server → `sync_to_toolbox` 幂等注册；消失的 server → 摘除 `__mcp__{server}__*` 工具 + 清悬空 tool_groups。当时诊断出的修法方向（`sync_to_toolbox` API 已存在、闭包没绑 agent/toolbox）即循此落地。**规避条款作废**：新增/删除 MCP 工具后 reload 即可，mock 验证含「ghost server 工具前缀被摘除」断言（见上文[全量重载](#全量重载reload_mcp-无参数--重读两级配置重建全部2026-09-18commit-c3554c9用户提案)章节）。

