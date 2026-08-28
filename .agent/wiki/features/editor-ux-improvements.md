# 工作流编辑器 UX 改进 · 多轮打磨

> **批次一（v0.18.7，2026-08-22，批次提交 `a634f83`）**：LLM 画布提示词框、批处理配置区上移、批处理输出自动管理、item 结构自动推断。
> **批次二**：字段行 flex 与紧凑编辑、子字段按钮行内统一归位、代码节点两处、属性面板加宽。
> **批次三（v0.19.2）**：批处理数组源可连线端口、LLM 画布 SYSTEM/PROMPT 双框 + 高度自适应、spec 浮层抽屉、LLM 输出格式下拉修复。
> **批次四（`29eb760`）**：属性面板 ✕ 关闭按钮无反应修复（selNode 选中态未清）。
> **批次五（`6bd7fa1`）**：触屏拖节点根因修复（mousemove 派发到 svg 而非死元素）+ 连线编辑下拉组（手机连线替代入口，两个默认折叠组）。
> **批次六（`50fce6e`）**：流程连线「入口」只能加不能删修复——占位边 onchange 死区（入边行 ✕ 按钮 + 首选项文案按边状态区分）。
> 均为纯前端改动，Ctrl+F5 刷新编辑器即见新 UI，无需 /restart。

## 编辑器页面层级（当前）

| 页面 | 职责 |
|------|------|
| `src/static/workflow_editor.html` | **编辑**——画布 + 右侧属性面板（本页改动落点） |
| `src/static/workflow_debug.html` | **调试**——播放后节点下挂输出白框（可折叠），见[工作流调试页](workflow-debug.md) |

