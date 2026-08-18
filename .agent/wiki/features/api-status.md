# /api/status 端点 · 实例运行时状态快照

> 源码：`src/server.py`（POST `/api/status`）。commit a922121。
> 用于跨实例诊断——从外部 HTTP 获取当前进程的运行时状态全貌。

## 职责

提供实例运行时状态的**只读快照**，供外部监控/调试工具消费。解决了此前 [运维与排障](../guides/ops.md) 中记录的"跨进程状态查询缺失"问题——旧版 `AgentRegistry`（`src/registry.py`）是进程内全局对象，外部无法通过 HTTP 获知运行时状态。

## 端点规格

| 属性 | 值 |
|------|-----|
| 方法 | POST |
| 路径 | `/api/status` |
| 位置 | `src/server.py`（FastAPI 路由） |
| 返回 | JSON 状态快照 |
| commit | a922121 |
| 生效 | 需 `/restart` 重启进程 |

## 返回结构

快照包含 **18 个顶层字段** + **3 个嵌套数组**，覆盖实例运行时的关键维度：

- 顶层字段：实例元信息、运行状态、配置摘要等 18 个字段
- 嵌套数组：3 个，分别对应不同维度的列表型状态（如 Agent 实例列表等）

> 具体字段名与结构以 `src/server.py` 中端点实现为准。

## 跨实例调用验证（2026-08-18）

已通过跨实例 POST `/api/status` 调用验证成功，确认：

- **registry 正确注册**：多实例环境下，各 Agent 实例（含子 Agent）在 registry 中正确注册，`caller_id` 路由可达
- **answer 路由正常**：子 Agent `_bg` 线程完成后，`push_message` 经 `AgentRegistry.get(caller_id)` 成功定位 caller，answer 入队 inbox
- **主 Agent 唤醒正常**：三层消费机制（`run()` 内 `pop_inbox` + `inbox_thread` 轮询 + `work_q` 触发新一轮）无丢消息，主 Agent 被正确唤醒

> 详见 [多 Agent 体系 · 跨实例验证](../architecture/multi-agent.md#跨实例验证2026-08-18)。

## 与其他模块的关系

| 模块 | 关系 |
|------|------|
| [AgentRegistry](../architecture/multi-agent.md#agentregistry-与-answer-路由修复2026-08)（`src/registry.py`） | 数据来源之一——registry 中各 Agent 的 id/name/status/caller_id 等只读字段被序列化进快照 |
| [server.py](../architecture/overview.md)（FastAPI 入口层） | 端点宿主，与 `/stats` `/memory` `/rag` `/wfeditor` 等页面路由并列 |
| [运维与排障](../guides/ops.md#可观测性) | 补全了可观测性的"跨进程实时查询"拼图，与 /stats 页（统计）、llm_calls.jsonl（日志）互补 |

## 注意事项

- **只读**：端点仅返回状态快照，不修改任何运行时状态
- **线程安全**：registry 非线程安全，读取时需加锁或取快照后序列化（实现中已处理）
- **生效方式**：commit a922121 已推送，但当前运行进程需 `/restart` 才能加载新代码
- **诊断场景**：多实例部署时，可逐个实例 POST `/api/status` 采集快照做横向对比
- **排障首选**：若再现"子 Agent 完成后主 Agent 未唤醒"症状，首先 POST `/api/status` 检查 registry 字段是否为 None——根因在入队端而非消费端

## 相关页面

- [运维与排障](../guides/ops.md) — 可观测性总览、常见错误、生命周期命令
- [多 Agent 体系](../architecture/multi-agent.md) — AgentRegistry 机制、三层消费机制与 registry 修复
- [系统总览](../architecture/overview.md) — 模块地图与数据流
