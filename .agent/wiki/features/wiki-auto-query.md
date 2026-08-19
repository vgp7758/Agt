# wiki_auto_query · before_turn 自动 wiki 检索

> 工作流：`.agent/workflows/wiki_auto_query.xml`（hook=before_turn，**默认关闭**），由 src/agent.py `_run_hooks` 每轮开头触发，按约定返回 `inject`+`result`。钩子内 LLM 走 `utility_client`（llm_calls.jsonl 中 scene=`hook:before_turn`，可观测见 [guides/ops](../guides/ops.md)）。

## 职责

每轮用户消息到达后、主循环开始前，自动在 repo-wiki（`.agent/wiki/`）检索相关知识并注入——主 Agent 不调 wiki_search 也能自带项目知识。默认关闭：启用改 meta `enabled=true`（或编辑器开启），避免开发期每轮都跑、白烧 utility 调用。

## 三档漏斗（成本递进，尽早短路）

```
用户消息
 → ① LLM1 意图识别：related? + 场景分类
 → related=False → 短路：零 wiki 搜索、零 LLM2，不注入
 → related=True  → ② wiki 搜索（取候选条目）
                 → ③ LLM2 精排（挑最相关内容）
                 → inject 注入（投影中的 before_turn hint，见 [context-engine](../architecture/context-engine.md)）
```

## 四场景全链路验证（2026-08-18 通过）

| 场景 | LLM1 意图 | wiki 搜索 | LLM2 精排 | inject |
|------|-----------|-----------|-----------|--------|
| 技术问题 | related=True | 执行 | 执行 | ✅ 相关知识注入 |
| 功能咨询 | related=True | 执行 | 执行 | ✅ 相关知识注入 |
| 闲聊 | related=False | 零 | 零 | 无 |
| 无关问题 | related=False | 零 | 零 | 无 |

验证要点：related=False 严格短路（只花一次 LLM1，零搜索零 LLM2）；inject 注入正确——技术/功能类带上下文，闲聊/无关不打扰主上下文。

## LLM2 输出纪律：只摘录、不生成（2026-08-20 修复）

**现象**：LLM2 精排会越界——不只挑+摘录 wiki 原文，还「结合用户 query 谈看法」：分析利弊、给改进建议。实测注入中推测约占 2/3 篇幅。

**自指案例（2026-08-20 当场抓获）**：注入内容引用的 wiki 原文明确写着 LLM2 定位是「**挑**最相关内容」，随后 LLM2 自己开始「**讲**内容」（"1. 有帮助的一面… 2. 有风险的一面… 3. 建议…"三段式生成）——约束与越界出现在同一次输出里。

**为什么「有点用」也站不住**（危害机制）：

| 问题 | 机制 |
|---|---|
| 职责错位 | 推测需要完整上下文（对话历史、任务状态、决策脉络）——主 Agent 有，检索 Agent 只见 system+query。它的推测必然是低配版的主 Agent 自己能做的分析 |
| 可信度污染 | 注入格式上无区分：wiki 原文（可信事实）与模型生成（可信度未知）混排，主 Agent 分不清哪些能当依据 |
| 幻觉通道 | query 与 wiki 匹配度低时，模型倾向「补全」出 wiki 里不存在的行为描述——知识库注入变幻觉库注入 |
| 信息密度倒挂 | 花钱买低配版推理；且「歪打正着」与「一本正经地错」格式上无法区分，错误注入直接进入下一轮推理前提 |

**核心原则**：检索钩子的价值全部来自**它读到的东西**（wiki 原文），而非**它对东西的想法**。通用原则提炼见 [workflow-hooks · 检索型钩子的输出纪律](../architecture/workflow-hooks.md#检索型钩子的输出纪律选择摘录禁止生成)。

**修复**（`130001` SYSTEM 追加硬约束，`/restart` 后生效）：

```
【输出纪律——硬约束】你只做「选择 + 摘录」：
- 每条命中必须是 wiki 页面【原文】的逐字/近逐字摘录（可截断、不可改写语义）；
- 严禁生成原文之外的内容：不要分析、不要建议、不要"结合问题谈谈看法"...
- 宁可少摘也不要补全：wiki 里没写的就让它不存在。
```

预期效果：inject 只剩「页面路径：原文摘录」行（与 retrieval-hint 同款干净格式）。

## 已知瑕疵（2026-08）

- **code1 在 related=False 时仍执行**：意图识别后、分支前的公共 code 节点未后移；开销可忽略，优化时把 code1 挪进 related=True 分支即可
- **utility 通道偶发 400**：utility_client 连续报 400 → `/restart` 重启进程即恢复（排障表见 [guides/ops](../guides/ops.md) 常见错误对照）

## 相关页面

- [工作流引擎与钩子](../architecture/workflow-hooks.md)：before_turn 约定返回 / utility_client / 检索型钩子输出纪律（通用原则）/ 同类钩子实例 py_auto_diag
- [上下文引擎](../architecture/context-engine.md)：inject 落在 current turn 的 before_turn hint
- [guides/ops](../guides/ops.md)：scene=hook:before_turn 观测、utility 400 处置
