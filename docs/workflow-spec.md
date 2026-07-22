# 工作流 JSON 规范 & 节点说明

> Agt 工作流采用 **Coze 原生画布 JSON 格式**。本文档详细说明画布结构、节点类型、变量引用、连线规则及执行语义。
>
> **推荐用 XML 写工作流**（见第 0 章）：代码块/提示词用 CDATA 包裹，内部双引号/花括号/换行/JSON 块无需转义，系统读入时自动转成下文的 Coze JSON 执行。

---

## 0. XML 格式（推荐写作格式）

模型/用户手写 Coze 画布 JSON 时，代码节点的 `code`、LLM 的 `prompt` 是 JSON 字符串里的字符串，内部双引号/花括号/换行/JSON 块要层层转义（JSON 套 JSON），极易出错。Agt 支持 **XML 写作 + JSON 执行**：写 `.xml`，扫描时自动转成上文的 Coze JSON，执行器/编辑器/Coze 互导能力全部保留。

### 0.1 顶层结构

```xml
<workflow name="工作流名" description="描述" coze_url="..." auto="true" auto_param="query">
  <node id="..." type="..."> ... </node>
  ...
  <edge from="源id" to="目标id" port="分支端口(可选)"/>
  ...
</workflow>
```

`<workflow>` 根属性即 meta（`name`/`description`/`coze_url`/`auto`/`auto_param`/`enabled`），无需单独 `.meta` 文件（也可用 `.xml.meta` 覆盖）。固定节点 ID 同 JSON：开始 `100001`、结束 `900001`。

### 0.2 节点 type 名字 ↔ 数字

XML 中 type 用可读名字（也兼容数字）：

| 名字 | 数字 | 名字 | 数字 |
|------|------|------|------|
| start | 1 | intent | 22 |
| end | 2 | aggregator | 32 |
| llm | 3 | assigner | 40 |
| code | 5 | http | 45 |
| selector | 8 | subworkflow | 9 |
| text | 15 | plugin | 4 |
| loop | 21 | tojson/fromjson | 58/59 |
| batch | 28 | output | 13 |

### 0.3 通用子元素

- `<in name="x" ref="源id.字段名"/>` — 输入引用上游节点输出
- `<in name="x" literal="5" type="number"/>` — 输入字面量（integer/number 自动转数字，boolean 转 bool）
- `<out name="y" type="number" required="true" default="10"/>` — 声明输出/入参
- `<param name="prompt"><![CDATA[ ... ]]></param>` — LLM 的 prompt/systemPrompt 等（**CDATA 内免转义**）
- `<code><![CDATA[ ... ]]></code>` — 代码节点的 Python（**CDATA 内免转义**）
- `<edge from="A" to="B" port="true"/>` — 流程边；selector/intent 的分支边用 port（`true`/`true_1`/`false` 或 `branch_0`/`default`）

### 0.4 各节点 XML 写法

**start**（入参 = `<out>`）：
```xml
<node id="100001" type="start">
  <out name="name" type="string" required="true"/>
</node>
```

**end**（返回变量绑定：`<out ref>` 或 `<in ref>` 都认——Coze JSON 里本就是 inputs.inputParameters）：
```xml
<node id="900001" type="end">
  <out name="greeting" ref="130001.output"/>
</node>
```
> ⚠️ end 易错：返回绑定写在 `<out ref>`（或 `<in ref>`）的 **ref 属性**里，**不是** `<in ref>` 配一个空的 `<out>`（空 out → 返回值为空）。`terminatePlan` 自动为 `returnVariables`，**不要**写 `<param name="terminatePlan">`。

**llm**（`<in>` 供 `{{}}` 模板，`<param>` 是 prompt/systemPrompt/temperature）：
```xml
<node id="130001" type="llm">
  <in name="name" ref="100001.name"/>
  <param name="prompt"><![CDATA[用一句话招呼 {{name}}]]></param>
  <param name="systemPrompt"><![CDATA[你是友好助手]]></param>
  <param name="temperature" type="float" literal="0.9"/>
  <out name="output" type="string"/>
</node>
```

声明**多字段 outputs** 即得结构化输出：模型按 schema 输出 JSON，执行器按字段名解析（按 type 强转）成具名输出，下游直接引用，无需 code 节点 `json.loads`：
```xml
<node id="130001" type="llm">
  <in name="q" ref="100001.q"/>
  <param name="systemPrompt"><![CDATA[只输出 JSON：{"related": true, "keywords": ["..."]}]]></param>
  <param name="prompt"><![CDATA[{{q}}]]></param>
  <out name="related" type="boolean"/>   <!-- 下游 selector 可直接 left="130001.related" -->
  <out name="keywords" type="list"/>
  <out name="output" type="string"/>     <!-- 始终保留=原文，调试/降级兜底 -->
</node>
```
模型输出非 JSON 时自动降级为 `{output: 原文}`，具名字段缺失（下游引用得 None）。

