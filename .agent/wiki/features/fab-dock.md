# 右侧工具 dock（fabDock）· 非对话按钮收进可折叠图标列（双列布局）

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

## dock 结构：双列布局（L361 起）

```html
<div id="fabDock">
  <button id="fabDockBtn" aria-label="工具面板（点击展开/收起全部）" data-label="工具面板" onclick="toggleFabDock()">🧰</button>
  <div id="fabDockMenu">
    <!-- 第一列（贴右缘）：原右侧独立 fab 4 个——主按钮正下方 -->
    <div class="fabCol">
      <button id="specFab" aria-label="施工方案（点击展开）" data-label="施工方案">📐<span class="badge" id="specFabBadge" style="display:none">!</span></button>
      <button id="logFab" class="fabItem" aria-label="日志（回退/限流/错误，点击展开）" data-label="日志">🐞<span id="logFabBadge">0</span></button>
      <button id="teamFab" class="fabItem" aria-label="Agent 团队看板（子 Agent + 远程实例，点击展开）" data-label="团队看板">👥</button>
      <button id="svcFab" class="fabItem" aria-label="后台看板（服务/定时/后台任务，运行/退出、实时日志，点击展开）" data-label="后台看板">🛠</button>
    </div>
    <!-- 第二列：原消息框上方控件栏收进的 9 个 -->
    <div class="fabCol">
      <button id="btnEditor" ... data-label="工作流编辑器">🧩</button>
      ... 📚 🧠 🤖 <img src="/icons/vscode.png" alt="VS Code"> 📊 💬 ⚙ 🚮
    </div>
  </div>
</div>
```

- `#fabDock`：`position:fixed; top:64px; right:16px; z-index:120`；`align-items:flex-end`——**主按钮贴右缘，展开两列时不横跳**（改自 align-items:center）；
- `#fabDockBtn`：42px 圆（深灰 #1f2937），默认**折叠态唯一可见元素**；
- `#fabDockMenu`：默认 `display:none`，`.open` 时 `display:flex; flex-direction:row`（**多列容器**）gap:8px；
- `.fabCol`：每列内部 `flex-direction:column; gap:8px; align-items:center`（2026-09-07 双列化新增）；
- 每个 `.fabItem`：42px 圆 + 各自背景色区分。

**按钮属性换代（2026-09-06，用户提案 hover 标签）**：全部按钮 `title` 移除 → `aria-label`（无障碍完整语义）+ `data-label`（hover 文字标签渲染源）——hover 机制见下文「hover 文字标签」章节。

**双列分组（2026-09-07，用户裁定「原本在消息框上面的那些图标一列，其余放在第二列」）**：

| 列 | 图标（id） | 来源 |
|---|---|---|
| 第一列（贴右缘，主按钮正下方） | 📐specFab / 🐞logFab / 👥teamFab / 🛠svcFab | 原右侧独立 fab（badge 红点原位保留） |
| 第二列（左侧） | 🧩btnEditor / 📚btnRag / 🧠btnMemory / 🤖btnAgents / VS Code logo btnIde / 📊btnStats / 💬btnFeedback / ⚙btnSettings / 🚮btnClear | 原消息框上方控件栏收进 |

**为什么双列**：单列 13 个 ≈ 13×50px 纵向排布超出小视口（用户反馈「图标还是有点超出范围」）；双列把高度砍半——展开总高约 490px，实测 818px 视口内 ✅ 不再超出。

**13 个图标完整清单**（功能不变，仅布局分列）：

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
| VS Code logo | `btnIde` | WebIDE | `openWebIde()` 新页签开 VS Code serve-web（见 [webide](webide.md)）——**2026-09-06 由 📝 换为 `<img src="/icons/vscode.png">`** |
| 📊 | `btnStats` | 统计 | 新页签 /stats |
| 💬 | `btnFeedback` | 反馈 | 反馈弹窗 |
| ⚙ | `btnSettings` | 设置 | 设置弹窗 |
| 🚮 | `btnClear` | 清空消息区 | 危险红 |

## hover 文字标签：data-label 左侧浮现（2026-09-06，用户提案）

dock 内所有图标（含主按钮 🧰、两列 13 个）hover 时**左侧立即浮现深色文字标签**，替代原生 `title` 延迟 1~2s 才出现的灰框提示（用户提案 2026-09-06「图标在 hover 的时候变成文字标题」）。

**纯 CSS 实现（零 JS）**：

```css
#fabDock [data-label]::after { content: attr(data-label); position:absolute; right:calc(100% + 8px);
  top:50%; transform:translateY(-50%); white-space:nowrap; background:#1f2937; color:#fff; font-size:12px;
  line-height:1; padding:6px 10px; border-radius:6px; opacity:0; pointer-events:none;
  transition:opacity .12s ease; box-shadow:0 2px 10px #0005; z-index:140; }
#fabDock [data-label]:hover::after { opacity:1; }
```

