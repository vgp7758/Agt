# vector_hub · 本地向量栈服务（机器级单例 embedder：懒拉起 + 空闲自关）

> `src/vector_hub.py`（独立轻量服务）+ agt 侧 `src/rag.py` / `src/session_vec.py` 的 `HubEmbedder` 接入。2026-11-24 用户提案（内存治理施工牌 p_fd248b88 第 6/7 步）：**设置页配端口，给本机所有 agt 实例共用向量栈；实例调用时懒拉起并连接；所有连接实例离线一段时间后自动关闭**。

## 动机：多实例向量栈内存放大

每个 agt 实例各自加载一份 SentenceTransformer（bge-small-zh 级别 ≈560MB 提交内存）——本机 6 个实例就是 6 份。hub 化后：**1 份服务进程（≈400MB）**，实例侧只剩 HTTP 客户端，净省 2GB+ 提交额度。

## 服务形态（src/vector_hub.py）

独立轻量服务，stdlib `ThreadingHTTPServer`（同 asr_server 形态，零重依赖）：

| 项 | 说明 |
|---|---|
| `GET /health` | 探活 |
| `POST /embed` | `{"texts": [...]}` → `{"vectors": [[...]]}` |
| 默认模型 | bge-small-zh-v1.5（sentence_transformers） |
| 启动参数 | `--port` / `--model` / `--idle-timeout` |
| 空闲自关 | 超过 `idle_timeout`（默认 600s）无任何请求即自行退出 |

## 懒拉起与连接（agt 侧 HubEmbedder）

`settings.vector_hub_port > 0` 时，embedder 获取点（`rag.py` / `session_vec.py`）首次调用改走 `HubEmbedder`：

1. **探活** `/health`——通了直接连
2. 不通 → **subprocess 拉起** hub 进程 → 轮询就绪（≤120s；模型冷载实测 ~40s）
3. **多实例并发拉起竞态**：靠端口占用自然互斥——先到者绑端口成功，后到者拉起失败/等待后转探活复用
4. **回退保底**：hub 不可达/超时 → 回退本地 SentenceTransformer（检索不中断，代价是那份内存临时回来）

## 空闲自关的语义（「所有连接实例离线」的代理）

HTTP 无状态，hub 无法直接感知「还有没有实例连着」——用**最近请求时间**代理：

- 还有实例在用 → 一定周期性有 embed 请求 → 不会触发自关
- 所有实例都离线 → 请求停 → 超过 `idle_timeout` 无请求 → hub 自行退出
- 下次任意实例需要时再走懒拉起——**生命周期完全跟随需求**，无需人工管理

## 设置页

⚙ 设置 → RAG 文档库 tab 新增**「本地向量栈服务端口」**：

- 默认 **8097**；**0 = 禁用**（走各实例本地加载，即旧行为）
- 写入 `settings.json` 的 `vector_hub_port`

## 注意事项

- **模型路径由首个拉起它的实例按其 rag 配置传入**——本机各实例的 RAG embedding 配置应保持一致，否则后接入实例拿到的向量与本地加载不一致
- hub 是**机器级单例**（端口全局唯一），跨 repo 的实例都连同一个——与 [RAG 共享 embedder](rag.md)（进程内单例）是两层：hub 把「进程内共享」再升到「机器级共享」
- 单实例跑也可启用（自身懒拉起，退出后 hub 空闲自关），但收益主要在多实例场景
- hub 进程独立于任何 agt 实例的生命周期；实例 `/restart` 不影响 hub

## 状态

✅ **已随 v0.35.17 发布**（2026-11-27；内存治理施工牌 p_fd248b88 全步落地——toollog 进程内共享 + 向量栈懒加载 + vector_hub 服务 / HubEmbedder 接入 / 设置页端口同批上线）。pip ≥ 0.35.17 或本机 `/restart` 生效；设置页 RAG tab「本地向量栈服务端口」默认 8097、0=禁用；hub 不可达/超时自动回退本地 SentenceTransformer。

## 相关页面

- [RAG](rag.md) —— embedder 进程内单例宿主；HubEmbedder 接入点（`_build_embedder` / `ensure_rag`）
- [toollog · 进程内共享](toollog.md) —— 同一施工牌的另一半：进程内 ToolLog 共享
- [长期记忆](longterm-memory.md) —— session_vec 语义召回共享 embedder（hub 化后经 HubEmbedder 取向量）
