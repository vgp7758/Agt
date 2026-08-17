# 系统总览

> 源码地图 + 一轮对话的完整数据流。模块细节见 [docs/architecture/](../../../docs/architecture/)。

## 模块地图（按职责分层）

```
入口层    chat.py（CLI main / Web web_main，work_q 驱动）
          server.py（FastAPI+WS，/memory /stats /rag /wfeditor 页面路由）
─────────────────────────────────────────────────
引擎层    agent.py（ReAct 循环、事件流 _emit、并行工具调度、钩子执行）
          llm_client.py（多模型回退链、token 轮换、DSML 兜底、usage 归一化）
          session.py（分层上下文引擎、事件流持久化、分档投影）
─────────────────────────────────────────────────
能力层    real_tools.py（130+ 内置工具）  tools.py（Tool/Toolbox schema）
          mcp_client.py  lsp_manager.py  workflow.py + workflow_xml.py
          multiagent.py（子 Agent）  registry.py（团队注册表）
─────────────────────────────────────────────────
支撑层    longterm_memory.py  plan_tools.py  spec_tools.py  survey_tools.py
          background.py（后台服务）  restart_watchdog.py  updater.py
```

## 一轮对话的数据流

```
用户输入（CLI input 线程 / WebUI WS）
  → work_q（("user", text)）
  → _worker 线程取出 → agent.run(text)
      ① start_turn（中断轮防御性收尾归档）
      ② 快照 workspace（回溯检查点）
      ③ before_turn 钩子（检索工作流：会话历史+episodic→精排→注入）
      ④ ReAct 循环（每步）：
           _chat_msgs() = session.messages_for_llm()（投影）+ hook 旁注
           llm.chat()（回退链；scene=react）
           tool_calls → before_tool 钩子 → 工具执行（并行/文件锁串行）
                     → after_tool 钩子（mtime 快照 diff → changed_files → py_auto_diag）
           工具结果 _materialize（图片落盘 <img> 标签）
      ⑤ answer → finish_turn（落盘+recap 异步生成）
  → 事件流 _emit → event_q（CLI _render_loop）/ broadcast（WebUI 多客户端）
```

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
