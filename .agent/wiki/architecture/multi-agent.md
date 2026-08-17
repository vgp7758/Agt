# 多 Agent 体系

> src/multiagent.py + registry.py。docs/architecture/06 有基础版，本页收录 2026-08 全部演进（全异步/reuse/复活/assembly/system_append/registry 修复）。

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

## AgentRegistry 与 answer 路由修复（2026-08）

### 旧版根因

旧版代码无 registry 机制。子 Agent 在后台 `_bg` 线程中运行，完成后调用 `push_message` 将 answer 路由回调用者 inbox。但 `push_message` 内部需要查 `agent.registry` 来定位 caller，旧版 `agent.registry` 为 `None`，导致 answer **未被入队** caller 的 inbox——主 Agent 永远收不到子 Agent 的回复，表现为"调了 vision 子 agent 后主 agent 未被唤醒"。

### 修复

引入 `AgentRegistry`（`src/registry.py`）：每个 Agent 实例创建时注册到全局 registry，`push_message` 通过 registry 按 `caller_id` 查找目标 Agent 并将 answer 入其 inbox。`_bg` 线程完成时 registry 不再为 None，answer 正常路由，主 Agent 下轮自动激活。

### 关键链路

```
子 Agent _bg 线程 finish
  → push_message(caller_id, answer)
  → AgentRegistry.get(caller_id)   ← 旧版此处返回 None → 跳过
  → caller.inbox.put(answer)       ← 修复后正常入队
  → 主 Agent 下轮 _worker 取出 inbox → 激活
```

### 注意事项

- registry 是进程内全局，**不跨进程**——外部进程无法通过 API 查询运行时 registry 状态（见 [运维与排障](../guides/ops.md#跨进程状态查询缺失)）
- 子 Agent 工具闭包重绑自身时也依赖 registry 确认身份，旧版同样受影响

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
- 派视觉任务务必在 prompt 带 `[图片 文件名，你无法直接查看；如需理解其内容请委托视觉子 agent：agent_prompt("vision", "请描述 <img>文件名</img> 的内容")]`（vision.md description 有提示）
