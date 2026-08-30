# 用户交互 · 插话机制与消息路由

> src/agent.py（消息队列：inbox + pending_messages 双队列）+ src/static/index.html（UI）。涵盖插话（中途打断）、后台触发与通知 wake 语义（2026-08 v0.19.2 修复）、并行钩子 UI 修复（2026-08-19 实测修复，commit fb115aa）、执行中行可点击观测（2026-08-20，commit 8aeb21a）、执行中实时计时与总耗时（2026-08-20，commit 6aa5903）。

## 职责

- **插话**：用户在 Agent 思考/生成 answer 期间发送消息，赶得上步边界则当步注入（`message_injected`），赶不上则暂存 `pending_messages`，待 answer 完成后自动开新轮（`background_trigger`·`user_insert`）
- **后台触发**：answer 完成后检查 `inbox`（后台队列）+ `pending_messages`（插话队列）**双队列**，有消息则自动触发新一轮处理（无需用户手动发送）
- **后台通知 wake 语义**：默认**不独立唤醒轮**——service_exit 等并入下一次自然轮处理（v0.19.2 修复）；2026-08-30 起按服务策略化——`start_service(on_exit_wake=...)` 启动参数声明 never/crash/always，crash/always 可主动唤醒；同日另一族：run_python/run_shell **超时转后台任务完成时恒唤醒通知**（一次性任务无套娃）（见下节）
- **user 消息语义标签**（2026-08-30，用户提案）：inbox 唤醒轮与真用户消息**渲染分流**——user 事件带 `source` 标签 → 系统通知气泡（默认折叠，图标按来源 📪📨⏰🤝）；无标签 → 蓝色 user 气泡；历史轮以 `[后台通知·` 文本前缀判别、混合批按**批首归属**定轮（commit 803b3a5，见下文专节）
- **并行钩子 UI 状态**：同 hook 位置的多个工作流收进**组折叠头**（`▸ [每轮开始前]钩子 ×2 (1/2) ⏳ 12s`，默认收起点击展开，commit 4455503）；组头带计数 + 组级秒表，行内保留观测页跳转/完成态
- **重启恢复广播**：/restart 看门狗重启后自动 /resume 并广播完整视图态（session_history + team_list + pending spec），早连页签/手机端重连立即渲染，不再多开浏览器 tab（commit 7ca6cfc，见下文专节）

## 多客户端 target 路由 · 页签级 Agent 隔离（2026-08，commit 30ac45b）

> src/server.py。此前事件广播是**全端广播**——每个 WS 客户端都收到所有事件，多页签同时与不同 Agent 交互会互相串台。本改动引入**客户端级交互目标 `target`**：每个客户端只收自己正在交互的 Agent 的事件、只把自己的文本路由给该 Agent。

**数据模型**：`_clients` 每项从 `{ws, queue}` 扩为 `{ws, queue, target}`——`target`=该客户端正在交互的 agent_id（默认 `_main_`）。`_event_log` 事件缓冲不变（500 条上限）。

**广播过滤**（`_broadcast`）：事件带 `agent_id`（`Agent._emit` 自动打标：主=`_main_`、子 Agent=各自 id，见 [多 Agent 体系 · 事件流打标](../architecture/multi-agent.md)）→ 只发给 `target` 匹配的客户端；无 `agent_id`（系统级：sessions/workflows/config/wf_debug/命令回显）→ 广播全部。无 WS 客户端（纯 CLI / 服务未起）时直接 return——零开销且 `_main_loop` 未就绪时不因 `call_soon_threadsafe` 报错。

**文本路由**（`_handle_user_input`）：客户端切换到子 Agent（target ≠ `_main_`）后，非 `/` 开头的文本**直达该子 Agent**（对齐 CLI `/agent` 切换后的直连语义）；多页签互不影响——其它页签仍走主 Agent work_q。目标失效（进程重启后 registry 重建）→ 自动复位 `_main_` + 提示，消息转主 Agent 不丢。

**会话视图隔离**：
- `current_history` / `expand_history`：按客户端 `target` 取对应 session——页签 A 在子 Agent 视图展开历史时不会拿到主 session 的轮次；重连/刷新后前端 sessionStorage 记住的 target 先校验存在性；`running` 目标曾退回 `_main_`「防卡忙实例」——**2026-08-30（commit d69bd8e）起放行**，busy 页面恰是观测价值最大的时刻（见下节 URL 路由的 busy 放行小节）
- `load_session` 广播历史：带 `agent_id="_main_"`（`_broadcast_history`）——其它页签正与子 Agent 交互时不被主 session 历史冲掉视图

