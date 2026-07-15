# 工作流 JSON 规范 & 节点说明

> Agt 工作流采用 **Coze 原生画布 JSON 格式**。本文档详细说明画布结构、节点类型、变量引用、连线规则及执行语义。

---

## 1. 画布顶层结构

```json
{
  "nodes": [...],     // 节点数组
  "edges": [...],     // 流程边数组（执行顺序）
  "versions": {}      // 特性版本（如 {"loop":"v2","batch":"v2"}，可留空）
}
```

**固定节点 ID**：
- 开始节点 id 恒为 `"100001"`（type `"1"`）
- 结束节点 id 恒为 `"900001"`（type `"2"`）

---

## 2. 节点结构

```json
{
  "id": "130001",
  "type": "3",
  "data": {
    "nodeMeta": { "title": "LLM" },
    "inputs": { ... },
    "outputs": [ ... ]
  },
  "blocks": [...],   // 仅复合节点（Loop/Batch）有
  "edges": [...]     // 仅复合节点的内部边
}
```

| 字段 | 说明 |
|---|---|
| `id` | 节点唯一标识（字符串） |
| `type` | 节点类型（见下表） |
| `data.nodeMeta.title` | 显示标题 |
| `data.inputs` | 节点输入参数与配置（各类型不同） |
| `data.outputs` | 节点输出字段声明 `[{name,type,description,required,schema}]` |
| `data.blocks`/`data.edges` | 复合节点（Loop/Batch）的内部子节点和边 |

---

## 3. 变量引用表达式

节点的输入字段值通过 `input.value` 表达式描述，支持三种 `type`：

### 3.1 literal（字面量/模板）
```json
{ "type": "literal", "content": "你好 {{name}}" }
```
- `content` 为标量（string/int/bool）或字符串模板
- `{{name}}` 模板插值：引用**本节点的 inputParameters** 中声明的变量

### 3.2 ref（引用）
```json
{
  "type": "ref",
  "content": {
    "source": "block-output",
    "blockID": "100001",
    "name": "user.name"
  }
}
```

`source` 取值：

| source | 含义 | 必填字段 |
|---|---|---|
| `"block-output"` | 上游节点的输出变量 | `blockID` + `name` |
| `"global_variable_app"` | 应用级全局变量 | `path: ["varName"]` |
| `"global_variable_system"` | 系统全局变量 | `path` |
| `"global_variable_user"` | 用户全局变量 | `path` |
| `"loop-item"` | 批处理/循环的当前元素 | `name`（空=整个item，字段名=取子字段） |
| `"loop-index"` | 批处理/循环的当前索引（integer） | 无 |

`name` 支持**点号取子字段**：`"user.name"` → 从 `user` 字典取 `name`。

### 3.3 object_ref（逐字段组装）
当输入是 object，逐字段声明：
```json
{
  "type": "object",
  "schema": [
    { "name": "field1", "input": { ...BlockInput... } }
  ]
}
```

> `rawMeta` 是前端渲染元数据，**执行器忽略**。

---

## 4. 流程边（edges）

```json
{
  "sourceNodeID": "100001",
  "targetNodeID": "130001",
  "sourcePortID": ""
}
```

- 普通节点之间：`sourcePortID` 为空
- 分支节点（Selector/IntentDetector）：`sourcePortID` 标识走哪个分支出口

---

## 5. 节点类型全表

| type | 节点 | 说明 |
|---|---|---|
| `"1"` | Entry 开始 | `outputs` = 工作流入参 schema |
| `"2"` | Exit 结束 | `inputs.inputParameters` = 返回变量绑定 |
| `"3"` | LLM | prompt + systemPrompt + 输出 schema 注入 |
| `"5"` | Code 代码 | Python3 沙箱 |
| `"8"` | Selector 选择器 | 条件分支 |
| `"15"` | TextProcessor 文本 | concat/split |
| `"13"` | OutputEmitter 输出消息 | 中途向用户输出 |
| `"21"` | Loop 循环 | 复合节点，遍历执行 |
| `"22"` | IntentDetector 意图识别 | LLM 分类分流 |
| `"28"` | Batch 批处理 | 复合节点，逐元素聚合 |
| `"32"` | VariableAggregator 变量聚合 | 多分支汇合 |
| `"40"` | VariableAssigner 变量赋值 | 修改全局变量 |
| `"45"` | HTTPRequester HTTP 请求 | |
| `"9"` | SubWorkflow 子工作流 | 调用其他工作流 |
| `"4"` | Plugin 插件 | 调用 Agent 工具 |
| `"58"` | ToJSON | 序列化 |
| `"59"` | FromJSON | 解析 |
| `"19"` | Break | 循环中断（仅复合体内） |
| `"29"` | Continue | 循环继续（仅复合体内） |
| `"20"` | LoopSetVariable | 循环内修改变量 |
| `"30"` | InputReceiver | 交互输入（暂不支持工具模式） |
| `"31"` | Comment | 注释（忽略） |

