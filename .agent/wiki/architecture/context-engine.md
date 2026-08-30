# 上下文引擎与缓存优化

> src/session.py 核心设计。详细分档机制见 [docs/architecture/02-session-memory.md](../../../docs/architecture/02-session-memory.md)，本页补充 docs 未收录的 2026-08 演进（分组衰减 / usage 归一化 / provider 缓存坑 / 折叠实证 / 轮边界统一重排 / 轮内应急折叠保命阀 / 投影转储文件名 / 投影分段统计 / 估算与校准口径闭环）。

## 投影总览（messages_for_llm 装配顺序）

**2026-08-29 起系统信息合并形态**（`_walk_plan` 走查，见下方专节）：连续的系统信息段合并成一条 system，动态注入一律 user role：

```
[1条 system]       system + rules + asm 动作项（text/file/dir/cmd/workflow/tool）裸文本合并
[tiered history]   分档投影（需 provider 配 max_effective_context_window）；摘要消息仍独立 system（frozen）
[1条 system]       ltm 静态层（独立成条——默认清单里被 history 隔开）
[current turn]     user_message + before_turn hint(user role) + steps（工具调用按分组衰减；文件快照以 <recent-file/> 挂在【该文件最新一次改它的 tool result】content 尾部、全轮按文件去重，见「recent-file 跟屁虫快照」节）+ pending hints
[tail ambient]     一组 <system-reminder>（时间/后台任务/计划/episodic 召回）——**role=user**（DeepSeek v4 对变化的 system 消息做规范化会断全序列缓存，2026-08-29 实证修复，见 provider 侧缓存坑）
```

