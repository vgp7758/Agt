# 多 Agent 体系

> src/multiagent.py + registry.py。docs/architecture/06 有基础版，本页收录 2026-08 全部演进（全异步/reuse/复活/assembly/system_append）。

## 声明与生命周期

子 Agent 声明：`.agent/agents/<name>.md`（frontmatter：name/description/model/tools + assembly/system_append DSL）。

```
agent_prompt(name, prompt, tools?, agent_id?, reuse?, assembly?)
  ├─ reuse=true 且有同名空闲活实例 → 直接派活（沿用 agent_id/session）
  ├─ reuse=true 无活实例但有同名历史条目 → 复活（读 md + Session.load 磁盘历史）
  └─ 否则新建（session 嵌套 主session/agents/<id>/，meta.json 记 _agent_meta）
全异步：立即返回；完成后按 caller_id 路由 answer 入调用者 inbox（下轮自动激活）
要结果才继续 → wait_subagents(agent_ids)
```

- 主 Agent id=`_main_`；registry 是唯一事实源（团队看板/recap/路由都读它）
- `_agent_meta`（agent_id/name/model/task/caller_id/recap/status）无条件写子 meta.json → 读档 `_restore_subagents` 恢复团队
- 子 Agent 的通信/会话工具**重绑自身**（继承的闭包绑主 Agent，会查错 session）

## 通信（agent 间）

| 工具 | 语义 | 落盘 |
|------|------|------|
| agent_ask | 无状态询问（对方上下文快照+问题→LLM→回你） | 否 |
| agent_notify | 有状态提示（入对方 inbox，等效用户插话） | 是 |
| agent_query_events / _tool_detail | 只读查对方轮次/工具调用详情（历史 Agent lazy load） | — |
| list_team | 团队清单（exclude 自己） | — |

## recap（每轮一句话总结）

finish_turn 后 daemon 线程异步生成（utility_client，scene=recap）——不进自己上下文，但显示在队友的 teammates_block。子 Agent 完成后 recap 写入 `_agent_meta` 随 meta.json 持久化。

## assembly DSL（上下文装配配方）

```yaml
assembly:
  - system          # 必装
  - rules|optional  # 可关：AGENTS.md/rules/skills
  - history|optional
  - user_message    # 必装
  - hooks|optional  # 关=整个 Agent 不跑钩子工作流
  - steps           # 必装
  - tail|optional   # 时间/计划/召回/队友看板
```

语义=**只装列出的段**；agent_prompt 可传 `assembly="rules=off,history=off"` 临时覆盖（仅 optional 段）。示范：vision=[system,user_message,steps]（纯函数型），wiki-updater 无 hooks/tail。

## system_append DSL（SYSTEM 动态追加）

```yaml
system_append:
  - workflow: wiki_tree_brief   # 执行工作流，result 追加到 SYSTEM 后（动态上下文）
  - text: "\n以上为知识库结构。"  # 静态文本
```

新建/复活时展开固化（reuse 不重算）；工作流入参注入 {prompt, agent_id}；LLM 节点走 utility_client；失败跳过保底 md 正文。

## 实践建议

- 高频反复派活（看图/检查）→ `reuse=True`：上下文只含当前轮，token 不随复用次数增长
- 长报告类子 Agent answer 上限 4000 字，超长指引用 `agent_query_events(id, 1)` 取全文
- 派视觉任务务必在 prompt 带 `<img>文件名</img>`（vision.md description 有提示）
