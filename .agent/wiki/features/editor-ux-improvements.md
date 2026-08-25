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

## 相关页面

- [v0.18.7 发布记录](../releases/v0.18.7.md) — 批次一（§1–§4）随该版发布；批次二为其后续打磨
- [v0.19.2 发布记录](../releases/v0.19.2.md) — 批次三（§8–§11）随该版发布
- [工作流调试页](workflow-debug.md) — 编辑器族另一页：调试画布节点输出白框（画布逻辑同源，含同步提醒）
- [工作流引擎与钩子](../architecture/workflow-hooks.md) — 批处理/聚合节点引擎侧语义
