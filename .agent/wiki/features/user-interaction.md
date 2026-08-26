# 用户交互 · 插话机制与消息路由

> src/agent.py（消息队列：inbox + pending_messages 双队列）+ src/static/index.html（UI）。涵盖插话（中途打断）、后台触发与通知 wake 语义（2026-08 v0.19.2 修复）、并行钩子 UI 修复（2026-08-19 实测修复，commit fb115aa）、执行中行可点击观测（2026-08-20，commit 8aeb21a）、执行中实时计时与总耗时（2026-08-20，commit 6aa5903）。

## 职责

- **插话**：用户在 Agent 思考/生成 answer 期间发送消息，赶得上步边界则当步注入（`message_injected`），赶不上则暂存 `pending_messages`，待 answer 完成后自动开新轮（`background_trigger`·`user_insert`）
- **后台触发**：answer 完成后检查 `inbox`（后台队列）+ `pending_messages`（插话队列）**双队列**，有消息则自动触发新一轮处理（无需用户手动发送）
- **后台通知 wake 语义**：后台事件通知**不独立唤醒轮**——service_exit 等并入下一次自然轮处理（v0.19.2 修复，见下节）
- **并行钩子 UI 状态**：多个 before_turn 钩子并行执行时，各自独立显示「执行中」状态，互不覆盖；执行中行带每秒跳动的实时秒表 `(Ns)`（commit 6aa5903）、可点击打开实时观测页，完成/失败行定格显示总耗时

## 多客户端 target 路由 · 页签级 Agent 隔离（2026-08，commit 30ac45b）

> src/server.py。此前事件广播是**全端广播**——每个 WS 客户端都收到所有事件，多页签同时与不同 Agent 交互会互相串台。本改动引入**客户端级交互目标 `target`**：每个客户端只收自己正在交互的 Agent 的事件、只把自己的文本路由给该 Agent。

**数据模型**：`_clients` 每项从 `{ws, queue}` 扩为 `{ws, queue, target}`——`target`=该客户端正在交互的 agent_id（默认 `_main_`）。`_event_log` 事件缓冲不变（500 条上限）。

**广播过滤**（`_broadcast`）：事件带 `agent_id`（`Agent._emit` 自动打标：主=`_main_`、子 Agent=各自 id，见 [多 Agent 体系 · 事件流打标](../architecture/multi-agent.md)）→ 只发给 `target` 匹配的客户端；无 `agent_id`（系统级：sessions/workflows/config/wf_debug/命令回显）→ 广播全部。无 WS 客户端（纯 CLI / 服务未起）时直接 return——零开销且 `_main_loop` 未就绪时不因 `call_soon_threadsafe` 报错。

**文本路由**（`_handle_user_input`）：客户端切换到子 Agent（target ≠ `_main_`）后，非 `/` 开头的文本**直达该子 Agent**（对齐 CLI `/agent` 切换后的直连语义）；多页签互不影响——其它页签仍走主 Agent work_q。目标失效（进程重启后 registry 重建）→ 自动复位 `_main_` + 提示，消息转主 Agent 不丢。

**会话视图隔离**：
- `current_history` / `expand_history`：按客户端 `target` 取对应 session——页签 A 在子 Agent 视图展开历史时不会拿到主 session 的轮次；重连/刷新后前端 sessionStorage 记住的 target 先校验存在性，`running` 中的目标退回 `_main_` 防卡在忙实例上
- `load_session` 广播历史：带 `agent_id="_main_"`（`_broadcast_history`）——其它页签正与子 Agent 交互时不被主 session 历史冲掉视图

