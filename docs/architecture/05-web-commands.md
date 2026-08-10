# 05 — Web 命令系统与终端渲染

> 涉及文件：`src/server.py`、`src/commands.py`、`src/mdrender.py`

本文档描述 Agent 的 WebUI 多客户端架构、事件广播与断线重连机制、CLI 斜杠命令系统，以及终端 ANSI 渲染管线。

---

## 1. 总体架构

```
┌──────────────────────────────────────────────────────────────┐
│                      chat.main (主线程)                       │
│  ┌─────────────┐    ┌──────────────┐    ┌─────────────────┐  │
│  │  work_q     │◄── │  worker 线程  │──► │  Agent.run()    │  │
│  │ (Queue)     │    │  (串行消费)   │    │  on_event=      │  │
│  │             │    │              │    │  _broadcast      │  │
│  └─────┬───────┘    └──────────────┘    └────────┬────────┘  │
│        │  ("user", text) / ("task", fn)           │            │
│        │                                         │            │
│        │              ┌──────────────────────────┘            │
│        │              │  事件流 (ev dict)                      │
│        ▼              ▼                                       │
│  ┌─────────────────────────────────────────────────────────┐  │
│  │              server.py (uvicorn 后台线程)                │  │
│  │                                                         │  │
│  │  ┌─────────┐  ┌─────────┐  ┌─────────┐  ← _clients[]  │  │
│  │  │ WS 客户端 │  │ WS 客户端 │  │ WS 客户端 │  多客户端广播  │  │
│  │  │ queue₁  │  │ queue₂  │  │ queue₃  │                │  │
│  │  └─────────┘  └─────────┘  └─────────┘                │  │
│  │                                                         │  │
│  │  _event_log[] (≤500 条环形缓冲)                          │  │
│  │  _seq (单调递增序号)                                     │  │
│  │  _main_loop (asyncio loop, 跨线程推送)                   │  │
│  └─────────────────────────────────────────────────────────┘  │
│                                                               │
│  ┌─────────────────────────────────────────────────────────┐  │
│  │              commands.py (CLI 命令注册器)                │  │
│  │  CommandRegistry → dispatch("/save", CommandContext)     │  │
│  └─────────────────────────────────────────────────────────┘  │
│                                                               │
│  ┌─────────────────────────────────────────────────────────┐  │
│  │              mdrender.py (终端 ANSI 渲染)                 │  │
│  │  render_cli(text) → 表格框线 + 代码块灰条               │  │
│  └─────────────────────────────────────────────────────────┘  │
└──────────────────────────────────────────────────────────────┘
```

核心设计原则：**CLI 与 WebUI 共享同一个 Agent 单例、同一个 work_q、同一个事件流**，天然串行一致，无装配漂移。

---

## 2. WebUI 多客户端架构

### 2.1 注入态模型

`server.py` 不独立装配 Agent，而是由 `chat.main` 经 `start_server()` 注入：

```python
# server.py 模块级全局注入态
_agent = None       # chat.build_agent 装配好的全局 Agent 单例
_work_q = None      # chat 主循环的 work_q（WS 文本 / task 喂入它）
_mcp_mgr = None     # MCPManager（/api/tools 用其 get_tools）
_state = None       # chat 主循环的 state dict（含 busy 标志）
```

`start_server()` 签名：

```python
def start_server(*, agent, work_q, mcp_mgr=None, workspace=WORKSPACE, port=8000, state=None):
```

注入发生在 `/web start` 命令触发时（`commands.py::_cmd_web`），确保 WebUI 与 CLI 操作的是同一个 Agent 实例。

### 2.2 多客户端连接管理

每个 WebSocket 连接注册为一个 `client` 字典，存入 `_clients` 列表：

```python
_clients: list[dict] = []  # [{ws, queue}]  所有活跃连接

# ws_endpoint() 中：
queue: asyncio.Queue = asyncio.Queue()
client = {"ws": websocket, "queue": queue}
_clients.append(client)
```

每个客户端拥有独立的 `asyncio.Queue`，`broadcast` 时往每个 queue 投递事件副本。连接断开时从 `_clients` 移除。

