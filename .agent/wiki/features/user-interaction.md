# 用户交互 · 插话机制与消息路由

> src/agent.py（消息队列：inbox + pending_messages 双队列）+ src/static/index.html（UI）。涵盖插话（中途打断）、后台触发与通知 wake 语义（2026-08 v0.19.2 修复）、并行钩子 UI 修复（2026-08-19 实测修复，commit fb115aa）、执行中行可点击观测（2026-08-20，commit 8aeb21a）、执行中实时计时与总耗时（2026-08-20，commit 6aa5903）。

## 职责

- **插话**：用户在 Agent 思考/生成 answer 期间发送消息，赶得上步边界则当步注入（`message_injected`），赶不上则暂存 `pending_messages`，待 answer 完成后自动开新轮（`background_trigger`·`user_insert`）；带图插话 2026-10-03 起原生看图——`<img>` 标签按当前模型 vision 门控展开（见 [图片输入链路 · 插话原生看图](image-input.md)）
- **后台触发**：answer 完成后检查 `inbox`（后台队列）+ `pending_messages`（插话队列）**双队列**，有消息则自动触发新一轮处理（无需用户手动发送）
- **后台通知 wake 语义**：默认**不独立唤醒轮**——service_exit 等并入下一次自然轮处理（v0.19.2 修复）；2026-08-30 起按服务策略化——`start_service(on_exit_wake=...)` 启动参数声明（crash/always 可主动唤醒）；**2026-09-14 起（commit 7283f52）非枚举任意文本 = 自定义作业指令 + 无条件唤醒**（误用收编）；**2026-09-23 起默认从 never 翻转为 `notify`——退出通知默认进 inbox**（持久化 + 空闲自动消费成轮 + 忙时步边界排队注入，见下文专节）；同族：run_python/run_shell **超时转后台任务完成时恒唤醒通知**（一次性任务无套娃）（见下节）
- **user 消息语义标签**（2026-08-30，用户提案）：inbox 唤醒轮与真用户消息**渲染分流**——user 事件带 `source` 标签 → 系统通知气泡（默认折叠，图标按来源 📪📨⏰🤝）；无标签 → 蓝色 user 气泡；历史轮以 `[后台通知·` 文本前缀判别、混合批按**批首归属**定轮（commit 803b3a5，见下文专节）
- **并行钩子 UI 状态**：同 hook 位置的多个工作流收进**组折叠头**（`▸ [每轮开始前]钩子 ×2 (1/2) ⏳ 12s`，默认收起点击展开，commit 4455503）；组头带计数 + 组级秒表，行内保留观测页跳转/完成态
- **重启恢复广播**：/restart 看门狗重启后自动 /resume 并广播完整视图态（session_history + team_list + pending spec），早连页签/手机端重连立即渲染，不再多开浏览器 tab（commit 7ca6cfc，见下文专节）
- **连接即推视图态**：新连接（刷新 / 新开页签 / 手机端重连）不再依赖「等下一次事件」——WS 建立即补推 `team_list`（2026-09-17）、`current_turn` 正在进行轮（2026-09-02）、**`plan` 活动计划步骤列表（2026-10-06，commit d573e1e）**；半成品时期各对应一个「刷新后空白」的实锤（见下文各自专节）

## ⏸ 挂起：/hold on|off + WebUI 按钮 + /api/hold 三通道（2026-09-21，用户提案）

> src/agent.py（`_hold`/`_hold_event` + `set_hold()` + step 循环挂起等待）+ src/commands.py（`_cmd_hold`）+ src/server.py（`POST /api/hold` + `/api/status` 加 `hold` 字段）+ src/static/index.html（⏸ 按钮）。用户提案 2026-09-21。语义：`/hold on` 让 react 在【下一步开始前】暂停（当前步照常跑完），`/hold off` 从下一步继续。

**三通道控制**：

| 通道 | 用法 | 说明 |
|---|---|---|
| CLI 命令 | `/hold on` / `/hold off` | 终端交互（命令分发，不在 react 循环内） |
| WebUI 按钮 | 控件栏「⏸ 挂起」→ 点击变「▶ 继续」（primary 高亮） | 快捷控制（`toggleHold`/`paintHold`，后端旧版无 `_hold` 时点击弹 toast 提示需升级） |
| HTTP | `POST /api/hold {on}` | 供脚本/其它端（`api_hold` 端点） |

**核心语义**：

- `on` → `agent._hold = True` + `_hold_event.clear()`——react 循环在**下一步开始前**执行 `while self._hold and not self._stop_flag: self._hold_event.wait(0.5)`（0.5s 轮询，Ctrl+C 仍可打断）
- `off` → `set_hold(False)` → `_hold_event.set()` **即时放行**，从下一步继续；`set_hold(on)` 返回 `{"hold": bool}` 状态
- `_hold_event` 用 `threading.Event`，初始 `set()`=不挂起

**关键设计：off 必须走独立通道（work_q 死锁）**

挂起时 react 阻塞在 `event.wait()`，此时 `work_q` 的消息循环不消费——若把 `/hold off` 也当普通消息塞进 `work_q`，它永远轮不到执行 → **死锁**。所以：

- WebUI 按钮 / `/api/hold` 走**独立 HTTP 端点**，直接 `agent.set_hold(False)` 唤醒，绕开 `work_q`
- CLI 的 `/hold off` 走命令分发（不在 react 循环内）

**四文件改动**：

| 文件 | 改动 |
|---|---|
| src/agent.py | `__init__` 加 `_hold`/`_hold_event`（Event 初始 set）+ `set_hold(on)->dict` + step 循环挂起等待（下一步开始前，0.5s 轮询可 Ctrl+C 打断） |
| src/commands.py | `_cmd_hold`（on/off/1/0/true/false，空串默认挂起 + 当前态提示）+ 注册进 `/help`（`hold` 命令 + 用法说明） |
| src/server.py | `POST /api/hold`（body `{"on": bool}`，agent 未就绪返回 error）+ `/api/status` 返回结构加 `hold` 字段（`bool(getattr(agent, "_hold", False))`） |
| src/static/index.html | 控件栏 `btnHold` 按钮（`toggleHold`/`paintHold`，切「▶ 继续」+ primary 高亮）；后端旧版无 `_hold` 时点击弹 toast 提示需升级 |

**生效方式**：引擎层三文件（agent.py / commands.py / server.py）+ index.html 均随服务进程载入 → 需 `/restart` 后生效。

**验证**：后端编译 3/3 · JS 语法 1/1 · 结构断言 10/10。

**应用场景**：多步任务里想让 Agent 先停一下（观察它下一步要改什么文件、或临时收手）时点 ⏸，它会在下一步开始前停住；想继续再点 ▶。

