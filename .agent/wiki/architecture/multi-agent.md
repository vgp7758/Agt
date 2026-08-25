# 多 Agent 体系

> src/multiagent.py + src/registry.py + src/agent.py。docs/architecture/06 有基础版，本页收录 2026-08 全部演进（全异步/reuse/复活/assembly/system_append/registry 修复）。

## 声明与生命周期

子 Agent 声明：`.agent/agents/<name>.yml`（v2.1：纯配置，persona 拆独立同名 .md，.yml 优先加载）或旧版单 `.md`（frontmatter：name/description/model/tools + assembly/system_append DSL）。**2026-08（commit f177674）存量 5 个子 Agent（coder/explorer/reviewer/vision/wiki-updater）已全部规范化为 v2.1**——必装段显式入 assembly（所见即所装）、recap_gen 显式入 hooks（详见 [/agents 管理页 · 声明规范化](../features/agents-admin.md#声明规范化5-个子-agent-全部转正所见即所装)）。**主 Agent 声明：`~/.agt/main.yml`**（全局，非 .agent/agents/ 成员；assembly 是完整配方，见下）。可视化管理走 [/agents 管理页](../features/agents-admin.md)。

```
agent_prompt(name, prompt, tools?, agent_id?, reuse?, assembly?)
  ├─ reuse=true 且有同名空闲活实例 → 直接派活（沿用 agent_id/session）
  ├─ reuse=true 无活实例但有同名历史条目 → 复活（读 md + Session.load 磁盘历史）
  └─ 否则新建（session 嵌套 主session/agents/<id>/，meta.json 记 _agent_meta）
全异步：立即返回；完成后按 caller_id 路由 answer 入调用者 inbox（下轮自动激活）
要结果才继续 → wait_subagents(agent_ids)
```

- 主 Agent id=`_main_`；registry 是唯一事实源（团队看板/recap/路由都读它）
- `_agent_meta`（agent_id/name/model/task/caller_id/recap/status）无条件写子 meta.json → 读档 `_restore_subagents` 恢复团队
- 子 Agent 的通信/会话工具**重绑自身**（继承的闭包绑主 Agent，会查错 session）

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

## 通信（agent 间）

| 工具 | 语义 | 落盘 |
|------|------|------|
| agent_ask | 无状态询问（对方上下文快照+问题→LLM→回你） | 否 |
| agent_notify | 有状态提示（入对方 inbox，等效用户插话） | 是 |
| agent_query_events / _tool_detail | 只读查对方轮次/工具调用详情（历史 Agent lazy load） | — |
| list_team | 团队清单（exclude 自己） | — |

## recap（每轮一句话总结）

finish_turn 后 daemon 线程异步生成（utility_client，scene=recap）——不进自己上下文，但显示在队友的 teammates_block。子 Agent 完成后 recap 写入 `_agent_meta` 随 meta.json 持久化。

**recap_gen 挂钩来源两代**：此前是 `agent_prompt` 派活时的**运行时注入默认**（yml 未声明 hooks → 注入 recap_gen）——行为正确但管理页看不见（yml 里没有）；2026-08（commit f177674）起 5 个子 Agent 的 `turn_end: recap_gen|async` 全部**显式写进声明**——注入逻辑幂等（已有即跳过），不会双跑，/agents 管理页所见即真实运行配置。

**运行观察（2026-08-21，团队看板）**：recap 全面工作——新条目（vision_4/6/7/9 等）各自带完整检查任务描述（配图逐张检查、分享卡胶囊区域检查）、caller 指向 `_main_`；`_agent_meta` 持久化上线**之前**的历史条目（coder_*/vision_3/5/8）无 meta → 列表显示「(历史任务)」兜底——两代数据的分界线在存档里清晰可见，验证了 meta 持久化前后行为符合预期。

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
- 长报告类子 Agent answer 上限 4000 字，超长指引用 `agent_query_events(id, 1)` 取全文
- 派视觉任务时，prompt 中带图片占位（如 `[图片 文件名]`），并按 vision.md description 的提示委托：`agent_prompt("vision", "请描述 [图片 文件名] 的内容")`——主 Agent 无法直接查看图片内容
