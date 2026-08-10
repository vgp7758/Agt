# 04 — 工作流引擎架构

> 源码：`src/workflow.py`（2043 行）、`src/workflow_xml.py`（845 行）、`src/workflow_debug_tools.py`（154 行）

---

## 1. 设计概览

本系统实现了一个**忠实兼容 Coze Studio 画布模型**的工作流引擎。核心理念：

| 原则 | 说明 |
|------|------|
| **画布即数据** | `.agent/workflows/<名>.json` 或 `.xml` = Coze 原生画布 JSON `{nodes, edges, versions}`，只读不改 |
| **旁车元数据** | `<名>.json.meta` / `<名>.xml.meta` = 旁车文件，存放 `name`/`description`/`coze_url`/`enabled`/`hook` 等 Agent 工具元数据 |
| **每轮扫描注册** | 每轮对话扫描 `.agent/workflows/` 目录，把每个工作流注册成工具 `wf_<name>`，入参取自开始节点（id=100001）的 outputs |
| **DAG 拓扑执行** | 解析画布建图，从开始节点出发按边前传；变量按 Coze 的 ref 表达式解析；分支节点按端口选路 |
| **XML 写作 + JSON 执行** | 模型/用户写 `.xml`（CDATA 免转义），扫描时转成 Coze JSON，现有执行器全部保留 |

### 1.1 文件职责

```
src/
├── workflow.py            # 核心引擎：画布解析、变量解析、DAG 调度、节点处理器、自动注册
├── workflow_xml.py        # XML ↔ JSON 双向转换（xml_to_canvas / canvas_to_xml）
└── workflow_debug_tools.py # 热调试工具：debug_workflow / hotswap / rerun / list_outputs
```

---

## 2. Coze 画布数据结构

### 2.1 顶层结构

```json
{
  "nodes": [ ... ],     // 节点数组
  "edges": [ ... ],     // 边数组
  "versions": {}         // 版本信息（执行器不使用）
}
```

### 2.2 节点结构

```json
{
  "id": "100001",          // 节点 ID（字符串）；开始节点固定 "100001"，结束节点固定 "900001"
  "type": "1",             // 节点类型（字符串数字）
  "x": 100, "y": 200,      // 画布坐标（可选）
  "data": {
    "nodeMeta": { "title": "开始" },
    "inputs":  { ... },   // 节点输入配置（因 type 而异）
    "outputs": [ ... ]    // 节点输出声明（字段名/类型/schema）
  },
  // 复合节点（type 21/28）特有：
  "blocks": [ ... ],       // 子画布节点数组
  "edges":  [ ... ]        // 子画布边数组
}
```

### 2.3 边结构

```json
{
  "sourceNodeID": "100001",
  "targetNodeID": "300001",
  "sourcePortID": "true"   // 分支端口名；线性边为空字符串
}
```

### 2.4 固定节点 ID

| 常量 | 值 | 含义 |
|------|----|------|
| `ENTRY_ID` | `"100001"` | 开始节点（type 1） |
| `EXIT_ID` | `"900001"` | 结束节点（type 2） |

### 2.5 BlockInput（变量引用块）

每个输入参数的 `input` 字段是一个 BlockInput：

```json
// literal — 字面量
{ "type": "string", "value": { "type": "literal", "content": "hello" } }

// ref — 引用上游节点输出 / 循环变量 / 全局变量
{ "type": "string", "value": { "type": "ref", "content": { "source": "block-output", "blockID": "300001", "name": "output" } } }

// object_ref — 按字段组装对象
{ "type": "object", "schema": [ { "name": "x", "input": <BlockInput> }, ... ] }
```

---

## 3. 节点类型表

### 3.1 全部已实现节点类型

