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

## 工具卡片简介补齐：Tool.brief 三级优先 + tool_briefs.py 集中字典（2026-09-02，commit 620fd3d）

**背景（缺口实证）**：工具箱浮窗卡片第二行（`.tpk-desc`，渲染条件 `t.desc`）**一直在等一个后端从未提供的字段**——前端 `t.desc ? … : ''` 恒空 → 简介行从未显示（搜索第三路 desc 也空）。用户请求「工具要给面向用户的一句话简介（选择理由）」时查实并补数据源。

**数据源三层**（src/tools.py `Tool.__init__` 构造期解析）：

```python
self.brief = brief or TOOL_BRIEFS.get(self.name) or _brief_from_desc(first_line)
```

| 层 | 内容 |
|---|---|
| ① 显式传参 | `Tool(..., brief=...)`——注册处就近给，最高优先 |
| ② 集中字典 | **src/tool_briefs.py（新建）`TOOL_BRIEFS`**：自有内置工具 130+ 条一句话简介。规则：≤30 字、动词开头、说「用它干什么」而非「它是什么」——与 `description`（给 LLM 的 docstring 首行，面向「模型判断该不该调」）分工 |
| ③ 首句兜底 | `_brief_from_desc(desc, limit=44)`：截到第一个句末标点（。；；:．.或换行）再限长加 …——**MCP 工具（`__mcp__` 前缀）与未来新增工具自动落这层**，工具箱里至少有句人话可显示 |

**消费接线**（src/server.py `/api/tools`，工具箱 `loadToolListForForm` fetch 的同一端点）：工具条目输出 `desc`（brief 解析结果）→ 卡片简介行 + 搜索第三路（名/display/desc）首次有数据；**参数级 `desc` 透传恢复**——schema properties 的 description 注入 `params[].desc`，表单 placeholder（`p.desc || 参数名`）不再退化成参数名（顺带修掉 2026-09-01 起文档写了、后端没给的缺口）。

**约定**：新增工具不写 brief 不报错——只是工具箱简介退化为 docstring 首句（第三层兜底）。brief 只维护自有工具，MCP 工具交给其自带 description 首句。

**生效**：/restart + Ctrl+F5。关联：卡片形态见上节「工具选择」、placeholder 机制见下节。

## 参数 description → placeholder

参数输入控件 placeholder 用**参数的 description（无则参数名）**：

```javascript
const ph = esc(String(p.desc || nm));   // 描述优先，无则参数名
`<input id="tp_${nm}" placeholder="${ph}" title="${ph}" ...>`
```

- 输入框宽度 120px → 190px（path/query 类 260px）——描述能显示更多内容
- `title` 同值（hover 悬停看全描述）

## 与后端的关系

- **2026-09-01 首版纯前端**（index.html）——Ctrl+F5 强刷即生效；**2026-09-02 起卡片简介有后端数据源**（Tool.brief 三级解析在 tools.py 构造期 + /api/tools 透传 desc，见上文「工具卡片简介补齐」），改 tool_briefs.py / Tool 构造后需 /restart + Ctrl+F5
- `sendToolCall` 读 `_pickedTool`（旧读 `toolSel.value`）；`_toolFormMode` 打开时若 `_toolList` 未加载先 `loadToolListForForm()`
- 工具列表来自既有 schema（`_toolList` ← fetch `/api/tools`），浮窗展示复用其 name/desc/group——与工具箱/编辑器 nodePicker 同一注册面

## 相关页面

- [工具外置](tool-externalization.md)：tools/builtin 工具体系（浮窗里列的工具来自同一注册面）
- [编辑器 UX 改进](editor-ux-improvements.md)：工具箱浮窗参照的编辑器 nodePicker 模式
- [用户交互](user-interaction.md)：WebUI 底部栏其它交互（toast 遮罩坑同款「透明元素吃点击」教训）
