# session 分支机制 · /branch——从任意轮带记忆分出支线（2026-10-08，用户提案，dc92c32 + v2 b37709e）

> 会话记忆是线性的，但任务不总是。主线（如 Agt 框架的自我版本迭代）积累了上千轮的项目架构认知；支线任务（B 站技术向视频、写简历、协助其它 repo 实例写 sub-agent、编写 MCP）想复用这份记忆作为经验，却不能把支线低相关上下文灌回主线稀释注意力。`/branch` 从当前会话第 N 轮处**引用式**分出一个支线 session：读侧合成完整记忆、写侧天然隔离。v2 起支持**支线上再分叉**（链式基底，记忆逐级继承）。

## 职责与动机（用户提案原文场景）

- **主线**：当前 repo 当前 session 的主任务——Agt 框架不断自我迭代，记忆=对项目核心架构的认知能力
- **支线**：与主线无关但需要这份认知的任务——继承 session 主线的架构理解，又不污染主线记忆
- **目录形态（用户设计）**：在 repo 存档目录下建分支目录（`项目社交卡片制作/`、`协助写简历/` 等），分支的 meta.json 标记「继承主线前 N 条事件」，events/toollog/llm_calls 从分支创建后独立追加
- 实现**只读引用、不复制**：支线不拷贝主线历史文件，load 时按需合成——零复制成本、基底可校验（目录名最终定为 `branches/`，提案原文的 `bratches` 按英文正确拼写落盘）

## 用法

```
/branch 项目社交卡片制作        ← 从当前会话（主线或支线）开支线，继承全部记忆（如 1380+ 轮）
/branch 协助写简历 600          ← 只继承当前会话 events 前 600 行（自选切点）
  ↓ 在分支上干支线活——新轮只写分支目录
/resume 主线名                  ← 随时回主线（记忆原地未动）
/resume 项目社交卡片制作         ← 再回支线（纯分支名，或「主线名/分支名」精确形式）
```

- 继承源 = **当前所在会话自己**的 events.jsonl：主线上分叉=主线前 N 行；支线上再分叉=该支线前 N 行（上游主线基底由 load 链式自动补上，见下节 v2）
- `/list` 与 WebUI 会话下拉显示**完整链**：`主线名 ⇢ 分支名 [⇢ 二级支线]`（`display_chain`）
- 支线内部继续 `/branch` 可再嵌套分叉——新分支目录一律**平铺在顶层主线的 `branches/` 下**（不做 `branches/branches/` 嵌套树）

## 目录与元数据

```
sessions/<主线ts>/
  events.jsonl / toollog.jsonl / llm_calls.jsonl / meta.json
  branches/<分支名>/              # 与主线同构四件套
    events.jsonl / toollog.jsonl / llm_calls.jsonl / meta.json
```

分支目录**一律平铺在顶层主线的 `branches/` 下**（一级、二级+ 同级），不做目录嵌套树。

分支 meta.json 顶层带 `branch` 字段（主线/普通会话**不写该字段**，零侵入；`save()` 中 `if self.branch_meta` 才落）：

| 键 | 语义 |
|---|---|
| `branch_of` | 基底所在层（**相对 sessions 根的路径串**，v2 起）：一级 = 主线目录名（时间戳文件夹名，非 session name——目录会改名，时间戳目录名稳定）；二级+ = `<主线ts>/branches/<支线名>` |
| `inherit_lines` | 继承**该层** events.jsonl 前 N 行（不带 N = 全部） |
| `base_hash` | 该层前 N 行内容的 sha256[:16]（`_events_fingerprint`，创建时算、加载时校验） |
| `display_chain` | 全链显示串（v2，2026-10-08）：`主线名 ⇢ 支线名 [⇢ 二级支线]`；`list_sessions` 优先消费，缺省回退 `主线名 ⇢ 分支名` |