- `::after` 内容 = 按钮 `data-label` 属性（`content: attr(data-label)`）——标签文案随按钮走，零 JS、零字典；
- **位置 `right: calc(100% + 8px)` + `top:50%` 垂直居中**：dock 贴右缘，label **向左展开**，不遮挡图标本身（fab 按钮 `position:relative`）；
- `opacity:0 → 1` 过渡 + `pointer-events:none`（标签不挡点击、不挡其它元素 hover）；
- **`title` 全部移除**：语义迁至 `aria-label`（描述比原 title 更完整、含动作说明）——避免原生 tooltip 与 CSS 标签**双重弹出**（两套提示叠一起反而乱）。

**同 commit 顺带修复——`openWebIde()` 加载态**：原 `b.textContent = '⏳'` 覆盖式加载提示——按钮内容已换成 `<img>`（VS Code logo），覆盖 textContent 会**直接清掉图标**。改为 `.fabItem.loading { opacity:.55; cursor:progress; }` class 切换（禁用 + 半透明），不动 DOM 内容。

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

- node --check + dock 区段 13 个 id 精确断言（**4+9 两列、无重复 id**）；
- 双列化插曲：交换列序时首轮编辑曾产生重复列（4 个 fab 在 DOM 里出现两份）——断言抓到后删除 7 行，终验无重复 id；
- playwright 真页面三态：折叠（仅主按钮）/ 展开（13 图标双列 + 主按钮旋转）/ 再点收起；
- 双列展开总高实测 ~490px（818px 视口内 ✅ 不再超范围；上一轮单列 13 个纵向超界）。

**生效方式**：纯前端（index.html 磁盘 serve），**Ctrl+F5 刷新即生效**，无需 /restart。

## 后记：三图标插件化退役（2026-10-16，用户提案「播种示例」扩散）

**2026-10-16 后记：三图标插件化退役（用户提案「播种示例」扩散）**——dock 图标来源分化为**内置 10 + 插件 3**（插件经 `AgtPlugin.ui.addButton` 动态挂 fabDock 席位，见 [插件 SDK](../architecture/plugin-sdk.md) 播种章节）：

| 图标 | 原内置 | 现由插件提供 | 说明 |
|---|---|---|---|
| 📁 打开文件夹 | `btnOpenDir` + `POST /api/open-workspace` | open-folder | 弹的是**跑 agt 那台机器**的文件夹（手机/远程访问同一场景） |
| 💬 反馈 | `btnFeedback` + 反馈弹窗 | feedback | 弹窗改 addPanel 侧滑表单（见 [feedback](feedback.md)） |
| VS Code logo | `btnIde` + `openWebIde()` | webide | icon=`<img src="/plugins-assets/webide/vscode.png">`（**插件本地资产**，随播种分发）；`.loading` 加载态经 onClick 事件透传实现（见 [webide](webide.md)） |

- 上文「13 个图标完整清单」中此三行转历史记录；插件按钮由插件 JS 自行创建（无内置固定 id），hover 文案由 addButton 的 label 字段承载
- 内置对应实现全删：index.html **-93 行**（三按钮 DOM / `openWebIde()` / 反馈弹窗 / open-workspace fetch）、server.py **-177 行**（`/api/open-workspace`、`/api/ide/open`、`_ide_payload`、WS 反馈分支）——单一实现源收敛到插件
- `/plugins disable <名>` 可单独关掉任一图标；重启用 `/plugins enable`

## 后记二：插件按钮第二席位——输入框上方工具条（2026-10-15，用户提案，commit 106c049）

> 用户提问：「只能是右侧的图标抽屉里的按钮吗？可以是消息窗口上面的按钮吗？」

