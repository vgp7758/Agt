# spec 工具集 · explore_subagent 同步探索（≠ explorer 子 Agent）

> `src/spec_tools.py`（支撑层模块）。spec 施工流程的工具集：流程第一步「探索（可选）」由 explore_subagent 承担，产出喂给后续 create_spec 等环节。2026-08 澄清身份 + 修复预算残留（commit eafed25）。

## explore_subagent vs explorer —— 名字相近，两回事

| | **explorer**（声明式子 Agent） | **explore_subagent**（spec 工具） |
|---|---|---|
| 身份 | `.agent/agents/` 里的声明，`agent_prompt("explorer", ...)` 派活 | `spec_tools.py` 里的**同步工具**，代码内嵌 system |
| 同步性 | **异步**——answer 入 inbox 唤醒主 Agent | **同步阻塞**——报告作为工具结果返回，主 Agent 等它才能走下一步 |
| 用途 | 通用只读搜索定位 | **spec 流程前置探索**：制定施工方案前，同一步并行派 N 个摸不同模块（如「摸清 session.py 的注入点」），汇总后喂 create_spec |
| 团队可见 | registry 注册（看板可见） | 不注册——一次性实例用完即弃（看板里从没见过它的原因） |
| 工具面 | 声明自由 | 硬编码只读白名单（read_file/grep/list_dir/find_function 等 9 个） |

## token_budget 残留修复（2026-08，commit eafed25）

早期「解除子 Agent 预算」改造只改了 `agent_prompt` 路径（→0），explore_subagent 的独立构造路径漏掉仍为 20000。已对齐：

```python
sub = SubAgent(name, model_name, system, Toolbox(*chosen), on_event=agent.on_event,
               max_steps=12, token_budget=0)   # 预算解除（与 agent_prompt 路径对齐）
```

`max_steps=12` 保留——探索是有界任务，步数封顶防跑飞。**教训：同一语义改动要核对所有构造路径。**

## on_event 与串台历史

构造 SubAgent 时传 `on_event=agent.on_event`——同步子 Agent 的 answer 事件直接流入主事件流，是 2026-08-21 answer 串台 bug 的源头之一；现靠事件统一 `agent_id` 打标 + 前端分页渲染兜住（[多 Agent 体系 · 串台修复](../architecture/multi-agent.md#事件流-agent_id-打标与-webui-串台修复2026-08-21commit-ba0940b)）。

## 注意事项

- 同步语义：调用期间主 Agent 阻塞等报告——适合「先摸清再定方案」的短程有界探索；长程调研应走 `agent_prompt` 异步派活（answer 走 inbox，见 [多 Agent 体系 · 三层消费机制](../architecture/multi-agent.md)）
- 白名单只读：explore_subagent 不能改文件，产出仅为结构化发现报告
- 它与 update_wiki 是仅有的两个「同步调用子 Agent」入口，事件流行为见串台修复记录

## 相关页面

- [多 Agent 体系](../architecture/multi-agent.md) —— SubAgent 构造 / registry / 串台修复
- [气泡交互 · answer 多 Agent 分页](bubble-interaction.md) —— agent_id 打标的前端兜底
- [系统总览](../architecture/overview.md) —— spec_tools.py 属支撑层
