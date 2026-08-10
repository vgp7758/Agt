# 核心引擎架构设计

> 对应源码：`src/agent.py` · `src/chat.py` · `src/llm_client.py` · `src/config.py` · `src/prompts.py`

---

## 1. 模块职责

| 文件 | 行数 | 职责 |
|------|------|------|
| `agent.py` | 1087 | ReAct 主循环、事件流、工具调度（并行/串行）、软 token 预算、Ctrl+C 打断、自主模式、工作流生命周期钩子 |
| `chat.py` | 628 | CLI/Web 双入口、work_q 队列驱动模型、_worker 线程、_render_loop 渲染循环 |
| `llm_client.py` | 506 | 多模型统一封装：热切换、回退链、token 轮流、DSML 兜底解析、截断重试、指数退避 |
| `config.py` | 351 | 模型配置加载（~/.agt/models.json 优先）、运行时设置、RAG 配置分层持久化 |
| `prompts.py` | 80 | 人设模板 + 动态环境上下文注入（日期/用户名） |

---

## 2. 核心数据结构

### 2.1 Agent 类

```python
class Agent:
    # 核心组件
    self.tools: Toolbox          # 工具箱（含 MCP 发现的、工作流注册的）
    self.llm: LLMClient           # 多模型客户端（Agent 与 Session 共用同一实例）
    self.session: Session         # 会话（上下文引擎）
    self.ltm: LongTermMemory      # 长期记忆（per-repo，跨 session）

    # 运行时状态
    self.on_event: Callable       # 事件回调（Web=broadcast，CLI=None）
    self.verbose: bool            # CLI 模式时 _print_event 复刻控制台格式
    self.cumulative_tokens: int   # 累计 token（软预算用）
    self._stop_flag: bool         # Ctrl+C 打断标志

    # 自主模式
    self.autonomous_mode: bool
    self.autonomous_end_time: datetime
    self.pending_messages: list[str]  # 忙时插话队列

    # 后台服务 + 定时调度
    self.services: ServiceManager
    self.scheduler: Scheduler
    self.inbox: deque             # 后台消息队列（producer→inbox→串行消费→run）
```

### 2.2 事件流

Agent 的所有输出抽象成结构化事件 `_emit(event: dict)`：

```
_emit({"type": "user", "text": "..."})           # 用户消息
_emit({"type": "step", "n": 1, "tokens": 500})   # 步骤开始
_emit({"type": "thinking", "text": "..."})       # 推理过程
_emit({"type": "tool_call", "name": "edit", ...})# 工具调用
_emit({"type": "tool_result", "name": "edit", "result": "..."})
_emit({"type": "answer", "text": "..."})          # 最终回答
_emit({"type": "interrupted"})                    # 被打断
_emit({"type": "_done"})                          # 一个 work_q 项处理完
```

**双通道分发**：

```
_emit(event)
    ├── on_event(event)     → broadcast → 所有 WS 客户端（Web 模式）
    └── _print_event(event)  → print()    → 终端（CLI verbose 模式）
```

---

## 3. ReAct 主循环

`agent.run()` 是核心入口，用**循环替代递归**（自主继续走下一轮迭代而非 self.run() 递归）：

```
用户消息
    │
    ▼
┌──────────────────────────────────────────┐
│  start_turn(user_message)                 │
│  ↓                                        │
│  before_turn 钩子（工作流预检索/意图识别）  │
│  ↓                                        │
│  快照检查点（snapshot_manager.snapshot）   │
│  ↓                                        │
│  refresh_workflow_tools（每轮扫描注册）     │
│  ↓                                        │
│  ┌─── for step_num in 1..max_steps ───┐   │
│  │  检查 _stop_flag / token_budget    │   │
│  │  ↓                                 │   │
│  │  中途插话注入（pending_messages）    │   │
│  │  ↓                                 │   │
│  │  msgs = _chat_msgs()               │   │
│  │  resp = llm.chat(msgs, tools)      │   │
│  │  ↓                                 │   │
│  │  DSML 泄漏保险丝 / 空回答保险丝     │   │
│  │  ↓                                 │   │
│  │  有 tool_calls?                    │   │
│  │  ├── 是 → 执行工具 → add_step → ─┘ │   │
│  │  └── 否 → before_answer 钩子       │   │
│  │           ↓ turn_end 钩子          │   │
│  │           finish_turn(answer)      │   │
│  └───────────────────────────────────┘   │
│  ↓                                        │
│  自主模式? → 继续下一轮迭代                 │
│  inbox 有消息? → 继续下一轮迭代             │
│  否则 → return answer                      │
└──────────────────────────────────────────┘
```

