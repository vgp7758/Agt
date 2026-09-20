# recall_turn · 会话历史轮次召回工具（memory_tools · 引擎内置）

> 工具面 `src/memory_tools.py` 注册 `recall_turn`；实现核心在 `src/session.py` 的 `recall()` + `_format_turn_full()`。属引擎内置（读 `events.jsonl`/turns，是[工具外置判别标准](../architecture/tool-externalization-criteria.md)下的「引擎写、工具读——可观测性出口」，不迁外置）。

## 职责与签名

按关键词/语义在**【全部】历史轮次**里搜索，返回匹配轮的上下文——当早期对话滑出上下文窗口、或忘了之前某轮的具体细节时，用它把那一轮捞回来。

```python
recall_turn(query: str, contains_reasoning: bool = False, tools: str = "brief") -> str
```

| 参数 | 默认 | 语义 |
|------|------|------|
| `query` | — | 关键词/短句；支持多关键词 OR 与通配符（见下） |
| `contains_reasoning` | `False` | `False` 不含思考过程；`True` 一并带上每步 reasoning 与回答的 reasoning |
| `tools` | `"brief"` | **召回展示分层**（见下） |

## 检索策略（自动降级，2026-09-20，commit a6e3290）

1. **配了 embed 模型**（`self.vec_store` 非 None，共享 [RAG 惰性单例](rag.md)的 embedder）→ **语义召回 top-K 轮**：换说法也能搜到
2. **否则**（没配 embed，或语义无结果）→ **summary + user + answer 子串/通配匹配**：

### query 的三种写法

| 写法 | 语义 | 示例 |
|------|------|------|
| `a \| b`（`\|` / 空格 / 中英文逗号 / 顿号 / 分号分隔） | **OR**——任一命中即命中 | `"replace_lines \| 封面"` → 命中两轮 |
| `replace_*`（含 `* ? []`） | **通配符**（fnmatch；前后包 `*` = 文本内任意处命中） | `"replace_*"` → 命中 replace_lines 那轮 |
| 普通词 | 子串（大小写不敏感） | `"entries"` → 原文含 entries 的轮 |

实现（`_re.split(r"[|\s,，、;；]+", q)` 拆词 → 逐 part「通配符走 `fnmatch`、普通词走子串」，任一命中即命中）。

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