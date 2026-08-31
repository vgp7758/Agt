# 工作流运行观测 · run registry + /wf/monitor 实时观测页

> src/workflow.py（`_WF_RUNS` 注册表 + 全量输出预算）+ src/agent.py（钩子 run_id 生成）+ src/server.py（4 路由）+ src/static/wf_monitor.html（观测页）+ src/static/index.html（执行中行可点击 + 实时计时）。2026-08-20 新建，commit 8aeb21a；执行中行秒表计时 commit 6aa5903；**节点全文纯文本查看 commit bb56a82**。

## 背景与职责

**问题**：工作流在对话中被调用时（before_turn/before_answer 钩子、wf_* 工具），UI 只显示「⏳ 执行中…」布尔态，推理长的（如 wiki_auto_maintenance）是**盲盒**——不知道跑到哪个节点、卡了多久、输出了什么。事后日志也只有整体结果。

**方案**：进程内 run registry——每次工作流执行注册一个 run，节点级事件实时写入，观测页轮询渲染节点时间线甘特图；节点预览截 200 字适合看「跑到哪」，需要完整输出时点击预览打开纯文本页。

```
你发消息 → 对话中紫色「⏳ 工作流「xxx」执行中… (Ns)」行（秒表每秒跳动，本地计时）
  → 点击（带 run_id 时）→ 新标签 /wf/monitor?run=<id>
      start  ✓ 2ms   {"user_message": "..."}
      LLM    ⏳ ────▓▓▓▓────（实时增长，橙色脉冲）
      选择器 ✓ 1ms   分流结果… 📄 ← 点击预览（has_full）
                              ↓ 新标签（纯文本页，无样式）
      {"content": "分析完成，……\n\n1. ……", "usage": {...}}（节点完整输出）
```

## 核心实现（src/workflow.py）

| 组件 | 职责 |
|------|------|
| `_WF_RUNS: dict` | run_id → `{name, hook, status, started_at, finished_at, nodes[]}`（node 内含 `preview` 与可选 `full`） |
| `_WF_RUNS_LOCK` | `threading.Lock`——**线程安全**：同步钩子（线程池）/ async 钩子（后台线程）/ 主循环 wf_* 工具可能并发执行 |
| `_WF_RUNS_MAX = 50` | 内存上限：只保留最近 50 次运行，evict 最旧 run 时**同步扣减 `_full_total`** |
| `new_wf_run(name, hook)` | 注册新 run（uuid），返回 run_id |
| `list_wf_runs()` | 最近运行列表（倒序摘要，观测页首页用） |
| `get_wf_run(run_id)` | 轮询视图：**剥离所有 `full`、补 `has_full` 标记**——2s 轮询不能每次传几十万字符，全文走单节点路由 |
| `get_wf_node_full(run_id, node_id)` | 单节点全量输出访问器（纯文本路由用）；run/node 不存在返回 None，节点存在但未记录全文（预算耗尽/running 中）返回 '' |
| `_preview_str(v)` | 预览：dict/list 转 JSON、换行压成空格、截 200 字 |
| `_full_str(v)` | 全文：dict/list 转 JSON **保留换行**，单节点截 `_FULL_CAP`（200K），超限尾部标注「完整值共 N 字符」 |
| `_node_title(n)` | 节点显示标题：`nodeMeta.title` 或 `节点{id}` 兜底 |

**`execute(run_id=...)`**：主执行入口新增 run_id 参数——传 `new_wf_run()` 的 id 时，**每个节点的 start/end/error 事件写入 `_WF_RUNS`**；end/error 事件并行携带 `preview` + `full`，entry（start 节点）与 exit（end 节点）的输出同样记录。

**接入点全覆盖**（三种调用路径都注册）：

| 路径 | 接入 |
|------|------|
| `run_hook(run_id=...)` | 透传给 execute（钩子工作流） |
| `make_workflow_tool._run` | Agent 场景注册 `rid = new_wf_run(name, "tool")`（wf_* 工具调用）；**agent=None（测试/独立注册）不注册**——避免测试污染注册表 |
| `src/agent.py` `_run_hooks` | 同步钩子（ThreadPoolExecutor 组）+ async 钩子（后台线程）各生成 run_id，`auto_wf_start` / `auto_wf` / `auto_wf_error` 事件全部带 run_id |