`AgtPlugin.ui.addButton` 增 `position` 参数：**`'fab'`（缺省）= 本页右侧 dock 图标**（现有插件不动）/**`'input'` = 输入框上方工具条**——`#pluginInputBar`（`#inputBar` 之前的横排容器，icon+文字胶囊小按钮；无按钮时隐藏，有则显示，flex wrap）。dock 仍是插件按钮的缺省席位，`input` 席位适合对话高频操作类按钮（更贴消息流、更醒目）；抽屉 push 布局（横竖屏两组选择器）同步纳入新容器防错位。机制与验证详见 [插件 SDK](../architecture/plugin-sdk.md)「SDK 增强：addButton position:'input'」节。

## 后记三：右侧抽屉统一容器 .agtdrawer + DrawerCtl 调度器（2026-10-15，用户提案，commit b380fd3）

> 用户提案：「右侧抽屉的实现逻辑现在是各实现各的，每一个面板自己在各自管理面板的布局；能不能统一一下，比如 spec 面板的那种布局方式提取为一个容器，然后所有抽屉的逻辑统一为在这个容器里添加一个特定的业务面板。」

**旧状：六处各写各的**——📐spec 有正式 CSS 类（spec-headbar/spec-scroll/spec-close），🐞log / 👥team / 🛠svc 用**内联 style 硬编码**同一套骨架，📄文件预览独立 CSS 块（fp- 前缀），插件 addPanel 用 JS `cssText` 复刻第 6 份。互斥逻辑写了 5 遍且互不对称：spec 开时关 3 个、filePreview 手写 4 个 if、插件面板**三不管**（零互斥、不 push、关闭不恢复）。

### CSS 层：.agtdrawer 通用骨架 + 主题色变量

```css
.agtdrawer      { position:fixed; top:0; right:0; bottom:0; width:40%; display:none;
                  flex-direction:column; overflow:hidden; }
.agtdrawer.open { display:flex; }
.agtdrawer-head { flex:0 0 auto + 底边线 }          /* .agtdw-title 标题 + .agtdw-close ✕ 圆钮 */
.agtdrawer-body { flex:1 1 auto; overflow-y:auto }   /* 业务滚动区 */
/* 各面板只挂 id 级主题变量，骨架零重复： */
#specPanel { --dw-bg:#fafbff; --dw-line:#c7d2fe; --dw-accent:#4338ca; }
#teamPanel { --dw-bg:#fbfdff; --dw-accent:#0e7490; }
```

竖屏 `width:100%` 媒体查询从「逐面板枚举」收敛为 `.agtdrawer` 一条（editor-ux 批次十二那种「portrait 只写了 specPanel、logPanel 漏段」的坑此后结构性不可能）。

### JS 层：DrawerCtl 注册表统一调度

```javascript
DrawerCtl.register('teamPanel', { fab:'teamFab',
  onOpen(){ renderTeamDash(); _dashPoll('teamPanel'); },   // 开=拉数据+起轮询
  onClose(){ _dashStop('teamPanel'); } });                 // 关=停轮询
function toggleTeamPanel(open){ DrawerCtl.toggle('teamPanel', open); }  // 薄包装
```

- **open** = 互斥关其它抽屉 + `body.drawer-push` 主区让位 + 藏自己的 fab（记住开前原态）
- **close** = 恢复 fab 原态 + 按**全局开态**解除 push
- 业务面板只管内容（onOpen / onClose 两个生命周期钩子），布局与开合全由本层负责——互斥 / push / fab 恢复从此**单源一份实现**

### 迁移结果

| 面板 | 迁移后 |
|---|---|
| 📐 spec / 🐞 log / 👥 team / 🛠 svc / 📄 file 五业务抽屉 | 全部 `.agtdrawer` 结构；旧 toggle 函数保留为**薄包装**（HTML onclick 零改动）；轮询挂 onOpen/onClose；log 面板「开即已读」清 badge + 滚底并入薄包装 |
| 📄 文件预览（filePreview） | 手写 4-if 互斥与 anyOpen push 清理 → `DrawerCtl.open/close` 各一行（见[气泡交互 · 文本文件预览抽屉](bubble-interaction.md)） |
| 🔌 插件 addPanel（example-hello / feedback） | 内部改用 `.agtdrawer` + DrawerCtl 托管——**三不管彻底修复**：插件面板从此参与互斥、push 让位、关闭正确收场（API 面不变，见[插件 SDK](../architecture/plugin-sdk.md)） |
| ⚙ 设置 / 💬 dashboard 群聊 | **不迁**——范式不同（居中模态弹窗 / 独立页面推挤并排），强迁破坏其语义 |

### 实测与生效

- playwright 真页面两轮：第一轮抓到「open 时忘藏 fab」漏项 → 修复 → 第二轮全绿（DrawerCtl 加载 / 7 个 .agtdrawer 在位=5 业务+2 插件 / 开合 / 互斥 / push 让位与解除 / fab 藏与恢复 / 插件面板联动）；node --check 内联 JS 通过
- 纯前端改动（commit b380fd3），**浏览器刷新即生效**，无需 /restart

## 相关页面

- [webide](webide.md)：VS Code logo 按钮入口（原控件栏，收进 dock；2026-09-06 图标 📝 → VS Code logo）
- [用户交互](user-interaction.md)：主对话页其余交互机制
- [气泡交互](bubble-interaction.md)：📐 施工方案/🐞 日志抽屉的展开对象

