# WebUI 工具表单模式 · 🔧 手动调用工具（按钮 + 工具箱浮窗）

> src/static/index.html（底部工具表单 #toolForm）。手动调用工具的表单模式：选工具 → 填参数 → 发送。2026-09-01（commit 7010d66）工具选择从下拉框改为**按钮 + 工具箱浮窗**（参照工作流编辑器 nodePicker）——工具多了下拉列表巨长难找，浮窗带搜索/分组/描述卡片。

## 职责

- 底部工具栏「🔧 工具」按钮 → 工具表单模式（`_toolFormMode`）：选择工具 + 填参数 + 发送工具调用（`sendToolCall`）
- 工具选择：按钮 + 浮窗（替代旧 `<select id="toolSel">` 下拉框）
- 参数输入：description 作为 placeholder（用户明确「不介意各参数输入控件布局稍微占点位置」）

## 工具选择：下拉框 → 按钮 + 工具箱浮窗（2026-09-01，commit 7010d66）

**形态**：

```
[🔧 选择工具…] 按钮 → 点击弹出工具箱浮窗（参照编辑器 nodePicker）
┌─ 工具箱浮窗 ─────────────────────────┐
│ 🔍 搜索工具名/描述…（实时过滤）        │
│ 内置工具（分组标题）                    │
│ ┌──────────────┐ ┌──────────────┐    │
│ │ run_python   │ │ read_file    │    │
│ │ 运行 Python… │ │ 读取文件（统… │    │
│ └──────────────┘ └──────────────┘    │
│ …（卡片：名 + 描述两行）               │
└──────────────────────────────────────┘
```

**关键点**：

- **搜索**：工具名 / 显示名 / **描述**三路匹配实时过滤（`#tpkFilter`，聚焦自动）
- **分组 + 卡片**：分组标题（`.tpk-group`）+ 卡片（名 + 描述两行）——工具多了不靠滚靠搜
- **选中**：按钮显示当前工具名（`_pickedTool`）、浮窗关闭 → 参数表单出现
- **关闭**：点浮窗外部自动关闭（`stopPropagation` + 全局监听）；手机端卡片全宽（`@media` 适配）
- **复位**：每次打开工具表单自动复位（`🔧 选择工具…` + 清参数）
- CSS：`#toolPicker` 460px / max-height 520px / overflow-y auto / 阴影（卡片式浮层）

## 参数 description → placeholder

参数输入控件 placeholder 用**参数的 description（无则参数名）**：

```javascript
const ph = esc(String(p.desc || nm));   // 描述优先，无则参数名
`<input id="tp_${nm}" placeholder="${ph}" title="${ph}" ...>`
```

- 输入框宽度 120px → 190px（path/query 类 260px）——描述能显示更多内容
- `title` 同值（hover 悬停看全描述）

## 与后端的关系

- **纯前端改动**（index.html）——**Ctrl+F5 强刷即生效，无需 /restart**
- `sendToolCall` 读 `_pickedTool`（旧读 `toolSel.value`）；`_toolFormMode` 打开时若 `_toolList` 未加载先 `loadToolListForForm()`
- 工具列表来自既有 schema（`_toolList`），浮窗展示复用其 name/desc/group——与工具箱/编辑器 nodePicker 同一注册面

## 相关页面

- [工具外置](tool-externalization.md)：tools/builtin 工具体系（浮窗里列的工具来自同一注册面）
- [编辑器 UX 改进](editor-ux-improvements.md)：工具箱浮窗参照的编辑器 nodePicker 模式
- [用户交互](user-interaction.md)：WebUI 底部栏其它交互（toast 遮罩坑同款「透明元素吃点击」教训）
