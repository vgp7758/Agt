# 长期记忆 · 三类记忆 × 注入方式 + episodic 召回演进

> 存储/检索核心 `src/longterm_memory.py`（支撑层，见 [系统总览](../architecture/overview.md)）；Agent 工具面五件套；WebUI `/memory` 管理页（`src/server.py` 路由）。
> **blog 04（记忆系统篇）的三类×三种注入框架仍成立，但写于 provider 直调时代**——本页记录其后的架构演进，校对/续写 blog 04 以此为准。

## 职责：三类记忆 × 三种注入

| 类型 | 注入方式 | 说明 |
|------|---------|------|
| semantic | 常驻 | 每轮固定注入（用户偏好/背景等稳定事实） |
| episodic | 按召回 | 检索命中才注入，投影中呈 `[epi·长期记忆]` 行（tail ambient 段，见 [上下文引擎](../architecture/context-engine.md)） |
| procedural | 标题常驻 + 正文按需 | 只注入标题清单，Agent 需要时 `read_procedure(id)` 取全文 |

Agent 工具五件套：`add_memory` / `search_memory` / `read_procedure` / `update_memory` / `delete_memory`（blog 04 只提了 `add_memory`，已过时）。

## episodic 召回：从标点分词到关键词提取（provider 直调 → 检索工作流）

### 旧版（blog 04 记载）

session 每轮直接调 provider 回调 `episodic_block(user_message)`：

```
user_message ──_TOKEN_SPLIT_RE 按标点分词──> search(query, type_="episodic", limit=3) ──top-3──> 注入
```

**中文命中率坑（逼出重构的根因）**：`_TOKEN_SPLIT_RE` 按标点切词，中文长句整段近似一个 token（如「我们的三种记忆中，情景经验是如何召回的？」），子串匹配几乎必然 miss——命中率"基本靠缘分"。

### 当前版（before_turn 检索工作流接管）

```
user_message
  → LLM 提关键词（local-qwen 小模型，抽 3~8 个短词）
  → join → search_memory(type=episodic)
  → 收集为 kind="epi" 候选（与历史轮/语义检索候选同台竞争）
  → LLM 精排裁决（无关的 episodic 被过滤掉）
  → 注入 [epi·长期记忆] 行
```

- provider 路径（`episodic_block`）降级为**工作流不存在时的兜底**
- 活例子（2026-08-21 本 session）：before_turn_retrieval 刚好检索出「第 106 轮：上次 blog 补充的讨论」——跨 session 经验回流的直接体感，可作 blog 04 开篇引子
- 与 [wiki_auto_query](wiki-auto-query.md) 同挂 before_turn 钩子，**并行执行**（ThreadPoolExecutor，见 [workflow-hooks](../architecture/workflow-hooks.md)）——两条检索流水线同族不同料（wiki 检索 vs 记忆检索）

### 设计迁移：该不该注入，从检索层上移到精排层

episodic 召回并入统一检索流水线后，与 blog 03 的检索工作流形成呼应，共享「宽进候选 → 严出裁决」骨架：

- **召回层**只负责"可能相关"（关键词宽匹配，宁可多收）
- **精排层** LLM 负责"真的相关"（无关 episodic 过滤）

误注入率显著下降——旧版 top-3 检索到什么就注入什么，没有相关性裁决。

## 写入幂等：add() 同 type+title 自动更新

`add()` 遇同 type+title **更新而非新增**。Agent 判断"值得记"的标准会漂移，同一条经验可能被记两三次（实测：本 session 中 replace_lines 参数那条记忆被沉淀了两次）。该去重语义让 `add_memory` 工具描述敢写"放心重复调用同主题"——**写入路径的幂等设计**。

## 双主权：Agent 沉淀 + 用户管理

Agent 自主沉淀的记忆会有错（如对用户偏好的误判），记忆不能是黑盒，用户必须能看、能改、能删：

- **CLI**：`/memory` 命令
- **WebUI**：`/memory` 页面——Tab 按 type 筛选 / 搜索 / 原地编辑 / 删除

「Agent 记它的，用户管着的」——双向纠错，比单向"Agent 自己记忆"完整得多。

## 存储

`~/.agt/repos/<fixed-cwd>/memories/`——cwd 斜线替换为 `-`（如 `D:\AI_Usings\Agt` → `D--AI_Usings-Agt`），一眼可辨是哪个 repo；旧 hash 目录启动自动迁移。存档布局全貌见 [guides/ops](../guides/ops.md)。

## 注意事项（blog 04 校对清单）

| 位置 | blog 写的 | 现状 |
|---|---|---|
| 存储路径 | `~/.agt/repos/<workspace_hash>/memories/` | `<fixed-cwd>/memories/`（hash → 路径可读化） |
| episodic 代码段 | `self.search(query, type_="episodic", limit=3)` | 代码本身仍对，但 query 来源已变（小模型提关键词，见上） |
| 工具 | 只提 `add_memory` | 五件套（见职责节） |
| （缺章节） | — | 建议补两节：「episodic 召回的演进：从标点分词到关键词提取」「记忆要能被用户管理」+ add 防重复沉淀一句（预计 +800~1000 字） |

## 相关页面

- [wiki_auto_query](wiki-auto-query.md)：同族 before_turn 检索流水线（wiki 检索版：本地提词 + 余弦阈值裁决，零云端 token）
- [上下文引擎](../architecture/context-engine.md)：`[epi·长期记忆]` 行注入 tail ambient `<system-reminder>` 段
- [工作流引擎与钩子](../architecture/workflow-hooks.md)：before_turn 多工作流并行执行、检索型钩子输出纪律
- [系统总览](../architecture/overview.md)：longterm_memory.py 归支撑层、`/memory` 路由
- [guides/ops](../guides/ops.md)：存档布局（memories/ 目录）
