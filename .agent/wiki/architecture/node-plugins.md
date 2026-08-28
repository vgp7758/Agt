# 节点插件化（Node Plugins）

> 节点类型从内置代码外置为「同目录同名 `.py` + `.js` 配对」脚本——写两个文件即得全新节点类型，零框架改动。
> 首批迁移：text(15)/tojson(58)/fromjson(59)；验收节点：timestamp(N1)；新节点示范：AND / OR 逻辑节点（v0.19.2）。
> 打包现状（v0.19.2 wheel 实测）：节点插件共 **12 组（24 个文件：12 py + 12 js）**，pip 安装即有。

## 目录约定（三级，同名 type 后扫覆盖先扫）

| 目录 | 用途 | 优先级 |
|---|---|---|
| `src/assets/nodes_builtin/` | 随包核心插件（pip 安装即有） | 最低 |
| `nodes/` | workspace 级 | 中 |
| `.agent/nodes/` | 用户/Agent 私有（扩展面） | 最高 |

同名 type 后扫覆盖先扫——用户在 `.agent/nodes/` 放同名文件即定制覆写内置节点。

**已迁移清单**（src/assets/nodes_builtin/，共 12 组）：
- 第一批：text(15) / tojson(58) / fromjson(59)
- 第二批：llm(3) / code(5) / selector(8) / intent(22) / aggregator(32) / assigner(40) / http(45)
- 第三批（v0.19.2）：**AND / OR 逻辑节点**——selector 同构条件组（条件组 × operator），输出聚合 bool + 每组结果，`eval_condition_lenient` 未设置恒真语义（详见 [workflow-hooks · 13 类节点速查](workflow-hooks.md#13-类节点速查)）；以纯插件形态交付，是「写两个文件即得新节点」路线的最新示范
- **核心内置（不可覆写）**：start(1) / end(2) / loop(21) / batch(28) / loop-setvar(20) / subworkflow(9) / plugin(4) / output(13)——调度器协议层

## 后端约定（.py）

```python
"""MyNode 节点插件（type N2）：一句话描述。"""
from workflow_node_api import resolve_value   # SDK 稳定依赖面

def _my_handler(node: dict, ctx) -> dict:
    # node: 画布节点（data.inputs.inputParameters 等）
    # ctx: 运行时上下文（node_outputs/batch_item/loop_vars/emit/llm/tools）
    # 返回: {"outputs": {字段名: 值}, "port": 端口名或 None}
    return {"outputs": {"output": "hello"}, "port": None}

def agt_node():
    return {"type": "N2", "label": "MyNode", "handler": _my_handler}
```

### SDK（`workflow_node_api.py`）

插件 handler 只应 import 本模块——框架内部函数改名不波及插件：
- `resolve_value(input_spec, ctx)` — 解析输入引用/字面量
- `resolve_input_params(inputParameters, ctx)` — 批量解析输入参数 → dict
- `render_template(template_str, params)` — `{{字段}}` 模板渲染
- `_dotted_get(obj, path)` — 点号路径取值（`a.b.c`）
- `_try_parse(s)` — 字符串智能解析（int/float/bool/JSON）

### 核心类型保护

`CORE_TYPES = {"1", "2", "21", "28", "19", "29", "13"}`——这些 type 的调度器协议不容覆写，插件覆盖会被拒绝并告警。

## 目录条目动态聚合：catalog_entries()（2026-08，commit 17312eb）

**缺口**：`_NODE_CATALOG` 此前是 real_tools 里手维护的静态数组——插件节点的新类型（AND/OR/timestamp）**进不了目录**：`list_workflow_nodes` 看不到、`query_workflow_node` 查不到，只能手工回补 real_tools。

**方案：元信息跟实现走**（目录条目与 handler 同文件维护）：

```
各插件 agt_node() 声明 catalog 字段（模块级 _CATALOG：name/desc/xml 示例）
  → node_plugins.catalog_entries()（_LAST 为空时自举扫描一次，node_js_payload 同款模式）
  → real_tools._node_catalog() = 核心 11 条 + 插件条目 = 24 种
```

- **核心 11 条**留在 real_tools（start/end/loop/batch/subworkflow/plugin/output 等调度器协议层节点 + Break/Continue/Comment 循环体协议节点）
- **10 个已迁移节点**（llm/code/selector/intent/aggregator/assigner/http/text/tojson/fromjson）的目录条目从 real_tools 搬进各自 .py——改插件 handler 与目录示例同文件维护
- **AND/OR/timestamp** 目录条目新写（此前正是进不了目录的三类）
- **用户级 `.agent/nodes/`** 的 catalog 声明同样生效（三级目录扫描天然支持——用户定制节点也能被 list_workflow_nodes 看到）

**验证**：聚合 13 条插件条目（10 搬运 + 3 新增）全覆盖；`query_workflow_node("AND")` / `("N1")` / `("3")` 实测命中；xml 示例完整性检查通过。

**效果**：以后写新节点插件，目录自动跟着进——不再有「节点能用但目录查无此类」的暗区。

### 第三个消费端：编辑器描述段（/api/wf/nodes，2026-08）

`_node_catalog()` 此前两个消费端：`list_workflow_nodes`、`query_workflow_node`。2026-08 增第三个——**`GET /api/wf/nodes`**（server.py）：把 `{type: desc}` 喂给工作流编辑器画布的 hover tooltip / props 面板描述段（前端 `NODE_DESC` 全局）。效果：插件节点（AND/OR/timestamp 等）的 desc 在编辑器 UI 同步可见，改插件 `.py` 的 desc 自动跟上，不用手工回填前端。详见 [工作流编辑器 UX · 批次九](../features/editor-ux-improvements.md)。

### syncPluginOutputs 与聚合 index 协议端口（2026-08）

### syncPluginOutputs：插件 fixed 协议端口补齐（2026-08，commits 36045ee + 7607e9b）

**背景链**：① AND/OR 节点的 result/results 输出端口在「保存 → 重载」后消失——`canvas_to_xml` 的 `("8","AND","OR")` 共享分支只写 `<branch>` 不写 `<out>`，读侧同款不解析 outputs=[]，**XML 序列化层双向丢失**（执行完全不受影响——`_handle_and` 运行时返回 outputs dict，所以 e2e 测试全过而编辑器里端口消失，纯显示层的双向丢失，最难排查那种）；② 编辑器兜底 `syncPluginOutputs` 初版查 `NODE_TEMPLATES` 按名补端口——但内置模板混着**示例端口**（code 的 key0/key1/key2 只是新建模板的示例），用户删掉后重开又冒出来（用户报告的回归）。

**修复（三层 + 语义定稿）**：

| 层 | 内容 |
|---|---|
| **XML 读侧**（workflow_xml.py） | AND/OR 分支补 `out.extend(_out_to_json(...))`——`<out name="result" type="boolean"/>` 正确解析 |
| **XML 写侧**（workflow_xml.py） | AND/OR 分支补 outputs 写出——**往返幂等验证**（二次转换逐字节一致）；selector(8) 保持原样（输出=分支端口） |
| **编辑器兜底**（workflow_editor.html `syncPluginOutputs`） | 只补 `NODE_PLUGINS[type].defaults.outputs` 里标记 **`fixed: true`** 的协议端口（handler 固定返回的，如 AND 的 result/results——删了重开也该回来）；**不再查 NODE_TEMPLATES**（内置示例模板出局——用户删掉的示例端口不该复活） |

**fixed 语义**：插件 `.js` 的 `defaults.outputs` 项标记 `fixed:true` = **协议端口**（handler 固定返回、删了也该回来）；未标记 = 示例/配置驱动端口（用户可自由增删，保存后按名保留/删除）。`syncPluginOutputs` 在 openWf 时对插件节点按名补缺失的 fixed 端口（深拷贝防串改模板）。

**node 模拟四场景验证**：code 删后不复活 ✓ / AND 缺失时 result/results 补齐 ✓ / selector no-op ✓ / 不重复补 ✓。

### aggregator(32) 的 index 协议输出（2026-08，commits 974deb2 + 548ef91）

**用户需求（调试观测）**：聚合节点分组输出的同时补一个 **0-base 的 index 输出端口**——调试时观测每个分组具体是哪个端口拿到值了。

**语义定稿（两轮修正）**：初版实现为**分组级**（第一个值非空分组的序号）——但 extract_keywords 的聚合只有一个分组 Group1，index 恒 0（两个钩子都显示 idx=0，用户实测发现不对）。修正为**变量级**：`index` = **贡献值的变量在组内的序号**（0 起，全空 = -1）：

| 场景 | index |
|---|---|
| 提取方钩子先到（走 var1） | 1 |
| 等待方钩子（取走 var0） | 0 |
| 缓存直取（var2） | 2 |
| 等待超时（var0 贡献空表） | 0 |
| 全未执行 | -1 |

fallback 情况下变量序号跟随 chosen/fallback 的实际选择；分组名恰好叫 index 时让位不覆盖；多分组报第一个值非空分组的组内序号。PARAMS desc 同步变量级语义。

**分层实现**：handler（`_handle_aggregator` 计算 index）/ XML 读侧追加协议输出、写侧不落盘（分组输出仍由 `<group>` 驱动，手写 XML 不需声明 `<out name="index">`，往返幂等）/ 编辑器模板 + aggregator.js defaults 带 index（`fixed:true`）→ **存量工作流打开自动补齐** / 画布分组后画一行 index 端口（integer 青色，可拖线）+ 节点高度 +1 行 / `_outFieldOptions` 自动包含 index（选择器条件、变量连线都能选 `聚合节点.index`）。九场景验证全过（真实 4 变量组结构：提取=1 / 等待=0 / 缓存=2 / 超时空表=0 / 全未执行=-1 / 多分组 / 让位 / XML 往返幂等）。

## 全景对账：所有节点都插件化了吗（2026-08）

针对「所有节点都是插件了吗？有没有漏网的」做全量对账。结论：**目录 24 类 = 插件 13 + 引擎内置 11**——能外置的业务节点已全部插件化，剩下 11 类内置是调度器本体或其协议节点，不是遗漏。

| 层 | type | 数量 | 为何 |
|---|---|---|---|
| **✅ 插件**（nodes_builtin 扫描，`.agent/nodes/` 同名 type 可覆盖） | 3/5/8/22/32/40/45/15/58/59 + AND + OR + N1(timestamp) | 13 | 业务节点，写两个文件即得 |
| **🔒 调度器核心**（`NODE_HANDLERS` 内置 handler） | 1(start)/2(end)/21(loop)/28(batch)/20(loop-setvar)/9(subworkflow)/4(plugin)/13(output) | 8 | 引擎本体：walker 递归、ENTRY/EXIT 协议、pending_in 计数、`_WF_CTX` 注入，外置=拆调度器 |
| **⚙️ 循环体协议节点**（无独立 handler，walker 特判） | 19(Break)/29(Continue)/31(Comment) | 3 | 只存在于 loop/batch 子画布内部，逻辑内嵌 `_handle_loop`/`_handle_batch` |

**唯一真漏网：InputReceiver(30)——已清（commit 5b23bbc）**

核心目录曾挂一条「输入接收 (InputReceiver)」条目（`_CORE_NODE_CATALOG` 里 type 30），声称「暂停工作流等待外部输入」，但引擎侧 0 处实际引用：

- `NODE_HANDLERS` 无 `"30"`（`_handle_input_receiver` 是未注册的死代码，从不被调用）
- `_SUPPORTED_TYPES = set(NODE_HANDLERS.keys()) | {"1","2","19","29","31"}` 不含 30
- walker 不特判 30

用户照目录示例写出 type 30 只会得到「未支持的节点类型 30」，永远报错——纯误导。已从 `_CORE_NODE_CATALOG` 删除该条目：核心目录 12 → 11，全量目录 25 → 24。重启后 `query_workflow_node` 查不到 InputReceiver。

## 前端约定（.js）

```javascript
EdFW.register({
  type: "N2", label: "MyNode", icon: "🔧", category: "data",
  // 建节点模板（createBasicNode 用；新 type 必须有）
  defaults: { nodeMeta: {title: "MyNode"},
              inputs: {inputParameters: [{name:"input", type:"any"}]},
              outputs: [{name:"output", type:"string"}] },
  // 可选：画布体扩展（LLM 大框 / text 大框等）
  body(n, g) { /* 用 makeTextArea 等框架组件 */ },
  // 可选：高度贡献
  nodeH(n) { return 80; },
  // 可选：属性面板扩展段（在通用输入/输出段之后）
  prop(n) { return EdFW.propShell("MyNode", EdFW.ioTable(n,'in')); },
});
```

### EdFW 组件 API

| 组件 | 用途 | 对应内置函数 |
|---|---|---|
| `EdFW.ioTable(n, kind)` | 字段表（flex 行 + 值按钮/引用 + schema 编辑） | renderIOFields |
| `EdFW.batchConfig(n)` | 批处理模块 | renderBatchConfig |
| `EdFW.textArea(n)` | 大文本框（LLM prompt / text 模板） | makeTextArea |
| `EdFW.propShell(title, bodyHtml)` | 属性面板外壳 | — |

关键：内联 script 本就是全局作用域——插件 js 直接用 `findN/renderAll/makeTextArea` 等全局名，零解耦改造。

## 注入通道

`server.py` 的 `_inject_node_plugins(html)` 在启动时扫描 `.js` → 拼 `<script>` 块（带 `//# sourceURL=nodes/xxx.js`）注入 `</body>` 前。两页同注（编辑器 + debug），避免 debug 页遇到新 type 白块。无 EdFW 的页面（debug 页）先注入最小 shim（仅登记 TYPE_LABEL）。

## 热加载

- `/reload nodes`：后端 handler mtime 重扫 + 摘旧重挂（同 `/reload tools` 模式）
- 前端改 `.js` 需 Ctrl+F5（注入是启动时一次性的）

## `_import_fresh` 的 .pyc 坑

Windows 上 `importlib.util.spec_from_file_location` + `exec_module` 偶发不失效 `.pyc`（SourceFileLoader 的 mtime 比较在 NTFS 上不可靠）。解法：改用 `exec(compile(source, path, "exec"))` 直读源码——绕过所有 .pyc 缓存，mtime 变了必生效。模块名含 `mtime_ns`：同 mtime → 同名 → `sys.modules` 命中（零开销）；mtime 变了 → 不同名 → 新模块 → 强制重执行。

## 打包确认（v0.19.2）

发布时 wheel 内节点插件 **24 个文件（12 组 py+js 配对，含新的 AND/OR）** 已确认打包齐全——新加插件节点后建议在发布前核对 wheel 文件清单（`unzip -l`），避免「本地可用、装包缺文件」的发布事故。

## 相关

- [工作流引擎](workflow-engine.md) — 节点调度器 / `_Ctx` / `NODE_HANDLERS`
- [工作流引擎与钩子 · 13 类节点速查](workflow-hooks.md#13-类节点速查) — AND/OR 节点语义
- [工作流编辑器 UX](../features/editor-ux-improvements.md) — EdFW 咨询点的渲染细节
- [v0.19.2 发布记录](../releases/v0.19.2.md) — AND/OR 随该版发布
- [工具外置](../features/tool-externalization.md) — 同构的扫描/装配模式