**code**（代码用 `<code>` 子元素 + CDATA，引号/花括号随便写）：
```xml
<node id="500001" type="code">
  <in name="x" ref="100001.x"/>
  <code><![CDATA[
async def main(args):
    return {"y": args.params["x"] * 2, "info": f"double of {x}"}
  ]]></code>
  <out name="y" type="number"/>
</node>
```
> ⚠️ code 易错：代码**必须**用 `<code>` 子元素，**不是** `<param name="code">`（用 param 代码会为空，节点静默返回空——工作流能跑但 code 不执行，最难查）。

**plugin**（调工具箱里已注册的工具，toolName=工具名；工具原始返回固定存 `raw`）：
```xml
<node id="200001" type="plugin" toolName="web_search">
  <in name="query" ref="100001.q"/>
</node>
```
工具返回固定存 `raw`。若工具返回 **JSON 字符串**，可声明额外 `<out name="字段"/>` 自动从 raw 解析抽取；返回**纯文本**（如 web_search）则下游直接 `ref="节点id.raw"`，**不要**声明同名 out 试图抽取（抽不到 → null，下游拿到空）。

**selector**（条件分支）。两种写法都支持——属性紧凑、子元素直观（单条件还可直接写在 branch 上）：
```xml
<node id="800001" type="selector">
  <branch><cond op="13" left="100001.score" right="60"/></branch>      <!-- score > 60 → true -->
  <branch><cond op="15" left="100001.score" right="30"/></branch>      <!-- score < 30 → true_1 -->
  <branch/>                                                            <!-- else → false -->
</node>
```
`op`（数字）运算符完整表：`1`=等 `2`≠ `7`包含 `8`不含 `9`空 `10`非空 **`11`=布尔为真 `12`=布尔为假**（这两个**只看 left、不写 right**）`13`> `14`≥ `15`< `16`≤（3-6=长度比较）。`left`/`right` 是**属性**（`left="节点id.字段"`），`right` 为字面量或 `ref:节点id.字段`。

> 写法二选一：属性 `<cond op="13" left="NODE.field" right="60"/>`，或子元素 `<cond op="13"><left ref="NODE.field"/><right>60</right></cond>`（单条件还可直接写在 branch 上：`<branch op="11"><left ref="NODE.found"/></branch>`）。
> ⚠️ 易错（写错会被静默忽略，条件恒空）：① 标签是 `<cond>`（**不是** `<condition>`）；② `op` 是**数字**（**不是** `operator="True"` 文本）。端口：第 i 个 branch 成立 → `true`(i=0)/`true_{i}`(i>0)，都不成立 → `false`。

**aggregator**（多分支汇合：`<group>` 内 `<var ref>`；每个 group 自动产生同名输出，无需 `<out>`）：
```xml
<node id="320001" type="aggregator">
  <group name="result">
    <var ref="130001.output"/>
    <var ref="130002.output"/>
  </group>
</node>
```
> ⚠️ aggregator 易错：① group 内变量标签是 `<var>`（**不是** `<variable>`，写错则分组变量为空）；② 每个 group 自动产生同名输出，**不要**再写 `<out>`；③ `<var ref>` 的**字段名要对**——text 节点输出固定叫 `output`（引用写作 `节点id.output`，不是 `.result`）。

**intent**（`<intent name>` 每个意图一个分支端口 branch_0/branch_1…，default 兜底）：
```xml
<node id="220001" type="intent">
  <in name="query" ref="100001.query"/>
  <intent name="闲聊"/>
  <intent name="查询"/>
</node>
```

**text**（`<result>` 是 concat 模板，CDATA）：
```xml
<node id="150001" type="text">
  <in name="r" ref="200001.raw"/>
  <result><![CDATA[结果：{{r}}]]></result>
</node>
```

**http**（`<in>` 声明输入变量，URL/body 用 `{{变量名}}` 引用——同 text/llm；配置用 `<method>`/`<url>`/`<header>`/`<body>` 子元素）：
```xml
<node id="500001" type="http">
  <in name="stock_code" ref="300001.stock_code"/>
  <method>GET</method>
  <url><![CDATA[http://qt.gtimg.cn/q={{stock_code}}]]></url>
  <header name="Authorization" value="Bearer xxx"/>
  <body type="JSON"><![CDATA[{"q": "{{stock_code}}"}]]></body>
</node>
```
输出 `body`(string)/`statusCode`(integer) **自动生成**，无需 `<out>`。响应按 Content-Type 的 charset 解码，utf-8 失败自动回退 gbk（中文站点）。