## 节点全文查看（2026-08-20，commit bb56a82）

**动机**：观测页预览截断（200 字、换行压扁）适合看「跑到哪」，但排障时经常需要**节点完整输出**（LLM 回包全文、retrieval 检索结果全量）。方案：点击节点预览 → 新标签打开 **text/plain 纯文本页**——页面文本直接就是节点完整输出，非 HTML、无任何样式，浏览器原生渲染。

三层实现：

| 层 | 实现 |
|----|------|
| 存储（src/workflow.py） | `_run_track` 的 node_end/node_error 事件在 `preview` 之外并行存 `full`（`_full_str` 序列化，保留换行/JSON 结构） |
| 路由（src/server.py） | `GET /api/wf/runs/{run_id}/node/{node_id}` → `PlainTextResponse`（`text/plain; charset=utf-8`）；未知 run/node → 404 `[不存在]`；节点存在但无全文 → `[无全文]`（执行中 / 全量预算耗尽只存预览） |
| 前端（src/static/wf_monitor.html） | `has_full` 时预览单元格可点击（虚线下划线 + 📄 + tooltip「点击查看完整输出」），`window.open('/api/wf/runs/…/node/…', '_blank')` 新标签打开 |

**内存防线**（观测功能不能吃爆进程内存）：

- **单节点上限 `_FULL_CAP = 200_000` 字符**：超出截断，尾部标注「完整值共 N 字符」（用户知道真实长度）
- **总预算 `_FULL_BUDGET = 20_000_000` 字符**：`_full_total` 全局计数（`_run_track` 存 full 时入账），**预算耗尽后后续节点只存预览**（前端无 📄 不可点）
- **evict 扣减**：`new_wf_run` 挤掉最旧 run 时，从 `_full_total` 扣除该 run 各节点 full 长度——计数始终与注册表实际存量一致
- **教训**：`_run_track` 内 `_full_total +=` 必须先 `global _full_total` 声明，否则 UnboundLocalError（调试中当场抓出，未带上线）

**轮询带宽**：`get_wf_run` 返回前剥离所有 `full` 并补 `has_full` 布尔——观测页 2s 轮询只传摘要，全文按需单节点拉取。

## 嵌套子画布轨迹：复合节点 / 子工作流的子节点事件（2026-08，commit 31d5ef3）

**背景**：观测页只能看到顶层节点——loop/batch/subworkflow 是一个黑盒节点，子画布内部跑到哪看不到（调试 wait_extract 等待循环 / 子工作流时缺关键视野）。

**引擎侧（src/workflow.py）**：

- **`track_stack` 嵌套观测容器栈**：`execute(canvas, ..., track_stack=[])` 新增参数——子工作流执行时 `_handle_subworkflow` push 容器，子节点事件写**栈顶容器**而非顶层 run；栈非空时本 execute **不发 run_done**（整体结束态归最外层）
- **复合节点（loop/batch）轮容器**：`_run_composite_body` 每轮迭代收集体内节点事件 → 每轮尾部实时更新 `node_meta`（`children`=最后一轮轨迹 + `rounds` + `childmeta` 子节点标题映射）——运行中展开观测页即可看到最后一轮逐轮刷新
- **嵌套复合**（子画布里还有 loop）经栈自然支持任意深度
- 子节点事件走 `_track_apply(store_full=False)`：嵌套子节点**只存 preview**，全文与预算仍归顶层节点（防 20M 预算被嵌套爆掉）

**前端（wf_monitor.html）**：顶层节点行可展开（`▸ 循环 200001 ♻ 12 轮 · 5 子节点` / `▸ sub_test 🔗 extract_keywords · 9 节点`）——子轨迹表（子节点/类型/状态/耗时/输出预览）；展开状态跨 2s 轮询保持；点击立即重画。