读取侧守卫：仅新文件夹结构支持分支（`path.name == "meta.json"` 才认 branch 字段）——旧扁平迁移路径无主线目录可指。

## 机制：引用式合成 + 写侧隔离

**读侧（Session.load）**：meta 带 `branch` 字段 → 读主线 events.jsonl 前 N 行 + 分支自己的事件行 → **拼成完整事件流喂现有重放器**——投影 / tier / 折叠引擎**零感知零改动**，分支里看到的历史与「主线真的跑到第 N 轮」完全同构。

**写侧**：分支的 events/toollog/llm_calls 句柄只绑分支目录，追加天然隔离——测试实测主线 events.jsonl **字节级不变**。

**周边流合成**：toollog / llm_calls / recaps 先载主线基底、再载分支追加——**call_id 全局续号**，基底轮的工具调用完整详情召回（agent_query_tool_detail 类通道）、recall_turn、折叠摘要全部照常可用。

### 基底指纹：base_hash 漂移校验

`_events_fingerprint(events)`：逐事件 `json.dumps(sort_keys=True)` 规范序列化后 sha256 取 [:16]。`/branch` 创建时对继承层前 N 行算出写进 meta；`Session.load` 用同一函数校验（v2 每层独立校验）：

| 层现状 | 行为 |
|---|---|
| 指纹一致 | 正常合成 |
| 该层被 rewind 重写（指纹漂移） | **告警仍继续合成**——记忆尽力保留 |
| 某层目录整个没了（挪走/删除） | 该层降级为空，**不断链**（下游剩自己可用的层） |

## v2：支线上再分叉——链式基底合成（2026-10-08 用户提案，commit b37709e）

**问题（用户原话）**：「如果已经在支线了，再创建支线的时候是把所在支线 copy 一份后把分支切过去对吧？」
答案：**语义等价**（带全链记忆过去），实现仍是**引用式链**而非物理 copy——主线与上游支线保持唯一事实源、零冗余、每层可校验。

### 链式形态

```
主线（如 1380 轮）                     ← 唯一事实源，被各分支共享
 └─ branches/A   （/branch A，继承主线前 N 行）
     └─ branches/B（在 A 上 /branch B，继承 A 自己 events 前 M 行）
```

- B 的 load 记忆 = **主线 events[:N] + A events[:M] + B 自己新增**（三级拼接，喂同一重放器）
- 新分支目录**平铺在顶层主线的 `branches/` 下**（不做 `branches/branches/` 嵌套树），目录名即分支名
- recall / 工具详情召回跨级全命中（场景⑥实测：二级支线 recall 到主线内容与一级支线内容）

### 两处实现改动

| 位置 | v1 | v2 |
|---|---|---|
| `meta.branch.branch_of`（src/commands.py） | 主线目录名 | **相对 sessions 根的路径串**：一级=`<主线ts>`；二级+=`<主线ts>/branches/<支线名>` |
| `Session.load` 基底合成（src/session.py） | 单层（主线前 N 行） | `_branch_chain_bases()` 沿 `branch_of` **逐层上溯到根**（叶→根收集、根→叶返回），每层带自己的 `events[:inherit_lines]` + `base_hash` |

- 继承源改为「当前会话自己的 events.jsonl」：主线上分叉=主线前 N；支线上分叉=该支线自己 events 前 M（其上游基底由 load 递归补上）
- 每层 toollog / llm_calls 逐层合载（`_chain_dirs`），counter 全局续号；每层 `_load_recaps(dir)` 使合成流 idx 与 recap 对齐
- 目录定位：`is_branch = bool(sess.branch_meta)` → `sessions_root = sdir.parents[2]`、`top_dir = sdir.parents[1]`（顶层主线）

### 边界（`_branch_chain_bases` 三重防御）

| 情形 | 行为 |
|---|---|
| 某层 events 缺失 / `inherit_lines=0` | 该层降级为空 + 日志警告，**不断链** |
| 链上某层 meta 不可读 | 停止上溯（已收集层仍生效） |
| 环 / 超深（>8 层） | 循环上限 8 层截断 |

