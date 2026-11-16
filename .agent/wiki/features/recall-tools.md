# recall_turn · 会话历史轮次召回工具（memory_tools · 引擎内置）

> 工具面 `src/memory_tools.py` 注册 `recall_turn`；实现核心在 `src/session.py` 的 `recall()` + `_format_turn_full()`。属引擎内置（读 `events.jsonl`/turns，是[工具外置判别标准](../architecture/tool-externalization-criteria.md)下的「引擎写、工具读——可观测性出口」，不迁外置）。

## 职责与签名

按关键词/语义在**【全部】历史轮次**里搜索，返回匹配轮的上下文；或**按轮号精确定位**展开某一轮——当早期对话滑出上下文窗口、或忘了之前某轮的具体细节时，用它把那一轮捞回来。

```python
recall_turn(query: str = "", contains_reasoning: bool = False,
            tools: str = "brief", turn: int = None, offset: int = 0) -> str
```

| 参数 | 默认 | 语义 |
|------|------|------|
| `turn` | `None` | **轮号精确定位**（2026-11-16 新参，优先于 query）：取第 N 轮完整内容，见[下节](#轮号精确定位--前后脉络展开turn--offset2026-11-16用户提案commit-674891e) |
| `offset` | `0` | 配合 turn：附带前后各 M 轮的 user/answer 精简脉络（见下节） |
| `query` | `""` | 关键词/短句；支持多关键词 OR 与通配符（见下） |
| `contains_reasoning` | `False` | `False` 不含思考过程；`True` 一并带上每步 reasoning 与回答的 reasoning |
| `tools` | `"brief"` | **召回展示分层**（见下） |

两种用法（**turn 优先**）：① `turn=N` 轮号精确定位展开（配 `offset` 带前后脉络）；② `query` 关键词/语义搜索全部历史（含被折叠为结构摘要的轮——其 user/answer 原文始终可召回）。

## 轮号精确定位 + 前后脉络展开：turn / offset（2026-11-16，用户提案，commit 674891e）

**动机（用户 2026-11-16）**：recall_turn 找回成功率偏低，且 [before_turn_retrieval](../architecture/workflow-hooks.md) 的命中只有摘录——agent 缺一个「展开某个命中的详细上下文」的准确工具。落点两处：**命中即带轮号 id** + **按轮号精确展开**（`674891e`）。

**新参数语义**（src/session.py `recall()` + src/memory_tools.py）：

- `turn=N`：直接取第 N 轮完整内容（`_format_turn_full` 全套：user/answer 原文 + 工具调用清单；`contains_reasoning` / `tools` 同样生效）。retrieval-hint 命中行首的 `[第N轮]` 号即此参数——**id 就是本会话轮次，不新造 id 体系**
- `offset=M`：附带前后各 M 轮的**精简脉络**（user/answer 各截 160 字一行）；脉络里想看某轮完整内容 → 再 `recall_turn(turn=该轮号)`；主体轮不重复出现在脉络里
- 边界处理：轮号越界 → 报总轮数范围；非法值 → 友好报错；offset 自动钳制在会话首尾

```python
recall_turn(turn=123, offset=2)   # 第123轮完整内容为主体 + 前后第121~125轮脉络
```

**配套：retrieval-hint 命中行带 `[第N轮]` + 尾部操作指引**（src/workflows/before_turn_retrieval.xml，种源 + ~/.agt/workflows/ 全局副本已同步）：

```
（展开某命中：recall_turn(turn=该轮号, offset=2)——取该轮 user/answer 原文+前后轮脉络；
工具调用细节另用 get_tool_detail(call_id)。仅带 [第N轮] 号的命中可这样展开——epi/跨会话命中无本会话轮号）
```

即 agent 每次看到命中就知道展开姿势；brief 模式折叠掉的**工具过程细节**按 `call_id` 用 `get_tool_detail` 查；episodic / 跨会话命中没有本会话轮号、不适用此展开（关键字搜索仍可用）。

**验证**：L3 十三项全过（定位取轮 / offset 边界首尾钳制与主体不重复 / 越界与非法值 / query 路径不回归 / 二段降级标注与命中 / 无命中新提示）；唯一初判 FAIL 复核为 mock 断言口径问题（summary 与 user 同文致标题行重复计数），产品逻辑正确。生效：`/restart`（engine 侧新参数）；hint 指引行随全局工作流副本就位。

## 检索策略（自动降级，2026-09-20 commit a6e3290 + 2026-11-16 二段降级）

1. **配了 embed 模型**（`self.vec_store` 非 None，共享 [RAG 惰性单例](rag.md)的 embedder）→ **语义召回 top-K 轮**：换说法也能搜到
2. **否则**（没配 embed，或语义无结果）→ **summary + user + answer 子串/通配匹配**：

### query 的三种写法

| 写法 | 语义 | 示例 |
|------|------|------|
| `a \| b`（`\|` / 空格 / 中英文逗号 / 顿号 / 分号分隔） | **OR**——任一命中即命中 | `"replace_lines \| 封面"` → 命中两轮 |
| `replace_*`（含 `* ? []`） | **通配符**（fnmatch；前后包 `*` = 文本内任意处命中） | `"replace_*"` → 命中 replace_lines 那轮 |
| 普通词 | 子串（大小写不敏感） | `"entries"` → 原文含 entries 的轮 |

实现（`_re.split(r"[|\s,，、;；]+", q)` 拆词 → 逐 part「通配符走 `fnmatch`、普通词走子串」，任一命中即命中）。

### 二段降级：主域无命中 → 工具调用流水文本再搜（2026-11-16，commit 674891e）

#### 二段降级：主域无命中 → 工具调用流水文本再搜（2026-11-16，commit 674891e）

**动机（同轮用户）**：很多记忆点只存在于**工具入参**里（文件名/路径/命令行）——主域（summary + user + answer）永远搜不到，是召回率偏低的主因之一。

**语义**（src/session.py `recall()` 文本匹配路径，即上列第 2 档的追加降级）：

- 主域无命中 → 扩到**每轮工具调用的「名字 + 入参 JSON」**文本再搜一轮（**不含 result**——体积大且噪音高）；命中结果带明确标注（来自工具流水）
- 入参文本**惰性生成 + 按轮缓存**（`_tc_cache`），重复搜索不重复序列化
- 主域命中行为不变（不额外扩搜）；**无命中**时提示改用关键字 / `turn` 定位（新提示语）

`turn=N` 精确定位不经过任何检索，与此降级无关。

## tools 召回展示分层（2026-09-20 新参）

```
tools="brief"（默认）  → 整段 user+answer 原文 + 工具调用折叠成一行
                        「🔧 工具调用 N 个: edit, replace_lines」
tools="full"          → 展开每个工具调用的入参与结果（旧行为默认）
```

**关键语义——被折叠为结构摘要的轮也可整段召回**：上下文引擎的「全档满 → 折叠成结构摘要」只是**投影渲染侧**的事（见 [上下文引擎 · 分档投影](../architecture/context-engine.md#分档投影轮间)），那一轮的 user/answer **原文始终留在 `turns` 里**——`recall_turn` 命中后 `brief` 模式即以整段原文返回。工具调用的过程细节（入参/结果）在 brief 模式下不展开，按 `call_id` 用 `agent_query_tool_detail` 查（与 [多实例 · 会话诊断工具](multi-instance.md) 同族）。

`contains_reasoning=True` 时，`_format_turn_full` 对每步与回答附带 reasoning 文本。

## 与其他模块的关系

- **上下文引擎折叠**（[context-engine](../architecture/context-engine.md)）：折叠只影响投影，不影响 recall 召回原文——「折叠摘要 tail 优先级」节已有注记「逐字原文靠 recall 召回」
- **长期记忆 episodic**（[longterm-memory](longterm-memory.md)）：`recall_turn` 是**会话内历史轮**召回，与 LTM 的 episodic（跨会话经验记忆，三代演进）是两套系统，别混淆
- **多实例会话诊断**：`recall_turn` 是 [multi-instance](../architecture/multi-instance.md) 里读远端实例存档的四工具之一（list_tool_logs / get_tool_detail / recall_turn / cache_breakpoint）
- **memory_tools 位置**：引擎内置、不迁移（events.jsonl 是引擎 `_emit` 落盘，重放拿到数据 ≠ 独立，格式契约耦合更危险）

## 相关页面

- [上下文引擎](../architecture/context-engine.md)：折叠机制（原文仍在 turns 可 recall）与 `_folded_summary`
- [长期记忆](longterm-memory.md)：episodic 三代演进（另一套召回，别混淆）
- [多实例组网](../architecture/multi-instance.md)：会话诊断工具三件套归属
- [工具外置判别标准](../architecture/tool-externalization-criteria.md)：memory_tools 为何内置不迁
- [工作流引擎与钩子](../architecture/workflow-hooks.md)：before_turn_retrieval 的 retrieval-hint 输出契约与 `[第N轮]` 命中行展开指引（本轮配套改动）