**顺手修的真 bug（测试暴露）**：execute 初始 ready **无条件排除 type 2**——`start→end` 直连的子工作流 exit 永远不进 ready 队列 → 隐式结束返回 `{}`（输出丢失）；`execute_debug` 没有这个排除所以调试页一直正常，掩盖了问题。修复：只排除「非 entry 后继的孤立 end」。

**e2e 验证**：loop rounds=3 + 最后一轮 children + childmeta ✓；subworkflow wf_name + 完整子轨迹（entry+exit）✓；run_done 只发一次（嵌套不发）✓；输出正确透传 ✓。需 `/restart` 生效（详见 [workflow-hooks · 嵌套子画布轨迹](../architecture/workflow-hooks.md#嵌套子画布轨迹复合节点--子工作流的子节点事件2026-08commit-31d5ef3)）。

## run_id 注入溯源：钩子注入标签带 run 属性（2026-08-31，commit 38afea6）

**背景（用户提案「把缓存 id 带上」）**：run registry 的 canvas 现场缓存（`new_wf_run` 注册时保存的执行画布快照——观测页「在调试页查看」导入的就是它）此前只与 UI 入口（执行中行可点击）关联；[recap_gen 间歇性 local-qwen 排查](../architecture/workflow-hooks.md#复发与三层观测网扫描层-model-校验2026-08-31commit-0dc1dfc)暴露的缺口是——异常轮的**注入文本**与该次执行的 run 无关联，间歇性异常只能从日志反推。

**改动两处（src/agent.py）**：

```python
# ① _run_hooks 合并段——rid 此前在返回值里、append 时丢了
notes.append({"hook": hook, "name": nm, "result": result.strip(), "rid": rid})

# ② _chat_msgs 注入标签带 run 属性（n.get 容错：旧 notes 无 rid → 空串）
parts = [f'<hook name="{n["name"]}" run="{n.get("rid", "")}">\n{n["result"]}\n</hook>' for n in items]
```

**注入形态**（下一轮起主 Agent 上下文里可见）：

```html
<hook name="wiki_auto_query" run="ec822fe4">
📖 相关 wiki（本地检索流水线命中）：...
</hook>
```

**意义**：注入文本自带溯源凭证——`run` 属性直达 `/wf/monitor?run=<rid>`，回溯该次执行的完整 canvas（节点输入里 llmParam 的 model 原值等现场证据）。间歇性异常（如 local-qwen 路由）复现时，**现场本身就是证据**——配合扫描层/执行层/流水三层观测网（0dc1dfc + 0d852a0 + 7c38a98）构成完整捕捉面。

**容量前提（已有，无需新做）**：注册表 50 次 LRU（≈16 轮钩子观测历史，每轮约 3 次钩子 run）+ `_full_total` 20M 全文预算——比原始提案「超 3 轮清掉」宽裕且内存已控。`/restart` 生效。

## API 与前端

### server.py 路由

- `GET /wf/monitor?run=<run_id>`：观测页（`_WF_MONITOR_HTML`）；带参=单次运行实时视图，无参=最近运行列表
- `GET /api/wf/runs`：最近运行列表（倒序摘要）
- `GET /api/wf/runs/{run_id}`：单次运行完整轨迹（**剥离 full + has_full 标记**）；未知 run 返回 error
- `GET /api/wf/runs/{run_id}/node/{node_id}`：**节点全量输出 text/plain 纯文本页**（见上文节点全文查看）

### wf_monitor.html（观测页）

- **单次运行视图**：节点时间线表格——序号 / 标题+id / 类型 / 状态 / 耗时 / **甘特时间条**（running 橙色脉冲、完成绿色）/ 输出预览；预览单元格 has_full 时可点击开全文纯文本页
- **2s 轮询 + 增量渲染**：已画的行不重画，只追加新节点/更新状态；run status=done 停止轮询
- **列表视图**（无参）：最近 50 次运行，点击行进入单次观测

### index.html（对话内入口）

`auto_wf_start` 事件带 `run_id` 时，「⏳ 工作流「xxx」执行中… (Ns)」行**可点击**（虚线下划线 + pointer），`window.open('/wf/monitor?run='+encodeURIComponent(m.run_id))` 新标签打开——这是并行钩子 UI 的自然延伸（见 [user-interaction · 并行钩子 UI](user-interaction.md#并行钩子执行中状态跟踪修复2026-08-19)）。

**实时计时（commit 6aa5903）**：执行中文本由前端 `setInterval` 每秒更新 `(Ns)` 后缀（纯 `Date.now()-t0` 本地计时，后端无过程心跳），完成事件行显示「完成（共 Ns）」、失败行显示「失败（Ns 后失败）」——与观测页甘特条/节点总耗时的秒数实时对照。实现细节（Map 值 `{el, timer, t0}`、isConnected 防泄漏、addTrace 迟到兜底）见 [user-interaction · 执行中实时计时](user-interaction.md#执行中实时计时ns-完成总耗时2026-08-20commit-6aa5903)。

## 与其他可观测能力的关系

| 能力 | 时效性 | 粒度 | 适用 |
|------|--------|------|------|
| **本页 /wf/monitor** | **实时**（2s 轮询） | **节点级** | 对话中点「执行中」行，看工作流跑到哪、卡多久、输出什么 |
| 本页节点全文纯文本路由 | 实时（按需） | 单节点输出全文 | 点预览看 LLM 完整回包/检索全量结果（200K 内原文） |
| [调试页节点输出白框](workflow-debug.md) | 画布内（播放后） | 节点级 | 调试页编辑工作流时节点下方直接看输出（可折叠），不切页面 |
| 对话内执行中行秒表 | 实时（1s 本地） | 工作流级 | 不开观测页也能感知钩子跑了多久（纯前端零开销） |
| [/stats](../guides/ops.md#stats-页webui-统计按钮) | 事后 | LLM 调用级 | 缓存命中率、token 经济 |
| llm_calls.jsonl | 事后（持久化） | LLM 调用级 | 钩子内 LLM 走 utility_client（scene=hook:xxx）可在此观测 |
| /api/status | 按需 | 实例级 | 跨实例诊断 |

注意：`_WF_RUNS` 是**进程内存**（/restart 即清空、非持久化）；50 条上限 + 20M 全文预算意味着只适合「正在跑/刚跑完」的观测，历史回溯靠 events.jsonl。

## 注意事项

- **生效方式分两截**：执行中行秒表计时是纯前端（index.html 由服务启动载入内存）——**Ctrl+F5 强刷即生效，无需 /restart**；run_id 注册/可点击/全文存储与路由依赖后端代码（workflow.py/server.py），旧进程事件不带 run_id、节点无 full，需 `/restart`
- 并发安全靠 `_WF_RUNS_LOCK`——所有 `_WF_RUNS` 读写必须持锁，勿绕过
- 测试场景（`make_workflow_tool(agent=None)`）不注册，保持注册表干净
- 观测页轮询在 run 完成后自动停止，长挂页面无持续请求
- **预算耗尽的表现**：节点预览照常显示但无 📄（不可点击）；强行访问全文路由返回 `[无全文]` 提示——观测功能内存占用有硬上限，不会拖垮进程

## 相关页面

- [工作流引擎与钩子](../architecture/workflow-hooks.md)：execute/run_hook/make_workflow_tool 接入点、run registry 摘要与全文预算
- [用户交互](user-interaction.md)：并行钩子「执行中」行（可点击打开观测页 + 实时秒表/总耗时，实现主记录在此）
- [工作流调试页 · 节点输出白框](workflow-debug.md)：画布内联节点输出——「自己调试盯着改」与本页「旁观跑的过程」互补
- [运维可观测性](../guides/ops.md)：/stats 事后统计（与本页实时观测互补）
- [wiki_auto_maintenance](wiki-auto-maintenance.md) / [wiki_auto_query](wiki-auto-query.md)：典型受益者——钩子推理长、LLM 节点输出大，以前完全盲盒