| type | 名称 | 处理器函数 | 说明 |
|------|------|-----------|------|
| 1 | 开始 (start) | `_passthrough`（调度器特判） | `data.outputs` = 工作流入参声明；`_bind_entry` 绑定外部入参 |
| 2 | 结束 (end) | `_passthrough`（调度器特判） | `_exit_result` 解析返回值；单键取值、多键转 JSON |
| 3 | LLM (llm) | `_handle_llm` | 渲染 prompt/systemPrompt，调用 LLM；outputs 声明自动转 JSON Schema 约束输出 |
| 4 | 插件 (plugin) | `_handle_plugin` | 按 toolName 匹配 Agent 工具箱，调用工具；支持声明 outputs 字段提取 |
| 5 | 代码 (code) | `_handle_code` | 沙箱执行 Python（subprocess + 临时文件）；`main(args)` 返回 dict |
| 8 | 选择器 (selector) | `_handle_selector` | 按分支顺序求值条件，命中端口 `true`/`true_{i}`，否则 `false` |
| 9 | 子工作流 (subworkflow) | `_handle_subworkflow` | `workflowId` 按本地 `.agent/workflows/` 匹配并执行 |
| 13 | 输出消息 (output) | `_handle_output_emitter` | 中途向用户输出内容（`ctx.emit` 推 workflow_message 事件） |
| 15 | 文本处理 (text) | `_handle_text` | concat（模板渲染）/ split（按分隔符切分） |
| 19 | Break | 复合体调度器判类型 | 循环内中断 |
| 20 | 循环赋值 (setvar) | `_handle_loop_setvar` | 循环内设置累加变量（写 `ctx.loop_vars`） |
| 21 | 循环 (loop) | `_handle_loop` | array（遍历 list）/ count（固定次数）/ infinite（直到 Break） |
| 22 | 意图识别 (intent) | `_handle_intent` | LLM 把 query 分到预设意图；端口 `branch_{i}` / `default` |
| 28 | 批处理 (batch) | `_handle_batch` | 对 list 每个元素跑子图，声明为 list 的 body 输出聚合成列表 |
| 29 | Continue | 复合体调度器判类型 | 循环内跳过本轮（解析 inputParameters 作为本轮输出） |
| 31 | 注释 | （忽略） | 不执行 |
| 32 | 变量聚合 (aggregator) | `_handle_aggregator` | 多分支汇合，取"实际执行到的那个"上游输出 |
| 40 | 变量赋值 (assigner) | `_handle_assigner` | 把 input 值写入 left 指向的全局变量 |
| 45 | HTTP (http) | `_handle_http` | method/url/headers/params/body/auth；URL/JSON 体支持 `{{变量名}}` 模板 |
| 58 | ToJSON (tojson) | `_handle_tojson` | 把 input 变量序列化成 JSON 字符串 |
| 59 | FromJSON (fromjson) | `_handle_fromjson` | 把 JSON 字符串解析成对象；解析失败降级返回原文 |

### 3.2 节点级批处理

任意普通节点（非 21/28）都可携带 `data.inputs.batch` 配置，启用后对该节点**逐元素执行**：

```json
{
  "enabled": true,
  "input": <BlockInput>,      // 数据源（解析为 list）
  "nth": 0,                    // 取第 n 个结果
  "filter": {                  // 筛选条件（复用 Selector condition 结构）
    "logic": 2,                // 1=OR, 2=AND
    "conditions": [ ... ]
  }
}
```

启用批处理时输出三组：`all_outputs` / `filtered_outputs` / `nth_output`。

### 3.3 复合节点子画布

type 21（循环）和 type 28（批处理）是复合节点，拥有自己的 `blocks`（子节点数组）和 `edges`（子边数组）。子画布通过 `-function-inline-output` 端口边找到迭代入口节点，每轮执行内部子图。

---

## 4. DAG 拓扑执行流程

### 4.1 调度核心：`execute()`

```python
def execute(canvas, inputs, *, tools, llm, emit=None, workspace=None,
            max_steps=1000, return_exit_dict=False) -> str
```

执行步骤：

```
1. 初始化 _Ctx（运行时上下文）
2. 解析 nodes/edges，找开始节点（ENTRY_ID=100001）
3. _bind_entry：把外部入参按开始节点 outputs 声明绑定 → ctx.node_outputs[ENTRY_ID]
4. _build_dag：构建拓扑索引
   - out_edges: sourceNodeID → [(targetNodeID, sourcePortID)]
   - pending_in: node_id → 未完成前驱数
   - aggregator(32)/exit(2) 节点 OR 语义：初值=1（任一前驱完成即可）
   - entry 后继 pending_in -1（视为已完成）
5. 初始就绪队列：所有 pending_in<=0 的节点
6. 循环调度（max_steps 防失控）：
   a. 从就绪队列取一个节点
   b. 若是 EXIT_ID → 返回 _exit_result
   c. 查 NODE_HANDLERS 找处理器；无则报错
   d. _run_node_with_batch：执行节点（含节点级批处理）
   e. 结果写入 ctx.node_outputs[current]
   f. 扇出：遍历出边，port 匹配 → pending_in[tid]--，<=0 则入就绪队列
   g. 节点报错 → error 端口（{node_id}_error），未声明 error 边则静默终止
7. 走完所有路径无 exit → 返回最后执行节点的输出
```