v0.18.7 时期的编辑页路径为 `src/static/editor.html`（发布记录沿用旧路径，现为 workflow_editor.html）。两页画布渲染逻辑同源（nodeH / _baseH / 端口锚点），一侧改动画布布局时注意同步另一侧（见 [workflow-debug 注意事项](workflow-debug.md#注意事项)）。

## 批次一（v0.18.7，批次提交 a634f83）

### 1. LLM 节点画布直编 prompt

`nodeH` 对 `type==='3'` 加 `TEXTAREA_H=120`（与文本节点同款）；`renderNode` 渲染 `makeLLMPromptArea(n)`。

关键实现：
- `makeLLMPromptArea` 从 `llmParam` 取 `prompt` 参数（无则惰性创建），textarea `oninput` **直改数据不重绘**——重绘会销毁 textarea 丢焦点
- placeholder 提示 `{{输入字段}}` 占位符 + "systemPrompt/模型在右侧面板"
- systemPrompt / model / thinking / timeout / onError 仍走右侧属性面板（`setLLM` 系列）

### 2. 批处理配置区上移

`showProps` 中 `renderBatchConfig(n)` 从输入字段之后移到之前——批处理开关影响输入字段的"源"选项（item.字段）与输出结构，先看到开关再编辑字段更符合操作顺序。

### 3. 批处理输出自动管理

批处理 `enabled` 时，右侧面板「输出字段」区隐藏编辑，替换为说明文字：
> 批处理已启用：all_outputs / filtered_outputs / nth_output 自动管理（item 结构=节点原输出 schema），关闭批处理恢复原输出编辑。

底层机制（`setBatch('enabled')`，此前已有）：`_origOutputs` 备份原输出 → 生成三输出（list/list/object，schema=nthSchema）→ 关闭时还原。

### 4. item 结构自动推断展示

`renderBatchConfig` 删除手动 `item 类型` 下拉（`setBatch('itemType')` 不再有 UI 入口），改为只读展示：

```
item 结构（自动）object（content, tool_calls, usage）  ← object 时列字段名
item 结构（自动）string                                ← 基础类型直接显示
```

来源 = `nth_output.schema`（= 节点原输出 schema，含 plugin 节点按工具 schema 补全的字段）。三字段在右侧面板无需手动设置。

## 批次二：字段行与子字段编辑统一

主线索：**行内化**——把散落在块底、换行堆叠的按钮与控件收进字段行内，结构编辑一眼可见。全部落在 `src/static/workflow_editor.html` 右侧属性面板（`showProps` 字段表）与画布。

### 5. 字段行 flex 布局与紧凑编辑

- 字段行改 **flex 布局**：值按钮、引用选择器（下拉）行内紧凑排布，不再换行堆叠
- 字段表 `border-spacing: 3px`——间距收紧后仍保留行间呼吸感
- **required 复选框与描述同排**（原先分离/换行）
- 属性面板加宽 **330 → 440px**；引用下拉 `max-width: 60%`——长引用名不撑爆面板，其余控件照常同行

### 6. 子字段按钮行内统一归位

- **删除**子字段块底部按钮区及其遗留占位，旧「子字段」独立按钮删除
- 字段行内按钮统一归位：**[名][类型][📋][+][×]**——名称、类型、📋（JSON 导入/结构编辑）、+（加子字段）、×（删行）
- 📋 JSON 导入与 + 子字段按钮**上移至字段行内**，覆盖三重维度：in/out 两侧、object 与 `list<object>`、嵌套层（子字段的子字段同款行内按钮）
- **输出字段 object 也给 📋/+ 编辑结构**——结构编辑不再限于输入侧

效果：object/list<object> 嵌套结构的编辑入口全部内聚在字段行，不再需要滚动到块底找按钮。

### 7. 代码节点两处

- **代码框行高自适应**：`rows = max(6, 行数)`——短代码不再空一大块，长代码不憋在固定高度里滚动
- **画布灰字预览摘要删除**：type5 节点画布上不再显示代码灰字摘要（对应 `_baseH` 的「type5 代码预览 +40」分支移除）；调试页共享同款画布逻辑，同步提醒见 [workflow-debug 注意事项](workflow-debug.md#注意事项)

## 批次三（v0.19.2：连线端口 + 双框 + 抽屉）

主线索：**少打字**——能拖线的不再手填字符串，画布上能看到的不藏进面板。

### 8. 批处理数组源可连线端口

批处理的「数组源」原先必须手填 `blockID.name` 引用字符串——现在批处理节点提供**可连线输入端口**，从上游节点的 list 输出直接拖线接入，免手填；连线引用与手填字面量同通道解析。

### 9. LLM 画布 SYSTEM/PROMPT 双框 + 高度自适应

LLM 节点画布在 prompt 框（§1）基础上增加 **systemPrompt 框**——SYSTEM 与 PROMPT 双框同屏直编，systemPrompt 不再必须开右侧属性面板；两框**高度按内容自适应**（短提示不空占、长提示充分展示）。

### 10. spec 浮层抽屉

spec / 结构编辑（📋）从弹窗改为**浮层抽屉**——默认折叠不占视口，右上角图标唤出，编辑时仍可看到画布上下文。

### 11. 修复：LLM 输出格式下拉切换不生效

右侧属性面板切换 LLM「输出格式」下拉后**静默丢失**——保存路径（lpSet）未携带该字段，切了等于没切、无任何报错。修复：补上通用补建路径，下拉切换即写回生效。

## 批次四（`29eb760`）

### 12. 修复：属性面板 ✕ 关闭按钮无反应

**现象**：点击右侧属性面板 ✕ 关闭，面板原样还在，按钮看似无反应。用户诊断正确——节点仍处于选择态。

**根因**：关闭与显隐走的是**两个不同的状态变量**：

```javascript
// 原版 closeProps 只清了连线选中态：
function closeProps(){selEdge=null;showProps();renderAll();}

// 而 showProps 的显隐判定看 selNode：
function showProps(){
  if(!selNode){wrap.style.display='none';...return;}  // selNode 有值就继续渲染
  ...
```

点 ✕ → `closeProps` 清 `selEdge` → `showProps()` 里 `selNode` 仍指向被选节点 → 走渲染分支，面板**立即原样重渲染**——净效果为零。

**修复**（`src/static/workflow_editor.html`）：

```javascript
function closeProps(){selNode=null;selEdge=null;showProps();renderAll();}
//                    ^^^^^^^^^^^ 清掉选中态 → showProps 走隐藏分支
```

连带效果：关闭后画布上该节点的选中高亮框随 `renderAll()` 一并消失——**选中态与面板状态保持一致**（此前面板关不掉+高亮在，本就是一对不一致状态）。

Ctrl+F5 强刷编辑器即生效（编辑器 HTML 服务启动时读入内存，若 /restart 过则已带修复）。

## 批次五（`6bd7fa1`）：触屏可用性两件套

主线索：**移动端**——拖节点终于能动，连线不再依赖手势。

### 13. 触屏拖节点失效：合成事件派发到死元素（根因修复）

**现象**：手机上单指拖节点始终拖不动（touch shim 已在，tap/平移表面正常，唯独节点拖拽无效）。

**根因链**（此前多轮修 touch shim 都没挖到这一层）：

```
触摸节点 → touchstart 合成 mousedown → g.onmousedown 执行
  → 其中 renderAll()（选中高亮）→ svg.innerHTML='' 销毁重建【全部】DOM
  → _tap.target 指向的旧元素已脱离文档（死元素）

后续 touchmove → 合成 mousemove 派发到【死元素】
  → 事件冒泡只到分离树，永远到不了 svg 上的 mousemove 监听器
  → dragNode 设置了但坐标永远不更新 → 拖不动
```

**为什么真实鼠标没这个问题**：mouse 事件的 target 是浏览器实时命中的**新 DOM**，每次都冒泡到 svg；而合成事件派发到缓存的旧引用（`_tap.target`），那次 `renderAll()` 之后这条引用指向的元素已不在文档里，派发出去的冒泡链是断的。

**修复（一行）**：mousemove 改派发到 `svg` 本身——svg 的 mousemove handler 只读 `clientX/clientY`（`svgPos(e)`），不依赖 target，派发到 svg 完全等价。

同处 touch shim 细节：`_tap.moved`（位移 >8px）才 `preventDefault()`——拖节点/连线/平移时阻止页面滚动，tap 静止不拦（保留轻扫退出等系统手势）。

> 教训：**给"会重绘全部 DOM 的页面"写合成事件 shim，永远把事件派发到稳定的容器（svg/document），别派发到命中的元素引用**——renderer 随时可能把 target 换掉。

### 14. 连线编辑下拉组：手机连线替代入口（默认折叠）

**背景**：拖拽连线在触屏上基本不可用。方案（用户提出）：白线输入端口用下拉框从上游流程线输出端口列表里选，才是最稳的；PC 上不太需要 → 默认分组折叠。

选中任意节点，属性面板**最顶**（批处理配置之前）出现两个**默认折叠**的组：

| 组 | 内容 |
|---|---|
| 🔗 **流程连线**（白色 summary） | 本节点每个出口一行下拉：主出口 / 错误出口 / selector 每个分支 / intent 每个意图 + 默认——选项 = 全部其它节点（排除开始节点），选（无）= 断开；入口侧另有「＋入口」追加入边行（见 §15） |
| 🔗 **变量连线**（蓝色 summary） | 每个输入字段一行下拉：选项 = 所有上游节点的输出字段（**同类型过滤** + object 子字段点号路径）+ item/index，选（字面量）= 清引用 |

关键实现（`renderFlowLinks` / `renderVarLinks` / `setFlowLink` / `setVarLink`，均在 `src/static/workflow_editor.html`）：

- **setFlowLink 单选语义**：先清空该出口的全部边再按需 push——分支端口对齐拖线「每分支一条」的覆盖语义；主出口牺牲多下游表达（多下游仍可用拖线实现）换 UI 一致性。数据直接改 `WF.edges`，与拖线完全等价
- **setVarLink 复用 `quickSetRef` 全套语义**（类型跟随）——与拖线 / 字段行 ▾ 引用选择**完全同一条代码路径**，三条入口不会漂移
- 出口枚举：分支类节点（selector/intent 等）只展开各分支端口，不给通用主/错误出口行

验证：`node --check` + 14 项结构断言全过。**Ctrl+F5 强刷编辑器**后手机实测：单指拖节点可动；连线展开属性面板折叠组，纯下拉选择，零手势要求。

## 批次六（`50fce6e`）：连线入口删除修复

### 15. 修复：流程连线「入口」只能加不能删（占位边 onchange 死区）

**现象**：§14 流程连线组的入口侧，「＋入口」加出来的行删不掉；出口侧正常。用户诊断正确——"入口只能加不能删"。

**根因**（onchange 空值死区）：

```
＋入口 push 空源占位边（sourceNodeID=""）→ 渲染出的 select 停在首选项（value=""）
  → 用户想删：选「（断开此连线）」——但当前值已经是 ""，onchange 不触发
  → 真实边可以删（从有值选回空会触发 onchange），占位边删不掉 ❌
```

onchange 只在值**变化**时触发——占位边的当前值与「断开」选项值同为空串，选它等于没变值，事件永远不来。

**修复**（`src/static/workflow_editor.html`）：

- **每行入边右侧补 ✕ 按钮**：`delFlowInLink(i)` 直接 `WF.edges.splice(i,1)`——显式删除，不依赖 onchange：

```
[开始节点 · 主出口 ▾]  ✕
[选择器 · 分支2 ▾]     ✕
＋ 入口
```

- **首选项文案按边状态区分**：占位边显示「（选择上游出口…）」、真实边显示「（断开此连线）」——语义不再混淆（此前占位边也显示"断开"但点了没反应，正是用户困惑的来源）

验证：`node --check` + 4 项结构断言全过。**Ctrl+F5 强刷编辑器**即生效。

> 教训：**把"选回空值"当唯一删除路径的 UI 存在空值死区**——当前值已等于空目标时 onchange 不触发；删除操作必须给显式按钮/入口。

## 批次七（`a667da4`）：减法两件——钩子入口移除 + hidden 默认翻转

主线索：**减法**——同一件事不留两个入口；默认值按多数真实用途取。两项均为用户提案。

### 16. 钩子配置入口移除（迁 /agents 管理页）

顶栏钩子下拉（fhook）+ onHookChange + HOOK_INPUTS（约 30 行）删除。钩子挂载点（before_turn/turn_end/…）描述的是 **Agent 声明的行为**而非工作流自身结构——它住在 Agent 声明的 hooks 里（/agents 管理页的 hooks 编辑），编辑器里那份是重复入口。存量钩子工作流的 `meta.hook` 原值透传（保存不丢字段），`get_hook_workflows` 读侧不受影响。

### 17. hidden 复选框语义翻转（默认勾选 = hidden）

| 状态 | 之前 | 现在 |
|---|---|---|
| 未写 hidden（新建工作流） | 注册为 wf_* 工具 | **hidden（不注册）** |
| 显式 hidden="false" | 注册 | 注册（取消勾选 + 保存） |

复选框默认勾选（勾=hidden 不进工具箱）；取消勾选并保存 = 显式 `hidden="false"` → 注册进 Agent 工具箱 schema。写侧（workflow_xml.py）显式写 true/false 两值保往返幂等；引擎侧三态解析与注册判断同步改（`!= "false"` / `is not False`）——详见 [workflow-hooks · hidden 默认翻转](../architecture/workflow-hooks.md)。存量 22 XML 全部已显式 true → 行为零变化，只影响未来新建的工作流。

## 批次八（`cf53aa0`）：子工作流快捷创建（浮窗一键按钮）

主线索：**免两步**——原先“建空节点 → 属性面板下拉选目标工作流”，改为节点选择浮窗里每个工作流一个按钮，点击直接建好并同步 schema。用户提案。

### 18. 子工作流快捷创建按钮（节点浮窗新分组）

节点选择浮窗（双击空白 / 拖线到空白弹出）新增「🔗 子工作流」分组（节点插件组之后、Agent 工具组之前），**每个工作流一个快捷按钮**：

```
┌─ 节点选择浮窗 ──────────────┐
│ 🔍 过滤节点/工具/工作流…      │
│ 🧩 节点插件                 │
│ 🔗 子工作流 (22)             │
│ [🔗 score_rerank] [🔗 rerank_topk] ...
│ 🔧 工具组 …                 │
└─────────────────────────────┘
```

点击按钮 = `pickSubWf(name)` 一步完成：

1. `createBasicNode('9', …)` 直接建 type9 子工作流节点，`workflowId` 直设目标名
2. 节点标题 `nodeMeta.title = name`（不再“猜这是调谁的节点”）
3. `syncSubworkflowNode` 同步入参/出参 schema——与属性面板 `setSubWf` **完全同一路径**：输入 = 目标 start outputs、输出 = 目标 exit inputParameters，**端口立即可连线**
4. 拖线触发浮窗时同样自动连线源端口（与 pickNode 连线逻辑一致）

防护与细节（全在 `src/static/workflow_editor.html`）：

- **排除当前编辑的工作流自身**（防自递归）：分组列表 `w.name !== WF.name` 过滤；`pickSubWf` 内再拦一道 `name===WF.name` → toast『不能在自身工作流内调用自身』
- 过滤框同时匹配工作流名与描述
- toast 报告同步结果（如 `已创建子工作流节点：score_rerank（入参2 出参3）`）

验证：JS 语法 + 10 项结构断言全过。Ctrl+F5 强刷编辑器即见新分组。

## 批次九（`0aee996`）：节点描述三处可见（tooltip + props 描述段 + 浮窗 title）

主线索：**描述可读**——节点创建后类型描述在画布上可见，不用靠猜。用户提案："节点创建出来以后就看不到节点描述了"。

### 19. 描述三处显示

| 显示位置 | 实现 |
|---|---|
| 画布 hover tooltip | `renderNode` 的 g 上加 SVG `<title>`（原生浏览器 tooltip，须为首个子元素才可靠显示）——悬停任意节点即见 |
| props 面板描述段 | 头部 `标题 · 类型` 行下方浅蓝底小卡片（10px 灰蓝字）——点选节点即见，不用等 hover |
| 浮窗按钮 title | 静态组（LLM/选择器/循环…）补齐 title（此前只有工具组有），悬停按钮即见 |

### 描述来源：一条新端点 + 两条路

`nodeDesc(n)`（src/static/workflow_editor.html）两条路取描述：

- **type4 工具节点** → 工具 schema 的 `description`（含参数语义，比目录更准）
- **其余类型** → `NODE_DESC[n.type]`，来自新端点 **`GET /api/wf/nodes`**

`/api/wf/nodes`（src/server.py）：返回 `{type: desc}`，数据源 = `real_tools._node_catalog()`——「核心 12 条 + 节点插件 `catalog_entries()` 动态聚合」的 25 类目录。插件节点 desc 跟实现走，改 `.py` 自动跟上。见 [节点插件化 · catalog_entries](../architecture/node-plugins.md)。

前端加载细节：

```javascript
let NODE_DESC={};
async function loadNodeDesc(){
  try{const r=await fetch('/api/wf/nodes');const d=await r.json();NODE_DESC=d.nodes||{};}
  catch(e){}  // 失败静默：离线/旧后端 → 无 tooltip 但不炸
}
```

- `renderNode` 在 g 首 append `<title>`——tooltip 与 props 描述段**同源**（均走 `nodeDesc`）
- `showProps` 头部行下追加描述段卡片
- `nodePicker` 浮窗按钮补 `title`（`NODE_DESC[b[0]]`），静态组与工具组统一
- 初始化链 `loadNodeDesc().then(()=>renderAll())`——描述异步到位后重绘一次

验证：`/api/wf/nodes` 实测 25 类全有 desc（LLM/代码/选择器/AND/N1 抽查通过）；type4 优先工具 schema。Ctrl+F5 强刷编辑器即生效。

## 附：enum 参数渲染为下拉框（通用机制）

工具 schema 里带 `enum` 的参数，编辑器自动渲染为 **select 下拉框**（替代 text input），空值选项显示「（跟随）」。三层链路：

```
工具 schema（parameters.properties.<param>.enum）
  → GET /api/tools 通用透传（server.py api_tools——此前只硬编码 llm_call.model 一条路，
     现 schema 自带 enum 一律透传；llm_call.model 仍走 API 侧附加，因 LIGHT_TOOLS 构造时无法静态声明）
  → 前端 syncToolNode（打开工作流时同步已有节点的选项）+ makeInputControl（检测 enum 渲染 select）
```

首个受益场景：`llm_call.model`（models.json provider 列表 + 空=跟随）。2026-08 多 Agent 工具跟进：`agent_prompt`/`kill_agent` 的 `name`（.agent/agents/ 声明扫描）、`agent_prompt.caller`（['', 'user']）、通信工具 `target_id`（registry 当前 agent_id）——enum 由 `_inject_agent_enums` 动态注入，create/kill 声明变化后自动刷新，详见 [多 Agent · caller 汇报对象与动态 enum 注入](../architecture/multi-agent.md#caller-汇报对象与动态-enum-注入2026-08)。

## 批次十一（2026-08）：钩子协议下拉回归（声明 + schema 规范化，挂载归 yml）

**背景（用户提案）**：钩子逻辑改到 agent 的 dsl 语义数据装配后，工作流里通过下拉框选择匹配钩子 + 自动规范化输入输出 schema 的编辑器特性不应丢——**但最终哪些 Agent 的钩子上挂着哪些东西由该 agent 的 .yml 定义**。

**恢复内容（workflow_editor.html，commit 628f5b1）**：顶栏「钩子」下拉（5 位置 + 无）——选定后：

- **start 输入自动规范化**：`HOOK_INPUTS` 按钩子位置补协议输入（turn_end 场景补 user_message/draft_answer/turn_context + hook_ctx；after_tool 含 tool_result/changed_files + hook_ctx；before_answer 含 draft_answer/changed_calls + hook_ctx——引擎后来注入的上下文字段，原版被删时还没有）
- **end 输出自动规范化**：`inject(boolean)` + `result(string)` 协议对
- 已有自定义输入 → **confirm 保护**（不静默覆盖现成工作流）
- toast 明示语义：「挂载由 agent 的 .yml 声明」
- openWf 回显 `WF.meta.hook`；保存时写 `meta.hook`（空=清除）

**三层分工定稿**：编辑器=声明「实现什么钩子协议」+ schema 规范化（写 meta.hook 根属性）；/agents 管理页=声明「哪些 Agent 挂哪些钩子」（yml 优先，运行时权威）；磁盘 meta=播种面兜底（server PUT 保底合并，见 [workflow-hooks · 钩子声明面三层](../architecture/workflow-hooks.md#钩子声明面三层编辑器协议下拉--磁盘-meta-保底--yml-挂载2026-08)）。

## 批次十（2026-08 下旬）：值容器统一 + 复合节点输出端口 + 筛选下拉修复

### 20. SetVar（type20）值容器 right 统一：类型下拉/引用选择/字面量控件全炸修复

**现象（用户报告）**：SetVar 节点输入字段选类型下拉报 `Cannot set properties of undefined (setting 'type')`，选引用端口报 `setting 'value'`。

**根因**：SetVar（type20）节点的数据结构**异构**——每个 inputParameter 是 `{name, left, right}` 三元组：`left`=目标变量名、**`right`=要写的新值（值容器）**、没有 `input` 键。而属性面板通用输入字段区（类型下拉 `setField`、值按钮、▾ 引用选择 `quickSetRef→setInputRef`、字面量展开 `_litControlHTML`、画布内嵌控件、变量连线折叠组 `setVarLink`、拖线落点 `finishVar`、断线重置、全局变量 `setRefPath/setRefName`）全部按普通节点的 `p.input` 结构读写——type20 时 `p.input` 是 undefined：**写** `f.input.type=v` 炸、**读**恒空（类型显示 string、值按钮恒显「（字面量）」、变量线恒黄）。

**修复（统一容器解析，workflow_editor.html）**：

```javascript
// 写路径：_valBlockW(p,n) —— type20 → right（确保存在），其余 → input
function _valBlockW(p,n){
  const t=(n||{}).type||(findN(selNode)||{}).type;
  if(t==='20'){if(!p.right)p.right={type:'string',value:{type:'literal',content:''}};return p.right;}
  if(!p.input)p.input={type:'string',value:{type:'literal',content:''}};return p.input;
}
// 读路径：_effInput(p)（right||input||left 兜底）——19 处读点全部接上
const t=_effInput(f).type||f.type||'string';   // 类型下拉
const lit=String(_effInput(f).value?.content ?? '');  // 值按钮
```

覆盖面：类型下拉 / 引用选择（▾ 下拉、变量连线组同路径）/ 字面量（值按钮展开 + 画布内嵌控件）/ 拖线落点 finishVar（fallback 此前还写错容器）/ 断线重置 / 全局变量 / 渲染读（值按钮显 🔗 引用名、类型下拉、refPicking 展开行、变量线颜色）。Ctrl+F5 强刷编辑器生效（纯前端）。

### 21. 复合节点本地变量输出侧端口（循环变量终值暴露）

**现象（用户报告）**：复合节点（loop/batch）的本地变量添加时只在输入侧有连线端口，输出侧没有——但循环变量的**终值**本就要从复合节点输出暴露（`890541.diag` 这类下游引用），输出侧没端口意味着既拖不出变量线、已连线下游引用源定位还画错位置（fallback 到节点顶部）。

**修复**（`nodeRows`）：variableParameters 每个变量同步 push 输入侧行 + **输出侧行**（`localVarOut` 标记）——输出端口渲染（`startVar` 拖线发起）、`portY('out')` 定位、变量线源对齐全部经既有机制自动生效，零额外接线。

### 22. 筛选下拉 `_round_out`/`op`/`literal` 全炸：渲染与写入对象不一致

**现象（用户报告）**：批处理筛选（filtered_outputs）的 `_round_out` / op / literal 三个下拉都报 `Cannot set properties of undefined (setting 'operator')`。

**根因**：渲染时 `filt = b.filter || {临时构造}` + push 占位条件——**没有写回 node**（loop 节点从来就没有 batch.filter）；点下拉时 `setCompFilt` 新建空 `{conditions:[]}` → `conditions[ci]` 是 undefined → `c.operator=v` 炸。

**双修复**：① 渲染处写回 `if(!b.filter)b.filter={logic:2,conditions:[]}; n.data.inputs.batch=b`——filt 与 setCompFilt 读写**同一对象**（治本）；② `setCompFilt` 越界防御 `while(conditions.length<=ci) push 占位`——已渲染的面板残留 ci 不再炸（向后兼容）。

## 相关页面

- [v0.18.7 发布记录](../releases/v0.18.7.md) — 批次一（§1–§4）随该版发布；批次二为其后续打磨
- [v0.19.2 发布记录](../releases/v0.19.2.md) — 批次三（§8–§11）随该版发布
- [工作流调试页](workflow-debug.md) — 编辑器族另一页：调试画布节点输出白框（画布逻辑同源，含同步提醒）
- [工作流引擎与钩子](../architecture/workflow-hooks.md) — 批处理/聚合节点引擎侧语义