### 2.3 WS 消息循环

`ws_endpoint` 使用三任务竞争模型（`asyncio.wait` + `FIRST_COMPLETED`）：

```python
while True:
    ws_task   = asyncio.create_task(websocket.receive_text())  # 客户端消息
    queue_task = asyncio.create_task(queue.get())              # 广播事件
    ping_task  = asyncio.create_task(asyncio.sleep(30))        # 心跳

    done, pending = await asyncio.wait([ws_task, queue_task, ping_task],
                                       return_when=asyncio.FIRST_COMPLETED)
```

- **ws_task**：用户输入 → `_handle_user_input` 路由
- **queue_task**：广播事件 → `_send(ws, ev)` 推给该客户端
- **ping_task**：30 秒心跳，发 `{"type": "_ping"}` 保活

### 2.4 REST API 概览

| 路径 | 方法 | 功能 |
|------|------|------|
| `/` | GET | 主聊天页 |
| `/editor` | GET | 工作流可视化编辑器 |
| `/wfdebug` | GET | 工作流调试页 |
| `/rag` | GET | RAG 文档库管理页 |
| `/api/wf/list` | GET | 列出所有工作流 |
| `/api/wf/{name}` | GET/PUT | 获取/保存单个工作流画布 |
| `/api/wf/create` | POST | 创建新工作流 |
| `/api/wf/{name}` | DELETE | 删除工作流 |
| `/api/tools` | GET | 返回 agent 已注册的全部工具（按模块分组） |
| `/api/models` | GET/PUT | 模型列表 / 保存模型配置 |
| `/api/model-list` | GET | 模型名→显示名映射（不含敏感信息） |
| `/api/mcp` | GET/PUT | 读取/保存 MCP 配置（含热重载） |
| `/api/stats` | GET | LLM 调用可靠性统计 |
| `/api/rag/config` | GET/PUT | RAG 配置读取/保存（热重建实例） |
| `/api/rag/stats` | GET | RAG 索引统计 |
| `/api/rag/query` | GET | RAG 页内查询测试 |
| `/ws` | WebSocket | 实时双向通信端点 |

---

## 3. 广播 vs 单发设计

### 3.1 广播（`_broadcast`）

所有 Agent 事件统一走广播路径，由 `Agent.on_event` 回调触发：

```python
def _broadcast(ev: dict):
    if not _clients:          # 守卫 1：无客户端时直接 return
        return
    global _seq
    _seq += 1
    _event_log.append((_seq, ev))     # 写入事件缓冲
    if len(_event_log) > 500:         # 环形缓冲上限 500
        _event_log.pop(0)
    loop = _main_loop
    if loop is None:          # 守卫 2：loop 未就绪时 return
        return
    for c in _clients:        # 逐客户端投递到各自 queue
        try:
            loop.call_soon_threadsafe(c["queue"].put_nowait, ev)
        except Exception:
            pass
```

关键设计点：

1. **零开销守卫**：纯 CLI 模式（无 WS 客户端）时直接 return，不分配序号、不写缓冲、不触 loop。
2. **跨线程安全**：`_broadcast` 在 worker 线程被调用（Agent.run 期间），通过 `call_soon_threadsafe` 将事件投递到 uvicorn 线程的 asyncio loop。
3. **公开别名**：`broadcast = _broadcast`，由 `chat.build_agent` 用作 `Agent.on_event`。

### 3.2 单发（`_send`）

针对单个客户端的即时响应（不进事件缓冲、不广播给其他客户端）：

```python
async def _send(ws: WebSocket, obj: dict):
    await ws.send_text(json.dumps(obj, ensure_ascii=False))
```

单发用于：
- 斜杠命令的即时输出（`{"type": "system", "text": out}`）
- 配置读取/设置的结果回传
- 工作流调试的 per-client 状态通知
- 连接握手消息（模型列表、会话列表等）

### 3.3 设计对比

