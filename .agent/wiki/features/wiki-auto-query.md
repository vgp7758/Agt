# wiki_auto_query · before_turn 自动 wiki 检索

> 工作流：`.agent/workflows/wiki_auto_query.xml`（hook=before_turn，**默认关闭**），由 src/agent.py `_run_hooks` 每轮开头触发，按约定返回 `inject`+`result`。钩子内 LLM 走 `utility_client`（llm_calls.jsonl 中 scene=`hook:before_turn`，可观测见 [guides/ops](../guides/ops.md)）。

## 职责

每轮用户消息到达后、主循环开始前，自动在 repo-wiki（`.agent/wiki/`）检索相关知识并注入——主 Agent 不调 wiki_search 也能自带项目知识。默认关闭：启用改 meta `enabled=true`（或编辑器开启），避免开发期每轮都跑、白烧 utility 调用。

## v4 流水线（2026-08-21，commit 952b801）

> **架构变更**：从「LLM 意图识别 + LLM 精排」改为「本地 LLM 提关键词 + embedding 余弦重排 + 阈值裁决」。核心洞察：本地 3B 模型做相关性打分区分度差（相关 0.1、无关 0.2、全 0.5），不如 embedding 模型余弦计算（0.69 / 0.42 / 0.16 判别力碾压）。本地 LLM 只负责提关键词。

```
user_message
  ├ ≤4字 → 短路 0s 不注入
  └ → extract_keywords (local-qwen @8080, 11s)
     → join: "|" 正则交替式          ← 逗号在 regex 模式是普通字符（坑①）
     → wiki_search(regex=True) → 按行切分命中行   ← 多行纯文本非 JSON 数组（坑②）
     → cosine_sim 批处理 (bge-small-zh, 25ms/片)   ← all_outputs.raw = number 数组
     → rerank_topk (subworkflow, k=3)
     → top1 < 0.5 → 不注入            ← 语义阈值裁决（关键词误命中兜底，坑③）
```

### 新增组件

| 组件 | 位置 | 职责 |
|------|------|------|
| [cosine_sim](../features/cosine-sim.md) | `src/real_tools.py` | 语义余弦相似度，复用 RAG embedding 模型 |
| `rerank_topk.xml` | `src/workflows/` | 子工作流：按分数降序取 top-k |
| `extract_keywords` | 工作流内 LLM 节点 | local-qwen@8080 提取关键词，免云端 token |

### 实测三场景

| 场景 | 耗时 | inject | 结果 |
|------|------|--------|------|
| 「上下文引擎的分档投影和毕业升档…」 | 16.3s | **True** | 3 条精准命中带分数（0.73/0.69/0.68）——home 导航行 + overview 两行 |
| 「今天天气怎么样」 | 18.3s | **False** | 关键词误命中 wiki 行，但 top1 余弦 <0.5 被阈值拦下 |
| 「hi」 | 0.0s | 短路 | — |

### 调试中抓到的三个真实坑（都已修进工作流）

1. **regex 逗号坑**：`wiki_search(regex=True)` 把 `"分档投影,毕业升档"` 当整词（逗号是普通字符）→ 恒 miss；改用 `|` 交替式才对
2. **多行文本坑**：wiki_search 返回多行纯文本不是 JSON 数组，FromJson 降级成字符串喂批处理 → 逐字符迭代（`(` 成了唯一候选）→ 改 code 节点按行切分
3. **误注入坑**：无关问题关键词可能撞上 wiki 词（"天气"命中某些行），topk 非空就注入会误报 → embedding 分数阈值做最终裁决

### 经济账

| 环节 | 成本 | 说明 |
|------|------|------|
| local-qwen@8080 提词 | 11s，**零云端 token** | 3B 小模型，免 API 费用 |
| bge-small-zh 余弦 | 25ms/片，**纯本地** | CPU 即可，零云端延迟依赖 |
| 阈值裁决 | ~0ms | 纯数值比较 |

整条检索流水线**零云端 token、零云端延迟依赖**，质量还比 3B 打分版高。

## 旧版三档漏斗（v3 及之前，已废弃）

> 保留作为历史参考。v4 用 embedding 余弦替代了 LLM 精排，用关键词提取替代了 LLM 意图识别。

```
用户消息
 → ① LLM1 意图识别：related? + 场景分类
 → related=False → 短路：零 wiki 搜索、零 LLM2，不注入
 → related=True  → ② wiki 搜索（取候选条目）
                 → ③ LLM2 精排（挑最相关内容）
                 → inject 注入
```

### LLM2 输出纪律：只摘录、不生成（2026-08-20 修复）

**现象**：LLM2 精排会越界——不只挑+摘录 wiki 原文，还「结合用户 query 谈看法」：分析利弊、给改进建议。实测注入中推测约占 2/3 篇幅。

**自指案例（2026-08-20 当场抓获）**：注入内容引用的 wiki 原文明确写着 LLM2 定位是「**挑**最相关内容」，随后 LLM2 自己开始「**讲**内容」（"1. 有帮助的一面… 2. 有风险的一面… 3. 建议…"三段式生成）——约束与越界出现在同一次输出里。

**核心原则**：检索钩子的价值全部来自**它读到的东西**（wiki 原文），而非**它对东西的想法**。通用原则提炼见 [workflow-hooks · 检索型钩子的输出纪律](../architecture/workflow-hooks.md#检索型钩子的输出纪律选择摘录禁止生成)。

**修复**（`130001` SYSTEM 追加硬约束）：LLM2 只做「选择 + 摘录」，严禁生成原文之外的内容。

> v4 中此问题已从架构上消除——精排由 cosine_sim 数值计算完成，不再有 LLM 生成环节。

## 已知瑕疵

- **utility 通道偶发 400**：utility_client 连续报 400 → `/restart` 重启进程即恢复（排障表见 [guides/ops](../guides/ops.md) 常见错误对照）
- **local-qwen@8080 依赖**：提词环节依赖本地 8080 端口的 qwen 服务，服务未启动时该环节失败

## 相关页面

- [cosine_sim · 语义余弦相似度工具](../features/cosine-sim.md)：v4 核心组件，复用 RAG embedding
- [长期记忆](../features/longterm-memory.md)：同族 before_turn 检索流水线（记忆版：local-qwen 提词 → search_memory → LLM 精排裁决，episodic 召回已并入）
- [工作流引擎与钩子](../architecture/workflow-hooks.md)：before_turn 约定返回 / utility_client / 检索型钩子输出纪律 / 批处理节点 + 子工作流调用
- [上下文引擎](../architecture/context-engine.md)：inject 落在 current turn 的 before_turn hint
- [guides/ops](../guides/ops.md)：scene=hook:before_turn 观测、utility 400 处置
