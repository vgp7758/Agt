# toollog · 工具调用详情库（ToolLog）· repo 共享单文件 + call_id=「&lt;session_id&gt;-N」

> `src/toollog.py`。工具调用在 events/steps 里只存 `call_id`，完整的 `name / arguments / result` 存这里；组装上下文时按 call_id 召回 + 按步距衰减摘要。**2026-10-09 起落盘形态从「每 session 一份」改为「一个 repo 一份共享 JSONL」**（`sessions/toollog.jsonl`），`call_id = "<session_id>-N"` 自带归属——分支 / 子 Agent 的工具详情从 id 直读归属，跨 session 物理不撞。

## 职责与数据形态

- `ToolLog._data: dict[call_id, {name, arguments, result, step, turn}]`——内存全量 + JSONL append 落盘
- **距离衰减**：`detail_limit(d) = max(DETAIL_FLOOR, DETAIL_BASE - d*DETAIL_STEP)`（当前步 1500 字、每远一步 -15、最低 20；`set_detail_params` 运行时可改）——入参/结果各自按 limit 摘，投喂投影时老调用的详情自动瘦身
- 落盘 append-only：record 一行、O(1)，不随 session 全量重写（0.7.x 时代嵌入字段的教训）

## 落盘状态机

| 状态 | 行为 |
|---|---|
| `_path=None`（session 未绑定 / name 未就绪） | record 只进内存 buffer |
| `set_path` 时文件**不存在** | flush 内存全量建立文件 |
| `set_path` 时文件**已存在** | 假定已 load，**不重写** |
| 已绑定 | record → 内存 + `_append_line` 一行（写失败静默吞——内存里仍有，主循环不阻塞） |

`load_from_jsonl` 流式读全部 entry 并**恢复 counter**（`_own_counter_from` 只数本前缀的序号）。

## repo 共享单文件（2026-10-09 二轮定稿，commits 1c320c8 + f0d1e92）

```
~/.agt/repos/<repo-key>/sessions/toollog.jsonl   ← 主线 + 各分支 + 子 Agent 全部 append 进来
```

| 件 | 说明 |
|---|---|
| `_shared_toollog_path(sdir)`（src/session.py） | `_sessions_root_of(sdir) / "toollog.jsonl"`；`_bind_persistence_paths` / `Session.load` / 旧扁平迁移三处统一走它 |
| `_sessions_root_of(sdir)` | 沿父链向上找名为 `sessions` 的目录（≤12 层，兼容多层嵌套 agents），兜底旧行为——主线 / 平铺分支 / 旧 branches 嵌套 / 子 Agent 四形态统一 |
| `_call_prefix_of(sdir)` | call_id 前缀 = **相对 sessions 根的路径去掉容器段**（`agents`/`branches`）后以 `-` 连接：主线/平铺分支 `a`/`b`；子 Agent `a-coder`、`a-wiki-updater_2`；旧嵌套 `a-b`。五轮定稿——初版直接用 agent_id，**不同主线的同名子 Agent 撞同一 `coder-1`**（共享文件下后者覆盖前者、counter 互顶），遂改路径拼接 |
| `next_id()` | `<prefix>-N`；无前缀退 `cN` 兼容 |
| `set_path` / `_append_line` | 文件不存在才 flush 建立，存在即假定已 load；一行一次 append |
| `list_tool_logs` | 只列**本前缀**的 id |

## call_id 三代形态

| 代 | 形态 | 说明 |
|---|---|---|
| 一 | `c1` `c3309` | 2026-10-09 前的 per-session 计数——**主线与子 Agent 会撞号**（历史缺陷，迁移时暴露损失） |
| 二 | `m1000-c1` | 分支锚点前缀（共享单文件落地前一天的中间方案，已废弃） |
| 三（现行） | `a-1024` `b-35` `a-coder-3` | `<前缀>-N`：前缀 = 相对 sessions 根的路径（字母 id / 含主线段）；归属从 id 直读、跨 session 物理不撞 |

旧 id 一律兼容可查：events 里的历史引用照旧 `get()` 得到，`_own_counter_from` 兼容两种旧形态。

## 消费端

| 消费端 | 用途 |
|---|---|
| `Session._steps_to_messages` | 投影按 call_id 召回工具结果 + 距离衰减摘要 |
| `get_tool_detail` / `agent_query_tool_detail` | Agent 自查某次调用的完整入参/结果 |
| `call_again` | 按 id 取原次 `{name, arguments}` 同参重跑（见下节） |
| [recall_turn](recall-tools.md) | 历史轮召回 |
| 折叠摘要 / 超深档投影 | [上下文引擎](../architecture/context-engine.md) |
| `search()` | 关键字初筛（无 LLM 的 Agentic RAG 第一阶段） |

## call_again · 同参重跑历史工具调用（2026-10-11，用户提案，commit 8a9cc8f，随 v0.34.5 发布）