### 4.2 端口分支语义

分支节点（selector/intent）返回 `port` 字段，调度器据此选路：

| 节点类型 | 端口值 | 含义 |
|---------|--------|------|
| selector(8) | `true` / `true_{i}` / `false` | 第 i 个分支成立 / 都不成立 |
| intent(22) | `branch_{i}` / `default` | 第 i 个意图命中 / 无匹配 |
| 任意节点 | `{node_id}_error` 或 `error` | 节点执行报错（两种写法兼容） |

**端口匹配规则**：有 port 时严格匹配 `sourcePortID == port`；error 端口兼容 `{node_id}_error` 和统一 `"error"` 两种写法；无 port 时优先空端口、再取第一个。

### 4.3 汇聚语义

- **Aggregator(32)**：OR 语义，任一前驱完成即可继续（`pending_in` 初值=1）
- **Exit(2)**：多分支汇聚到 exit，任一路径到达即结束（`pending_in` 初值=1）
- **普通节点**：AND 语义，所有前驱完成才就绪

### 4.4 调试执行：`execute_debug()`

```python
def execute_debug(canvas, inputs, *, tools, llm, on_node,
                  emit=None, workspace=None, max_steps=1000)
    -> (exit_dict, order, trace)
```

与 `execute()` 逻辑一致，但每个节点执行**前后**回调 `on_node(event)`：

```json
{"phase": "start", "id": "100001", "title": "开始", "ntype": "1"}
{"phase": "end",   "id": "100001", "outputs": {...}}
```

返回三元组：`exit_dict`（结构化结果）、`order`（执行顺序）、`trace`（全量输出快照）。同时缓存到模块级 `_debug_ctx` 供热调试工具使用。

---

## 5. 变量引用 ref 机制

### 5.1 三种值类型

`resolve_value(block_input, ctx)` 解析 BlockInput：

| value.type | 解析方式 | 函数 |
|-----------|----------|------|
| `literal` | 直接取 `content` | — |
| `ref` | 按 `content.source` 查上游/循环/全局 | `_resolve_ref` |
| `object_ref` | 按 `schema[]` 逐字段组装 dict | `_resolve_object_ref` |

### 5.2 ref source 类型

| source | 含义 | 解析目标 |
|--------|------|---------|
| `block-output` | 上游节点输出 | `ctx.node_outputs[blockID]`，按 `name` 点号取子字段 |
| `loop-item` | 当前循环元素 | `ctx.batch_item`（name 空=整个 item，name=字段名取子字段） |
| `loop-index` | 当前循环索引 | `ctx.batch_index` |
| `global_variable_app` | 应用级全局变量 | `ctx.global_vars`，按 `path` 点号取 |
| `global_variable_system` | 系统级全局变量 | 同上 |
| `global_variable_user` | 用户级全局变量 | 同上 |

### 5.3 点号取值 `_dotted_get`

支持 `a.b.c` 逐层取值，还支持特殊属性：

- `.length`：返回 `len()`（适用 list/str/dict）
- `.is_empty`：返回 `len() == 0`（适用 list/str）
- list 下标：`items.0` → `items[0]`

### 5.4 模板渲染 `render_template`

把 `{{name}}` / `${name}` / `{{a.b}}` / `${a.b}` 替换为 params 中的值：

- dict/list 转 JSON 字符串
- None 转空串
- 同时支持 `{{}}` 与 `${}` 两种占位语法（先 `${}`，再 `{{}}`）

### 5.5 LLM 结构化输出

LLM 节点（type 3）声明了多字段/结构化 outputs 时：

1. `_outputs_to_json_schema` 把 outputs 声明转成 JSON Schema，并入 systemPrompt
2. 模型返回后 `_parse_structured_output` 尝试解析 JSON（截取首个 `{` 到末个 `}`，抗 markdown 围栏）
3. 按声明 type 强转字段值（`_coerce_field`：boolean/integer/number）
4. 解析失败降级回 `{output: 原文}`

---

## 6. 生命周期钩子

