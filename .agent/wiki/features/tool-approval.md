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

- **CLI / 无前端连接**：`_approval_cb = None` → `_ask_approval` 直接 True，CLI 行为完全不变——**仅指 run_python / run_shell 的 opt-in 审批**；file_outside（越界文件）2026-10-16 起走**独立 fail-closed 通道**（`_outside_approval_cb`），无人可问 = 拒绝，见下方专节
- **工作流 plugin 节点**调用 run_python / run_shell：同上（无 Agent 注入回调），不阻塞工作流；但**文件越界访问**同条 fail-closed 拒绝
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

## 审批默认【关闭】：opt-in 开关（2026-09-29，commit 24ee874，50052 实锤）


**现象（50052 实例实锤，2026-09-29）**：用户报「50052 调 run_python 时进程就挂了」。同轮真根因是 **D 盘 0GB**（写文件全失败），但审批层暴露了更严重的使用问题：**run_python 每次调用都弹审批、无人值守没人点 → 阻塞 600s → 流程卡死**（子 Agent / 后台/巡检场景尤其致命）。

**修复（src/agent.py，回调注入点）**：从「有 WS 连接才阻塞等审批」收紧为 **默认关闭的 opt-in**——

```python
_rt._approval_cb = (self._tool_approval if self.on_event
                    and getattr(self, "_approval_enabled", False) else None)
```

| `_approval_enabled` | 行为 |
|---|---|
| 未设（默认） | `_approval_cb = None` → **完全旧行为**：越界直接 `PermissionError` 拒绝、run_python / run_shell 直接执行不询问 |
| `True`（显式开） | 三段触发点照旧阻塞等用户在卡片上决定（含 600s 超时兜底） |

**动机**：审批是**可选的安全层**，不是默认路径——默认开着会把「无人值守/子 Agent/工作流」这些没有人在场的场景全变成一次 600s 挂起。要用时显式开启（`agent._approval_enabled = True`，或后续接 `settings.enable_tool_approval`），开启后刷新恢复也一并生效（见下节）。

**排查提示**：「实例调某工具后整个进程挂着不动」先分清两类根因——① 审批阻塞等不到人（看 answer 区有无审批卡片 / 600s 是否自动解除）② 磁盘满等环境级故障（本轮 50052 的真凶，见 [ops 常见错误对照](../guides/ops.md)）。

## 刷新恢复：pending 审批重发（2026-09-29，commit 7bdaa7a，用户实锤）


**现象（用户实锤，2026-09-29）**：审批卡片出现时按 `Ctrl+Shift+R` 刷新 WebUI → 页面转为**历史渲染**，审批卡片消失，但 Agent 侧 `Event` 仍在阻塞——**用户既看不到也点不了**，只能干等 600s 超时。

**根因**：审批卡片由**实时事件** `approval_request` 驱动，刷新即丢；历史渲染路径（`current_history`）只重放 turns/steps，不产出审批卡。

**修复（照 `_pending_survey` / `check_pending_spec` 同款模式）**：

| 层 | 改动 |
|---|---|
| `src/agent.py` `_tool_approval` | 发起时写 `session.extra_state["_pending_approval"] = {"id", "tool", "detail"}`（**随 session 落盘**）；resolve / 超时解除后 `pop` |
| `src/server.py` `current_history` | 历史发送完、spec/survey 补发之后，检查 `_pending_approval` → **re-emit** `{"type":"approval_request", id, tool, detail}` → 前端重新渲染卡片 |

**效果**：刷新后卡片复现，点击照常解除阻塞（与 spec 面板补发、survey 补发同址同哲学——**实时事件必须有持久化的补发源**）。

**边界**：`human_step` 的 pending（`_pending_human_step`）已同样落 extra_state，但 `current_history` 的补发目前只覆盖审批卡——见 [human_step · 注意事项](human-step.md)。

## 越界放行 → 工具结果内提示：漏传 remote_instance_id 的当步可见（2026-09-29，用户补充，v0.30.10）

**场景**（50052 实锤）：write_file("C:\ProgramData\frida_spawn_hook.py") 漏传 remote_instance_id → 本地执行 → 路径越界 → 审批放行 → **静默写进本地**。模型当步毫无感知，错位写入既成事实。

**机制**（real_tools.py + agent.py，用户补充「捕获报错后作为工具结果返回」）：
- `_resolve` 审批放行 workspace 外路径时置 `_tls.outside = str(path)`（threading.local 越界放行标记）
- `agent._exec_tool` 工具返回后读取并**清位**，结果尾部追加：

```
⚠️ [越界放行] 刚才访问的 C:\ProgramData\frida_spawn_hook.py 在 workspace 外（审批已放行）。
若本意是操作远端实例的文件，可能是漏传了 remote_instance_id（当前路由：taobao）——请确认目标实例。
```

- 「当前路由」显示粘性实例（若有）——与[粘性路由](../architecture/multi-instance.md)联动，模型**下一笔即可自纠**，而不是静默错位写入。

**设计取舍**：提示走**工具结果通道**（模型当步可见），而非 warn 事件（只有人看得到，模型看不见）；不阻断执行——放行了就执行，只是让模型知道发生了什么。

### 后记：提示文案随路由参数退役改引导（2026-10-11，commit 212f33a）

普通工具 schema 不再注入 `remote_instance_id`（远端调用统一走 remote_call_tool，见 [multi-instance · 路由参数退役](../architecture/multi-instance.md)）后，越界放行提示的引导文案同步改——从「可能是漏传了 remote_instance_id」改为指向 `remote_call_tool`：