| 维度 | 广播 (`_broadcast`) | 单发 (`_send`) |
|------|---------------------|----------------|
| 接收者 | 所有活跃 WS 客户端 | 单个请求客户端 |
| 事件缓冲 | 写入 `_event_log`（≤500） | 不写入 |
| 序号分配 | `_seq` 单调递增 | 无序号 |
| 触发方 | Agent.on_event（worker 线程） | WS handler（uvicorn 线程） |
| 线程跨越 | 是（call_soon_threadsafe） | 否（同 loop） |
| 典型场景 | LLM 流式输出、工具调用事件 | 命令结果、配置回传 |

### 3.4 混合模式

部分 action 同时使用两种模式。例如 `load_session`：

```python
# 1) 走 work_q 串行执行 /resume 命令（与 CLI 完全相同路径）
_work_q.put(("user", f"/resume {_ls_name}"))

# 2) 紧随其后：广播 session_history 给所有客户端
def _sync_loaded():
    _broadcast({"type": "session_history", ...})
_work_q.put(("task", _sync_loaded))

# 3) 即时单发确认
await _send(ws, {"type": "system", "text": "🔄 恢复中…"})
```

---

## 4. 事件缓冲与断线重连

### 4.1 事件缓冲结构

```python
_event_log: list[tuple[int, dict]] = []  # [(seq, ev), ...]
_seq: int = 0                              # 单调递增序号
```

- 每条广播事件分配唯一序号 `_seq`，存入 `_event_log`。
- 缓冲上限 500 条，超出时 `pop(0)` 淘汰最旧（FIFO 环形缓冲）。
- 缓冲仅在 `_clients` 非空时才写入（与广播守卫一致）。

### 4.2 重连检测与恢复流程

```python
# ws_endpoint() 连接握手时：
is_reconnect = len(_event_log) > 0
if is_reconnect:
    await _send(websocket, {
        "type": "system",
        "text": "✅ 已重连（前端会自动请求当前对话历史）",
        "models": [...],
        "current_model": agent.model_name,
    })
```

重连后前端通过 `current_history` action 拉取当前内存中 session 的完整历史：

```python
if _d.get("action") == "current_history":
    await _send(ws, {
        "type": "session_history",
        "name": agent.session.name or "(当前会话)",
        "turns": agent.session.to_history(),
    })
```

### 4.3 设计权衡

| 方案 | 本项目选择 | 理由 |
|------|-----------|------|
| 事件重放（补发 _event_log 中的增量） | ❌ 未采用 | 事件类型多样（stream/tool_call/tool_result 等），按序号补发复杂度高 |
| 全量历史重载 | ✅ 采用 | 前端拉 `session.to_history()` 重建完整对话，简单可靠 |
| 心跳保活 | ✅ 30s ping | 防止代理/防火墙超时断连 |

事件缓冲的主要价值不是重放，而是：
1. **重连检测**：`len(_event_log) > 0` 判断是否为重连场景。
2. **调试参考**：保留最近 500 条事件供排查。

### 4.4 心跳机制

```python
ping_task = asyncio.create_task(asyncio.sleep(30))
# ...
if ping_task in done:
    try:
        await websocket.send_json({"type": "_ping"})
    except Exception:
        break  # 心跳失败 → 断开
    continue
```

30 秒间隔的 `_ping` 消息保持连接活跃，前端可忽略该类型。心跳发送失败时主动 break，触发 `finally` 清理。

### 4.5 断线清理

```python
finally:
    if client in _clients:
        _clients.remove(client)
```

连接断开（`WebSocketDisconnect` 或其他异常）时从 `_clients` 移除。`stop_server()` 时直接清空 `_clients = []`，断开所有连接。

---

## 5. 用户输入路由（`_handle_user_input`）

`_handle_user_input` 是 WS 消息的核心路由器，按优先级处理三类输入：

### 5.1 JSON Action（最高优先级）

以 `{` 开头的消息尝试解析为 JSON，按 `action` 字段路由：

