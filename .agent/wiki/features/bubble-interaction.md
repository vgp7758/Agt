# 气泡交互 · 展开折叠 + 气泡级复制 + answer 多 Agent 分页

> 前端两处：`static/editor.html`（工作流编辑器气泡面板，展开/折叠）、`static/index.html`（WebUI 聊天面板，气泡级复制 + answer 多 Agent 分页）。后端关联 `src/agent.py` 事件流 `_emit`（统一打 `agent_id` 标）。

## 职责

气泡交互目前有三个独立特性：

| 特性 | 前端文件 | 上线 |
|------|---------|------|
| **系统消息展开/折叠**：系统气泡默认折叠、用户气泡默认展开，点击切换 | `static/editor.html` | v0.18.2 |
| **气泡级复制按钮**：user/answer 气泡 hover 浮现「📋 复制」，一键复制整个气泡内容 | `static/index.html` | 2026-08-19，commit 3a7e9de |
| **answer 多 Agent 分页**：子 Agent 回应与主 answer 同轮时，气泡顶部小 tag 按钮翻页 | `static/index.html` + `src/agent.py` | 2026-08-21，commit ba0940b |

## 系统消息展开/折叠（editor.html）

气泡分两类，默认展开/折叠状态不同，支持点击切换：

| 气泡来源 | 默认状态 | 典型内容 |
|----------|---------|---------|
| **系统自动触发**（钩子注入、工具副作用通知、auto_diag 结果等） | **折叠** | 较长、辅助性信息，不打扰主对话流 |
| **用户指令**（用户消息、用户主动触发的工具输出） | **展开** | 核心对话内容，需即时可见 |

```
气泡渲染（editor.html）
  ├─ 系统气泡：collapsed=true（初始）→ 显示标题/摘要行
  │    └─ 点击气泡头部 → toggle expanded → 展开完整内容
  └─ 用户气泡：collapsed=false（初始）→ 完整可见
       └─ 点击气泡头部 → toggle collapsed → 折叠为摘要
```

- **点击区域**：气泡头部（标题行），带 `▶`/`▼` 方向指示符
- **折叠态**：仅显示标题 + 首行摘要（CSS `max-height` 截断 + 渐变遮罩）
- **展开态**：完整内容，长内容区可滚动
- **状态持久化**：当前会话内保持（刷新页面重置为默认态）

**设计意图**：系统消息（py_auto_diag 注入、async 钩子日志、wiki_auto_query inject 等）篇幅长但非当前关注焦点 → 默认折叠降噪；用户气泡是对话主线 → 默认展开。点击切换让用户按需深入，不强制滚动跳过。

## 气泡级复制按钮（index.html，2026-08-19）

### 交互效果

鼠标悬停到 **user 气泡**（右侧蓝色）或 **answer 气泡**（左侧白色）→ 底部角落浮现 `📋 复制` 小按钮 → 点击 → 整个气泡内容进剪贴板 → 按钮变 `✓ 已复制`，1.5 秒后恢复。

### 四处挂载（实时 + 历史全覆盖）

| 位置 | 函数（均在 `static/index.html`） |
|---|---|
| 实时 user 气泡 | `addUserBubble` |
| 实时 answer 气泡 | `newTurn` |
| 历史 user 气泡 | `renderHistTurn` |
| 历史 answer 气泡 | `renderHistTurn` |

挂载点形如 `col`/`urow`/`row`/`host`（见下）——注意是**宿主容器**而非 bubble 本身。

### 关键设计——按钮挂在宿主（row/col）上而非 bubble 里

```
answer 内容会被 innerHTML 反复重写（finishAnswer / renderAnswerPages / renderSpecBubble / renderSurveyBubble）
  → 按钮放 bubble 内 = 每次重写都被清掉
  → 按钮放 col 上（absolute 定位在气泡下方角落）= 与内容解耦，始终存活
```

- hover 触发区也用宿主（`.row.me:hover` / `.turn:hover`）——鼠标在气泡和按钮之间移动不会闪烁（触发区连成一片）
- 这是「DOM 会被整体重写的容器，交互控件必须挂到不被重写的祖先上」的通用范式，后续给气泡加其它悬浮按钮时同理

### 复制内容与剪贴板降级

- 取文本改用**克隆排除法**（2026-08-21，ba0940b）：`bubble.cloneNode(true)` 后 `querySelectorAll('.ans-tabs,.copy-btn,.run-btn')` 全部 remove，再取 `innerText`——answer 多 Agent 分页的 tabs 按钮字、其他 UI 元素不混进复制内容，**复制到的只有当前页正文**
- 取 `innerText` 而非 `textContent`——answer 里渲染成表格/代码块的内容复制后保留文本结构（表格变成制表对齐的行、代码块原样），不是一坨裸文本
- `clipboard API` 失败自动降级 `execCommand`（兼容老浏览器）