```
⚠️ [越界放行] 刚才访问的 … 在 workspace 外（审批已放行）。
若本意是操作远端实例的文件，请用 remote_call_tool(remote_instance_id, name, arguments) 显式调用（当前路由：taobao）。
```

机制本身（`_tls.outside` 越界标记 → 工具结果尾附提示）不变；「当前路由」显示粘性实例的联动照旧。

### 后记二：「当前路由」粘性显示一并移除（2026-10-12，commit eee624f）

粘性路由整体退役（见 [multi-instance · 粘性路由 · 后记三](../architecture/multi-instance.md)）后，「当前路由：X」失去来源——它显示的正是粘性实例。文案收敛为：

```
⚠️ [越界放行] 刚才访问的 … 在 workspace 外（审批已放行）。
若本意是操作远端实例的文件，请改用 remote_call_tool——请确认目标实例。
```

机制本身（`_tls.outside` 越界标记 → 工具结果尾附提示、不阻断执行）不变。

## 越界放行默认改需审批：file_outside 独立 fail-closed 通道 + 子 Agent 弹卡可达（2026-10-16，用户提案，commit 5dcbed6，随 v0.35.3 发布）

**动机（用户提案）**：file_outside（越界文件访问）此前**跟随 run_python/run_shell 的 opt-in 开关**——`_approval_enabled` 未开（默认）时 `_approval_cb=None` → `_ask_approval` 直接 True：**越界访问静默放行**；即使开关打开，WS 响应也只达主 Agent 实例，子 Agent 弹的卡无人能解。语义错位：run_python/run_shell 是主动执行类操作，opt-in 合理；而**访问 workspace 外的文件本身就是敏感越界**——默认应该「不问不给」（fail-closed），UI 在场才弹卡交给人裁决。

**改动三层**：

| 层 | 改动 |
|---|---|
| **real_tools.py** | 新增 `_outside_approval_cb`——**仅管 file_outside**，与 run_python/run_shell 的 opt-in `_approval_cb` 分离互不干扰；`_resolve` 越界改走 `_ask_outside_approval`——**fail-closed**：UI 同意才放行并登记 `_approved_paths`；拒绝 / 无人可问（CLI、`on_event=None` 的子 Agent、工作流节点）→ `PermissionError「越界未获审批，拒绝访问 workspace 外的路径」`；`_resolve_read` 的「读放行」退役（**diff_files 越界同样需审批**）；新增 `_resolve_quiet` 供簿记纯解析（不弹卡） |
| **agent.py** | 装配点注入 `_outside_approval_cb`（**有 on_event 即注入**——不与 run_* 共用 `_approval_enabled` 开关）；`_APPROVAL_ROUTES` 全局注册表：WS `approval_response` 按审批 id 路由到**发起弹卡的 Agent 实例**——子 Agent 弹的卡也能解除（原先只达主 Agent）；`_collect_file_snapshots` 改 `_resolve_quiet` + **仅拍 `_approved_paths` 内的越界文件** |
| **server.py / 前端** | `approval_response` 处理改查 `_APPROVAL_ROUTES`（卡片三按钮交互不变） |

**行为矩阵（改后）**：

| 场景 | file_outside（越界文件） | run_python / run_shell |
|---|---|---|
| WebUI（有 on_event） | **默认弹卡**（原默认静默放行） | opt-in 不变（显式 `_approval_enabled` 才弹） |
| CLI / 无人值守 / 无 on_event 的子 Agent / 工作流节点 | **默认拒绝**（fail-closed） | 直接执行不变 |

**两处旧语义就此终结**：

- **读写不对称终结（t1195）**：`_resolve_read` 此前对读操作越界直接放行——退役后 diff_files 等读工具越界同样需审批
- **快照侧信道堵死**：拒绝后越界文件不再进 recent-file 快照——「文件工具被拒、内容却经快照进投影」的旁路消失（[context-engine · recent-file 后记三](../architecture/context-engine.md) 的 relative_to 双层容错退居纯兜底）；同时簿记解析改 `_resolve_quiet`，快照采集不再二次弹卡

**验证**：`.agent/tmp/verify_oob_approval.py` **15/15 绿**；`verify_edit_tools` 两条「跨 workspace 被拒」由基线红转绿（其余 7 处 ❌ 为改动前已存在的行级视图门基线失败，与本改动无关）。

**注意事项**：无人值守场景（定时任务 / 后台服务 / 施工 worker）访问 workspace 外路径将直接拒绝——需要越界的任务要么在 WebUI 在场时人工放行，要么把所需文件挪进 workspace。

## 相关页面


- [@file: 文件引用](file-ref-params.md) — 同在 `_exec_tool` 调用管线上的参数预处理（展开在路由/执行之前）；「工具结果通道」附注同哲学（2026-10-11）
- [run_python](run-python.md) — TOOL_TIMEOUT / 超时转后台（审批段在计时之外，两机制正交）
- [human_step 人在环](human-step.md) — 同款 Event 阻塞 + WS action + pending 落 extra_state（安全门 vs 任务步骤）
- [LLM 网络韧性](llm-network-resilience.md) — 同日另一问诊批（API 超时阶段诊断）
- [用户交互](user-interaction.md) — WS action 通道（survey_decision / approval_response / human_step_response 同款）
- [运维与排障](../guides/ops.md) — PermissionError / 磁盘满常见错误对照

