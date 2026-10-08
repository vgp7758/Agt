# session 分支机制 · /branch——从主线任意轮带记忆分出支线（2026-10-08，用户提案，commit dc92c32）

> 会话记忆是线性的，但任务不总是。主线（如 Agt 框架的自我版本迭代）积累了上千轮的项目架构认知；支线任务（B 站技术向视频、写简历、协助其它 repo 实例写 sub-agent、编写 MCP）想复用这份记忆作为经验，却不能把支线低相关上下文灌回主线稀释注意力。`/branch` 从主线第 N 轮处**引用式**分出一个支线 session：读侧合成完整记忆、写侧天然隔离。

## 职责与动机（用户提案原文场景）

- **主线**：当前 repo 当前 session 的主任务——Agt 框架不断自我迭代，记忆=对项目核心架构的认知能力
- **支线**：与主线无关但需要这份认知的任务——继承 session 主线的架构理解，又不污染主线记忆
- **目录形态（用户设计）**：在 repo 存档目录下建分支目录（`项目社交卡片制作/`、`协助写简历/` 等），分支的 meta.json 标记「继承主线前 N 条事件」，events/toollog/llm_calls 从分支创建后独立追加
- 实现**只读引用、不复制**：支线不拷贝主线历史文件，load 时按需合成——零复制成本、基底可校验（目录名最终定为 `branches/`，提案原文的 `bratches` 按英文正确拼写落盘）

## 用法

```
/branch 项目社交卡片制作        ← 从当前主线开支线，继承全部记忆（如 1380+ 轮）
/branch 协助写简历 600          ← 只继承主线前 600 行事件（自选切点）
  ↓ 在分支上干支线活——新轮只写分支目录
/resume 主线名                  ← 随时回主线（记忆原地未动）
/resume 项目社交卡片制作         ← 再回支线（纯分支名，或「主线名/分支名」精确形式）
```

- `/list` 中分支显示为 `⇢ 主线名 ⇢ 分支名`；WebUI 会话下拉同链可见
- 支线内部继续 `/branch` 可再嵌套分叉

## 目录与元数据

```
sessions/<主线ts>/
  events.jsonl / toollog.jsonl / llm_calls.jsonl / meta.json
  branches/<分支名>/              # 与主线同构四件套
    events.jsonl / toollog.jsonl / llm_calls.jsonl / meta.json
```

分支 meta.json 顶层带 `branch` 字段（主线/普通会话**不写该字段**，零侵入；`save()` 中 `if self.branch_meta` 才落）：

| 键 | 语义 |
|---|---|
| `branch_of` | 主线目录名（时间戳文件夹名，非 session name——目录会改名，时间戳目录名稳定） |
| `inherit_lines` | 继承主线 events.jsonl 前 N 行事件（不带 N = 全部） |
| `base_hash` | 主线前 N 行内容的 sha256[:16]（`_events_fingerprint`，创建时算、加载时校验） |

读取侧守卫：仅新文件夹结构支持分支（`path.name == "meta.json"` 才认 branch 字段）——旧扁平迁移路径无主线目录可指。

## 机制：引用式合成 + 写侧隔离

**读侧（Session.load）**：meta 带 `branch` 字段 → 读主线 events.jsonl 前 N 行 + 分支自己的事件行 → **拼成完整事件流喂现有重放器**——投影 / tier / 折叠引擎**零感知零改动**，分支里看到的历史与「主线真的跑到第 N 轮」完全同构。

**写侧**：分支的 events/toollog/llm_calls 句柄只绑分支目录，追加天然隔离——测试实测主线 events.jsonl **字节级不变**。

**周边流合成**：toollog / llm_calls / recaps 先载主线基底、再载分支追加——**call_id 全局续号**，基底轮的工具调用完整详情召回（agent_query_tool_detail 类通道）、recall_turn、折叠摘要全部照常可用。

### 基底指纹：base_hash 漂移校验

`_events_fingerprint(events)`：逐事件 `json.dumps(sort_keys=True)` 规范序列化后 sha256 取 [:16]。`/branch` 创建时对主线前 N 行算出写进 meta；`Session.load` 用同一函数校验：

| 主线现状 | 行为 |
|---|---|
| 指纹一致 | 正常合成 |
| 主线被 rewind 重写（指纹漂移） | **告警仍继续合成**——记忆尽力保留 |
| 主线目录整个没了（挪走/删除） | 降级为纯分支事件流，不炸轮 |

## 调试中抓到的两个坑

1. **新分支首载零基底**：基底合成最初写在 `if events_path.exists()` 分支内——新分支**还没有自己的 events.jsonl**，条件恒假 → 首次 load 记忆全空（场景②测试抓的）。修复：基底合成提为无条件前置步骤。
2. **主线目录定位**：`sdir.parent / branch_of` 与 `parent.parent` 都不对（目录嵌套层级随部署形态变）。修复：**从 sessions 根按 `branch_of` 名遍历解析**（`_find_session_dir_by_name` 同款），场景④实测主线目录挪走后正确降级。

## 验证

- 28 项场景测试全绿（含写隔离字节级比对、四场景定位链）
- L2 隔离实跑（regr-9678）：分支实例启动 + 钩子 + react 全程无 traceback
- commit `dc92c32` 已推送；site-packages 已同步（**pip 实例需 /restart 生效**）

## 与其它模块的关系

- [检查点快照与回溯](snapshot-rewind.md)：主线 rewind 重写 events → base_hash 漂移告警（校验即为此设计）
- [中断轮恢复](resume-interrupted.md)：/resume 体系入口，分支名/主线名均可解析（解析顺序：路径 → id → name → 分支名 → 旧扁平回退）
- [recall_turn](recall-tools.md)：基底轮召回依赖 toollog call_id 全局续号
- [上下文引擎](../architecture/context-engine.md)：投影/tier/折叠零感知——分支只是「另一份事件流」喂进同一重放器

## 注意事项

- 分支是**只读引用主线历史**：主线后续新增轮次不会自动进分支（继承边界固定在创建时点的 N 行）
- 支线的产出想回主线需显式操作（如让主 Agent 消化支线结论）——机制本身不回流，这正是「不稀释主线注意力」的语义
- 仅新文件夹结构（`sessions/<ts>/`）支持；旧扁平迁移存档无分支能力
- 改动在 `src/session.py`（合成/指纹/解析）+ `src/commands.py`（/branch、/list 展示）+ WebUI 会话下拉；生效需 /restart

## 相关页面

- [运维与排障 · 存档布局](../guides/ops.md)：`sessions/<ts>/branches/<分支名>/` 目录全景
- [检查点快照与回溯](snapshot-rewind.md)：主线被重写的漂移来源
- [上下文引擎与缓存优化](../architecture/context-engine.md)：被「零感知」的投影/折叠引擎
