# 节点插件化（Node Plugins）

> 节点类型从内置代码外置为「同目录同名 `.py` + `.js` 配对」脚本——写两个文件即得全新节点类型，零框架改动。
> 首批迁移：text(15)/tojson(58)/fromjson(59)；验收节点：timestamp(N1)。

## 目录约定（三级，同名 type 后扫覆盖先扫）

| 目录 | 用途 | 优先级 |
|---|---|---|
| `src/assets/nodes_builtin/` | 随包核心插件（pip 安装即有） | 最低 |
| `nodes/` | workspace 级 | 中 |
| `.agent/nodes/` | 用户/Agent 私有（扩展面） | 最高 |

同名 type 后扫覆盖先扫——用户在 `.agent/nodes/` 放同名文件即定制覆写内置节点。

**已迁移清单**（src/assets/nodes_builtin/，两批共 10 类）：
- 第一批：text(15) / tojson(58) / fromjson(59)
- 第二批：llm(3) / code(5) / selector(8) / intent(22) / aggregator(32) / assigner(40) / http(45)
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

## 相关

- [工作流引擎](workflow-engine.md) — 节点调度器 / `_Ctx` / `NODE_HANDLERS`
- [工作流编辑器 UX](../features/editor-ux-improvements.md) — EdFW 咨询点的渲染细节
- [工具外置](../features/tool-externalization.md) — 同构的扫描/装配模式
