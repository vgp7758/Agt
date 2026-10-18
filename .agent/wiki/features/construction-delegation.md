# 施工委托 · create_plan / spec 通过 → 自动派 worker-`<plan_id>` 施工（2026-10-15，用户提案，commit 3dc3f7b）

> 挂点：`src/agent.py`（施工牌生成 + 幂等派发）× `src/multiagent.py`（引擎程序化派发入口）× `src/session.py`（施工模式让位）× `src/plan_tools.py`（计划注入块委托行）× `src/agents/worker/`（worker 声明，见 [多 Agent 体系](../architecture/multi-agent.md)）。配套改变主 Agent 的施工期投影形态，见 [context-engine · 施工委托让位](../architecture/context-engine.md)。

## 职责与动机（用户提案原文）

用户提案（2026-10-15）：「添加一个施工 sub-agent——一进入施工状态自动 agent_prompt 把施工牌发送给 worker 开一个 session 进行施工，主 agent 看到的是：我创建了一个计划 / 提交了一个 spec，然后把施工过程委托给 worker 了，**这样主 agent 的状态也别换来换去了**」。

两个收益：① **投影形态稳定**——主 Agent 不再在「施工视图 ↔ 常规视图」之间来回切换（细节见 [context-engine 后记](../architecture/context-engine.md)）；② **上下文分工**——施工期间每步的文件快照/施工流都活在 worker 自己的 session 里，主 Agent 只留「我建了计划 → 委托出去了」这一条认知。

## 全链路

```
主 Agent：create_plan（或 spec 通过 → 自动建 plan）
   │
   ├─ 每步工具执行后 / 每轮 run 开头 → Agent._maybe_dispatch_construction()（幂等）
   │    ├─ 活动计划有未完成步 且 尚未委托 → _construction_brief() 生成【施工牌】
   │    ├─ _prompt_subagent("worker", 牌, agent_id="worker-<plan_id>",
   │    │                   reuse="yes", caller=主 Agent, current_turn_only=True)
   │    └─ 标记 session.extra_state["_plan_dispatch"] = {plan_id, agent_id, ts, result}
   │
   ├─ 同轮旁注（_hook_notes）+ system 事件：🏗️ 施工已委托 worker-p_xxx（计划 · N 步）
   │
   ├─ 主 Agent 退出施工视图（session._construction_mode() 让位，见下）
   │
   ├─ worker 在自己 session 里按牌施工（多步 react + 自检 + 跑验证）
   │
   └─ worker 完成 → answer 按 caller 路由回主 Agent inbox → 唤醒一轮
        → 主 Agent update_plan 记账 + 向用户汇总
```

## 施工牌 → 派发 prompt：`_construction_brief()` → `_construction_dispatch_prompt()`（src/agent.py）

