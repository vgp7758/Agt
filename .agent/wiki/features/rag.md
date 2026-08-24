# RAG · 本地文档语义检索 + 共享 embedder 单例

> 核心 `src/rag.py`（LocalRAG）；注册外置件 `tools/builtin/rag_tools.py`（+随包副本 `src/assets/tools_builtin/`）；配置 `<workspace>/.agent/rag.json`，WebUI `/rag` 管理页。2026-08 commit 71e0b90 重构为**异步预热 + 惰性单例**架构。

## 职责

- **文档语义检索**：`rag_query(query, top_k)` 在本地文档库（faiss 向量索引 + chunks.db）做语义搜索，返回 `相对路径:起行-止行: 片段预览` 多行，供智能体回答涉及本地项目文档/设计/代码的问题
- **embedder 单例宿主**：`/rag` 页面配置的 embedding 模型（SentenceTransformer 或 API）全进程只加载一份，cosine_sim / session_vec / emb_probe 共享——省模型内存 + 共享 `_CachedEmbedder` LRU 缓存
- 向量 `.db`/索引由 rag 组自写自读——按[判别标准](../architecture/tool-externalization-criteria.md)属真限界上下文

## 惰性单例三件套（2026-08 重构核心）

| 入口 | 语义 |
|---|---|
| `ensure_rag(workspace=None, force=False)` | **线程安全惰性构建**：未构建且未尝试过（或 force）→ seed + from_config + set_rag；返回实例（配置缺失/加载失败 → None）。锁内幂等——预热中的并发调用等锁后拿已完成结果，不重复加载 |
| `preload_async(workspace)` | **后台预热**：daemon 线程跑 ensure_rag——模型加载（bge-small-zh 实测 22.8s，含 torch import）不阻塞启动 |
| `get_rag()` | 只读取单例（不构建）——session_vec `_build_embedder` 探测共享用 |

关键状态 `_init_attempted`：首次尝试后置 True，配置缺失场景 `rag_query` 不必每次重试加载；配置变更走 `init_rag → ensure_rag(force=True)` 强制重建（`src/chat.py` 的 `init_rag` 保留，专供 `/rag` 页面保存配置后 server.py 调用）。`make_rag_tools` 已删除。

## 预热时序（谁触发、谁等待）

```
agt_register()（tools/builtin/rag_tools.py，扫描注册时）
  → rag.preload_async()         ← 注册即异步预热（用户提议的设计）
build_agent()（src/chat.py）
  → preload_async(workspace)    ← 显式兜底外置件缺失场景（ensure 内部锁+attempted，双触发不重复加载）
rag_query / cosine_sim / emb_probe 被调用时
  → ensure_rag()                ← 惰性兜底：预热未完成则等锁拿结果（大概率早已就绪）
```

**启动时序收益**：以前 build_agent 同步加载 RAG 模型（每次启动白等 22.8s）且 session_vec 再建一份；现在 `/restart` 后立即就能对话，模型后台预热，session_vec 线程排队等它完成后共享同一 embedder。预热完成前的最初几轮 session recall 走子串匹配（vec_store=None 既有降级路径），随后自动升级语义召回。

## 共享 embedder（修双份内存旧疾）

`src/session_vec.py` `_build_embedder` 优先 `rag.get_rag().embedder`：

- **旧疾**：此前 LocalRAG 和 session_vec 各建一份 bge（双倍内存 + 双份等待）
- **现在**：同一对象（`is` 判定验证过）；RAG 未启用（enabled=false 但 session_index_enabled=true）或未预热完成时 session_vec 才自建——session 向量库可独立于文档 RAG 启用
- `chat.py` `_init_session_vec` 同改后台线程：先 ensure_rag 等 RAG 预热完成再建 SessionVectorStore

`src/real_tools.py`：

- `cosine_sim` 改走 `ensure_rag()`——启动初期首次调用可能等几秒（等预热）
- `emb_probe` 同改：**等预热完成再判定**——避免预热期误降级到关键词路径，且 5 分钟探测缓存把误判粘住

## 外置件：rag_tools.py（混合形态：注册外置 + 实现留框架）

```
外置的是【注册】与【预热触发】，不是复制实现：
- import rag 在主进程零成本（chat.py 已 import，sys.modules 直接命中）
- rag_query 函数本体留在框架 src/rag.py——与 cosine_sim/session_vec 共享单例 embedder
- agt_register()：触发 preload_async + 注册 rag_query（group=rag，desc 从 docstring 正确回退）
```

与首例 [fs_tools.py](glob-files.md)（纯函数整体外置）对照：rag 是「注册外置 + 实现留框架」——数据主权（向量库）在 rag 组可外置，但 embedder 是被多组共享的进程内单例（按判别标准属"纯进程内状态"），复制实现反而破坏共享。chat.py 不再 `_reg(make_rag_tools())`，rag_query 由外置件提供。外置体系全貌（五件两形态）见 [工具外置](tool-externalization.md)。

## 注意事项

- enabled=false：ensure_rag 快速返回 None + attempted 标记，二次调用幂等瞬时（实测 0.0ms）
- `/reload tools` 重扫外置件时 agt_register 再次触发 preload——幂等无害
- 外置件改动需同步随包副本 `src/assets/tools_builtin/rag_tools.py`
- 验证基线（10/10）：force 绕过 attempted、真实预热 22.8s（71 向量索引）、session_vec `is` 共享、cosine_sim 真算 0.6987、rag_query 命中 data-and-config.md

## 相关页面

- [cosine_sim](cosine-sim.md) —— 共享同一 embedder 的余弦相似度工具（同样走 ensure_rag）
- [长期记忆](longterm-memory.md) —— session_vec / episodic 语义召回共享 embedder，LRU 缓存层；ltm 外置同款单例哲学（ensure_ltm）
- [工具外置](tool-externalization.md) / [判别标准](../architecture/tool-externalization-criteria.md) —— 外置体系全貌与「注册外置 + 实现留框架」边界裁剪
- [glob_files](glob-files.md) —— 外置首例（纯函数整体外置模式）
