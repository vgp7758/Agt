# 工作流编辑器 UX 改进 · 多轮打磨

> **批次一（v0.18.7，2026-08-22，批次提交 `a634f83`）**：LLM 画布提示词框、批处理配置区上移、批处理输出自动管理、item 结构自动推断。
> **批次二**：字段行 flex 与紧凑编辑、子字段按钮行内统一归位、代码节点两处、属性面板加宽。
> **批次三（v0.19.2）**：批处理数组源可连线端口、LLM 画布 SYSTEM/PROMPT 双框 + 高度自适应、spec 浮层抽屉、LLM 输出格式下拉修复。
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

## 相关页面

- [v0.18.7 发布记录](../releases/v0.18.7.md) — 批次一（§1–§4）随该版发布；批次二为其后续打磨
- [v0.19.2 发布记录](../releases/v0.19.2.md) — 批次三（§8–§11）随该版发布
- [工作流调试页](workflow-debug.md) — 编辑器族另一页：调试画布节点输出白框（画布逻辑同源，含同步提醒）
- [工作流引擎与钩子](../architecture/workflow-hooks.md) — 批处理/聚合节点引擎侧语义