**用户提案（2026-10-11）**：「加一个工具 call_again，agent 想以同一组参数重复某一次工具调用，直接 call_again(call_id) 就可以了，不过 llm 知道每一次工具调用的 call_id 吗？」

## LLM 如何拿到 call_id（提案疑问的答案：三渠道天然就位）

| 渠道 | 说明 |
|---|---|
| tool_use 回传 id | OpenAI 协议原生：模型发起 tool_call 时引擎分配 id 并在 assistant 消息回传——模型上下文里**每次调用都带着自己的 id**（它「刚做过」的调用 id 可见） |
| 衰减摘要末尾标注 | 历史工具结果按步距衰减后被截断的，末尾标注 id（如 `a-1024`）——SYSTEM 里 get_tool_detail 说明就写着「需要完整内容时按 id 拉取」 |
| `list_tool_logs` 兜底 | 不确定 id 时列清单（id · 工具名 · 结果字数 · 步号） |

## 机制（src/toollog.py `call_again`）

```
call_again(call_id)   # 如 call_again("a-1024")
```

- 查共享 toollog 取原次 `{name, arguments}` → `agent._exec_tool(name, args)` **标准管线重跑**（审批 / 结果截断照常）→ 返回 `[重跑 a-1024 → <工具名>（参数与原次一致）]` + 结果
- **本次结果作为 call_again 自己的返回与记录**（`get_tool_detail` 可查）——重跑不额外落一条被重跑工具的记录
- **适用场景**（写进工具描述，模型自行判断）：再跑一次检索/查询拿最新结果、重试时好时坏的外部调用、以相同输入验证修复效果

**四类兜底**（全部返回文案、不炸轮）：无此 id（提示先 `list_tool_logs` 查清单）/ 记录缺损（无工具名）/ **套娃拒绝**（重跑 call_again 自身——请用原工具的 call_id）/ 执行入口未就绪；重跑异常包成 `[重跑失败] <类型>: <信息>`。

**已知边界**：远程执行的旧调用重跑为**本地**——`remote_instance_id` 是路由元数据、不进存档参数；需要远程请直接调原工具（描述里已注明）。

**验证**（单测四场景全绿，commit 8a9cc8f）：① 正常重跑——参数原样透传 `_exec_tool`（`{"path":"src/x.py","line_numbers":true}` 一字不差）；② 无此 id → 明确文案；③ 套娃拒绝；④ 记录缺损兜底。注册处带 `param_descriptions`（call_id 的指引与工具描述同款）。

**关联**：call_id 形态与衰减机制见本页上文；与 [工具外置](tool-externalization.md) 无关——toollog 是「引擎写、工具读」的可观测性出口，call_again 属引擎内置工具。

## 与 rewind / merge 的关系

- **rewind 不重写 toollog**（`_rewrite_persistence` 只截 events + recaps）——共享文件按 session 重写会误删别的 session 记录；被回退轮的多余记录留着无害（call_id 不撞、`get` 直查仍可用）
- **`/merge` 不搬运 toollog**（同一个文件，轮/事件 append 即可）
- 旧扁平结构 `<name>.toollog.jsonl` 迁移：`Session.load` 一次性并入共享文件（按 call_id 去重后 append）

## 迁移实盘与教训（2026-10-09）

- 41 repo / 62 份 per-session toollog → 各 repo 一份共享文件、残留 0 份；含子 Agent 共 18 repo / **221 份（105.6MB）** → 各 repo 一份
- 分支重复物化实证：a/b/c 三份 41MB → 一份 40.6MB（43779 行 → 去重 14724 条，省 29055 行）
- ⚠️ **一处不可逆损失**：子 Agent 旧记录 id 是裸 `cN`（与主线撞号），迁移按 call_id 去重时被主线同 id 记录挤掉 → 部分子 Agent 历史工具详情张冠李戴（不崩但内容错位），原件已删。教训：**迁移去重前先备份 / 同 id 两份都保留**。新前缀从机制上杜绝撞号

## 注意事项

- 共享文件删/坏 = **全 repo 失去工具详情**（events 仍在，投影里工具结果变「（详情已失效）」）
- 只 append，禁止按 session 重写/裁剪；一个 repo 只支持单实例运行（无跨进程锁，大 result 行理论可交错——[ops · 单实例约束](../guides/ops.md)）
- 会话目录改名不影响旧 call_id（历史字面量），但新轮用新前缀（目录名即前缀来源）
- 判别标准下 toollog 是「引擎写、工具读」的可观测性出口——**不外置**（[工具外置 · 四象限](../architecture/tool-externalization-criteria.md)）

## 相关页面

- [session 分支机制](session-branching.md)：共享单文件的来龙去脉（40MB 全量拷贝症状 → 终版）与平铺化
- [运维与排障 · 存档布局](../guides/ops.md)：sessions 目录树现状
- [recall_turn](recall-tools.md)：call_id 依赖方
- [trace-fold](trace-fold.md) / [上下文引擎](../architecture/context-engine.md)：衰减摘要与折叠消费
