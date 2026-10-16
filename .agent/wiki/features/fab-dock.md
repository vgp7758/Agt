# 右侧工具 dock（fabDock）· 非对话按钮收进可折叠图标列（自动分列：列上限 10）

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

## dock 结构：按钮平铺 + 自动分列（_layoutFabCols，列上限 10）

```html
<div id="fabDock">
  <button id="fabDockBtn" aria-label="工具面板（点击展开/收起全部）" data-label="工具面板" onclick="toggleFabDock()">🧰</button>
  <div id="fabDockMenu">
    <!-- 按钮平铺（原生顺序 = 看板 fab + 控件栏按钮 + 插件动态按钮）：_layoutFabCols() 开合时自动分列 -->
    <button id="specFab" ...>📐</button> <button id="logFab" ...>🐞</button> ...（11 个原生平铺，插件动态追加）
  </div>
</div>
```

- `#fabDock`：`position:fixed; top:64px; right:16px; z-index:120`；`align-items:flex-end`——**主按钮贴右缘，多列展开时不横跳**；
- `#fabDockMenu`：默认 `display:none`；`.open` 显示为 flex，**`flex-direction:row-reverse`（2026-10-15 由 row 改）**——**DOM 首列渲染在最右 = 主按钮正下方**，溢出列依次往左；
- `.fabCol`：列容器**由 `_layoutFabCols()` 自动构建，HTML 不再手写**。

**自动分列机制（2026-10-15，用户提案「一列最多 10 个图标，超出往左侧增加一列」）**：`const FAB_COL_MAX = 10`；`_layoutFabCols()` 收集 menu 下全部按钮（平铺直属子节点 + 已在旧 `.fabCol` 里的 + 插件动态追加的）→ 清掉旧列重建 → 每 10 个一组包一个 `.fabCol` 追加回去，末列取余；**幂等可重入**。触发点 = dock 打开时 + 插件 `addButton` 之后（详见[后记四](#后记四自动分列列上限-10fab_col_max溢出往左加列2026-10-15用户提案commit-e7b6e92)）。

**按钮属性换代（2026-09-06，用户提案 hover 标签）**：全部按钮 `title` 移除 → `aria-label`（无障碍完整语义）+ `data-label`（hover 文字标签渲染源）——hover 机制见下文「hover 文字标签」章节。

**分列方式演进**：

| 时期 | 分法 |
|---|---|
| 2026-09-07 ~ 2026-10-15 | 手写双列：第一列（贴右缘）= 原右侧独立 fab 4（📐🐞👥🛠），第二列 = 原控件栏 9——起因是单列 13 个纵向超出小视口（~490px 内收） |
| 2026-10-15 起（现行） | **平铺 + 自动分列**：16 个图标（11 原生 + 5 插件）自动两列 10+6；插件再增 → 自动往左开新列 |

**当前清单**（功能与 id 不变，仅列位自动化；📁open-folder / 💬feedback / VS Code 三个内置已插件化退役见后记，🔴record / 📸screenshot 为后续插件播种新增）：

| 图标 | id / 来源 | 功能 |
|---|---|---|
| 📐 | `specFab` | 施工方案（展开 spec 抽屉；红点 badge 原位保留） |
| 🐞 | `logFab` | 日志抽屉；**有日志才显示**，badge=未读 error 数 |
| 👥 | `teamFab` | Agent 团队看板抽屉 |
| 🛠 | `svcFab` | 后台看板抽屉（服务/定时/后台任务 + 实时日志） |
| 🧩 | `btnEditor` | 工作流编辑器（新页签 /editor） |
| 📚 | `btnRag` | RAG 文档库（新页签 /rag） |
| 🧠 | `btnMemory` | 记忆管理（新页签 /memory） |
| 🤖 | `btnAgents` | Agent 声明管理（新页签 /agents） |
| 📊 | `btnStats` | 统计（新页签 /stats） |
| ⚙ | `btnSettings` | 设置弹窗 |
| 🚮 | `btnClear` | 清空消息区（危险红） |
| 插件 ×5 | open-folder / feedback / webide / record / screenshot | 见各自页面 |

**隐藏按钮也占位**：`logFab` 隐藏态（display:none）仍占列位——展开收起时列序稳定不跳动（2026-10-15 明确语义）。

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
- `force` 可选参数支持强制开/关（`toggleFabDock(true/false)`），为后续程序化联动留口；
- **2026-10-15 自动分列加挂**：打开时触发 `_layoutFabCols()` 重排（见上文「dock 结构」/后记四）——插件新增按钮在每次展开前按列上限归组完毕；关闭仍是纯 class 切换、零副作用。

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

## 后记四：自动分列——列上限 10（FAB_COL_MAX），溢出往左加列（2026-10-15，用户提案，commit e7b6e92）

> 用户提案：「右侧抽屉里的按钮越来越多了，我觉得一列最多10个图标吧，超出的图标往左侧增加一列」

**背景**：三内置插件化退役（后记）+ 插件陆续播种（record / screenshot）后，dock 图标达 **16 个 = 原生 11 + 插件 5**——2026-09-07 的手写双列装不下了，插件 `addButton`「硬找第二列追加」的做法也失去前提。

**四处改动、一处统一**：

| 层 | 改动 |
|---|---|
| HTML | `fabDockMenu` 按钮**平铺**（删手工 `.fabCol` 包装——「看板列 4 + 控件列 9」静态分法退役） |
| CSS | `#fabDockMenu` `row` → **`row-reverse`**——DOM 首列渲染在最右 = 主按钮正下方，溢出列往左长 |
| JS | 新增 `const FAB_COL_MAX = 10` + `_layoutFabCols()`：收集全部按钮（平铺 / 旧列内 / 插件动态）→ 清旧列重建，每 10 个一列，**幂等可重入** |
| SDK | `AgtPlugin.ui.addButton` 不再硬找「第二列」——平铺 append 后触发 `_layoutFabCols()` 重排（插件按钮与原生同权参与分列） |

- **触发时机**：dock 打开 + 插件 `addButton` 之后——增删按钮自动适应，无需刷新
- **隐藏按钮也占列位**：logFab 隐藏态仍占位——列序稳定，展开收起不跳动
- **列生长方向**：第一列贴右缘（主按钮正下方），第 N+1 列在 N 列左侧——契合「溢出往左加列」；16 图标实测两列 10+6
- **验证（playwright 真页面）**：两列 10+6、`firstColIsRightmost=true`、开合正常、5 个插件按钮全部就位；node --check 内联 JS 通过
- 纯前端（commit e7b6e92），**浏览器刷新即生效**，无需 /restart

## 后记五：🛒 插件市场入口入列首位（2026-10-16，用户实锤缺口，commit 2a93c96）

`fabDockMenu` 首位静态插入 `#marketFab`（`fabItem` 类）——`onclick` 新标签打开 `/market` 独立市场页（[marketplace](marketplace.md)）。纯 HTML 插入、零 JS 依赖、零 SDK 时序问题；分列交给既有 `_layoutFabCols` 自动收纳。

生效两段式：按钮走 index.html mtime 热更（**刷新即见**）；`/market` 路由在引擎侧 server.py，随 `/restart` 生效（此前点击 404）。

## 相关页面

- [webide](webide.md)：VS Code logo 按钮入口（原控件栏，收进 dock；2026-09-06 图标 📝 → VS Code logo）
- [用户交互](user-interaction.md)：主对话页其余交互机制
- [气泡交互](bubble-interaction.md)：📐 施工方案/🐞 日志抽屉的展开对象