> ⚠️ http 易错：① 配置用 `<method>`/`<url>`/`<header>`/`<body>` 子元素（**不是** `<param name="url">`）；② URL 引用上游值：先 `<in name="x" ref="上游.字段"/>` 桥接成变量 x，URL 写 `{{x}}`（**不是** `{{上游.字段}}` 直引）；③ 不要写 `<out>`（输出固定自动有）。

**subworkflow / tojson / fromjson / assigner / output**：见对应 JSON 章节，XML 子元素一一映射（subworkflow 用 `workflowId` 属性 + `<in>`）。

### 0.5 完整范例

```xml
<workflow name="double_xml" description="翻倍">
  <node id="100001" type="start">
    <out name="x" type="number" required="true"/>
  </node>
  <node id="500001" type="code">
    <in name="x" ref="100001.x"/>
    <code><![CDATA[
async def main(args):
    return {"y": args.params["x"] * 2}
    ]]></code>
    <out name="y" type="number"/>
  </node>
  <node id="900001" type="end">
    <out name="result" ref="500001.y"/>
  </node>
  <edge from="100001" to="500001"/>
  <edge from="500001" to="900001"/>
</workflow>
```

### 完整 XML 示例（覆盖 15 种节点类型）

以下 `full_demo.xml` 用 XML 写了一个"用户问题处理"工作流，把能自然串联的节点类型全部覆盖 —— 意图分流 → 各分支（代码计算、plugin 调工具转大写、fromjson 解析、text 格式化 → HTTP 查询 → fromjson 解析 → selector 条件分流 → output / llm）→ subworkflow 闲聊 → aggregator 汇合 → assigner 赋值 → tojson 序列化 → 结束：

```xml
<workflow name="workflow_demo" description="全节点类型演示：意图分流→各分支处理→聚合→序列化（格式演示；http/plugin/subworkflow 依赖外部资源）">
  <!-- 开始 -->
  <node id="100001" type="start" title="start" x="120" y="80">
    <out name="question" type="string" required="true"/>
  </node>
  <!-- 意图识别：计算/查询/闲聊 -->
  <node id="200001" type="intent" title="intent" x="420" y="80">
    <in name="query" ref="100001.question" type="string"/>
    <intent name="计算"/>
    <intent name="查询"/>
  </node>
  
  <!-- 分支0(计算)：code 算 → plugin 转大写 → fromjson 解析字段 → text 格式化 -->
  <node id="300001" type="code" title="code" x="720" y="80">
    <in name="question" ref="100001.question" type="string"/>
    <code language="3"><![CDATA[
async def main(args):
    q = args.params.get("question", "")
    return {"calc_result": f"输入长度={len(q)}"}
]]></code>
    <out name="calc_result" type="string"/>
  </node>
  <node id="310001" type="plugin" title="plugin" x="1020" y="80" toolName="to_uppercase">
    <in name="text" ref="300001.calc_result" type="string"/>
    <out name="raw" type="string"/>
  </node>
  <node id="315001" type="fromjson" title="fromjson" x="1320" y="80">
    <in name="input" ref="310001.raw" type="string"/>
    <out name="output" type="object"/>
  </node>
  <node id="320001" type="text" title="text" x="1620" y="80">
    <in name="r" ref="315001.output" type="string"/>
    <result><![CDATA[🧮 计算分支：{{r}}]]></result>
  </node>
  
  <!-- 分支1(查询)：http 查 → fromjson 解析 → selector 按字段分流 → output / llm -->
  <node id="400001" type="http" title="http" x="720" y="230">
    <method>GET</method>
    <url><![CDATA[https://httpbin.org/json]]></url>
  </node>
  <node id="410001" type="fromjson" title="fromjson" x="1020" y="230">
    <in name="input" ref="400001.body" type="string"/>
    <out name="output" type="object"><field name="slideshow" type="object"><field name="author" type="string"/><field name="date" type="string"/><field name="slides" type="list"/><field name="title" type="string"/></field></out>
  </node>
  <node id="420001" type="selector" title="selector" x="1320" y="230">
    <branch><cond op="7" left="400001.body" right="Yours"/></branch>
  </node>
  <node id="430001" type="output" title="output" x="1620" y="230">
    <content><![CDATA[✅ 查询命中演示作者]]></content>
  </node>
  <node id="440001" type="llm" title="llm" x="1620" y="380">
    <in name="question" ref="100001.question" type="string"/>
    <param name="prompt" type="string"><![CDATA[查询未命中。用一句话向用户说明，问题：{{question}}]]></param>
    <out name="output" type="string"/>
  </node>
  
  <!-- default(闲聊)：子工作流 greet_xml -->
  <node id="500001" type="subworkflow" title="subworkflow" x="720" y="380" workflowId="greet_xml">
    <in name="name" ref="100001.question" type="string"/>
    <out name="greeting" type="string"/>
  </node>
  
  <!-- 聚合：汇合四条路径（实际只一条执行，aggregator 取已执行者） -->
  <node id="600001" type="aggregator" title="aggregator" x="1908" y="76">
    <group name="answer"><var ref="320001.output"/><var ref="430001.output"/><var ref="440001.output"/><var ref="500001.greeting"/></group>
  </node>
  
  <!-- 赋值：设全局 processed=true -->
  <node id="610001" type="assigner" title="assigner" x="2220" y="80">
    <in name="status" path="processed" literal="true"/>
  </node>
  
  <!-- 序列化 -->
  <node id="620001" type="tojson" title="tojson" x="2520" y="80">
    <in name="input" ref="600001.answer" type="string"/>
    <out name="output" type="string"/>
  </node>
  
  <!-- 结束 -->
  <node id="900001" type="end" title="end" x="2820" y="80">
    <out name="result" ref="620001.output" type="string"/>
  </node>
  
  <!-- 流程边 -->
  <edge from="100001" to="200001"/>
  <edge from="200001" to="300001" port="branch_0"/>
  <edge from="200001" to="400001" port="branch_1"/>
  <edge from="200001" to="500001" port="default"/>
  <edge from="300001" to="310001"/>
  <edge from="310001" to="315001"/>
  <edge from="315001" to="320001"/>
  <edge from="400001" to="410001"/>
  <edge from="410001" to="420001"/>
  <edge from="420001" to="430001" port="true"/>
  <edge from="420001" to="440001" port="false"/>
  <edge from="320001" to="600001"/>
  <edge from="430001" to="600001"/>
  <edge from="440001" to="600001"/>
  <edge from="500001" to="600001"/>
  <edge from="600001" to="610001"/>
  <edge from="610001" to="620001"/>
  <edge from="620001" to="900001"/>
</workflow>
```