| action | 处理方式 | 说明 |
|--------|---------|------|
| `restore` | work_q → task | 回溯到快照检查点 |
| `get_config` | 单发即时 | 返回当前运行时配置 |
| `set_config` | 单发即时 | 应用配置变更 |
| `stop` | 单发即时 | 设置 `agent._stop_flag` |
| `open_terminal` | 单发即时 | 在终端中执行命令 |
| `list_sessions` | 单发即时 | 返回已保存会话列表 |
| `current_history` | 单发即时 | 返回当前 session 完整历史（重连用） |
| `new_session` | work_q → `/reset` + task | 新建会话（走 CLI 相同路径） |
| `save_session` | 单发即时 + 广播 | 保存当前会话 |
| `load_session` | work_q → `/resume` + task | 恢复指定会话（走 CLI 相同路径） |
| `insert_message` | 单发即时 | 自主模式下插入消息到队列 |
| `list_workflows` | 单发即时 | 返回工作流列表 |
| `reload_workflows` | 单发即时 | 重载工作流工具 |
| `debug_run` | work_q → task | 工作流调试执行（逐节点流式） |
| `hotswap_node` | 单发即时 | 热替换节点配置 |
| `rerun_node` | 单发即时 | 单节点重跑 |
| `rag_build` | work_q → task | RAG 建库（进度流式） |
| `feedback` | 单发即时 | 提交反馈 |

### 5.2 斜杠命令（即时处理，不进 work_q）

```python
if text.startswith("/"):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        registry.dispatch(text, CommandContext(agent=agent))
    out = buf.getvalue().strip()
    if out:
        await _send(ws, {"type": "system", "text": out})
```

斜杠命令在 WS handler 线程即时执行（通过 `redirect_stdout` 捕获 print 输出），不进 work_q，不影响 Agent 主循环。

### 5.3 普通对话文本（进 work_q）

```python
if _state is not None and _state.get("busy"):
    # Agent 正在跑：入 pending_messages，本步边界注入
    agent.queue_user_message(text)
    await _send(ws, {"type": "system",
                     "text": f"📥 已排队并将在下一步注入当前任务（队列 {len(agent.pending_messages)} 条）"})
else:
    # Agent 空闲：正常入队下一轮
    _work_q.put(("user", text))
    await _send(ws, {"type": "system", "text": "✅ 已接收，处理中…"})
```

**忙时注入**是关键设计：Agent 正在执行时，新消息不另起一轮，而是进入 `pending_messages`，在下一步边界注入当前任务——模型可以在工具调用间隙看到、改向。

### 5.4 work_q 两类任务

```python
_work_q.put(("user", text))       # 用户文本 → agent.run 一轮
_work_q.put(("task", fn))         # 任意函数 → worker 线程串行执行
```

`("task", fn)` 用于工作流调试、RAG 建库等占用 Agent 的长任务，与聊天同流串行，避免并发冲突。

---

## 6. CLI 命令系统

### 6.1 CommandContext

```python
@dataclass
class CommandContext:
    agent: "Agent"     # 提供 session / llm / base_system / max_steps / token_budget / cumulative_tokens
    work_q: object = None  # chat 主循环的 work_q（/web 启动服务时注入）
    state: object = None   # chat 主循环的 state dict（含 busy）

    @property
    def session(self) -> Session:
        return self.agent.session
```

`CommandContext` 是所有命令处理函数的统一参数，封装 Agent 引用及主循环上下文。CLI 和 WebUI 共用同一套命令实现。

### 6.2 CommandRegistry

```python
class CommandRegistry:
    def __init__(self):
        self._cmds: dict[str, tuple[Callable, str]] = {}  # {name: (handler, help_text)}

    def register(self, name: str, handler: Callable, help_text: str = ""):
        self._cmds[name] = (handler, help_text)

    def dispatch(self, line: str, ctx: CommandContext) -> bool:
        """返回 True=是命令(已处理)，False=不是命令(交给 Agent)"""
```

`dispatch` 的解析逻辑：
1. 不以 `/` 开头 → 返回 False（交给 Agent 处理）
2. `shlex.split` 拆分参数（引号感知）
3. 命令名不在注册表 → 提示未知命令
4. `/call` 特殊处理：参数含 JSON，传原始字符串避免 shlex 破坏引号
5. 其余命令：传 `parts[1:]` 作为 args 列表

