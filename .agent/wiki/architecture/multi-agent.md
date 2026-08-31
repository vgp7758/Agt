# 多 Agent 体系

> src/multiagent.py + src/registry.py + src/agent.py。docs/architecture/06 有基础版，本页收录 2026-08 全部演进（全异步/reuse/复活/assembly/system_append/registry 修复）。

## 声明与生命周期

子 Agent 声明：`.agent/agents/<name>.yml`（v2.1：纯配置，persona 拆独立同名 .md，.yml 优先加载）或旧版单 `.md`（frontmatter：name/description/model/tools + assembly/system_append DSL）。**2026-08（commit f177674）存量 5 个子 Agent（coder/explorer/reviewer/vision/wiki-updater）已全部规范化为 v2.1**——必装段显式入 assembly（所见即所装）、recap_gen 显式入 hooks（详见 [/agents 管理页 · 声明规范化](../features/agents-admin.md#声明规范化5-个子-agent-全部转正所见即所装)）。**主 Agent 声明：`~/.agt/main.yml`**（全局，非 .agent/agents/ 成员；assembly 是完整配方，见下）。可视化管理走 [/agents 管理页](../features/agents-admin.md)。

```
agent_prompt(name, prompt, tools?, agent_id?, reuse?, assembly?, caller?)
  ├─ reuse=true 且有同名空闲活实例 → 直接派活（沿用 agent_id/session）
  ├─ reuse=true 无活实例但有同名历史条目 → 复活（读声明同源 _agent_def_path+load_agent_yml，+ Session.load 磁盘历史）
  └─ 否则新建（session 嵌套 主session/agents/<id>/，meta.json 记 _agent_meta）
全异步：立即返回；完成后按 caller_id 路由 answer 入调用者 inbox（下轮自动激活）
要结果才继续 → wait_subagents(agent_ids)
caller: 汇报对象（answer 完成后路由给谁）——留空=自动捕获调用者；'user'=fire-and-forget
        不路由任何 Agent；显式 agent_id=跨 Agent 委托（见下节 caller 章节）
```

- 主 Agent id=`_main_`；registry 是唯一事实源（团队看板/recap/路由都读它）
- `_agent_meta`（agent_id/name/model/task/caller_id/recap/status）无条件写子 meta.json → 读档 `_restore_subagents` 恢复团队
- 子 Agent 的通信/会话工具**重绑自身**（继承的闭包绑主 Agent，会查错 session）
- `name`/`caller`/`target_id` 参数动态注入 enum（合法值提示 + 编辑器下拉，见 [caller 汇报对象与动态 enum 注入](#caller-汇报对象与动态-enum-注入2026-08)）

## 复活路径 NameError · wiki-updater 多实例根因修复（2026-08-26，commit 6d396af）

**现象**：团队看板出现 `wiki-updater` / `wiki-updater_2` / `wiki-updater_3` 多个同 name 实例，各自带着同样的攒批任务 recap——"不是检查忙就攒批短路了吗，为什么还建新实例？"

**根因链**（与 busy 检查无关，问题在复用路径的**复活分支**）：

```
每次 /restart → _restore_subagents 按 meta.json 恢复 registry
  → 旧 wiki-updater 条目 agent=None（历史条目，非活实例）
  → 下次派活 reuse=true：无空闲活实例 → 有同名历史条目 → 走复活
  → _revive_subagent 调用【已删除的 _build_subagent_system】
  → NameError → except 吞掉 → 静默落回「新建」路径
  → auto-numbering 造出 wiki-updater_2 / _3
```

日志铁证：`[WARNING] 复活子 Agent wiki-updater(wiki-updater) 失败: name '_build_subagent_system' is not defined`，跨度 08-25 23:24 ~ 08-26 15:24，恰好对应三次重启。

**修复**（src/multiagent.py `_revive_subagent`）：
- 声明加载与新建路径【同源】——`_agent_def_path` + `load_agent_yml`（v2.1 yml / 旧 md 双格式）
- 顺带根因二：旧实现 `_agent_md_path` + `_split_frontmatter` + 已删除的 `_build_subagent_system`，对 v2.1 纯 YAML 解析出 `meta={}`（工具白名单/模型全丢）——即便不 NameError 也会丢配置
- 复活后投影 `current_turn_only=True`（reuse 语义）：历史轮完整归档可查，但不进上下文

**本地清理**：删掉 `wiki-updater/`、`wiki-updater_2/` 的 meta.json（`_restore_subagents` 读 meta.json 恢复，删后重启不再注册这两个重复条目；当前进程看板仍显示是因目录句柄占用删不了整目录）——`/restart` 后只恢复 wiki-updater_3，实例数收敛为 1。

**关联**：wiki_auto_maintenance 的 busy 检查按 **name 子串**（`"wiki-updater" in l`，见 [wiki 自动维护 · busy 检查](../features/wiki-auto-maintenance.md#busy-检查与攒批短路按-name-子串判定--多实例多行判定2026-08-26)），其多行判定已同步加固。

### 恢复状态修正：meta running → failed（2026-08-28，commit b674265）

**背景（用户诊断链）**：wiki-updater_3 看板恒显示 ❌（任务被 /restart 中断——events 尾部 turn_start + 5 step 后无 turn_end，meta 已写 `status="failed"`），但 busy_parse 把 ❌ 行判成忙 → 攒批队列对躺平失败者死锁（pending 堆 21 条永不消费）。用户猜想：「是不是前面的任务被中断后状态一直显示忙，后面任务还认为它在忙（实际早中断躺平了）」。

**修复**：`_restore_subagents` 读档恢复时，meta 存 `status="running"`（进程被杀时任务在跑，`_bg` 没来得及写终态）→ **修正为 `failed`**——重启物理上杀掉了所有 daemon 线程，恢复出的条目不可能还在跑；不修正的话看板谎报「忙」→ 攒批判定永久死锁。配套：busy_parse 判定改为 ✅(done)/❌(failed) **都算空闲**（见 [wiki-auto-maintenance · busy 检查](../features/wiki-auto-maintenance.md#busy-检查与攒批短路按-name-子串判定--多实例多行判定2026-08-26)）。

**后续自动恢复**：堆积的 pending 不用手动清——/restart 后下一轮 before_answer 触发 → 判空闲 → 全量读批次 → wiki-updater_3 复活（reuse 复活路径已修好）消费 → 队列轮转清空。

**最终验收（2026-08 末，随 Agent 专属页 URL 路由同批）**：闭环达成——wiki-updater_3 从 ❌ 复活为 running、消化完堆积的 21 条 pending（攒批队列轮转清空）、看板回 ✅ 带新 recap——「❌ 判忙 → 永不入队 → 永不消费」的死锁链彻底断开。

## 声明级回退链（fallback 键，2026-08 起管理页表单化）

声明里的 `fallback` 键决定该 Agent 的 LLM 回退链，**覆盖全局 settings.fallback_chain**。三形态（`_parse_agent_fallback`，src/multiagent.py）：

```yaml
fallback: "glm, deepseek-chat"            # ① 逗号串
fallback: [glm, deepseek-chat]            # ② list
fallback: {chain: [glm], policy: sticky}  # ③ 链 + 策略
```

- **agent_prompt 新建/复活/reuse 三条路径都消费**——改链后 reuse 实例下一任务即生效
- 未声明 = 继承全局 `fallback_chain / fallback_policy`（见 [配置体系](../guides/config-and-models.md)）
- 2026-08（commit a667da4）起 [/agents 管理页表单化编辑](../features/agents-admin.md#回退链表单--钩子行布局修复2026-08commit-a667da4)（此前只能手写 yml）：模型 chips 点选（顺序=链序）+ 策略下拉；**留空 = 继承全局**（保存不写键）；「显式关回退」（区别于继承）手写 yml 空串；`_main_` 主 Agent 同样支持声明级回退链（main.yml，留空=删键）

## AgentRegistry 与 answer 路由修复（2026-08，v0.18.2 正式发布）

### 旧版根因

旧版代码无 registry 机制。子 Agent 在后台 `_bg` 线程中运行，完成后调用 `push_message` 将 answer 路由回调用者 inbox。但 `push_message` 内部需要查 `agent.registry` 来定位 caller，旧版 `agent.registry` 为 `None`，导致 answer **未被入队** caller 的 inbox——主 Agent 永远收不到子 Agent 的回复，表现为"调了 vision 子 agent 后主 agent 未被唤醒"。

**根因确认（2026-08-18 诊断）**：消息根本没入队，不是消费端丢失。旧版 `push_message` 路径在 registry 为 None 时直接跳过，inbox 从未收到 answer。

### 修复

引入 `AgentRegistry`（`src/registry.py`）：每个 Agent 实例创建时注册到全局 registry，`push_message` 通过 registry 按 `caller_id` 查找目标 Agent 并将 answer 入其 inbox。`_bg` 线程完成时 registry 不再是 None，answer 正常路由，主 Agent 下轮自动激活。

### 关键链路

```
子 Agent _bg 线程 finish
  → push_message(caller_id, answer)
  → AgentRegistry.get(caller_id)   ← 旧版此处返回 None → 跳过（消息未入队）
  → caller.inbox.put(answer)       ← 修复后正常入队
  → 主 Agent下轮 _worker 取出 inbox → 激活
```

### 三层消费机制（当前代码，消息不会丢——前提：进程存活）

| 层 | 机制 | 源码位置 | 说明 |
|----|------|----------|------|
| ① | `run()` 内 `pop_inbox` | `agent.py` ReAct 循环每步前 | 每轮 ReAct 步骤开始前检查 inbox，有消息则注入当前上下文 |
| ② | `inbox_thread` 轮询 | `agent.py` 后台线程 | 独立线程持续轮询 inbox，收到消息后触发处理 |
| ③ | `work_q` 触发新一轮 | `agent.py` → `chat.py` | inbox 收到消息后向 work_q 投递任务，驱动 `_worker` 开启新一轮 `run()` |

三层互为补充：①在 run 进行中时即时消费；②在 run 空闲时后台拾取；③确保新一轮 run 被调度。只要 answer 成功入队（registry 非 None），至少一层会消费它。

**第四类消息源：用户插话队列（pending_messages）——2026-08-19 前是消费盲区（commit fb115aa 补全）**：用户在 answer 生成期间插话**不进 inbox**（走独立的 `pending_messages` 队列，步边界能赶上则当步注入），而 answer 完成后的自动触发点旧版**只查 inbox** → 插话滞留，直到用户手动发下一条消息才被注入。fb115aa 补全这一兜底：inbox 空 → `pending_messages` 非空 → 立即开新一轮（`background_trigger`·`user_insert`）。两套队列至此都闭环，详见 [用户交互 · 插话机制与消息路由](../features/user-interaction.md)。

**边界条件**：三层均为进程内对象/线程——若宿主进程退出（含 rc=0 正常退出），daemon 线程（②③及子 Agent `_bg`）随之死亡，inbox/work_q 中已入队消息**全部丢失**。见下节 9100 案例。

### 端到端验证状态（2026-08-18，三阶段，v0.18.2 已发布）

**阶段一（通过）**：POST `/api/status` 跨实例调用成功（见 [/api/status 端点](../features/api-status.md)），确认：
- registry 在多实例环境下正确注册各 Agent
- 子 Agent 完成后 answer 经 `push_message` 正常路由回 caller inbox
- 三层消费机制无丢消息

**阶段二（环境问题，根因已修正）**：在 9100 端口新起 agt-web 实例复测 vision 唤醒链路，服务**反复退出（rc=0）**——daemon 线程随进程死亡，inbox 消息丢失，复现"vision 完成后主 Agent 未被唤醒"表象。
**根因（后续排查确认）**：9100 端口被**旧实例（pid 22636）占用**，新实例起不来反复自退——非代码 bug，此前"entry point 不解析命令行参数 + 端口探测逻辑异常"的推断**不成立，已修正**。处置：`taskkill` 清理 pid 22636，端口释放后新实例稳定运行。
**教训**：新端口实例反复退出 rc=0，第一步先 `netstat -ano | findstr <端口>` 查占用再怀疑其他（见 [ops 常见错误对照](../guides/ops.md#常见错误对照)）。

**阶段三（进行中，等待闭环）**：
- 端口清理后新实例稳定；**stdin 通道端到端验证成功**——`send_to_service` 发送后实例 busy=True，外部消息→实例处理通道打通
- **诊断日志已埋点（commit e0ae60b）**：两处核心观测点加日志——① `_bg` 完成路由 `push_message`（answer 入 caller inbox 处）② `inbox_thread` 搬运（inbox → work_q 触发新一轮处），均在 `src/agent.py`
- **阻塞**：新实例首轮因 **proxy 响应极慢（单次 590+ 秒）**未跑完，子 Agent 尚未派发，两处观测点日志未出现——"链路未走完"≠"链路失败"
- **当前策略**：已挂**定时巡检**（定期回看实例日志/状态），等待首轮完成后回收观测点日志，闭环确认整条链路

> **v0.18.2 发布状态**：registry 根因修复代码已发布（PyPI `agt-agent` 0.18.2），stdin 通道验证通过，观测点日志已埋。阶段三的"等待首轮完成闭环"属运行时验证，不影响代码正确性——根因已定位并修复，三层消费机制在进程存活前提下设计上不丢消息。详见 [v0.18.2 发布记录](../releases/v0.18.2.md)。

**待闭环链路（★=e0ae60b 观测点）**：

```
registry 注册 → push_message(caller_id, answer)★ → caller.inbox
  → inbox_thread 搬运★ → work_q 触发 → _worker 调度 → 主 Agent run() 新一轮
```

### 注意事项

- registry 是进程内全局对象；运行时状态现可通过 POST `/api/status` 端点从外部 HTTP 查询（commit a922121，见 [/api/status 端点](../features/api-status.md)），读取时已加锁/快照
- 子 Agent 工具闭包重绑自身时也依赖 registry 确认身份，旧版同样受影响
- **排障速查**：若再现"子 Agent 完成后主 Agent 未唤醒"，按序排查——
  1. **实例是否反复退出 rc=0**：先查端口占用——`netstat -ano | findstr <端口>` → `taskkill /PID <pid>`（9100 案例即旧实例 pid 22636 占用端口，清理后稳定）。进程死亡 → daemon 线程死亡 + inbox 消息丢失，表象同链路 bug 但属环境问题（见 [ops 常见错误对照](../guides/ops.md#常见错误对照)）
  2. **registry 是否为 None**：查 `/api/status` 快照 registry 字段
  3. **观测点日志定位断点**：commit e0ae60b 已在 push_message 入队与 inbox_thread 搬运两处埋日志，看实例日志即可判断 answer 是否入队、是否被搬运
  4. **排除"首轮太慢"误判**：proxy 单次响应可慢至 590+ 秒，观测点未出现≠链路坏，看 llm_calls.jsonl 的 elapsed 区分"慢/死"
  三层消费机制在进程存活前提下设计上不丢消息

## 事件流 agent_id 打标与 WebUI 串台修复（2026-08-21，commit ba0940b）

### 现象与根因

WebUI 上子 Agent 的实时输出与主 Agent 串台——同一轮 answer 气泡里混入子 Agent 回应和主 Agent answer，互相覆盖。

```
根因链（spec_tools.py L482）
  explore_subagent 构造 SubAgent 时传 on_event=agent.on_event
  → 同步子 Agent 的 answer 事件直接流入主事件流
  → 前端 finishAnswer 写当前轮 answerEl → 与主 answer 覆盖混排
```

**范围**：仅**同步调用**的子 Agent（explore_subagent / update_wiki / 早期 wait 场景）——主 Agent 等它工具结果期间其 answer 先到。异步 `agent_prompt` 路径 on_event=None 本就不串——answer 走 inbox → 主 Agent 新一轮（消费机制见上节）。

> **身份澄清（2026-08，commit eafed25）**：explore_subagent 是 `src/spec_tools.py` 里的**同步工具**（spec 前置探索：制定施工方案前并行派 N 个摸不同模块，产出喂 create_spec；不注册 registry、硬编码只读白名单），与 `.agent/agents/explorer.md` 的 explorer 声明式子 Agent 是**两回事**——对照表见 [spec 工具集](../features/spec-tools.md)。同 commit 顺带修复其残留的 `token_budget=20000`（早期「解除子 Agent 预算」改造漏掉的独立构造路径）→ 对齐为 0，`max_steps=12` 保留。

### 修复

- **后端一处全覆盖**（`agent.py` `_emit`）：`event.setdefault("agent_id", self.agent_id)`——所有 Agent 的所有事件（answer/thinking/step/tool_*）统一打标，主=`_main_`，子 Agent=各自 id。在 `_emit` 收口而非各发射点补标，天然全覆盖（含漏网事件类型）
- **前端**：answer 事件按 agent_id 分页渲染（气泡顶部小 tag 按钮点击翻页，仅该轮有效，最新到达页自动激活）；thinking/step 事件子 Agent 的带 `[agent_id]` 前缀进 trace——详见 [气泡交互 · answer 多 Agent 分页](../features/bubble-interaction.md#answer-多-agent-分页indexhtml--agentpy2026-08-21)

### 注意事项

- `setdefault` 而非直接赋值：若上游已显式带 agent_id 的事件不被覆盖
- 前端 `finishAnswer(text, agentId)` 中缺省 agent_id 一律归 `_main_` 页；历史渲染路径（renderHistTurn 临时 curTurn）靠 `pages || {}` 兜底
- 派生需求：凡走 `on_event=agent.on_event` 的新同步子 Agent 创建点，都会受益于此打标——无需再单独处理

## 事件广播 target 过滤 · agent_id 的第二个消费端（2026-08，commit 30ac45b）

`_emit` 给事件打的 `agent_id` 除了驱动前端 answer 分页 / trace 前缀（上节），现在被 **`src/server.py` 的 `_broadcast` 消费**做多客户端过滤：每个 WS 客户端记录正在交互的 `target`（agent_id，默认 `_main_`），带 `agent_id` 的事件只发给 target 匹配的客户端——多页签各与不同 Agent 交互时互不串台；无 `agent_id` 的系统级事件仍全端广播。

配套：客户端切 Agent 改自身 target + 响应单发；文本直达 target 子 Agent（对齐 CLI `/agent` 切换语义）；`load_session` 历史广播带 `agent_id="_main_"`；`current_history`/`expand_history` 按客户端 target 取对应 session。完整行为表与 answer 特例见 [用户交互 · 多客户端 target 路由](../features/user-interaction.md)。

## caller 汇报对象与动态 enum 注入（2026-08）

两个用户提案同 commit 落地（src/multiagent.py + src/server.py）：① `agent_prompt` 增 `caller` 参数——汇报对象可显式指定；② 多 Agent 工具参数动态注入 enum → 工作流编辑器渲染成下拉框。

### caller：answer 完成后路由给谁

`agent_prompt(name, prompt, ..., caller="user")`——汇报对象三态：

| caller 值 | 语义 |
|---|---|
| `''`（默认） | 自动捕获调用者（`agent.agent_id`）——现有行为不变 |
| `'user'` / `'system'` | **fire-and-forget**：answer 不路由任何 Agent（`'system'` 归一为 `'user'`） |
| 显式 agent_id | 跨 Agent 委托：registry 校验存在性，不在表内返回 `[未知 caller]` |

实现上**零新增路由分支**——`_route_answer` 现有 `caller_id == "user"` 判断天然接住 fire-and-forget；派发信息段（〔任务派发信息〕，告知子 Agent 如何反查派发者上下文）同样跳过——子 Agent 无法反查 user 的上下文，语义自洽。

**典型场景（工作流节点里派活）**：`agent_prompt(caller="user")` + `wait_subagents(agent_ids)` 取结果——子 Agent 完成不再唤醒主 Agent 烧一轮 token。这是 [后台通知 wake 语义（service_exit 不再独立触发轮）](../features/user-interaction.md#后台通知-wake-语义service_exit-不再独立触发轮2026-08v0192) 同款语义在**派活侧**的补全：钩子工作流派子 Agent 干活、结果由工作流自身消费的场景用它。

### _inject_agent_enums：动态 enum 注入

给多 Agent 工具的参数注入动态 enum（合法值提示 + LLM schema 约束 + 编辑器下拉框三合一）：

| 参数 | enum 来源 |
|---|---|
| `agent_prompt` / `kill_agent` 的 `name` | `.agent/agents/` 声明扫描（`load_agents_index`）——coder/explorer/reviewer/vision/wiki-updater |
| `agent_prompt` 的 `caller` | `['', 'user']`（显式 agent_id 仍可手填） |
| 通信工具（agent_ask/notify/query_*）的 `target_id` | registry 当前全部 agent_id（动态性强，提示性候选） |

**刷新时机**：`make_subagent_tools` 装配时注入一次；**create_agent / kill_agent 声明变化后重注入**——新建一个子 Agent，agent_prompt 节点的 name 下拉里立刻出现它。enum 是**提示性的**（不在列表内的值仍可传），过期无害。

### 三层全通：LLM schema → /api/tools → 编辑器下拉框

`/api/tools`（server.py `api_tools`）此前只硬编码 `llm_call.model` 一条 enum 透传路；现改为**通用透传**——工具 schema 自带 enum 的参数一律原样带给编辑器（`llm_call.model` 保留 API 侧附加路径——它在 LIGHT_TOOLS 构造时无法静态声明 enum）。前端基建早已就位：`syncToolNode` 同步 enum（工具 schema 更新后已有节点选项跟着变）、`makeInputControl` 检测 enum 渲染 select 下拉（空值选项显示「（跟随）」）。

```
_inject_agent_enums（multiagent.py；装配 / create / kill 时刷新）
  → Tool.schema.parameters.properties.<param>.enum   ← LLM 调用时的合法值约束
  → /api/tools 通用 enum 透传（server.py）
  → syncToolNode + makeInputControl（workflow_editor.html）→ 下拉框
```

`/restart` 后在编辑器拖一个 agent_prompt 工具节点即可看到 name/caller 均为下拉框（enum 机制详见 [编辑器 UX · enum 参数渲染为下拉框](../features/editor-ux-improvements.md#附enum-参数渲染为下拉框通用机制)）。

## 通信（agent 间）

| 工具 | 语义 | 落盘 |
|------|------|------|
| agent_ask | 无状态询问（对方上下文快照+问题→LLM→回你） | 否 |
| agent_notify | 有状态提示（入对方 inbox，等效用户插话） | 是 |
| agent_query_events / _tool_detail | 只读查对方轮次/工具调用详情（历史 Agent lazy load） | — |
| list_team | 团队清单（exclude 自己） | — |

通信工具的 `target_id` 动态注入 enum（registry 当前全部 agent_id，作提示性候选）——见 [caller 汇报对象与动态 enum 注入](#caller-汇报对象与动态-enum-注入2026-08)。

## wait_subagents：干等调查与诊断埋点（2026-08，commit 22cf719）

**现象（/restart 前，t456）**：观测页 wait_subagents 节点一直 running（"干等"）——用户怀疑「判断 agent 状态的逻辑是否被改过」。

**排查结论：wait 逻辑从未被改过**——`agent_ids` 空→取所有活线程、`th.is_alive()`→`join(timeout)`、读 `background_tasks.status`，从引入以来一行没变。近期改动都在周边（busy_parse 工作流侧判空闲、复活路径 NameError），与 wait 本身无关。

**"忙完了还在等"的真实机制（时间线推断）**：

```
本轮 before_answer 钩子触发 → agent_prompt 派 wiki-updater_3（21 条攒批大维护）
  → wait_subagents("wiki-updater_3") join 等待
  → 大维护 × ms-deepseek 慢速跑了很久
  → 钩子超时（hook_timeout=300s）：fut 取消 + 结果丢弃 + 主循环放行（主 Agent 轮继续）
     ↑ 但执行线程还活着在 join → 观测页 wait 节点一直 running ← 你看到的"干等"
  → 看板 ✅/recap 更新 = 上一轮任务的完成态（让你以为它忙完了）
  → 实际本轮新任务它还在跑，wait 在等的就是这个新任务
```

要点：**hook 超时放行 ≠ 线程被杀**——daemon 线程继续跑完，wait 的 join 一直阻塞到任务真结束；「干等」表象其实是「真在等一个慢任务」——wait 没判断错，错的是"以为它忙完了"的观感来源（看板显示的是上一轮完成态）。wait 的 `timeout` 只约束本工具调用，超时返回 running 项、不杀线程（见 [声明与生命周期](#声明与生命周期) 的调用约定）。

**修复（src/multiagent.py，commit 22cf719）**：wait 入口记录 `ids / timeout / 每个 id 的线程态与任务态`；join 超时再记一条。下次卡等日志直接显示「在等谁、它处于什么状态」——线程没退 / 任务真没完 / join 错对象，一眼可辨。需 `/restart` 生效。关联的派活侧语义见 [caller 汇报对象](#caller-汇报对象与动态-enum-注入2026-08)（`caller="user"` + wait_subagents 取结果）。

## recap（每轮一句话总结）

finish_turn 后异步生成（utility_client，scene=recap）——不进自己上下文，但显示在队友的 teammates_block；子 Agent 完成后 recap 写入 `_agent_meta` 随 meta.json 持久化。

**两条生成路径（都回写 `Turn.recap` + recaps.jsonl，2026-08 新）**：

| 路径 | 触发时点 | `turn_idx` 捕获 | 回写执行者 |
|------|----------|-----------------|-----------|
| recap_gen 工作流（`turn_end: recap_gen\|async` 钩子） | turn_end 钩子在 **finish_turn 之前**触发 | `len(session.turns)`（轮尚未归档，落点即 len(turns)）——经 **hook_ctx 上下文袋**注入 | **工作流自身**：`start(+hook_ctx) → llm → code 组装 payload → plugin hook_write → end`（见 [workflow-hooks · hook_ctx/hook_write](workflow-hooks.md#hook_ctx-上下文袋--hook_write-工具2026-08)） |
| 内置 `_generate_recap`（无工作流声明时） | finish **之后** daemon 线程 | `len(session.turns) - 1` | 引擎（同 hook_write 的 set_turn_recap 落点） |

**2026-08（commit 91b8437）回写迁移**：recap_gen 工作流路径的回写**从引擎特判移到工作流**——`_async_hook` 的 recap 分支（meta.recap/name 兜底 17 行）删除，工作流经 `hook_write` 工具显式回写（三落点：`_recap` / registry / Turn.recap+recaps.jsonl，错误特征过滤 `_RECAP_ERR_MARKS` 不污染）。**多 turn_end 钩子共存时「以谁为准」由工作流显式决定**（谁调 hook_write 谁负责，后写覆盖先写）。`hook_write` 闭包绑定 agent，主/子 Agent 双注册（子 Agent 重绑自身版本）。

**rewind 一致性**：`_rewrite_persistence` 同步裁剪 recaps.jsonl（只留 idx < keep）——否则回溯后新轮「长到」旧 idx 会被旧 recap 张冠李戴（load 侧按 idx 盲配）。

**recap_gen 挂钩来源两代**：此前是 `agent_prompt` 派活时的**运行时注入默认**（yml 未声明 hooks → 注入 recap_gen）——行为正确但管理页看不见（yml 里没有）；2026-08（commit f177674）起 5 个子 Agent 的 `turn_end: recap_gen|async` 全部**显式写进声明**——注入逻辑幂等（已有即跳过），不会双跑，/agents 管理页所见即真实运行配置。

**运行观察（2026-08-21，团队看板）**：recap 全面工作——新条目（vision_4/6/7/9 等）各自带完整检查任务描述（配图逐张检查、分享卡胶囊区域检查）、caller 指向 `_main_`；`_agent_meta` 持久化上线**之前**的历史条目（coder_*/vision_3/5/8）无 meta → 列表显示「(历史任务)」兜底——两代数据的分界线在存档里清晰可见，验证了 meta 持久化前后行为符合预期。

**第二消费端（2026-08 新）**：recap 同时填进 fc 折叠摘要行的 tail——`_folded_summary` 每轮 tail 优先级改为 **recap → answer 代码摘要 → 中断标注**（recap 语义密度高于 answer 代码摘要的「首行+标题」，后者常是「完成并推送 ✅」类横幅文案）。详见 [context-engine · 折叠摘要 tail 优先级](context-engine.md#折叠摘要-tail-优先级recap--answer-代码摘要--中断标注2026-08)。

### recap_gen 模型切 local-lfm（2026-08-30，commit e8ef64a）

**背景**：用户发现 recap 批量失败（llm_calls 319 条 429）并质疑「工作流里选了 local-lfm，日志却报 utility」——排查真相：`.agent/workflows/recap_gen.xml` 的 LLM 节点磁盘上一直是 `<model>proxy</model>`（用户在编辑器选过 local-lfm 但**未点保存**），真实失败链 = proxy 路由 → glm（bigmodel）→ 429 insufficient balance。此前「model 未设置 → utility 兜底」的诊断是 **grep 形态错误**：type3 LLM 节点的 model 是独立 `<model>` 标签而非 `<param name="model">`，搜不到 ≠ 未设置（坑详见 [workflow-hooks · 双格式与热加载](workflow-hooks.md#双格式与热加载)）。

**修复三处（commit e8ef64a）**：
1. `.agent/workflows/recap_gen.xml`：model `proxy → local-lfm`
2. 播种源 `src/workflows/recap_gen.xml`：清双重声明——老 `<param name="model">local-qwen</param>` 残留（该 provider 键已不存在）与新 `<model>` 标签并存，删旧留新
3. models.json：local-lfm `thinking → false`（节点级 thinking:false 原本已配，实例级兜底）——关思考链提速 ~38s→~15s

**生效方式**：工作流每轮重扫，**当轮 turn_end 即走 local-lfm**（免重启；llm_calls 里 scene=`hook:turn_end·recap_gen` 的 model 字段可验证）。零 token 成本的 recap 通道至此落地（见 [本地模型评估](../guides/local-models.md)）。

**遗留**：utility 的 glm（bigmodel，glm-5.3-flash）余额不足未修——意图分类 / 精排等 utility 场景仍会撞 429（充值或 `/config utility_model` 换通道）。

**后续一（同日 commit 0d852a0）**：「当轮即走 local-lfm 免重启」有一层前提——**进程得认识该 provider 键**。`config.MODELS` 是启动时快照，长寿进程启动于 local-lfm 条目加入 models.json 之前 → 运行时 `get_profile('local-lfm')` KeyError → `_get_llm` **静默回退 ctx.llm（utility）**——用户再报「还是 utility 走回退链」（param 反序列化假设五层验证排除后锁定此层）。修复：回退处加 `_LOG.warning`（下次一眼定位）；当时处置 `/reload models` 后才真正走 8081（llm_calls 出现 model=local-lfm 的记录）。

**后续二（次日 commit 85a41fd，用户裁定「只加一行错误日志不算修复」——根因闭环）**：`get_profile` 入口惰性 mtime 重载（`_maybe_reload_models` + `_MODELS_RELOADING` 重入保护）——models.json 磁盘变化自动重读，条目缺失类故障整类消除，**无需人工 /reload models**。至此 recap_gen 模型路由三层全修：XML 声明（e8ef64a）→ fallback warning（0d852a0，诊断层）→ 惰性重载（85a41fd，根因层）。详见 [workflow-hooks · `_get_llm` 静默 fallback 与 MODELS 惰性重载](workflow-hooks.md#_get_llm-静默-fallback-加日志与-models-惰性重载根因修复2026-08)。

### recap_gen 间歇性 local-qwen 复发与观测网（2026-08-31）

**现象（用户：「/restart 后第二轮 repl 再次出现 recap_gen 先调 utility → 回退链——上一轮看起来正常了这一轮又来了，很玄学」）**：同进程内 17:42 local-lfm 正常（日志面板证据）→ 18:04 异常。

**实锤（0d852a0 的 warning 抓到）**：`LLM 节点模型 'local-qwen' 未找到`——那轮执行读到的 model 确实是 **local-qwen**（非 local-lfm）→ KeyError → fallback utility（400「该模型始终思考，不支持关闭思考」）→ 回退链 glm 429（insufficient balance——glm 条目 ModelScope 余额又见底，充值或调回退链）→ proxy（deepseek-v4-flash）兜底成功。

**玄学的本质**：磁盘 recap_gen.xml mtime 13:04 后未变（一直是 `<model>local-lfm</model>`）、解析路径统一（scan / get_hook / _load 全走 xml_to_canvas，逐一验证）、无缓存冲突/全局副本——**静态排查已到极限，local-qwen 的运行时来源无法从代码层推出**。前次三层修复（e8ef64a / 0d852a0 / 85a41fd）覆盖的是「可静态解释」的成因，间歇性复发属另一层。

**应对：三层观测网 + 注入溯源（commits 0dc1dfc + 7c38a98 + 38afea6，下次复现不会溜走）**：

| 层 | 捕捉 |
|---|---|
| 扫描层：`validate_canvas_detailed` LLM 节点 model 校验（0dc1dfc） | 每次钩子触发前的 scan——local-qwen 出现在 canvas 里就告警（workflows_info warn 状态） |
| 执行层：`_get_llm` KeyError warning（0d852a0 已有） | 18:04 实锤即它抓到 |
| 流水：独立 client 调用记入 llm_calls（7c38a98） | /stats 可见每跳 model / 错误 |
| 注入溯源：结果注入带 run id（38afea6） | `<hook name="..." run="<rid>">`——异常轮注入文本自身即凭证 |

下次再出现 local-qwen，**扫描层先告警**——对比扫描时刻与文件 mtime 即可锁定是「读到的瞬间就有」还是「运行中变化」。引擎侧详情（排除法 / validate_canvas_detailed 实现 / 定位策略）见 [workflow-hooks · 复发与三层观测网](workflow-hooks.md#复发与三层观测网扫描层-model-校验2026-08-31commit-0dc1dfc)。

**run_id 注入已落地（用户提案「把缓存 id 带上」→ commit 38afea6）**：每次钩子执行的结果注入带 run id——`/wf/monitor?run=<rid>` 可回溯**那次执行用的完整 canvas**（llmParam 的 model 原值在里面）——间歇性异常发生时现场本身就是证据，不用再从日志反推。实现两处（src/agent.py：notes 补 rid 键 + 注入标签带 run 属性）详见 [wf-monitor · run_id 注入溯源](../features/wf-monitor.md#run_id-注入溯源钩子注入标签带-run-属性2026-08-31commit-38afea6)；`/restart` 生效。

#### 观测网首验：recorder 实证生效与 18:22 遗物归因（2026-08-31）

**recorder（7c38a98）生效铁证**：/restart 后 18:26:21 llm_calls 出现 `model=local-lfm-vl  resp=…lfm2.5-vl-3b…gguf` 记录——**独立 client 的调用（wiki_auto_query 的本地 vl）首次进 llm_calls 流水**，此前这类调用全为盲区。

**18:22 的 utility 回退 = 旧进程遗物**：时间线——18:22:41 proxy 兜底成功（旧进程最后一次 recap）→ restart → 18:26 新进程（local-lfm-vl 已正常路由）——该次回退**不构成新进程复发证据**。

**下一个观察点**：新进程首个真实 turn_end recap_gen（三层观测网 + 注入溯源就位）——

| 观测点 | 看什么 |
|---|---|
| llm_calls | `model=local-lfm` ✅（玄学或随旧进程消亡——canvas 残留是旧进程内存态，restart 即清）/ `model=utility` 🔴 复发——但这次有现场：注入 `run` 属性直达 `/wf/monitor?run=<rid>` 回溯 canvas |
| 扫描层校验（0dc1dfc） | recap_gen 状态变 warn（模型不在 models.json）——扫描时刻即抓到 |
| 🐞 面板 | 成功/回退日志带 scene 标注 |

注：before_turn 位置的 run 属性（38afea6 漏网路径）同轮已补（e8d3792，见 [wf-monitor · before_turn 补遗](../features/wf-monitor.md#before_turn-专用渲染路径补遗2026-08-31commit-e8d3792)），需再 /restart 生效。

## assembly DSL（上下文装配配方）

```yaml
assembly:
  - system          # 必装（未列出引擎自动补插）
  - rules|optional  # optional：默认不装配，agent_prompt assembly="rules=on" 按需打开
  - history|optional
  - user_message    # 必装（自动补插）
  - hooks|optional  # 默认关=整个 Agent 不跑钩子工作流；=on 打开
  - steps           # 必装（自动补插）
  - tail|optional   # 时间/计划/召回/队友看板；=on 打开
```

语义=**只装列出的段**；必装段（system/user_message/steps）未列出时引擎**自动补插**——历史上子 Agent 常裸 `[text]` 依赖此兜底，编辑器只见 1 项、实际投影跑 3 段。agent_prompt 可传 `assembly` 临时覆盖：`seg=on` 打开/补插、`seg=off` 移除。

### `|optional` 真语义：默认不装配，`=on` 打开（2026-08，commit 1e3b206）

**语义翻转**：此前 `|optional` 只是文档性注释——解析时 split 丢弃尾标、**列出即装配**（可 `=off` 关）；现在变成真开关——**声明 optional 即默认不装配**（无记忆态），需要时 `agent_prompt(name, 任务, assembly="history=on")` 清标记显式打开（带记忆态）。动机：子 Agent 此前声明里根本没有 history → 一律无记忆；现在 optional 声明好，由主 Agent 按任务性质自决——普通任务不带记忆（省 token），「继续上次那个重构」类需要上下文的任务才开。

四处改动（一处语义、全链路配合）：

| 层 | 文件 | 内容 |
|---|---|---|
| 解析 | src/multiagent.py | `raw.partition("\\|")` 尾标 `optional` → `item["opt"]=True`（此前 split 直接丢弃） |
| 投影 | src/session.py | `messages_for_llm` seg 分支对 `opt=True` **跳过**——默认不进投影；与 reuse 的 current_turn_only 正交叠加 |
| 覆盖 | src/multiagent.py | `_apply_assembly_overrides` add 分支：已有段 `=on` **清 opt 标记**（打开）；`=off` 移除不变；给没声明 optional 的 agent 传 `seg=on` 仍走必装补插（原有能力保留） |
| 主 Agent 感知 | src/agent_config.py | `agents_summary` 给声明了 optional 段的子 Agent 附一行提示（`_HINT` 映射 history/ltm/rules/tail/hooks 五段）——主 Agent SYSTEM 里直接看到可用参数才知道可传 |

```
- coder: 写代码实现功能。何时调用：需要实现/修改代码（写函数、修 bug、加功能）时。
  [可选装配: history=on 可带本 agent 历轮对话记忆（默认关）]
```

**声明更新（同 commit）**：coder / explorer / reviewer / vision 四个在 file 与 user_message 之间插入 `history|optional`；**wiki-updater 不加**——它是 update_wiki 的内部 worker，一次性摘要任务无记忆需求。

- coder / explorer / reviewer / vision = `[file(persona.md), history|optional, user_message, steps]`
- wiki-updater = `[file(persona.md), tool:wiki_tree(), user_message, steps]`（不变）——**tool 项每轮投影求值**注入 wiki 树，是 wiki-updater Agent 的动态上下文自注入源（主 Agent 配方中 tool 动作项的同款机制，子 Agent 侧的实例）
- hooks：5 个子 Agent 均显式声明 `turn_end: recap_gen|async`（见上节）

端到端验证 5/5：opt 解析 / 默认投影跳过历史（无记忆）/ `history=on` 后历史进投影（带记忆）/ `=off` 移除 / summary 提示注入。`/restart` 后生效——之后派 coder 干活时主 Agent 就会看到提示行。

**段可带模式**：`history=tiered`——主 Agent 用分档 history（见 [context-engine 分档投影](context-engine.md)）。段名与模式的编辑往返在 [/agents 管理页编辑器](../features/agents-admin.md#编辑器-assembly-往返增强agentshtml同-commit)已支持。

**主 Agent 的 assembly 是完整配方**（`~/.agt/main.yml`，当前 17 项）：人设分块 text（主体/多 Agent 协作规则/披露规则…）与**动态动作交错**——`{func:load_models()}` 可用模型插值、`{func:load_agents()}` 子 Agent 清单、`tool read_file(AGENTS.md)` / `tool concat_files(.agent/rules/*.md)` 每轮读取注入——尾部才是 history=tiered/ltm/user_message/steps/tail 五段。**"清单即装配顺序"**：主 Agent 的 SYSTEM 不是一块静态人设，子 Agent 的单 md persona 模型不适用。可在 [/agents 管理页](../features/agents-admin.md#_main_-主-agent-纳入管理2026-08commit-3f0ef32) 直接编辑原始清单（保存原样写回 main.yml，`/restart` 生效）。

## system_append DSL（SYSTEM 动态追加）

```yaml
system_append:
  - workflow: wiki_tree_brief   # 执行工作流，result 追加到 SYSTEM 后（动态上下文）
  - text: "\\n以上为知识库结构。"  # 静态文本
```

新建/复活时展开固化（reuse 不重算）；工作流入参注入 {prompt, agent_id}；LLM 节点走 utility_client；失败跳过保底 md 正文。

## 实践建议

- 高频反复派活（看图/检查）→ `reuse=True`：上下文只含当前轮，token 不随复用次数增长
- 需要子 Agent 带历轮记忆的派活（「继续上次那个重构」类）→ 传 `assembly="history=on"`；普通任务默认无记忆态省 token（见上节 optional 真语义）
- **工作流节点里派活、结果由工作流自身消费**（`wait_subagents` 取）→ `agent_prompt(..., caller="user")`：fire-and-forget，子 Agent 完成不唤醒主 Agent 烧一轮 token（见 [caller 汇报对象](#caller-汇报对象与动态-enum-注入2026-08)）
- 长报告类子 Agent answer 上限 4000 字，超长指引用 `agent_query_events(id, 1)` 取全文
- 派视觉任务时，prompt 中带图片占位（如 `[图片 文件名]`），并按 vision.md description 的提示委托：`agent_prompt("vision", "请描述 [图片 文件名] 的内容")`——主 Agent 无法直接查看图片内容

