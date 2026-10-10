# WebUI 工具表单模式 · 🔧 手动调用工具（两级弹窗：工具箱 → schema 动态表单）

> src/static/index.html（两级弹窗 `#toolPickModal` / `#toolCallModal`）+ src/server.py（/api/tools 补 required）。手动调用工具：点 🔧 → ①工具箱弹窗（搜索/分组/卡片）→ 选工具关①开 ②该工具 schema 动态生成的参数表单弹窗（工具描述全文 + 参数控件 + description 小字占位）→ 执行发 `/call`。**消息输入框全程不被顶替**（2026-10-10，commit c6732b1，用户提案——此前表单内嵌顶替输入框，历史见下文各节）。

## 职责

- 底部工具栏「🔧 工具」按钮 → ①工具箱弹窗（`openToolPick`：搜索/分组/卡片）→ 点卡片 → ②schema 动态表单弹窗（`openToolCallForm`：工具描述全文 + 参数控件）→ 执行（`sendToolCall` 构造 `/call` 走 ws 发送）
- 两级都是居中模态（点遮罩空白关闭）——**无「表单模式」状态**：输入框全程可见可用（`_toolFormMode` / `exitToolForm` 已随 2026-10-10 重构删除）
- 参数控件按 schema 分型：enum → 下拉 / boolean → 勾选 / integer·number → 数字输入 / 其余文本框；description 作 placeholder 或控件下小字；required 参数带红星

## 工具选择：下拉框 → 按钮 + 工具箱浮窗（2026-09-01，commit 7010d66）

> 历史节：本节的「页面内浮窗 + 表单展开」形态已被 2026-10-10 两级弹窗取代（工具箱交互本身——搜索/分组/卡片——延续至今，见下文「两级弹窗重构」）。

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

### 后记（2026-10-10，随两级弹窗重构实测对账）：卡片简介行从未亮过——t.desc 恒空，改读 t.description

代码对账推翻本节「消费接线」的记载：`/api/tools` 工具级字段**只有 `description`（schema docstring），从未输出过 `desc`（brief 解析结果）**——src/server.py 全文 0 处引用 brief，`Tool.brief`（tools.py 三级解析）至今没有任何消费端。前端卡片渲染条件读 `t.desc` → 恒空 → 卡片第二行简介与搜索第三路自 2026-09-01 起一直是死的（卡片简介从未显示过）。

本轮修复（前端侧改读真实字段）：卡片描述改读 `t.description`（空白折叠、截 110 字），搜索第三路同步改 `t.description`——卡片描述行实测已出现。Tool.brief / tool_briefs.py 三级体系保留在 tools.py（未来真接线时启用）；工具箱简介现行口径 = **schema description 截断**，brief 词典暂不参与 /api/tools。

## 参数 description → placeholder

表单弹窗的参数小字说明按控件分型落位（2026-10-10 弹窗版；旧内联表单只支持 input placeholder，已随重构删除）：

- **input（文本/数字）**：placeholder = 参数 description（无则参数名），`title` 同值 hover 看全
- **select（enum）**：select 无处放 placeholder → description 作控件下方 11px 灰字 + title
- **checkbox（boolean）**：description 作勾选框 label 文本（无则「启用」）+ title

数据源：`/api/tools` 的 `params[].desc`（schema properties 的 description 透传，src/server.py）——2026-09-02 补齐的参数级透传延续至今。输入控件全宽（`width:100%`），不再用旧版 190/260px 定宽。

## 发送即退表单模式（2026-09-04，commit fd3d465，用户提案）

> **历史（旧内联表单模式）**：`sendToolCall()` 发出 `/call`、清空参数后**直接调 `exitToolForm()`**——表单收起、输入框恢复，发完即可继续打字对话。动机：手动工具调用低频，发完滞留表单还得手动点 ✕ 才回得了对话。

2026-10-10 两级弹窗重构后该语义**天然成立**：输入框从未被顶替，`sendToolCall()` 末尾 `closeToolCallForm()` 执行即关弹窗回对话（见下文「两级弹窗重构」）。`exitToolForm` 已随内联表单删除；中途放弃用表单弹窗的 [取消] 按钮或点遮罩空白处。

## 两级弹窗重构：表单不再顶替消息输入框（2026-10-10，commit c6732b1，用户提案）