### 6.3 参数解析（`_parse_args`）

```python
def _parse_args(args: list[str]) -> tuple[list[str], dict]:
    """把 ['--k','v', 'pos'] 拆成 (位置参数，{flag: value/True})"""
```

支持 `--flag value` 和位置参数混合：
- `--key value` → `flags["key"] = value`
- `--key`（无后续值或后续值以 `--` 开头）→ `flags["key"] = True`
- 非 `--` 前缀 → 位置参数

### 6.4 命令清单

`build_default_registry()` 注册的全部命令：

| 命令 | 处理函数 | 说明 |
|------|---------|------|
| `/save [name]` | `_cmd_save` | 保存当前会话（改名另存） |
| `/rename <新名>` | `_cmd_rename` | 重命名当前会话 |
| `/resume <name>` | `_cmd_resume` | 恢复指定会话 |
| `/list` | `_cmd_list` | 列出所有已保存会话 |
| `/show [name]` | `_cmd_show` | 查看会话详情 |
| `/recall <关键词>` | `_cmd_recall` | 召回匹配历史轮次 |
| `/reset` | `_cmd_reset` | 重置会话（清空历史+计划+自主模式） |
| `/config <k> <v>` | `_cmd_config` | 改运行时配置 |
| `/budget` | `_cmd_budget` | 查看 token 消耗 |
| `/stats [all]` | `_cmd_stats` | LLM 调用可靠性统计 |
| `/model [name]` | `_cmd_model` | 列出/切换 LLM 模型 |
| `/reload_mcp <name>` | `_cmd_reload_mcp` | 重连指定 MCP server |
| `/autonomous ...` | `_cmd_autonomous` | 纯自主模式控制 |
| `/workflows [reload]` | `_cmd_workflows` | 列出/重载工作流 |
| `/memory ...` | `_cmd_memory` | 长期记忆管理 |
| `/logs [N]` | `_cmd_logs` | 查看当前 session 日志尾部 |
| `/download ...` | `_cmd_download` | 下载随包资产 |
| `/feedback ...` | `_cmd_feedback` | 提交反馈给作者 |
| `/web [start] [port]` | `_cmd_web` | 按需启停内嵌 Web 服务 |
| `/snapshot ...` | `_cmd_snapshot` | 工作区快照回溯 |
| `/rewind [count]` | `_cmd_rewind` | 回溯到 count 个 turn 之前 |
| `/rag ...` | `_cmd_rag` | RAG 文档库管理 |
| `/tools [关键词]` | `_cmd_tools` | 列出所有工具（按来源分组） |
| `/call [yes] tool(args)` | `_cmd_call` | 手动调用工具 |
| `/update` | `_cmd_update` | 检查并升级到 PyPI 最新版 |
| `/help` | lambda | 显示帮助 |

### 6.5 配置系统（`read_config` / `apply_config`）

可配置项定义在 `CONFIGURABLE` 字典中，每项标注目标对象（agent/llm）和类型转换函数：

```python
CONFIGURABLE = {
    "max_steps":      ("agent", int),
    "token_budget":   ("agent", int),
    "max_retries":    ("llm", int),
    "temperature":    ("llm", float),
    "enable_thinking": ("llm", _to_bool),
    "fallback_policy": ("llm", _policy_cast),
    "reasoning_completer": ("llm", _str_or_none),
}
```

此外有多个特殊处理项（不在 CONFIGURABLE 中，`apply_config` 单独处理）：

| 配置项 | 持久化位置 | 即时生效方式 |
|--------|-----------|-------------|
| `tool_timeout` | real_tools 全局变量 | `set_tool_timeout()` |
| `fallback_chain` | agent.llm 属性 | 直接赋值 |
| `max_level` | settings.json | session.max_level + 清冻结缓存 |
| `max_effective_context_window` | models.json | llm + session + 清冻结缓存 |
| `retrieval_model` | settings.json | 新建 LLMClient 实例 |
| `detail_base` / `detail_step` | settings.json | toollog 模块变量 |

### 6.6 命令在 CLI 与 WebUI 中的差异

同一套命令实现，两种执行路径：

