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

## 已知瑕疵（2026-08）

- **code1 在 related=False 时仍执行**：意图识别后、分支前的公共 code 节点未后移；开销可忽略，优化时把 code1 挪进 related=True 分支即可
- **utility 通道偶发 400**：utility_client 连续报 400 → `/restart` 重启进程即恢复（排障表见 [guides/ops](../guides/ops.md) 常见错误对照）

## 相关页面

- [工作流引擎与钩子](../architecture/workflow-hooks.md)：before_turn 约定返回 / utility_client / 同类钩子实例 py_auto_diag
- [上下文引擎](../architecture/context-engine.md)：inject 落在 current turn 的 before_turn hint
- [guides/ops](../guides/ops.md)：scene=hook:before_turn 观测、utility 400 处置
