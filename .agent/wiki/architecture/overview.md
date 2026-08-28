# 系统总览

> 源码地图 + 一轮对话的完整数据流。模块细节见 [docs/architecture/](../../../docs/architecture/)。

## 模块地图（按职责分层）

```
入口层    chat.py（CLI main / Web web_main，work_q 驱动）
          server.py（FastAPI+WS，/memory /stats /rag /wfeditor /wf/monitor /api/status /api/tool/exec 路由）
─────────────────────────────────────────────────
引擎层    agent.py（ReAct 循环、事件流 _emit、工具执行统一入口 _exec_tool（server_id 远程路由）、并行工具调度、钩子执行、消息队列）
          llm_client.py（多模型回退链、token 轮换、DSML 兜底、scene/turn/step 调用埋点、usage 归一化）
          session.py（分层上下文引擎、事件流持久化、分档投影）
─────────────────────────────────────────────────
能力层    real_tools.py（130+ 内置工具，含 diff_files 文件级 Myers Diff，见 [features/diff-files](../features/diff-files.md))  tools.py（Tool/Toolbox schema）
          mcp_client.py  lsp_manager.py  workflow.py + workflow_xml.py（含 run registry 运行观测）
          multiagent.py（子 Agent）  registry.py（团队注册表）
          remote_tools.py（多实例组网：server_id 路由 + 远程连接管理，见 [architecture/multi-instance](multi-instance.md)）
─────────────────────────────────────────────────
支撑层    longterm_memory.py  plan_tools.py  spec_tools.py  survey_tools.py
          background.py（后台服务）  restart_watchdog.py  updater.py
```

longterm_memory.py（长期记忆三类 + episodic 召回流水线）详见 [长期记忆](../features/longterm-memory.md)。

## 一轮对话的数据流

```
用户输入（CLI input 线程 / WebUI WS）
  → work_q（("user", text)）
  → _worker 线程取出 → agent.run(text)
      ① start_turn（中断轮防御性收尾归档）
      ② 快照 workspace（回溯检查点）
      ③ before_turn 钩子（检索工作流：会话历史+episodic→精排→注入）
         → 同一 hook 多工作流并行执行（ThreadPoolExecutor + as_completed，等待全部完成）
         → 每个钩子执行注册 run_id（run registry），UI「执行中」行可点击实时观测
      ④ ReAct 循环（每步）：
           _chat_msgs() = session.messages_for_llm()（投影）+ hook 旁注
           llm.chat()（回退链；scene=react·{agent_id} + turn/step 埋点，与投影转储文件名同源）
           tool_calls → before_tool 钩子 → 工具执行（并行/文件锁串行）
                     → wf_* 工具调用同样注册 run（可观测）
                     → after_tool 钩子（mtime 快照 diff → changed_files → py_auto_diag）
           工具结果 _materialize（图片落盘 <img> 标签）
      ⑤ answer → finish_turn（落盘+recap 异步生成）
         → before_answer 钩子（async 后台线程，如 wiki_auto_maintenance；带 run_id 可观测）
         → 检查 inbox（后台队列）→ 非空则开新一轮（background_trigger）
         → 【新增】inbox 空 → 检查 pending_messages（插话队列）→ 非空则开新一轮（background_trigger·user_insert）
  → 事件流 _emit → event_q（CLI _render_loop）/ broadcast（WebUI 多客户端）
```

**插话流**（`src/agent.py` + `src/static/index.html`）：

```
用户中途插话
  → 步边界检查 → 赶上则 message_injected 当步可见 ✓
  → 赶不上（answer 生成中）→ 暂存 pending_messages
      → answer 完成 → pop_inbox 检查 inbox（空）
      → pending_messages 非空 → 立即开新一轮（background_trigger·user_insert）
```

详见 [用户交互 · 插话机制与消息路由](../features/user-interaction.md)。

## 关键设计决策（为什么长这样）

| 决策 | 理由 |
|------|------|
| 事件流 _emit 而非直接 print | CLI/Web/子 Agent 各取所需，渲染与逻辑解耦 |
| work_q 单 worker 串行 | 天然避免两个 run 并发写 session（子 Agent 各有独立 session 无冲突） |
| Session 挂 provider 回调 | 上下文组装向 Agent 要运行时状态（计划/队友/记忆），两者解耦 |
| 全异步子 Agent + inbox 路由 | caller_id 绑定，完成后自动激活调用者（复用现有 background 链路） |
| XML 工作流格式 | CDATA 免转义，LLM 写起来远比 JSON 不易出错 |
| mtime 快照 diff 检测副作用 | 按工具名猜测不可靠（run_python 绕过）；gitignore+嵌套仓库剪枝后 124 文件 51ms |
| 事件流 events.jsonl append-only | 读档重放即恢复（含中断轮兜底），无需手写序列化 |
| before_turn 钩子并行执行 | 同一 hook 多工作流并发跑，ThreadPoolExecutor + as_completed 确保全部完成才进入主循环，避免「一个钩子未完成就开始第1步」 |
| 插话队列（pending_messages）+ 后台触发 | answer 完成后检查 inbox + pending_messages，确保插话不滞留，自动开新一轮处理（2026-08-19 修复） |
| UI 并行钩子状态 Map 索引 | 避免多个并行钩子的「执行中」状态互相覆盖，按 hook::name 独立跟踪（2026-08-19 修复） |
| 工作流执行 run registry（进程内注册表） | 钩子/wf_* 工具执行的节点级实时观测：内存 dict + 锁，最近 50 次——不落盘、零成本，观测页轮询即得（2026-08-20 新） |
| 多客户端 target 路由 | 每客户端记录正在交互的 agent_id，事件按 agent_id 过滤分发——多页签各与不同 Agent 交互互不串台、同 Agent 的多客户端仍组播（2026-08，commit 30ac45b） |

## 相关页面

- [长期记忆](../features/longterm-memory.md) — 三类记忆注入 / episodic 召回流水线（before_turn 检索工作流）/ `/memory` 管理页
- [工作流引擎与钩子](../architecture/workflow-hooks.md)：before_turn 并行执行 / async 钩子 / 运行观测 / 快照检测闭环
- [工作流运行观测](../features/wf-monitor.md)：run registry + /wf/monitor 实时节点轨迹
- [用户交互 · 插话机制与消息路由](../features/user-interaction.md)：插话全生命周期 / 后台触发 / 并行钩子 UI 修复
- [多 Agent 体系](../architecture/multi-agent.md)：inbox 路由 / 三层消费机制 / 子 Agent 唤醒
- [多实例组网](multi-instance.md)：server_id 工具路由 / /api/tool/exec 工具级直执行 / 远程连接管理（与 WS 消息驱动的"脑"互补的"手"）
- [上下文引擎与缓存优化](../architecture/context-engine.md)：投影装配 / 分档投影 / 前缀缓存三层优化
- [wiki_auto_query · before_turn 自动 wiki 检索](../features/wiki-auto-query.md)：before_turn 典型实例（默认关闭）

