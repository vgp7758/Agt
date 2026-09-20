# 上下文引擎与缓存优化

> src/session.py 核心设计。详细分档机制见 [docs/architecture/02-session-memory.md](../../../docs/architecture/02-session-memory.md)，本页补充 docs 未收录的 2026-08 演进（分组衰减 / usage 归一化 / provider 缓存坑 / 折叠实证 / 轮边界统一重排 / 轮内应急折叠保命阀 / 投影转储文件名 / 投影分段统计 / 估算与校准口径闭环）。

## 投影总览（messages_for_llm 装配顺序）

**2026-08-29 起系统信息合并形态**（`_walk_plan` 走查，见下方专节）：连续的系统信息段合并成一条 system，动态注入一律 user role；**2026-08-31 起 tail 段不再独立成条——装配后并入最后一条 message 的 content 末尾**（用户方案 bafaf7e，见下方「tail 并入末条 content」节）；**2026-09-07 起 recent-file 快照段式化——不再内嵌 tool result 尾部，独立 `recent_file` 段走装配清单（steps 后、tail 前，与 tail 同桶并入末条 content 的 system-reminder）**（用户方案，见「recent-file 跟屁虫快照」节）：

```
[1条 system]       system + rules + asm 动作项（text/file/dir/cmd/workflow/tool）裸文本合并
[tiered history]   分档投影（需 provider 配 max_effective_context_window）；摘要消息仍独立 system（frozen）
[1条 system]       ltm 静态层（独立成条——默认清单里被 history 隔开）
[current turn]     user_message + before_turn hint(user role) + steps（工具调用按分组衰减）+ pending hints
[tail merge]       recent_file 快照段 + 一组 <system-reminder>（时间/后台任务/计划/episodic 召回）——**并入最后一条 message 的 content 末尾**（2026-08-31 起，用户方案 bafaf7e：不额外创建一条 message——正常情况下末条是 user/tool 非 assistant；末条是 assistant 或空时才回退独立 user 消息；2026-09-07 起 recent_file 段同桶并入）
```