### 6.1 四种钩子位置

| hook | 触发时机 | 典型用途 |
|------|---------|---------|
| `before_turn` | 每轮对话开始前 | 历史检索、上下文注入 |
| `before_tool` | 工具调用前 | 参数预处理 |
| `after_tool` | 工具调用后 | 结果后处理（如写 .cs 后自动诊断） |
| `before_answer` | 生成回答前 | 知识检索、风格控制 |

> 向后兼容：`meta.auto: true` 且未显式设 `hook` 的工作流，视为 `before_turn`。

### 6.2 钩子工作流约定

钩子工作流的结束节点返回 `{inject, result, message}`：

```python
def run_hook(canvas, context, *, tools, llm, workspace=None) -> (inject, result, message)
```

| 返回值 | 含义 |
|--------|------|
| `inject=True` + `result` | 作 system 旁注喂主 LLM（注入语义） |
| `message`（无论 inject） | 发 workflow_message 事件到 UI，**不进主 LLM**（静默执行+系统通知） |

解析规则：
- 显式 end 返回 dict：`inject` 缺省按 `result` 非空推断；`result` 兜底 `output`
- dict 无 `inject` 键（旧式 `{output:x}`）：取唯一值，None/空 → 不注入
- 隐式 end/纯文本 → 尝试 JSON 解析；失败则整体当 result，inject=True（非空即注入）
- `inject` 可能以字符串 `'false'/'true'` 传来，按布尔语义归一化

### 6.3 相关函数

| 函数 | 作用 |
|------|------|
| `get_hook_workflows(workspace, hook)` | 返回所有声明在某 hook 位置的工作流 |
| `get_auto_workflows(workspace)` | [兼容] 等价 `get_hook_workflows(hook='before_turn')` |
| `run_hook(canvas, context, ...)` | 执行一个钩子工作流，返回 `(inject, result, message)` |

### 6.4 hidden 工作流

`meta.hidden: true` 的工作流不注册成 Agent 工具，仅供钩子/子工作流调用。

---

## 7. XML 双格式系统

### 7.1 为什么用 XML

模型直接写 Coze 画布 JSON 时，代码节点的 code、LLM 的 prompt 等字段是 **JSON 字符串里的字符串**，里面的双引号/花括号/换行/JSON 块要层层转义，极易出错（JSON 套 JSON）。

XML 用标签 + CDATA 包裹代码/模板块，**内部无需转义**：

```xml
<node id="500001" type="code">
  <in name="x" ref="100001.x"/>
  <code><![CDATA[
    async def main(args):
        return {"y": args.params["x"] * 2}   # 引号花括号随便写
  ]]></code>
  <out name="y" type="number"/>
</node>
```

### 7.2 落地策略

**XML 写作 + JSON 执行**：模型/用户写 `.xml`，扫描时 `xml_to_canvas()` 转成 Coze JSON，现有执行器/编辑器/Coze 互导能力全部保留。

### 7.3 双向转换

| 方向 | 函数 | 用途 |
|------|------|------|
| XML → JSON | `xml_to_canvas(xml_str)` | 扫描时加载 .xml 工作流 |
| JSON → XML | `canvas_to_xml(canvas, meta)` | 保存时序列化 |

### 7.4 节点 type 可读名 ↔ 数字

```python
TYPE_NAME_TO_NUM = {
    "start": "1", "end": "2", "llm": "3", "code": "5", "selector": "8",
    "text": "15", "loop": "21", "batch": "28", "intent": "22",
    "aggregator": "32", "assigner": "40", "http": "45", "subworkflow": "9",
    "plugin": "4", "tojson": "58", "fromjson": "59", "output": "13",
    "break": "19", "continue": "29", "setvar": "20",
}
```

XML 中 `type` 属性既可用可读名（`start`/`llm`/`code`...），也兼容数字。

### 7.5 ref 字符串编码

XML 用简洁的 ref 字符串代替 BlockInput 的嵌套 JSON：

| ref 字符串 | 解析为 |
|-----------|--------|
| `NODEID.field` | `block-output`，blockID=NODEID，name=field |
| `loop-item` | `loop-item`，name=""（整个 item） |
| `loop-item.field` | `loop-item`，name=field |
| `loop-index` | `loop-index` |
| `global:path` | `global_variable_app`，path=[path] |

### 7.6 CDATA 处理