---

## 6. 各节点详细规范

### Entry（开始）— type `"1"`
- **id 固定 `"100001"`**
- `data.outputs`：工作流入参 schema。外部调用时传入的参数绑定到这里。
```json
"data": {
  "outputs": [
    {"name": "name", "type": "string", "required": true},
    {"name": "age", "type": "integer", "required": false}
  ]
}
```

### Exit（结束）— type `"2"`
- **id 固定 `"900001"`**
- `data.inputs.terminatePlan`：`"returnVariables"`（键值返回，默认）/ `"useAnswerContent"`（模板渲染返回）
- `data.inputs.inputParameters`：返回字段绑定（通常 ref 到上游节点）
```json
"data": { "inputs": {
  "terminatePlan": "returnVariables",
  "inputParameters": [
    {"name": "result", "input": {"type": "ref", "content": {"source": "block-output", "blockID": "130001", "name": "output"}}}
  ]
}}
```

### LLM — type `"3"`
- `data.inputs.llmParam`：数组，每项 `{name, input}`，name 含 `prompt`/`systemPrompt`/`temperature`/`maxTokens`
- `data.inputs.inputParameters`：供 prompt 模板 `{{}}` 引用的变量
- `data.outputs`：**自动转为 JSON Schema 并并入 systemPrompt**，约束模型按结构输出
- 输出：`{output: 文本}`（推理模型额外有 `reasoning_content`）

### Code（代码）— type `"5"`
- `data.inputs.code`：Python 代码，`async def main(args) -> Output: ... return ret`
- `data.inputs.language`：`3` = Python3
- `data.inputs.inputParameters`：注入到 `args.params`
- 通过 `args.params['字段名']` 取输入，`ret = {...}` 返回
- 输出：`ret` 字典的字段

### Selector（选择器）— type `"8"`
- `data.inputs.branches`：分支数组，每项 `{condition: {logic, conditions: [{operator, left, right}]}}`
- `logic`：`1`=OR，`2`=AND
- `operator`：1=等 2≠ 7包含 8不含 9空 10非空 13> 14≥ 15< 16≤（+3-6长度比较 11/12布尔）
- **端口**：第 i 个分支成立 → `true`(i=0) / `true_{i}`(i>0)；都不成立 → `false`

### Loop（循环）— type `"21"`（复合）
- `data.inputs.loopType`：`array`(遍历list)/`count`(固定次数)/`infinite`(到Break)
- `data.inputs.loopCount`：count/infinite 的次数（BlockInput）
- `data.inputs.inputParameters`：array 模式下含 list 输入（每轮绑定为当前元素）
- `data.inputs.variableParameters`：循环累加变量初值
- `data.blocks`/`data.edges`：循环体子图
- 子图端口：入口 `loop-function-inline-output`，回边 `loop-function-inline-input`

### Batch（批处理）— type `"28"`（复合）
- `data.inputs.batchSize`/`concurrentSize`：批次大小/并发数
- `data.inputs.inputParameters`：含 list 输入（批处理源）
- 输出聚合：`xxx_list`（list 类型）

### IntentDetector（意图识别）— type `"22"`
- `data.inputs.intents`：`[{name}]` 预设意图
- `data.inputs.llmParam`：object 形式（modelName/temperature/prompt/systemPrompt）
- `data.inputs.inputParameters`：含 `query`
- 输出：`{classificationId, reason}`
- **端口**：命中第 i 意图 → `branch_{i}`；未命中 → `default`

### TextProcessor（文本）— type `"15"`
- `data.inputs.method`：`concat` / `split`
- concat：`concatParams[0]` 的 concatResult 模板渲染
- split：按分隔符切分 → list

### ToJSON/FromJSON — type `"58"`/`"59"`
- `inputParameters`：`[{name:"input", input}]`
- 58：变量 → JSON 字符串
- 59：JSON 字符串 → 按 outputs schema 解析的对象

### VariableAggregator（聚合）— type `"32"`
- `data.inputs.mergeGroups`：`[{name, variables: [BlockInput]}]`
- 多分支汇合，取"实际执行到的"上游输出

### VariableAssigner（赋值）— type `"40"`
- `data.inputs.inputParameters`：`[{name, left, input}]`
- `left` 指向全局变量（source: global_variable_app）
- `input` 为新值
- 输出：`{isSuccess}`

### HTTPRequester — type `"45"`
- `data.inputs.apiInfo`：`{method, url}`
- `data.inputs.headers`/`params`：参数 Param 数组
- `data.inputs.body`：`{bodyType, bodyData}`，bodyType = EMPTY/JSON/FORM_DATA/FORM_URLENCODED/RAW_TEXT
- URL 支持 `{{block_output_ID.field}}` 模板
- 输出：`{body, statusCode, headers}`

