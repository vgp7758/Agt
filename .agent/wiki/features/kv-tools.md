# kv_cache 三件套 · 应用级 KV 结果缓存（外置件 kv_tools.py）

> `tools/builtin/kv_tools.py`——纯函数批第五批外置（2026-08，commit 17312eb 新建）；2026-09-13（commit `1c1c944`）增 **kv_cache_claim 原子互斥**（用户实测竞态修复）。外置体系定位见 [工具外置](tool-externalization.md)，判别标准见 [判别标准](../architecture/tool-externalization-criteria.md)。

## 职责

同输入结果确定的 LLM 调用（如关键词提取）做 memoization——同轮多个 before_turn 工作流共用一次提取，避免重复打本地单并发模型。缓存键 = namespace + 内容 sha1；**namespace 兼作版本号**：改提示词/换模型换 namespace 即整体失效。重启清空 = 结果缓存语义（丢失 = 下次重新计算，无正确性影响）。

## 三件套

| 工具 | 语义 |
|---|---|
| `kv_cache_read(namespace, key)` | 读缓存 → `{hit, value}` |
| `kv_cache_write(namespace, key, value)` | 写回（同时清 pending 判据） |
| `kv_cache_claim(namespace, key)` | **原子互斥提取（2026-09-13）**——三态单次判定，见下节 |

状态 `_KV_CACHE` 是进程级 dict（自写自读，纯工具组状态）——随外置件走；改完本文件用 `/reload tools` 热加载（**/reload 会重建模块，缓存随旧模块丢弃**）。

## 竞态修复：kv_cache_claim 原子互斥（2026-09-13，commit 1c1c944，用户实测抓到）

**用户猜想（完全命中）**：extract_keywords 工作流同时执行时偶发一个失败——钩子上的两个工作流两个线程同时执行，都没从 kv_cache 拿到 pending，同时发请求，本地模型单并发只能处理一个，另一个失败。

**旧互斥的两节点窗口**：`cache_read（miss 判定）→ write_pending（占位）→ LLM → write 结果`——read 与 write_pending 分处两个工作流节点、隔着引擎调度**非原子**：

```
T0  线程A（wiki_auto_query 的 extract_keywords）cache_read → miss
T1  线程B（before_turn_retrieval 的 extract_keywords）cache_read → miss   ← A 还没走到 write_pending！
T2  A: write pending            T3  B: write pending（覆盖，无感）
T4  A: LLM ────────────────────┬→ 双双打 local 模型（单并发）
T5  B: LLM ────────────────────┘   → 一个成功一个失败 ✗
```

同 hook 双工作流由 `ThreadPoolExecutor` 并发起跑（见 [workflow-hooks · before_turn 并行](../architecture/workflow-hooks.md)），read 几乎同毫秒——窗口虽小但每轮都开。还有个更隐蔽的**后遗症**：失败方 LLM 挂了不写回 → `pending` 永久残留 → 后续轮同一条消息傻等 90×2s 超时。

**修复：claim 把 check-and-set 收进进程级 `_KV_LOCK`**，一次调用三态返回：

| 返回 | 语义 | 调用方动作 |
|---|---|---|
| `{state: hit, value}` | 有真实值 | 直接用 |
| `{state: pending}` | 有 pending 且未超 TTL（150s） | 走等待循环轮询 |
| `{state: claimed}` | 锁内已原子占位 | **你就是提取方**——独占跑 LLM → `kv_cache_write` 写回 |

**pending TTL 150s**（< 等待循环 180s 兜底）：治残留——提取方死亡后 pending 过期，下一个 claim 方自动接管；等待方 180s 空表兜底语义不变。

**消费方接线**：`extract_keywords` 子工作流删 write_pending 节点，selector 改三分支路由（hit 直取 / pending 等待 / claimed 进提取链）。

**验证**：

- **单元 5/5**——8 线程 barrier 同时 claim 同一 key → **恰好 1 个 claimed / 7 个 pending**；TTL 过期接管；write 清 pending 判据
- **debug e2e 两路径**——第一次（冷）claimed → local-lfm-vl 真实提取写回 ✓；第二次同输入 hit 秒回（6 节点，不碰 LLM）✓
- `/reload tools` 热加载即时生效（53 工具，+1）——下一轮 before_turn 双钩子并发即真实检验

## 注意事项

- **/reload tools 清缓存**——模块重建 `_KV_CACHE` 随旧模块丢弃（结果缓存语义，可接受）
- 只适合「同输入必同结果」的调用；带随机性/时效性的 LLM 调用不要进缓存
- 并发正确性依赖 claim：**不要再手写 read → write_pending 两节点互斥**——竞态窗口恰在这两节点之间（本页教训）

## 与其他模块的关系

- **消费方**：`extract_keywords` 子工作流（wiki_auto_query / before_turn_retrieval 两个 before_turn 钩子工作流各自内嵌调用）
- **并发来源**：同 hook 多工作流 `ThreadPoolExecutor` 并行（[workflow-hooks](../architecture/workflow-hooks.md)）
- **外置件体系**：[工具外置](tool-externalization.md)（纯函数批第五批，`_KV_CACHE` 状态随外置件走）

## 相关页面

- [工具外置](tool-externalization.md) —— 外置件清单与装配
- [工作流引擎与钩子](../architecture/workflow-hooks.md) —— before_turn 并行执行（竞态产地）
- [wiki_auto_query](wiki-auto-query.md) —— before_turn 检索钩子（extract_keywords 所在链路）