### 关键设计决策

1. **循环替代递归**：自主继续时走 `while True` 的下一轮迭代，不调 `self.run()` 递归——避免栈溢出 + 状态隔离清晰
2. **DSML 泄漏保险丝**：DeepSeek/ModelScope 偶尔把工具调用以 DSML 文本塞进 content，`_postprocess_response` 先兜底解析；若仍残留则提示模型重试一次
3. **空回答保险丝**：无 tool_calls 且 content 为空时，追加 system 消息提示重试
4. **中途插话**：忙时用户消息挂到 `_pending_step_hint`，下一步边界注入（带 `📨〔用户中途补充，非新一轮〕` 标签），模型当步可见可改向

---

## 4. 工具调度：并行/串行锁设计

### 问题

同文件的多个 read-modify-write 工具调用如果并行执行，会读同一快照→各自改写→后写覆盖先写（静默丢更新）。

### 解决方案

```python
_FILE_TOOLS = frozenset({"read_file", "write_file", "edit", "insert",
                         "delete", "move", "grep", "find_function"})
```

`_file_key(tc)` 返回该调用锁定的文件绝对路径（非文件工具返回 None）。分组逻辑：

```
calls = [edit(a.py), read(b.py), edit(a.py), web_search(q)]
                  ↓ _file_key 分组
groups = {"/abs/a.py": [0, 2], "/abs/b.py": [1]}
free = [3]  # 无文件锁的调用
                  ↓
tasks = [[0, 2], [1], [3]]  # 组内串行，组间并行
                  ↓ ThreadPoolExecutor
results[0] = edit(a.py)  ──┐
results[2] = edit(a.py)  ──┘ 串行
results[1] = read(b.py)  ───── 并行
results[3] = web_search  ───── 并行
```

三种执行路径：
- **有 before_tool/after_tool 钩子**：逐 call 顺序执行（保证钩子时序）
- **单个调用**：直接执行（无并行开销）
- **多个调用无钩子**：`_run_tools_parallel` 跨文件并行

---

## 5. chat.py 队列驱动模型

### 双入口

```
agt (CLI)                    agt-web
  │                            │
  main()                      web_main()
  │                            │
  ┌──────────┐               ┌──────────────┐
  │ input()  │               │ start_server │
  │ 线程     │               │ (FastAPI+WS) │
  └────┬─────┘               └──────┬───────┘
       │                            │
       ▼                            ▼
  ┌─────────────────────────────────────┐
  │            work_q (Queue)            │
  │  ("user", "你好")  ("user", "/list") │
  │  ("task", fn)     ("background", x)  │
  └─────────────────┬───────────────────┘
                    │ 串行消费
                    ▼
              ┌───────────┐
              │  _worker  │  daemon 线程
              │           │
              │ kind=user │ kind=task │ kind=background
              │ + "/" →   │ → fn()   │ → agent.run()
              │ dispatch  │          │
              │ else →    │          │
              │ agent.run │          │
              └─────┬─────┘
                    │ on_event
                    ▼
              ┌───────────┐
              │ event_q   │
              │ (Queue)   │
              └─────┬─────┘
                    │
                    ▼
              ┌─────────────┐
              │ _render_loop │  主线程
              │             │
              │ _print_event│ → CLI 终端
              │ + 心跳 spinner│
              │ + "🧑 你："  │
              └─────────────┘
```

### _worker 三种 kind

| kind | 触发 | 处理 |
|------|------|------|
| `"user"` + `/` 前缀 | CLI input / WS 文本 | `registry.dispatch()` 斜杠命令 |
| `"user"` 普通文本 | CLI input / WS 文本 | `agent.run()`（drain 合并同类） |
| `"task"` | 工作流调试 / RAG 建库 / 会话操作 | `payload()` 直接执行 |
| `"background"` | inbox 消息 | `agent.run()` |

### _render_loop 心跳（v0.14.2+）

ANSI escape 原地刷新，不再刷屏：