**动机（用户提案）**：旧模式点 🔧 后表单**顶替消息输入框**（`#toolForm` 内嵌展开 + `_toolFormMode`），选工具、填参数期间不能打字；且选完工具参数表单直接铺开，工具描述只有卡片上两行。改为两级弹窗，输入框全程可见可用：

```
点 🔧 工具
  → ① 工具箱弹窗（toolPickModal：搜索 + 分组卡片）
  → 点某个工具卡片 → ①关闭 → ② 该工具 schema 动态表单弹窗（toolCallModal）
  → [执行] 发送 /call 并关弹窗
```

**① 工具箱弹窗 `toolPickModal`**（z-index 220，全屏遮罩 + 居中面板）：

- 复用 `renderToolPicker`（搜索 名/显示名/描述 三路实时过滤、分组卡片、分组内卡片宽度自适应）——容器从页面内浮窗换成居中模态；打开自动聚焦搜索框（`#tpkFilter`）
- 点遮罩空白处 / 选中工具后关闭（`closeToolPick`）
- 顺手修复：卡片描述悬空（前端读 `t.desc`，后端实为 `description`）——改读 `t.description`（截 110 字），见上文「工具卡片简介补齐」后记

**② schema 动态表单弹窗 `toolCallModal`**（z-index 221，560px 白卡，超高滚动；结构由 `openToolCallForm` 按工具 schema 现场生成）：

| 区 | 内容 |
|---|---|
| 标题行 | 🔧 工具显示名 + 分组徽章 |
| 描述区 | 工具 description **全文**（pre-wrap，超 110px 内滚） |
| 参数区 | 逐参数渲染控件（见下表）；无参数工具显示「（无参数——直接点执行）」 |
| 底部 | [取消]（关弹窗）/ [执行]（sendToolCall） |

参数控件分型（description 落点见上文「参数 description → placeholder」）：

| schema 形态 | 控件 |
|---|---|
| enum 非空 | `<select>`（非必填首位加「（不填）」空选项） |
| boolean | checkbox（label = desc 或「启用」） |
| integer / number | `<input type=number>` |
| 其余（含 any） | `<input type=text>`（全宽） |

- **required 红星**：`/api/tools` 新增 `required` 数组输出（src/server.py，schema `parameters.required` 透传），必填参数名后标 `*`；**旧进程无该字段 → 无红星优雅降级，/restart 一次后出现**
- 打开自动聚焦第一个输入控件；点遮罩空白关闭

**执行链（`sendToolCall`）**：逐控件收集参数（select 空 = 不填；boolean 勾选才送；integer/number 转 int/float）→ 构造 `/call 工具名(JSON参数)` → ws 发送 + sys 行回显 → **执行即关弹窗**（旧「发送即退」语义天然成立，见上文）。WS 未连接时 toast 拦截，防 `null.send` 级联报错。

**删除清单**：内联 `#toolForm`、`_toolFormMode`、`exitToolForm`（✕ 按钮）、drawer-push 样式中 `#toolForm` 引用——「表单模式」作为页面状态不复存在。

**生效**：前端改动（弹窗 HTML + JS 块重写）Ctrl+F5 即生效；`required` 红星需 /restart（后端 /api/tools 改动）。playwright 真页面（用户实例 9013）实测：工具箱 → 表单 → placeholder/小字 → 执行回显全链路 ✓。

## 与后端的关系

- `/api/tools`（src/server.py）是唯一数据源：工具级输出 `name/display/group/description/params/outputs/required`——工具箱弹窗吃 name/display/group/description，表单弹窗吃 description/params/required；`required` 为 2026-10-10 新增（必填红星），旧进程缺字段时优雅降级
- params[] 逐参数透传 `desc`（参数 description → placeholder/小字）与 `enum`（下拉值域）；llm_call.model 特判附加 models.json provider 列表
- 生效口径：前端（index.html）改动 Ctrl+F5 即生效；/api/tools 字段改动需 /restart

## 相关页面

- [工具外置](tool-externalization.md)：tools/builtin 工具体系（弹窗里列的工具来自同一注册面）
- [编辑器 UX 改进](editor-ux-improvements.md)：工具箱弹窗参照的编辑器 nodePicker 模式
- [用户交互](user-interaction.md)：WebUI 底部栏其它交互（toast 遮罩坑同款「透明元素吃点击」教训）