### 显示链：display_chain

创建时写入真值（父链 + `⇢` + 本分支名，如 `主线测试 ⇢ 测试分支 ⇢ 二级支线`）；`list_sessions` 优先读它（缺失回退 `主线名 ⇢ 分支名`），WebUI 会话下拉同链可见——支线上再分叉不再只看到最后一段。

## 调试中抓到的两个坑

1. **新分支首载零基底**：基底合成最初写在 `if events_path.exists()` 分支内——新分支**还没有自己的 events.jsonl**，条件恒假 → 首次 load 记忆全空（场景②测试抓的）。修复：基底合成提为无条件前置步骤（v2 的链式合成同样在 events 判断之前）。
2. **主线目录定位**：`sdir.parent / branch_of` 与 `parent.parent` 都不对（目录嵌套层级随部署形态变）。修复：**从 sessions 根按 `branch_of` 名遍历解析**（`_find_session_dir_by_name` 同款），场景④实测主线目录挪走后正确降级。v2 升级为路径串解析（`sessions_root / branch_of`），仍需 `parents[2]` 定位 sessions 根。

## 验证

- 37 项场景测试全绿（v1 28 项含写隔离字节级比对、四场景定位链；v2 加场景⑥ 链式嵌套分叉：`branch_of` 相对路径 + `display_chain` 完整链 + 二级支线 recall 跨级命中主线与一级支线内容）
- L2 隔离实跑（regr-9678）：分支实例启动 + 钩子 + react 全程无 traceback
- commits `dc92c32`（v1）+ `b37709e`（v2 链式嵌套）已推送；site-packages 已同步（**pip 实例需 /restart 生效**）

## 与其它模块的关系


- [检查点快照与回溯](snapshot-rewind.md)：主线 rewind 重写 events → base_hash 漂移告警（校验即为此设计）
- [中断轮恢复](resume-interrupted.md)：/resume 体系入口，分支名/主线名均可解析（解析顺序：路径 → id → name → 分支名 → 旧扁平回退）
- [recall_turn](recall-tools.md)：基底轮召回依赖 toollog call_id 全局续号（链式多级同样续号）
- [上下文引擎](../architecture/context-engine.md)：投影/tier/折叠零感知——分支只是「另一份事件流」喂进同一重放器
- [wiki 自动维护](wiki-auto-maintenance.md)：`before_answer`/`turn_end` 钩子 context 注入 `is_branch` → check_changes 节点短路——**支线干活 wiki 维护静默**（只有主线轮触发）；支线产出的 wiki 知识回主线后由主线轮自然维护

## 注意事项

- 分支是**只读引用主线历史**：主线后续新增轮次不会自动进分支（继承边界固定在创建时点的 N 行）
- 支线的产出想回主线需显式操作（如让主 Agent 消化支线结论）——机制本身不回流，这正是「不稀释主线注意力」的语义
- **链式嵌套时上游不可缺**：删/挪顶层主线目录会让链上所有下游分支一起降级（各层基底降为空，只剩自己的轮）；链上限 8 层，超深或成环自动截断
- 仅新文件夹结构（`sessions/<ts>/`）支持；旧扁平迁移存档无分支能力
- 改动在 `src/session.py`（`_branch_chain_bases` 链解析 / 合成 / 指纹 / list_sessions 展示）+ `src/commands.py`（/branch、/list 展示）+ WebUI 会话下拉；生效需 /restart

## 相关页面

- [运维与排障 · 存档布局](../guides/ops.md)：`sessions/<ts>/branches/<分支名>/` 目录全景
- [检查点快照与回溯](snapshot-rewind.md)：主线被重写的漂移来源
- [上下文引擎与缓存优化](../architecture/context-engine.md)：被「零感知」的投影/折叠引擎