### SubWorkflow（子工作流）— type `"9"`
- `data.inputs.workflowId`：目标工作流名（按本地 `.agent/workflows/` 名匹配）
- `data.inputs.inputParameters`：传给子工作流 Entry 的入参

### Plugin（插件）— type `"4"`
- `data.inputs.toolName`：Agent 工具箱中的工具名
- `data.inputs.inputParameters`：调用参数
- 输出：`{raw: 原始返回}` + 用户声明的字段（从 raw 解析抽取）

### OutputEmitter（输出消息）— type `"13"`
- `data.inputs.content`：消息模板
- 推送 `workflow_message` 事件给用户

### LoopSetVariable — type `"20"`
- `data.inputs.inputParameters`：`[{left, right}]`
- `left` ref 指向循环变量名，`right` 为新值

---

## 7. 节点级批处理（任意节点）

除 Entry/Exit 外，任何节点可在 `data.inputs.batch` 配置批处理：

```json
"batch": {
  "enabled": true,
  "input": { "type": "list", "value": {"type": "ref", "content": {"source":"block-output","blockID":"100001","name":"items"}} },
  "itemType": "object",
  "filter": { "logic": 2, "conditions": [
    {"operator": 14, "left": {"input":{"type":"integer","value":{"type":"ref","content":{"source":"block-output","blockID":"__batch_output__","name":"score"}}}},
     "right": {"input":{"type":"integer","value":{"type":"literal","content":80}}}}
  ]},
  "nth": 0
}
```

开启后节点对数组逐元素执行，输入字段可用 `loop-item`/`loop-index` 引用。输出三组：
- `all_outputs`（list）：所有结果
- `filtered_outputs`（list）：非 null 且满足 filter 的
- `nth_output`：filtered 的第 nth 个（负/越界取最后；空则 null）

---

## 8. .meta 旁车文件

`<名>.json.meta` 提供编辑器/Agent 元数据：

```json
{
  "name": "greet",
  "description": "打招呼工作流",
  "enabled": true,
  "coze_url": "https://www.coze.com/...",
  "auto": false,
  "auto_param": "query"
}
```

| 字段 | 作用 |
|---|---|
| `name` | 工具名 → `wf_<name>` |
| `description` | Agent 据此判断是否调用 |
| `enabled` | false 则跳过 |
| `coze_url` | 编辑器「Coze」按钮打开的链接 |
| `auto` | true = 自动工作流（Agentic RAG） |
| `auto_param` | 自动执行时传入的参数名（默认 query） |

---

## 9. 复合节点子画布

Loop/Batch 的 `blocks`/`edges` 构成子图。在可视化编辑器中**双击复合节点**进入子画布编辑。

子画布内自动生成控制点：
- **迭代入口**（type 1）：输出 `item`/`index`/本地变量
- **中断(Break)**（type 19）：跳出循环
- **继续(Continue)**（type 2）：本轮结束，下一轮

本地变量通过 `data.inputs.variableParameters` 声明，子图内用 LoopSetVariable(type 20) 修改。

---

## 10. 类型系统

| JSON 类型 | Python 类型 | 说明 |
|---|---|---|
| string | str | |
| integer | int | |
| float / number | float | |
| boolean | bool | |
| object | dict | 可展开子字段（schema 数组） |
| list | list | 选 item 类型；item 为 object 再展开 |
| file | str | 文件路径 |
| time | str | 时间 |

object/list 字段在编辑器可展开为子端口单独连线，引用用点号路径（`user.name`）。

---

## 11. 完整示例

```json
{
  "nodes": [
    {"id": "100001", "type": "1", "data": {"outputs": [
      {"name": "name", "type": "string", "required": true}
    ], "trigger_parameters": []}},
    {"id": "130001", "type": "3", "data": {"inputs": {
      "inputParameters": [
        {"name": "name", "input": {"type": "string", "value": {"type": "ref", "content": {"source": "block-output", "blockID": "100001", "name": "name"}}}}
      ],
      "llmParam": [
        {"name": "prompt", "input": {"type": "string", "value": {"type": "literal", "content": "跟 {{name}} 打个招呼"}}}
      ]
    }, "outputs": [{"name": "output", "type": "string"}]}},
    {"id": "900001", "type": "2", "data": {"inputs": {
      "terminatePlan": "returnVariables",
      "inputParameters": [
        {"name": "result", "input": {"type": "ref", "content": {"source": "block-output", "blockID": "130001", "name": "output"}}}
      ]
    }}
  ],
  "edges": [
    {"sourceNodeID": "100001", "targetNodeID": "130001", "sourcePortID": ""},
    {"sourceNodeID": "130001", "targetNodeID": "900001", "sourcePortID": ""}
  ],
  "versions": {}
}
```
