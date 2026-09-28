# 工具执行审批 · workspace 外路径 / run_shell / run_python 三按钮放行（2026-09-28，用户提案，commit 4d67ba0）

## 职责与交互形态

此前 Agent 访问 workspace 外路径直接拒绝：`PermissionError: 拒绝访问 workspace 外的路径: D:\`——用户指出「很多时候确实需要访问 workspace 外的路径」，提案改为**人在回路的审批**：涉及敏感操作时，Agent 阻塞等待用户在 WebUI 上决定放行与否。

**交互形态**：answer 气泡区域渲染一张琥珀色审批卡片——

```
🔐 💻 run_shell 请求审批
┌─────────────────────────────────────┐
│ 执行 shell: pip install flask       │
│ ✅ 同意    ❌ 拒绝    ✅✅ 一直同意  │
└─────────────────────────────────────┘
```

**三类触发点**（tool_key 粒度）：

| tool_key | 触发位置 | detail 内容 |
|---|---|---|
| `file_outside` | `real_tools._resolve()` 路径越界时 | 文件工具访问 workspace 外路径: `<path>` |
| `run_python` | `run_python()` Popen 前 | 执行 Python（file 路径 / 内联代码预览 ≤200 字） |
| `run_shell` | `run_shell()` Popen 前 | 执行 shell: `<command ≤300 字>` |

## 三种决定的语义

| 按钮 | 行为 |
|---|---|
| ✅ 同意 | 本次放行（下次同类操作再问） |
| ❌ 拒绝 | 文件类抛 `PermissionError: 用户拒绝访问 workspace 外的路径: …`；run_shell / run_python 返回文本 `[用户拒绝] xxx 执行未获批准`（不炸轮，Agent 收到拒绝结果自行调整） |
| ✅✅ 一直同意 | **按 tool_key 粒度**记入 `agent._approval_always`（set，session 级内存，不落盘）——本 session 该类操作不再问；`/restart` 后恢复询问 |

## 架构四层（照 survey_tools 的 Event 阻塞模式）

| 层 | 改动 |
|---|---|
| **real_tools.py** | 模块级 `_approval_cb` 回调 + `_ask_approval(tool_key, detail) -> bool` 统一入口；`_resolve` 越界改走审批（同意 → 返回目标路径放行本 session，拒绝 → 抛 PermissionError）；`run_python` / `run_shell` 在 **Popen 之前**审批，拒绝返回 `[用户拒绝]` 文本。**回调为 None（CLI 无 WS / 工作流 plugin 节点）→ 短路放行**，默认行为不变 |
| **agent.py** | `_tool_approval(tool_key, detail)`：emit `tool_approval` 事件 → 建 Event 阻塞等待 → **600s 无响应自动拒绝**（防 Agent 永久卡死）；`resolve_tool_approval(id, response)` 被 WS 唤醒并置结果 + always 记忆；工具执行前注入回调（有 `on_event` 且 `_approval_enabled` 才注入，与 `_tool_emit` 注入同点） |
| **server.py** | WS 消息 `action: "approval_response"` → `agent.resolve_tool_approval(id, response)`（与既有 `survey_decision` 同款处理，紧邻） |
| **index.html** | `renderApprovalCard(id, tool, detail)`（琥珀边框卡片 + 三按钮）+ `respondApproval(id, resp)`（WS 发响应 + 按钮置灰防重复点）+ `resolveApprovalCard(id, ok)`（600s 超时由 agent 侧 emit 恢复事件 → 卡片标注「已超时拒绝」） |

## 关键设计：审批在 Popen 之前 → 「计时暂停」零代码满足

用户要求「阻塞期间工具计时什么的都暂停」——因为审批调用发生在 `_run_subprocess_streaming` 的 `start = time.time()` **之前**，TOOL_TIMEOUT 计时、30s 心跳进度、超时转后台全部**天然不包含审批等待段**，无需任何计时器暂停逻辑。等用户 5 分钟再点同意，工具照样满额超时预算起跑。

## 边界与默认

- **CLI / 无前端连接**：`_approval_cb = None` → `_ask_approval` 直接 True，CLI 行为完全不变
- **工作流 plugin 节点**调用 run_python / run_shell：同上（无 Agent 注入回调），不阻塞工作流
- **600s 超时**：自动拒绝并回填结果——审批卡片不会永久挂着（agent 侧 Event 超时兜底 + 前端标注）
- **拒绝不炸轮**：文件类 PermissionError 会进工具错误通道（Agent 可见），shell/python 类返回拒绝文本——Agent 能理解「用户不让」并换路

## 验证（mock agent 六场景全绿，commit 4d67ba0）

```
① 用户同意（2s 后点）   → True，阻塞 2.0s ✓
② 一直同意              → True ✓
③ always 后再调         → True，events=0（不再阻塞）✓
④ 拒绝                  → False ✓
⑤ _resolve 越界放行      → D:\Programs\test.txt 读到 ✓
⑥ 无回调（CLI 模式）     → 自动放行 C:\Windows\test.txt ✓
```

## 注意事项

- 「一直同意」是 **session 级**（内存 set）——重启即失效重问，这是有意的安全默认（不像全局配置永久放行）
- 审批只在**引擎注入回调**的工具路径生效；Agent 自己用 run_python 写 subprocess 的等价物不受管（黑盒执行器本就无法完全拦截，与 mtime 快照 diff 兜底同一哲学，见 [系统总览](../architecture/overview.md)）
- 生效需 `/restart`（引擎层 + 前端 Ctrl+F5）

## 相关页面

- [run_python](run-python.md) — TOOL_TIMEOUT / 超时转后台（审批段在计时之外，两机制正交）
- [LLM 网络韧性](llm-network-resilience.md) — 同日另一问诊批（API 超时阶段诊断）
- [用户交互](user-interaction.md) — WS action 通道（survey_decision / approval_response 同款）
- [运维与排障](../guides/ops.md) — PermissionError 常见错误对照
