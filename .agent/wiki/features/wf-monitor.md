# 工作流运行观测 · run registry + /wf/monitor 实时观测页

> src/workflow.py（`_WF_RUNS` 注册表）+ src/agent.py（钩子 run_id 生成）+ src/server.py（3 路由）+ src/static/wf_monitor.html（观测页）+ src/static/index.html（执行中行可点击 + 实时计时）。2026-08-20 新建，commit 8aeb21a；执行中行秒表计时 commit 6aa5903。

## 背景与职责

**问题**：工作流在对话中被调用时（before_turn/before_answer 钩子、wf_* 工具），UI 只显示「⏳ 执行中…」布尔态，推理长的（如 wiki_auto_maintenance）是**盲盒**——不知道跑到哪个节点、卡了多久、输出了什么。事后日志也只有整体结果。

**方案**：进程内 run registry——每次工作流执行注册一个 run，节点级事件实时写入，观测页轮询渲染节点时间线甘特图。

```
你发消息 → 对话中紫色「⏳ 工作流「xxx」执行中… (Ns)」行（秒表每秒跳动，本地计时）
  → 点击（带 run_id 时）→ 新标签 /wf/monitor?run=<id>
      start  ✓ 2ms   {"user_message": "..."}
      LLM    ⏳ ────▓▓▓▓────（实时增长，橙色脉冲）
      选择器 ✓ 1ms
  → 工作流完成 → 节点全绿 + 总耗时，停止轮询；对话内行定格「完成（共 Ns）」
```

## 核心实现（src/workflow.py）

| 组件 | 职责 |
|------|------|
| `_WF_RUNS: dict` | run_id → `{name, hook, status, started_at, finished_at, nodes[]}` |
| `_WF_RUNS_LOCK` | `threading.Lock`——**线程安全**：同步钩子（线程池）/ async 钩子（后台线程）/ 主循环 wf_* 工具可能并发执行 |
| `_WF_RUNS_MAX = 50` | 内存上限：只保留最近 50 次运行，旧的清掉 |
| `new_wf_run(name, hook)` | 注册新 run（uuid），返回 run_id |
| `list_wf_runs()` | 最近运行列表（倒序摘要，观测页首页用） |
| `get_wf_run(run_id)` | 单次运行完整轨迹（节点时间线 + 输出预览）；未知 run 返回 None |
| `_node_title(n)` | 节点显示标题：`nodeMeta.title` 或 `节点{id}` 兜底 |

**`execute(run_id=...)`**：主执行入口新增 run_id 参数——传 `new_wf_run()` 的 id 时，**每个节点的 start/end/error 事件写入 `_WF_RUNS`**，记录标题/类型/耗时/输出预览（截 200 字）。

**接入点全覆盖**（三种调用路径都注册）：

| 路径 | 接入 |
|------|------|
| `run_hook(run_id=...)` | 透传给 execute（钩子工作流） |
| `make_workflow_tool._run` | Agent 场景注册 `rid = new_wf_run(name, "tool")`（wf_* 工具调用）；**agent=None（测试/独立注册）不注册**——避免测试污染注册表 |
| `src/agent.py` `_run_hooks` | 同步钩子（ThreadPoolExecutor 组）+ async 钩子（后台线程）各生成 run_id，`auto_wf_start` / `auto_wf` / `auto_wf_error` 事件全部带 run_id |

## API 与前端

### server.py 路由

- `GET /wf/monitor?run=<run_id>`：观测页（`_WF_MONITOR_HTML`）；带参=单次运行实时视图，无参=最近运行列表
- `GET /api/wf/runs`：最近运行列表（倒序摘要）
- `GET /api/wf/runs/{run_id}`：单次运行完整轨迹；未知 run 返回 error

### wf_monitor.html（观测页，新建）

- **单次运行视图**：节点时间线表格——序号 / 标题+id / 类型 / 状态 / 耗时 / **甘特时间条**（running 橙色脉冲、完成绿色）/ 输出预览
- **2s 轮询 + 增量渲染**：已画的行不重画，只追加新节点/更新状态；run status=done 停止轮询
- **列表视图**（无参）：最近 50 次运行，点击行进入单次观测

### index.html（对话内入口）

`auto_wf_start` 事件带 `run_id` 时，「⏳ 工作流「xxx」执行中… (Ns)」行**可点击**（虚线下划线 + pointer），`window.open('/wf/monitor?run='+encodeURIComponent(m.run_id))` 新标签打开——这是并行钩子 UI 的自然延伸（见 [user-interaction · 并行钩子 UI](user-interaction.md#并行钩子执行中状态跟踪修复2026-08-19)）。

**实时计时（commit 6aa5903）**：执行中文本由前端 `setInterval` 每秒更新 `(Ns)` 后缀（纯 `Date.now()-t0` 本地计时，后端无过程心跳），完成事件行显示「完成（共 Ns）」、失败行显示「失败（Ns 后失败）」——与观测页甘特条/节点总耗时的秒数实时对照。实现细节（Map 值 `{el, timer, t0}`、isConnected 防泄漏、addTrace 迟到兜底）见 [user-interaction · 执行中实时计时](user-interaction.md#执行中实时计时ns-完成总耗时2026-08-20commit-6aa5903)。

## 与其他可观测能力的关系

| 能力 | 时效性 | 粒度 | 适用 |
|------|--------|------|------|
| **本页 /wf/monitor** | **实时**（2s 轮询） | **节点级** | 对话中点「执行中」行，看工作流跑到哪、卡多久、输出什么 |
| 对话内执行中行秒表 | 实时（1s 本地） | 工作流级 | 不开观测页也能感知钩子跑了多久（纯前端零开销） |
| [/stats](../guides/ops.md#stats-页webui-统计按钮) | 事后 | LLM 调用级 | 缓存命中率、token 经济 |
| llm_calls.jsonl | 事后（持久化） | LLM 调用级 | 钩子内 LLM 走 utility_client（scene=hook:xxx）可在此观测 |
| /api/status | 按需 | 实例级 | 跨实例诊断 |

注意：`_WF_RUNS` 是**进程内存**（/restart 即清空、非持久化）；50 条上限意味着只适合「正在跑/刚跑完」的观测，历史回溯靠 events.jsonl。

## 注意事项

- **生效方式分两截**：执行中行秒表计时是纯前端（index.html 由服务启动载入内存）——**Ctrl+F5 强刷即生效，无需 /restart**；run_id 注册/可点击依赖后端代码，旧进程事件不带 run_id（不可点击），需 `/restart`
- 并发安全靠 `_WF_RUNS_LOCK`——所有 `_WF_RUNS` 读写必须持锁，勿绕过
- 测试场景（`make_workflow_tool(agent=None)`）不注册，保持注册表干净
- 观测页轮询在 run 完成后自动停止，长挂页面无持续请求

## 相关页面

- [工作流引擎与钩子](../architecture/workflow-hooks.md)：execute/run_hook/make_workflow_tool 接入点、async 钩子后台线程同样注册
- [用户交互](user-interaction.md)：并行钩子「执行中」行（可点击打开观测页 + 实时秒表/总耗时，实现主记录在此）
- [运维可观测性](../guides/ops.md)：/stats 事后统计（与本页实时观测互补）
- [wiki_auto_maintenance](wiki-auto-maintenance.md)：典型受益者——async 钩子推理长，之前完全盲盒