```python
# \033[A = 光标上移  ⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏ = spinner
_show_status(f"⠋ 处理中「xxx」· 30s · 队列 0（Ctrl+C 停止）")
# 有事件输出时 _clear_status() 先隐去，下个 tick 再现
```

---

## 6. LLMClient 多模型封装

### 核心能力

```
LLMClient
  ├── chat()          非流式调用（回退链 + token 轮流 + 退避重试）
  ├── chat_stream()   流式调用（逐块回调 reasoning/content）
  ├── switch_model()  运行时热切换
  └── _chat_inner()   单模型调用（截断重试 + DSML 兜底）
```

### 回退链机制

```python
# 配置：fallback_chain = ["glm", "deepseek", "qwen"]
# 策略：sticky（降级后永久） / reset（每轮回退链首）

chat() 调用流程：
  1. _maybe_reset_to_head()  # reset 策略：先切回链首
  2. _chat_inner()           # 当前模型调用
  3. 成功 → 预旋转 token → 返回
  4. 失败(RateLimitError) → 换下一个 api_token 重试
  5. 失败(其他错误) → 沿回退链切下一个模型 + 退避等待
  6. 链耗尽 → raise RuntimeError
```

### DSML 兜底解析

DeepSeek/ModelScope 偶尔不通过标准 `tool_calls` 字段返回，而是把工具调用以内部 DSML 文本塞进 content：

```
<｜｜DSML｜｜tool_calls><｜｜DSML｜｜invoke name="工具名">
  <｜｜DSML｜｜parameter name="path" string="true">xxx</｜｜DSML｜｜parameter>
</｜｜DSML｜｜invoke>
```

`_parse_dsml_calls()` 用正则还原成标准 tool_calls，并从 content 剥除 DSML 文本。

### 截断重试

推理模型的长 reasoning 可能吃光 max_tokens → content 空/半截（`finish_reason=length`）：

```
finish_reason == "length"
  → _bump_max_tokens(cur)  # 翻倍，封顶 16384
  → 退避重试
  → 仍截断 → 返回截断结果（保留 partial 内容）
```

---

## 7. config.py 配置管理

### 模型配置优先级

```
1. ~/.agt/models.json（WebUI 可编辑，优先）
2. models.py（项目根，向后兼容，含 token，已 gitignore）
3. 空启动（首次安装，WebUI 引导添加）
```

### 运行时设置

```python
~/.agt/settings.json:
{
  "max_level": 4,              # 分档投影最大级别
  "retrieval_model": "glm",    # RAG 检索用的便宜模型
  "detail_base": 1500,         # 步距衰减初始摘要字数
  "detail_step": 15,           # 步距衰减每步减少字数
  "auto_reconnect": true       # WS 断线自动重连
}
```

### RAG 配置分层

```
全局 (~/.agt/rag.json)：embed 相关（provider/model_path/api_*），所有 repo 共用
Per-repo (~/.agt/repos/<hash>/rag.json)：索引策略（docs_dir/exts/top_k/...）
WebUI 一张表单 → save 时自动分层拆写
```

---

## 8. prompts.py 人设管理

```python
PERSONAS = {
    "默认助手": "你是一个友好、简洁的中文助手。",
    "严谨科学家": "...",
    "苏格拉底式老师": "...",
    "毒舌评论员": "...",
}

build_system(persona, user_name, today, extra, with_date)
```

**设计要点**：生产 Agent 关掉 `with_date`（日期改由 tail 每步注入实时时间），persona 保持纯净稳定——利于 **前缀缓存**（prefix cache）。如果日期烤死进 system prompt，每天变化后前缀缓存全部失效。

---

## 9. 模块间依赖关系

```
prompts.py ──→ config.py
                   ↑
agent.py ─────→ llm_client.py
    │              ↑
    ├──→ session.py (Session/Step/ToolCall)
    ├──→ tools.py (Toolbox)
    ├──→ background.py (ServiceManager/Scheduler)
    ├──→ longterm_memory.py (LongTermMemory)
    ├──→ plan_tools.py / spec_tools.py
    └──→ mdrender.py (render_cli)
              ↑
chat.py ──→ agent.py + server.py + commands.py
```

**关键约束**：`agent.run()` 非线程安全——任何时候只有一个 run 在跑。`work_q` 串行消费保证这一点。