覆盖：start / intent / code / plugin / fromjson / text / http / selector / output / llm / subworkflow / aggregator / assigner / tojson / end（15 种）。插件节点输出 `raw` 后接 fromjson 解析成字段是常用模式（工具返回 JSON 字符串 → 解析出结构化字段供下游引用）。

> XML 只在读入时转 JSON；复合节点（loop/batch 的内部子画布）目前仍建议用 JSON 编辑器编辑。loop/batch 暂不支持 XML 描述内部 blocks。

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
- 输出：
  - 单个 `output:string`（默认 / 纯文本 LLM 节点）→ `{output: 文本}`（推理模型额外有 `reasoning_content`）
  - 多字段 / 结构化 outputs（任一字段非 `output:string`）→ 模型返回的 JSON **按字段名解析（按声明 type 强转）**展开成具名输出，并保留 `output`=原文；**解析失败降级回 `{output: 文本}`**。下游可直接 `ref="节点id.字段名"` 引用，selector 也可按强类型字段分流，无需再挂 code 节点 `json.loads`

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

---

## 12. Agent 工具 → 工作流节点映射

Agent 所有内置工具可作为工作流节点使用：属性面板点「🔧 工具名」或手写 JSON 时设 `type:"4"`、`data.toolName`。

### 工具输出自动推断

内置工具的 Python 函数有返回类型注解，创建工具节点时自动推断输出字段：

| 返回注解 | 输出字段 |
|---|---|
| `str` | `{name:"result",type:"string"}` |
| `int` | `{name:"result",type:"integer"}` |
| `float` | `{name:"result",type:"number"}` |
| `bool` | `{name:"result",type:"boolean"}` |
| `list` | `{name:"result",type:"list"}` |
| `dict` | `{name:"result",type:"object"}` |

### 工具 JSON 示例（join 拼接字符串）

```json
{
  "id": "130200",
  "type": "4",
  "data": {
    "nodeMeta": {"title": "join"},
    "toolName": "join",
    "inputs": {
      "inputParameters": [
        {"name": "items", "input": {"type": "list", "value": {"type": "ref", "content": {"source": "block-output", "blockID": "100001", "name": "words"}}}},
        {"name": "separator", "input": {"type": "string", "value": {"type": "literal", "content": ","}}}
      ]
    },
    "outputs": [
      {"name": "result", "type": "string", "description": "工具返回值"}
    ]
  }
}
```

### 输入字段约定

- `inputParameters` 字段名 = 工具参数名
- 类型映射：string→string, integer→integer, float→number, bool→boolean, list→list
- 值可选 **ref**（引用上游节点输出）或 **literal**（直接填值）
- 工具原始返回存 `raw` 字段；用户可声明额外输出字段，执行器从 raw JSON 解析抽取。