assembly DSL（子 Agent 声明）段可带 `|optional` 尾标——**2026-08（commit 1e3b206）起真语义：标记即默认不装配**（`messages_for_llm` 的 seg 分支对 `opt=True` 的项跳过），`agent_prompt assembly="seg=on"` 清标记打开、`=off` 移除；未标记段列出即装，必装 system/user_message/steps 未列出自动补插；`reuse`（current_turn_only）与 opt **正交叠加**（reuse 时 history 强制关）。详见 [multi-agent · assembly DSL](multi-agent.md#assembly-dsl上下文装配配方)。

装配时顺手记录分段统计到 `_proj_stats`，并覆盖写旁车 `session_dir/proj_stats.json`（含档位边界快照，2026-08-29 起——`/context` 三级读取：内存 live → 旁车 sidecar（跨重启）→ 现算兜底，见 [投影分段统计](#投影分段统计-context-改读真实投影缓存commit-4212f65)）。

episodic 召回行（`[epi·长期记忆]`）由 before_turn 检索工作流产出、注入 tail ambient——演进史与中文命中率坑见 [长期记忆](../features/longterm-memory.md)。

### 系统信息合并与动态注入 user 化（2026-08-29，用户设计）

**两件事同一天落地，动机同源**：投影形态既要语义干净（静态指令 vs 动态注入分开），又要对 provider 前缀缓存友好（byte-stable 头部 + 不触碰 system 规范化的坑）。

**① 系统信息合并（`_walk_plan` 重构）**：`history`/`user_message`/`steps` 之外的一切（system/rules/ltm/tail 段 + asm 动作项）归为系统信息，装配走查时**连续的系统信息段合并成一条 system 消息**（content 空行连接）。旧形态每个 asm 动作项独立一条 system 且带 `[assembly:kind label]` 前缀 + `<system-reminder>` 包裹——语义噪声；新形态裸文本合并（main.yml 11 个 text 项 + rules 合成一条 ~5.3k 字符）。`projection_breakdown` 现算兜底同用 `_walk_plan`（消掉 ~70 行重复走查）；段统计改**块级口径**：合并 run 非首段 `msgs=0` + meta 注明「并入上方相邻段」，`sum(sections.msgs)==total_msgs` 严格成立。`<system-reminder>` 只保留给真正动态的块（tail 组、钩子、history 摘要、中途补充）——静态指令裸、动态注入带标签，对齐 Claude Code 线上协议。

**② asm 内插空判**：text 项里任一**已注册** `{func:...()}` 占位求值为空串 → 整段返回 ""（装配侧丢弃）。混合文本（`【远程实例】\n{func:load_remote_instances()}`）无连接时旧形态剩标题空壳；纯占位项的丢弃语义补齐到混合形态。未注册名仍保留占位原样（声明写错的提示语义）。

**③ 动态注入 user 化（当天下午的缓存实证催生，见 [provider 侧缓存坑](#provider-侧缓存坑重要教训)）**：tail 合并消息、钩子旁注（`<system-reminder pos=...>`）、before_turn hint 的 role 从 system 改为 **user**（内容层的 `<system-reminder>` 包裹保留）。原因：DeepSeek v4 对 messages 里**变化的 system** 消息做规范化处理，tail 时间块每步变 → 全序列缓存断（实测 6%）——user role 的动态注入不触发（99%）。

验证：verify_assembly 34 项（含合并形态/无前缀无标签/tail user role 断言）、verify_agent_yml 29 项、midturn/recent_file/debug_hook/ltm 全过；端到端真请求三步步进 99% 命中（修复前同形态 6%）。

## 投影转储文件名与 t/s 标记（commit 4aced81）

当 `/config dump_projections true` 时，每次调用 LLM 前会转储完整投影到 `sessions/<ts>/projections/` 目录，文件名格式：

```
t{轮号}_s{步号}_{微秒戳}.json
```

- `t{轮号}`：已完成轮数（`len(turns)`，进行中的是第 turn+1 轮），与 llm_calls.jsonl 中的 `turn` 字段对应
- `s{步号}`：当前轮已完成步数（`len(_current.steps)`），与 llm_calls.jsonl 中的 `step` 字段对应
- `{微秒戳}`：调用时间戳（去重用）

**格式：负载本体 pretty-print，零构造零截断（2026-08，commit 2dc64f2）**：`_dump_projection`（src/agent.py）直接 `json.dumps(payload, indent=2, ensure_ascii=False, default=str)` 转储**发给模型的完整负载**——messages 全量（含 tool_calls 结构、reasoning_content 等），发什么存什么，不重新构造中间格式、不截断：`json.load` 即可消费，考古/对拍零损耗。元信息（turn/step/model/agent_id/time）收进 `_meta` 顶层字段：`{"_meta": {...}, "messages": [...]}`。⚠️ 历史 dump 是 `.txt` 自定义格式（`=== 投影转储 turn=... ===` + `--- [N] role=... (chars) ---` 逐条 + 8000 字截断），仅存于旧存档——本页下方 t206/t224/t228 实证引用 `*.txt` 均为当时的历史文件；新进程一律 `.json`。

**同源保证**：react 主循环 3 处 `llm.chat` 调用点（主调用 / DSML 重试 / 空回答重试，`src/agent.py`）均传 `turn=len(turns), step=len(_current.steps)`——与 `_dump_projection` 完全同一取值；`src/llm_client.py` `chat()` 经 `_turnstep_ctx`（与 `_scene_ctx` 同构 contextvar，finally 清理，不进 API 请求）落盘 jsonl，`src/server.py` `/api/stats` 透传。**在 react 循环新增 LLM 调用点时记得带上这两个参数**，否则该点 tooltip 无轮步标记、无法映射到投影文件。

**与 /stats tooltip 对齐**：从 `/stats` 折线图 hover 获取 `· t206 · s6` 标记 → 直接打开 `projections/t206_s6_*.json` 查看当时完整投影，快速定位升档/折叠等事件断点（详见 [运维与排障](../guides/ops.md#stats-页webui-统计按钮)）。

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

### 段统计异常诊断：sample 字段（2026-08，commit feeb123）

**背景（454 轮 session 实测）**：live 段统计出现「当前轮steps(1步)=83,161 tok」异常——上一步只有一次 grep，1 步不可能 83K tok；且该轮下一轮归档进档2 仅 2,070 tok（压缩量级正常）——「归档后瞬间从 83K 压成 2K」在数值链条上自相矛盾。同时近 2 轮 answer 顶部标注 `---- 已折叠共0次工具调用 ----` 与「实际只调了 4 次」也疑似对不上。

**排查：干净重算完全正常（无法静态复现）**：子进程 `Session.load` 重载当前 session（455 轮）跑 `messages_for_llm` 干净重算——Σ段=合计、无巨型消息（最大单条 ~8K chars）、`_steps_to_messages` 对单步的输出上限 FULL_STEP_CAP_CHARS≈32K chars（≈8K tok）——83K tok 需 ~133K chars，静态推导不出。**live 异常无法跨进程复现**，指向进程内瞬时态。

**澄清「已折叠共0次」**：dump 数据证实那些是**真实的纯讨论轮**（remote_tools 评估、server_id 评估等架构讨论，一字工具没调）——数据没错、标注次数与 events 完全一致；但「0 次也加标注行」是纯噪声 → 另见 [超深档折叠标注：0 次工具调用省略标注行](#超深档折叠标注0-次工具调用省略标注行2026-08commit-feeb123)。

**根因假设（未证实）**：live 异常快照时段（t452/t453）恰好是连续编辑 session.py **本身**的轮次——live 进程还跑着编辑前的旧代码，新旧 `_render_tiered_history`/`_hist_marks` 的 marks 语义可能有瞬时不一致（旧代码 marks 与新版 `_seg_msgs_history` 分派不匹配）。

**修复（session.py，commit feeb123）**：`_proj_stats.sections[]` 每段增 `sample` 字段（该段首条消息 content 前 120 字、换行压空格）——段统计异常时（如 msgs=1 却巨大）直接看切片里装的是什么，live 异常无需跨进程静态复现即可定位。`projection_breakdown()` 浅拷贝透传（含 sample）；`/context` 展示侧未接（要显示需在段落表加一行）。需 `/restart` 生效。

#### 段统计错位实证闭环：总量守恒、段间错位（2026-08，t456/t457）

/restart 后异常复现（这次只有 2 次 grep）：`当前轮steps(1步)=100,401 tok`（24.3%）。t456 干净重算 + t457 live 对照两轮调查，把「段统计错位」的性质钉死：

| 项 | 数据 | 判定 |
|---|---|---|
| /context 合计 | 327,868 est + schema 30,257 ≈ 358K ≈ 实测 prompt 356,528（92% cached） | ✅ **总量其实准的**（Σ段=合计守恒） |
| 段间分布 | steps 段 100,401 est tok ≈ 163K chars，但那 1 步（2 个 grep）实际仅 ~25K chars | ❌ **steps 虚高 ~85%，别段被低估** |
| 理论上限 | 单步 cap FULL_STEP_CAP_CHARS=32K chars；2 个 grep 最多 ~45K chars ≈ 28K tok | 100K 超上限 3.5×，**必是统计错位而非内容真实** |

**关键结论：错位在统计层、不在投影层**——干净子进程重算 Σ段=合计、无巨型消息（最大单条 ~8K chars）；live 进程的段切分把 steps 段算大、别段算小，但总和守恒（各段占比失真、合计可信——`/context` 的总量与实测 prompt_tokens 对得上）。最可疑机制：hist 子标记偏移换算（`st + off`）在某种边界下错位——静态读码三轮未抓获现行。

**收尾**：/restart 后的进程已带 sample 诊断（本页上节）——下次异常段出现，`/context` 输出直接显示「msgs=1 的段里装的是什么消息」，错位边界当场现形。归因与修复等待下一次复现的证据（live 瞬时态无法静态推导）。

### 段统计旁车持久化：proj_stats.json + /context 三级读取（2026-08-29，commit e703c67）

**提案（用户，2026-08-29）**：投影时把各档位边界记录下来写入旁车文件；`/context` 优先读最新旁车、结合消息体复原各档在总上下文中所占的比例。

**动机**：`_proj_stats` 是进程内存——/restart 后消失，`/context` 退化为「现算估算」（事后模拟；在 reuse / 保命阀触发的轮，模拟与真实投影口径本来就不一致）。旁车把「上次真实投影」的 live 口径（含档位边界快照）持久化到 session 目录，跨重启存活。

**演进对照**：本节 4212f65 版读取侧是两级（内存 live → 现算兜底）；本次扩为三级，中间插旁车层。上方读取侧描述中的「只有本进程还没跑过投影时才回退现算兜底」即旧两级口径。

**写入侧（src/session.py）**：`_save_proj_stats_sidecar(stats)`——`messages_for_llm` 装配记录 `_proj_stats` 后**覆盖写** `session_dir/proj_stats.json`（永远只留最新一份）：

```json
{
  "tier_boundaries": [9, 19, 29],          // 档位边界快照（提案点名的核心）
  "fold_count": 227, "max_level": 6, "chars_per_token": 1.62,
  "sections": [ {"name": "...", "msgs": ..., "chars": ..., "tokens": ..., "sample": "..."} ],
  "total_msgs": ..., "total_chars": ..., "total_tokens": ...,
  "ts": 1756..., "turn": 468, "step": 3, "source": "live"
}
```

- **原子写**；session_dir 未就绪 / 写失败**静默**——旁车只是诊断增强，绝不影响投影主路径

**读取侧三级（`projection_breakdown()` + src/commands.py `/context`）**：

1. **内存 live**：本进程最近一次真实投影（首选，最新）
2. **旁车 sidecar**：内存为空（重启后首次 /context）时读 `proj_stats.json`——`source` 标 `sidecar`
3. **现算兜底**：旁车也没有（新 session / 文件缺失）才重算段函数

`/context` 输出三态来源标注：

```
段落统计（采自上次真实投影 t468·s3，5秒前）                    ← live
段落统计（采自旁车 t468·s3——上次真实投影的存档，跨重启有效）  ← sidecar
段落统计（现算估算——本进程尚未跑过投影，且无旁车存档）        ← 兜底
```

**验证四场景全过**：旁车写入含档位边界快照（`[9,19,29]` + fold_count + max_level）✓；模拟重启（清空内存 `_proj_stats`）后 breakdown 读旁车 `source=sidecar` ✓；删旁车文件现算兜底 ✓；/context 三态渲染分支在位 ✓。

需 `/restart` 生效；攒批待发 0.22.2。

#### 旁车窗口快照：win / fold_target / panic（2026-08-29，commit f57de5d）

**动机（诊断盲点补齐）**：「触发毕业/折叠时 live 窗口到底是多少」此前只能从行为反推——「投影 200K 为何每轮毕业」排查辛苦的根源正是没有这个证据（真相是 live 窗口为旧值 256K 档而非配置里的 400K/700K，见 [窗口值生命周期](#窗口值生命周期llm-固化副本--改窗口四入口同步2026-08-29commit-f57de5d)）。旁车直接留证据。

**新增字段**（src/session.py `_save_proj_stats_sidecar`，与 tier_boundaries 同级写出）：

| 字段 | 计算 | 实例（win=700K） |
|------|------|------|
| `win` | `session.max_effective_context_window` 快照——本次投影判阈**实际用的值** | 700000 |
| `fold_target` | `int(win × FOLD_TARGET_RATIO)`——折叠目标线（75%） | 525000 |
| `panic` | `config.load_panic_window()`——保命阀阈值（settings `panic_context_window`，0=跟随窗口） | 900000 |

win 为 None（未配窗口、分档禁用）时 fold_target/panic 不写。

**诊断口径**：旁车 est 长期贴线（`est ≈ fold_target`）→ 先看 `win` 是否等于当前配置预期值；不等 → `/reload models`（0.22.2+ 会同步 session 副本并打印目标线）或 `/restart`。

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

#### 触发线修复：win 才是触发线，win×ratio 是保留水位（2026-08-30，commit 304bc16，用户诊断）

**现象（用户从行为反推直接点破根因）**：「现在看起来还是每一轮都在触发毕业…应该是 `max_effective_context_window`，不是 `max_effective_context_window * fold_ratio`」——诊断完全正确。

**根因（语义与实现错位一个版本周期）**：v0.22.3（commit 27fea56）定稿 per-provider 参数时，**文档语义已写对**（「ratio = 触发后保留水位，不是触发阈值」），但 `_plan_fold` 入口判据实际仍是 `if est <= target: return`（target = win×ratio）——文档对、代码没改：

- 旧判据下 win=400K、ratio=0.7 → 触发线 = 280K：投影在 **280K~400K 健康区间**每轮都进压力循环升档毕业——本 session 投影常年 300~440K，「每轮毕业」恒真
- 修复后 docstring 定稿：**触发线 = `max_effective_context_window`（投影顶窗才触发）→ 触发后压到 win×ratio（保留水位）**；「ratio 是触发后保留多少，不是触发阈值；低 ratio 压得狠、下次顶窗间隔长」

**修复（src/session.py `_plan_fold` 入口判定）**：改 `if est <= self.max_effective_context_window: return`——健康区零动作；顶窗后才进压力循环（先升档再折叠，两步都以保留水位为目标）。未触发区间（target~win）零动作纯追加 = 前缀缓存最优（轮内 `_build` 以计划结果为起点不再调整）。上方 [澄清案例](#_planned_fold0-但-context-显示超窗轮边界估算时点2026-08-澄清) 在修复后结论不变（256K < win=400K 同样不触发）。

**验证（真实数据 dry-run + mock 场景）**：

| 场景 | 结果 |
|------|------|
| 本 session est=442,167（win=400K，target=280K） | 真顶窗 → 正常触发，计划 fold=250 轮 ✓ |
| 顶窗场景（win=250K，ratio=0.7） | fold=380 → 执行后 est=173,878 ≤ target=175,000 ✓ 精准压到保留线 |
| 健康区（est 280K~400K）/ 无窗口配置 | 零动作 ✓ / 早退 ✓ |
| 卫生段（档1 >60 轮） | GRADUATE_FORCE_TURNS 独立生效，不受触发线影响 ✓ |

**效果（毕业频率）**：修复前 est>280K 恒触发 ≈ **每轮毕业**；修复后触发一次压到 280K、需涨回 120K（≈30% 窗口）才再次触发——按每轮 20~90K 增长 **2~6 轮一次**，轻度对话更久；且该区间零毕业零折叠 = 前缀完全稳定。**三线体系至此各司其职：触发线（win）/ 保留线（win×ratio）/ 保命线（panic）**。需 `/restart` 生效——注意若重启时已真超窗（如 est=442K > 400K），**第一轮边界会先来一次大折叠**（正常行为，压到保留线），之后进入「280K→400K 缓涨零动作」稳定周期。

**与 comfy「每轮毕业」案例（[窗口值生命周期](#窗口值生命周期llm-固化副本--改窗口四入口同步2026-08-29commit-f57de5d)，f57de5d）对照**：两次同症状、两个根因——那次是 live 窗口副本停在旧值（256K 档、192K 线被 200K 投影持续贴线滚动），这次是**窗口值正确、触发判据本身错用保留线**。排障先看旁车 `proj_stats.json` 的 `win`/`fold_target`：窗口值错 → 修配置同步（四入口）；窗口值对还每轮毕业 → 查触发判据（本节）。

### 窗口值生命周期：llm 固化副本 + 改窗口四入口同步（2026-08-29，commit f57de5d）

**背景（comfy session 用户报告）**：models.json 已把 deepseek 条目窗口改到 700K（此前 400K），`/reload models` 后投影 200K 仍然**每轮毕业/折叠**——用户质疑「200K 远没达到 400K 的窗口」成立：若窗口真是 400K（目标线 300K），est≈250K 根本不可能触发。

**根因：窗口是 llm client 创建时固化进 session 的副本**。`session.max_effective_context_window` 在 session 构造时从 llm 读一次；`/reload models` 旧版只 `llm._apply_profile(...)` 刷新 profile，**session 副本不同步**——毕业/折叠计划与 detail_base 推导仍按旧窗口算。

**真相三段证据互锁（live 窗口 = 旧值 256K 档，非配置里的 400K/700K）**：

| 证据 | 内容 |
|------|------|
| ① 反证 | 若窗口 400K（target=300K）：轮边界 est≈250K << 300K → 不该触发——但边界确实在持续增涨 |
| ② 贴线实证 | fold_count 停在 148 时旁车 est=184,964≈185K，恰好压到 192K（=256K×0.75）目标线下一点点——每轮 est≈192~250K 持续超线 → 升档 + fc 小口吞 → 吞到 185K < 192K 才停。200K+ 的投影对 192K 的线就是「持续贴线滚动」状态 |
| ③ 引擎自证 | dry-run（win=700K）不触发——引擎判定逻辑本身正确，问题只在 live 窗口值 |

**修复（四入口同步：`switch_model` / `/reload models`（src/commands.py）/ WebUI 保存模型配置（src/server.py）/ `/config`）**：换 profile 后同步 session 窗口副本 + `invalidate_detail_base()`——reload 输出多一行 `· session 窗口已同步：700000（折叠目标线 525,000 tok）` 即生效。直接改 models.json 文件后：跑一次 `/reload models`（0.22.2+）或 `/restart`（新进程构造时读新配置）。

**排障第一手证据**：旁车 `proj_stats.json` 的 `win`/`fold_target` 字段（见 [旁车窗口快照](#旁车窗口快照-win--fold_target--panic2026-08-29commit-f57de5d)）——「投影 200K 为何每轮毕业」这类问题不再需要从行为反推 live 窗口值（本 commit 顺带新增，正是那次排查辛苦的产物）。

### per-provider 缓存经济学参数：fold_target_ratio + detail_step（2026-08-30，commit 27fea56，用户提案）

**动机（用户提案 2026-08-30）**：各 provider 缓存命中的价差结构不同——GLM miss≈hit 的 **4 倍**、DeepSeek miss≈hit 的 **30~60 倍**（v4-flash 账单实测 30 倍）：折扣越悬殊，越不该轻易破坏缓存前缀（省下的命中价差 >> 超窗的占用）。故把**折叠目标线比例**与**组间衰减步距**从引擎全局常量提升为 models.json 的 per-provider 参数。

**两参数**（`src/llm_client.py` profile 读取与 clamp + `src/session.py` 消费）：

| 参数 | 范围/缺省 | 语义 |
|------|----------|------|
| `fold_target_ratio` | clamp 0.5~0.99；缺省=引擎 FOLD_TARGET_RATIO(0.75) | 折叠保留目标线占窗口比——**调低（如 0.5）= 超线更早、小步升档（断尾部小）先消化压力、大折叠（断头部大）极少触发**（DeepSeek 60x 差价推荐）；调高（如 0.95）= 近窗才超线、历史多已退化超深档升档压不动、只得大折叠（断头部大）。方向论证见下方 [方向澄清](#方向澄清为什么-deepseek-配低-ratio-才对升档断尾部小折叠断头部大2026-08) |
| `detail_step` | clamp 0~200；缺省=全局 settings(15) | 组间步距衰减字数——**0=不衰减**：所有组 limit 恒定、老步骤渲染字节永不回缩 → 前缀缓存打满（宁可投影大也不让组边界衰减重截断缓存） |

**单一真相源与同步点**：

- `session.fold_target()` = win × ratio——**轮边界计划与保命阀回落目标共用**（`_plan_fold` / 保命阀 settle 均改调它）；旁车 `proj_stats.json` 记 `fold_ratio`；轮边界日志改实际百分比（`轮边界计划：升档 2 档 + 折叠 0 轮（目标 ≤95%×700000）`——🐞 面板可见）
- `session.detail_step` property（profile > 全局 settings > 15）——分组衰减公式消费（见 [分组衰减](#分组衰减轮内2026-08-新)）；**冻结渲染缓存 key 加 detail_step 维度**：切 provider（衰减参数变）时归档轮渲染正确失效重算
- 三处同步点：`switch_model` / `/reload models` / WebUI 保存模型配置（src/agent.py / commands.py / server.py）——与窗口副本同步同款（见 [窗口值生命周期](#窗口值生命周期llm-固化副本--改窗口四入口同步2026-08-29commit-f57de5d)）
- WebUI：模型卡片 max_tokens 旁 +2 数字框（折叠目标% / detail_step，tooltip 带经济学说明）

**验证**：0.95 → 目标线 380,000 tok ✓（ratio→目标线换算生效）；detail_step=0 生效、未配置回退全局 15 ✓；**渲染字节稳定性**——detail_step=0 下 25 步 3 组的 tool 结果长度全同（1556 字节），组边界衰减不再回缩缓存，缓存打满的直接证据 ✓。

**推荐配置（DeepSeek 缓存三件套）**：`fold_target_ratio: 0.5`（**低**——更早让小步升档消化压力、少触发大折叠）+ `detail_step: 0`（不衰减）+ `token_rotate: false`（sticky）——配合动态注入 user 化，把前缀稳定到物理极限；GLM（4x 价差）用默认 0.75+15 的平衡点即可。需 `/restart` 生效；配置示例见 [配置体系](../guides/config-and-models.md)。

#### 方向澄清：为什么 DeepSeek 配低 ratio 才对——升档断尾部小、折叠断头部大（2026-08）

**两轮对话锁定方向（我初版论证反了，用户纠正「低目标线撑更久」）**：断缓存的元凶分两类、影响天差地别——t206 实证「是折叠事件，不是升档」：

| 动作 | 改写位置 | 断缓存范围 |
|------|---------|-----------|
| **升档（graduate）** | 档内轮（历史段**尾部**，档1 最靠后） | **小**——滚动毕业下尾部每轮边界本就顺移，升档字节变化与日常重合，非额外断点 |
| **折叠（fold）** | fc 摘要（历史段**头部**） | **大**——头部之后全失效，一次性 ~98% miss + 下一步恢复（= t206 的「一次性成本事件」） |

`_plan_fold` **先升档后折叠**（都压到 ≤ target）：先跑升档刀（循环 `_graduate_once`），`_fold_leap_target` 折叠刀是「升档压不动后的兜底」。方向由此确定：

```
低 ratio（如 0.5）→ 触发后压到低线（350K）→ 涨回窗口需 (1−ratio)×win 更久 → 顶窗间隔长
  → 且压到低线用【升档】（尾部小断）就够了 → 折叠（头部大断）极少触发
  → DeepSeek 60x 差价下缓存友好 ✅

高 ratio（如 0.95）→ 只压到近窗高线（665K）→ 涨回窗口快、边界密集
  → 历史长期滚动毕业、多已退化为超深档（升档对它无效）→ 只能【折叠】（头部大断）❌
```

**所以 DeepSeek（miss≈hit 60x）配低 ratio（0.5）——触发后一次压狠、下次顶窗间隔拉长、升档就能消化压力；GLM（4x）用默认 0.75 平衡点**。配 `detail_step:0`（不衰减）更进一步：组内字节恒定 + 折叠频率最低 = 前缀物理极限稳定。

**教训（两次方向）**：缓存优化判据不是「投影多大/追加多少」（追加在尾部、不影响前缀），而是「**折叠（头部大断）vs 升档（尾部小断）各发生多少次**」——先升档后折叠的机制决定：低 target 让便宜的升档消化压力，把贵的折叠留给真正的近窗危机。

> **后记（2026-08-30，commit 304bc16）**：本节初稿时触发线误用 target（`est > win×ratio` 即触发），故原文写「低 ratio → target 低 → 投影更快超线」；[触发线修复](#触发线修复win-才是触发线winratio-是保留水位2026-08-30commit-304bc16)后触发恒为 `est > win`（与 ratio 无关），低 ratio 的机制改为「**触发后压得狠 → 涨回窗口需 (1−ratio)×win → 顶窗间隔长**」——结论不变（低 ratio 缓存友好），上方代码块已按新语义改写。

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

#### 卫生性强制毕业：GRADUATE_FORCE_TURNS=60（2026-08，commit f57b147）

**背景（8000 实例实证）**：档1 是全量披露档（「近期窗口」语义），但毕业只在窗口压力下触发（估算 >75%×win 才跑 `_graduate_once`）。窗口宽绰时（8000 实例投影只占 44.6%）压力循环永不触发 → **档1 无限膨胀**——实测涨到 64 轮 / 占投影 58.6%，早就不「近期」；更糟的隐患是：一旦后续碰到窗口压力，要连续多刀毕业才压得下来（缓存断点反而更大）。

**实现（src/session.py）**：

```python
GRADUATE_BATCH_TURNS = 30   # 大档分批毕业：一次只升【前 N 轮】，近期轮保持 level1（保真）
GRADUATE_FORCE_TURNS = 60   # 卫生性强档阈值：当前档超过此轮数时，无窗口压力也分批升前 30 轮
```

`_plan_fold` 里、窗口压力循环【之前】：当前档（最后边界之后的段）> 60 轮 → 强制分批升档（复用 `_graduate_once`，每刀前 30 轮，近期轮保持 level1）。日志标注「卫生性强制毕业 +N 刀（当前档曾 >60 轮）」。

**六场景验证全过**：64 轮档1 → 一刀 `[29]`，档1 剩 34 ✓；60 轮整不触发（严格大于）✓；61 轮一刀剩 31 ✓；已有边界续切（105 轮 `[40]` → `[40,70]`）✓；150 轮大膨胀连三刀收敛到 60 ✓；小窗口压力回归不受影响 ✓。需 `/restart` 生效（8000 那个实例还在跑旧代码，下个超 60 轮的轮边界触发）。

#### fc 大刀首折：至少吞超深档一半（2026-08，commit 4d37e90）

**背景（用户裁定 2026-08-28）**：边界密集（滚动毕业 ~1.9 轮/边界）时碎刀偏勤——每轮边界触发折叠、每刀只折 1-2 轮，超深态（answer/reasoning 原文保留）留存太短，从[超深档工具调用折叠]到[fc 折叠为摘要]过于频繁。裁定：**每次首折至少折叠超深档的一半以上**。

**实现（src/session.py `_fold_leap_target(fc)`）**：

- 超深段 = `[fc, bs[-max_level]]`（最后一个超深轮 = 倒数第 max_level 个边界）
- 目标 = `fc + (段长 // 2)`，对齐到合法折叠点（boundary+1）
- 三态退化碎刀：fold_deep_tools 关 / 档梯未满 / 超深段已折完

两处消费点：`_plan_fold` 折叠循环（首刀 fc==0 大刀，之后仍超线再碎刀微调）+ 保命阀应急循环（同款，首刀从 `_planned_fold` 起点大刀）。

**对照验证（203 密集边界 / 406 轮，本 session 真实形态）**：旧碎刀一次 `_plan_fold` 吞 52 轮；新大刀一次吞 200 轮（超深段 398 轮过半）——触发间隔约翻 4 倍，超深态平均留存同幅延长。

**缓存经济无损**：`_folded_summary` 是尾部追加式（吞新段只往摘要末尾加行，不动已有前缀）——大刀与碎刀不互相破坏缓存，区别只在触发频率，而频率降低本身即缓存友好。

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

**`detail_step` per-provider 化（2026-08-30，commit 27fea56）**：步距衰减字数不再只有全局 settings 一档——models.json profile 可按 provider 覆盖（`session.detail_step` property：profile > settings > 15；clamp 0~200）。**0=不衰减**：所有组 limit 恒定、老步骤渲染字节永不回缩——DeepSeek 类 miss/hit 价差悬殊（~60x）的 provider 推荐配 0（宁可投影大也不让组边界衰减重截断缓存）；GLM（4x）用默认 15 平衡点即可。详见 [per-provider 缓存经济学参数](#per-provider-缓存经济学参数fold_target_ratio--detail_step2026-08-30commit-27fea56用户提案)。

## recent-file 跟屁虫快照：注入三版演进 + rf 免疫收拢单源 + 源头收缩（2026-08-29，dd7fd81 + 39e7115 + 348adfc + 983c417 + 22eaa04）

**机制是什么**：react 每步工具调用读写 repo 文件时，把文件快照记进 `step.file_snapshots`（call_id → {path, version, structure, content…}），投影装配时以 `<recent-file file='…' version='…'>` 块注入——让模型看到自己「刚操作的是什么版本的文件」，同文件连续操作不必反复 read（跟屁虫语义）。

**注入位置三版演进（2026-08-29 当天走完一个螺旋）**：v1 每步逐 call 注入（重复放大）→ v2 轮尾去重独立消息（唯一性有了、因果位置丢了）→ **v3 终版：按 call_id 命中，快照附加在【该文件最新一次改它的那次 tool_call 的 result content】尾部——位置与唯一性兼得**（见下方修复三；修复一/二为中间态，机制语义仍有效）。

### 旧注入的重复放大（用户实测抓到）

用户检查当前 session 最近一次请求负载：`src/llm_client.py` ×4、`src/agent.py` ×2、`src/static/index.html` ×2——同一文件多份不同版本快照并存。

根因两层：

1. **步内逐 call 注入（源头）**：旧 `_steps_to_messages` 每步逐 tool_call 注入 recent-file——连续编辑同一文件的轮里，每步各带一份当时版本快照，一轮累积 N 份
2. **档1 冻结复用（放大器）**：档1 归档轮渲染（base=None 全量路径）也注入 → `_render_turn_frozen` 把含 recent-file 的渲染缓存进 `_frozen_renders`（key = level/fold/base），档1 存续期内每次投影复用，直到毕业顺移才失效重算——上一轮的文件快照跟着档1 轮继续出现在后续投影里

### 修复一：轮尾去重注入（2026-08-29，commit dd7fd81，用户裁定「只有当前轮管，前面的轮都不管」）

- `_steps_to_messages` 步内注入**完全移除**——连带消除冻结放大器：`_frozen_renders` 缓存的是渲染结果，源头不产生、缓存里自然没有；且它本就是内存缓存不落盘，无跨重启残留
- `_seg_msgs_steps` 统一注入：**只在当前轮 steps 尾部**、全轮按文件去重——同文件后写覆盖，每文件只保留【最新一份】快照
- 归档轮/历史段（含档1 的 base=None 全量路径）**一律不注入**
- 顺带修掉 `_seg_msgs_steps` 里 `out.append({...}` 缺右括号的既有语法 bug

六场景验证全过（含档1 归档轮 0 份）。机制自证：编辑 session.py 的轮次，轮尾注入的就是最新版全文一份（旧代码下是两份不同版本并存）。

### 修复二：毕业判阈 rf 免疫（2026-08-29，commit 39e7115，用户裁定）

**问题**：rf 是轮内易变项（同文件后写覆盖、**归档即消失**——下一轮投影里就没了），但它的体积计入实测 prompt_tokens → 大 rf 轮把 over 判真 → 下一轮边界频繁触发毕业（不可逆历史压缩）。裁定：**rf 的体积不该推动毕业**。

**实现（src/session.py）**：

- 新增 `_rf_chars()`：当前轮 rf 按文件去重后的集合（与 `_seg_msgs_steps` 注入集合同款）的总字符数
- 回包触发判定（observe_llm_usage）：`over = (prompt − rf_tok) > win`，其中 `rf_tok = _rf_chars() / max(0.1, _chars_per_token)`——实测 prompt_tokens 刨除 rf 估算后仍超窗才算 over
- **panic 判阈不刨**（`hit_panic = total > panic` 按真实请求体积保命：请求确实超了就必须救——rf 也在真实请求里）

**口径哲学**：与 [估算与校准口径闭环](#估算与校准口径闭环tools-schema-补齐2026-08) 同族——判阈分子应反映「归档后真实留存的历史体积」。schema 是请求级固定项要加进来，rf 是轮内易变项要刨出去：方向相反、原则同一。

### 修复三（终版设计）：快照挂进「改它的那次工具调用」result content（2026-08-29，commit 348adfc，用户设计）

**动机**：修复一的轮尾注入保住了唯一性，但快照变成轮尾一条独立消息——模型看不出「哪次调用改的它」，因果位置丢了。用户裁定改回【挂在工具调用结果上】+ 投影时命中式去重：**内存维护 filename → 最新改它的 tool_call 映射，渲染 role:tool 时按 call_id 命中，把 `<recent-file/>` 块附加在该次工具调用 result content 尾部**。

**实现（src/session.py 四处）**：

- 新增 `_rf_latest_map()`：从当前轮 `_current.steps` 各步的 `file_snapshots` 构建 `filename → {cid, path, version, text}`——同文件多次 edit **后写覆盖**，只有【最新一次改它的 call】进映射（会在投影时命中挂快照）；映射只从 `_current` 构建 → 归档轮/历史段的 call_id 天然不在映射里，「前面的轮不管」零判断开销
- `_seg_msgs_steps` 构建映射传入 `_steps_to_messages`（签名增 `rf_map: dict = None`）；归档轮/历史段不传——call_id 不在映射里自然不命中
- `_steps_to_messages` 内建 `_rf_hit = {m["cid"]: m for m in (rf_map or {}).values()}`（call_id → 快照，O(1) 索引）
- 渲染循环：content 走完 cap_full/summarize + 图片投影后，`tc.call_id` 命中 → content 尾部追加 `<recent-file file version>全文</recent-file>`

**四场景验证全过**：同文件两次 edit 仅最新 call（c3）带 rf、旧 c1 不带 ✓；另一文件正常挂 ✓；独立 system rf 消息 0 条 ✓；归档轮零注入 ✓。机制自证：编辑 session.py 的轮次，session.py 全文快照就挂在本轮最后一次 edit 的工具结果后面。

**口径连带**：修复二的 `_rf_chars()` 判阈刨除集合终版起与 `_rf_latest_map` 同源（同文件留最新一份口径不变，毕业判阈 rf 免疫语义不变）。

### 修复四：估算剥离 `_rf_stripped`——rf 免疫扩展到毕业/保命阀，四层口径收拢单源（2026-08-29，commit 983c417，用户设计）

**背景**：修复三把快照挂进 tool result content 后，rf 体积成了真实投影的一部分，但两处估算分子没跟上——①毕业升档/折叠判阈（`_plan_fold` 的 cur_est）含 rf 块：大 rf 轮会推动升档/折叠这类**不可逆历史压缩**，panic 轮内路径调 `_plan_fold` 时（cur_est 含 rf）尤其过激；②保命阀应急估算（`_history_tiered_msgs` 的 rest）同理。用户裁定：rf 是轮内易变项（归档即消失），**它的体积不该推动升档/折叠**——修复二的判阈刨除逻辑要跟着注入机制一起改：从当前轮所有 role:tool 区按 rf_map 找命中，把多出来的估算量去掉。

**实现（src/session.py）**：

- `_RE_RF_BLOCK` 模块级正则 `\n<recent-file[\s\S]*?</recent-file>`：剥离与量体积共用
- `_rf_stripped(msgs)`：返回剥除 rf 块的消息副本（**不动原消息**——投影产物不可变）。两处消费：`_plan_fold` 的 cur_est（毕业升档/折叠估算）、`_history_tiered_msgs` 的 rest（保命阀应急估算，循环外算一次）
- `_rf_in_msgs(msgs)`：诊断口径——msgs 中实际附加的 rf 块总字符数

**判定口径（用户设计；content 子串版中间态被推翻）**：第一版按 content 子串检测（`"<recent-file" in c`）——「按实际附加了多少算」的思路没错（full/字符串等附加条件都已体现在渲染产物里，直接量最准），但用户裁定改为**按 `tool_call_id ∈ rf_map 命中集合` 判定**：与附加时的命中条件同源——**附加按 cid 命中，剥离也按 cid，因果一致不漂移，不做 content 子串检测**。防御两处：sub 对无块 content 是 no-op；多模态 list content 由 isinstance 天然跳过。

**rf 四层口径收拢 `_rf_latest_map` 单一真相源（同源映射，永不漂移）**：

| 层 | 消费点 | 口径 |
|---|---|---|
| 附加（投影） | `_steps_to_messages` 的 `_rf_hit`（rf_map 仅当前轮传入） | cid 命中 → `<recent-file/>` 挂该次 tool result content 尾部 |
| 判阈刨除（over 判定） | `observe_llm_usage`：`over = (prompt − rf_tok) > win`（`_rf_chars()` = 映射 text 总量） | **panic 判阈不刨**（按真实请求体积保命） |
| 估算剥离 | `_rf_stripped`（毕业 cur_est + 保命阀 rest） | 先剥块再进 `_estimate_tokens`——估算分子反映「归档后真实留存体积」 |
| 诊断 | `_rf_in_msgs` | 量实际附加的块总字符（验证用） |

**验证**：c1 挂 / c2 不挂回归 ✓；剥离差值闭环 12,514 tok ✓；无 rf 轮零影响 ✓。与 [估算与校准口径闭环](#估算与校准口径闭环tools-schema-补齐2026-08) 同族口径哲学：schema 是请求级固定项要**加进**估算分子，rf 是轮内易变项要**刨出去**——方向相反、原则同一。需 `/restart` 生效；攒批待发 0.22.2。

### 修复五：读工具移出 `_FILE_SNAP_TOOLS`——rf 只跟写操作，源头收缩（2026-08-29，commit 22eaa04，用户裁定）

**背景**：用户问「grep 是不是也会挂 recent-file？grep 要不就算了吧」——排查确认**在列**：收集侧白名单 `_FILE_SNAP_TOOLS`（src/agent.py，step 完成后收全文速照进 `file_snapshots` 的**源头**）此前 10 工具，三个读工具全在。用户直觉（grep 不配）成立且可推广——三个读工具各自不配的理由不同：

| 读工具 | 为什么不该配 rf |
|---|---|
| `read_file` | result **本身已是全文**——再挂 rf = 投影里同一内容出现两份 |
| `grep` | result 只含命中片段——grep 一个 2000 行文件，80K 字符全文速照纯膨胀（用户点名的那个） |
| `find_function` | 同 grep（返回函数体片段） |

**修复（src/agent.py 一处，源头层）**：`_FILE_SNAP_TOOLS` 10 → **7**，只留【写】工具 + 黑盒执行器——`edit`/`insert`/`delete`/`move`/`write_file` + `run_python`/`run_script`（黑盒可能写文件，快照仍必要）；排除理由写进集合上方注释（防后人再加回去）。

**写语义零损失**：`read_file(a.py) → edit(a.py)` 场景里 edit 自己在集合内，rf_map 仍挂【最新一次改它的 call】（edit）——「编辑连续性」设计意图完整保留。rf 语义至此收敛为一句话：**只有写过的文件才配拥有全文跟屁虫**——这也是投影膨胀治理系列（修复一~四）的收尾漏洞：此前修过「同文件重复注入」，这次补上「读工具也挂全文」。

**口径注**：本节开头「每步工具调用**读写** repo 文件时」即修复五前的旧口径（收集侧此后只看写工具 + 黑盒执行器）；注入侧四层口径（修复三/四：rf_map 命中附加 / 判阈刨除 / 估算剥离 / 诊断）**零改动**——`file_snapshots` 只是少收三类读工具的条目，映射/命中/剥离机制原样。需 `/restart` 生效；攒批待发 0.22.2。

## 折叠摘要 tail 优先级（recap → answer 代码摘要 → 中断标注，2026-08）

`_folded_summary(fold_count)` 生成被折叠早期轮次的结构概览（纯结构信息、无需 LLM；逐字原文靠 recall 召回）。每轮一行：`user[:80]` + `(已折叠N次工具调用) ` + tail。tail 的优先级链：

1. **recap**（`turn_end` 异步生成的一句话总结）——语义密度最高，是「这轮做了什么」而非「回答首行是什么」
2. answer 代码摘要（首行 + 标题）——常退化为「完成并推送 ✅」类横幅文案，信息量低
3. 中断标注（未回答）

recap 作为 tail 的落地：`set_turn_recap(idx, recap)` 写 `Turn.recap` + `recaps.jsonl` sidecar 持久化（recap 是事后异步产物，**不进事件流**，events 重放不含它，load 侧 `_load_recaps` 按 idx 恢复）；两条生成路径的 turn_idx 捕获时机与 rewind 裁剪见 [multi-agent · recap](multi-agent.md#recap每轮一句话总结)。注意它与 `Turn.summary`（finish 时生成、贴在该轮最后的一句话摘要）是**两个不同字段**——用户提案「recap 填到触发那轮的 summary」实现为写 `Turn.recap`、供折叠摘要行消费。`/restart` 后生效——每轮 recap 落 recaps.jsonl，下次折叠触发即见 recap 版轮次概览。

## 超深档折叠标注：0 次工具调用省略标注行（2026-08，commit feeb123）

**背景**：fc 折叠后，超深档历史的每轮折叠行格式为 `---- 已折叠共N次工具调用 ----\n\n{answer}`。**纯讨论轮**（架构评估类，一字工具没调）N=0 也照加标注行——用户实测指出两处观感问题：①「已折叠共0次」不传递任何信息，纯噪声；② 近 2 轮 answer 顶部标注次数与「实际工具调用次数」印象对不上，疑似统计错乱。dump 数据澄清②：那些轮**确实是 0 工具调用的真实讨论轮**（remote_tools 评估、server_id 评估），标注次数与 events 完全一致——数据没错，问题只在①的展示冗余。

**修复（session.py，commit feeb123）**：折叠渲染处 `n_calls = sum(len(s.tool_calls) for s in turn.steps)` 判空——`n_calls > 0` 才加标注行；`n_calls == 0` 时 content 直接是 answer 原文（`answer_reasoning` 照常附）。效果：折叠历史里 0 工具轮显示为纯净 answer，与「纯讨论轮」语义一致；非 0 轮标注照旧。

**注意**：该标注是**折叠历史渲染**的产物（见 [fc 大刀首折](#fc-大刀首折至少吞超深档一半2026-08commit-4d37e90)），与 `_folded_summary` 的轮次概览行（recap tail，见上节）是两套格式——前者保 answer 原文、后者保结构摘要。

## 前缀缓存三层优化（详见 blog/03）

1. **布局层**：易变块（时间/计划/召回/后台）统一收尾成 tail ambient，前缀区纯稳定
2. **轮间层**：分档冻结渲染（见上）+ **轮边界统一重排**（升档+折叠统一计划，见 [轮边界统一计划](#升档graduate-与折叠轮边界统一计划2026-08commit-1e9af8f)）；折叠为预期一次性 miss，见 [t206 实证](#折叠事件与缓存命中t206-实证2026-08)；正常轮边界平滑路径见 [t224 实证](#正常轮边界路径t224-实证2026-08)；超长轮保命阀例外见 [t228 实证](#轮内应急折叠保命阀t228-实证2026-08)。⚠️ 判阈依赖 `_estimate_tokens` 估算，口径已闭环（含 tools schema，见 [估算与校准口径闭环](#估算与校准口径闭环tools-schema-补齐2026-08)）
3. **轮内层**：分组衰减 + `_build` 以 `_planned_fold`/`_planned_graduates` 为起点**零调整**（顶满窗口时保命阀应急折叠例外，见 [轮边界统一计划](#升档graduate-与折叠轮边界统一计划2026-08commit-1e9af8f) / [t228 实证](#轮内应急折叠保命阀t228-实证2026-08)）

## usage 归一化（llm_call_log.normalize_usage）

各家缓存字段差异：GLM=`prompt_tokens_details.cached_tokens`，DeepSeek=`prompt_cache_hit_tokens`/`prompt_cache_miss_tokens`。写入 jsonl 前归一化为标准格式；读取侧 `cached_tokens_of()` 三级兜底（标准→DS hit→miss 推算），历史记录免迁移。

## provider 侧缓存坑（重要教训）

- **DeepSeek v4 system 规范化（2026-08-29 实锤，价差最大的坑）**：v4 后端对 messages 里**变化的 `role:system` 消息**做规范化处理（疑似前置/合并进缓存键），任一 system 内容变化（无论头部/中部/尾部）→ **全序列缓存断**（实测 6%，只残存头部 ~14k）。对策：动态注入（tail/钩子/before_turn）一律 **user role + `<system-reminder>` 标签**（已修，见上文「动态注入 user 化」）；**缓存价差：miss 单价 ≈ hit 的 30-50 倍**（官方账单 v4-flash 实测 hit ¥0.05/M vs miss ¥1.5/M = 30 倍），低命中时代价极大
- **tools 列表变化也全断（2026-08-29 实锤）**：messages 完全相同、tools 尾部 +1 工具 → 全断（实测 7%）。tools 在缓存序列最前。Agt 的 `refresh_workflow_tools` 是 `drop(wf_*)` + sorted(glob) 重注册——集合不变时每轮 byte-stable ✓；一次性动态注册（ensure_lsp 首调等）断一步后稳定，可接受。**勿在高频路径动态增删工具**
- **随机路由（2026-08-29 修正）**：旧判定「deepseek-v4-flash 随机分实例 → 命中恒 0」大概率是 system 规范化/tools 变化断裂的误判（当时未找到根因）——见下方实证。真随机路由无法客户端修，但先按三条铁律排查
- **per-token 隔离**：GLM 缓存按 api_token 隔离且容量有限 → 同 token 交错 react 长调用与 utility 短调用互相驱逐缓存 → **utility 必须独立条目+独立 token**；该类条目配 `"token_rotate": false`（sticky）。ModelScope 不吃缓存但按号限额度 → 多 token 预旋转分摊是刚需，保持默认 true。DeepSeek 曾疑同款 per-token 隔离——**2026-08-29 否证**（单 token 账号同样断，根因是 system 规范化）
- 判别：**单步深跌后立即恢复**=折叠事件（预期一次性成本，见 [t206 实证](#折叠事件与缓存命中t206-实证2026-08)）；**同轮连续两次深跌**=保命阀折叠目标太保守（见 [t228 实证](#轮内应急折叠保命阀t228-实证2026-08)）；骤降且与 utility 调用交错相关=驱逐；**DeepSeek 端持续低命中**=先查动态 system/tools 变化（见下方实证三铁律）。另注意：**折叠计划判阈的估算口径**——估算"以为达标"但实际超窗时，症状是新一轮初始 prompt 远超 75% 目标却折叠 0 轮（见 [估算与校准口径闭环](#估算与校准口径闭环tools-schema-补齐2026-08)）

### DeepSeek 缓存行为实证：v3 位置敏感 → v4 system 规范化（2026-08 两代后端）

#### 第一代实证（v3 后端，deepseek-chat 旧模型，2026-08-14 前有效）

**背景（用户怀疑）**：分层投影把 system 块分散在历史中间——怀疑 deepseek 端点默认把 messages 中所有 role:system 合并放到最前 → 规范化重排 → 缓存前缀断裂 → 命中率低。

**探针（probes/deepseek_cache_probe.py，当时后端）**：用模拟 agt 分层投影形态的 payload（system 分散中段 + 历史 reasoning_content + tool_calls 结构），5 组判别：

| 组 | 操作 | 结果 | 判定 |
|---|---|---|---|
| A 基线 | 同 payload 连发 2 次 | A1=0% → A2=91.5% | 缓存通道本身健康 |
| B 尾部追加 | hist + 一条 user | 90.8% | 前缀不变 → 跨请求命中 ✓ |
| C 中插 system | 历史中插新 system 块后重发 | C1=0% → C2=93.6% | 同 payload 正常 |
| D 位置重排（判别组） | S3 从中间挪到最前（其余字节完全不变） | D1(原位)=91.5% 命中 A；D2(挪前)=**0.0%** | **不做 system 合并** |
| E 去 reasoning | 历史 assistant 去掉 reasoning_content | E1(带)=91.5%、E2(去)=91.5% | reasoning 不参与缓存键 |

**v3 结论**：缓存按**原始消息序列位置敏感**、不合并 system；`reasoning_content` 不参与缓存键（历史 reasoning 放心回传）。**⚠️ 2026-08-29 起后端换代 v4，"不合并 system"结论已失效**（v4 对变化的 system 做规范化，见下）；"位置敏感/前缀匹配/reasoning 不参与"三条仍成立（v4 复测一致）。

#### 第二代实证（v4 后端，v4-flash/v4-pro，2026-08-29，13 轮探针链）

**触发场景**：comfy session t253——s0 冷启动后 s1-s3 命中仅 3-6%（8320/14080 恒定残段），而 glm-official 同投影 99.6%；用户自述 claude-code 里 deepseek 命中正常、别的时间段 Agt 也持续低命中。官方账单按小时对照：同 key 同日，cc（claude-code，00-07 点，单请求 2.5-6.7 万 tok）56-79% ✅、探针（21 点）87% ✅、**Agt（13-16 点，单请求 20-32 万 tok，81 请求 miss 1480 万）2-4% ❌**——账号/端点正常，问题特定于请求形态。

**排查链（probes/deepseek_v4_cache_probe2~13.py，逐步对齐变量全 99% 直到锁定）**：R2 真实投影重发/45s 间隔 ✓、R3 假 tools+真实投影 ✓、R4 增量（前缀 byte 同+尾部变）✓、R5/R7-R11 真实重建工具箱（99 个）/agent 消息形态（assistant+tool_calls+reasoning 回传+tool）/时序/参数/温度/OpenAI SDK 同栈 ✓——全部 99%。R12 复刻 agent 节奏（步进生长+**每步变化的 tail system**）→ **全 6% 复现**。R12b 变体矩阵锁定元凶：

| 尾部消息 | 内容 | 增量命中 |
|---|---|---|
| system | 固定 | 99% ✅ |
| system | 变化 | **6% ❌（恒定 14,080 残段）** |
| user | 变化 | 99% ✅ |

R13 机制判别：E3 头部 system 变化同样全断（5%，**位置无关**，"per-system 独立分层"假说被否定）；E5 **tools 尾部 +1 工具也全断**（7%，messages 全同）。

**行为模型（工程可依赖的三条铁律）**：

```
缓存序列 ≈ [tools] + [规范化的 system 块（疑前置合并）] + [其余消息原序]，前缀匹配
① 任何 system 消息变化（无论位置）→ 全断
② tools 列表任何变化（哪怕尾部 +1）→ 全断
③ user/assistant 变化 → 只断其位置之后，前缀照常命中
```

（"重排合并" vs "级联分层"两种机制解释在全部实验上等价，外部不可再分；行为模型已足够指导工程。）

**修复（当天落地）**：动态注入（tail 合并消息/钩子旁注/before_turn hint）role system → **user**，`<system-reminder>` 内容包裹保留——对齐 Claude Code 线上协议（其动态注入本就是 user role，故 cc 在 v4 上 79% 正常）。端到端验证：修复形态三步步进 99/99/99（修复前同形态 6%）。

**候选根因旧案销案**：multi-token 轮换 per-token 隔离——**否证**（单 token 账号同样断）；TTL——**否证**（45s 重发 99%，且断裂与间隔无关）；"随机路由"——大概率误判（见缓存坑条目）。

**成本量级**：comfy session 16 点段 81 请求 miss 1480 万 tok ≈ ¥22/小时输入费；修复后按 90%+ 命中（miss 价 30 倍于 hit）输入费降约一个量级。GLM 的 miss/hit 价差约 4 倍——DeepSeek 对缓存的敏感度比 GLM 高一个数量级，长会话优先保证 DeepSeek 前缀稳定。

> 探针族留存 `probes/deepseek_cache_probe*.py`（v1=五组判别、2~13=R 系列变量对齐链），读取 models.json 的 deepseek profile 直连官方端点，`prompt_tokens_details.cached_tokens`/`prompt_cache_hit_tokens` 读命中；DeepSeek 再换行为可复跑对照。⚠️ 探针必须放 `probes/`（**勿放 `tools/`——那是 script_tools 的插件扫描目录，import 即执行顶层代码**，曾致 agt 启动自动烧探针 token；全部探针已加 `__main__` 保护双保险）。

## 相关页面

- [长期记忆](../features/longterm-memory.md) — episodic 召回（tail ambient `[epi·长期记忆]` 行来源）的检索流水线与演进
- [multi-agent](multi-agent.md) — 子 Agent assembly DSL（`|optional` 段默认不装配、`=on` 按需打开）与 reuse 隔离
- [运维与排障](../guides/ops.md) — /stats 页、投影转储、/context live 分段统计、常见错误对照
- [系统总览](../architecture/overview.md) — 模块地图、数据流