### 与代码块级复制的层级

`index.html` 原有 `.copy-btn`（代码块级复制）与气泡级按钮形成两层：整气泡要 → 气泡按钮；只要某个代码块 → 代码块按钮。两者互不干扰。

## answer 多 Agent 分页（index.html + agent.py，2026-08-21）

### 背景：同步子 Agent 输出串台

现象：同一轮 answer 气泡里混入子 Agent 的回应消息和主 Agent 的 answer，互相覆盖混排。

```
根因链（spec_tools.py L482）
  explore_subagent 构造 SubAgent 时传 on_event=agent.on_event
  → 子 Agent 的 answer 事件（type="answer"）直接流入主事件流
  → 前端 finishAnswer 写入当前轮 answerEl
  → 与主 Agent 的 answer 互相覆盖 ← 串台
```

**范围界定**：只有**同步调用**的子 Agent（explore_subagent / update_wiki）有此问题——主 Agent 正在等它的工具结果时，它的 answer 先到，写进了同一个气泡。异步 `agent_prompt` 路径 on_event=None 本就不串——其 answer 走 inbox → 主 Agent 新一轮处理（见 [多 Agent 体系](../architecture/multi-agent.md)）。

### 修复：事件统一打 agent_id（后端一处改动全覆盖）

`src/agent.py` `_emit`：

```python
event.setdefault("agent_id", self.agent_id)   # 主=_main_，子 Agent=各自 id
```

所有 Agent 的所有事件（answer/thinking/step/tool_*）统一打标——前端据此分流渲染，而不是各发射点各自补标。

### 前端分页渲染

```
┌─ answer 气泡 ─────────────────────────────┐
│ [🤖 主] [reader] [wiki-updater]  ← tag 按钮（当前页高亮）│
│ （当前页的 markdown 渲染内容）                  │
└───────────────────────────────────────────┘
```

| 行为 | 实现 | 说明 |
|---|---|---|
| 页收集 | `finishAnswer(text, agentId)`：`id = agentId \|\| '_main_'`，`curTurn.pages[id] = text` | 主 answer 与每个子 Agent 回应各一页 |
| 自动激活 | `curTurn.activePage = id`（最新到达的页） | 子 Agent 回应到达时自动切过去看；主 answer 后到再切回 |
| tag 按钮 | `renderAnswerPages()`：多页时顶部渲染 `.ans-tabs` 一排 `.ans-tab`（11px 圆角小标签），`switchAnswerPage` 点击翻页 | **仅对该轮有效**——新轮 `newTurn` 后 pages 重置 |
| 单页 | 同样走 `renderAnswerPages`，但无 tabs | 与旧版渲染完全一样 |
| trace 前缀 | `step`/`thinking` 事件：`m.agent_id !== '_main_'` 时加 `[agent_id] ` 前缀 | 子 Agent 的过程事件不再裸混进主 trace |

**历史渲染兼容**：`renderHistTurn` 构造的临时 curTurn 无 pages 字段，`finishAnswer` 内 `curTurn.pages = curTurn.pages || {}` 兜底——读档路径不炸。

### 与复制的配合

分页引入后，answer 气泡的 innerText 会带上 tabs 按钮文字 → 复制按钮改为**克隆排除 UI 元素**再取文本（见上文「复制内容」小节），复制内容始终是当前页正文。

## 与后端的关系

- 气泡内容由 `agent.py` 事件流 `_emit` → WS broadcast → 前端渲染；**所有事件统一携带 `agent_id` 字段**（主=`_main_`，子 Agent=各自 id，`setdefault` 兜底）——前端 answer 分页 / trace 前缀均据此分流
- 系统气泡 vs 用户气泡的区分依据：事件类型（`system` / `user`）——前端按类型赋默认 collapsed 状态
- async 钩子工作流（见 [工作流引擎与钩子](../architecture/workflow-hooks.md#async-元信息字段2026-08-新)）的返回值不注入主循环，但若产生日志/副作用事件，仍以系统气泡形式展示（默认折叠）
- 气泡级复制、answer 分页翻页均为纯前端行为（只读 innerText / 切换已存页面），不涉及后端额外改动

## 相关页面

- [多 Agent 体系](../architecture/multi-agent.md)：同步/异步子 Agent 的 on_event 差异、事件流 agent_id 打标
- [工作流引擎与钩子](../architecture/workflow-hooks.md)：async 元信息字段、钩子链路
- [系统总览](../architecture/overview.md)：事件流 _emit → broadcast 链路
- [运维与排障](../guides/ops.md)：可观测性（llm_calls.jsonl / events.jsonl）
- [v0.18.2 发布记录](../releases/v0.18.2.md)：气泡折叠为该版交付项之一