| 维度 | CLI (chat 主循环) | WebUI (WS handler) |
|------|------------------|-------------------|
| 执行方式 | `registry.dispatch` 直接调用 | `redirect_stdout` 捕获后单发 |
| 输出目标 | 终端 stdout | `{"type": "system", "text": out}` |
| work_q | 直接可用 | 通过 `_work_q` 全局引用 |
| 涉及 Agent 状态变更的命令 | 即时生效 | 部分走 work_q 串行（如 `/resume`、`/reset`） |

WebUI 中涉及 Agent 状态变更的命令（如 `load_session`、`new_session`）不直接调 `registry.dispatch`，而是将 `/resume` / `/reset` 作为文本喂入 `_work_q`，走与 CLI 完全相同的 worker 路径，确保串行一致。

---

## 7. 终端 ANSI 渲染（`mdrender.py`）

### 7.1 设计目标

零依赖（仅标准库 `re` + `unicodedata`），将 LLM 回答中的 Markdown 子集渲染为终端友好的 ANSI 文本。被 `agent.py::_print_event` 和 `session.py::_format_turn_full` 共用。

渲染范围与 WebUI 前端 `renderAnswer` 对齐，只处理**表格 + 代码块**，不做完整 Markdown 解析。

### 7.2 ANSI 颜色常量

```python
GRAY  = "\033[90m"   # 暗灰（代码块边框、行内 code）
RESET = "\033[0m"    # 重置
GREEN = "\033[32m"   # 绿色（表头行、表格框线）
```

### 7.3 显示宽度计算（`disp_width`）

```python
def disp_width(s: str) -> int:
    """CJK/全角/Ambiguous 算 2，其余 1；
    跳过 Variation Selectors / ZWJ / 组合符（宽 0）"""
```

基于 `unicodedata.east_asian_width()`，将 `W`（Wide）、`F`（Fullwidth）、`A`（Ambiguous）计为 2 列，其余 1 列。特殊处理：
- Variation Selectors（U+FE00–FE0F）：宽 0
- ZWJ（U+200D）：宽 0（emoji 组合序列）
- 通用组合符（`combining` 类）：宽 0

`pad_right` 按显示宽度补空格，保证表格列对齐。

### 7.4 表格渲染

#### Cell 拆分（`split_cells`）

处理 Markdown 表格行的单元格拆分，支持 `\|` 转义和反引号保护：

1. 成对反引号段 `` `…` `` 替换为 NUL 占位符（保护 code span 中的 `|`）
2. 按非转义 `|` 切分（正则 `(?<!\\)\|`）
3. 每 cell 还原 `\|` → `|`，还原反引号占位符
4. `strip()`

#### 表格检测

```python
_TABLE_ROW_RE = re.compile(r"^\s*\|.*\|\s*$")  # 行首尾有 |
_SEP_CELL_RE  = re.compile(r"^:?-+:?$")       # GFM 分隔行 :-- / --: / :--:
```

合法表格需满足：
1. 至少 2 行连续的表格行
2. 第 2 行为分隔行（每个 cell 匹配 `:?-+:?`）

#### ASCII 框线表（`ascii_table`）

```python
def ascii_table(header: list, rows: list) -> str:
```

使用 box-drawing 字符绘制：

```
┌──────┬──────┐
│ 列1  │ 列2  │   ← 表头 GREEN
├──────┼──────┤
│ 数据 │ 数据 │   ← 数据行，inline code 标灰
└──────┴──────┘
```

- 列数按 header 归一：短行补空格，多出格子拼进末格
- 列宽 = 该列最大显示宽度（`disp_width`）
- 表头行整行 GREEN，框线 GRAY
- 数据行 cell 中的行内 `` `code` `` 标灰（`_inline_gray`）
- `pad` 按原始文本算（ANSI 码不计入宽度），保证对齐

### 7.5 代码块渲染（`render_code_block`）

```python
def render_code_block(code_lines: list, lang: str = "") -> str:
```

```
  [python]        ← 语言标签（灰色，可选）
 │ code line 1   ← 每行灰色 │ 左边框
 │ code line 2
```