**关联**：[后台通知 wake 语义](#后台通知-wake-语义service_exit-不再独立触发轮2026-08v0.19.2)（同属用户对运行中 Agent 的控制）、[多客户端 target 路由](#多客户端-target-路由--页签级-agent-隔离2026-08-commit-30ac45b)（/api/hold 是独立 HTTP 通道，不经 WS target 路由）、[api-status](api-status.md)（`/api/status` 新增 `hold` 字段）。

### 子 Agent 挂起支持：/api/hold 增 target 参数——挂哪个 Agent 按交互对象路由（2026-10-07，用户问诊，commit bc3804c）

**用户问诊（2026-10-07）**：「还有我们现在 /hold 的时候对 sub-agent 生效吗？」——答案**分层**：

| 层 | 现状 |
|---|---|
| 闸门机制 | ✅ **天然支持**——子 Agent 是独立 Agent 实例，各自有 `_hold` + `_hold_event`，react step 循环开始前的挂起检查是同一段代码（每实例独立挂起） |
| `/hold` 命令 | ✅ 本日 [模型下拉框 target 感知](#模型下拉框-target-感知model-读写跟随本页签交互对象子-agent-页面不再错切主-agent2026-10-07用户实锤commit-38b3b46)（commit 38b3b46）已**顺带修好**——页签切到子 Agent 后发 `/hold on` 即作用于它 |
| `/api/hold` | ✅ 本轮补上（commit bc3804c）——body 从 `{on}` 扩为 `{on, target}`：`_target_agent({"target": d.get("target")}, _agent)` 解析目标后 `set_hold`；**缺省 = 主 Agent**，指定子 Agent 的 agent_id 则作用于它 |

**用法**：`POST /api/hold {"on": true, "target": "unity-tester"}` 挂起指定子 Agent；脚本/外部通道此前只能挂主 Agent，现在可逐实例控制。

**残留（有意留白）**：顶栏 ⏸ 按钮仍只控制主 Agent——它发 `/api/hold` 不带 target；语义上「顶栏按钮 = 总闸」也说得通。若希望按钮跟随当前页签（挂谁看切到谁），改前端一处即可，待用户裁定。

**生效方式**：引擎层（src/server.py），需 `/restart`。

**关联**：[模型下拉框 target 感知](#模型下拉框-target-感知model-读写跟随本页签交互对象子-agent-页面不再错切主-agent2026-10-07用户实锤commit-38b3b46)（`_target_agent` 路由的又一消费端——/hold 命令是它的顺带受益者，/api/hold 是显式接入）、[多客户端 target 路由](#多客户端-target-路由--页签级-agent-隔离2026-08-commit-30ac45b)（target 语义总纲）。

## 多客户端 target 路由 · 页签级 Agent 隔离（2026-08，commit 30ac45b）

> src/server.py。此前事件广播是**全端广播**——每个 WS 客户端都收到所有事件，多页签同时与不同 Agent 交互会互相串台。本改动引入**客户端级交互目标 `target`**：每个客户端只收自己正在交互的 Agent 的事件、只把自己的文本路由给该 Agent。

**数据模型**：`_clients` 每项从 `{ws, queue}` 扩为 `{ws, queue, target}`——`target`=该客户端正在交互的 agent_id（默认 `_main_`）。`_event_log` 事件缓冲不变（500 条上限）。

**广播过滤**（`_broadcast`）：事件带 `agent_id`（`Agent._emit` 自动打标：主=`_main_`、子 Agent=各自 id，见 [多 Agent 体系 · 事件流打标](../architecture/multi-agent.md)）→ 只发给 `target` 匹配的客户端；无 `agent_id`（系统级：sessions/workflows/config/wf_debug/命令回显）→ 广播全部。无 WS 客户端（纯 CLI / 服务未起）时直接 return——零开销且 `_main_loop` 未就绪时不因 `call_soon_threadsafe` 报错。

**文本路由**（`_handle_user_input`）：客户端切换到子 Agent（target ≠ `_main_`）后，非 `/` 开头的文本**直达该子 Agent**（对齐 CLI `/agent` 切换后的直连语义）；多页签互不影响——其它页签仍走主 Agent work_q。目标失效（进程重启后 registry 重建）→ 自动复位 `_main_` + 提示，消息转主 Agent 不丢。

**会话视图隔离**：
- `current_history` / `expand_history`：按客户端 `target` 取对应 session——页签 A 在子 Agent 视图展开历史时不会拿到主 session 的轮次；重连/刷新后前端 sessionStorage 记住的 target 先校验存在性；`running` 目标曾退回 `_main_`「防卡忙实例」——**2026-08-30（commit d69bd8e）起放行**，busy 页面恰是观测价值最大的时刻（见下节 URL 路由的 busy 放行小节）
- `load_session` 广播历史：带 `agent_id="_main_"`（`_broadcast_history`）——其它页签正与子 Agent 交互时不被主 session 历史冲掉视图

**answer 特例**：同步工具型子 Agent（update_wiki 等仍存；explore_subagent 已于 2026-09-09 删除，见 [spec 工具集](spec-tools.md)）的回应**额外放行给主视图**——主 Agent 正在等其工具结果（保住 answer 分页，见 [气泡交互](../features/bubble-interaction.md#answer-多-agent-分页indexhtml--agentpy2026-08-21)）；反向：子 Agent 视图不收主 Agent 的 answer。

| 场景 | 行为 |
|---|---|
| 页签 A（主）+ 页签 B（coder_1）同时在线 | 主 Agent 事件只到 A，coder_1 事件只到 B，互不串台 |
| B 切换 Agent | 只改 B 的 target + 响应单发（A 视图不动）；sessionStorage 记住，刷新/重连自动恢复；busy 实例也可切（d69bd8e 起，提示排队注入） |
| B 向 coder_1 发消息 | 忙时走 coder_1 插话队列；空闲时 task 进 work_q 与主 Agent run 串行，交互期临时接通事件流 |
| B 的目标失效 | 复位主 Agent + 提示，消息转主 Agent |

**调试插曲**：① 前端 JS 误用 Python 风格 `#` 注释会炸掉整个 script 块——node --check 抓出改 `//`（py_auto_diag 只查 .py 看不到）；② 测试 stub 用 `[]` 冒充 queue → `.put_nowait` 抛 AttributeError 被 `_broadcast` 的 `except` 吞 → 事件全丢、测试假失败，换真 `queue.Queue` 后 6 场景全绿。

## 模型下拉框 target 感知：/model 读写跟随本页签交互对象——子 Agent 页面不再错切主 Agent（2026-10-07，用户实锤，commit 38b3b46）

> src/server.py（`_target_agent` helper + 两处 dispatch 调用点 + `switch_agent` 回推）+ src/static/index.html（`case 'target_model'`）。用户实锤（2026-10-07）：「sub-agent 的 ui 页面里选模型的下拉框看起来读写的还是主 agent 的模型」——页签已切到子 Agent，模型下拉框显示与切换的对象却仍是 `_main_`。

**根因**：WebUI 顶部模型下拉框的切换走 `/model <名>` 斜杠命令，`registry.dispatch` **恒绑定主 Agent**——不管当前页签交互的是谁，切的永远是 `_main_`；读取侧（回显 `current_model`）同理，读的也是主 Agent 的模型。

**修复三件**：

| 改动 | 说明 |
|---|---|
| `_target_agent(client, agent)` 新 helper | 按 `client["target"]`（缺省 `_main_`）解析目标：非 `_main_` 时 `registry.lookup(ct)` 取对应实例（`e.agent`），查不到回退主 Agent。**两处 dispatch 调用点同改**——`/model` 等 agent 绑定命令作用于【本页签正在交互的对象】 |
| `switch_agent` 回推 `target_model` 事件 | 切换成功后 `_send` `{"type":"target_model","target":target_id,"current_model":...}`——前端据此回显下拉框。**注意用【切换后】的 target_id 查目标，而非 client 现值**——避免「从子 Agent 切回主 Agent」时读成旧对象的模型（`_ta = agent if target_id == "_main_" else ...`，lookup 失败再回退主 Agent） |
| 前端 `case 'target_model'` | 下拉框 value + 选中态同步回显（切回 `_main_` 也能正确显示主 Agent 模型） |

**效果**：切到子 Agent 页 → 下拉框显示它的模型 → 切换只作用于它 → 切回主页面恢复主 Agent 模型，互不串台。顺带理清了此前「切到 qwen 回不来」的体验困惑——切换语义归位后，模型归属清晰。

**生效方式**：引擎层（server.py）+ index.html 均随服务进程载入 → 需 `/restart`；site-packages 已同步。

**关联**：[多客户端 target 路由](#多客户端-target-路由--页签级-agent-隔离2026-08-commit-30ac45b)（target 语义的消费端再加一个——此前已有事件广播过滤 / 文本路由 / 会话视图隔离，本次把**命令绑定**也纳入）、[Agent 专属页 URL 路由](#agent-专属页-url-路由--agentsagent_id-直接落位2026-08-commit-5393ee4修复-c819618)（子 Agent 页面的入口，下拉框错位在其上最刺眼）、[配置体系与模型调优](../guides/config-and-models.md)（模型档案与能力位本身在 models.json / settings 配置）。

## WS 斜杠命令回显清洗：系统气泡混入 CLI spinner/ANSI 噪音——redirect_stdout 进程级全局（2026-10-08，用户实锤，commit 9343107）

> src/server.py（`_ANSI_RE` + `_clean_console_noise` helper，紧挨 `_target_agent`；两处 dispatch 调用点同改）。用户实锤（2026-10-08）：busy 时在 WebUI 顶栏切模型，`/model` 回显的系统气泡里混进了 CLI spinner 与 ANSI 转义碎片——`[A[2K`、`⠦ 处理中「[后台通知·service_exit:…] · 2247s · 队列 0（Ctrl+C 停止）」`等控制台噪音原样渲染。

**根因——`redirect_stdout` 是进程级全局**：WS 斜杠命令回显用 `contextlib.redirect_stdout(buf)` 捕获命令输出；这个重定向换的是 **`sys.stdout` 本身（进程级）**，不是线程局部——捕获窗口内**其它线程**的 print 同样落进这一个 buf。busy 时切模型正好撞上：dispatch 打开捕获窗口的瞬间，CLI 轮进度刷新线程也在打 spinner 行（`⠦ 处理中「…」· Ns`，且带 `\x1b[A\x1b[2K` 光标上移/清行转义）——全被收进 buf，随 system 事件渲染进系统气泡。

**修复：回显前统一过一遍 `_clean_console_noise`**：

| 件 | 说明 |
|---|---|
| `_ANSI_RE` | CSI 序列 `\x1b\[[0-9;?]*[A-Za-z]`（`\x1b[2K` 清行 / `\x1b[A` 光标上移等）+ OSC 序列 `\x1b\][^\x07]*\x07`——剥 ANSI 转义 |
| spinner 行剔除 | `⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏` 开头或含「处理中「的行整行丢弃 |
| 两处 dispatch 调用点同改 | `out = _clean_console_noise(buf.getvalue().strip())`——沿袭 `_target_agent` 的「两处同改」纪律（见上节） |
| 顺带收窄另一处行过滤正则 | 字符类去掉 `-` 与 `✅`——防误杀正常列表行与命令回执 |

**实测效果**（用用户贴的原始输出验证）：清洗后只剩命令真回执两行——「✅ 已切换到 glm-official-flash: glm-5.3-flash @ …」+「有效回退链：glm-official-flash → proxy → deepseek」；spinner/ANSI 噪音消失。清洗有意**不动正常回执行**（字符类不含 `✅`/`-` 即此意）。

**生效方式**：引擎层（src/server.py），需 `/restart`；commit `9343107` 已推送，site-packages 已同步。

**关联**：[模型下拉框 target 感知](#模型下拉框-target-感知model-读写跟随本页签交互对象子-agent-页面不再错切主-agent2026-10-07用户实锤commit-38b3b46)（`_target_agent` + 两处 dispatch 调用点的出处——本节清洗正挂在这两处）、[气泡交互 · 系统气泡 markdown 渲染](bubble-interaction.md#系统气泡-markdown-渲染indexhtml2026-08-31commit-fdfc28a)（渲染端；本次治理的是它的内容源）、[后台通知 wake 语义](#后台通知-wake-语义service_exit-不再独立触发轮2026-08v0192)（混进来的 spinner 行内容正是「处理中「[后台通知·service_exit:…]」」）。

## /reset 清空后新会话立即可见 + 会话下拉框跟随当前会话（2026-09-23，用户实锤，commit 3350a68）

**用户实锤（2026-09-23）**：「点清空按钮后发送信息的时候，并没有开一个新的 session」——下拉框还显示旧会话名。

**根因**：`/reset`（清空按钮背后的命令，`_cmd_reset`）构造的新 Session 此刻**无目录、无名**——events/toollog 未绑路径、meta.json 未落盘；而 session 列表是**扫目录**的 → 扫不到新会话；要等**首轮回答完成后** `_ensure_name`（LLM 自动命名）才实体化。期间下拉框显示的还是旧会话。

**修复四处，全链路做实**（commit `3350a68`）：

| 位置 | 改动 |
|---|---|
| `commands._cmd_reset` | set_session 后**立即** `_bind_persistence_paths()` + `save()`——新会话目录即刻创建、events/toollog/llm_calls 就位、meta 落盘（name 兜底 `session_<ts>`，**不写回 `self.name`**——首轮 LLM 自动命名仍会生效改名） |
| `server._sync_new` | 补发 `sessions` 列表广播——新会话**立刻**出现在下拉框 |
| `server._history_event` | 新增 `sid` 字段（session 目录名） |
| `index.html` | `session_history` 存 `_curSid`；sessions 重建后**优先选中当前会话**——reset/resume 均跟随（此前下拉框从不跟随当前会话，顺手治了这个老毛病） |

**效果**（`/restart` 后）：点 🚮 清空 → 会话目录实体创建（列表马上可见）→ 下拉框自动选中新会话（先显示 `session_<ts>`，首轮答完变语义名）→ 发消息 events 直接写进新目录（不再走缓冲）。

**验证**：JS 语法 1/1 · 结构断言 3 项 · py 编译 ×2 全过。

### 泛化：所有切会话命令统一「会话身份检测 → 广播」（2026-10-08，用户提案，commit 0192d33）

本节修的 /reset 可见性如今泛化为通用机制：两处 dispatch（插话兜底 + 主路径）前后对比 `id(agent.session)`，session 实例身份变化（切了会话）即 `broadcast_session_state` + 广播 `sessions` 列表——`/branch`、`/resume`、`/reset` 全覆盖，无需逐命令特判；前端会话下拉同时补了分支复合 id（`主线ts/分支名`）的**后缀匹配**选中。触发场景与完整机制见 [session 分支机制 · WebUI 切会话 UI 自动刷新](session-branching.md#webui-切会话-ui-自动刷新会话身份检测--广播2026-10-08用户提案commit-0192d33)。

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

### 团队看板「🔗 专属页」链接：新页签打开（2026-09-04，commit 59f9140）

**用户请求**：Agent 团队抽屉面板里的子 Agent 点击时最好打开新页签，而不是替换当前页——此前团队看板（`renderTeamDash`，src/static/index.html L1974）每个本机 Agent 条目的「🔗 专属页」是同页跳转，点开子 Agent 专属页就把当前主 Agent 对话视图替换掉了。

**修复**（一处）：链接补 `target="_blank" rel="noopener"`，title 同步标注「（新页签）」。要点：

- **新页签独立落位、原页签不动**：专属页靠 URL 路由（本节开头）初始化交互目标，`sessionStorage` 按页签隔离——新页签自动落位 `/agents/<agent_id>`，**原页签交互目标不变**，可回原页签继续与主 Agent 对话，两边并行观测
- **`rel="noopener"`**：新页签拿不到 `window.opener` 引用，防反向操作原页签
- **对齐既有惯例**：远程实例分组（`d.remotes`）的实例链接（L1998）本就带 `target="_blank" rel="noopener"`，本次把本机 Agent 分组与之对齐；页面顶部 🧩/📚/🧠/🤖/📊 按钮、/wf/monitor 观测页入口同款新页签语义
- **与上节 busy 放行（d69bd8e）配合**：正在跑的子 Agent 也能新页签直达其专属页实时观测 thinking/step，原页签会话不被打断

**验证**（playwright 真页面）：团队看板 6 个链接（coder / vision / vision_10 / vision_11 / wiki-updater_3 专属页 + director 远程实例）全部 `target="_blank"`。纯前端，Ctrl+F5 刷新即生效。

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

## sub-agent 下拉框展开期渲染残缺：team_list 暂存至收起后应用（2026-09-17，commit 696197a，用户报告）

> src/static/index.html（纯前端）。用户报告：WebUI 上 sub-agent 下拉框（agentSel，切换交互目标的控件）弹出的列表"经常只渲染了前一两个，中间的部分没有渲染"，刷新后第一次点击高发、且不必现。

**根因——懒加载 select 与原生弹出层的展开期热替换竞态**：agentSel 是原生 `<select>` 且**懒加载**——页面 HTML 里初始只有 1 项（"🤖 主 Agent"），点击时（mousedown）才 `ws.send({action:'list_team'})` 拉最新团队列表，响应到达后 `sel.innerHTML = opts` 整体重建 options。坏就坏在时序：

- 原生弹出层在 mousedown 那一刻**立刻**按旧 DOM（可能只有 1 项）打开渲染；
- WS 往返几十 ms 后响应到达，此时弹出层**还开着**，innerHTML 热替换；
- Windows Chrome/WebView2 对已展开的原生下拉弹出层做内容热替换渲染有缺陷：弹出窗口项数/尺寸部分更新，中间条目不重绘 → "只画出前一两项、中间空白"。

**为什么刷新后首点高发**：刷新后旧列表只有 1 项，与真实团队（11 项）差异最大，重建冲击最强；第二次点击 DOM 已完整、基本正常。**为什么不必现**：WS 往返耗时（10~100ms）与弹出层首次光栅化是竞态——响应到得晚就侥幸正常，撞上中间态就残缺。

**排除项**：文档内渲染（`size` 属性展开）复现不了；页面 CSS（appearance/clip-path 等）均无关——是 OS 级 popup 窗口的热替换问题，只能从"**展开期不动 options DOM**"入手。

**修复五件套**：

| 改动 | 说明 |
|---|---|
| `mousedown` 打 `_agentSelOpening` 标记 | 仅左键触发（右键不展开弹出层，不误标） |
| `team_list` 响应逢展开期 → 暂存不重建 | 判定 `_agentSelOpening && document.activeElement===sel`；不碰 options |
| `blur`（弹出层收起）后再应用 | `{once:true}`；收起态收到响应行为不变、直接应用 |
| 应用时按当前 `myTarget` 重算选中态 | 展开期用户可能已 change 切换目标（**change 先于 blur**），闭包里请求时的旧值会让下拉框显示跳回旧项 |
| `_agentListLoading` 3s 超时兜底 | 响应丢失时标记复位，防后续点击永久不再拉取 |

**验证**（playwright 真页面 + WS，4 场景全过）：刷新后首次点击（旧列表 1 项）展开期 options 保持 1 项 ✓；收起后 options 变 11 项完整 ✓；收起态收到 team_list 直接应用（行为不变）✓；展开期选中旧项 → change → blur 后选中态跟随新目标 ✓。

**生效方式**：纯前端（index.html 服务端每次请求都从磁盘读），刷新页面即生效，无需 /restart。模型/会话下拉框是页面加载时全量填充的，无此懒加载时序问题；自定义 div 浮层（工具选择器等）不受原生弹出层渲染缺陷影响。

**关联**：[fab-dock · 控件栏](fab-dock.md#控件栏瘦身controls)（agentSel 所在控件栏）、[多客户端 target 路由](#多客户端-target-路由--页签级-agent-隔离2026-08-commit-30ac45b)（下拉切换即改 target）、[Agent 专属页 URL 路由](#agent-专属页-url-路由--agentsagent_id-直接落位2026-08-commit-5393ee4修复-c819618)（change 时 replaceState 同步 URL——"change 先于 blur"在本节修复中同样关键）。

## 团队列表分发：连接即推 + registry 变更推送 team_changed（2026-09-17 · 二，commit 0f322af）

> src/server.py（WS 层）+ src/static/index.html。上节修了展开期热替换的渲染竞态，但列表的**分发时机**仍是纯客户端拉——两个残留缺口：①懒加载下拉"刷新后第一次点击"仍以 1 项旧列表展开（响应要等收起后才应用，首点看到的还是残缺列表）；②子 Agent 增减（create_agent / 注销）后，已打开的页面不手动刷新永远看不到新列表。本改动把团队列表升级为**连接即推 + 变更即推**（registry `on_change` 的第二个消费端）。

**三件**：

| 改动 | 说明 |
|---|---|
| WS 连接建立即推一次 `team_list` | 客户端连上（含刷新重连）服务端主动推完整列表——刷新后 agentSel options 立即完整，懒加载首点不再展开旧列表；既有纯拉路径（手动 `list_team`）保留不变 |
| `start_server` 订阅 registry `on_change` → 广播 `team_changed` | 子 Agent 创建/注销（registry 增减条目）实时广播 `team_changed`——无 `agent_id` 的系统级事件，按 [target 路由](#多客户端-target-路由--页签级-agent-隔离2026-08-commit-30ac45b)语义**全端广播**（所有页签的下拉框都该刷新）。registry `on_change` 的第一个消费端是 [enum 动态注入](../architecture/multi-agent.md)（commit 12d1ff4），本改动是第二个 |
| 前端 `team_changed` → 300ms 去抖重拉 | 收到变更事件不直接改 DOM，去抖 300ms 后重新 `list_team`——连续增减（批量 team_up）合并为一次拉取，且复用既有 team_list 处理链（含上节展开期暂存逻辑：展开期收到变更同样收起后再应用，不与渲染竞态修复冲突） |

**生效方式**：引擎层（src/server.py 常驻进程代码），需 `/restart`。

### 跨仓库补丁交付：patches/ 导出（2026-09-17）

本轮两笔 commit（696197a + 0f322af）经 `git format-patch HEAD~2 -o patches/` 导出为标准补丁文件——`patches/0001-fix-webui-sub-agent-team_list-options.patch` + `patches/0002-feat-webui-registry.patch`（含完整提交信息，目标仓库 `git am` 应用；有本地改动冲突可 `git am -3` 三方合并）。适用场景：**旧版本实例（如 8000）不重装 pip 包、只挑指定 commit 升级 WebUI/引擎局部**——比整包升级轻，比手动复制代码可追溯。应用后引擎侧改动（0002 的 server.py）仍需重启进程生效。

**补丁清单（2026-09-17 · 二轮，累计 4 个——两轮 format-patch 范围不同，编号各自独立）**：

| 补丁 | 内容 | 提交 | 生效方式 |
|---|---|---|---|
| `0001-fix-webui-sub-agent-team_list-options.patch` | 下拉框展开期渲染残缺修复 | 696197a | 刷新页面 |
| `0002-feat-webui-registry.patch` | 团队列表连接即推 + team_changed 推送 | 0f322af | /restart |
| `0001-fix-workflow-extract_keywords-claim-pending-recheck.patch` | extract_keywords 等待循环就绪判定自包含（落 `.agent/workflows/`） | 553d2cd | 热加载即生效（工作流 XML 按需读盘） |
| `0002-chore-workflows-extract_keywords-claim-recap_gen-wik.patch` | 播种源三工作流对齐（落 `src/workflows/`） | 5992929 | 播种/重播种时（已部署环境不自动重播种，靠补丁） |

后两个的修复细节见 [pasted-log · claim 修复与播种源对齐](pasted-log.md)。**编号注意**：`patches/` 里两组 0001/0002 并存（format-patch 按各自范围从 0001 起编号）——`git am` 按文件名逐个应用即可，编号不全局唯一。

## plan 面板刷新恢复：WS 连接即推活动 plan（2026-10-06，用户实锤，commit d573e1e）

> src/server.py（`ws_endpoint` 连接建立后的初始推送区）+ src/static/index.html（`case 'plan'` → `renderPlan(m.plan, m.plan_id, m.plan_title)`，**前端零改动**）。用户实锤（2026-10-06）：「刷新网页以后，web 顶部的 plan 的 step 列表就不显示了」。

**现象**：Agent 有活动计划（顶部 `#planPanel` 步骤列表已有内容）时按 F5 刷新 → 面板变空；要等下一次 plan 变更事件（create_plan / update_plan / add_step / edit_plan / join_plan / exit_plan）才重新出现。

**根因——纯推送制 + 无拉取端点**：plan 面板内容只由 `plan` 事件驱动，来源两处——

| 来源 | 时机 |
|---|---|
| `plan_tools._emit_plan` | 7 个计划工具每次变更时 emit（改 `active_plan` → `_flush` 落盘 → emit） |
| `agent._emit_plan_if_any` | **只在 set_session（resume / 切换会话）路径推一次**——覆盖 /restart 恢复后的同步 |

WS 遇刷新 = **新连接**，两条都不触发 → 前端 `case 'plan'` 永远等不到消息，panel 停在初始空态；且没有 `/api/plan` 之类的拉取口子可退（纯推送制）。**与 2026-09-17 团队下拉框（team_list 只在首次点击时拉）是同一个病**。

**修法（12 行，与旁边 team_list 同款处方并排）**：连接建立后的初始推送区补推当前活动 plan——

```python
_ag = _state.get("agent") or agent
if _ag is not None and getattr(_ag, "active_plan", None):
    await _send(websocket, {"type": "plan",
                            "plan": [dict(s) for s in (_ag.plan or [])],
                            "plan_id": _ag.active_plan_id,
                            "plan_title": (_ag.active_plan or {}).get("title", "")})
```

四处设计点：

- **事件形态与工具变更时逐字段同形**（`plan` / `plan_id` / `plan_title`，同 `_emit_plan`）→ 前端 `case 'plan'` 直接吃，不需要新增分支或事件类型
- **判空即跳过**：无活动计划（`active_plan is None`——exit_plan / /reset 之后）不推 → 刷新后保持空面板，与真实状态一致，不会用陈旧 steps 画出已退出的计划
- **steps 经 `dict(s)` 拷贝**（与 `_emit_plan` 同款）：WS 序列化不共享可变对象，后续工具改 steps 不串改已发快照
- **`_state.get("agent") or agent` + try/except 全包**：兼容两条取 agent 的路径（`_state` 未挂载时退回闭包里的 agent）；推送失败不影响后续连接流程

**顺带治理：team_list 推送块去重**——初始推送区里 2026-09-17 的 team_list 推送**存在两份重复**（历史编辑遗留），edit 匹配时报「匹配 2 处」才暴露；本次以新 plan 块**替换掉重复那份**，现只剩一份（行为不变，少一次冗余 `format_team`）。

**生效方式**：引擎层（src/server.py），需 `/restart`。此后刷新 / 新开页签 / 手机端重连都能立即看到当前计划及各步状态（含 `active_window=False` 全量语义的 team_list 同批）。

**关联**：[团队列表分发 · 连接即推](#团队列表分发连接即推--registry-变更推送-team_changed2026-09-17--二commit-0f322af)（同款修法，本节的直接参照）、[连接补发进行中轮 · current_turn](#连接补发进行中轮--current_turn-事件2026-09-02用户提案)（新连接补发「正在进行态」的另一半：轮）、[/restart 重启双坑](#restart-重启双坑电脑无端多开-tab--早连页签空白2026-08commit-7ca6cfc)（`broadcast_session_state` 补推 session_history + team_list + pending spec——plan 面板当时漏在门外）、[上下文引擎 · 施工模式投影](../architecture/context-engine.md)（同一个 `active_plan` 状态在 LLM 侧投影里的另一副面孔）。

**模式推广（2026-10-08，commit 2ecc6f4）**：`_state.get("agent") or agent` 兜底自此推广到服务看板/定时任务家族**六端点**（`svc_op` / `svc_log` / `svc_fav` / `sched_upd` / `sched_add` / `sched_del`）——20048 实锤「Stop 恒报缺少agent/name」（缺的其实是 agent 不是 name）后统一收编，详见 [background-scheduler · 服务看板四件套后记](background-scheduler.md)。

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

## 连接补发进行中轮 · current_turn 事件（2026-09-02，用户提案）

> src/server.py（`_current_turn_event` + `current_history` 处理器两分支补发）+ src/static/index.html（`case 'current_turn'`）。d69bd8e 放行了 busy 实例页浏览 + 实时事件后仍有一个「现在进行时」缺口：**新连接的客户端（刷新 / 新开页签 / URL 直达 busy 页）只渲染历史轮**——后端正在进行的轮（`session._current`）在历史渲染完的那一刻没有任何呈现，UI 停在上一轮完成态，用户以为卡了/坏了，实际 agent 正在跑长工具（长 read_file / run_python / 后台等待）。

**背景缺口**：`session_history` 事件只带 `to_history()` 的**已完成轮**；`session._current` 是内存态活轮，不落历史。此前 busy 页打开只能看到「上一轮的完成态 + 之后陆续到达的实时事件」，**历史渲染与实时事件之间夹着的正在进行轮完全不可见**——本轮用户提案正是补这段：历史渲染完后把进行中轮**已完成的步骤**也补发出来。

**方案**（用户提案 2026-09-02）：`session_history` 发完后紧接着补发一个 `current_turn` 事件——格式对齐 `to_history()` 的 steps 结构（name/arguments/result/call_id），无 answer（还在跑）。前端渲染完该轮后 `curTurn` 指向它，后续实时事件自然追加，无缝衔接。

**后端**（src/server.py）：

```python
def _current_turn_event(agent):
    """构造 current_turn 事件：正在进行的轮（session._current）——新连接的客户端
    在历史渲染完后补发当前轮已完成的步骤（用户提案 2026-09-02）。格式对齐 to_history()
    的 steps 结构（name/arguments/result/call_id），无 answer（还在跑）。"""
    s = agent.session
    cur = getattr(s, "_current", None)
    if cur is None or not (cur.user_message or "").strip():
        return None          # 空闲 / 无有效 user 消息 → 不补发
    steps = []               # 遍历 cur.steps 的 tool_calls：经 s.toollog.view(tc.call_id)
    ...                      # 取 name/arguments/result（单条异常降级跳过）
```

- **主 Agent 分支**（`current_history` 处理器）：`session_history` 发完 → `cur_ev = _current_turn_event(agent)`，非 None 即补发（agent 空闲 `_current=None` 不发，老路径零开销不变）
- **子 Agent 分支同款**：`cur_ev0["agent_id"] = cur` 后补发——观测**正在跑的子 Agent**（刷新 wiki-updater 等专属页）恰是切换页价值最大的场景

**前端**（src/static/index.html）`case 'current_turn'`：

```javascript
case 'current_turn': {
    addTimeDivider();
    addUserBubble(m.user || '(进行中)', []);
    newTurn();                       // 建 trace + answer 结构
    setBusy(true);                   // UI 显示忙状态
    if (m.agent_id && m.agent_id !== '_main_')
      addTrace('step', `[${m.agent_id}] — 进行中的轮（已完成 ${m.steps.length} 步）—`);
    // 遍历 m.steps：s.reasoning → renderThinking；s.tool_calls → renderToolCall（含 changed）
    // curTurn 指向本轮 → 后续实时事件（新 step / tool_call / finishAnswer）自然追加
}
```

关键设计：**`curTurn` 指向新渲染的轮**——之后的实时广播事件无缝追加到同一 DOM 结构，answer 区留空等 `finishAnswer` 事件来填，**不需要任何特判**；thinking 走既有 renderThinking 默认折叠，与 trace-fold 约定一致。

**顺手清理死代码**：`session_history` case 中原 `in_progress` 摘要渲染块删除——后端从未发过 `in_progress` 字段（遗留幻想代码），本次由真实 `current_turn` 事件取代。

**生效方式**：引擎层（server.py）+ index.html 均服务启动载入内存 → `/restart` 后生效；之后刷新/新开页签，正在跑的轮的 user 气泡与已完成步骤（含思考）直接可见。

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

## busy 误判插话修复：answer 后收尾期秒跟进开新轮（2026-10-03，用户实锤，commit 3e0714a）

**现象**：answer 气泡已经出来（对用户而言本轮已结束），紧接着发的「做」却被判成插话——UI 提示「📥 已排队」，消息滞留插话队列，直到下一轮开新轮才以〔用户中途补充〕降级注入。用户疑问：「结束了我才发的，为啥发的时候判定为 busy？」

**根因——busy 标志的生命周期缺口**：`state["busy"]` 在 `_worker` 从 work_q 取任务时置 True、`agent.run()` **完全返回**后的 finally 才清 False；但 `answer` 事件发出 ≠ run 返回——中间还有**秒级收尾窗口**：

```
answer 事件（气泡出来，用户视角 = 轮已结束）
  → ① wrap_up（finish_turn 轮归档 + autosave 落盘）
  → ② turn_end 钩子（本轮正是 wiki_auto_maintenance 派活了 wiki-updater_9）
  → ③ events flush + _done 事件
  → run() 返回 → finally: busy=False
```

用户在此窗口发消息 → server 查 `busy=True` → 误判插话；而上一轮已无下一步边界可注入 → 滞留成〔用户中途补充〕。

**修复——`answered` 标志三处接线**（commit 3e0714a）：

| 处 | 改动 |
|---|---|
| src/server.py `_broadcast` | 主 Agent 的 `answer`/`wrap_answer` 事件广播时置 `_state["answered"]=True`——**agent_id 过滤**（`aid in ("", "_main_")`），子 Agent 的 answer（wiki-updater 等）不影响主循环判定 |
| src/server.py busy 判定 | `busy and not answered` 才走插话队列——answer 后的快速跟进改走 **work_q 开新轮**（worker 串行，收尾完自动消费，无并发风险） |
| src/chat.py `_worker` | 每轮 run 开始时清 `answered=False` |

**边界语义（三不变）**：

- **answer 前插话**（长任务中改向 / 补充）→ 仍走插话注入 ✓
- **中断轮**（无 answer）→ `answered` 恒 False → 仍可插话 ✓
- **子 Agent 的 answer** → 不影响主循环 ✓

**可观测差异**：修复后 answer 一出来立刻跟进 → 提示「✅ 已接收，处理中…」（开新轮）；旧行为 → 「📥 已排队」（误判插话）。生效需 `/restart`。

**与本页其它章节的关系**：[插话全生命周期](#插话全生命周期2026-08-19-修复闭环commit-fb115aa)（fb115aa）修的是「该触发的没触发」（插话滞留不消费）；本节修的是「不该判插话的被判了插话」——两者共同拼出插话 vs 新轮的完整判定边界：**answer 发出前 = 插话；answer 发出后 = 新轮**。（2026-10-07 又补第三块：前端态漂移导致的插话死信兜底，见下节。）

## 插话死信修复：前端 busy 陈旧 → 空闲态消息走插话通道永不消费（2026-10-07，20048 实锤，commit 1c0d2f9）

**现象**（20048 实例，2026-10-07 用户实锤）：结束一轮后始终 busy，发消息提示「插话已入队」，却始终不开下一轮——消息成了死信。

**根因三层叠加**：

```
① 前端 busy 变量陈旧（WS 断线重连 / 事件丢失，turn_end 后未复位）
② → 用户消息走了前端插话通道（sendInsertMessage → insert_message）
③ → 后端 insert_message 分支【无条件】入 pending_messages——但 agent 实际空闲
    （/api/status busy=false 实测确认），pending 没有「下一步边界」可注入 = 永久死信
```

「插话已入队」却永不开轮的本质：**消息躺在空闲 agent 的 pending 里无人消费**——空闲 worker 只等 work_q、不扫 pending；answer 后的 pending 兜底（[插话全生命周期](#插话全生命周期2026-08-19-修复闭环commit-fb115aa)）只在 run 收尾时查一次，轮结束了就再没人回头看。排障三证据链：后端 `/api/status` busy=false（真态空闲）、events 停在 turn_end（08:11）无后续、py-spy 栈无卡点（worker 空闲等队列）——与 50052 的「MCP hang 拖死 worker」（见 [运维 · 常见错误对照](../guides/ops.md)）是**两种不同的 busy 假死**。

**修复——后端权威真态兜底**（src/server.py insert_message 分支，commit 1c0d2f9，已推送+同步）：

```python
if text:
    # 真态兜底（2026-10-07·20048 实锤）：前端 busy 变量可能陈旧（WS 断线重连/事件丢失），
    # 导致空闲态的消息走了插话通道——入 pending 后无下一轮消费 = 死信（"插话已入队"
    # 却始终不开新轮）。后端权威判定：agent 实际空闲 → 直接转 work_q 开新轮（同正常发送）。
    if not (_state is not None and _state.get("busy") and not _state.get("answered")) and _work_q is not None:
        _work_q.put(("user", text))
        await _send(ws, {"type": "system", "transient": True,
                         "text": "✅ 已接收（agent 空闲，转入新一轮处理）…"})
    else:
        ...  # 真在轮内 → 保持插话语义（pending + 步边界注入）
```

判定复用 3e0714a 的 `answered` 标志：`busy and not answered` = 真在轮内（走插话）；否则视为空闲 → work_q 开新轮（与正常发送同路径）。**前端再怎么陈旧也不会死信**——判定权从前端 busy 变量收归后端真态。

**可观测差异**：空闲态发消息的提示由「插话已入队」变为「✅ 已接收（agent 空闲，转入新一轮处理）…」——看到哪条提示即知走了哪条通道。

**与上节 3e0714a 修复的关系**：两者拼完插话误判的最后一块——3e0714a 修「answer 后收尾窗口被误判插话」（后端 busy 置位过晚），本节修「前端态漂移导致的误判」（前端 busy 复位失败）；共同原则：**插话 vs 新轮的判定只信后端真态，不信前端变量**。

**存量死信救济（不用重启清队）**：修复生效的实例随手发一条新消息（如「继续」）即可全清——新轮 run 的批合并机制会把 pending 里的存量死信一并带出（合并进该轮 user_message），一次全清。

**生效方式**：引擎层（src/server.py），需 `/restart`。

## 插话 target 路由：子 Agent 页面发的"继续"被插话给主 Agent（2026-10-07，用户实锤，commit 42f70e3）

> src/server.py（`insert_message` WS 分支）。用户实锤（2026-10-07）：「我在 sub-agent 页面发送的`继续`被以插话发送给了主 agent」——页签已切到子 Agent，插话却注入了主 Agent 的 pending。

**根因**：`insert_message`（WS 插话通道）此前**恒写主 Agent 的 pending**——`agent.queue_user_message(text)` 里的 `agent` 永远是主 Agent 实例，完全没有 target 感知：

```
子 Agent 页面发「继续」→ 前端 busy 判定 → 插话通道 insert_message
  → agent.queue_user_message(text)   ← 这个 agent 永远是主 Agent
```

至此三条消息通道里两条早有 target 路由：**正常发送**（2026-08 [多客户端 target 路由](#多客户端-target-路由--页签级-agent-隔离2026-08-commit-30ac45b) 改造即有，直达子 Agent work_q）、**斜杠命令**（同日 [模型下拉框 target 感知](#模型下拉框-target-感知model-读写跟随本页签交互对象子-agent-页面不再错切主-agent2026-10-07用户实锤commit-38b3b46)，`_target_agent` 顺带覆盖 /model / /hold 等）——**唯独文本插话一直漏着**，本节补齐。

**修复**（`if text:` 块加 target 解析，commit `42f70e3`）：

```python
if text:
    # target 路由（2026-10-07 用户实锤：子 Agent 页面发的"继续"被插话给了主 Agent）：
    # 插话按本客户端交互对象路由——子 Agent 的 run 同样在步边界消费 pending。
    _tgt = (client or {}).get("target", "_main_")
    _tgt_ag = None
    if _tgt != "_main_":
        _reg2 = getattr(agent, "registry", None)
        _e2 = _reg2.lookup(_tgt) if _reg2 else None
        if _e2 is not None and _e2.agent is not None:
            _tgt_ag = _e2.agent
    if _tgt_ag is not None:
        _tgt_ag.queue_user_message(text)
    # …（后半段主 Agent 原路径不变，含真态兜底）
```

三态语义：

| client.target | 行为 |
|---|---|
| `_main_`（缺省） | 原路径不变——含 [插话死信修复](#插话死信修复前端-busy-陈旧--空闲态消息走插话通道永不消费2026-10-0720048-实锤commit-1c0d2f9) 的真态兜底（空闲转 work_q 开新轮） |
| 子 Agent 且 registry 有实例（`e.agent is not None`） | `_tgt_ag.queue_user_message(text)`——进**它自己的** pending；busy 则步边界消费（`message_injected` 当步可见 / 赶不上则轮后注入），不再旁落主 Agent |
| 子 Agent 但 registry 无实例（重启后磁盘恢复条目 `e.agent is None` / 已注销） | `_tgt_ag=None` → 落回主 Agent 原路径，消息不丢 |

子 Agent **不在跑**时的插话语义：消息排队在它自己的 pending 里，等它下次被派活开轮时**首先消费**——比旧行为（插进主 Agent、污染主上下文且子 Agent 永远看不到）语义正确且不丢。

**三通道 target 路由全景**（至此补齐，页签间互不串台——在子 Agent 页面发的任何消息都只进它的上下文）：

| 通道 | 路由点 | 补齐 |
|---|---|---|
| 正常发送（空闲 / answer 后跟进） | `_handle_user_input` 按目标 work_q | 30ac45b（2026-08 即有） |
| 斜杠命令（/model / /hold 等） | `_target_agent(client, agent)` dispatch | 38b3b46（2026-10-07 早） |
| 文本插话（busy 期间 insert_message） | `if text:` 按 client.target 选实例 | **本轮 42f70e3** |

**与上节（1c0d2f9）的关系**：同通道（insert_message）同日姊妹修复——上节修「判错**时机**」（空闲态消息被当插话 → 死信），本轮修「判错**对象**」（子 Agent 页面的插话插给主 Agent）；上节给出的存量死信救济动作恰是「随手发一条『继续』」——用户照做时踩中本 bug，救济消息插错了门。两条修完后该救济动作才真正点到正确的门。

**生效方式**：引擎层（src/server.py），需 `/restart`；commit `42f70e3` 已推送 + site-packages 已同步（20048 实例 restart 后生效）。

**关联**：[多客户端 target 路由](#多客户端-target-路由--页签级-agent-隔离2026-08-commit-30ac45b)（target 语义总纲——本节把最后一条通道纳入）、[模型下拉框 target 感知](#模型下拉框-target-感知model-读写跟随本页签交互对象子-agent-页面不再错切主-agent2026-10-07用户实锤commit-38b3b46)（命令侧姊妹修复，`_target_agent` 模式参照）、[插话死信修复](#插话死信修复前端-busy-陈旧--空闲态消息走插话通道永不消费2026-10-0720048-实锤commit-1c0d2f9)（同通道时机侧姊妹修复）、[多 Agent 体系](../architecture/multi-agent.md)（`registry.lookup` 与 Agent 实例挂载——`e.agent is None` 的磁盘恢复条目形态）。

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

### 误用收编：非枚举文本 = 自定义作业指令 + 无条件唤醒（2026-09-14，commit 7283f52，用户裁定·8000 实例实测）

> src/agent.py（`_on_service_exit` 策略判定 + 通知三处注入）+ src/background_tools.py（`start_service` docstring）。**起因**：8000 实例（comfy repo）启动 mimg5_chain 镜像构建服务时，把 `on_exit_wake` 当「服务退出时的返回提示」填了一大段自然语言指令——「mimg5_chain（第5轮镜像构建·CPU远端手脚）退出——查 mimg5_result.txt 终局（PUSH_RC=0=成品/失败）+ r5 阶段，报用户……先报结果等指示」。旧实现：非枚举串**静默丢弃、按 never 处理**——调用方的意图整个落空。

**用户裁定**：「如果 llm 传了这样的东西，就按这个返回并唤醒吧」——这种"误用"其实是好模式（启动时就把「退出后该干嘛」写好，醒来直接照作业执行），收编为特性。

**四分支语义**（在 eb9a7de 三分支基础上扩一；上节全景表 service_exit 行自此为四分支）：

| on_exit_wake | 行为 |
|---|---|
| `never`（默认）/ 空串 | 任何退出仅登记，并入下次自然轮（不变） |
| `crash` | rc≠0 唤醒 + 5 分钟同名退避（不变） |
| `always` | 任何退出都唤醒（大小写归一，`ALWAYS` 等价；不变） |
| **其它非空任意文本** | **= 自定义作业指令：无条件唤醒（always 语义、无退避）+ 提示原文注入通知** |

**通知形态**（醒来时看到）：header `📨〔后台服务退出〕「mimg5_chain」已自行退出（rc=0（正常退出））·附启动指令`；合成 stop_service 记录**三处注入**：

1. **result 尾部**：「—— 启动时你留下的指令（on_exit_wake）——」+ 指令原文
2. **reasoning**：「（你启动该服务时留的指令：…）」
3. **header** 标记 `·附启动指令`

Agent 醒来直接看到自己启动时留的作业——按指令查文件、报用户，决策闭环不断。

**配套**：`start_service` docstring 补自定义指令语义说明——参数描述即提示词，引导后续 LLM 正确使用该用法（与 [run-python · 参数决策指引](run-python.md) 同款思路）。

**验证**（9/9）：8000 真实场景四断言（唤醒 / header 标记 / result 注入 / reasoning 注入）+ 枚举全回归（never / crash+rc0 不唤醒 / crash+rc1 首次唤醒 / ALWAYS 大写 / 空串）。

**生效方式**：引擎层（agent.py / background_tools.py），需 `/restart`。commit `7283f52` 已推送，随下个版本发布；8000 实例 `pip install -U agt-agent` 后生效。

### 默认翻转：never→notify——退出通知默认进 inbox + on_exit_style 注入姿势（2026-09-23，用户提案，commit 275049c）

**动机**：旧默认 `never` 的「并入下次自然轮」有可见性盲区——通知只进内存 `_notices`（不持久化、不触发轮），**没有自然轮就永远看不到**（agent_watch 退出通知深夜躺一整夜、次日才发现即此症状）。用户裁定翻转：**默认 `notify`，通知进 inbox**——`inbox.jsonl` 持久化（/restart 不丢）+ Agent 空闲时自动消费成轮（保证可见）+ 忙时排队到下一步边界注入（不打断进行中的轮）。

| on_exit_wake（翻转后全表） | 行为 |
|---|---|
| `notify`（**新默认**） | 进 inbox（持久化 + 空闲自动消费 + 忙时步边界排队） |
| `never`（旧默认） | 仅内存登记（最安静；无自然轮则不可见）——语义不变，按需显式声明 |
| `crash` | rc≠0 才进 inbox + 5 分钟同名连续崩溃退避（常驻关键服务） |
| `always` | notify 同义别名（历史枚举保留） |
| 非枚举任意文本 | 自定义作业指令：退出即通知 + 指令原文注入（2026-09-14 收编语义不变） |

存量服务 entry 已存显式值按启动时约定走，不受翻转影响（仅缺省值 never→notify）。

**新参数 `on_exit_style`（注入姿势，按服务实例）**：`tool`（默认）= 合成 `stop_service` 工具记录（启动参数 + 退出码 + 尾部日志，信息最全）；`text` = 纯文本通知（轻量，仅 header + 简要命令，不落工具记录）。签名：`start_service(name, command, cwd, on_exit_wake="notify", on_exit_style="tool")`——`start_service` docstring 同步更新（参数描述即提示词）。

**验证**（mock Agent 策略矩阵六项全过）：①默认=notify（wake + seed 工具记录）✓ ②never 登记不唤醒 ✓ ③crash rc0 静默 / rc1 唤醒 / 二连崩退避 ✓ ④always / 自定义文本 ✓ ⑤style=text 纯文本 vs tool 合成记录 ✓ ⑥ServiceManager 签名 ✓。引擎层三文件（background.py / agent.py / background_tools.py），`/restart` 生效——机制细节与枚举演进史见 [background-scheduler · 退出通知默认进 inbox](background-scheduler.md#退出通知默认进-inboxnevernotify-翻转--on_exit_style-注入姿势2026-09-23用户提案commit-275049c)。

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
- **合成记录键名契约**（2026-09-11 修复，v0.26.6）：seed 四键 `{tool, args, result, reasoning}`——`tool` 是消费侧 `_seed_steps` 读的键，写成 `name` 则工具名恒空、通知轮退化成纯 user 通知（本族曾踩，见 [键名漂移修复](#键名漂移修复bg_task-合成记录-name--tool2026-09-11用户观察触发)）

**后台事件通知语义全景（至此三族齐）**：

| 事件族 | 唤醒语义 | 依据 |
|---|---|---|
| service_exit（start_service） | 策略化：never（默认）/ crash（rc≠0 唤醒 + 5min 退避）/ always | 服务重要性由启动方声明；崩溃循环要退避 |
| **bg_task（run_python/run_shell 超时转后台）** | **恒唤醒**（wake=True） | 原同步等待被迫转后台，结果是决策链一环；一次性跑完即报无循环 |
| 定时任务（schedule 原有） | 按 schedule 自身语义 | 既有机制 |

共同原则：每种按「结果是否决策链一环 + 有无循环风险」定唤醒，而非一刀切。

**验证**：链路 mock（回调收到 (bg_id, name, rc) / None 安全）+ 全链路（msg/seed=check_bg_task 合成记录/wake=True 全过）。**生效方式**：引擎层三文件，需 `/restart`。**后续修正**：seed list 包装（44ae953，2026-08-31）+ 键名漂移（2026-09-11，v0.26.6）两处产地缺陷，见 [seed 契约三层防御](#seed-契约三层防御非-dict-坏-seed-不再崩唤醒轮2026-08-31bg_task-唤醒轮秒崩修复) 及其后两节。

### 服务退出双渲染辨析（2026-09-28）：实时事件流 × inbox——UI 两遍、Agent 一遍（处置待裁定）

用户观察（2026-09-28）：服务退出消息在轮进行过程中已经消费过一次，轮结束后又从 inbox 出队再消费一次。辨析结论：**不是 Agent 双消费**，是两条通道各自渲染一次——

| 通道 | 发生时机 | 谁看到 |
|---|---|---|
| 实时事件流（语义标签紫色折叠气泡） | 退出瞬间 | 只有用户（UI）——Agent 上下文里没有 |
| inbox 队列（`on_exit_wake=notify` 默认，见 [background-scheduler](background-scheduler.md)） | 下一轮开头 | Agent 唯一的一次消费（seed 以 stop_service 工具记录并入上下文） |

Agent 只消费一次（inbox 那次）；UI 上两遍是「事件发生时的实时提示」与「下一轮的轮记录」都渲染了。消 UI 重复两条路：

- **A. 前端历史轮渲染去重**：同 source 的系统通知若实时气泡已存在则跳过——保实时性，改动在渲染层
- **B. 退出事件不再实时推 UI**：只在 inbox 消费轮渲染——实时性丢，但实时流仍在 🐞 日志面板

**待用户裁定**；维持现状亦可——两条各表达「发生」与「被处理」两个语义，读档回看时间线是准确的。

## seed 契约三层防御：非 dict 坏 seed 不再崩唤醒轮（2026-08-31，bg_task 唤醒轮秒崩修复）

> src/agent.py（push_message / _drain_notices / _seed_steps 三处）。bg_task 唤醒轮**触发了但秒崩**的根因修复——用户报告「卡在这里不动了，下一轮没触发」，实测是**触发了但秒崩**（旧代码异常中断不可见 → 看起来像纯卡住）。

**复现链（11:22 实验）**：timeout 180→20s + run_python sleep(30) → 转后台 → 本轮 turn_end → 10 秒后后台完成 → push(wake=True) → 唤醒轮确实触发（turn_start 写入 events）→ run() 顶部消费 inbox → `AttributeError: 'list' object has no attribute 'get'` → 轮秒崩（「中断，本轮未完成」）。**不是没触发，是触发了但秒崩**。

**根因链**（雷是早前自己埋的）：凌晨 05:51 VideoGameTeam 后台任务的 team_create 通知 push 时 **seed 传了 list**（违反 dict 契约）→ 在 inbox 躺了 5 小时 → bg_task 唤醒轮把它和通知一起 pop → `_seed_steps` 的 `sd.get("tool")` 对 list 调 `.get` → AttributeError → 轮秒崩。8000 实例同款根因（旧代码连中断标记都不写，看起来纯卡住；其 llm_calls 09:51 的 react 是前一轮尾巴/切换期调用，唤醒轮本身秒崩没留痕）。

**三层修复**（src/agent.py，子进程验证全过）：

| 层 | 位置 | 修复 | 效果 |
|---|---|---|---|
| ① 入口（治本） | `push_message` | `if seed is not None and not isinstance(seed, dict)` → 包装成 notice seed（tool=notice + `_LOG.warning` 注明 source/类型） | **inbox/_notices 里永远不进坏 seed** |
| ② 通知队列兜底 | `_drain_notices` | `isinstance(seed, dict)` 防御——原 `if not seed` 漏过 **truthy 的 list**（非空 list 为真值，`if not seed` 不拦截） | 历史/旁路坏条目直通不崩 |
| ③ 最后防线 | `_seed_steps` | 循环体首行同款防御 | 任何路径进来的坏 seed 都降级包装，绝不崩轮 |

降级包装形态统一：`{"tool": "notice", "args": {"raw": str(seed)[:500], ...}, "result": ..., "reasoning": "（系统通知——seed 结构异常，已降级包装）"}`——模型在上下文里看到的是可读通知而非崩溃。

**验证**：list seed 三层全降级——① push 入口包装 tool=notice ✓ / ② _drain_notices 2 条全转 dict ✓ / ③ _seed_steps 直吞 list 不崩 ✓。

**排障口诀**：**「turn_start 落了但无 step ≠ 没触发，是秒崩」**——唤醒轮的异常中断在旧代码里是隐形的（8000 连中断标记都不写，看起来纯卡住）；新代码（中断留痕 + 三层防御）把它变成**可见且不致命**。

**备注**：崩溃时坏 seed 已被 pop 出队（inbox.jsonl 已清）+ 三层防御双保险，bg_task 唤醒链稳定；/restart 生效（消费侧防御仍在，历史持久化的坏条目靠消费侧兜）。

### 产地修正：_on_bg_task_done 的 seed list 包装（2026-08-31，commit 44ae953，终验发现）

> src/agent.py `_on_bg_task_done`（bg_task 通知构造）。三层防御（上节，commit 0565971）兜住了崩溃，但终验发现通知轮仍**降级显示** notice——降级形态本身暴露了产地：`_on_bg_task_done` 构造 seed 时包了层 list（`seed=[rec]`），**每一次 bg_task 通知都在产坏 seed**（不是历史残留）——11:23 秒崩的产地即此；凌晨 05:51 team_create 那条是另一个独立的 list，恰好同型。

**修复**（commit 44ae953，一处）：

```python
# agent.py _on_bg_task_done（修正前）
self.push_message(header, source=f"bg_task:{bg_id}", seed=[rec], wake=True)
                                          #      ^^^^^^^^ list 包装——11:23 崩溃元凶
# 修正后：seed=rec  # dict 直传——通知轮恢复标准合成记录
```

**效果**：通知轮恢复**标准 check_bg_task 合成记录**（工具名/参数正确，含尾部输出）——不再依赖三层防御的降级包装（模型上下文里看到 `check_bg_task(task_id=...)` 而非 `notice(raw=[...])`）。三层防御保留，兜历史/旁路条目。

> ⚠️ **后记（2026-09-11）**：本节注释宣称的「工具名正确」**当时并未成立**——`rec` 用的键是 `"name"`，而消费侧 `_seed_steps` 读 `"tool"`，工具名恒为空串。list 包装修了，**键名漂移漏网**，症状（通知轮无工具形态、看起来像 user 通知）一直持续到 2026-09-11 用户观察才闭环——见下节。

**完整修复链**：

| 提交 | 层次 | 效果 |
|---|---|---|
| 0565971 | 三层防御（push 入口 + drain + seed_steps） | 坏 seed 不再崩轮，降级为可读 notice |
| 44ae953 | 产地修正（`[rec]` → `rec`） | 坏 seed 产地关闭（**但键名仍错，工具名空**） |
| 2026-09-11 | 键名修正（`"name"` → `"tool"`） | 合成记录真正带名带参（见下节） |
| （终验） | /restart 后全链路 | 转后台 → 忙时排队 → 唤醒 → 处理 ✓ 全绿 |

**终验时序（2026-08-31 12:10 实验：timeout 15s + run_python sleep(25)，bg_1788149442381）**：

```
12:10:03  sleep(25) 启动 → 15s 超时转后台
12:10:28  后台完成 → 通知到达 → agent 忙（观察轮在跑）→ 忙时排队：压 inbox 等本轮结束（2m14s）
12:12:42  观察轮 turn_end → 通知【立即】触发新轮
          → 钩子照常跑、check_bg_task 合成记录正确、处理一次通过
```

终验同时补上 [bg_task 恒唤醒](#后台任务完成自动通知bg_task-恒唤醒2026-08-30commit-6460ad1) 节中「忙时排队」路径的**首次实测证据**（闲时立即此前已有 8000 案例佐证）：通知不丢、不抢当前轮，turn_end 即触发。

**新排障口诀（补充上节）**：**「降级包装出现在上下文 = 仍有产地在产坏 seed」**——三层防御是兜底不是免罪牌；看到 `notice(raw=[...])` 形态的合成记录，应顺着 `push_message` 调用方找产地修正，而不是满足于不崩。

**生效方式**：引擎层（src/agent.py），需 `/restart`。

### 键名漂移修复：bg_task 合成记录 `name` → `tool`（2026-09-11，用户观察触发）

> src/agent.py `_on_bg_task_done`（一处）。**用户观察**：「bg_task 现在退出时看起来是 user 吧？我觉得也可以以工具记录的通知形式（不过现在似乎只有 check_bg_task 工具？）」——用户看到的「user 通知」不是渲染问题，是**合成记录从落地起就是残缺的**。

**根因：seed 键名契约不匹配**（生产侧与消费侧各写各的）：

```python
# 生产侧 _on_bg_task_done（修复前）—— 用 "name" 键：
rec = {"name": "check_bg_task", "args": {...}, "result": ...}

# 消费侧 _seed_steps（src/agent.py L892）—— 读 "tool" 键：
self.session.toollog.record(cid, sd.get("tool", ""), sd.get("args", {}), sd.get("result", ""))
#                              ^^^^^^^^^^^^^^^ 恒拿到空串 → 工具名空
```

→ `toollog` 里记下的工具名恒为 `""`，投影出来**无名无工具形态**，看起来就是纯 user 通知。**service_exit 一直用 `"tool"` 键所以正常**——两类通知的对照恰好让用户发现了不一致。

**历史澄清**：t530 那次修复（[上节](#产地修正_on_bg_task_done-的-seed-list-包装2026-08-31commit-44ae953终验发现)，commit 44ae953）的注释宣称「恢复标准 check_bg_task 合成记录（工具名/参数正确）」——**当时只修了 list 包装，键名漂移漏网，注释里的效果从未成立**。三层防御（0565971）同理只兜「不崩」，兜不住「键名对不对」。

**修复**（一处，与 `_on_service_exit` / `_drain_notices` 降级包装的 `{tool, args, result, reasoning}` 四键契约对齐）：

```python
rec = {"tool": "check_bg_task", "args": {"task_id": bg_id},
       "result": (f"[后台任务完成·自动通知] {name}（{bg_id}）{ok}。\n尾部输出：\n{out[-4000:]}")}
```

**验证**（单测三场景）：① 修复后 seed → 工具记录 `name=check_bg_task`、args/result 完整 ✅；② service_exit 契约不变 ✅；③ **旧 `"name"` 键形态复现：工具名 = `''`**——用户观察到的症状实锤。

**为什么不需要新工具**（回答用户括号里的问题）：合成记录的 `tool` 字段本来就是**虚拟标注**——`stop_service` 那条也不是真的 stop 调用（其 reasoning 明确写「这是自行退出，并非你主动 stop」）。用 `check_bg_task` 作 bg_task 通知的合成名语义正合适：它就是「查这个后台任务」的结果形态，且模型想深挖时还能真的调 `check_bg_task(bg_id)` 拿全量输出——**合成记录与真实查询共用同一心智模型**。

**修复后效果**：bg_task 完成通知醒来时，上下文里是一条**带名带参带尾部输出的完整工具记录**（assistant `tool_use` → `tool` 结果配对渲染），与 service_exit 同款。

**新排障口诀（补充前两节）**：**「合成记录渲染成 user 通知 = 先查 seed 键名，再查是否 list 包装」**——三层防御 / list 包装修正都只管「不崩」，**键名契约**是第三条独立故障线；看到通知轮没有工具形态，先比对生产侧 rec 的键与 `_seed_steps` 读的键。

**生效方式**：引擎层（src/agent.py），需 `/restart`。随 **v0.26.6 补丁版**发布（2026-09-11，commit `1d47b37`，PyPI 已上线；未单独 tag 桌面版）——见 [v0.26.6 发布记录](../releases/v0.26.6.md)。

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

**通知气泡形态**（`renderNotifyBubble`，index.html）：`row sys` + 默认折叠一行摘要、点击展开全文——与（已退役的）autonomous 系统消息同款交互（autonomous 已于 2026-10-04 融合进 [schedule](background-scheduler.md)，交互形态同款保留于通知气泡）（见 [气泡交互](bubble-interaction.md)）；图标按 source 前缀取：📪 service_exit / 📨 bg_task / ⏰ schedule / 🤝 subagent / 🔔 其它，标题形如「后台通知（bg_task:x1）」。

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

### 新消费端：before_turn 检索钩子短路（2026-10-07，用户提案，commit 3d5fb42）

`_msg_source` 的第二个引擎消费端：`agent.py run()` 在钩子触发点判别——**非空（后台来源）整组短路 before_turn 检索钩子**（wiki_auto_query / before_turn_retrieval / skill_suggest），检索只服务人类直输轮；空串照跑；短路时 info 日志留痕。混合批判定沿用本节批首归属（first_src），无需新逻辑。详见 [workflow-hooks · 后台来源短路](../architecture/workflow-hooks.md)。

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

### 跨轮复用修复：钩子组归属 turn + _runningWf key 加 run_id（2026-09-06，commit bca1932）

**现象（用户报告）**：WebUI 启动后，所有钩子的执行信息都被渲染在第一条 answer 的过程区里——折叠进第一次触发的位置，而不是触发的那一轮。

**根因**：组复用条件只查 `g.head.isConnected`——组 DOM 留在历史轮的过程区里**永不销毁**（isConnected 恒 true），跨轮复用导致后续每轮钩子执行行全 append 进第一次触发位置的旧组。`curTurn` 是 `newTurn`/`renderHistTurn` 每轮新建的对象引用——引用比较即可区分轮次。

**修复四处联动**（src/static/index.html，纯前端）：

| 改动 | 语义 |
|---|---|
| 复用条件加 `g.turn === curTurn` | 组复用仅限**同一轮**（同轮多个 before_turn 钩子仍收进同组——组折叠本意）；跨轮自动建新组、渲染在触发位置 |
| 建组时记 `turn: curTurn` | 对象引用比较天然区分轮次，无需轮号 |
| `_runningWf` key 加 run_id（`hook::name::run_id`） | 修复第二个 bug：跨轮同名钩子工作流（每轮都跑的 wiki_auto_query 等）key=`hook::name` 跨轮冲突——后轮 start 覆盖前轮 ent，异步完成时更新错行 |
| 完成事件（auto_wf / auto_wf_error）组头推进优先 `ent.grp` | 完成可能晚于建组到达（异步/已跨轮）——`ent.grp` 精确指向自己的组，停表/计数不丢；退化取 `_hookGrp[hook]`（兼容无 ent 的漏事件） |

**生效**：纯前端（index.html），Ctrl+F5 刷新即生效，无需 /restart。

## 前端 UI 遮罩坑：toast 透明条遮挡输入框失焦（2026-08，commit 0a415bc）

**现象**：对话几轮后，WebUI 消息输入框中间靠后的位置被「透明的东西」挡住，点击那里输入框会失去焦点。

**根因**：`toast()`（`src/static/index.html`）惰性创建 `#toast` 提示条——`position:fixed; bottom:20px; left:50%` 居中 + `z-index:999`。第一次调用（发送消息时的「✅ 已接收，处理中…」transient 提示）后元素**永久驻留 DOM**；2 秒后只把 `opacity` 降到 0 淡出，**元素仍在**，且原 cssText **没有 `pointer-events:none`** → 点击落在那个透明 div 上，textarea 拿不到焦点。三个现象全部对上：

① 「几轮后出现」= toast 首次调用才创建，之后一直残留；
② 「中间靠后」= `bottom:20px` 正好落在底部 inputBar（约 66px 高）范围内、`left:50%` 居中盖住中段，宽度随最后一条文案变化（较长文案伸得更远）；
③ 「点击失焦」= 无 `pointer-events:none`，透明元素照样吃点击。

**修复**：`cssText` 追加 `pointer-events:none`——toast 是纯提示元素，本来就不需要交互，显示期间点击也穿透到下层输入框。

**同类坑**：与[气泡级复制按钮](bubble-interaction.md)的 `.bubble-copy` 同款——**`opacity:0` ≠ 不存在，透明元素照样吃点击**（淡出 + `pointer-events:none` 二者缺一不可）。顺带复核其它遮罩物确认安全：specPanel/specFab/modal-overlay 默认 `display:none`、剪贴板兜底 textarea 即用即删。

**生效方式**：index.html 磁盘 serve，**Ctrl+F5 强刷即生效，无需 /restart**。

## WS 协议自适应：CNB HTTPS 反代 Mixed Content 修复（2026-09-13，v0.27.1）

> src/static/index.html 两处（`connectWS` + `sendToolCall`），commit `57a2d30`，随 v0.27.1 发布。用户在 CNB 容器启动 `agt-web`，经 HTTPS 反代域名 `https://6hz0h133db-8000.cnb.run/` 打开 WebUI，控制台大量报错、消息发不出去。

**报错与根因链**：

```
Mixed Content: The page at 'https://…cnb.run/' was loaded over HTTPS,
  but attempted to connect to the insecure WebSocket endpoint 'ws://…cnb.run/ws'
  → SecurityError: Failed to construct 'WebSocket'          ← 连接构造当场失败
  → ws 保持 null
  → 点工具按钮 sendToolCall 里 ws.send                       ← 级联 TypeError: null.send
```

- **根因**：`connectWS()` 硬编码 `ws = new WebSocket('ws://' + location.host + '/ws')`——浏览器安全策略**禁止 HTTPS 页面发起不加密的 ws:// 连接**（Mixed Content），`new WebSocket` 直接抛 SecurityError，后面的 null.send 全是级联噪声
- **部署结构**：CNB 是 **TLS 边缘反代**——浏览器 ↔ `https://xxx-8000.cnb.run`（HTTPS/WSS）↔ 容器内 `:8000`（HTTP/WS），TLS 由边缘终结。容器内服务始终明文没有问题，问题只在前端协议写死
- **漏网原因**：`rag.html` / `workflow_debug.html` 早就是协议自适应写法（`'https:'?'wss':'ws'`）——主界面 index.html 是唯一漏网

**修复**（两处，src/static/index.html）：

1. **协议自适应**（治本）：

```javascript
ws = new WebSocket(`${location.protocol==='https:'?'wss':'ws'}://${location.host}/ws`);
```

   WSS 到边缘代理解密后转发容器内 `ws://localhost:8000/ws`——**服务端零改动**，FastAPI 侧无任何感知。

2. **sendToolCall 未连接防护**（治标防级联）：

```javascript
if(!ws || ws.readyState !== 1){ toast('⚠️ WebSocket 未连接（页面刷新可重连）——消息发不出去'); return; }
```

   连接被拦/断开时点工具按钮不再抛 TypeError，改 toast 明示。

**生效方式**：index.html 随服务启动载入内存——升级 + `/restart` 生效（浏览器侧 Ctrl+F5 强刷兜底）。CNB 容器内 `pip install -U agt-agent` 后重启 agt-web 即通。

**收益外延**：任何 HTTPS 反代场景（CNB / Cloudflare Tunnel / frp+TLS / nginx 反代）同一修复全部直接可用——**协议跟着页面走，部署拓扑不再影响前端连接**。部署排障速查见 [ops · 常见错误对照](../guides/ops.md#常见错误对照)。

## before_turn 钩子并行执行保证

见 [工作流引擎与钩子](../architecture/workflow-hooks.md#before_turn-钩子并行执行2026-08-新v0182-发布)：

- **全部完成才返回**：`ThreadPoolExecutor` + `as_completed` 确保所有钩子跑完才进入 ReAct 主循环
- **不会出现「一个钩子未完成就开始第1步」的现象**（用户实测现象为旧代码行为）

## 相关页面


- [工作流引擎与钩子](../architecture/workflow-hooks.md)：before_turn 并行执行 / async 钩子 / 快照检测闭环
- [工作流运行观测](wf-monitor.md)：执行中行点击后的观测页（run registry、节点甘特时间线，与本页秒表计时对照）
- [多 Agent 体系](../architecture/multi-agent.md)：inbox 路由 / 三层消费机制（+ pending_messages 盲区补全）/ 子 Agent 唤醒
- [human_step 人在环](human-step.md)：Agent 指挥人类操作 GUI/物理界面（WS action `human_step_response` + Event 阻塞，同款交互底座）
- [工具执行审批](tool-approval.md)：审批卡片的 WS action 通道 + 刷新后 pending 重发
- [图片输入链路](image-input.md)：WebUI 贴图的两条注入通道（空闲原生多模态 / 忙碌落盘 + `<img>` 引用）
- [wiki_auto_query](../features/wiki-auto-query.md)：before_turn 自动检索实例（默认关闭）
- [v0.19.2 发布记录](../releases/v0.19.2.md)：本页 wake 语义修复随该版发布

