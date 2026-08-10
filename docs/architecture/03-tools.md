# 工具系统架构设计

> 对应源码：`src/tools.py` · `src/real_tools.py` · `src/mcp_client.py` · `src/lsp_manager.py`

---

## 1. 模块职责

| 文件 | 行数 | 职责 |
|------|------|------|
| `tools.py` | 176 | 工具基类：从 Python 函数的类型注解 + docstring **自动生成** OpenAI function schema |
| `real_tools.py` | 2023 | 所有内置工具的实现（文件读写/编辑/搜索/Python 执行/shell/步距衰减/图片落盘等） |
| `mcp_client.py` | 223 | MCP (Model Context Protocol) 客户端：自动发现外部工具、连接管理 |
| `lsp_manager.py` | 104 | LSP 语义导航：按需装配 Python/C# 的定义跳转/引用查找/诊断工具 |

---

## 2. Tool 类：从函数自动生成 Schema

### 核思想

写一个普通 Python 函数，加上**类型注解**和 **docstring**，就自动得到一个 OpenAI function calling 兼容的工具——零样板代码。

```python
def edit(path: str, old_string: str, new_string: str) -> str:
    """精确替换文件中的一段文本。"""
    ...

# 自动生成的 schema：
{
    "type": "function",
    "function": {
        "name": "edit",
        "description": "精确替换文件中的一段文本。",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "old_string": {"type": "string"},
                "new_string": {"type": "string"}
            },
            "required": ["path", "old_string", "new_string"]
        }
    }
}
```

### 类型映射

```python
_PY_TO_JSON_SCHEMA = {
    str: "string",
    int: "integer",
    float: "number",
    bool: "boolean",
}

# list → {"type": "array", "items": {}}
# dict → {"type": "object"}
# 无注解 → 默认 str
```

### 关键坑

**无注解的参数默认 `str`**——如果参数实际是数组（如 `replace_lines` 的 `entries`），必须写 `: list`，否则 schema 会标成 `"type": "string"`，导致 LLM 传 JSON 字符串而非数组。

### Toolbox：注册与派发

```python
class Toolbox:
    def register(self, name, func, description, outputs, hidden, param_descriptions)
    def schemas(self) -> list[dict]     # 产出 tools 列表给 LLM
    def call(self, name, args) -> str   # 按名字派发执行
```

**错误处理**：工具执行出错不抛异常炸流程，而是把错误**如实以文本回传**给模型，让它有机会修正。

---

## 3. real_tools.py：内置工具全景

### 工具分类

| 类别 | 工具 | 说明 |
|------|------|------|
| **文件读取** | `read_file`, `list_dir` | 支持 txt/Word/Excel/PDF 自动提取 |
| **文件编辑** | `write_file`, `edit`, `insert`, `delete`, `move`, `replace_lines` | 各种粒度的修改 |
| **搜索** | `grep`, `find_function` | 正则/函数级搜索 |
| **代码执行** | `run_python`, `run_shell`, `run_script` | 内联/文件/脚本执行 |
| **网络** | `web_search`, `open_url` | DuckDuckGo + 网页抓取 |
| **Agent 系统** | `create_agent`, `agent_prompt`, `list_agents`, `kill_agent` | 多 Agent 声明与调度 |
| **计划/方案** | `create_plan`, `update_plan`, `create_spec`, `commit_spec` | 任务管理 |
| **记忆** | `add_memory`, `search_memory`, `read_procedure` | 长期记忆 CRUD |
| **会话** | `recall_turn`, `rename_session` | 会话管理 |
| **Wiki** | `wiki_read`, `wiki_write`, `wiki_search` | 知识库 |
| **工作流** | `debug_workflow`, `hotswap_workflow_node` | 工作流调试 |
| **后台服务** | `start_service`, `stop_service`, `add_schedule` | 长运行/定时 |

### 并行/串行锁设计

```python
# agent.py 中定义，real_tools 的工具函数本身不知道锁的存在
_FILE_TOOLS = frozenset({"read_file", "write_file", "edit", "insert",
                         "delete", "move", "grep", "find_function"})
```

Agent 的 `_file_key(tc)` 返回工具调用锁定的文件绝对路径。同一文件的多个调用按原顺序串行执行（防 read-modify-write 竞态丢更新），不同文件并行。

### Recent-file 快照跟屁虫

```python
_FILE_SNAP_TOOLS = frozenset({"edit", "insert", "delete", "move", "write_file",
                               "read_file", "grep", "find_function",
                               "run_python", "run_script"})
_FILE_SNAP_MAX = 3  # 每步最多快照 3 个不同文件
```

每步工具执行完后，`_collect_file_snapshots()` 扫描涉及的文件路径，读取当前全文速照，挂在对应 tool result 的尾巴上（仅全量步注入）。模型不用再 `read_file` 就能看到改后的完整文件内容——减少一轮工具调用。

### 图片落盘

工具结果里的 data-URL 图片（base64）不进存档/事件流，而是落盘到 `~/.agt/repos/<hash>/images/` 并替换成 `<img>name</img>` 占位标签：

