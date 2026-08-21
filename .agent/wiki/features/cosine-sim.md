# cosine_sim · 语义余弦相似度工具

> 源码：`src/real_tools.py`（`cosine_sim` 函数 + `Tool` 注册）

## 职责

计算两段文本的语义余弦相似度（-1~1，越大越相关）。复用 `/rag` 页面配置的 embedding 模型（SentenceTransformer 或 API）分别向量化后做归一化点积。

**设计动机**：本地小模型（3B 级）直接做相关性打分区分度差（相关 0.1、无关 0.2、全 0.5），不如 embedding 模型向量化后余弦计算——后者实测 0.69 / 0.42 / 0.16，判别力碾压。本地 LLM 只负责提关键词，余弦裁决交给 embedding。

## 签名与用法

```python
cosine_sim(text1: str, text2: str) -> float
```

| 参数 | 说明 |
|------|------|
| `text1` | 第一段文本（批处理时接 loop-item=候选切片） |
| `text2` | 第二段文本（通常接 query 原文） |

**输出**：`raw`（number）——余弦相似度，-1~1，越大越相关。

**前置条件**：需先在 `/rag` 页面配置 embedding 模型（SentenceTransformer 或 API）。未配置时抛 `RuntimeError("RAG embedding 未配置")`。

## 关键实现

```python
def cosine_sim(text1: str, text2: str) -> float:
    from rag import get_rag
    rag = get_rag()
    if rag is None:
        raise RuntimeError("RAG embedding 未配置（/rag 页面配置后可用）")
    import numpy as np
    vecs = rag.embedder.encode([str(text1 or ""), str(text2 or "")],
                               normalize_embeddings=True, show_progress_bar=False)
    return round(float(np.dot(vecs[0], vecs[1])), 4)
```

- `normalize_embeddings=True`：encode 时即归一化，`np.dot` 直接得余弦值
- `round(..., 4)`：保留 4 位小数，便于阈值比较和展示

## 工作流批处理用法

在 wiki_auto_query v4 流水线中，`cosine_sim` 作为**批处理节点**逐片计算 query 与每个候选 wiki 行的相似度：

```
cosine_sim(text1={{loop_item}}, text2={{query}})
  → all_outputs.raw = [0.73, 0.69, 0.68, 0.42, 0.16, ...]
```

- `text1` 接 loop-item（候选切片，由 code 节点按行切分 wiki_search 结果）
- `text2` 接 query 原文（固定不变）
- `all_outputs.raw` 为 number 数组，喂给 [rerank_topk](../features/wiki-auto-query.md#v4-流水线2026-08-21) 子工作流做 top-k 选取

## 性能

| 指标 | 实测值 |
|------|--------|
| 单片计算耗时 | ~25ms（bge-small-zh，CPU） |
| 模型 | SentenceTransformer `BAAI/bge-small-zh-v1.5`（本地，零云端依赖） |
| 批处理 10 片总耗时 | ~250ms |

## 与其他模块的关系

- **[wiki_auto_query v4](../features/wiki-auto-query.md#v4-流水线2026-08-21)**：核心消费者——关键词检索后用 cosine_sim 做语义重排 + 阈值裁决
- **RAG 模块（`src/rag.py`）**：通过 `get_rag()` 获取已配置的 embedder，复用 `/rag` 页面的 embedding 配置，无需独立维护模型
- **`src/real_tools.py`**：工具注册入口，`Tool(cosine_sim, outputs=[...], param_descriptions={...})`

## 注意事项

- **依赖 RAG 配置**：未配置 embedding 时直接报错，不会静默返回 0
- **空文本安全**：`str(text1 or "")` 处理 None/空值，不会崩溃
- **非对称语义**：余弦相似度衡量语义方向而非内容长度，短 query 与长 wiki 行可正常比较
- **阈值经验值**：v4 流水线中 top1 < 0.5 判为不注入（实测无关问题 0.42 被拦下，相关问题 0.69+ 通过）

## 相关页面

- [wiki_auto_query · before_turn 自动 wiki 检索](../features/wiki-auto-query.md)：v4 流水线的核心消费者
- [工作流引擎与钩子](../architecture/workflow-hooks.md)：批处理节点 + 子工作流调用机制
- [系统总览](../architecture/overview.md)：real_tools.py 在能力层的位置
