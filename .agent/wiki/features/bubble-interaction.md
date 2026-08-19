# 气泡交互 · 展开折叠 + 气泡级复制

> 前端两处：`static/editor.html`（工作流编辑器气泡面板，展开/折叠）、`static/index.html`（WebUI 聊天面板，气泡级复制按钮）。后端关联 `src/server.py` WS 广播与 `src/agent.py` 事件流 `_emit`。

## 职责

气泡交互目前有两个独立特性：

| 特性 | 前端文件 | 上线 |
|------|---------|------|
| **系统消息展开/折叠**：系统气泡默认折叠、用户气泡默认展开，点击切换 | `static/editor.html` | v0.18.2 |
| **气泡级复制按钮**：user/answer 气泡 hover 浮现「📋 复制」，一键复制整个气泡内容 | `static/index.html` | 2026-08-19，commit 3a7e9de |

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
answer 内容会被 innerHTML 反复重写（finishAnswer / renderSpecBubble / renderSurveyBubble）
  → 按钮放 bubble 内 = 每次重写都被清掉
  → 按钮放 col 上（absolute 定位在气泡下方角落）= 与内容解耦，始终存活
```

- hover 触发区也用宿主（`.row.me:hover` / `.turn:hover`）——鼠标在气泡和按钮之间移动不会闪烁（触发区连成一片）
- 这是「DOM 会被整体重写的容器，交互控件必须挂到不被重写的祖先上」的通用范式，后续给气泡加其它悬浮按钮时同理

### 复制内容与剪贴板降级

- 取 `bubble.innerText` 而非 `textContent`——answer 里渲染成表格/代码块的内容复制后保留文本结构（表格变成制表对齐的行、代码块原样），不是一坨裸文本
- `clipboard API` 失败自动降级 `execCommand`（兼容老浏览器）

### 与代码块级复制的层级

`index.html` 原有 `.copy-btn`（代码块级复制）与本次气泡级按钮形成两层：整气泡要 → 气泡按钮；只要某个代码块 → 代码块按钮。两者互不干扰。

## 与后端的关系

- 气泡内容由 `agent.py` 事件流 `_emit` → WS broadcast → 前端渲染
- 系统气泡 vs 用户气泡的区分依据：事件类型（`system` / `user`）——前端按类型赋默认 collapsed 状态
- async 钩子工作流（见 [工作流引擎与钩子](../architecture/workflow-hooks.md#async-元信息字段2026-08-新)）的返回值不注入主循环，但若产生日志/副作用事件，仍以系统气泡形式展示（默认折叠）
- 气泡级复制为纯前端行为（只读 innerText），不涉及后端改动

## 相关页面

- [工作流引擎与钩子](../architecture/workflow-hooks.md)：async 元信息字段、钩子链路
- [系统总览](../architecture/overview.md)：事件流 _emit → broadcast 链路
- [运维与排障](../guides/ops.md)：可观测性（llm_calls.jsonl / events.jsonl）
- [v0.18.2 发布记录](../releases/v0.18.2.md)：气泡折叠为该版交付项之一
