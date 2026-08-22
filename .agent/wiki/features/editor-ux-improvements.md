# 工作流编辑器 UX 改进（2026-08-22，随 v0.18.7 发布）

> 批次提交 `a634f83`：LLM 画布提示词框、批处理配置区上移、批处理输出自动管理、item 结构自动推断。
> 随 **v0.18.7** 打包发布（PyPI `agt-agent`，commit `aae43b0`）——纯前端改动（`src/static/editor.html`），Ctrl+F5 刷新编辑器即见新 UI，无需 /restart。

## 1. LLM 节点画布直编 prompt

`nodeH` 对 `type==='3'` 加 `TEXTAREA_H=120`（与文本节点同款）；`renderNode` 渲染 `makeLLMPromptArea(n)`。

关键实现：
- `makeLLMPromptArea` 从 `llmParam` 取 `prompt` 参数（无则惰性创建），textarea `oninput` **直改数据不重绘**——重绘会销毁 textarea 丢焦点
- placeholder 提示 `{{输入字段}}` 占位符 + "systemPrompt/模型在右侧面板"
- systemPrompt / model / thinking / timeout / onError 仍走右侧属性面板（`setLLM` 系列）

## 2. 批处理配置区上移

`showProps` 中 `renderBatchConfig(n)` 从输入字段之后移到之前——批处理开关影响输入字段的"源"选项（item.字段）与输出结构，先看到开关再编辑字段更符合操作顺序。

## 3. 批处理输出自动管理

批处理 `enabled` 时，右侧面板「输出字段」区隐藏编辑，替换为说明文字：
> 批处理已启用：all_outputs / filtered_outputs / nth_output 自动管理（item 结构=节点原输出 schema），关闭批处理恢复原输出编辑。

底层机制（`setBatch('enabled')`，此前已有）：`_origOutputs` 备份原输出 → 生成三输出（list/list/object，schema=nthSchema）→ 关闭时还原。

## 4. item 结构自动推断展示

`renderBatchConfig` 删除手动 `item 类型` 下拉（`setBatch('itemType')` 不再有 UI 入口），改为只读展示：

```
item 结构（自动）object（content, tool_calls, usage）  ← object 时列字段名
item 结构（自动）string                                ← 基础类型直接显示
```

来源 = `nth_output.schema`（= 节点原输出 schema，含 plugin 节点按工具 schema 补全的字段）。三字段在右侧面板无需手动设置。

## 相关页面

- [v0.18.7 发布记录](../releases/v0.18.7.md) — 本页四项随该版发布
- [工作流调试页](workflow-debug.md) — 编辑器族：画布节点输出白框
- [工作流引擎与钩子](../architecture/workflow-hooks.md) — 批处理/聚合节点引擎侧语义