**answer 特例**：同步工具型子 Agent（explore_subagent / update_wiki）的回应**额外放行给主视图**——主 Agent 正在等其工具结果（保住 answer 分页，见 [气泡交互](../features/bubble-interaction.md#answer-多-agent-分页indexhtml--agentpy2026-08-21)）；反向：子 Agent 视图不收主 Agent 的 answer。

| 场景 | 行为 |
|---|---|
| 页签 A（主）+ 页签 B（coder_1）同时在线 | 主 Agent 事件只到 A，coder_1 事件只到 B，互不串台 |
| B 切换 Agent | 只改 B 的 target + 响应单发（A 视图不动）；sessionStorage 记住，刷新/重连自动恢复 |
| B 向 coder_1 发消息 | 忙时走 coder_1 插话队列；空闲时 task 进 work_q 与主 Agent run 串行，交互期临时接通事件流 |
| B 的目标失效 | 复位主 Agent + 提示，消息转主 Agent |

**调试插曲**：① 前端 JS 误用 Python 风格 `#` 注释会炸掉整个 script 块——node --check 抓出改 `//`（py_auto_diag 只查 .py 看不到）；② 测试 stub 用 `[]` 冒充 queue → `.put_nowait` 抛 AttributeError 被 `_broadcast` 的 `except` 吞 → 事件全丢、测试假失败，换真 `queue.Queue` 后 6 场景全绿。

## 多端消息同步 · user 事件渲染到同 Agent 的其它客户端/CLI（2026-08，commit 1168ea9）

**背景**：一个前端发消息后，另一个正与同一 Agent 交互的前端/CLI 只见回答不见问题——`agent.run()` 的 user 事件（`_emit` 自动带 `agent_id`）早已经 `_broadcast` 按 target 分发，只是两端消费侧都不渲染。本次补齐两端消费，复用既有事件流，**零新增广播**。

**机制**：

```
user 事件 → _broadcast 按客户端 target 分发（原有）
  → 前端 case 'user'：_pendingLocalEcho 对账 → 他端消息渲染 user 气泡 + busy
  → CLI   _render_loop：_cli_echo 对账 → 「🧑 你（来自其它客户端）：…」
```

**两端对账（同款语义，发送端自己不双渲染）**：
- **逐条匹配**：前端 `send()` 记录文本进 `_pendingLocalEcho`（`src/static/index.html`）；CLI 输入 `_record()` 进 `_cli_echo`（`src/chat.py`）——user 事件到达时移除一条，本端乐观渲染过的跳过
- **合并形态**：worker drain 把多条合并成 `"a\n\n---\nb"` 时逐段对账，不误杀
- **过滤**：`[后台通知·]` 跳过（`_merge_batch` 已打 ⏰ 行、`background_trigger` 事件已渲染）；其它 Agent 的 user 事件（如 wiki-updater 批量任务输入）按 agent_id 过滤不渲染
- **附图**：图片 data URL 不随事件走（太大），他端显示「（附带 N 张图片）」计数

**关键改动点**：
- `src/chat.py`：`_render_loop` 新增 `echo_pending` 参数；`_input_thread` 定义内新增 `_cli_echo` 账本，`entry.agent.run(user)` / `work_q.put(("user", user))` 前 `_record(user)`
- `src/static/index.html`：新增 `_pendingLocalEcho`；`send()` 非 busy 时 `addUserBubble` 后 push；`case 'user'` 处理对账 + 他端气泡渲染

| 场景 | 效果 |
|---|---|
| 页签 A 发消息，页签 B 同看主 Agent | B 实时看到蓝色气泡 + 回答过程 |
| 手机发消息，PC 终端（web 模式日志） | 终端显示 `🧑 你（来自其它客户端）：…` |
| CLI 输入（已回显） | `_cli_echo` 对账跳过，不双打印 |
| 页签 B 切到子 Agent X，有人向 X 发消息 | B 看到 X 的 user 气泡（target 路由） |
| stdin 驱动（send_to_service） | web 终端日志显示驱动消息 |

**生效方式**：前端 Ctrl+F5 刷新；CLI 侧 chat.py 为引擎代码需 `/restart`。与上一节（target 路由）共同构成多客户端改造闭环：**事件按 Agent 分发 + user 消息多端可见**。

## 插话全生命周期（2026-08-19 修复闭环，commit fb115aa）

**修复前问题（用户实测报告）**：answer 完成后的自动触发点只查 `inbox`（后台队列），不查 `pending_messages`（插话队列）——**两套队列漏了一半** → answer 期间发的插话滞留在队列里，直到用户手动发下一条消息才被注入消费。

**修复后闭环**：

```
忙时插话 → pending_messages（步边界检查）
  → 赶上步边界：message_injected 当步可见 ✓（原有）
  → 没赶上（answer 生成中）：answer 完成 → pop_inbox 检查 inbox
      → inbox 有消息：background_trigger 开新轮（原有，调度器/服务推送链路）
      → 【新增】inbox 空 → pending_messages 非空 → 立即开下一轮处理插话
        （background_trigger · source=user_insert）
```

**核心改动**（`src/agent.py`，answer/finish_turn 后的消息处理，摘录）：

```python
# 后台推送（调度器/服务）：消费 inbox 触发下一轮
item = self.pop_inbox()
if item:
    src, next_msg, seed = item
    self._emit(...)  # background_trigger 事件（source=src）
    msg, auto_flag, imgs, continue_loop = next_msg, False, None, True
    seeds = [seed] if seed else []   # 下一轮迭代预置该合成 Step
# else 分支（fb115aa 新增，语义）：inbox 空 → 检查 pending_messages，
# 非空则 emit background_trigger(source="user_insert")，取出首条开新一轮
```

**触发后时序**：插话在 answer 完成瞬间自动开新轮，**该轮 before_turn 钩子检索/注入的就是插话内容**——旧代码表现为"下一轮钩子已跑完、用户又发了新消息后，旧插话才姗姗注入"，根因即上述滞留。

**验收观测**：`/restart` 加载新代码后，answer 出现的瞬间 UI 应立即显示 `[后台触发·user_insert]` 并自动开新轮处理插话。

### 实测现象对照（2026-08-19 用户报告 8 条 → 结论）

| # | 现象 | 结论 |
|---|------|------|
| ① | 两个 before_turn 钩子紫色「执行中」并行闪烁 | 设计行为（ThreadPoolExecutor 并发，见 [钩子并行执行](../architecture/workflow-hooks.md)）✓ |
| ② | retrieval 完成后 wiki_auto_query 未完就开始第 1 步 | 旧代码行为；新代码 `as_completed` 等全部钩子完成 ✓ |
| ②b | retrieval 的「执行中」行永远闪烁不消失 | UI bug，已修（本页下节 Map 索引）✓ |
| ③ | wiki_auto_maintenance 与 answer 同时执行 | async 钩子设计行为 ✓ |
| ④ | answer_reasoning 期间插话入队 inbox 等待 | 正常（message_queued）✓ |
| ⑤⑥ | answer 后插话滞留队列、不触发下一轮 | 🔴 核心引擎 bug，已修（本节 pending_messages 兜底） |
| ⑦⑧ | 下条消息发出后旧插话才被注入 | ⑤ 的直接后果，随 ⑤ 闭环 |

## 后台通知 wake 语义：service_exit 不再独立触发轮（2026-08，v0.19.2）

**修复前（套娃循环）**：后台事件通知（如 `service_exit`）各自**独立唤醒一轮**——这轮没有用户消息、只有通知本身，但 before_turn 钩子照常全量跑一遍（空转），answer 也照常生成；若钩子/流程本身又产生后台事件（如 async 钩子完成、后台任务退出），则再次唤醒——「一通知一轮、一轮又一通知」，循环套娃，token 在无人对话时持续燃烧。

**修复后（v0.19.2）**：

| 项 | 新语义 |
|---|---|
| service_exit | **不再独立触发轮**——通知不再有专属唤醒权 |
| 通知消费 | **并入下一次自然轮**（用户消息 / 正常 background_trigger 到来时一并处理） |
| before_turn 钩子 | **不再为纯通知空转**——没有自然轮，就没有钩子执行 |

**收益**：每空转一轮 = 一整套检索钩子（[wiki_auto_query](wiki-auto-query.md) + retrieval）+ answer 生成的完整开销；wake 语义收紧后这类隐性成本根治。

**与上节的区分**：上节（fb115aa）修的是「该触发的没触发」（插话滞留），本节修的是「不该触发的乱触发」（通知套娃）——**唤醒权收敛到自然轮**是两者共同原则。

**生效方式**：引擎层（`src/agent.py`），需 `/restart`。

## 并行钩子「执行中」状态跟踪修复（2026-08-19）

**问题**：两个 before_turn 钩子并行执行时，第二个「执行中」UI 覆盖第一个的引用 → 第一个永远闪烁不消失

**根因**：前端 `runningAutoWf` 为单数变量，`auto_wf_start` 事件处理时 `window._runningWf = rw` 直接覆盖

**修复**（`src/static/index.html`）：Map 按 `hook::name` 索引——并行钩子（before_turn 同时挂两个工作流）时各自的 running 行独立跟踪。Map 值自 commit 6aa5903 起为 `{el, timer, t0}`（见下节计时扩展）：

```javascript
// auto_wf_start：Map 按 hook::name 索引，值为 {el, timer, t0}
(window._runningWf = window._runningWf || new Map())
  .set((m.hook||'')+'::'+m.name, {el: rw, timer: _timer, t0: _t0});

// auto_wf_end / auto_wf_error：clearInterval 停表 + 按 t0 算总耗时 + delete 对应 key
clearInterval(rec.timer);
(window._runningWf || new Map()).delete((m.hook||'')+'::'+m.name);
```

**效果**：每个钩子独立跟踪「执行中」状态，并行执行时各自独立显示、独立移除（含各自独立计时）。

### 执行中实时计时（Ns）+ 完成总耗时（2026-08-20，commit 6aa5903）

紫色闪烁的「执行中」行现在带每秒跳动的秒表，完成/失败时停表定格显示总耗时：

```
执行中（每秒跳动）：⏳ [每轮开始前]钩子工作流「wiki_auto_maintenance」执行中… (15s)
完成（停表定格）：✅ [最终回答前]钩子工作流「wiki_auto_maintenance」完成（共 23s）：…
失败（带耗时）　：❌ [每轮开始前]钩子工作流「wiki_auto_query」失败（8s 后失败）：RateLimitError…
```

实现要点（全部在 `src/static/index.html` 的 `auto_wf_start` / `auto_wf_end` / `auto_wf_error` 事件处理内）：

| 点 | 说明 |
|---|------|
| **本地计时** | `setInterval` 每秒更新 `(Ns)` 后缀——纯前端 `Date.now()-t0`，零后端开销（后端事件只有开始/结束两个时间点，没有过程心跳） |
| **Map 值扩展** | `_runningWf` 从存 `el` 改为 `{el, timer, t0}`——完成/失败时 `clearInterval` + 按 `t0` 算总耗时 |
| **计时器防泄漏** | 行被历史重渲染清掉时 `isConnected` 检测自动停表，孤儿 setInterval 不空转 |
| **迟到完成兜底** | 跨 turn 的完成事件（原 running 行已不在 DOM）走 `addTrace` 路径，同样带总耗时 |
| **并行钩子** | 各自独立计时（Map 按 `hook::name` 索引，上一节修复保证并行独立性） |

**生效方式**：纯前端改动，但 index.html 是服务启动时载入内存的——**Ctrl+F5 强刷即可生效，无需 /restart**；而执行中行的可点击 run_id（下节）依赖后端事件，旧进程仍需 `/restart`。

**验证**：JS 语法 + 5 断言（计时后缀 / 总耗时 / 失败耗时 / clearInterval / isConnected）全过。秒数可与[观测页](wf-monitor.md)的节点级甘特时间线实时对照，时间感完全对齐。

### 执行中行可点击 → 实时观测页（2026-08-20，commit 8aeb21a）

上述「执行中」行在事件携带 `run_id` 时**可点击**（虚线下划线 + pointer），`window.open('/wf/monitor?run='+encodeURIComponent(m.run_id))` 新标签打开观测页，实时查看该工作流的节点时间线甘特图（跑到哪个节点、卡了多久、输出预览）——解决"钩子在跑但完全是盲盒"的观测需求。

`run_id` 由 `src/agent.py` `_run_hooks` 生成（同步线程池 + async 后台线程全覆盖，`auto_wf_start`/`auto_wf`/`auto_wf_error` 事件均携带），注册表与观测页实现见 [工作流运行观测](wf-monitor.md)。旧进程的事件不带 run_id（不可点击），需 `/restart` 生效。

## 前端 UI 遮罩坑：toast 透明条遮挡输入框失焦（2026-08，commit 0a415bc）

**现象**：对话几轮后，WebUI 消息输入框中间靠后的位置被「透明的东西」挡住，点击那里输入框会失去焦点。

**根因**：`toast()`（`src/static/index.html`）惰性创建 `#toast` 提示条——`position:fixed; bottom:20px; left:50%` 居中 + `z-index:999`。第一次调用（发送消息时的「✅ 已接收，处理中…」transient 提示）后元素**永久驻留 DOM**；2 秒后只把 `opacity` 降到 0 淡出，**元素仍在**，且原 cssText **没有 `pointer-events:none`** → 点击落在那个透明 div 上，textarea 拿不到焦点。三个现象全部对上：

① 「几轮后出现」= toast 首次调用才创建，之后一直残留；
② 「中间靠后」= `bottom:20px` 正好落在底部 inputBar（约 66px 高）范围内、`left:50%` 居中盖住中段，宽度随最后一条文案变化（较长文案伸得更远）；
③ 「点击失焦」= 无 `pointer-events:none`，透明元素照样吃点击。

**修复**：`cssText` 追加 `pointer-events:none`——toast 是纯提示元素，本来就不需要交互，显示期间点击也穿透到下层输入框。

**同类坑**：与[气泡级复制按钮](bubble-interaction.md)的 `.bubble-copy` 同款——**`opacity:0` ≠ 不存在，透明元素照样吃点击**（淡出 + `pointer-events:none` 二者缺一不可）。顺带复核其它遮罩物确认安全：specPanel/specFab/modal-overlay 默认 `display:none`、剪贴板兜底 textarea 即用即删。

**生效方式**：index.html 磁盘 serve，**Ctrl+F5 强刷即生效，无需 /restart**。

## before_turn 钩子并行执行保证

见 [工作流引擎与钩子](../architecture/workflow-hooks.md#before_turn-钩子并行执行2026-08-新v0182-发布)：

- **全部完成才返回**：`ThreadPoolExecutor` + `as_completed` 确保所有钩子跑完才进入 ReAct 主循环
- **不会出现「一个钩子未完成就开始第1步」的现象**（用户实测现象为旧代码行为）

## 相关页面

- [工作流引擎与钩子](../architecture/workflow-hooks.md)：before_turn 并行执行 / async 钩子 / 快照检测闭环
- [工作流运行观测](wf-monitor.md)：执行中行点击后的观测页（run registry、节点甘特时间线，与本页秒表计时对照）
- [多 Agent 体系](../architecture/multi-agent.md)：inbox 路由 / 三层消费机制（+ pending_messages 盲区补全）/ 子 Agent 唤醒
- [wiki_auto_query](../features/wiki-auto-query.md)：before_turn 自动检索实例（默认关闭）
- [v0.19.2 发布记录](../releases/v0.19.2.md)：本页 wake 语义修复随该版发布