```python
_DATA_URL_RE = re.compile(r"data:image/(png|jpe?g|gif|webp);base64,([A-Za-z0-9+/=]+)", re.I)

def _materialize_tool_result(result, tool_name, args, cid):
    # 1. 正则匹配所有 data-URL 图片段
    # 2. base64 解码落盘 → {cid}_{idx}.{ext}
    # 3. 替换成 <img>{cid}_{idx}.{ext}</img> 占位
    # 线程安全：文件名含 cid（并行调用唯一）+ 序号
```

### file_version 校验

编辑类工具（edit/insert/delete/move/replace_lines）需要传 `version` 参数（从 `read_file`/`grep` 返回的 `file_version`）。如果文件已被修改（版本不匹配），工具拒绝执行——要求重读。这是一个**乐观锁**机制，防止基于过期快照的修改。

### 步距衰减摘要

工具结果在喂给 LLM 时按步距衰减（详见 02-session-memory.md）。`real_tools.py` 定义了衰减参数：

```python
DETAIL_BASE = 1500   # 当前步（d=0）的摘要上限
DETAIL_STEP = 15     # 每远一步减少的字数
DETAIL_FLOOR = 20    # 下限

def detail_limit(distance: int, base: int = None) -> int:
    b = base or DETAIL_BASE
    return max(b - distance * DETAIL_STEP, DETAIL_FLOOR)
```

### 流式回调

`run_python` / `run_shell` 支持实时流式输出。Agent 设置 `_rt._tool_emit` 回调，子进程的 stdout 通过 `tool_stream` / `tool_progress` 事件推给 UI：

```python
# agent.py 在工具执行前设置
_rt._tool_emit = self.on_event if self.on_event else (self._print_only_emit if self.verbose else None)
# real_tools.py 的 run_python 内部
if _tool_emit:
    _tool_emit({"type": "tool_stream", "text": chunk})
# 执行完后清理
_rt._tool_emit = None
```

---

## 4. mcp_client.py：MCP 工具发现

### 设计

MCP (Model Context Protocol) 是 Anthropic 的标准协议，用于 Agent 发现和调用外部工具服务器。

```python
class MCPManager:
    def connect_from_config(self, config_path: str)
    # 读取 .mcp.json → 启动子进程 MCP server → 握手 → 发现工具 → 注册进 Toolbox

    def shutdown(self)
    # 断开所有 MCP server 连接
```

### 配置文件

两处 MCP 配置（都读）：
```json
// workspace/.mcp.json（项目级）
// ~/.agt/mcp.json（全局级，如 ensure_lsp 装配的 LSP server）
{
    "mcpServers": {
        "python-lsp": {
            "command": "python",
            "args": ["~/.agt/lsp/python_lsp.py"]
        }
    }
}
```

### 工具命名

MCP 发现的工具名加 `__mcp__{server}__{tool}` 前缀，避免与内置工具名冲突：

```python
# server="python-lsp", tool="py_def"
# → 工具名 = "__mcp__python-lsp__py_def"
```

### 热重载

WebUI 的 MCP 配置页保存后，先全部断开旧连接，再按新配置重新连接——工具列表实时更新。

---

## 5. lsp_manager.py：语义导航

### 问题

`grep` 找代码引用/定义在重载/泛型/分部类/扩展方法前会失效。LSP（Language Server Protocol）能做真正的语义分析。

### 按需装配

```python
def ensure_lsp(lang: str):
    """按需装配某语言的 LSP 语义导航工具。
    1. 复制对应的 LSP 脚本到 ~/.agt/lsp/
    2. 安装依赖（pyright for Python, OmniSharp for C#）
    3. 启动 MCP server（lsp_manager 管理生命周期）
    4. 工具注册进 Toolbox（__mcp__{lang}-lsp__*）
    当轮即可用
    """
```

### 支持的工具

| 语言 | 工具 | 功能 |
|------|------|------|
| Python | `py_def`, `py_ref`, `py_syms` | 定义跳转 / 引用查找 / 符号列表 |
| C# | `cs_def`, `cs_ref`, `cs_wsym`, `cs_hover`, `cs_syms`, `cs_diag` | 定义 / 引用 / 符号搜索 / 悬停信息 / 符号列表 / 诊断 |

### 写 .cs 后自动诊断

`after_tool` 钩子工作流（`cs_auto_diag.xml`）在写 `.cs` 文件后自动调用 `cs_diag`，仅当有 `[ERROR]` 时注入诊断结果——形成"改→查错→再改"闭环。

---

## 6. 设计亮点总结

1. **零样板注册**：写函数 = 得到工具，类型注解自动生成 schema
2. **并行/串行锁**：跨文件并行、文件内串行，优雅解决 read-modify-write 竞态
3. **file_version 乐观锁**：编辑类工具要求版本校验，防基于过期快照的修改
4. **Recent-file 跟屁虫**：每步自动快照涉及的文件，减少模型再 read 的轮次
5. **图片落盘**：base64 不进存档/事件流，落盘后用占位标签引用
6. **步距衰减**：工具结果按距离差异化摘要，截断处标注 call_id 可按需拉取
7. **MCP 标准协议**：支持外部工具服务器自动发现，热重载
8. **LSP 按需装配**：grep 不够用时一键装上语义导航，改完代码自动诊断