不画 ``` 围栏头尾，靠左边框 ` │ ` + 颜色标识，避免反引号计数歧义。

### 7.6 顶层渲染管线（`render_cli`）

逐行状态机，判定顺序固定为**「先 ``` 围栏，再 `|表格|`，其余原样」**：

```python
def render_cli(text: str) -> str:
    lines = (text or "").split("\n")
    out, buf, i, n = [], [], 0, len(lines)

    while i < n:
        line = lines[i]
        stripped = line.strip()

        # 1) fenced code（围栏优先级最高）
        if stripped.startswith("```"):
            flush_plain()
            lang = stripped[3:].strip()
            # 收集到闭合围栏
            out.append(render_code_block(code_lines, lang))

        # 2) table（连续表格行）
        elif is_table_row(line):
            tbl = []
            while i < n and is_table_row(lines[i]):
                tbl.append(lines[i]); i += 1
            rendered = _try_render_table(tbl)
            if rendered is not None:
                flush_plain(); out.append(rendered)
            else:
                buf.extend(_inline_gray(x) for x in tbl)  # 非法表→普通文本

        # 3) 普通行
        else:
            buf.append(_inline_gray(line)); i += 1

    flush_plain()
    return "\n".join(out)
```

优先级保证代码块内的 `|` 不被表格吞掉。非法表格（无分隔行）回退为普通文本，行内 `` `code` `` 仍标灰。

### 7.7 行内 code 标灰（`_inline_gray`）

```python
def _inline_gray(s: str) -> str:
    return re.sub(r"`([^`]*)`", lambda m: f"{GRAY}{m.group(1)}{RESET}", s)
```

去反引号、内容标灰。仅作用于显示，不影响 `disp_width` 计算（padding 按原始文本算）。

---

## 8. 服务启停

### 8.1 启动流程

```
/web [start] [port]
  → _cmd_web (commands.py)
    → start_server(agent=, work_q=, mcp_mgr=, workspace=, port=, state=)
      → 端口探测（socket.bind 试占）
      → 注入 _agent / _work_q / _mcp_mgr / _state
      → 创建 uvicorn.Server，daemon 线程跑 serve()
      → 等待 srv.started=True（最多 4 秒）
      → open_browser(port) + 打印局域网地址
```

### 8.2 停止流程

```
/web stop
  → stop_server()
    → _server.should_exit = True
    → _server = None
    → _main_loop = None
    → _clients = []  （断开所有 WS 客户端）
```

`stop_server_if_running()` 作为 chat 退出兜底，确保端口释放。

### 8.3 状态查询

`server_status()` 返回：

```python
{
    "running": bool,        # _server 存在且 started
    "port": int | None,
    "local_url": str,       # http://127.0.0.1:{port}/
    "lan_urls": list[str],  # 局域网地址（枚举本机网卡 IPv4）
    "error": str | None,
}
```

`lan_urls()` 通过 `socket.getaddrinfo` 枚举本机网卡 IPv4，过滤回环地址和 IPv6，供局域网设备连接。

---

## 9. 关键设计决策总结

| 决策 | 选择 | 理由 |
|------|------|------|
| Agent 装配 | CLI 与 WebUI 共享单例 | 天然串行一致，无装配漂移 |
| 事件推送 | 广播 + 每 client queue | 多客户端独立消费，互不阻塞 |
| 跨线程通信 | call_soon_threadsafe | worker 线程 → uvicorn loop 安全投递 |
| 纯 CLI 开销 | 无客户端时 return | 零开销，不分配序号/写缓冲 |
| 断线重连 | 全量历史重载 | 简单可靠，避免事件重放复杂度 |
| 命令执行 | CLI/WebUI 共用同一套实现 | 行为一致，维护成本低 |
| 忙时消息 | 入 pending_messages 边界注入 | 不另起一轮，可在工具间隙改向 |
| Markdown 渲染 | 仅表格+代码块 | 覆盖 LLM 输出最高频结构，零依赖 |
| 表格对齐 | CJK 双宽 + ANSI 不计入 | 中文环境下列对齐正确 |