`_text_block(el)` 取元素文本，ElementTree 把 CDATA 合并进 `text`，原样保留内部字符（换行/引号/花括号）。反向序列化时 `_cdata(text)` 包裹 `<![CDATA[...]]>`。

### 7.7 静默吞错检测 `_lint_node`

解析时检测常见"写法错误但解析器默默丢弃"的情况并 log warning（不阻断解析）：

| 节点类型 | 检测内容 |
|---------|---------|
| selector(8) | `<condition>` 错标签（应用 `<cond>`） |
| http(45) | `<param name="method/url/...">` 误写（应用 `<method>`/`<url>` 子元素） |
| aggregator(32) | `<variable>` 错标签（应用 `<var>`） |
| code(5) | `<param name="code">` 误写（代码会为空！必须用 `<code>` 子元素） |
| end(2) | `<param>` 无效（返回绑定用 `<out ref>`/`<in ref>`） |

### 7.8 复合节点子画布 XML 往返

`_read_composite_body(nd, node)` 从 `<blocks><node.../></blocks>` 和 `<edges><edge.../></edges>` 读回子图。`_composite_body_xml(n)` 反向序列化，递归调用 `_node_to_xml`。

---

## 8. 自动注册机制

### 8.1 每轮刷新 `refresh_workflow_tools`

```python
def refresh_workflow_tools(toolbox, workspace=None, agent=None) -> (ok_names, broken)
```

每轮对话调用：

1. `seed_default_workflows`：把随包附带的默认工作流（`src/workflows/*.xml`）播种到 workspace（存在则不覆盖）
2. `toolbox.drop(WF_PREFIX)`：清掉旧 `wf_*` 工具
3. `scan_workflows`：扫描 `.agent/workflows/` 下所有 `*.json` 和 `*.xml`
4. 对每个工作流：
   - `meta.enabled is False` → 跳过（禁用）
   - `meta.hidden is True` → 跳过（隐藏，不注册成 Agent 工具）
   - 有 error → 加入 broken 列表
   - 否则 `make_workflow_tool` 封装成 Tool → `toolbox.register_or_replace`

### 8.2 工作流 → Tool 封装 `make_workflow_tool`

```python
def make_workflow_tool(meta, canvas, path, agent) -> Tool
```

- 工具名：`wf_` + `_safe_name(meta.name 或 path.stem)`
- 工具描述：`meta.description`
- 入参 schema：`meta.inputs`（覆盖）或 `_entry_input_schema(canvas)`（开始节点 outputs 声明）
- 执行函数：调用 `execute(canvas, kwargs, tools=agent.tools, llm=agent.llm, ...)`
- 异常处理：`WorkflowError` → 返回失败文本；其他异常 → 返回出错文本，不炸 Agent 主循环

### 8.3 扫描 `scan_workflows`

```python
def scan_workflows(workspace=None) -> list[dict]
```

返回 `[{name, path, meta_path, meta, canvas, error, warnings}]`：

- **JSON 工作流**：`*.json`（排除 `.meta`），直接 `json.loads`
- **XML 工作流**：`*.xml`（排除 `.meta`），`xml_to_canvas()` 转 JSON；meta 优先根属性，`.xml.meta` 可覆盖
- 每个 item 调 `validate_canvas_detailed` 检查未支持的节点类型

### 8.4 用户脚本工具 `load_user_tools`

扫描 `.agent/workflows/tools/*.py`，把每个文件里**本模块定义**的顶层函数注册成工具：

- 跳过私有（`_` 开头）、`main`、import 进来的函数
- 支持模块级类型声明：
  - `INPUT_SCHEMA = {"参数名": "object|array|integer|...", ...}`
  - `OUTPUT_SCHEMA = [{"name": "字段", "type": "object", ...}, ...]`
- 有则优先于函数注解；都没有的参数回退 `str`

### 8.5 工作流管理工具

`make_workflow_mgmt_tools` 提供 `list_workflows` 工具，列出所有工作流及加载状态（✅可用 / ⚠️有误 / ⏸已禁用）。

---

## 9. 热调试系统

### 9.1 设计目标

配合 ws debug 后端，Agent 可**自驱动**编排迭代：跑工作流 → 看指定节点输出 → 热替换节点配置 → 单节点重跑验证——无需人工手动。

### 9.2 四个调试工具

