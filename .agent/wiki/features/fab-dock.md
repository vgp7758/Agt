# 右侧工具 dock（fabDock）· 非对话按钮收进一列可折叠图标

> src/static/index.html。用户提案 2026-09-06「消息输入框上面的按钮越来越多——把和对话交互不直接相关的按钮都收到右侧那一竖排 icon 里；icon 也越来越多，折叠到一个 icon，点击时打开/折叠所有」。

## 职责

WebUI 主对话页的**工具收纳层**：输入框上方控件栏只留**对话直接相关**的操作，其余管理/观测/编辑类入口全部收进右侧一列圆形 fab（浮动操作按钮），整列再折叠成单个 🧰 主按钮——折叠态只占一个图标位，点击展开整列、再点收起。

## 控件栏瘦身（#controls）

保留的对话直接相关按钮（L401-412）：

| 控件 | 用途 |
|---|---|
| `modelSel` | 模型切换 |
| `sessionSel` | 会话切换 |
| `agentSel` | Agent 直接交互切换 |
| `btnImg` 📁 | 加图 |
| `btnToolCall` 🔧 | 工具调用表单 |
| `btnStop` ⏹ | 停止（忙时显示） |

原先散排在控件栏/浮动位的管理按钮（设置/反馈/统计/团队/后台/编辑器/记忆/RAG/Agent 管理/清空/WebIDE）全部移出。

## dock 结构（L361-379）

```html
<div id="fabDock">
  <button id="fabDockBtn" title="工具面板（点击展开/收起全部）" onclick="toggleFabDock()">🧰</button>
  <div id="fabDockMenu">
    <button id="specFab" title="施工方案（点击展开）">📐<span class="badge" id="specFabBadge">!</span></button>
    <button id="logFab" class="fabItem" ...>🐞<span id="logFabBadge">0</span></button>
    <button id="teamFab" class="fabItem" ...>👥</button>
    <button id="svcFab" class="fabItem" ...>🛠</button>
    ...共 13 个图标
  </div>
</div>
```

- `#fabDock`：`position:fixed; top:64px; right:16px; z-index:120` 纵向 flex——右上角贴边一列；
- `#fabDockBtn`：42px 圆（深灰 #1f2937），默认**折叠态唯一可见元素**；
- `#fabDockMenu`：默认 `display:none`，`.open` 时 `display:flex` 纵向 gap:8px；
- 每个 `.fabItem`：42px 圆 + 各自背景色区分。

**13 个图标清单**：

| 图标 | id | 功能 | 动作 |
|---|---|---|---|
| 📐 | `specFab` | 施工方案 | 展开 spec 抽屉（**id/onclick/badge 原样保留**，原独立 floating fab 改 relative 进 dock） |
| 🐞 | `logFab` | 日志 | 展开日志抽屉；**有日志才显示**，badge=未读 error 数 |
| 👥 | `teamFab` | Agent 团队看板 | 展开团队抽屉（子 Agent + 远程实例） |
| 🛠 | `svcFab` | 后台看板 | 展开后台抽屉（服务/定时/后台任务 + 实时日志） |
| 🧩 | `btnEditor` | 工作流编辑器 | 新页签 /editor |
| 📚 | `btnRag` | RAG 文档库 | 新页签 /rag |
| 🧠 | `btnMemory` | 记忆管理 | 新页签 /memory |
| 🤖 | `btnAgents` | Agent 声明管理 | 新页签 /agents |
| 📝 | `btnIde` | WebIDE | `openWebIde()` 新页签开 VS Code serve-web（见 [webide](webide.md)） |
| 📊 | `btnStats` | 统计 | 新页签 /stats |
| 💬 | `btnFeedback` | 反馈 | 反馈弹窗 |
| ⚙ | `btnSettings` | 设置 | 设置弹窗 |
| 🚮 | `btnClear` | 清空消息区 | 危险红 |

## 开合机制（toggleFabDock，L1791-1797）

```javascript
function toggleFabDock(force){
  const d = document.getElementById('fabDock');
  if(!d) return;
  const open = (typeof force === 'boolean') ? force : !d.classList.contains('open');
  d.classList.toggle('open', open);
}
```

- 纯 class 切换，无显隐状态变量：`.open` → `#fabDockBtn` `transform:rotate(45°)`（主按钮旋转作 ✕ 收起暗示）+ `#fabDockMenu` 显示；
- `force` 可选参数支持强制开/关（`toggleFabDock(true/false)`），为后续程序化联动留口。

## 关键设计：id 原样保留

所有按钮 **id 不变、只改 DOM 位置**——JS 侧 `getElementById` 绑定（`btnSettings`/`btnFeedback`/`btnClear` 等）与既有条件显示（`logFab` 有日志才出现、badge 未读计数、`specFabBadge` 红点）零改动。13 个 id 精确断言防回归。

## 验证

- node --check + dock 区段 13 个 id 精确断言；
- playwright 真页面三态：折叠（仅 3 按钮 + 图标列隐藏）/ 展开（13 图标 + 主按钮旋转）/ 再点收起。

**生效方式**：纯前端（index.html 磁盘 serve），**Ctrl+F5 刷新即生效**，无需 /restart。

## 相关页面

- [webide](webide.md)：📝 IDE 按钮入口（原控件栏，本次收进 dock）
- [用户交互](user-interaction.md)：主对话页其余交互机制
- [气泡交互](bubble-interaction.md)：📐 施工方案/🐞 日志抽屉的展开对象