> **形态三变**：① 初版（2026-10-15）四段全文直接进 prompt → ② 同日二轮瘦身（用户裁定）：全文拆解，计划信息改走【装配注入】（worker.yml `func: plan_content()` / `func: plan_steps()` 每轮喂），prompt 只留一句派发指令 → ③ **2026-10-18 回归全文（用户裁定，现行）**：「把设计和步骤原文完整作为 prompt 发送给 worker，这样也不用额外装配 plan_content 了」。③ 与 ① 的差异：spec 细则此时已全文进步骤描述（2026-10-15，见[后记](#后记图纸全程可见--worker-工具面2026-10-15--二三连-commits-446c7d1--7ca0aa5dashboard-施工线实锤)），步骤原文自带 file/action/anchor/content/rationale，无需再嵌 spec 结构化段；装配侧 plan_content 退役后，**图纸唯一真源=施工牌 prompt**。

原 ① 版 `_construction_brief()` 派给 worker 的 prompt 四段（历史对照）：

| 段 | 内容（① 历史版） |
|---|---|
| 头部 | `【施工牌】计划 <id> · <标题>` |
| 设计 / 目标 | `active_plan["design"]` 全文 |
| 步骤 | 每步 `☐/▶/✅`（pending/in_progress/completed）+ 描述——worker 一上来就知道干到哪 |
| 施工详情 | **源 spec 结构化全文**（`file` / `action` / `anchor` / `content`（截 6000 字）/ `rationale`） |
| 结尾 | 分工声明 + 要求按 worker 的汇报格式给结论 |

现行 `_construction_dispatch_prompt()`（src/agent.py，③ 版 2026-10-18）：【施工牌】头部（计划 id · 标题）+ `## 设计` 全文 + `## 步骤` 逐条原文（spec 细则含其中）+ 汇报格式要求。**prompt 是派发时刻快照**（含步骤初始状态）——续派/补派不重发全文，由主 Agent 另行手写指令（如「从第 N 步继续」）；`recall_spec` 降为兜底查询通道（主路径=prompt 全文，2026-10-18 口径）。

## 派发：`Agent._maybe_dispatch_construction()`（幂等）

- **触发条件**：活动 plan 存在 且 `steps` 非空 且 非全部 completed 且 本 plan 未委托
- **两个调用点**：① 每步工具执行后（`add_step` 之后——`create_plan` 建好当步即派）；② 每轮 `run()` 开头（**续跑 / 重启后补派**）
- **幂等**：`session.extra_state["_plan_dispatch"]`（`setdefault` 原地改——防 provider 覆盖式重建抹键）记 `{plan_id, agent_id, ts, result}`，本方法每步每轮都会被调，靠该标记短路；`result` 存 `agent_prompt` 返回值前 300 字（如 `[忙]`）便于事后诊断
- **标记清理**：plan 全完成 / 无 plan / `exit_plan` → `_d.clear()`；换计划 → 新 `plan_id` 不匹配即视为未委托，重新派一条 worker 线
- **静默兜底**：`_prompt_subagent` 缺失（未装配 multiagent 的纯引擎场景）→ 直接返回；`agent_prompt` 抛异常 → `_LOG.warning` 不写标记（下一个调用点自然重试），**都不影响本轮**
- **同轮即告**：派发成功后 `_hook_notes` 追加一条旁注（「施工已委托 xxx：这条计划由 worker 执行，你不必再自己动手；worker 完成会自动汇报进你的 inbox——收到后用 `update_plan` 记账」）+ emit `🏗️ 施工已委托 worker-p_xxx` 系统事件——**主 Agent 后续步骤若不告，就会继续自己施工**（浪费 + 与 worker 打架）

## 引擎程序化派发通道：`agent._prompt_subagent`

`_resolve_tools` 尾部（src/multiagent.py）把 `agent_prompt` 实现挂到 agent 实例上（`agent._prompt_subagent = agent_prompt`）——**引擎不经 LLM 工具调用即可起子 Agent** 的通用入口，当前唯一消费端就是施工委托。`agent_prompt` 其余语义（复用/复活/新建、`caller` 路由、registry 登记、`[忙]` 提示）照旧应验。（程序化派发家族第二个成员是 doctor：[`dispatch_doctor`](doctor.md) 直接 `agent.tools.run("agent_prompt", …)` 走工具面，不占此挂点。）

## worker 声明

| 项 | 值 |
|---|---|
| 源 → 目标 | `src/agents/worker/`（随包播种源）→ `.agent/agents/worker/`（[seed_default_agents](workspace-seeding.md) 判存在跳过） |
| 目录形态 | `<name>/` 自包含：`worker.yml` + `worker.md` 人设（yml `file:` 装配引用） |
| 工具 | `tools: ''`——继承引擎工具箱（自动排除管理工具）+ 目录 `tools/` 专属工具 |
| 模型 / 步数 | `glm-official-flash` / `max_steps: 150`（长施工够用，见 [步数上限三级取值](../architecture/multi-agent.md)） |
| 装配 | `file: .agent/agents/worker/worker.md` + `history` + `user_message` + `steps` + `func: plan_steps()`——**2026-10-18 起 `plan_content` 装配退役**：计划设计与步骤全文走施工牌 prompt（首轮即得全图），`plan_steps()` 只留轻量进度视图（☐/▶/✅，截断提示「全文见你的施工牌 prompt」）；`history` 为 2026-10-15 · 三增补（断点续跑记忆接续） |
| 钩子 / 回退 | `turn_end: recap_gen(async)` + `after_tool: py_auto_diag` / `fallback: [glm-official, deepseek]` |
| 人设要点（worker.md） | 按牌施工（一步一自检）· 不重复调研（牌里没写清才探索）· 小步可回滚 · 不转包 · 验证通过后再 `git_commit`；**四段式汇报**：完成情况 / 改动清单 / 验证结果 / 遗留风险 |

## 主 Agent 侧三处配套

| 位置 | 改动 |
|---|---|
| `src/session.py _construction_mode()` | 判定末尾增一条：本 session 活动 plan 已被委托（`extra_state["_plan_dispatch"]` 的 `plan_id` 相同且 `agent_id` 非空）→ `return False`——**施工视图让位**（history 恢复装配、施工牌/施工摘要不注入、施工期内嵌 recent-file 不触发、`/context` 顶部施工模式标注消失） |
| `src/agent.py capture_runtime_state / restore_state` | `"plan_dispatch"` 纳入存档往返——重启读档后**不重复派发**（幂等跨进程成立），`_construction_mode` 也继续让位 |
| `src/plan_tools.py _format_plan_block()` | 计划 SYSTEM 注入块（tail 每轮注入）在【当前计划】后追加一行 `施工：已委托 worker-xxx（施工细节在其 session；完成会自动汇报——收到汇报后用 update_plan 记账）` |

## 记账、续派与开关

- **记账已下放 worker（2026-10-15 · 二翻转，commit 7ca0aa5，dashboard 施工线实锤推翻原设计）**：`update_plan` 从子 Agent 排除集移出——worker 直接标**共享 plan 文件**的进度（主/子双方面板同步），主 Agent 不再凭汇报代记；`create_plan` 仍排除（worker 只推进度不建计划）。详见[后记](#后记图纸全程可见--worker-工具面2026-10-15--二三连-commits-446c7d1--7ca0aa5dashboard-施工线实锤)
- **失败不自动重派**（防死循环）：worker 汇报失败 / 半途中断时，主 Agent 手动续派**同一施工线**（显式 `agent_id` 保证接在同一实例、同一 session 上）：
  `agent_prompt(name="worker", agent_id="worker-<plan_id>", prompt="从第 N 步继续…")`
- **关掉委托**：主 Agent 实例设 `_construction_delegate = False`（默认 True）——回到「主 Agent 自己进施工视图施工」的老行为
- 调参：模型 / 步数上限改 `.agent/agents/worker/worker.yml`，下次派活即生效

## 验证（2026-10-15 开发轮）

- **L3 六组断言全绿**：施工牌文本 / 派发 + 幂等（重复调不重派、`agent_id=worker-<plan_id>`、caller 正确）/ plan 完成清标记 / 三分支（开关关 · 无 multiagent · 无计划）/ 施工态让位（已委托 False、未委托 True、别的 plan 不影响）/ plan 注入块委托行
- **worker 声明解析**：路径命中、yml 解析、assembly/hooks/fallback 正常、`tools=''` 继承工具箱且排除 `create_plan` / `agent_prompt`
- **L2 隔离实跑**（临时 workspace 直跑最新源码）：启动 → 收消息 → before_turn 三钩子执行 → 轮启动**无 NameError / traceback**（新调用点每轮每步都走到）

## 后记：图纸全程可见 + worker 工具面（2026-10-15 · 二/三连，commits 446c7d1 + 7ca0aa5，dashboard 施工线实锤）

首个实战施工线（**dashboard 看板**，plan `p_c0542ede`）三次断点续跑暴露出**图纸三层断链**——worker 拿不到完整施工依据，被迫满世界找细则（实锤：worker 找不到 `recall_spec` 而绕路）。两连修齐（commit `446c7d1` + `7ca0aa5`）：

| 层 | 断链 | 修复 |
|---|---|---|
| spec→plan 转换 | `_step_to_plan_desc`（src/spec_tools.py）把 spec 结构化 step 压成 plan 描述时 **content/rationale 截 150 字** | **全文进 description**——plan 步骤自带完整细则，worker 不必回查 spec（`446c7d1`） |
| 装配注入 | 施工牌全文塞 prompt——与装配口径打架、重复 | worker.yml 装配段增 **`func: plan_content()`**（id/标题/design + 每步全文）与 **`func: plan_steps()`**（轻量进度视图，自行截断）；prompt 瘦身成一句派发指令（见上章） |
| worker 工具面 | `update_plan` / `recall_spec` 均在 `_AGENT_TOOL_NAMES`（src/multiagent.py）排除集——worker 标不了进度、查不了 spec | **`update_plan` 下放**（worker 直接标共享 plan 文件，主/子双方面板同步；`create_plan` 仍排除）+ **`recall_spec` 例外保留**（纯只读查 spec 全文，兜底通道——主路径=施工牌 prompt 全文，2026-10-18 口径更新）（`7ca0aa5`） |

**byte-stable（缓存断点治理·二连，commit `7ca0aa5`，用户裁定）**：`plan_content()` **每步不标状态**（✅/☐ 状态标记归 `plan_steps()`）——worker 用 update_plan 推进进度不再扰动 plan_content 段的字节，worker 装配前缀缓存不被施工推进打断；全部步骤完成后整段收敛为一行标题（2026-09-13 完成态瘦身口径延续）。*（2026-10-18 起 plan_content 装配退役，本段治理随之失效——整段直接没了。）*

**实证（2026-10-15 当日）**：dashboard 施工线 worker 用 `update_plan` 标满 10/10（工具面修复生效实测）；三断点（进程波动 ×2 + /restart）后每轮 run 开头自动补派接续，无一丢线。

**连带对账（本节装配口径已被[后记四](#后记四施工牌回归全文派发worker-装配退役-plan_content2026-10-18用户裁定)取代）**：本节时期 worker.yml assembly = `file(人设) + func:plan_content() + history + user_message + steps + func:plan_steps()`，hooks = `turn_end: recap_gen(async)` + `after_tool: py_auto_diag`——本文上文「无 history」「仅 recap_gen」为初版形态的历史描述；worker 自带 history 后断点续跑的记忆接续不再依赖施工牌重述。**2026-10-18 起装配段退役 plan_content**（计划全文走施工牌 prompt），现行 assembly 见[后记四](#后记四施工牌回归全文派发worker-装配退役-plan_content2026-10-18用户裁定)。

## 后记二：首例回执丢失——根因反转：`_route_answer` registry 静默丢通知，闭包兜底修复（2026-10-16，plan p_d721b263）

**首个框架施工线交付 + 首例回执事故（已闭环·根因反转）**：plan `p_d721b263`（[explore 重写](explore-tool.md)：嫁接改投影层内插 + run_id 旁车断点续跑）派 `worker-p_d721b263`，worker 独立 session 全 8 步完成、单测+真 LLM 冒烟全过、本地 commit `ba2ede1`——但 caller=`_main_` 的 answer **没进主 Agent inbox**，用户先发现（「你传的是 _main_ 啊，answer 没有 inbox 吗？」）。

### 根因反转：FS 抖动是伪因——真凶是 `_route_answer` registry 静默丢通知

上轮定性「完工正落 FS 抖动窗、回执链某环节撞异常被吞」**被推翻**——用户两句话点破：① 落盘/事件环节有异常也不该阻塞通知（两者本就解耦）；② 跟进 worker 用 agent_ask 是纯模型调用不真执行（此前靠 worker「三路自查」拿到的线索全部作废）。重新取证：

| 证据 | 结论 |
|---|---|
| worker meta.json：`status=done` + `caller_id=_main_` | `_bg` 线程跑完且路由代码**必然执行过**（`_route_answer` 在写 done 之前），通知与落盘解耦、异常独立捕获——丢的不是落盘 |
| 主 Agent inbox=0、inbox.jsonl 不存在 | `push_message` **从未成功** |
| `/api/dash`：registry **无 `_main_` 活条目** | `reg.lookup("_main_")` 落入条目缺失分支 |
| 原代码该分支 | **静默蒸发**：无日志、无动作、无兜底——answer 出来了但 caller 永远收不到 |

即：**registry 索引失效（查不到 `_main_`）→ 通知无声丢弃**。registry 为何丢了 `_main_`（疑似 `/restart` 后注册/恢复路径问题）——悬案待深挖，修复已堵死丢失面，详见 [多 Agent 体系 · 再发：registry 条目缺失静默丢通知](../architecture/multi-agent.md#再发2026-10-15--worker-p_d721b263registry-条目缺失静默丢通知_route_answer-闭包兜底)。

### 修复：`_route_answer` 闭包兜底三层防御（src/multiagent.py，已随 v0.35.3 推送落远端）

1. registry 命中 → 走原路；2. 条目缺失/`agent=None` 且 **caller=派发者自身**（施工委托 `caller=self.agent_id` 正是此形态）→ **闭包 agent 实例直推**——派发时闭包持有的引用才是真源，registry 只是加速索引；3. 跨 Agent 显式 caller 查不到 → 只告警不硬推（推错比丢更糟）。三条路径全补日志，不再有无声分支。**需 `/restart` 生效**。

### FS 抖动的定性收尾（伴生现象，非本案根因）

cmd（及部分 Python 调用）看不见 `.agt` 数据根子目录、**PowerShell/robocopy 可靠访问**——数据全程无损（已用 robocopy 拷出证据完成上述分析），疑似**杀毒实时防护/索引服务**对目录的间歇扫描独占。建议：`C:\Users\vgp77\.agt` 加杀毒白名单；后续再现可先换 PowerShell 探一遍再定性。

### 损失盘点与待办

代码零损失（`ba2ede1` 在 git）；主 Agent 内存 session 完整。直接代价是唤醒轮未发生 → `update_plan` 记账与向用户汇总未自动进行（本次由用户指出后人工介入）。**待办**：① worker 施工成果验收（explore 重写 `ba2ede1`，见 [explore-tool.md](explore-tool.md)）；② registry 丢 `_main_` 根因（另开一轮）；③ `/restart` 后新代码生效再跑一条施工线实测回执闭环。

## 后记三：中断/失败静默——caller 非主 Agent 时主 Agent 兜底直推 + 通知附近期上下文（2026-10-16，marketplace 施工线实锤，commit 1c6d7b0）

**现象**：marketplace 施工线（plan `p_1945c5a0`）的 worker 中断——用户在看板看到 ❌，主 Agent 却**零感知**（inbox 无任何通知），续跑只能靠用户手动点「继续」；worker 随后一次 RuntimeError 异常同样无声。

**根因（第二种静默，与[后记二](#后记二首例回执丢失根因反转_route_answer-registry-静默丢通知闭包兜底修复2026-10-16plan-p_d721b263)互补）**：`_bg` 完成路径的通知只对 **caller** 路由（`_route_answer`）——后记二是 caller=`_main_` 但 registry 条目缺失导致静默丢；本案是 **caller 压根不是主 Agent**（marketplace 施工委托链的 caller 记为系统侧）——主 Agent 天然不在通知路径，中断/失败信号没有任何消费端。正常完成回执有调用方接管，**中断/失败却是「需要有人接管重派」的信号**，静默即失控。

**修复（src/multiagent.py，commit `1c6d7b0` 已推送）三层**：

| 层 | 改动 |
|---|---|
| 上下文袋 `_interrupt_ctx(sa, task_text)` | 中断/失败通知附近期上下文：**任务头 150 字 + 最后两步各 260 字 + recap**——主 Agent 不用 `agent_query_events` 翻事件就能判断怎么重派 |
| 中断分支 | 原 2026-09-02 中断检测（caller 收中断通知 + registry 标 failed）叠加 `_interrupt_ctx` |
| 异常分支 | `res = [失败] <异常类型>: <信息> + 近期上下文`——**失败也立即路由**（旧版失败不通知 caller）；同样走主 Agent 兜底 |

**主 Agent 兜底**：caller 非主 Agent 时，中断/失败**额外直推 `_main_` inbox**（`source=subagent:interrupted`）——施工委托、用户 `@name` 派活、跨 Agent 委托全覆盖；**完成回执仍按 caller 路由不变**（兜底只针对中断/失败这两个接管信号）。以后 worker 再断，主 Agent 收到的通知自带「中断原因 + 正在干什么 + 干到哪了」，可直接带上下文重派。

**生效**：需 `/restart`（与同日 ccs / marketplace 改动一起重启生效）。

## 后记四：施工牌回归全文派发——worker 装配退役 plan_content（2026-10-18，用户裁定）

**用户提案（2026-10-18）**：「我想 create_plan/commit_spec 开始施工的时候，把设计和步骤原文完整作为 prompt 发送给 worker 吧，这样也不用额外装配 plan_content 了」——对[后记](#后记图纸全程可见--worker-工具面2026-10-15--二三连-commits-446c7d1--7ca0aa5dashboard-施工线实锤)「装配注入」路线的再翻转：图纸从【装配逐轮注入】改回【派发 prompt 一次性携带】。worker 首轮即得全图，装配侧少一段随计划演进的全图维护（后记的 byte-stable 治理随之失效——整段直接没了，比治理更省）。

四文件改动：

| 文件 | 改动 |
|---|---|
| `src/agent.py _construction_dispatch_prompt()` | 重写回全文版：【施工牌】头部（计划 id · 标题）+ `## 设计` 全文 + `## 步骤` 逐条原文 + 汇报格式要求——**prompt 是派发时刻快照**（含步骤初始状态），续派/补派由主 Agent 另行手写指令（如「从第 N 步继续」），不重发全文 |
| `src/agents/worker/worker.yml` | 装配段移除 `func: plan_content()` → 现行 assembly = `file(人设) + history + user_message + steps + func: plan_steps()`——plan_steps 轻量进度视图保留（☐/▶/✅ 状态归它） |
| `src/agent_config.py` | `plan_steps()` 截断提示改「…（全文见你的施工牌 prompt）」（原指 plan_content 装配） |
| `src/multiagent.py` | `recall_spec` 兜底注释同步（主路径=施工牌 prompt 全文，2026-10-18 裁定）——兜底通道本身不变 |

与 ① 初版全文牌的差异：spec 细则自 2026-10-15 起**全文进步骤描述**（`_step_to_plan_desc` 不截断），步骤原文已含 file/action/anchor/content/rationale——无需再嵌 spec 结构化段。`plan_content()` 函数本身保留（FUNC_REGISTRY 通用函数，其他声明仍可引用），只是 worker 不再装配。

**验证（四绿，2026-10-18 开发轮）**：L3 双态渲染断言（spec 型 = design + 3 步全文 + 汇报格式；轻量型 = 无 design 单步）/ worker.yml 真实 `_parse_assembly` 解析（段序 `[file, history, user_message, steps, plan_steps]` 合法、plan_content 零残留）/ worker.md 双份（repo + 播种源）无 plan_content 引用 / py_diag 双文件绿。

**生效**：`/restart`（引擎代码 + worker.yml 播种源同需）；commit 本地待网补推。下次 `create_plan` / spec 通过派 worker 即用新施工牌。

## 注意事项

- **需 `/restart` 生效**；重启后**若有活动 plan 会自动派 worker**（正是设计意图）
- **施工模式全部机制保留**：委托只是新增的一个「出口」——未委托 / 关闭委托 / 当前场景无 `_prompt_subagent` 时，主 Agent 照旧进施工视图自己干
- **worker 自己不进施工模式**：plan 归属主 Agent，worker 的 session 里没有活动 plan → 按常规子 Agent 投影（人设 + 当前轮）
- 一个计划一条 worker 线（`worker-<plan_id>`）；换计划会得到新的 worker 实例——旧线的汇报仍会路由回来，按 plan_id 分辨

## 相关页面

- [多 Agent 体系](../architecture/multi-agent.md) —— 子 Agent 声明/复用/复活、`agent_prompt` 语义、inbox 唤醒链（worker 的汇报路径）
- [context-engine · 施工模式投影与委托让位](../architecture/context-engine.md) —— 施工视图的判定与装配细节
- [spec 工具集](spec-tools.md) —— spec 通过 → 建 plan 的上游
- [workspace 播种](workspace-seeding.md) —— bundled agents 播种（worker 随包）
- [用户交互](user-interaction.md) —— 后台通知/唤醒与系统气泡渲染
- [dashboard](dashboard.md) —— 首个实战施工线（plan p_c0542ede：worker 三断点续跑 + update_plan 标满 10/10，即上文后记的实锤来源）
- [后台任务完成回执按发起者路由](background-scheduler.md) —— worker 施工期间起的后台任务，完成回执归 worker 不再吵主 Agent