| 工具 | 签名 | 作用 |
|------|------|------|
| `debug_workflow` | `(name, inputs="")` | 调试执行工作流，返回各节点输出摘要；缓存 ctx+画布到 `_debug_ctx` |
| `list_workflow_outputs` | `(node_ids="")` | 列出上次 debug 后指定节点的输出（截断 300 字防爆上下文） |
| `eval_node_output` | `(node_id, script)` | 对节点完整输出执行 Python 片段（变量 `output`），过滤/投影/提取 |
| `hotswap_workflow_node` | `(node_id, xml_fragment)` | 热替换节点配置（XML 片段 → `parse_xml_fragment` → 合并 data → 单节点重跑） |

### 9.3 模块级缓存 `_debug_ctx`

```python
_debug_ctx: dict = {}  # {canvas, nodes, edges, ctx}
```

`execute_debug` 执行后缓存，供热调试工具读取：

- `ctx.node_outputs`：各节点输出快照
- `nodes`：节点字典（热替换时修改 `data`）
- `canvas`/`edges`：完整画布

### 9.4 热替换流程

```
hotswap_workflow_node(node_id, xml_fragment)
  1. 从 _debug_ctx 取缓存的 ctx + nodes
  2. 找到旧节点 old = nodes[node_id]
  3. ET.fromstring("<node id=...>" + xml_fragment + "</node>")
  4. parse_xml_fragment(root, ntype) → 新 data
  5. old["data"] = {**old["data"], **new_data}  （合并）
  6. _run_node_with_batch(old, handler, ctx)  （单节点重跑）
  7. ctx.node_outputs[node_id] = 新输出
  8. 返回新输出摘要
```

---

## 10. 运行时上下文 `_Ctx`

```python
class _Ctx:
    node_outputs: dict[str, dict] = {}    # 各节点输出 {node_id: outputs_dict}
    global_vars: dict = {}                # 全局变量（assigner 写入）
    loop_vars: dict | None = None         # 当前循环的累加变量（LoopSetVariable 读写）
    batch_item: None                      # 当前批处理的 item（loop-item source 用）
    batch_index: None                     # 当前批处理的 index（loop-index source 用）
    tools: ToolBox                        # Agent 工具箱引用
    llm: LLMClient                        # LLM 客户端引用
    emit: callable = None                 # 事件回调（workflow_message）
    workspace: Path = WORKSPACE           # 工作目录
```

### 10.1 LLM 客户端缓存

`_get_llm(ctx, model_name)` 支持节点级选模型：空名返回 `ctx.llm`；有模型名则按 `config.get_profile` 创建新 `LLMClient`，缓存在 `ctx._llm_cache` 复用。

---

## 11. 错误处理策略

| 场景 | 策略 |
|------|------|
| 节点执行报错 | 写 `{_error: ...}` 到 node_outputs，走 error 端口；未声明 error 边则静默终止，不阻塞并行分支 |
| LLM 调用异常 | 配了 `onError` → 返回反馈文本，不中断；未配 → 抛异常 |
| FromJSON 解析失败 | 降级返回原文，不崩溃工作流 |
| 代码节点未产出结果 | 报 `WorkflowError`（含 stderr 尾部 500 字） |
| 子工作流未找到 | 报 `WorkflowError`（提示按 name/文件名匹配） |
| 工具未在工具箱找到 | 报 `WorkflowError` |
| 工作流执行失败（工具层） | 返回 `[工作流 wf_xxx 执行失败] ...` 文本，不炸 Agent 主循环 |

---

## 12. 关键设计决策

1. **XML 优先写作格式**：解决 JSON 套 JSON 转义地狱，CDATA 让代码/模板内部无需转义
2. **DAG 拓扑调度（非线性遍历）**：支持扇出（一个节点多后继）+ 汇聚（多前驱）+ 端口分支，比线性 `_next_node` 更强大
3. **OR 汇聚语义**：aggregator/exit 初值=1，任一前驱完成即可继续，适配多分支汇聚场景
4. **节点级批处理**：任意节点都可带 batch 配置，输出 all/filtered/nth 三组，与复合节点(28)的批处理互补
5. **LLM 结构化输出**：outputs 声明自动转 JSON Schema 约束模型，解析后按声明 type 强转，保证下游 selector 按强类型判断
6. **热调试自驱动**：Agent 用 debug_workflow → list_workflow_outputs → hotswap_workflow_node 循环迭代，无需人工介入