assembly DSL（子 Agent 声明）段可带 `|optional` 尾标——**2026-08（commit 1e3b206）起真语义：标记即默认不装配**（`messages_for_llm` 的 seg 分支对 `opt=True` 的项跳过），`agent_prompt assembly="seg=on"` 清标记打开、`=off` 移除；未标记段列出即装，必装 system/user_message/steps 未列出自动补插；`reuse`（current_turn_only）与 opt **正交叠加**（reuse 时 history 强制关）。详见 [multi-agent · assembly DSL](multi-agent.md#assembly-dsl上下文装配配方)。

装配时顺手记录分段统计到 `_proj_stats`，并覆盖写旁车 `session_dir/proj_stats.json`（含档位边界快照，2026-08-29 起——`/context` 三级读取：内存 live → 旁车 sidecar（跨重启）→ 现算兜底，见 [投影分段统计](#投影分段统计-context-改读真实投影缓存commit-4212f65)）。

episodic 召回行（`[epi·长期记忆]`）由 before_turn 检索工作流产出、注入 tail ambient——演进史与中文命中率坑见 [长期记忆](../features/longterm-memory.md)。

## 投影三区重构：tail 拆段 + 区3 统一包裹 + 钩子 merge 化（2026-09-01，用户提案）

**一句话**：tail merge（2026-08-31，bafaf7e）再进一步——tail 从写死的一块拆成六个可配子段（time/system/plan/spec/episodic/remote），steps 之后的全部动态内容（tail.* + asm 动作项 + 钩子旁注）统一 `<system-reminder>` 包裹后 merge 到末条 content 末尾。消息序列形状完全由对话本体（history/user/steps）决定 → byte-stable 最大化。同批补上钩子注入的落盘可追溯性（hook_note 事件，见 [workflow-hooks · 钩子注入 merge 化 + hook_note 落盘](workflow-hooks.md#钩子注入-merge-化--hook_note-落盘2026-09-01)）。

**三区划分（src/session.py `_walk_plan`，`passed_steps` 三区状态位——steps 段处理过后进入区3）**：

| 区 | 内容 | 形态 |
|----|------|------|
| **区1 头部** | system/rules/ltm + 头部 asm 动作项 | 合并成一条 system（2026-08-29 `_walk_plan` 形态延续）——**头部不因 tail 变化而变**（byte-stable 关键） |
| **区2 对话本体** | history（分档/窗口）+ user_message + steps | 唯一随轮演化的部分——**消息形状由它决定** |
| **区3 尾部动态** | steps 之后的 asm 动作项 + tail.* 六子段 | 统一 `<system-reminder>` 包裹 → merge 到末条 content 末尾（2026-08-31 tail merge 的推广；`tail_merge_text` 收集桶成分改为「区3 收集桶」） |

**tail 拆段（写死内容组装化，用户提案落地）**：

- `_DEFAULT_ASSEMBLY_PLAN` 尾部从单一 `tail` 段拆成六个 `tail.*` 子段：time / system / plan / spec / episodic / remote——各自可配顺序/开关/增删
- **旧 yml 兼容**：`set_assembly_plan` 对旧 `tail` 段自动 `_expand_tail()` 展开成六子段——yml 不改不断；`_assembly_once_cache` 一并失效重算
- `_tail_block_msgs(name)`：按子段名取各自 provider 的块 → `_ambient_group` 分组渲染；返回 `[]` 则该段不出现（零噪声）
- **remote 迁移（动态内容放尾部）**：远程实例清单从 SYSTEM 头部（main.yml 的 `{func:load_remote_instances()}` 占位）迁移到 `tail.remote` 段——agent.py 挂 `_remote_provider = _func_remote_instances`；实例连接/断开变化只落在区3 末条，零缓存代价（此前在头部，变化全序列断）

**钩子注入 merge 化（同批，src/agent.py）**：

- **before_turn hint**（`_current._before_turn_hint`）：不再独立成条——merge 到当前轮 user 消息 content 末尾（触发位置的上一条 = 当前轮 user）；跨轮变化只影响本条（本来就在未命中区），消息形状由对话本体决定
- **before_tool / after_tool / before_answer 的 notes**：按 hook 位置分组（`OrderedDict`，hook 名归组）渲染后 merge 到 `msgs[-1]`（触发位置的上一条：最后的 tool result / user）content 末尾——多个位置组按 `pos` 属性顺序追加到同一条末尾
- **兜底**：msgs 空或末条是 assistant（重做草稿场景，不污染草稿语义）→ 回退独立 user 消息（旧行为）

**缓存收益（为什么这是形态终局）**：

```
之前：tail 独立 user 消息 + before_turn hint 独立 user 消息 + notes 独立 user 消息
       → 每步动态内容增加 1-3 条消息 → 消息序列形状每步都变 → 缓存边界漂移
现在：所有动态内容（tail/钩子/hint）merge 到现有消息的 content 末尾
       → 消息序列形状完全由对话本体（history/user/steps）决定 → byte-stable 最大化
```

**最终消息形状**：

```
[system]   区1 头部合并（system+rules+ltm+text...）
[user]     history 末条 / 当前轮 user（含 before_turn hint merge）
[tool]     工具结果（含 after_tool hook merge）
[assistant] 模型回应
...
[user]     最新 user（末条含 <system-reminder> 统一包裹的 tail.* + asm + hooks）
```

（末条 assistant / msgs 空 → 兜底独立 user 消息。）

**验证**：三区形态 / byte-stable（头部不因 tail 变化而变）/ 旧 yml 兼容（tail → 六子段展开）/ hook merge / hook_note 落盘——全过。需 `/restart` 生效（commit fc3db93）。

### 后记（2026-09-02，commit 504a518，用户裁定）：tail.* 拆段撤销——恢复单一 tail 段，动态内容 func 项化

**用户裁定（2026-09-02，commit 504a518）**：「没必要定义 {seg:x} 和 tail.* 这些东西——在 steps 后面直接添加 func 项就可以了」。已定义很多段名，动态内容不需要专门段名：**三区「steps 后全进区3 merge」语义下，func 项放清单尾部就是 tail 位置**。

**撤拆段（src/session.py）**：

- `_DEFAULT_ASSEMBLY_PLAN` 尾部恢复**单一 `tail` 段**（六 `tail.*` 移除）；`_expand_tail` 恒等直通（保留方法兼容调用点，注释记根因）
- `_walk_plan` tail 分支走整段：`_seg_msgs_tail()` 一次性收集全部 provider（time / system_extra / plan / spec / episodic）→ 区3 merge 语义不变（并入末条 content 末尾，`<system-reminder>` 统一包裹）
- `_tail_block_msgs` / `_TAIL_EXPAND` 保留但不再被走查调用（legacy——兼容旧 yml 手写的 `tail.*` 段名；`set_assembly_plan` 的 `tail.*` 开关判定仍在）

**动态内容 func 项化（src/agent_config.py）**：FUNC_REGISTRY 注册 `print_time()`（实时时段块——替代 tail.time）/ `get_team_profiles()`（团队成员看板——替代 tail.system 团队部分）；`load_remote_instances()` 已有——远程实例清单从 `tail.remote` 段回到 func 动作项形态（声明需要就自己放清单尾部，如样板 func 三件套）。

**区3 三区划分与 merge 语义全部保留**——撤的只是「写死内容拆成六段名」这层过度设计；区1/区2/区3 边界、byte-stable 收益、钩子 merge 化不受影响。声明文件同步改（team-manager.md / wf-calibrator.yml / wf-designer.yml——裸字符串段 + func 项）。形态细节与撤除清单见 [multi-agent · 段形态简化定稿](multi-agent.md)。

### tail 段剥自带 system-reminder：双层嵌套修复（2026-09-02）

**现象（2026-09-02，本 session 每步注入实锤）**：投影里出现**双层 `<system-reminder>` 嵌套**。

**根因（两层包裹叠加，src/session.py `_walk_plan` tail 段分支）**：tail 子段经 `_ambient_group` 分组渲染**自带一层 `<system-reminder>` 包裹**；而区3 merge（并入末条 content）又**统一再包一层**——两层叠加（`<system-reminder>\n<system-reminder>…</system-reminder>\n</system-reminder>`）。

**修复**：tail 分支收集子段文本（`_b = "\n".join(...)`）时**剥掉自带包裹**——`startswith("<system-reminde"` 形态判断后剥除首尾标签，只留 merge 段统一包的那一层。空段自动跳过（零噪声）语义不变。其它动态块（钩子旁注 / before_turn hint）原本就只 merge 包一层，不受影响。

### steps 段注入模式：steps=reasoning（2026-09-02，用户提案）

**提案（用户，2026-09-02）**：steps 段可带参数声明其后尾段的注入姿势，两种模式：

| 模式 | 行为 |
|------|------|
| `reminder`（默认） | 区3 收集桶（steps 后的段/动作项求值合并）→ `<system-reminder>` 包裹 → 并入最后一条 message 的 content 末尾（三区重构语义，不变） |
| `reasoning` | **作为思考链一部分注入**：user_message 之后**最后一条 assistant** 的 `reasoning_content` 前缀——`"当前状态：{result}\n" + 原reasoning_content`（消息形态 `{"role":"assistant","tool_calls":[...],"content":null,"reasoning_content":"当前状态：…"}`）；原 reasoning 为空则只注前缀。语义：环境状态是**思考的输入**而非对话内容，不占 content 位。 |

**回退**：s0 首步（user 后还没有 assistant）→ 回退 reminder 模式（不能注入到 history 里上一轮的 assistant 上）。

**空槽承载（用户提案·同日补充）**：默认 reminder 模式下，若模型 `requires_reasoning_in_history=true`（DeepSeek 思考模式——历史带 tool_calls 的 assistant 必须有 reasoning_content 字段，`_build_kwargs` L432-439 发请求前给缺字段的补**空串占位**）且末条 assistant 的 reasoning 为空 → **也注入**（`"当前状态：{result}"`，末条 content 不再叠加 system-reminder）：空槽反正要占位，放动态状态严格优于空占位——信息量↑、缓存代价相同（都在未命中区）、占位逻辑跳过已带字段（注入值直接生效）。reasoning 非空（模型产生了真实思考）→ 不打断，保持 reminder。meta：`requires_reasoning·空槽承载动态状态`（/context 可观测）。

**DSL 三形态 + 覆盖**（src/multiagent.py）：`steps=reasoning`（yml 裸字符串）/ `{steps: reasoning}`（dict）/ `{seg: steps=reasoning}`（seg 做值——本批顺带修复该形态此前被静默丢弃的既有 bug，现委托 `_asm_item_from_str` 全语义解析）；`agent_prompt(assembly="steps=reasoning")` 参数覆盖（在必装检查前拦截；base 清单无 steps 项时也插入——否则投影走默认清单丢 mode）。非法值 warning + 按默认 reminder。

**实现（src/session.py `_walk_plan`）**：steps 分支记 `tail_mode`（item.mode）；user_message 分支记 `user_end_idx`（reasoning 注入的搜索起点——只认 user 之后的 assistant）；区3 merge 双模式分派——reasoning 找到目标后**浅拷贝替换**（`{**msg}` 不就地改，防污染 session 数据），`content`/`tool_calls` 不动；`_sec` 记 `reasoning前缀注入末条assistant(#idx)`（msgs=0，/context 可见）。

**兼容性**：DeepSeek 占位（`requires_reasoning_in_history`）只补 `"reasoning_content" not in m` 的消息——注入后的值不被覆盖；末条 assistant 本在未命中区，缓存代价与 reminder 模式相同。

**验证**（mock Session 全链路）：reasoning 注入末条（多 text 项空行连接合并）✓ / 无污染（`_current.steps` 原文不动）✓ / 显式 reminder = 默认行为 ✓ / s0 回退 ✓ / `messages_for_llm` 全链路 ✓ / DSL 解析 6 形态 ✓ / overrides 三场景（已有 steps 改 mode / 空 base 插入 / steps=off 拒绝）✓。

管理页（/agents）steps 段增模式下拉（默认/reminder/reasoning）；hint 文案同步。需 `/restart` 生效。

#### 粒度演进：steps 全局 → 逐动作项 pose 双桶（2026-09-03，commit 24597f3，用户提案）

**用户请求（2026-09-03）**：「我又想了想，我们把 steps 后面的下拉框（附加在正文尾部/注入思考链）改成在后面的各段分别通过下拉框选择吧」——注入姿势从**单点全局**（steps 一处声明管其后所有段，dd5b0b4）演进为**每个动作项各自选择**（粒度变化；两种姿势的注入语义本身不变）。

**DSL（src/multiagent.py）**：动作项 dict 支持 `mode:` 键——`{func: load_models(), mode: reasoning}`；`mode: reminder|reasoning`（默认 reminder）。`_ASSEMBLY_STEPS_MODES` 枚举保留做校验（非法值 warning + 按默认 reminder）。`steps=reasoning` 段级声明**保留**（改写 `tail_mode`，语义降为「**未标 pose 的动作项**的整体默认」——**2026-09 起该段级写法废弃，见下方后记**）。

**引擎双桶（src/session.py `_walk_plan` 区3）**：steps 之后的 asm 动作项不进 run 缓冲（不独立成条），按每项 pose（`item.mode`）**归双桶**：

| 桶 | 收纳 | 装配后去向 |
|---|---|---|
| `tail_merge_text` | 默认/reminder 项 + tail.* 块 | `<system-reminder>` 包裹并入末条 content（三区 merge 语义不变） |
| `reasoning_merge_text` | reasoning pose 项 | 作为思考链注入末条 assistant `reasoning_content` 前缀（`"当前状态：{result}\n"`+原文；s0 无 assistant 回退并入 reminder 桶——文字不丢） |

同姿势**合批**：每轮至多两批注入，不逐段 create 消息——缓存友好（不新增断点）；DeepSeek 空槽承载语义对 reasoning 桶同样适用（末条 reasoning 空则注入值直接生效）。

**管理页（src/static/agents.html）**：steps 段的全局模式下拉**删除**（history 段 mode 文本框保留）；**非 seg（动作项）行**（text/file/dir/cmd/workflow/tool/func）各自新增 pose 下拉：`并入正文(reminder)`（默认）/ `注入思考链(reasoning)`——`onchange` 写 `asmData[i].mode` + 重渲染；rowToItem 动作项把 mode 写回 `mode: xxx` 保往返；hint 文案同步（「每个动作项可选注入姿势…steps 后的项想当环境状态给模型思考看就选它」）。`/restart` 生效（agents.html 随启动载入内存）。

**mock 装配验证**（关键形态）：

```
[2] assistant (tool_calls)                                        ← 末条 assistant 候选
[4] assistant reasoning_content='当前状态：val:A⏎⏎val:B⏎我先想'      ← 两个 reasoning 项合成一次注入
[5] user content='当前问题⏎<system-reminder>val:C⏎</system-reminder>' ← reminder 项独立并入
```

语义清楚：A/B 放进 assistant 思考槽（环境状态当「想」的输入），C 走对话正文备注。UI 细节见 [agents-admin · 动作项 pose 下拉](../features/agents-admin.md)。

#### 后记：段级旧写法 steps=reasoning 废弃——姿势收敛到逐动作项 pose（2026-09，commit b60cce8，t781_s2 投影实证）

**实证（t781_s2 投影）**：段级 `steps=reasoning`（2026-09-02 引入；24597f3 后语义=「未标 pose 动作项的整体默认」）的实际影响面比设计大——它改写的 `tail_mode` 会把 **tail / recent_file / 钩子旁注等无姿势选择器的内建段**一并拖进思考链（作为 reasoning_content 前缀注入），而这些段本应走 reminder 桶（`<system-reminder>` 并入正文）。

**废弃（commit `b60cce8`，随 [v0.27.0](../releases/v0.27.0.md) 发布）**：段级 `steps=reasoning` 写法废弃——声明不再生效，无姿势内建段回归 reminder 桶；注入姿势的声明入口**收敛为逐动作项 `mode:` 键**（pose 双桶）——想进思考链的段/动作项逐项标注，其余一律 reminder。

（上方「段级声明保留 / 语义降为整体默认」为 2026-09-03 时点状态，已被本后记取代。）

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

### /context 展示侧两修复：scene 精确匹配失配 + markdown 段落表（2026-08-31，commit 3ae7a76）

**① 「最近 react 调用 41.3 小时前」——scene 精确匹配失配**：用户报告 /context 顶部时间戳停在 41.3h 前。根因（src/commands.py 取最近 react 记录处）：

```python
if r.get("scene") == "react" and r.get("outcome") == "success":   # 旧：精确匹配
```

scene 增强后（2026-08-29 起，格式见 [ops · scene 取值](../guides/ops.md#llm_callsjsonl-每条记录)）react 主循环记录的 scene 是 `react·_main_`（带 agent 后缀）——精确匹配 `== "react"` **一条都命中不了**，reversed 扫描一路落到增强部署前最后一条纯 `"react"` 记录（41.3 小时前）。修复：

```python
if str(r.get("scene") or "").startswith("react") and not (r.get("error") or ""):   # 新：前缀匹配
```

实测命中 0 分钟前（glm-official）✓。**教训（又一例「功能增强打破下游假设」）**：scene 加 agent 后缀时，[ops.md](../guides/ops.md#llm_callsjsonl-每条记录) 早已写明新格式，但 /context 这个消费端仍用旧精确匹配——**改字段语义必须全局搜消费点**（与 [事件流 agent_id 打标](multi-agent.md#事件流-agent_id-打标与-webui-串台修复2026-08-21commit-ba0940b) 时的「收口一处全覆盖」同属一条纪律的两面）。

**② 段落表 markdown 表格化（用户「能不能用 div 约束一下让几列对齐」）**：段落构成此前用 `{name:<28}` 空格填充对齐——**中文段名 CJK 显示宽度 2、格式化按字符数 1 计**，列永远参差。改 markdown 表格：

```
| 段落 | 估算 tok | 占比 | 图表 |
|---|---:|---:|---|
| 折叠摘要(227轮) | 25,164 | 7.7% | ███████████████ |
| **合计** | **435,409** | **100%** | 756,568 字符 / 527 条消息 |
```

WebUI 的 markdown 渲染自动变对齐表格（表格单元格即天然 div 约束）；CLI 裸文本 `|` 分隔仍可读。段名 meta（「工具结果上限 xxx 字/步」等说明）并入图表列尾部。/restart 生效。

## 分档投影（轮间）

- 每档字数上限：1500 → 750 → 375 → 187（`_tier_limit`，detail_base 减半递进）
- 同档位**冻结渲染**（byte-stable）：档内内容字节级不变 → 前缀缓存可命中
- 全档满 → 折叠成结构摘要（fold），原文仍在 turns 可 recall（[recall_turn 工具](../features/recall-tools.md) 命中即整段 user+answer 原文召回）
- **压力动作阶梯（2026-09-14 起）**：顶窗后 `_plan_fold` 按「**① 升档（无损，只降文字档上限）→ ② 推老档进工具折叠档（`_deepen_oldest_tier`，阶梯中间一级）→ ③ 折叠（按轮吃到水位）**」顺序压到保留线；三处规则改动与 862 轮真实回放对照见 [压缩阶梯三改 + 投影模拟器](#压缩阶梯三改--投影模拟器先升档推老档按轮吃2026-09-14用户提案)

#### 历史补记 · fc 大刀首折：至少吞超深档一半（2026-08，commit 4d37e90）

**背景（用户裁定 2026-08-28）**：边界密集（滚动毕业 ~1.9 轮/边界）时碎刀偏勤——每轮边界触发折叠、每刀只折 1-2 轮，超深态（answer/reasoning 原文保留）留存太短，从[超深档工具调用折叠]到[fc 折叠为摘要]过于频繁。裁定：**每次首折至少折叠超深档的一半以上**。

**实现（src/session.py `_fold_leap_target(fc)`）**：

- 超深段 = `[fc, bs[-max_level]]`（最后一个超深轮 = 倒数第 max_level 个边界）
- 目标 = `fc + (段长 // 2)`，对齐到合法折叠点（boundary+1）
- 三态退化碎刀：fold_deep_tools 关 / 档梯未满 / 超深段已折完

两处消费点：`_plan_fold` 折叠循环（首刀 fc==0 大刀，之后仍超线再碎刀微调）+ 保命阀应急循环（同款，首刀从 `_planned_fold` 起点大刀）。

**对照验证（203 密集边界 / 406 轮，本 session 真实形态）**：旧碎刀一次 `_plan_fold` 吞 52 轮；新大刀一次吞 200 轮（超深段 398 轮过半）——触发间隔约翻 4 倍，超深态平均留存同幅延长。

**缓存经济无损**：`_folded_summary` 是尾部追加式（吞新段只往摘要末尾加行，不动已有前缀）——大刀与碎刀不互相破坏缓存，区别只在触发频率，而频率降低本身即缓存友好。

> **后记（2026-09-14）**：本节的「碎刀微调」已被 [压缩阶梯三改](#压缩阶梯三改--投影模拟器先升档推老档按轮吃2026-09-14用户提案) 的**按轮吃到水位**取代——首刀大刀语义不变（`_fold_leap_target(fc, est_fn, target)` 增参），但起点之后的微调不再按 boundary 整档吞，而是从当前 fc **按轮**精确吃到达标（不多吃一轮、不产生碎刀）；同时压力循环在升档与折叠之间插入「推老档进工具折叠档」一级。

### 压缩阶梯三改 + 投影模拟器：先升档→推老档→按轮吃（2026-09-14，用户提案）

**用户提案（2026-09-14）**：「改了以后可以通过模拟器按照 events.jsonl 从头模拟着跑一遍，看看这个规则跑下来整个上下文会是什么形状」——先补三处阶梯规则，再用真实 session 回放对照新旧。

**三处改动（src/session.py）**：

| # | 改动 | 效果 |
|---|------|------|
| ① | **新增 `_deepen_oldest_tier(fold_count=None)`** | 升档刀切完（年轻端无物可切）后，在**未折区最小边界处再插一刀** → 最老那一档整档 +1 级；跨过 `max_level` 即整档进**工具折叠档**。插小位置 → 更年轻的轮 `count` 不变、保真零损失 |
| ② | **`_next_fold_target(fold_count, est_fn=None, target=0)` 改「按轮吃到水位」** | 被折叠的轮不再参与档位渲染 → 对齐 boundary 已无意义；从 `fold_count` 起**按轮**吃，返回恰好达标（估算 ≤ target）的轮数——一次到位，既不多吃一轮，也不产生「每刀只折 1-2 轮」的碎刀（碎刀每轮都断前缀缓存）。不带 `est_fn` 时保持旧语义（超过 fold_count 的最小 boundary+1，按整档吃） |
| ③ | **`_plan_fold` 末尾 prune `< fc` 死边界** | 已折叠轮不参与渲染 → `raw_level(i>=fc)` 只数 `b >= i` 的边界，`< fc` 的边界是死重，清掉。对未折轮零影响（已论证 + 实测） |
| ④ | **`_fold_leap_target(fc, est_fn, target)` / `_deepen_oldest_tier(fold_count)` 增参** | 压力循环与保命阀应急循环统一传当前 fc（保命阀从 `_planned_fold` 起点而非内存值）；`_last_boundary()` 取代 `_tier_boundaries[-1]` 裸取（bisect 引入） |

**`_plan_fold` 压力循环新顺序**（先升档 → 再推老档 → 最后折叠）：

```python
if self._graduate_once():                       # ① 先升档（无损：只降文字档上限）止血
    continue
if self._deepen_oldest_tier(fold_count):        # ② 再推老档进工具折叠档（阶梯中间一级）
    continue
# ③ 应急首刀同款大刀（超深一半）；起点之后的微调碎刀
```

**模拟器（tools/proj_simulator.py，用户提案）**：按 `events.jsonl` 从头重放 session，逐轮跑 `_plan_fold`，输出形状演化（fc / 边界数 / 各档轮数 / 工具折叠档 / 投影 tok / 动作）；`--rule old|new` 同一条 events 流对照；`--every N` 采样、`--csv` 落盘逐轮快照。自测矩阵 `tools/proj_simulator_selftest.py`（17/17 通过：阶梯顺序 / 推老档精确行为 / 达标即停不多吃一轮 / `fold_deep_tools` 关不回归 / prune 零影响 / restore 截断）。

**862 轮真实回放（形状演化，新规则）**：

```
轮号    fc   边界  档1 档2 档3 档4 档5 档6  折叠档   投影tok    动作
  61     0    1    31  30   0   0   0   0      0   140,259  升档1刀
 211     0    6    31  30  30  30  30  30     30   444,758  升档1刀   ← 工具折叠档 30 轮
 271     0    8    31  30  30  30  30  30     90   525,994  升档1刀   ← 90 轮
 299   174    5     0  29  30  30  30   6      0   349,240  升档2刀+折叠+174轮
 837   713    5     0  22  30  30  30  12      0   349,345  升档2刀+折叠+145轮
```

每次顶窗的动作序列都是**先升档（1~2 刀）→ 不够才折叠**（阶梯顺序正确）；中间段（轮 181~271）升档把 90 轮整档推进工具折叠档——即「先整档进工具折叠档」。

**新旧对照（同一条 events 流）**：

| | 旧规则 | **新规则** |
|---|---|---|
| 终态 fc | 784（91.0% 折叠） | **713（82.7%）** → 多保留 **71 轮原文** |
| 终态投影 | 514,426 tok（73.5% win） | **477,879 tok（68.3%）** |
| 档位分层 | 档1=32 档2=16 档3=30（3 档） | **档1..档6 全有轮**（25/22/30/30/30/12） |
| 工具折叠档峰值 | 30 轮 | **99 轮**（真正承压） |
| 残留边界 | 32 个 | **5 个**（prune 后，meta.json 更小） |

**三个诚实的发现**：

1. `_deepen_oldest_tier` 在真实回放里触发很少——`_graduate_once` 在多数场景已足以形成超深档，deepen 是「升档推到极限仍不够」时的兜底（自测用手工边界状态精确验证其行为：est 39,846 → 5,095（−87.2%）、重复边界=多切一刀、已在超深档则不再空转）。
2. 主要收益来自改动 ②（按轮精确吃）：不再「整档吞」→ 折叠量下降、档位分层变细——713 vs 784 的主因。
3. 开发中抓到一个性能坑：初版推老档「每刀全量重渲染」，300 轮超深档时单刀 ~1s，大 session 直接卡死 → 已改为**先估再动 + 收益 <2% 不动**（862 轮回放 37s 跑完）。

**验证**：场景矩阵 17/17 通过；`verify_assembly.py` 的 4 项失败与 `verify_events.py` 失败用 `git stash` 跑原始代码同样失败（既有问题，与本次改动无关）。对照数据留 `sim_new3.csv` / `sim_old.csv`。需 `/restart` 生效（改的是 session.py）。

**用法**：`python tools/proj_simulator.py <session_dir> --rule new --every 50 --csv out.csv`（换 `--rule old` 复现对照）。

#### 随 v0.28.0 发布（2026-09-14）

**发布（2026-09-14，随 [v0.28.0](../releases/v0.28.0.md)）**：本节三改 + 模拟器随 v0.28.0 上线 PyPI + Git（发布提交 `1f66a4e`，VERSION 0.27.2 → 0.28.0）。同版随包分发的相关项：`/api/callback` 回调 header 鉴权（此前修复仅本地生效，见 [scnet-async-pipeline](../features/scnet-async-pipeline.md)）。改的是 session.py（引擎层），`/restart` 即生效；已有长 session 下次顶窗时新阶梯自然接管。

### 三项调整：卫生毕业 15 轮 / llm_calls 附投影分布 / 超深档不投影 reasoning（2026-09-16，用户提案，commit 17af0a8）

一轮三项投影侧调整（用户一条提案打包，commit `17af0a8`）：

**① 卫生性强档阈值 60 → 15（src/session.py `GRADUATE_FORCE_TURNS`）**：当前档超过 15 轮时，无窗口压力也分批升前 30 轮——防档1 无限膨胀的卫生线收紧。该机制 v0.21.1 引入时阈值 60（诱因：8000 实例档1 膨胀到 64 轮/58.6%）；[压缩阶梯三改](#压缩阶梯三改--投影模拟器先升档推老档按轮吃2026-09-14用户提案)后升档路径更精细，卫生线同步收紧到 15。`/restart` 生效。

**② llm_calls.jsonl 附投影分布 proj 字段（src/agent.py）**：react 主调用记录时附 `proj=[{n:段名, tok, pct}]`——口径取 `projection_breakdown()`（真实装配统计，与 [/context](#投影分段统计context-改读真实投影缓存commit-4212f65)、旁车 proj_stats.json 同源）。此前要看「这次调用时上下文由什么构成」只能事后翻投影转储或现跑 /context。
- 条件：`scene` 以 `react` 开头且 `_proj_stats` 非空（钩子/recap/debug 等场景不附）
- **顺带修隐患**：record wrapper 原捕获的 session 引用在 load_session 换档后会失效（recorder 留旧 session）——改为持 `_agent`、动态取 `self.session`
- 消费端见 [ops · /stats 投影分布 tooltip](../guides/ops.md#stats-页webui--统计按钮)；老记录无 proj 字段向前兼容

**③ 超深档（工具折叠档）不投影 answer_reasoning（src/session.py）**：fold 渲染分支删除 `reasoning_content` 附加——用户裁定「answer content 原文中的信息量已经很充足了」；标注行 + answer 原文构成不变（0 工具轮省标注行逻辑照旧，见 [超深档折叠标注](#超深档折叠标注0-次工具调用省略标注行2026-08commit-feeb123)）。**正常档位（档1..档N）照旧保留 reasoning**——只砍折叠档，省的是折叠档逐轮 reasoning 的 token（折叠档越深越常驻）。

**验证**：模拟 react 记录带 proj、hook 场景不带；fold 轮投影无 reasoning / 正常档有。Python 侧 `/restart` 生效，前端 Ctrl+F5。

#### 后记：卫生毕业参数用户裁定修正——触发线 30 / 每刀 15（2026-09-16 二轮，commit 82f6265）

**用户裁定（2026-09-16 二轮，commit `82f6265`）**：「卫生性强制毕业应该是当前档超 30 轮时触发，毕业当前档的前 15 轮」——上节 ① 的参数语义两处修正（触发线与每刀批量各归各位）：

| 参数 | 17af0a8 版（误） | 终版 |
|---|---|---|
| 触发线 `GRADUATE_FORCE_TURNS` | 15 | **30**（当前档 >30 才触发） |
| 每刀批量 | 复用 30（一刀升前 30 轮） | **新常量 `GRADUATE_FORCE_BATCH = 15`**（每刀只升当前档前 15 轮） |

**实现**：`_graduate_once()` 增 `batch` 参数（默认 `GRADUATE_BATCH_TURNS=30`）——**压力驱动路径行为完全不变**，只有卫生性循环传 `batch=15`；循环到当前档 ≤30 停（**触发线与停刀线同一阈值**，语义自洽：档1 常态在 15~30 轮间滚动，每次动的都是最老的 15 轮，近期窗口保真区更整齐）。

**模拟验证（6 场景全绿）**：29 轮不触发 / 31 轮 1 刀剩 16 / 45 轮 1 刀剩 30 / 46 轮 2 刀剩 16 / 70 轮 3 刀剩 25 / 档内 40 轮 1 刀剩 25。`/restart` 生效。

#### 后记：proj 链路当日排障闭环 + tooltip 内联条形 v2（2026-09-16，commits d425358 + d4796e3 + a25d389）

llm_calls 附投影分布（本节三项之一）首日即被用户实测抓出**两个断点 + 一次形态重构 + 两次渲染层修复**：①**记录侧**——`set_session` 裸赋值把 `__init__` 挂的 recorder wrapper 覆盖（读档路径全裸；用户以为「前面有后面没有」，实扫 14514 行 0 条 proj）——修复抽 `_install_recorder(session)` 单一出口，`__init__` 与 `set_session` 都调用、wrapper 内动态取 self.session（d425358）；②**端点侧**——`/api/stats` 端点白名单构造 recs 手工列字段漏 `proj`（记录/前端都好、字段没出 server，tooltip 永远空；d4796e3）；③**形态 v2**——tooltip 投影分布改每段行内断点条：条形横向位置=该段在总条中的偏移、断点字符 █（白）、断点前 ─ 绿（命中）后红（重算）、条形区左端 tspan 绝对定位对齐（a25d389，用户 ASCII 草图设计，纯前端 Ctrl+F5 生效）；④**渲染坐标（同日二轮，a59f8c0）**——v2 段行 y 用了自创公式、未接续 headL 标题行距体系，差 11px 导致「投影分布」标题行与首段背景块重叠压字——修复=段行 y 接续标题公式 `by + 19 + (headL.length + i) * 15`，背景顶=基线−11；⑤**运行时 TypeError（同日三轮，d0825ec）**——断点反推 `missTok`（prompt−cached）声明 `const` 却在逐格分配循环里扣减，每次拖拽 mousemove 触发一次 `show()` 抛一条 `Assignment to constant variable`（用户贴控制台 **22 连**）——修复 const→let；`node --check` 只查语法不查 const 赋值，④⑤**连续两次「验证通过但运行时炸」**同模式，前端交互改动的验证自此升级为 **playwright 真页面 + console 收集**（本例拖拽 14 次移动 0 错误）。闭环全记录见 [ops · proj 链路排障闭环](../guides/ops.md#llm_calls-proj-链路排障闭环三连--tooltip-内联条形-v22026-09-16)。

#### 后记二：v3 形态 + 断点段对齐 + 断点竖线贯穿修复（同日续三连，commits c102a71 + 7debb43 + 26e05e2）

上记五连之后同日又续三轮（前两轮曾漏记，随 ⑥ 修复一并补录）：

- **v3 背景色块形态（c102a71，用户设计）**：v2 的 tspan 100 格条形有对齐瑕疵 → 每段整行背景 rect 红绿填充（断点前绿后红、跨断点段行内分色 + 白竖线），**像素级精确**；标题行改「绿底=缓存命中 红底=重算 │=断点」。
- **断点尾部反推·段对齐（7debb43，用户实测精度修正）**：绝对比例 `cached/prompt×100` 受尾部小段整数格虚高（<1 给 1）+ 估算口径偏差影响，落点漂进尾段内部 → 改 `missTok = prompt − cached` **从尾段往前扣估算 tok**，不够扣取段开头（整段标红）——**段对齐**；物理断点 = 缓存前缀末端 = 重算区前沿，锚定尾部更符合直觉。
- **⑥ 断点竖线消失修复（26e05e2）**：段对齐后 bp **恒恰好落在段边界**（首个红段左缘）→ 画在「跨断点段」行内分支里的白竖线**永不触发**而消失（用户报「那个|的断点位置看不到了」）→ 竖线提为**整列贯穿**（0<bp<100 即画，段区顶到底），落边界/落段内都可见。

**教训（⑥ 提炼）**：数据算法形态变化（段对齐）会绕空依赖旧形态巧合的下游渲染分支——改断点/分布这类底层算法后要**重查下游渲染分支的可达性**，别只验「数据对不对」。全记录见 [ops · proj 链路排障闭环](../guides/ops.md#llm_calls-proj-链路排障闭环三连--tooltip-内联条形-v22026-09-16)。

#### 后记三：断点恢复段内比例——段对齐反推推翻，跨断点段行内劈开（同日五轮，commit 2291122，用户裁定）

**用户裁定**：「还是不对，比如断点在 steps 里，它的背景应该是断点前绿色后面红色吧」——后记二的**段对齐**（7debb43「不够扣取段开头整段标红」）被推翻：断点落段内时该段整段红，丢了「断点前绿后红」的行内劈开语义（当时以估算口径偏差为由简化成整段红，过度设计）。

- **修复**（src/static/stats.html，commit `2291122`）：断点反推恢复**段内比例**——miss 不够扣整段时 `bp -= counts[i] * (missTok/st)`（段内按 miss/段tok 比例定位），不再吸附段边界；断点所在段背景**行内劈开前绿后红**，贯穿竖线画同一 bp 位置（6e05e2 的贯穿式机制不变，只是 bp 不再恒落边界）
- **验证**：playwright 真页面 /stats 拖拽实测——绿块 23 / 红块 1、行内劈开 1 行、贯穿竖线 3 条 ✓。纯前端 Ctrl+F5 生效

**断点反推五轮收敛链**：绝对比例（漂移）→ 段对齐（7debb43，边界吸附 → 竖线死分支）→ 贯穿竖线（26e05e2）→ **段内比例 + 行内劈开（2291122；贯穿竖线随后被后记四撤销）**。全链路见 [ops · proj 链路排障闭环](../guides/ops.md#llm_calls-proj-链路排障闭环三连--tooltip-内联条形-v22026-09-16)。

#### 后记四：贯穿竖线撤销——断点标记只留行内劈开（同日六轮，commit eef5f58，用户裁定）

**用户裁定**：「那个竖线上下贯穿看上去怪怪的，去掉吧」——⑥ 的贯穿竖线在 ⑦（后记三）恢复段内比例后已无必要：bp 不再恒落段边界，行内劈开正常触发、断点信息由劈色承载，整列贯穿反成视觉噪声（上下穿过所有段行）。

- **修复**（src/static/stats.html，commit `eef5f58`）：贯穿竖线绘制块整体删除（含「段对齐反推后断点恒落段边界」的过时注释），断点标记**只留跨断点段行内绿│红劈开 + 一小截行内竖线**（高度=行高）；playwright 拖拽实测贯穿线消失（全页仅剩拖拽吸附扫描线 1 条）✓。纯前端 Ctrl+F5 生效
- **投影分布 tooltip 形态至此定稿**：左列段名+pct / 整行背景色块（断点前绿后红、跨断点段行内劈开）+ 行内竖线；标题行「绿底=缓存命中 红底=重算 │=断点」语义不变
- **排障补充（proj 记录全 0）**：验证时发现进程写出的 proj 记录 tok 全 0——tok 键名修复（`732e2af`）未随上次 /restart 加载（进程仍跑 bug 版）→ tok=0 无段可画。教训：proj 链路前端（Ctrl+F5）与记录侧（/restart）**两个生效通道并存**，改完前端别误判记录侧也已生效。全记录见 [ops · proj 链路排障闭环](../guides/ops.md#llm_calls-proj-链路排障闭环三连--tooltip-内联条形-v22026-09-16)

#### 后记五：t933_s0 断点误读诊断——端点单发缓存丢失（非工具变更）+ bp NaN 防护（同日七轮，commit 待推）

**用户问诊**：「t933_s0 断在了 tools schema 里，是端点问题还是改了工具？」——诊断闭环：**端点单发缓存丢失/冷路由，非工具变更**。三重证据：① t932 尾部 99% → t933_s0（20:13:42）prompt 343,600 / cached 16,704 = **4%** → t935 起 98-99% 持续（工具若变更应持续 miss，排除）；② cache_breakpoint 显示 messages 前缀 637/641 条字节级相同；③ 单发突降 + 自动恢复 = glm-official 已知行为（存力不足随机清缓存 / 冷节点路由）。**tooltip 显示物理正确**：命中 16,704 tok 恰为请求头部 tools schema 段（请求序列化顺序 tools 在前、messages 在后），断点物理位置就在 tools 内部——尾部反推红区从第一段起 ✓（同型佐证：t936_s3 单发 cached 0）。结论：客户端无法避免、下一发即恢复；回退链只对 429 生效，「成功但缓存冷」不触发回退（正常行为）。

**顺带 bp 防护（src/static/stats.html）**：断点初值条件化 `bp = (tt > 0 && counts.length) ? 100 : NaN`——tt=0（旧版 0% 记录）或 counts 空时 NaN、`if(!Number.isNaN(bp))` 跳过反推循环，防 NaN 渗入渲染坐标造成背景错乱（正是「断在 tools schema」一类误读的画面来源）。全记录见 [ops · proj 链路排障闭环 ⑨](../guides/ops.md#llm_calls-proj-链路排障闭环三连--tooltip-内联条形-v22026-09-16)。

#### 后记六：断点精度再两轮——恒变段反推修正被 revert（落 tail 物理合理）+ 跨断点段分位换算修复（t934_s3 实测，同日八/九轮，commits 6069cf4（revert）+ e651ebb）

**八轮（commit `6069cf4`，被 revert）**：用户观察轮内断点「最好落 steps 末尾，有的落 tails 段里」→ 主 Agent 把 tail( / recent_file / hook 识别为恒变段（每步重渲染、物理恒在缓存区外），反推跳过其扣减 + bp 起点=100−恒红段总格数。**用户 revert 并裁定**：「断点落在 tail(思考链) 这个位置是可以理解的」——恒变段物理恒红，轮内断点落其段内=物理正确，反推算法保持原样。

**九轮（commit `e651ebb`，真修复）**：用户以 t934_s3 分段数值钉死渲染层 bug：cached 347.7k / prompt 350k → miss 2.3k，反推应落 tail(思考链) 段内 ~9% 分位（绿区占段头），页面却画在 ~95%（红区只剩一条缝）——跨断点段 `gW = bkW × (bp/100)` 把**全局 bp 坐标**（97.09）当**段内分位**乘了整行宽；修复 `segFrac=(bp/100−segStart)/(segEnd−segStart)` 显式换算，该行绿 9%│红 91% 与反推一致。**反推算法未动**（revert 正确）。教训：坐标语义（全局↔段内）跨层传递必须显式换算。全记录见 [ops · proj 排障闭环 ⑩⑪](../guides/ops.md#llm_calls-proj-链路排障闭环三连--tooltip-内联条形-v22026-09-16)。

## 分组衰减（轮内，2026-08 新）

老方案按步距衰减（distance×15 字符）——每走一步前面所有步 limit 全变，**轮内缓存每步全 miss**。

新方案：每 `GROUP_STEPS=10` 步一组，组号差决定 limit：

| 组号差 | limit |
|--------|-------|
| ≤1（当前组+上一组） | 全量（当前轮 FULL_STEP_CAP_CHARS=32000 上限；老轮用档位 base） |
| ≥2 | `base - 10 × detail_step × 组号差`（≥DETAIL_FLOOR=20） |

组内 10 步字节稳定 → **从"每步全变"变"每 10 步变一次"**，轮内跨步缓存命中大幅提升。

**`detail_step` per-provider 化（2026-08-30，commit 27fea56）**：步距衰减字数不再只有全局 settings 一档——models.json profile 可按 provider 覆盖（`session.detail_step` property：profile > settings > 15；clamp 0~200）。**0=不衰减**：所有组 limit 恒定、老步骤渲染字节永不回缩——DeepSeek 类 miss/hit 价差悬殊（~60x）的 provider 推荐配 0（宁可投影大也不让组边界衰减重截断缓存）；GLM（4x）用默认 15 平衡点即可（⚠️ 2026-09-15 起 settings 全局 15 已删——想要衰减须在模型卡片显式填，见下方「全局 detail_step 字段删除」）。详见 [per-provider 缓存经济学参数](#per-provider-缓存经济学参数fold_target_ratio--detail_step2026-08-30commit-27fea56用户提案)。

### 全局 detail_step 字段删除：只认模型卡片，未填默认 0（2026-09-15，commit 4ad2812，用户裁定）

**诊断收尾（t877_s51 链闭环，2026-09-15）**：[cache_breakpoint 元组解包修复](../features/cache-tools.md) + 原参数重放确认 s51 断点本身是「正常断点」后，用户点破真正根因：「proxy 的步距衰减是全局的 15，s51 发生了**轮内小毕业**」——proxy 未在模型卡片配 `detail_step`，回落全局 settings 15 → 所有 ≥2 组号差的组 limit 持续回缩（`base − 10×15×组号差`）→ 同轮内更早步骤的渲染字节每过一组就变 → **组边界成了轮内缓存断点**。

**用户裁定（2026-09-15，commit `4ad2812`）**：「把全局步距衰减那个字段去掉，模型卡片的步距衰减不填就默认是 0，填了就按照填了的为准」。

**新语义（`session.detail_step` property，src/session.py）**：

| 优先级 | 旧（2026-08-30 ~ 09-15） | 新（2026-09-15 起） |
|---|---|---|
| 1 | profile.detail_step（模型卡片） | profile.detail_step（**填了按填的**） |
| 2 | settings.detail_step（全局） | **已删除** |
| 兜底 | 15 | **0（不衰减）** |

- **0=不衰减**：所有组 limit 恒定、老步骤渲染字节永不回缩 → 前缀缓存打满——现在成为**默认**（此前只有显式配 0 才享受）；**想要衰减的 provider 必须在模型卡片显式填**——上节 27fea56 段「profile > settings > 15」的链路被本裁定取代，「GLM 用默认 15 平衡点」不再存在
- settings.json 里残留的旧 `detail_step: 15` 键无任何消费方，留着无害（想清爽可手动删）

**落地五文件（commit 4ad2812）**：

| 文件 | 改动 |
|---|---|
| src/session.py | `detail_step` property：profile > 0，不再回落 config |
| src/config.py | `load_detail_step()` 删除（settings 通道不复存在） |
| src/commands.py | `/config detail_step` 收到**不再落盘**、提示去向「改在模型卡片配置」；read_config 不再显示全局字段；`detail_base` 保留不动（显示实际生效值：显式 > 窗口推导 > 1500） |
| src/llm_client.py | per-provider 注释同步（None=不衰减=0） |
| src/static/index.html | 设置页步距衰减输入框/回显/保存三处删除；模型卡片 placeholder 改「空=0」 |

**验证**：残留引用扫描 0 + py_compile×4 + JS 语法 + 行为单测（未填→0、填 15→15）全过。改的是引擎层，`/restart` 生效。

**对 t877_s51 类断点的效果**：proxy 卡片未填 → 衰减恒 0 → 轮内组边界**永不回缩** → 轮内小毕业的触发源消失（此前 s50→s51「断点在当前轮第 0 步、前缀 559K 全保」的场景变为完全命中）。

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

**补遗（2026-09-13，kv_cache_claim 竞态修复轮顺带抓到）**：`replace_lines` 补进白名单（7→8）——它是按行替换的**写操作**却不在集合内，rf 快照对它改的文件显示**旧版**（调试轮 edge 区可见滞后快照）。写工具全家至此在列；机制零改动（映射/命中/剥离原样）。

### 修复六：超大文件跳过——RF_MAX_CHARS=100K 上限（2026-08-31，commit 2c71e9a，用户裁定）

**背景（用户裁定 2026-08-31：「recent-file 确实有可能会很大，容易让近期缓存上蹿下跳的……文件尺寸超过 100K 的跳过」）**：rf 的设计初衷是省 read（跟屁虫全文），但**超大文件全文注入 = 单步前缀稀释**——巨型文件一次进投影就是当步全 miss：index.html 130K 实测把命中率从 99% 打到 81%（当步 +46K tok 全 miss）。

**实现（src/session.py 三处，commit 2c71e9a）**：

1. 常量 `RF_MAX_CHARS = 100_000`——单文件上限，一处可调
2. `_rf_latest_map`：构建映射时 text 超限的条目 **text 置空 + skip 标记（记字符数）**——cid 仍进映射、仍会命中（模型看得到提示），只是不携带全文
3. 挂载点渲染：skip 条目挂**一行自闭合提示**而非全文——形如 `<recent-file file='index.html' skipped='too-large …——已修改，需要时 read_file 查看当前内容'/>`

**自闭合形式的口径红利（四层口径零改动自动一致）**：

| 层 | 为什么不受影响 |
|---|---|
| 诊断 `_rf_in_msgs` | 提示行不匹配 `_RE_RF_BLOCK`（配对标签正则）——天然不计 |
| 估算剥离 `_rf_stripped` | 同上——天然不剥 |
| 判阈刨除 `_rf_chars` | 按映射 text 计（已置空）→ 计 0 |
| 附加挂载 | skip 分支单独渲染提示行，小文件全文路径不变 |

**验证四项全过**：120K 大文件 → skip 标记 + 提示行（全文不出现）；5K 小文件 → 照常全文挂载；`_rf_chars` 只计小文件（修复二判阈刨除口径一致）；提示行不误伤剥离与统计。

**语义**：rf 的「省 read」承诺对超大文件降级为「知道改过 + 需要时自己读」——与修复五（读工具不配 rf）同族，都是「全文跟屁虫不划算时收缩」。效果对照：同样 edit index.html（130K），之前是全文注入稀释缓存（99%→81%），之后只有 ~150 字符提示行，命中率不受影响；模型仍知道文件改过，edit 的 old/new diff 本就在结果里。/restart 生效；攒批待发（拟 0.22.5）。

**首次实战（2026-08-31，合入约一小时后）**：同 session 末尾 edit `src/static/index.html`（136K 字符）——正是本修复针对的头号嫌疑文件，rf 全文注入被替换为一行 `skipped='too-large'` 提示，缓存不再被打断。新机制在自己的开发流程里当场生效、改它的那一轮就吃到收益——「省 read 降级为知道改过」的实证闭环。

### 修复七：超大文件结构大纲投影——从「跳过」到「投影结构」（2026-08-31，commit c888876，用户提案迭代）

**用户提案（对修复六的迭代）**：「对 100k 以上的文件不是跳过，而是投影出文件的一个大致结构……主要是各函数的行号结构、markdown 文件的大纲」——用户记得代码里有相关处理逻辑：正是 `real_tools._md_outline`（与 dir_outline / _md_snapshot 同源的 md 标题大纲提取）。

**实现三处（src/session.py，commit c888876）**：

1. **`_rf_outline(path, text)`（新 staticmethod）**：超大文件的结构摘要——`md` → 复用 `real_tools._md_outline`（标题大纲，截 4000 字符）；`py` → `ast.parse` 提取类/函数行号结构（`class X [L1-L2]` / `def y [L3-L4]`，纯标准库、无 LSP 依赖）；其它 → 行数 + 头部 5 行缩略。统一限 60 行（防超大类清单本身膨胀）。
2. **`_rf_latest_map`**：超大条目（>RF_MAX_CHARS）在 text 置空 + skip 标记（修复六）之外新增 `outline` 字段——构建映射时顺手生成，不额外读盘。
3. **挂载点**：skip 条目有 outline → 挂**配对标签** `<recent-file file version note='文件过大（N 字符）——投影结构大纲，全文可 read_file'>` + 大纲内容 + `</recent-file>`；无 outline（提取失败等）回退修复六的一行自闭合提示。

**口径变化（与修复六「自闭合红利」对照）**：修复六的自闭合提示行不匹配 `_RE_RF_BLOCK`（天然不计不剥）；修复七的配对标签**有内容、匹配配对正则——rf 统计（`_rf_in_msgs`）与剥除（`_rf_stripped`）口径正常计入**；判阈刨除 `_rf_chars` 仍按映射 text（已置空 → 计 0）。大纲体积上限（60 行 / 4000 字符）保证其对估算与缓存的影响可控。

**验证（三种真实形态）**：py（120K 假文件）→ 类/函数行号结构 ✓；md（100K）→ 标题大纲（frontmatter / 多级标题 / 行号范围）✓；其它（100K）→ 行数 + 头部 5 行缩略 ✓。

**语义升级**：rf 对超大文件从「知道文件改了但看不到内容」（修复六）升级为「**改了 + 结构全貌**」——函数在哪、行号范围直接可见，需要细节时 read_file 精准段读取。效果实证：本 repo `src/session.py`（147K）——修复六形态是一行 `skipped='too-large'`，本修复后投影完整类/函数行号结构（几十行）。/restart 生效。

### 修复八（第四版·段式化）：快照独立装配段 _seg_msgs_recent_file（2026-09-07，用户提案）

**用户提案（2026-09-07）**：「把 recent-file 改一下吧，不附加在那次工具调用的结果位置了，而是作为一个段走装配逻辑」——结构：

```xml
<recent-file>
<file path="xxx.py" version="a1b2">          ← 小文件：行号化全文（与 read_file 同款宽度自适应）
 1| import os
 2| import abc from xxy.py
</file>
<file path="dd.md" version="c3d4" size="130537">   ← 大文件（>RF_MAX_CHARS=100K）
<overview>
（py=类/函数行号结构 / md=标题大纲——修复七的 outline 语义延续）
</overview>
<content note="文件过大（130,537 字符 > 100,000）——此处省略，需要时 read_file 分段读取"/>
</file>
</recent-file>
```

**实现（src/session.py）**：

- `_DEFAULT_ASSEMBLY_PLAN` 新增 `{"kind": "seg", "name": "recent_file"}`——插在 `steps` 后、`tail` 前（可配位置/开关：声明清单不列它就不投影，标准声明式语义）
- 新段 `_seg_msgs_recent_file()`：构建 `<recent-file>` 包裹的 XML 块——小文件走行号化全文（read_file 同款）；大文件（>RF_MAX_CHARS）`<overview>` 结构大纲（py=ast 类/函数行号、md=标题大纲，修复七的 `_rf_outline` 延续）+ `<content note=…/>` 省略提示；同文件多次 edit 只带最新一份
- `_steps_to_messages` **移除内嵌**：`rf_map` 参数、`_rf_hit` 命中索引、content 尾部追加逻辑整体删除——tool result 不再携带快照（`_seg_msgs_steps` docstring 同步更新）
- `_walk_plan` walk 清单 `recent_file` 段 → 与 `tail` 同桶 merge（区3 收集桶）→ `<system-reminder>` 统一包裹并入末条 content 末尾（末条 assistant / 空 → 回退独立 user 消息兜底不变）
- **估算口径不变**：rest 的 `ltm + user_message + steps + tail` 不含 recent_file——rf 仍是轮内易变项（归档即消失），不该推动升档/折叠等不可逆历史压缩（用户裁定 2026-08-29 延续）；`_rf_stripped` / `_RE_RF_BLOCK` / `_rf_in_msgs` 保留作旧内嵌形态兜底（剥离/诊断口径）
- **清单同步**：播种 `src/assets/main.yml` + 全局 `~/.agt/main.yml`（`steps=reasoning` 后）都加段；**显式声明了 assembly 的子 Agent 不列它就没有**（如 VideoGameTeam 成员要的话需自己加）
- **测试**：新增 `test/test_recent_file_segment.py`（13 断言全过：结构包裹 / `path`+`version` 属性 / 行号宽度自适应 / 大文件 size+overview+content note / 体积可控（<全文一半）/ tool result 不再含 `<recent-file>` / walk_plan 集成并入末条 reminder 桶 / projection_breakdown 单列 / 空映射零噪声 / 同文件多改仅最新）

**缓存收益（为什么比第三版更稳）**：第三版快照挂在中段 tool result content 尾部——快照每步变化会让该 tool result 位置之后的全部消息重算；段式化后快照并入**末条**（本来就在缓存未命中区）——每步快照变化零前缀扰动。`/context` 段落统计出现 `recent_file(改文件快照段)` 单列、投影转储（旁车）里 XML 块直接可见。需 `/restart` 生效。

#### 注入姿势确认：固定 reminder 桶，不受 steps=reasoning 影响（2026-09-07 交互确认）

用户问：「recent-file 放在 steps 后面是和配置的其它尾部段一起被 system-reminder 包裹附加在最后一条 message 的 content 上的对吧？」——**对，且固定**：

- **同桶 merge 到末条**：`_walk_plan` 里 `recent_file` 段与 `tail` 同走 `tail_merge_text` 桶（源码注释「与 tail 同桶——`<recent-file>` 块并入 tail_merge_text（装配后统一 `<system-reminder>` 包裹并入末条 content）」）——**不额外创建 message**，正是三区 merge 语义（末条本来就在缓存未命中区，快照每步变化零前缀扰动）
- **不因 `steps=reasoning` 改变姿势**：`steps=reasoning` 只影响 steps 后【动作项】（text/file/func 等按各自 pose 分桶，见 [粒度演进](#粒度演进steps-全局--逐动作项-pose-双桶2026-09-03commit-24597f3用户提案)）；`recent_file` 固定走 reminder 桶——文件快照是大体积内容，塞进 `reasoning_content` 思考链不合适
- 清单顺序：`steps` 后、`tail` 前（`_DEFAULT_ASSEMBLY_PLAN` 插位）——reminder 内部靠前

#### 后记：_ASSEMBLY_SEGS 漏加 recent_file——DSL 声明段被静默丢弃（2026-09-10，commit 1c4aaa7）

段式化新增 `recent_file` 段时，只补了**顶层判据**（`_DEFAULT_ASSEMBLY_PLAN` 默认装配序 / `_seg_msgs_recent_file` 构建函数 / 前端 SEG_TYPES 枚举 [agents-admin](agents-admin.md#seg_types-补-recent_file-段枚举段名不再走文本框兜底2026-09-07commit-67c7f57)），**`src/multiagent.py` 的合法段名校验集合 `_ASSEMBLY_SEGS` 漏加**——DSL 解析 `_asm_item_from_str` 把 `recent_file` 当未知段，打 `assembly 含未知段名 'recent_file'` 告警并返回 None **静默丢弃**（显式声明 assembly 的子 Agent 即使清单里写了该段也不投影，只见告警不见段）。与 v0.26.1 修的前端 SEG_TYPES 漏加**同构**（同一新段的两半白名单），v0.26.1 只补了前端那一半。

**发现路径（桌面版端到端验证偶得）**：`logs/desktop.log` 里一行被掩盖的警告——assembly 解析发生在启动装配时，主 Agent 启动日志把这行残坑暴露出来。修复（commit 1c4aaa7）：`_ASSEMBLY_SEGS` 补 `"recent_file"` 一项；单测验证 dict/str 两路径解析全过、无告警。

**教训**：新增装配段时应**三处同查**——前端枚举（agents.html `SEG_TYPES`）+ 后端校验集（`_ASSEMBLY_SEGS`）+ 默认装配序（`_DEFAULT_ASSEMBLY_PLAN`）；前端补白名单时须排查后端同名白名单（各管一端的合法段集合，漏一处 = 静默丢弃，连报错都没有）。

#### 后记二：快照层行号化污染——rf 双前缀 + outline 恒失败（2026-09-13，随施工模式实测抓到）

段式化后 rf 渲染全走 `_seg_msgs_recent_file`（小文件行号化全文），但快照收集层（src/agent.py `_collect_file_snapshots`）存进 `file_snapshots` 的非 md 文件**已是行号化文本**——渲染层再行号化 → `1| 1│` 双前缀；大文件 outline 拿行号化文本 `ast.parse` → 恒 IndentationError（`session.py` 结构提取一直失败的根因，t789 投影实证）。修复（随施工模式收官 `f375483`）：**快照存原文、行号化归展示层**——数据层存事实（原文+版本），渲染姿势归投影层。详见 [施工模式 · 顺带修复](#顺带修复同轮实测抓到)。

## 折叠摘要 tail 优先级（recap → answer 代码摘要 → 中断标注，2026-08）

`_folded_summary(fold_count)` 生成被折叠早期轮次的结构概览（纯结构信息、无需 LLM；逐字原文靠 recall 召回）。每轮一行：`user[:80]` + `(已折叠N次工具调用) ` + tail。tail 的优先级链：

1. **recap**（`turn_end` 异步生成的一句话总结）——语义密度最高，是「这轮做了什么」而非「回答首行是什么」
2. answer 代码摘要（首行 + 标题）——常退化为「完成并推送 ✅」类横幅文案，信息量低
3. 中断标注（未回答）

recap 作为 tail 的落地：`set_turn_recap(idx, recap)` 写 `Turn.recap` + `recaps.jsonl` sidecar 持久化（recap 是事后异步产物，**不进事件流**，events 重放不含它，load 侧 `_load_recaps` 按 idx 恢复）；两条生成路径的 turn_idx 捕获时机与 rewind 裁剪见 [multi-agent · recap](multi-agent.md#recap每轮一句话总结)。注意它与 `Turn.summary`（finish 时生成、贴在该轮最后的一句话摘要）是**两个不同字段**——用户提案「recap 填到触发那轮的 summary」实现为写 `Turn.recap`、供折叠摘要行消费。`/restart` 后生效——每轮 recap 落 recaps.jsonl，下次折叠触发即见 recap 版轮次概览。

## 超深档折叠标注：0 次工具调用省略标注行（2026-08，commit feeb123）

**背景**：fc 折叠后，超深档历史的每轮折叠行格式为 `---- 已折叠共N次工具调用 ----\n\n{answer}`。**纯讨论轮**（架构评估类，一字工具没调）N=0 也照加标注行——用户实测指出两处观感问题：①「已折叠共0次」不传递任何信息，纯噪声；② 近 2 轮 answer 顶部标注次数与「实际工具调用次数」印象对不上，疑似统计错乱。dump 数据澄清②：那些轮**确实是 0 工具调用的真实讨论轮**（remote_tools 评估、server_id 评估），标注次数与 events 完全一致——数据没错，问题只在①的展示冗余。

**修复（session.py，commit feeb123）**：折叠渲染处 `n_calls = sum(len(s.tool_calls) for s in turn.steps)` 判空——`n_calls > 0` 才加标注行；`n_calls == 0` 时 content 直接是 answer 原文（当时 `answer_reasoning` 照常附）。效果：折叠历史里 0 工具轮显示为纯净 answer，与「纯讨论轮」语义一致；非 0 轮标注照旧。（⚠️ 2026-09-16 起 **fold 档 reasoning 一律不再附**——用户裁定 answer 原文信息量已足，正常档位不受影响；见 [三项调整](#三项调整卫生毕业-15-轮--llm_calls-附投影分布--超深档不投影-reasoning2026-09-16用户提案commit-17af0a8)）

**注意**：该标注是**折叠历史渲染**的产物（见 [历史补记 · fc 大刀首折](#历史补记--fc-大刀首折至少吞超深档一半2026-08commit-4d37e90)），与 `_folded_summary` 的轮次概览行（recap tail，见上节）是两套格式——前者保 answer 原文、后者保结构摘要。

## 前缀缓存三层优化（详见 blog/03）

1. **布局层**：易变块（时间/计划/召回/后台）统一收尾成 tail ambient，前缀区纯稳定
2. **轮间层**：分档冻结渲染（见上）+ **轮边界统一重排**（升档+折叠统一计划，见 [轮边界统一计划](#升档graduate-与折叠轮边界统一计划2026-08commit-1e9af8f)）；折叠为预期一次性 miss，见 [t206 实证](#折叠事件与缓存命中t206-实证2026-08)；正常轮边界平滑路径见 [t224 实证](#正常轮边界路径t224-实证2026-08)；超长轮保命阀例外见 [t228 实证](#轮内应急折叠保命阀t228-实证2026-08)。⚠️ 判阈依赖 `_estimate_tokens` 估算，口径已闭环（含 tools schema，见 [估算与校准口径闭环](#估算与校准口径闭环tools-schema-补齐2026-08)）
3. **轮内层**：分组衰减 + `_build` 以 `_planned_fold`/`_planned_graduates` 为起点**零调整**（顶满窗口时保命阀应急折叠例外，见 [轮边界统一计划](#升档graduate-与折叠轮边界统一计划2026-08commit-1e9af8f) / [t228 实证](#轮内应急折叠保命阀t228-实证2026-08)）

## system 段 append-not-replace（2026-09-12）

缓存连续时把 system 新版本 append 进当前轮（历史前缀完整命中）、毕业/折叠/tools 变化等断点处归一化清账（append-not-replace，spec s_eb14a8fd）——专题块见下方。

## 施工模式投影（2026-09-13）

存在未完成活动 plan 时投影切换施工视图：history 停装 + 头部施工牌（design 全文，byte-stable）——专题块见下方；2026-09-13 收官（commit `f375483`，16+3 场景测试全绿 + 首个实战样本）。

# —— 施工模式投影（2026-09-13，spec s_e1804804，用户提案） ——

## 语义

存在**未全部完成的活动 plan** 时，主 Agent 投影切换施工视图：
- history 段整个不装配（段统计标"跳过(施工模式)"）——施工背景以施工牌为准，recall 兜底
- 头部第二条 system = 施工牌（plan design 全文 + 提示语）
- 全部步骤 completed / exit_plan → 判定自然为否 → 恢复正常投影（动态判定，无状态同步）
- 判定绑定 session 同一性（`_RUNTIME_AGENT.session is self`）——主 Agent 施工不波及子 Agent

## 缓存经济

- design（施工期恒定）进头部 → system 快照之后前缀 byte-stable，每步只增量计 steps/tail
- plan_steps（每步变）留在区3 reminder 桶（未命中区零扰动）
- 进入/退出各 miss 一次（形态切换），换取施工期省掉整个历史段——大 session 显著划算
- 施工牌是第二条 system，不进 append-not-replace 账本口径（只认 msgs[0]）

## 防双份

施工模式下 `plan_content()` 返回空（design 已前移施工牌）；`plan_steps()` 原位输出。
`_seg_msgs_recent_file()` 同样返回空（快照回内嵌进 tool result，见 [施工期 recent-file：回内嵌形态](#施工期-recent-file回内嵌形态2026-09-13用户裁定)）。
非施工模式两者行为不变（全完成 → plan_content 一行 / plan_steps 空 / rf 独立段照旧）。

## 可观测

/context 顶部：`⚙️ **施工模式**（plan 未完成：history 未装配·施工牌 N 字…）`；
段清单 history 行 = 跳过(施工模式)。

## 顺带修复（同轮实测抓到）

**发现（2026-09-13，施工模式验证轮 t789 投影实测抓到）**：rf 段小文件显示 `1| 1│` **双行号前缀**、`session.py` 恒显示 `(结构提取失败: IndentationError)`——同源根因：快照收集（src/agent.py `_collect_file_snapshots`）给非 md 文件存的是**行号化文本**（`_number_lines`），rf 段渲染层（修复八）渲染时再行号化一次 → 双前缀；大文件 outline 拿行号化文本 `ast.parse` 必炸（行号前缀破坏缩进）→ 恒失败——`session.py` 的结构提取一直空转的根因。

**修复（src/agent.py `_collect_file_snapshots`）**：快照存**原文**（`text = _md_snapshot(raw) if md else raw`），行号化归展示层——rf 段渲染时自行做；md 保留 `_md_snapshot`（摘要态，非行号化源码）。分工原则：快照层只管「原文 + 版本号」这份事实，渲染姿势是投影层的事。

**验证**：修复后子进程实测 outline 正常输出 `def _repo_key() [L72-L77]` 式类/函数结构；双前缀消失。

## 施工收官：16+3 场景测试全绿 + 首个实战样本（2026-09-13，commit f375483）

spec s_e1804804 四步全部完成（16+3 场景测试全绿），commit `f375483` 推送。

**变更落点（4 文件）**：

| 文件 | 变更 |
|---|---|
| src/session.py | `_construction_mode()` 判定（`_RUNTIME_AGENT` 与 `plan_content()` 同源，绑定 session 同一性）；history 段装配跳过（段统计 meta=`跳过(施工模式——plan 未完成，历史不装配；recall 可查)`）；施工牌装配（`_append_answer_style` 之后插第二条 system = design 全文，不进 `_apply_system_ledger` 快照口径） |
| src/agent_config.py | `_func_plan_content()` 施工模式返回空（防双份，见 [防双份](#防双份)） |
| src/commands.py | `/context` 顶部施工模式标注（施工牌字数从段统计的 `施工牌` 行提取） |
| src/agent.py | 顺带修复：rf 快照存原文（见 [顺带修复](#顺带修复同轮实测抓到)） |

**首个实战样本（2026-09-13，交付验证轮自身投影）**：主 Agent 下一轮投影注入即见【施工牌·进行中计划】`p_c1848eb7` 出现在头部、`plan_content` 区3 块同步消失（防双份实证闭环）——新机制在自己的开发流程里当场生效。需 `/restart` 生效。

## 施工期 recent-file：回内嵌形态（2026-09-13，用户裁定）

**用户裁定（2026-09-13·施工中两次插话）**：「施工期的 recent-file 还是和之前一样跟在具体的工具调用后面吧，施工期不用去重，不用限制文件数量」+「工具调用后面贴的应该是**当时的文件快照**」——施工语义要的是**因果上下文**：每次操作时文件长什么样（施工在文件上连续推进，看当时的版本才知道每步改了什么），而不是第四版段式的"最新版集中投影"。

## 行为（施工模式特例）

- `_steps_to_messages`：施工模式按 call_id 命中 `step.file_snapshots`，把该次调用【当时】的快照以 `\n<recent-file file version>` 块附加在该次 tool result content 尾部——**不去重**（同文件多次编辑各挂各的当时版本）、**不限数量**（每个写调用都挂）；块构造 `_rf_inline_block`（小文件行号化全文 / 超大文件 >RF_MAX_CHARS 走 outline + 省略提示，与段式同口径）
- `_seg_msgs_recent_file`：施工模式返回空——**防双份**（内嵌与段式互斥；与 `plan_content()` 的防双份同构）
- 非施工模式行为不变（第四版段式照旧：同文件仅最新一份、独立段走装配）

## 口径一致性

- 命中集合 `_rf_hit_cids()`（剥离 `_rf_stripped` / 诊断 `_rf_in_msgs` 判定源，与附加口径同源）：非施工 = `_rf_latest_map` 的 cid（最新版集中）；施工 = 当前轮**全部写调用** cid——附加全挂 → 剥离全剥，估算免疫不漏
- 内嵌块以 `\n<recent-file` 开头 → `_RE_RF_BLOCK` 配对正则天然命中
- 归档轮 `file_snapshots` 不持久化（空 dict）+ 非施工不内嵌——双保险，第三版"归档轮注入"老问题不会复发

## 缓存代价权衡

内嵌位置在 steps 区 tool result 尾部——每步新快照使该位置之后的消息重算（正是第四版段式化要消除的扰动）。施工期接受该代价换因果保真：施工期 history 本就不装配（前缀只有 system + 施工牌，byte-stable），扰动面 = 当前轮 steps（本来就在未命中区）——实际缓存损失趋零；施工结束自动回段式（第四版恢复）。

## 验证

`test/test_construction_rf_inline.py` 14 断言全绿（判定 / 防双份 / 内嵌命中 / 不去重各挂当时版本 / 不限数量 / 剥离全量 / 非施工回归）+ `test_recent_file_segment.py` 13 断言回归全绿。需 `/restart` 生效。

## 上一轮施工摘要：recap 数据跨轮接力（2026-09-16，commit bb3a4d4，用户裁定）

**需求**：跨轮施工时，Agent 开局不知道「上一轮干了什么」——施工牌只给 plan 状态（进行中计划/design/步骤），上一轮的产出只能盲目重读文件（安全可从事件流反推，但费轮）。用户裁定：施工牌后追加「上一轮施工摘要」独立小段，实现跨轮接力。

**装配形态（src/session.py 施工投影分支，紧随既有施工牌注入之后）**：

```
[system] 人设+环境（恒定）
[system] 【施工牌·进行中计划】p_xxx · 标题 + design 全文   ← 恒定不动（byte-stable 缓存吃满）
[system] 【上一轮施工摘要】完成了脚本工具外置与验证          ← 新增：每轮只换这一条（≤60 字）
         （详细过程已归档，需要用 recall 召回。）
[user]   继续施工第 2 步
[steps]  当前轮 append-only（_constr_buf）...
```

- **数据源 = `Turn.recap`**：turn_end 异步生成的一句话总结（≤60 字，`recaps.jsonl` 持久化——重启恢复也在），**零额外推理成本**（复用已有 recap 产物）；取 `self.turns[-1].recap`，无上文 / 空串则整条跳过（不占位）
- **注入位置 `msgs.insert(2, ...)`**：紧随施工牌（msgs[1]）之后、user 之前——独立的第三条 system 消息
- **缓存经济**：牌保持恒定不动（byte-stable 缓存吃满）；**缓存断点 = 摘要消息开头**——其后 user/steps 本就是新内容，每轮实际牺牲的只有这条 ≤60 字小段，其余前缀全命中
- **口径**：sections 段名「施工摘要(prev recap)」——/context 分段统计与 /stats 投影分布可见；履带式只留最新一条（每轮覆盖写）

**验证（mock Session 两版校准）**：首版验证脚本未正确注入 `_RUNTIME_AGENT`/未走 start_turn，摘要未落在 msgs[2]（测试脚本问题，非代码缺陷）；二版 mock 全链路：`施工牌: ✓ | 摘要: ✓ | sections: ['system(人设+环境)', '施工牌(plan design)', '施工摘要(prev recap)', 'history段', '当前轮user(第2轮)']`。commit `bb3a4d4` 已推送，`/restart` 后生效。

## 跨轮施工流：从施工开始的轮全部保留（2026-09-17，用户裁定）

**用户裁定（2026-09-17）**：「我觉得还是从施工开始后的轮都保留吧，虽然上下文膨胀的比较快，不过起始总上下文低且持续维持缓存连续和思维链连续，能够更快的完成任务」——施工期投影从「仅当前轮」升级为**跨轮连续聊天记录**：施工中每轮归档时把本轮 `[user + steps 定型]` 追加进 `_constr_stream`（append-only），从施工激活轮起**全部保留**直到 plan 收工。

**形态（装配后）**：

```
[tools schema]
[system 人设]                ← 恒定（缓存吃满）
[system 施工牌 design]        ← 恒定
── 施工历史流（append-only，跨轮累积）──
[user] 开始施工第一步          ← 轮1（施工激活轮）
[assistant/tool] ...          ← 轮1 的 steps 定型
[user] 继续第二步              ← 轮2
[assistant/tool] ...          ← 轮2 的 steps 定型
── 当前轮 ──
[user] 继续第三步              ← 本轮 user（施工历史流在 user_message 段位置整体输出，其后接当前轮 user）
[assistant/tool] ...          ← 本轮 append-only（_constr_buf）
```

**实现（src/session.py）**：

- `self._constr_stream: list[dict]`——跨轮施工流（turn 级 append-only）
- `_constr_migrate_turn(turn)`：每轮归档统一入流钩子——`finish_turn` 与 `abort_current_turn`（中断同口径）都调用。plan 仍在施工中 → 把 `[本轮 user + 各 step 定型 msgs]`（与 `_constr_buf` 同定型器，字节形态一致）追加进流；plan 全 completed（收工）/ 无 plan → 清空流（恢复常规历史装配，**形态跳变一次**）
- 投影：user_message 段分支施工中先 `_constr_rebuild()`（惰性重建，兼顾重启）再 extend 历史流，steps 段照常输出当前轮 buf——序列 = 完整施工聊天记录
- **重启持久化**：`extra_state["constr_start_idx"]`（起始轮号）——load 后 `_constr_stream` 为空（运行时内存不持久化），施工中再投影时按 `turns[start_idx:]` 惰性重建（重定型）
- `_constr_buf` 类型注解简化：从「每 step 一组定型 msgs（与 _current.steps 对齐）」改为纯缓冲「施工投影缓冲：新轮清空（turn 级 append-only）」

**验证（模拟多轮施工）**：轮2 投影 user = 恰当前轮 1 次 ✓；轮3 = 三轮齐、无重复 ✓；重启 load 后投影含全部历史施工轮 ✓；plan 全完成 → 流清空、恢复常规装配 ✓。

**权衡与可见性**：施工越久上下文越大——但如用户判断：施工起点低（history 不装配）+ append-only 缓存连续 + 思维链连续，换的是施工效率。`/context` 与 /stats 投影分布显示「施工历史流(N轮起·append-only)」段。`/restart` 后生效。

# —— system 段 append-not-replace：缓存连续时追加、毕业断点处归一化（2026-09-12，spec s_eb14a8fd，用户提案+裁定） ——

## 动机

SYSTEM 段每次投影全量渲染（replace 语义）：人设/钩子清单/团队看板变化 → 从序列头断缓存 →
长会话全量重算。DSH（deepseek-harness）的 SystemPromptProjection 给出教科书解法，本页
策略是其在 Agt 架构的精确映射（用户裁定：**归一化挂在毕业时**——毕业/折叠历史全量重排、
缓存必断，此时收敛堆积版本零成本 = DSH「断点清账」）。

## 实测背书（2026-09-12，api.deepseek.com · deepseek-flash，固定前缀 ≈4055 tok）

| 请求 | hit | 命中率 | 结论 |
|---|---|---|---|
| A2 基线重发 | 3840 | 94.7% | 通道健康 |
| C1 尾部 append user(同文本) | 3840 | 94.3% | append 本身不断 |
| **B1 尾部 append system(v2)** | **3840** | **94.3%** | **前缀完整命中，仅 miss 新增 233 tok** |
| B2 B1 同 payload 重发 | 3840 | 94.3% | 含中部 system 的 payload 稳定可缓存 |
| D1 头部 replace system(v2) | 0 | 0% | 全断（对照组：实验对 system 变化灵敏） |

与既有记录的关系：574 轮「v4 对**变化的** system 规范化」是同位置改内容触发；472 轮
「中插 system hit=0」是前缀断在插入点（越靠前 miss 越大）——两者都不是"尾部追加"，
本实验把**位置**与**角色**两个变量拆开做了对照。

## 账本（session._system_ledger，meta.json 顶层键持久化——绕开 extra_state 全量替换）

- `last_text`：头部快照字节（归一化时刷新；存**含回答风格提示的最终文本**——比较点在
  `_append_answer_style` 拼接之后，hint 不随 append 重复叠加）
- `appends`：**按轮锚定的追加版本列表** `[{turn, text}]`（2026-09-12 · 二形态 A 修正起，取代旧
  单值 `pending_text`——append 时记录。**没有它会死循环**：下次同文本渲染再次视为"变化"
  重复 append，首版实现实测踩到）。多版本共存于历史各自原位（≤4 条，DSH system/message
  surface 节点同款）；同轮内版本再变原地替换末条，不堆积
- `count`：堆积数（>4 防御性归一化）；`dirty`：断点标记

## 决策表（`_apply_system_ledger`，装配后处理）

| 条件 | 动作 | 缓存效果 |
|---|---|---|
| cur == 上次投影输出形态的版本 | 重放/原样（byte-stable） | 全命中 |
| 变化 && llm.in_history_system && !dirty && 堆积<4 | 头部=last_text 快照 + append 新版本**按轮锚定**（形态 A）：账本记 `{"turn": N, "text"}`——轮 N 进行中插当前轮 user 前；轮 N 归档后由 `_render_tiered_history` 固定插在**轮 N 渲染块之前** | 历史前缀全命中 |
| dirty（毕业/折叠执行 / tools hash 变 / 堆积超限）或 !in_history_system | 归一化单条=cur，last_text 刷新、清账；**顺带摘除历史渲染已插入的本批废弃 append** | 断点处免费清账 |
| cur 回归 last_text 且有堆积 append | 撤回 append（尾部少一条，前缀到历史段稳定） | 小断 |

插入锚点（形态 A·按轮锚定，2026-09-12 · 二修正，commit be55762）：append 在轮 N 发生 →
锚定轮 N，轮归档后位置永不漂移、多版本历史原位共存——首版「浮动插入（最后一条 user 之前）」
每开新轮漂一格、公共前缀每轮断一次，用户两图对照抓出后废弃（见[下节](#形态修正浮动插入--按轮锚定形态-a2026-09-12--二commit-be55762用户两图对照裁定)）。

## 形态修正：浮动插入 → 按轮锚定（形态 A，2026-09-12 · 二，commit be55762，用户两图对照裁定）

用户拿两张投影图对照发问「和 DSH 是一样的逻辑吗？」——图 A = DSH 形态（v2 固化在 turn3
之后、v3 出现时新添一条在 turn5 之后，多版本共存），图 B = 全量收拢形态。对照暴露首版实现
的真实缺陷：

**首版 = 形态 B 变体（浮动插入），有跨轮漂移**：append 的 system 恒插在「当前轮 user 之前」
——轮内步进稳定，但**每开一个新轮它跟着新 user 后移一格**（sys_v2 从 t2 后漂到 t3 后……），
公共前缀每轮断一次。append 的前缀收益只吃一轮，第二轮就把断点转移到历史中部，比 replace
还不如（replace 至少断在头部、历史重算一次后稳定）。

**修正 = 形态 A（按轮锚定，与 DSH 同构：append 即固化）**：

- 账本 `appends` 记 `{"turn": N, "text": ...}`——**append 发生轮即锚点**
- 轮 N 进行中：插当前轮 user 前（不变）
- 轮 N 归档后：`_render_tiered_history` 建 `turn → text` 插入表，固定插在**轮 N 渲染块之前**
  （`_turn_block` 前置，轮号 1-based）——位置永不漂移，前缀跨轮稳定
- 多版本历史原位共存（v2@t2、v3@t5，正是图 A）；同轮内版本再变（s0=v3、s3=v3b）**原地替换
  末条**，不堆积
- 归一化清账时**同步摘除历史渲染已插入的废弃条目**——时序坑：渲染先于清账（`_render_tiered_history`
  先读账本插入了），不清则废弃版本残留一条到下次投影才消失

验证 7 项全绿（v2 锚定 t2 块前 / v2·v3 历史共存 / t3→t4 前缀含 v2 / 重启后 v3 仍在原锚位 /
同轮原地替换 / 撤回回归 / 归一化清账+残留摘除）。`/restart` 生效。

## 三断点置 dirty（mark_system_dirty）

1. `_plan_fold` 计划真实变化处（升档/折叠执行——轮边界/回溯/轮内应急三路径共用此点）
2. tools schema hash 变化（`Agent._sync_tools_schema_hash`：schema 在请求级、是 provider
   前缀的一部分，变化必断——首次调用只建基线不算断点）
3. 堆积 >4 条防御

## 归一化点顺带刷新长期记忆投影快照（_ltm_refresh_epoch，2026-09-17，commit 12c7594，用户提案）

**背景**：长期记忆静态层（semantic 常驻 + procedural 标题，`_ltm_static_block`）此前每轮全量重渲染——轮中途 add_memory → ltm 段文本变化 → 从序列头断缓存全序列重算。用户提案（2026-09-17，commit `12c7594`）：「add_memory 只落盘，投影先渲染旧的，到 system 归档时再重读」。

**落地**（src/session.py 归一化分支 + src/agent.py `_ltm_static_block`）：

- `_apply_system_ledger` 归一化分支（= system 归档点，本就断缓存）顺带 `session._ltm_refresh_epoch += 1`——长期记忆快照失效信号
- `_ltm_static_block` 投影走 `self._ltm_snap` 快照：epoch 未变直接返回上次文本（byte-stable）；epoch 变化才重读记忆库刷新；`set_session` 换档时快照重置（`self._ltm_snap = None`）

**设计哲学**：与 DSH「断点清账」同源——把必须做的失效/刷新挪到本来就断的时刻，零额外断点；单例 `_origin_session` 握手不受影响（快照的是文本，provider 仍每轮被调）。机制详情与四场景验证见 [长期记忆 · 静态层投影快照](../features/longterm-memory.md)。`/restart` 生效。

## provider 能力位

models.json profile 增 `in_history_system`（deepseek/deepseek-chat 已标 true，实测背书；
默认 false=现状归一化行为；anthropic 形态 system 为顶层参数天然不支持）。Agent.run 轮初
同步 `session._in_history_system = llm.in_history_system`。

## 施工收官（2026-09-12，commit 1dc8829）：五步落地 + 13 项场景测试全绿

spec s_eb14a8fd 五步施工落地（commit `1dc8829`），13 项场景测试全绿（重放 / append / 归一化 / 撤回 / 跨重启持久化 / 断点置脏等）：

| # | 文件 | 落地 |
|---|------|------|
| 1 | src/session.py | `_system_ledger` 四键账本 + `_apply_system_ledger`（装配后处理，**挂 `_append_answer_style` 之后**——比较口径含回答风格提示文本，hint 不随 append 叠加）+ `mark_system_dirty`（幂等置脏） |
| 2 | src/session.py | save/load：meta.json 顶层键 `system_ledger` 持久化与恢复（绕开 extra_state 的 `_state_provider` 全量替换语义）；load 缺省恢复值 `{"last_text":"","count":0,"dirty":True}`（存量 session 首轮建基线） |
| 3 | src/session.py | `_plan_fold` 计划真实变化处（`fc != _planned_fold or g != _planned_graduates`）顺带 `mark_system_dirty`——历史段形态真实变化（毕业顺移/折叠重排）= 前缀必断，断点清账零成本；**计划未变的纯追加轮不置** |
| 4 | src/llm_client.py | profile 能力位 `in_history_system` 读取（默认 False = 现状归一化行为） |
| 5 | src/agent.py | 轮初同步两点：`_sync_tools_schema_hash`（schema hash 变化=请求级前缀断 → 顺带置 dirty；首次调用只建基线不算断点）+ `session._in_history_system` 透传（切模型后下次 run 生效） |

**版本号跨重启延续**：append 生效文本带版本号（vN），`count` 随账本持久化——重启前 append 到 v2、重启后下一次 append 是 **v3 不重置**（重置会让重启首轮 append 的版本号回跳，引入一次多余的前缀变化）→ 跨重启前缀依然逐字节稳定，与 fold_count 持久化同属「restart 缓存稳定」家族（见 [折叠计划持久化](#折叠计划持久化fold_count--折叠粘性2026-08-31commit-7893bd5用户裁定)）。

**生效方式与首轮预期**：`/restart` 一次；重启后**首次投影**账本为空 → 走 `normalized 首建快照`（建基线），这一次断点是预期内的建账成本，之后进入正常三态循环。

## 可观测

`/context` 段落表上方显示 `system 段形态：**appended v2**（append-not-replace 账本；in_history_system=on）`；形态记入 _proj_stats.system_form（live+旁车都带）。byte-stable=全命中复用 / appended vN=追加且前缀保持 / normalized=归一化单条（断点清账）。

**跳变观察口径（施工收官后）**：改人设/钩子清单 → 形态 byte-stable → appended v1 跳变，`/stats` 折线对应位置**不再出现头部大断点**；毕业/折叠执行轮 → normalized（断点清账，预期内单断）；重启首轮 → normalized 首建快照（账本建基线，预期内）。

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

### 工具 schema 变化断点实证：tools 内部前缀匹配 + 64-token 块对齐 + 冷节点全 miss（2026-09-05，双端点探针）

**触发（用户请求）**：「测一下使用 fk-ds-v4-flash 和 deepseek 时候的缓存命中情况，比如在工具 schema 变化时，缓存会从哪里断开」——上方 v4 实证（R13 E5）只测过「tools 尾部 +1」且判「全断」，未测 tools **内部**的断点粒度，也没测中转层（flatkey）能否吃到缓存。

**探针**（`tmp/cache_probe_tools.py`）：固定 messages（system + 3 轮对话，~300 tok），只变换 tools 数组（2 个真实感工具 A/B 及变体），7 场景对照（DeepSeek 官方直连 api.deepseek.com，读 `prompt_cache_hit_tokens`）：

| 场景 | tools | prompt | hit | 断点解读 |
|---|---|---:|---:|---|
| S1 基线 | [A,B] | 662 | 0 | 首次全 miss |
| S2 完全重复 | [A,B] | 662 | 640 | 缓存生效（22=尾块不满 64 对齐） |
| S3 尾部追加 | [A,B,C] | 753 | **640** | **只 miss 新增（C 的 91 tok + 尾块）——前缀完整保留** ✅ |
| S4 恢复 | [A,B] | 662 | 640 | 缓存恢复 |
| S5 改第 1 个工具 description | [A′,B] | 652 | **0** | **从该工具起全断（含全部 messages）** |
| S6 改第 2 个工具 description | [A,B′] | 666 | **256** | **只保留工具 A 部分（256=4×64 块）** |
| S7 messages 尾部追加 | [A,B] | 678 | 640 | 增量命中 ✅ |

**三条规律**：

1. **缓存序列 = tools → system → messages**：tools 排在最前参与前缀缓存（与 v4 实证一致）；
2. **tools 内部同样是前缀匹配**：改第 N 个工具 → 从它断到结尾（其后工具与全部 messages 全部重算）；**尾部追加工具 → 只付增量 miss**（与追加消息同理）。⚠️ **细化 R13 E5 的「tools 尾部 +1 → 全断」字面口径**——受控探针（同脚本构造、字节级同前缀、复验稳定）下尾部 +1 只付增量；当时的全断观察疑受冷节点/序列化伪象干扰（冷节点现象当时未知，见下）。对 agt 的重定价：**会话中途追加新工具只付增量成本**（此前按全断预期管理）；但**修改已有工具 schema（加参数/改描述）仍是从该工具起全断**——300K 大会话一次重算按 miss≈hit 数十倍计价，**工具版本变更放新 session**；
3. **64-token 块对齐**：hit 恒为 64 的倍数（640/256），末尾不满一块的部分恒 miss（S2/S4 的 22 tok）。

**插曲：S3 首测 hit=0 是「冷节点」，不是断缓存**：S3 第一次跑出全 miss，差点误判「追加工具断全部」——复验 **[A,B,C] 连发 5 次全部稳定 hit=640**。结论：**DeepSeek 官方也是多实例部署、节点级缓存偶不共享**，撞上冷节点就一次性全 miss（之后 8 连发全是热节点）。与 ModelScope「随机路由吃不到缓存」同机制，官方概率低得多。**排障口径补充：单次全 miss ≠ 缓存规则破坏，连发复验再定性**。

**fk-ds-v4-flash（flatkey 中转）：缓存不可观测 ⚠️**：7 场景 + 重复调用 `cached_tokens` **恒 0**——中转只回 OpenAI 风格 usage，DeepSeek 的 `prompt_cache_hit/miss_tokens` 字段被剥掉。**无法证明它有没有缓存**，按「无缓存、全价计费」做预期管理（该渠道限时免费，损失可忽略）；若将来在 flatkey 跑**付费模型**做大上下文任务，缓存经济账按无缓存重算——大上下文优先官方直连或 glm-official。

> 探针留存 `tmp/cache_probe_tools.py`（DeepSeek 再换代可复跑对照；勿移入 `tools/`——插件扫描目录 import 即执行，见上方探针警告）。

## 相关页面

- [长期记忆](../features/longterm-memory.md) — episodic 召回（tail ambient `[epi·长期记忆]` 行来源）的检索流水线与演进
- [multi-agent](multi-agent.md) — 子 Agent assembly DSL（`|optional` 段默认不装配、`=on` 按需打开）与 reuse 隔离
- [运维与排障](../guides/ops.md) — /stats 页、投影转储、/context live 分段统计、常见错误对照
- [系统总览](../architecture/overview.md) — 模块地图、数据流