**answer 特例**：同步工具型子 Agent（explore_subagent / update_wiki）的回应**额外放行给主视图**——主 Agent 正在等其工具结果（保住 answer 分页，见 [气泡交互](../features/bubble-interaction.md#answer-多-agent-分页indexhtml--agentpy2026-08-21)）；反向：子 Agent 视图不收主 Agent 的 answer。

| 场景 | 行为 |
|---|---|
| 页签 A（主）+ 页签 B（coder_1）同时在线 | 主 Agent 事件只到 A，coder_1 事件只到 B，互不串台 |
| B 切换 Agent | 只改 B 的 target + 响应单发（A 视图不动）；sessionStorage 记住，刷新/重连自动恢复；busy 实例也可切（d69bd8e 起，提示排队注入） |
| B 向 coder_1 发消息 | 忙时走 coder_1 插话队列；空闲时 task 进 work_q 与主 Agent run 串行，交互期临时接通事件流 |
| B 的目标失效 | 复位主 Agent + 提示，消息转主 Agent |

**调试插曲**：① 前端 JS 误用 Python 风格 `#` 注释会炸掉整个 script 块——node --check 抓出改 `//`（py_auto_diag 只查 .py 看不到）；② 测试 stub 用 `[]` 冒充 queue → `.put_nowait` 抛 AttributeError 被 `_broadcast` 的 `except` 吞 → 事件全丢、测试假失败，换真 `queue.Queue` 后 6 场景全绿。

## Agent 专属页 URL 路由 · /agents/&lt;agent_id&gt; 直接落位（2026-08，commit 5393ee4；修复 c819618）

> src/server.py（路由）+ src/static/index.html（URL 解析与同步）。同一服务多个 Agent 各有一个专属对话页——URL 直接编码交互目标：`/agents/_main_` 主 Agent、`/agents/wiki-updater_3` 各子 Agent；裸 `/` 默认主 Agent。**刷新/分享/收藏自动落在对应视图**，不再只靠 sessionStorage（它记不住跨页签/新设备）。

**路由形态**（与声明管理页按路径形态区分，互不冲突）：

| 路径 | 视图 |
|------|------|
| `/` | 主 Agent 对话页（默认） |
| `/agents` | 声明管理页（agents.html，无 id；见 [Agent 管理页](agents-admin.md)） |
| `/agents/<agent_id>` | **Agent 专属对话页**——同一 index.html（`_INDEX_HTML`），前端读 URL 初始化交互目标 |

**三个衔接点**（server.py + index.html）：

1. **加载落位**：`connectWS` 解析 `location.pathname` 匹配 `^/agents/([^/]+)/?$` → `_main_` 清 sessionStorage、其它 id 写入 `agt_target`——**URL 优先级高于 sessionStorage 残留**，再走既有 target 恢复链路（校验存在性、失效复位 `_main_` 兜底）
2. **切换同步**：agentSel change 里 `history.replaceState` 同步 URL——主 Agent 回 `/`、子 Agent 到 `/agents/<encodeURIComponent(id)>`（replaceState 不产生历史记录噪声）；页面内切换后刷新/分享/收藏都保持该视图
3. **多页签独立**：与客户端级 target 路由（上节）自然衔接——每个页签的 URL 各自带自己的目标，互不串台

**坑与修复（commit c819618，用户实测两现象）**：

- **静态资源 404**：index.html 内 6 处引用原为相对路径（`icons/favicon.ico`、`manifest.json`）——子路径下解析成 `/agents/icons/...` 全 404（manifest 404 返回 HTML 错误页 → 报 "Syntax error"）。全部改根相对 `/icons/...`、`/manifest.json`。**教训：子路径路由页面里的资源引用一律根相对**。
- **URL 直达被弹回主视图**：`current_history` 对**历史子 Agent**（重启后磁盘恢复条目，`e0.agent is None`）走"失效"分支 → 复位 `_main_` + 返回主历史 → 打开 `/agents/wiki-updater_3` 看到的却是主 Agent 页面。而 `switch_agent`（下拉切换路径）对同款条目有磁盘加载分支（`Session.load(agents/<id>/meta.json)`）——补齐 current_history 的 `agent=None` 分支同款磁盘加载，两条路径行为一致。**连带症状**："切换后 URL 不变"——复位发生后前端 myTarget 仍记着子 Agent、下拉值不变 → change 事件不触发 → replaceState 不执行；视图真实落位后链路自然恢复。

**生效方式**：引擎层（server.py）需 `/restart`；index.html 随服务启动载入内存，重启一并生效。


### busy 实例页面放行：running 目标不再弹回主视图（2026-08-30，commit d69bd8e）

**用户报告**：浏览器打开正在跑任务（busy，`status == "running"`）的子 Agent 专属页 `/agents/wiki-updater_3`，看到的却是**主 Agent 的会话上下文**。

**根因——「防卡忙实例」旧防御**（上上节 target 路由改造 commit 30ac45b 时引入）：

```python
# current_history 的存在性校验（修复前）：
if e0 is not None and e0.status != "running":   # ← busy 目标被拒
    client["target"] = rt
    agent._active_target = rt
else:
    rt = ""                                       # → 复位 → 返回主 Agent 历史
```

- `current_history`（URL 直达/刷新路径）：busy 目标被拒 → `rt=""` 复位 → 走主历史分支——现象即此
- `switch_agent`（下拉切换路径）同款拒绝：`⏳ 'xxx' 正在执行任务，完成后才能切换直接交互`

防御本意是「重连时别卡在忙实例视图上」，但误伤了正当需求：**busy 实例的页面恰恰是观测价值最大的时刻**（看它正在跑什么）。

**修复**（src/server.py 两处，commit d69bd8e）：

| 路径 | 行为 |
|---|---|
| current_history（URL 直达/刷新） | running 也允许设为 target——busy 页面：历史正常浏览 + 事件流按 agent_id 分发**实时可见**（它跑的每一步 thinking/step 都推给该页签） |
| switch_agent（下拉切换） | running 允许切换，提示带排队说明：`✅ 已切换到与 'xxx' 直接交互（正在执行任务：消息将排队注入其当前轮）` |

安全语义不变：向 busy 实例发文本走**插话队列**（`_handle_user_input` 既有路径，注入其当前轮），**不会并发 run**。

与上节 c819618 修复对照：同是「URL 直达被弹回主视图」，彼次根因是历史子 Agent `agent is None`，本次是 busy 防御——两条都已闭环。**生效方式**：引擎层（src/server.py），需 `/restart`。

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

## /restart 重启双坑：电脑无端多开 tab + 早连页签空白（2026-08，commit 7ca6cfc）

> 用户报告（手机 `/restart` 场景）：① 电脑端每次无端多开一个浏览器 tab；② 新开 tab 显示「(当前对话) · Agt」，需手动刷新才见 session。两个现象是**同一条时序链上的两个 bug**（src/chat.py + src/server.py，commit 7ca6cfc）。

**根因链**：

```
手机 /restart → 看门狗拉新进程 web_main：
  ① start_server → open_browser        ← 无条件开浏览器——手机触发的重启，电脑端无端多开 tab（问题1）
  ② 新 tab 秒连 WS → current_history   ← 此刻 _recover_restart_env 还没跑
  ③ _recover_restart_env → /resume → Session.load（大 session 重放数千 events，
     秒级~十秒级——慢于页面连接）
  ④ resume 完成后无任何推送            ← 早连页签拿到 ③ 之前的空 session，永远没人
                                          告诉它「已恢复」→ 一直 (当前对话) 直到手动刷新（问题2）
```

**修复**：

| 问题 | 修复 |
|---|---|
| 多开页签 | `web_main` 的 `open_browser` 前检测 `AGT_RESTART_SESSION` / `AGT_RESTART_MESSAGE` env——重启场景跳过（用户已有页签靠 WS 自动重连）；正常 `agt-web` 启动照旧开浏览器。检测窗口成立的原因：env 要到 `_recover_restart_env` 才 pop，此处仍在 |
| 空白直到刷新 | `_recover_restart_env` 的 `/resume` 成功后调 `broadcast_session_state`（新公共函数）——早连的页签 / 重连的手机端收到推送立即渲染，页面标题随推送从「(当前对话)」更新为会话名 |

**broadcast_session_state**（`src/server.py` 新公共函数）：广播完整视图态——session_history（经 `_broadcast_history` 带 `agent_id="_main_"`，按 target 分发：与子 Agent 交互的页签视图不被冲掉，见 [target 路由](#多客户端-target-路由--页签级-agent-隔离2026-08-commit-30ac45b)）+ team_list（全端广播，agent 下拉刷新）+ pending spec。从 `load_session` 的 `_sync_loaded` 闭包提取为公共函数——**`/resume` 会话切换与重启恢复两条路径共用同一广播**（此前恢复路径完全没有这一步，正是问题 2 的根因）；`load_session` 侧改为直接调用。

**生效方式**：引擎层（chat.py / server.py），需 `/restart`。收益双向——电脑端不再多开 tab；手机端自己的 tab 重连后也不再需要手动刷新即见恢复的会话。

**验证**：mock 全链路——编译 ×2、三处消费点（load_session 复用 / chat 恢复路径）、open_browser 跳过逻辑、广播 target 分发（session_history 按 agent_id / team_list 全端）+ 无客户端 no-op。

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

### 唤醒策略化：on_exit_wake 启动参数 + crash 五分钟退避（2026-08-30，commit eb9a7de）

> src/background_tools.py（工具签名 + docstring 选择指引）+ src/background.py（entry 存储）+ src/agent.py（策略判定 + 退避表）。v0.19.2 一刀切「通知一律不唤醒」根治了套娃，但也把「常驻关键服务崩了没人管」一起埋了——本改动把唤醒权还给**启动时的每服务声明**（用户设计：调用方最清楚这个服务重不重要，比全局按 rc 硬编码精确）。

**三策略**（`start_service(name, command, cwd, on_exit_wake="never")`）：

| on_exit_wake | 语义 | 适用 |
|---|---|---|
| `never`（默认） | 任何退出仅登记 `_notices`，并入下次自然轮 | 一次性验证服务——行为与 v0.19.2 完全一致（防套娃基线不动） |
| `crash` | rc≠0 → `push_message(wake=True)` 唤醒一轮处理；**同名 5 分钟内第二次异常降级为登记**（退避） | 常驻关键服务——既保住「服务死了要人管」，又封死 通知→重启→又崩→又通知 循环 |
| `always` | 任何退出都唤醒（含 rc=0） | 单次任务型服务跑完即报 |

**退避细节**（`src/agent.py`）：rc≠0 唤醒时记 `self._crash_wake_ts[name]`（`{name: last_wake_ts}` 退避表）；同名 5 分钟内第二次异常 → 降级登记；**rc==0 正常退出清退避状态**——服务活过一次，重新计崩窗（连续崩→修好跑通→再崩，仍会唤醒）。

**数据链**（策略从启动参数来，随 entry 走）：

```
启动：start_service(..., on_exit_wake="crash")          # background_tools.py：工具参数 +1
      → svc.start(name, command, cwd, on_exit_wake)     # background.py：start() 签名 +1 参数
      → entry["on_exit_wake"]                            # 存进 self._services[name]，退出时原样带回
退出：_on_service_exit(name, entry, rc)                  # agent.py
      pol = entry.get("on_exit_wake", "never")           # 旧 entry 无字段缺省 never（向后兼容）
      pol=="always" 或（pol=="crash" 且 rc!=0 且不在退避窗）→ wake=True → inbox → 触发一轮
      其余 → wake=False 登记（下次自然轮以 stop_service 合成记录并入，v0.19.2 语义）
```

**验证**：六场景全过——never 崩不唤醒 / crash 首崩唤醒 / 连崩退避 / rc=0 清退避后再崩又唤醒 / always 正常退出也唤醒 / 旧 entry 缺省 never；三文件编译通过。

**生效方式**：引擎层三文件（background.py / background_tools.py / agent.py），需 `/restart`；之后启动 watchdog 型服务自动带 `on_exit_wake="crash"`（docstring 已写选择指引）。

### 后台任务完成自动通知：bg_task 恒唤醒（2026-08-30，commit 6460ad1）

> 用户直觉触发：「同步自动异步转后台的任务一般都需要收到通知」。run_python / run_shell 超时转后台此前完成后**无人通知**——Agent 只能记着 check_bg_task 轮询或干脆忘了。本改动补上完成回调链：跑完那一刻一条 📨 通知推 inbox 唤醒，决策链不中断。commit 6460ad1。

**链路**（real_tools.py / agent.py / chat.py 三文件）：

```
run_python / run_shell 同步等待超时 → 转后台（_bg_tasks 登记 + _bg_reader daemon 读线程继续跑）
  ↓ 返回文案：「完成时会自动推送通知唤醒你（无需轮询）」
_bg_reader：进程退出 → returncode/finished 落表 → _bg_notify_cb(bg_id, name, rc)
  ↓ 模块级钩子（chat.py build_agent 在 _reg(make_background_tools(...)) 后注入）
real_tools.set_bg_notify(agent._on_bg_task_done)
  ↓ agent.py（仿 _on_service_exit 的 seed 模式）
_on_bg_task_done → 包成 check_bg_task 合成工具记录（含尾部输出 40 行）
  → push_message(wake=True) → 闲时立即唤醒一轮 / 忙时 inbox 排队步边界注入
```

**设计要点**：

- **wake=True 恒唤醒、不需策略参数**（与上节 service_exit 对照）：转后台任务本来是**同步等待**（超时被迫转后台），结果通常是决策链一环；且一次性任务跑完即报、**无套娃循环**——service_exit 那边崩溃场景要 5 分钟退避，这边天然安全
- **回调隔离**：cb 抛异常仅记日志，不影响 `_bg_reader` 读线程；未注册（`_bg_notify_cb=None`）静默跳过
- **check_bg_task 不变**：手动查询仍可用（docstring 同步更新）——自动通知即其合成记录（msg 形如「📨〔后台任务完成〕run_python（⚠️ 异常结束 rc=3）」，含尾部输出）

**后台事件通知语义全景（至此三族齐）**：

| 事件族 | 唤醒语义 | 依据 |
|---|---|---|
| service_exit（start_service） | 策略化：never（默认）/ crash（rc≠0 唤醒 + 5min 退避）/ always | 服务重要性由启动方声明；崩溃循环要退避 |
| **bg_task（run_python/run_shell 超时转后台）** | **恒唤醒**（wake=True） | 原同步等待被迫转后台，结果是决策链一环；一次性跑完即报无循环 |
| 定时任务（schedule 原有） | 按 schedule 自身语义 | 既有机制 |

共同原则：每种按「结果是否决策链一环 + 有无循环风险」定唤醒，而非一刀切。

**验证**：链路 mock（回调收到 (bg_id, name, rc) / None 安全）+ 全链路（msg/seed=check_bg_task 合成记录/wake=True 全过）。**生效方式**：引擎层三文件，需 `/restart`。

## user 消息语义标签 · 后台通知轮 vs 用户轮（2026-08-30，用户提案；批首归属 commit 803b3a5）

> 用户提案（2026-08-30）：inbox 唤醒的轮（service_exit / bg_task / schedule / 子 Agent 反馈）此前与真用户消息渲染成**同款蓝色 user 气泡**——「这轮谁在说话」不可辨。语义标签体系把「通知轮 vs 用户轮」的判别落到三条路径、语义一致闭环。涉及 `src/chat.py`（`_merge_batch` 批合并与 first_src）+ `src/agent.py`（`run(_msg_source=)` 事件打标）+ `src/static/index.html`（`renderNotifyBubble` + 历史前缀判别）。

**三条路径**：

```
① 实时（结构化 source）
   worker drain → _merge_batch(batch) → (user_msg, seeds, first_src)
     → agent.run(user_msg, _seeds=…, _msg_source=first_src)
     → user 事件仅 source 非空时带 source 字段（agent.py run() 条件展开）
     → 前端 case 'user' 分流：m.source → renderNotifyBubble（系统通知气泡）
                          无 source → 蓝色 user 气泡（原逻辑 + _pendingLocalEcho 对账不变）
② 历史（前缀判别）
   历史轮 source 未持久化 → renderHistTurn 以文本前缀判别：
   startsWith('[后台通知·') 或 startsWith('[后台触发·') → 系统通知气泡（默认折叠）
   ——与实时 source 分流同语义
③ 混合批边界（批首归属，commit 803b3a5）
   手输 + 后台通知合进同一批（通知唤醒轮 + 用户搭车插话）时：
   first_src 仅当 background 排批首（parts 尚空）才记——
   批首是谁，这轮就是谁的轮
```

**通知气泡形态**（`renderNotifyBubble`，index.html）：`row sys` + 默认折叠一行摘要、点击展开全文——与 autonomous 系统消息同款交互（见 [气泡交互](bubble-interaction.md)）；图标按 source 前缀取：📪 service_exit / 📨 bg_task / ⏰ schedule / 🤝 subagent / 🔔 其它，标题形如「后台通知（bg_task:x1）」。

**修复③的根因**（用户实测抓到的瑕疵）：旧代码 `if not first_src:` 只看「是否已记过」——**手输在先**的混合批（批序 user → background）里，后到的 background 项仍会抢走 first_src → 整轮被渲染成通知气泡，**用户的话被折进通知气泡**。修复加 `and not parts`（批首判别）；搭车的通知不丢——文本自带 `[后台通知·<source>]` 前缀，在气泡内自识别，历史路径②同前缀判别。

**四场景验证**（全过）：

- 纯后台批 → first_src=`bg_task:x1` → 系统通知气泡
- **手输在先混合批**（用户指出的瑕疵）→ first_src=空 → **蓝色 user 气泡**（通知文本带前缀自识别）
- 后台在先混合批（通知唤醒 + 搭车插话）→ first_src=`schedule:z` → 系统通知气泡（这轮确实是通知触发的）
- 纯手输批 → first_src=空 → 蓝色 user 气泡（现状不变）

**与相关机制的关系**：

- **唤醒端 vs 呈现端**：上两节通知 wake 语义（service_exit 策略化 / bg_task 恒唤醒）管「该不该醒」，本节管「醒了长什么样」——通知轮不再伪装成用户轮
- **`[后台通知·` 前缀一键三用**：LLM 侧来源标注（`_merge_batch` 组装时打，让模型识别是哪个调度任务/进程发的）+ 历史渲染判别（路径②）+ 多端同步对账过滤（`_pendingLocalEcho` / `_cli_echo` 跳过通知文本，见[多端消息同步](#多端消息同步--user-事件渲染到同-agent-的其它客户端cli2026-08-commit-1168ea9)）
- **background_trigger 事件行**（📭/⏰ 行）与通知气泡并存——事件行标注触发来源，气泡承载消息全文

**生效方式**：引擎层（chat.py / agent.py）需 `/restart`；index.html 随服务启动载入内存，重启一并生效（Ctrl+F5 强刷兜底）。

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

### 钩子组折叠显示：同 hook 位置收进一个组头（2026-08，commit 4455503）

**用户诉求**：钩子触发时默认折叠显示，形态类似 `before_turn (1/2)`——点击展开看钩子里各工作流的具体执行情况。此前每行独立「执行中」闪烁 + 逐行秒表（commit 6aa5903）在多个钩子并行时刷屏。

**实现**（`src/static/index.html`，纯前端，`auto_wf_start` / `auto_wf` / `auto_wf_error` 三事件处理内）：

```
▸ [每轮开始前]钩子 ×2（0/2）⏳ 3s          ← 运行中：脉冲动画 + 组级秒表（每秒跳动）
▸ [每轮开始前]钩子 ×2（2/2）✅ 共 5s        ← 全部完成：停表定格，仍可点开回看详情
▸ [每轮开始前]钩子 ×2（1/2）⚠️ 共 8s       ← 有失败：黄色定格
  点击展开：
    ⏳ 「wiki_auto_query」执行中…            ← 各工作流行（虚线下划线=可点观测页）
    ✅ 「before_turn_retrieval」完成（2s）：…（点击展开）   ← 长文本行内二级折叠
```

| 点 | 说明 |
|---|---|
| **按 hook 分组** | `window._hookGrp = {hook: {head, box, total, done, failed, t0, timer, upd}}`——同一位置挂 N 个工作流收进一个组头；同轮多个 hook 位置（before_turn/after_tool…）各一组 |
| **组级计时** | 首个 start 起表（t0）、全部 done/failed 停表（`clearInterval`）——一行只有一个跳动的秒数，替代 commit 6aa5903 的逐行 `(Ns)` 秒表（逐行 setInterval 随组折叠移除） |
| **计数动态增长** | `total` 随每个 start 事件递增——并行钩子可能不同时 start（`as_completed` 等待期间有先后），分母实时长大 |
| **默认收起** | `box.style.display='none'`，点组头展开/收起；head 前缀 `▸/▾` 复用 [trace-fold](trace-fold.md) 的折叠约定（内联实现同款 toggle，额外调 `g.upd()` 刷新组头） |
| **行内保留** | 各工作流行：⏳ 执行中（脉冲）→ ✅ 完成（>160 字行内二级折叠）/ ❌ 失败；带 `run_id` 可点击打开观测页（commit 8aeb21a 能力保留） |
| **Map 值扩展** | `_runningWf` 值从 `{el, timer, t0}` 改为 `{el, grp, t0}`——完成/失败时借 `grp` 引用推进组头计数并检查停表 |
| **迟到完成兜底** | 跨轮完成的 async 钩子组已脱 DOM → `addTrace` 独立行（原有行为不变） |

**生效方式**：纯前端（index.html 磁盘 serve），**Ctrl+F5 强刷即生效，无需 /restart**。事件协议零改动——纯渲染层聚合。

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
