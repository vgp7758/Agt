# 上下文引擎与缓存优化

> src/session.py 核心设计。详细分档机制见 [docs/architecture/02-session-memory.md](../../../docs/architecture/02-session-memory.md)，本页补充 docs 未收录的 2026-08 演进（分组衰减 / usage 归一化 / provider 缓存坑 / 折叠实证 / 轮边界统一重排 / 轮内应急折叠保命阀 / 投影转储文件名 / 投影分段统计 / 估算与校准口径闭环）。

## 投影总览（messages_for_llm 装配顺序）

```
[system]           md/人设正文（子 Agent 可被 system_append DSL 追加动态段）
[rules]            AGENTS.md + .agent/rules/*.md + skills 清单（每轮重读，当轮生效）
[summary]          历史会话摘要（无分档时的老路径）
[tiered history]   分档投影（需 provider 配 max_effective_context_window）
[current turn]     user_message + before_turn hint + steps（工具调用按分组衰减）+ pending hints
[tail ambient]     合并成一组 <system-reminder>：时间/后台任务/计划/episodic 召回
```

assembly DSL（子 Agent 声明）段可带 `|optional` 尾标——**2026-08（commit 1e3b206）起真语义：标记即默认不装配**（`messages_for_llm` 的 seg 分支对 `opt=True` 的项跳过），`agent_prompt assembly="seg=on"` 清标记打开、`=off` 移除；未标记段列出即装，必装 system/user_message/steps 未列出自动补插；`reuse`（current_turn_only）与 opt **正交叠加**（reuse 时 history 强制关）。详见 [multi-agent · assembly DSL](multi-agent.md#assembly-dsl上下文装配配方)。

装配时顺手记录分段统计到 `_proj_stats`（`/context` 直接读——live 口径，见 [投影分段统计](#投影分段统计-context-改读真实投影缓存commit-4212f65)）。

episodic 召回行（`[epi·长期记忆]`）由 before_turn 检索工作流产出、注入 tail ambient——演进史与中文命中率坑见 [长期记忆](../features/longterm-memory.md)。

## 投影转储文件名与 t/s 标记（commit 4aced81）

当 `/config dump_projections true` 时，每次调用 LLM 前会转储完整投影到 `sessions/<ts>/projections/` 目录，文件名格式：

```
t{轮号}_s{步号}_{微秒戳}.txt
```

- `t{轮号}`：已完成轮数（`len(turns)`，进行中的是第 turn+1 轮），与 llm_calls.jsonl 中的 `turn` 字段对应
- `s{步号}`：当前轮已完成步数（`len(_current.steps)`），与 llm_calls.jsonl 中的 `step` 字段对应
- `{微秒戳}`：调用时间戳（去重用）

**同源保证**：react 主循环 3 处 `llm.chat` 调用点（主调用 / DSML 重试 / 空回答重试，`src/agent.py`）均传 `turn=len(turns), step=len(_current.steps)`——与 `_dump_projection` 完全同一取值；`src/llm_client.py` `chat()` 经 `_turnstep_ctx`（与 `_scene_ctx` 同构 contextvar，finally 清理，不进 API 请求）落盘 jsonl，`src/server.py` `/api/stats` 透传。**在 react 循环新增 LLM 调用点时记得带上这两个参数**，否则该点 tooltip 无轮步标记、无法映射到投影文件。

**与 /stats tooltip 对齐**：从 `/stats` 折线图 hover 获取 `· t206 · s6` 标记 → 直接打开 `projections/t206_s6_*.txt` 查看当时完整投影，快速定位升档/折叠等事件断点（详见 [运维与排障](../guides/ops.md#stats-页webui-统计按钮)）。

## 投影分段统计：/context 改读真实投影缓存（commit 4212f65）

`/context` 的分段统计从「发指令时现算」改为「真实投影装配时顺手记录」——看到的是**事实**（真实发给模型的口径），不是事后模拟；两者在特殊路径（reuse、保命阀触发的轮）本来就可能不一致。

**记录侧（`messages_for_llm`，src/session.py）**：

- 装配循环每段 `extend` 前记一个 `(段名, 全局起始 idx)` 标记；装完统一切片，计算各段 msgs/chars/tokens，存入 `session._proj_stats`（带 `ts`/`turn`/`step` 元信息）
- 历史段的子段（折叠摘要 → 超深档 → 档4…档1）由 `_render_tiered_history` 渲染时经 `_hist_marks` **临时通道**填充（装配进行中才非 None）；窗口模式由 `_history_window_msgs` 同理记录（历史摘要(窗口外) / 近窗口段起点）。分组渲染与逐轮渲染的消息序列完全一致——**byte-stable 不变**，专门验证过
- 保命阀循环内会多次调 `_render_tiered_history`（各自塞标记），`to_history` 里 `_hist_marks.clear()` 清空，只保留**最终渲染**的标记为唯一真相

**读取侧**：

- `projection_breakdown()` 优先返回 `_proj_stats`（浅拷贝，调用方改动不污染缓存），只有本进程还没跑过投影时才回退现算兜底（重算一遍段函数）
- `/context`（src/commands.py）输出新增来源标注，并结合 llm_calls 最近 react 回包的实测 prompt_tokens 校准（取代纯估算）：

```
最近 react 调用（3分钟前，proxy）：prompt 233,169 tok，缓存命中 98.8%
段落统计（采自上次真实投影 t336·s2，4分钟前）   ← live；无缓存则标注「现算估算——本进程尚未跑过投影」
```

**调试中抓到的真 bug（切口错位）**：第一版子段标记记的是**结束位置**而非开始位置——切口整体错位一格：首段标记把前一个顶层段的消息吞走（rules 凭空显示 10 条），末段（档1历史）起点与下一段重合被静默丢弃。修复=统一为 **extend 前快照**，并用 8 场景验证 **Σ段 chars == total chars、Σ段 msgs == total msgs**（切口无缝无重叠）：分档模式（fold 关/开）、带折叠摘要、窗口模式（摘要+近窗口）、reuse 模式、byte-stable（分组标记渲染 vs 逐轮渲染逐条相等）、breakdown live 优先/兜底。

**收益**：`/context` 零重算（读缓存 + 实测 token 校准），数据口径=真实发给模型的那份。

### 段统计 schema 重复计入修复（2026-08，commit 23bd994）

**表象（t370 实测投影 /context 输出）**：段落构成里 14 个 asm 段每个都 ~19,426~19,841 tok——人设 826 字的段不可能 19.5K，均匀得反常；且**各段之和（≈893K）≠ 合计（449,285）**，口径裂缝。

**根因**：schema 校准修复（见 [估算与校准口径闭环](#估算与校准口径闭环tools-schema-补齐2026-08)）让 `_estimate_tokens` 分子**无条件**加 `_tools_schema_chars`。对整包判阈这是对的（schema 请求级只计一次）；但 `projection_breakdown` **逐段**调用它 → 22 个段各带一份 schema 底噪 ≈19.5K，真实的小段尺寸被完全淹没。

**修复（session.py，commit 23bd994）**：`_estimate_tokens(msgs, include_schema=True)` 增参——默认 `True`（旧行为，整包判阈口径不动）；段统计改传 `include_schema=False`（纯内容口径），schema **单列一段** `tools schema(请求级·计一次)` 展示。

**验证四项全过**：内容段纯口径 / schema 段单列 / **Σ段 = 合计**（口径闭环）/ 整包判阈估算不受影响（默认 `include_schema=True`）。修复后 asm 段显示真实小尺寸（几百 tok 级），schema 占比（~4%，≈19.5K）单独可见（此前藏在每段底噪里查不到）。需 `/restart` 生效。

## 分档投影（轮间）

- 每档字数上限：1500 → 750 → 375 → 187（`_tier_limit`，detail_base 减半递进）
- 同档位**冻结渲染**（byte-stable）：档内内容字节级不变 → 前缀缓存可命中
- 全档满 → 折叠成结构摘要（fold），原文仍在 turns 可 recall

### 升档（graduate）与折叠：轮边界统一计划（2026-08，commit 1e9af8f）

**核心经济模型：轮内零调整，只在轮边界做一次全局重排（升档+折叠统一）。**

升档逻辑从**轮内 `_build` 超窗触发**移到**轮边界 `_plan_fold` 统一计划**：

1. **轮边界统一计划**：`_plan_fold` 在轮边界统一判断——先升档到 75%（档位+1，之前各档全部 +1），再折叠到 75%（档梯满时）。`_planned_graduates` 记录本轮计划。⚠️ 判阈用 `_estimate_tokens`，其估算分子必须与 `observe_llm_usage` 校准同口径（tools schema 补齐见 [估算与校准口径闭环](#估算与校准口径闭环tools-schema-补齐2026-08)）。
2. **轮内 `_build` 零调整**：`_build` 以 `_planned_fold` / `_planned_graduates` 为**起点**，轮内不再自行触发升档/折叠，**零调整**。⚠️ 唯一例外：轮内投影顶满 `max_effective_context_window` 时，应急折叠（保命阀）仍会触发止血，见 [t228 实证](#轮内应急折叠保命阀t228-实证2026-08)。
3. **收益**：轮内字节稳定 → **前缀缓存整段命中**。升档与折叠都只在轮边界发生一次，避免轮内中途重排打断缓存。

> 旧逻辑（已废弃）：升档由轮内 `_build` 超窗触发，折叠在档梯满后由轮内投影超窗随机触发——两者都在轮内中途发生，打断前缀缓存。

#### `_planned_fold=0` 但 /context 显示超窗：轮边界估算时点（2026-08 澄清）

**现象**：t370 进行中 `/context` 显示投影 449,285 tok（112.3% win），但 `_planned_fold=0`。用户疑「应在本轮开始就折叠到 300K」。

**结论：数字自洽、符合设计**——`_plan_fold` 在轮边界（`start_turn`）估算，**那时当前轮还不存在**：

- 轮边界估算 ≈ 449,285 − 当前轮 user(19,788) − 当前轮 steps(172,668) ≈ 256,829 < 300,000（75%×400K）→ 不需折，`_planned_fold=0` ✓
- 超窗增量是**当前轮自己**：这轮 5 步大工具调用把 steps 段撑到 ~172K，投影从 ~257K 涨到 449K

**轮内零调整正是缓存经济核心**：75% ~ panic(900K) 之间纯追加、不动前缀；超窗部分几乎全命中缓存（react 99%），实际成本不爆炸。下一轮边界（t371）时这 5 步已归档进历史，估算 ≈450K > 300K → 届时 `_plan_fold` 才实际折叠/升档，`/context` 里的 `_planned_fold` 即非 0。

### 估算与校准口径闭环：tools schema 补齐（2026-08）

**表象（用户报告）**：上一轮末实测 prompt 530,469 tok（win=400,000），折后期望落在 ≈400K×0.75=300K，但新一轮起始 token 达 412K。用户怀疑按 `530,469×0.75` 算——排查确认：**目标算法没错（`_plan_fold` 恒用 win×0.75，不是上轮实测值），漏的是估算分子**。

**根因：`_estimate_tokens` 分子不含 tools schema，与校准口径错位**：

- 折叠判阈/保命阀都走 `_estimate_tokens` = chars(投影) ÷ `_chars_per_token` ← 分子**不含** tools schema（schema 不在投影 msgs 里，但随请求计费进 prompt_tokens）
- 比率校准走 `observe_llm_usage`：校准比率 = (chars + extra_chars) ÷ prompt_tokens ← 分子**含** schema
- 估算分母按校准比率换算，分子却缺 schema → **系统性低估 ≈147K token/请求**（本 session 实测：估算 271,623 → 实际 419,284；130+ 工具 schema ≈264K 字符）。折叠计划永远「以为达标」实超窗——正是用户看到 412K（实际 419,284，超 win=400K）的来源

**修复（session.py + agent.py，4 处）**：

1. `Session.__init__` 增 `self._tools_schema_chars = 0`
2. `_estimate_tokens` 分子改 `(chars + _tools_schema_chars)`——与校准同口径（估算/校准统一为 `(chars+schema)/比率`）
3. `agent.py` 轮初算完 `tool_schema_chars`（`len(json.dumps(tool_schemas))`）写入 `session._tools_schema_chars`
4. 工具重注册后同样刷新（schema 变了同步，与轮初同式）

**验证闭环（本 session 三组实测）**：

| | 估算（引擎判断） | 实际（API 回包） |
|---|---|---|
| 修复前 | 271,623 tok → 判 ≤300K 达标，**折叠 0 轮** | 419,284 tok（含 schema）→ 超 win=400K |
| 修复后 | 298,249 tok（含 schema）→ 触发**升档 1 档 + 折叠 202 轮** | ≈298K ✓ 正好落在 300K 目标 |

**教训**：`_plan_fold`（75% 目标）与保命阀（顶窗判阈）共用 `_estimate_tokens`——估算分子必须与 `observe_llm_usage` 校准分子同口径，否则每步比实际少算 ~120K、折叠计划恒晚一步。改动在 session.py/agent.py，需 `/restart` 生效。

#### include_schema 参数：段统计改用纯内容口径（2026-08，commit 23bd994）

后继纠偏：本节的 schema 校准让 `_estimate_tokens` 分子无条件含 `_tools_schema_chars`，复用进 `/context` 逐段统计时 schema 被重复计入 N 段（段间之和 ≠ 合计）。已增 `include_schema` 参数（默认 `True` = 判阈旧行为），段统计改传 `False` 纯内容口径 + schema 单列一段——详见 [投影分段统计·段统计 schema 重复计入修复](#段统计-schema-重复计入修复2026-08commit-23bd994)。

### 折叠事件与缓存命中（t206 实证，2026-08）

对 t206_s6→s7 命中率波动的定位结论：**是折叠（fold）事件，不是升档**。

- **触发条件**：档梯已满（早期轮全部毕业衰减到最深档 187 字下限）后投影仍超 `max_effective_context_window` → 触发全档折叠：630 条早期消息折叠成 1 条 8020 字结构摘要（原文仍在 turns 可 recall）。在新模型下，此折叠由轮边界 `_plan_fold` 计划触发，不再轮内随机。
- **缓存断点**：精确定位在历史段首条消息起点（投影 `[tiered history]` 段的第一条消息）——折叠重写了整个历史段，断点之后前缀全部失效
- **代价仅一步**：s7 当步 98.4% miss（512746/521091 字符全价重算），/stats 折线呈单点深跌
- **核心性质（已验证）**：折叠摘要 byte-stable → s7→s8 命中率恢复至 ~99.9%。**折叠是一次性成本事件，不破坏后续缓存**
- 观测手段：/stats 缓存命中率折线 + llm_calls.jsonl 归一化 usage + 投影转储文件名（见 [ops 可观测性](../guides/ops.md#可观测性)）

### 正常轮边界路径（t224 实证，2026-08）

与 t206 折叠事件形成对比：当投影未超 75% 阈值时，轮边界默认走平滑路径，前缀缓存大幅命中。

- **会话数据**：`sessions/20260811_013200`，投影对比 `t223_s6_*.txt` → `t224_s0_*.txt`
  - t223_s6：3515 条消息 / 730,848 字符（进行中轮活体渲染）
  - t224_s0：3513 条消息 / 709,912 字符（归档轮标准渲染）
  - 前 3495 条消息【逐字节一致】= 稳定前缀 701,605 字符
  - 断点后重算仅 8,307 字符（1.2% miss，98.8% 命中）
- **断点定位**：消息 [3495]（上一轮 user 之后）——活体渲染的 `before_turn` 旁注（1.3KB）与插话标签在归档后消失，投影形态从活体追加转为标准 `user→assistant(tool_calls)→tool` 序列
- **本质**：断点为**投影形态转换**，非折叠/升档重排。历史段（[3]~[3493]）全部 SAME，字节级稳定
- **与 t206 对比**：
  - t206：档梯满 → 轮边界 `_plan_fold` 触发全档折叠 → 历史段首条断开 → 98.4% miss（一次性成本，下一步恢复 ~99.9%）
  - t224：投影未超 75% → 轮边界无折叠/升档 → 只有活体→归档的结构性重算 → 98.8% 命中
- **验证结论**："轮边界统一计划 + 75% 目标"有效——轮内零调整、轮边界只在必要时重排，正常轮边界接近理论下限

观测手段同上：/stats 缓存命中率折线 + 投影转储文件名定位断点

### 轮内应急折叠（保命阀）：t228 实证（2026-08）

**机制**："轮内零调整"是理想模型，但保留一个兜底例外——**轮内投影即将顶满 `max_effective_context_window` 时，`_build` 触发一次性应急折叠（"保命阀"）止血**。t228 实证暴露其缺陷：目标是"折到刚好进窗"，长轮余量不足会导致**同轮连续触发两次**。

**t228 数据链**（超长轮：连续调试工作流，每步工具结果 +34K tokens）：

| step | prompt tokens | Δ | 命中率 | 事件 |
|---|---|---|---|---|
| s0 | 443,875 | – | 98% | 轮边界计划生效（t227 末 599K → 折到 74% ≈ 444K ✓） |
| s1–s5 | 478K → 610K | +34K/步 | 93–95% | 纯追加（巨型工具结果）；s5 顶满窗口 |
| s6 | 552,829 | −57K | **3%** | 🔴 保命阀折叠#1（消息 3613→2984，只回收一档） |
| s7 | 574,299 | +21K | 96% | 恢复（折叠后新前缀 byte-stable） |
| s8 | 578,294 | +4K | **3%** | 🔴 保命阀折叠#2（2988→2602，摘要扩容，token 几乎没降） |
| s9–s10 | ~579K | +数百 | 100% | 恢复收尾 |

- **断点特征**：s5→s6、s7→s8 投影 diff 的第一条 DIFF 均在消息 **[3]（历史段起点/折叠摘要处）**，消息数骤减 629/386 条——教科书级折叠特征，与 t206 同款断点位置（见 [t206 实证](#折叠事件与缓存命中t206-实证2026-08)）
- **根因链**：超长轮每步 +34K → 五步 +166K 顶满窗口 → 折叠#1 只回收一档（−57K，553K 时余量仅 ~50K）→ 两步 +21K 后再次顶满 → 折叠#2
- **成本核算**：两次 3% 深跌 ≈ 各 55 万 token 全价重算；换取的是其余 9 步 93–100% 命中（不折叠投影会涨到 65 万+ 继续全价）——**是"止血手术做了两次"，非设计失效**
- **缺陷症结**：应急折叠目标太保守——退出条件只到 `estimate ≤ window`（刚好进窗），不留余量
- **改进建议（2026-08 分析结论，未实施）**：
  - **方案 A（推荐）**：保命阀一次折到位——退出条件改 `estimate ≤ window * 0.9`（一行改动），留 ~60K 余量 ≈ 2 步增量，可避免 s8 类二次触发，普通轮零影响（注：t228 后估算分子已补齐 tools schema，`estimate` 已按真实口径判阈，见 [估算与校准口径闭环](#估算与校准口径闭环tools-schema-补齐2026-08)）
  - 方案 B：轮边界计划更激进——`_plan_fold` 目标 75%→70%，所有长轮余量 +30K，但普通轮白折更多

观测手段同上：/stats tooltip 取 `t{N} · s{M}` → `projections/t228_s*.txt` 投影 diff 定位消息级断点 + llm_calls.jsonl 归一化 usage。

## 分组衰减（轮内，2026-08 新）

老方案按步距衰减（distance×15 字符）——每走一步前面所有步 limit 全变，**轮内缓存每步全 miss**。

新方案：每 `GROUP_STEPS=10` 步一组，组号差决定 limit：

| 组号差 | limit |
|--------|-------|
| ≤1（当前组+上一组） | 全量（当前轮 FULL_STEP_CAP_CHARS=32000 上限；老轮用档位 base） |
| ≥2 | `base - 10 × detail_step × 组号差`（≥DETAIL_FLOOR=20） |

组内 10 步字节稳定 → **从"每步全变"变"每 10 步变一次"**，轮内跨步缓存命中大幅提升。

## 折叠摘要 tail 优先级（recap → answer 代码摘要 → 中断标注，2026-08）

`_folded_summary(fold_count)` 生成被折叠早期轮次的结构概览（纯结构信息、无需 LLM；逐字原文靠 recall 召回）。每轮一行：`user[:80]` + `(已折叠N次工具调用) ` + tail。tail 的优先级链：

1. **recap**（`turn_end` 异步生成的一句话总结）——语义密度最高，是「这轮做了什么」而非「回答首行是什么」
2. answer 代码摘要（首行 + 标题）——常退化为「完成并推送 ✅」类横幅文案，信息量低
3. 中断标注（未回答）

recap 作为 tail 的落地：`set_turn_recap(idx, recap)` 写 `Turn.recap` + `recaps.jsonl` sidecar 持久化（recap 是事后异步产物，**不进事件流**，events 重放不含它，load 侧 `_load_recaps` 按 idx 恢复）；两条生成路径的 turn_idx 捕获时机与 rewind 裁剪见 [multi-agent · recap](multi-agent.md#recap每轮一句话总结)。注意它与 `Turn.summary`（finish 时生成、贴在该轮最后的一句话摘要）是**两个不同字段**——用户提案「recap 填到触发那轮的 summary」实现为写 `Turn.recap`、供折叠摘要行消费。`/restart` 后生效——每轮 recap 落 recaps.jsonl，下次折叠触发即见 recap 版轮次概览。

## 前缀缓存三层优化（详见 blog/03）

1. **布局层**：易变块（时间/计划/召回/后台）统一收尾成 tail ambient，前缀区纯稳定
2. **轮间层**：分档冻结渲染（见上）+ **轮边界统一重排**（升档+折叠统一计划，见 [轮边界统一计划](#升档graduate-与折叠轮边界统一计划2026-08commit-1e9af8f)）；折叠为预期一次性 miss，见 [t206 实证](#折叠事件与缓存命中t206-实证2026-08)；正常轮边界平滑路径见 [t224 实证](#正常轮边界路径t224-实证2026-08)；超长轮保命阀例外见 [t228 实证](#轮内应急折叠保命阀t228-实证2026-08)。⚠️ 判阈依赖 `_estimate_tokens` 估算，口径已闭环（含 tools schema，见 [估算与校准口径闭环](#估算与校准口径闭环tools-schema-补齐2026-08)）
3. **轮内层**：分组衰减 + `_build` 以 `_planned_fold`/`_planned_graduates` 为起点**零调整**（顶满窗口时保命阀应急折叠例外，见 [轮边界统一计划](#升档graduate-与折叠轮边界统一计划2026-08commit-1e9af8f) / [t228 实证](#轮内应急折叠保命阀t228-实证2026-08)）

## usage 归一化（llm_call_log.normalize_usage）

各家缓存字段差异：GLM=`prompt_tokens_details.cached_tokens`，DeepSeek=`prompt_cache_hit_tokens`/`prompt_cache_miss_tokens`。写入 jsonl 前归一化为标准格式；读取侧 `cached_tokens_of()` 三级兜底（标准→DS hit→miss 推算），历史记录免迁移。

## provider 侧缓存坑（重要教训）

- **随机路由**：deepseek-v4-flash 等按请求随机分实例 → 每实例缓存独立 → 命中恒 0。客户端无法修，应对=回退链后置或 provider 会话粘性
- **per-token 隔离**：GLM 缓存按 api_token 隔离且容量有限 → 同 token 交错 react 长调用与 utility 短调用互相驱逐缓存 → **utility 必须独立条目+独立 token**；该类条目配 `"token_rotate": false`（sticky）。ModelScope 不吃缓存但按号限额度 → 多 token 预旋转分摊是刚需，保持默认 true
- 判别：**单步深跌后立即恢复**=折叠事件（预期一次性成本，见 [t206 实证](#折叠事件与缓存命中t206-实证2026-08)）；**同轮连续两次深跌**=保命阀折叠目标太保守（见 [t228 实证](#轮内应急折叠保命阀t228-实证2026-08)）；骤降且与 utility 调用交错相关=驱逐；恒 0=不支持/随机路由。另注意：**折叠计划判阈的估算口径**——估算"以为达标"但实际超窗时，症状是新一轮初始 prompt 远超 75% 目标却折叠 0 轮（见 [估算与校准口径闭环](#估算与校准口径闭环tools-schema-补齐2026-08)）

## 相关页面

- [长期记忆](../features/longterm-memory.md) — episodic 召回（tail ambient `[epi·长期记忆]` 行来源）的检索流水线与演进
- [multi-agent](multi-agent.md) — 子 Agent assembly DSL（`|optional` 段默认不装配、`=on` 按需打开）与 reuse 隔离
- [运维与排障](../guides/ops.md) — /stats 页、投影转储、/context live 分段统计、常见错误对照
- [系统总览](../architecture/overview.md) — 模块地图、数据流
