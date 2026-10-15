# 插件 SDK · 引擎级全栈插件方案（P0+P1 已落地，2026-10-14 提案当日实装）

> 2026-10-14 用户提案：「支持更多引擎层的可拓展插件，允许用户只需要追加一些自己的代码放在指定位置，
> 就可以给引擎添加一些 DIY 功能（比如在前端加个按钮？给后端增加某种规则？），可能需要定义 install
> 版可用于动态处理的 SDK」。
> 参照系：DeepSeek Harness（dsh，tmp/deepseek-harness 本地源码调研）。
> **状态：P0+P1 已实装落地（提案当日收官，2026-10-14）**——见文末「落地」节；四问已全裁定。设计全文：[docs/plugin-sdk-design.md](../../docs/plugin-sdk-design.md)。

## 定位：不重写内核，加聚合层

agt 已有 7 类「放对位置即生效」的扩展资产——[工具外置](../features/tool-externalization.md)（`.agent/tools/`）/ 工作流+钩子 / 技能 / 子 Agent / 规则 / 服务 / MCP，另有[节点插件化](node-plugins.md)的「写两个文件即得新节点」路线——但都是**单切面**扩展。三个结构性缺口：

1. **前端零扩展点**——无法加按钮/卡片/面板（用户点名需求）
2. **无统一插件包**——想同时提供「前端按钮 + 后端路由 + 一个工具」要散落 4 个目录，无清单、无生命周期、无分发
3. **无引擎 seam**——工具执行管线（pre/post）、HTTP 路由、WS 事件、斜杠命令均不可被用户代码挂载

**核心取舍**：DSH 的「一切皆插件」要以全仓 DI 内核重写为代价（agt 是 Python 单体 + 海量现有扩展资产，大爆炸重写风险极高）——**否决重写，新增插件包聚合层**：现有 7 类协议原样复用（插件目录只是聚合壳），`backend.py` / `frontend.js` 是新增的两个一等入口，不碰引擎内核。

## DSH 调研结论（抄什么、不抄什么）

### 核心理念：一切皆插件，无特权内核

dsh 构建在 Cordis（DI/组合框架）之上：模型适配器、工具注册表、会话日志、agent loop 本身都是插件——运行中的 dsh 就是一棵插件树。扩展不是给内核打补丁，而是把插件**挂载到其它插件旁边**：插件 `apply(ctx)` 向共享上下文贡献服务、类型化事件和**可逆副作用**（注册自动记录，插件卸载时全部撤销）。

### 三种扩展点形态

| 形态 | 机制 | agt 现状对照 |
|---|---|---|
| **服务 seam** | `ctx.tools / ctx.llm / ctx.sessions / ctx.settings…` 能力注册表 | 工具表/回退链/session——但无注册表抽象 |
| **事件** | 三域（会话持久事件 / agent 实时事件 agent/* / 能力事件 fs/*、tools/*）；**waterfall 事件=拦截链**（pre-step/request/llm-stream/tools-*），必须 `next()` 放行，可返回 deny 拒绝 | 钩子工作流只覆盖轮生命周期固定挂点 |
| **UI slot** | 浏览器插件注册 `ConversationNodeDefinition`（自定义聊天卡片渲染）+ keyed renderer + `sidebar.footer.action` 席位 + `tool.view` slot | **完全没有**（index.html 单文件静态） |

### 分发与组装：profile / bundle / patch 层叠

运行中的 dsh = 一棵插件树，启动按序叠加：profile 列出的 bundle → profile 级 patch → home 级 patch → `--patch` overlay（patch 按 id 定位条目**整体替换**或插入）。bundle = 分发格式（package.json 的 `dsh` 字段）；profile = 具名组装模板（web/headless/sdk）。可发现性设计：`--dump-config` 打印的任何条目都可被自己的 patch 替换。`dsh plugin` 命令装持久外部插件；web profile 支持实时重载 patch。

### 两个激进设计

- **模型动态定义插件**：`cordis_define/run/stop/undefine` 工具——模型在会话里写插件源码、阻塞等人批准（框架级审批面板）、进程内运行、刷新即失
- **SDK = profile**：sdk profile 起 JSON-RPC host 子进程，Python SDK 与 TS SDK 同构，不引入第二个可执行入口

### 抄 / 不抄

- **抄**：插件=目录+manifest 的分发形态；`apply(ctx)`+注册即副作用可逆的生命周期；waterfall 拦截链（pre-execute 拒绝 / post-execute 变换）；UI slot 协议（席位+自定义卡片）；dump-config 式可发现性；审批门禁
- **不抄（现阶段）**：一切皆插件的 DI 内核（重写风险）；profile/bundle/patch 多档组装（agt 用 enable/disable 足够）；模型动态 define（待 SDK 稳定后再作受限工具引入）

## 插件包形态（总设计）

```
<repo>/.agent/plugins/<name>/          # repo 级（随仓库走，团队共享）
~/.agt/plugins/<name>/                 # 全局级（本机个人插件）
    manifest.yml        # 唯一必需文件：声明入口与贡献物
    backend.py          # 可选：后端入口（Python，PluginAPI）
    frontend.js         # 可选：前端入口（浏览器，AgtPlugin API）
    tools/ workflows/ agents/ skills/   # 可选：复用现有协议的静态资产
```

### P0：插件发现 + 后端 SDK（引擎 seam 最小集）

manifest.yml：`name / version / agt: ">=0.35"`（兼容范围）/ `backend` / `frontend` / `provides`（tools/workflows/agents/skills 目录声明——无代码也能挂静态资产）。

backend.py 稳定 API 面（`PluginAPI`，`setup(api)` 为唯一约定方法）：

```python
api.register_tool(name, fn, schema)                # 并入工具表（进 LLM 工具投影）
api.register_command("mycmd", handler, help)       # 斜杠命令（CLI/Web 两入口自动可用）
api.register_route("POST", "/api/my/echo", h)      # FastAPI 动态挂载
api.on_tool_pre_execute(lambda call: None | {"deny": "原因"})   # waterfall 拦截链：deny 即拒调
api.on_tool_post_execute(lambda call, result: ...)              # 结果变换/观察
api.on_turn_start(...) / api.on_turn_end(...)
api.emit({"type": "my_event", ...})                # WS 广播（前端 addChatCard 接）
```

- **生命周期**：setup 时注册即记录（注册表带 owner=插件名）→ disable/卸载按 owner 撤销全部注册（DSH 可逆副作用同款）；`reload_hot` 扩展 scope=plugins 支持热重载
- **状态持久化**：`.agent/plugins_state.json` + `~/.agt/plugins_state.json`：`{"my-plugin": {"enabled": true}}`
- **失败隔离**：单个插件 setup 抛异常 → 记 warn + 跳过，绝不炸启动

### P1：前端 slot 协议（「前端加个按钮」最小闭环）

server 新增 `GET /api/plugins`（已启用插件的 frontend_js 清单）+ 插件目录静态路由（no-cache）；index.html 启动时为每个 frontend.js 注 `<script>`，宿主在插件脚本前注入极小 SDK `window.AgtPlugin`：

```js
AgtPlugin.ui.addButton({ icon:'📊', title:'我的面板', dock:'fabbar', onClick(){...} })  // fabDock 席位
AgtPlugin.ui.addPanel({ title:'...', render(el){...} })   // 侧滑抽屉 tab（与日志/团队抽屉同款）
AgtPlugin.ui.addChatCard('my_event', (el, m) => {...})    // WS 事件自定义聊天卡片（DSH ConversationNode 同款）
AgtPlugin.ws.on('my_event', handler)                       // 订阅任意 WS 广播
AgtPlugin.api.post('/api/my/echo', {...})                  // 带回调 token 的后端调用
```

- **四个声明式席位**：fabDock / 工具 dock tab / 聊天卡片渲染表 / 设置页 tab——覆盖 95% 场景
- **信任模型**：同 DSH/Obsidian——本机自装代码全信任，不做 JS 沙箱；manifest 不设权限声明（YAGNI）
- **刷新即回放**：卡片渲染基于 WS 事件 + current_history 事件重放，与 DSH「会话卡片回放」同语义

### P2：install 分发

`agt plugin install <git-url|本地路径|pypi:xxx>` + list/enable/disable/uninstall/reload。install = 克隆到 `~/.agt/plugins/<name>/` + 可选 `pip install -r requirements.txt`；卸载 = 目录删除 + 撤销注册；manifest `agt:` 字段加载时校验。设置页「插件」tab **用 P1 的 addPanel 自举实现**（吃自己的狗粮）。

### P3（远期）：模型动态插件

`define_plugin` 工具（写 backend.py 源码 → 人审批 → 临时挂载，进程内、重启即失）——DSH `cordis_define` 的保守版，待 P0/P1 协议稳定后引入。

## 实施切分（5 步 ~1200 行）→ 实际落地对照（一轮交付）

原切分 5 步收敛为提案当日一轮交付（2026-10-14）：

| 原 5 步 | 落地 |
|---|---|
| 1 发现器 + manifest + plugins_state + 启动装配 | ✅ src/plugins.py（473 行，含 2~3 步的 PluginAPI） |
| 2 PluginAPI + 可逆撤销 | ✅ 同上单文件；接线 src/agent.py / chat.py / commands.py |
| 3 /api/plugins + 静态服务 + 前端注入器 + 三席位 | ✅ src/server.py + src/static/index.html |
| 4 CLI 子命令 + 设置页 tab | ✅ `agt plugin` CLI 落地；设置页 tab 未做（`/plugins` 命令先行） |
| 5 开发指南 + 模板插件 + wiki | ✅ docs/plugins.md + demo [.agent/plugins/example-hello/](../../.agent/plugins/example-hello/manifest.yml) + 本页 |

规模：引擎层 6 文件（新增 src/plugins.py + 5 处接线）+ 前端 SDK + demo + 文档；`/restart` 生效。

## 开放问题 → 四问全裁定（2026-10-14 提案当日，随实装收官）

四个开放问题提案当日全部按推荐裁定，随实装一并落地：

1. **工具投影污染** → ✅ 裁定：manifest 增 `exposed_to_llm` 键——`false` 时注册工具 hidden（不进 LLM 工具投影，不占 SYSTEM token，只给前端/命令用）
2. **repo 级 vs 全局级优先级** → ✅ 裁定：**repo 级覆盖全局级**（同名插件），同 main.yml 双份管理语义
3. **插件文案 i18n** → ✅ 裁定：**不纳入** dict.json 体系，frontend.js 文案插件自带
4. **信任模型显式化** → ✅ 裁定：**全信任**（同 `.agent/tools/`、Obsidian）——目录内代码被 import 执行，不做 JS 沙箱、manifest 不设权限声明；在 [docs/plugins.md](../../docs/plugins.md) 显式写明安全模型

## 落地：P0+P1 全链路实装（2026-10-14，提案当日收官）

> 本页状态已从「设计草案」翻转为**已落地**。以下为实装记录；上方设计章节保留作历史依据，与实装差异见本节末「差异对账」。

### 交付件

| 件 | 内容 |
|---|---|
| src/plugins.py（核心单文件，473 行） | 发现 + PluginAPI + 可逆撤销 + 钩子链 + 启停管理 |
| src/agent.py | `_exec_tool` 统一入口注入 pre/post 钩子 |
| src/chat.py | 启动装配 `load_plugins(agent, workspace)`（失败隔离） |
| src/commands.py | `/plugins` 命令 + `/reload plugins` + `append_commands` 插件命令并入 registry |
| src/server.py | `GET /api/plugins` 清单 + `/plugins-assets/{name}/{path}` 静态资产 |
| src/static/index.html | 前端 SDK `window.AgtPlugin` + 启动注入器 |
| .agent/plugins/example-hello/ | demo 插件（manifest.yml + backend.py + frontend.js，全 seam 演示） |
| docs/plugins.md | 开发指南（含安全模型声明） |

### src/plugins.py 职责面

- **发现**：repo `<repo>/.agent/plugins/<name>/` + 全局 `~/.agt/plugins/<name>/`，**同名 repo 覆盖全局**；manifest.yml 唯一必需（name/version/description/backend/frontend/exposed_to_llm）；一次性任务脚本形态目录拒载（防御）
- **PluginAPI（`setup(api)` 唯一约定）**：`register_tool / register_command / register_route / on_tool_pre_execute / on_tool_post_execute / emit` + `api.agent / api.workspace`
- **注册即副作用可逆（DSH 同款）**：每次注册记 owner=插件名并附 undo 闭包，disable/reload 按插件粒度整组撤销
- **失败隔离**：单插件 setup 抛异常 → warn + 跳过，绝不炸启动
- **钩子链**：`run_pre_hooks`（返回 deny 即拒）/ `run_post_hooks`（可改写结果）；无插件时懒 import 首行即返回（**零开销**）

### 引擎接线五处

1. **工具执行管线**（src/agent.py `_exec_tool`，远程路由之前）：pre 钩子 deny → 返回 `[插件 X 拒绝] <tool> 未执行：原因`，不执行；post 钩子可改写工具结果；钩子自身异常吞掉，不炸管线
2. **启动装配**（src/chat.py）：restart_tools 注册之后 `load_plugins(agent, workspace)`，异常时打印「加载失败（跳过）」
3. **斜杠命令**（src/commands.py）：`/plugins [enable|disable <名>|reload]`；`/reload` 扩 `plugins` 子命令；registry 构建尾部 `append_commands(reg)` 把插件注册的命令并入——**CLI / WebUI / WS 三处构建点自动生效**
4. **HTTP 面**（src/server.py）：`/api/plugins` 返回全部插件 + 启用态 + frontend.js 入口 URL（前端注入器消费）；`/plugins-assets/{name}/{path}` 静态资产——resolve 后必须仍在该插件目录内（防 `../` 穿越，含绝对路径注入），**no-cache**（改 frontend.js 刷新页面即生效）；`register_route` 支持进程运行中即时挂载（`_RT.attached_app` 已附着时 `_attach_one` 直挂 + undo 记录）
5. **前端注入器**（index.html）：启动 fetch `/api/plugins` → 为每个启用插件的 frontend.js 注 `<script>`；宿主在插件脚本前注入极小 SDK

### 前端 SDK（window.AgtPlugin）

- `AgtPlugin.ui.addButton / addPanel / addChatCard`——fabDock 席位 / 侧滑抽屉 tab / WS 事件自定义聊天卡片（DSH ConversationNode 同款）
- `AgtPlugin.ws.on(type, handler)` 订阅任意 WS 广播；`AgtPlugin.api.post(url, data)` 带 token 的后端调用
- `AgtPlugin.util.esc / toast` 工具件
- 插件文案不进 i18n dict（自带，见裁定 3）

### demo 插件 example-hello（2026-10-16 精简：只留 🔌 面板入口，并入随包种源）

`.agent/plugins/example-hello/`（2026-10-16 起同时入随包种源 `src/assets/plugins/example-hello/`，manifest + backend.py + frontend.js 三件）：`/phello` 命令 + 插件路由 + deny 钩子（拦一个演示工具名）+ emit 事件 → 前端卡片 + **🔌 面板一个入口**（卡片/面板/订阅演示能力保留；原 👋「插件打招呼」按钮按用户裁定删除——按钮席位让给 open-folder / feedback / webide 三个实用插件）。`/restart` 后自动加载。

### 管理面

- 会话内：`/plugins`（清单：版本/来源/启停）/ `enable|disable <名>` / `reload`（改 backend.py 热生效）；`/reload plugins` 同款
- CLI：`agt plugin list|enable|disable|install|remove`
- 启停状态持久化，disable 后 reload 不复活

### 与设计的差异对账

- `exposed_to_llm` 为实装新增 manifest 键（对应裁定 1）
- `provides` 静态资产聚合（tools/ workflows/ agents/ skills/ 目录扫描）**当前未扫**——目录存在即忽略，预留扩展点
- `register_route` 实装强于设计：运行中 app 即时挂载 + 插件粒度可逆撤销
- 设置页「插件」tab 未做（`/plugins` 命令先行，自举 addPanel 实现留待后续）

### 验证

- **单测 11 组全 PASS**：发现与同名覆盖 / 失败隔离 / 工具注册与调用 / 命令经三处 registry 构建点追加 / pre deny 拒调 / post 改写 / **零插件零开销** / 注册撤销 / disable 生效 / 静态路由防穿越（含绝对路径注入）/ FastAPI 路由挂载幂等（重复 load 不双挂）+ demo 插件真实加载
- L2 隔离实例：新代码启动 + Agent 装配 + before_turn 钩子链正常
- node --check：index.html 注入器 script 块 + demo frontend.js
- 生效方式：`/restart`（引擎层装配，改 backend.py 后 `/plugins reload` 热生效）

## 随包播种示例插件 + 内置 dock 功能插件化收官（2026-10-15/16，用户提案）

> 用户提案（2026-10-15）：「我觉得把 Open Folder 和 Feedback 作为示例功能播种吧」——把两个实用功能做成插件 SDK 的真实示例随包分发。落地时扩散成**四例播种 + 内置 dock 功能全面退役**（2026-10-16 收官）：插件体系从「新增扩展点」升级为「框架自身功能的承载形态」——dock 上 📁 打开文件夹 / 💬 反馈 / VS Code logo 三个内置按钮全部改由插件注入，引擎与前端的对应实现全删。

### seed_default_plugins()：种源 → 本地，已装不覆盖

src/plugins.py 新增播种函数（与 `seed_default_skills` 同款模式），src/chat.py 启动装配时紧跟技能播种之后调用：

- **种源**：`src/assets/plugins/<名>/`（随包分发，打包装箱）→ **播种**：`.agent/plugins/<名>/`（本 repo 生效）
- **幂等**：以 `manifest.yml` 判定存在——**已装则整插件跳过**（用户对已播种插件的任何改动永不覆盖）
- **/update-assets 纳管**：播种时写 seed_state 基线（key `plugin/<名>/<rel>`，.py/.js 后缀）——升级时对已播种插件的覆盖判定走三方 hash 比对（与技能/静态资产同款）

### 四个示例插件

| 插件 | 席位 | 要点 |
|---|---|---|
| open-folder | addButton 📁 | `POST /api/open-folder/open`——在跑 agt 的机器上弹文件管理器（win=`os.startfile` / mac=`open` / linux=`xdg-open` 三平台兜底）；`exposed_to_llm: false`（纯前端功能，不占 LLM 投影） |
| feedback | addPanel 💬 | 两条插件路由：`POST /api/feedback/meta`（类型选项/作者联系方式/环境预览）+ `POST /api/feedback/submit`；**核心逻辑零复制**——backend.py 直接 `import feedback`，落盘/飞书推送/探针过滤全继承（见 [feedback](../features/feedback.md)） |
| webide | addButton（VS Code logo） | serve-web 全链路搬迁：端口潜规则 / 复用优先 / `--commit-id` 钉缓存 / 滚动预热 / 12s 上限 / 乐观退页签原样入插件 backend.py（见 [webide](../features/webide.md)） |
| example-hello | 面板 + 命令/钩子/卡片 | demo 精简版（见上文），首次入种源 |

### 插件本地静态资产：图标随插件分发

- 既有静态路由 `/plugins-assets/<插件名>/<文件>`（`plugins_assets` → `plugin_asset`：resolve 后必须仍在该插件目录内，防 `../` 穿越）首次派上真实用场：**webide 的 vscode.png（9511B）放插件目录内随播种分发**，不再依赖 `src/static/icons/`
- `AgtPlugin.ui.addButton` 的 `icon` 字段收 HTML——`icon: '<img src="/plugins-assets/webide/vscode.png" ...>'` 即任意自定义图标；插件自带 css/字体/图片同理（放插件目录、走同一路由）

### SDK 增强：addButton onClick 事件透传

`addButton` 的 `onClick` 由无参升级为**透传事件对象**——插件可拿按钮元素引用做 loading 态（webide 沿用内置版 `.loading` class 手法：半透明 + progress 光标，避免 textContent 覆盖 `<img>` 图标的旧坑，见 [fab-dock](../features/fab-dock.md)）。

### SDK 增强：addButton position:'input'——输入框上方工具条席位（2026-10-15，用户提案，commit 106c049）

> 用户提问：「只能是右侧的图标抽屉里的按钮吗？可以是消息窗口上面的按钮吗？」——按钮席位从一个变两个：同一 API、`position` 参数选位。

- **`position: 'fab'`（缺省）**：右侧 dock 图标（原行为，向后兼容——现有插件不动，想挪入输入条改一行即可）
- **`position: 'input'`**：**输入框上方工具条**——新容器 `#pluginInputBar`（src/static/index.html，DOM 顺序在 `#inputBar` 之前），icon + 文字**横排胶囊小按钮**（`.pluginInputBtn`：圆角 16px、浅底细边框、12.5px），更贴消息流、更醒目；容器默认 `display:none`，有插件注册按钮即 flex 显示（wrap 可换行）
- API 形态：`AgtPlugin.ui.addButton({ position:'input', icon:'📁', label:'打开文件夹', onClick(){...} })`——其余字段（onClick 事件透传、icon 收自定义 HTML）与 fab 位完全一致
- 布局配套：drawer-push（侧滑抽屉推开内容）横竖屏两组选择器同步纳入 `#pluginInputBar`——抽屉打开时不错位
- 验证（隔离实例 Playwright 实测 + 截图）：注册两按钮 → 容器自动显示、DOM 顺序 `[pluginInputBar, inputBar]`（确在输入框上方）、胶囊文本/样式正确
- 生效方式：纯前端（index.html 磁盘 serve），Ctrl+F5 即生效

### 内置全面退役：单一实现源收敛到插件

- index.html **-93 行**：📁/💬/VS Code logo 三按钮 DOM + `openWebIde()` + 反馈弹窗 DOM/JS + open-workspace fetch
- server.py **-177 行**：`/api/open-workspace`、`POST /api/ide/open`（`_ide_payload` / serve-web 拉起 / 滚动预热）、WS 反馈提交分支全删
- 同一功能从此只有插件一份实现——`/plugins disable feedback` 反馈面板即消失（**启停管理对内置功能同样成立**）

### 验证

- 8 份 JS 语法 node --check 全过（四插件 frontend.js + index.html 注入器等）
- 干净 workspace 冷启动：自动播种 4 插件（含 vscode.png 图标资产）；重跑幂等不覆盖
- seed_state 基线 12 条落盘（`/update-assets` 可对账）
- 生效方式：`/restart`

## 开发文档播种 + main.yml 扩展点索引：新 repo 开箱即懂（2026-10-16，用户中途补充提案）


> 用户中途补充（随示例插件播种同轮）：「如何开发这些插件的文档 README.md 也需要播种一下……默认 main.yml 里最好要提一下所有这些特殊目录中的 README.md，以便在新的 repo 中 agent 能发现这些拓展方式」——两件都落在**新 repo 开箱链路**上：① 文档给「想写插件的人和 Agent」，② 索引给「运行中的 Agent」。

### ① 插件开发 README：src/dir_docs/plugins.md → .agent/plugins/README.md

- 84 行一页通，七块内容：目录三件结构（manifest.yml 必需 + backend.py / frontend.js / 静态资产可选）、manifest 字段表、**backend 装配协议**（`setup(api)`：register_tool · register_command · register_route · on_tool_pre/post_execute · emit 全带示例）、**前端 AgtPlugin SDK**（addButton · addPanel · addChatCard · ws.on · api.post 全带示例）、**自定义图标机制**（图标放插件目录，经 `/plugins-assets/<名>/<文件>` 引用，同 css/字体等任意资产）、开发调试（backend `/reload plugins` 热生效、frontend 刷新即生效）、自己的插件进随包分发的路径（`src/assets/plugins`）
- 接线：src/agent_config.py `seed_dir_docs` 目录循环加 `(".agent/plugins", "plugins")`——六个扩展目录凑齐，**新 repo 首启自动播种**；本 repo 已播种 + seed_state 基线记账（/update-assets 可对账）
- 播种管道语义见 [workspace 播种](../features/workspace-seeding.md)；与 docs/plugins.md（引擎侧开发指南 + 安全模型声明）双文档并存：README 面向新 repo 入门，docs 面向协议细节

### ② main.yml 扩展点索引：SYSTEM 注入【扩展点·特殊目录】段

- 种源 src/assets/main.yml + 当前全局 main.yml **双份同步**（双份均过 YAML 结构校验）；插在【随包资产】段之后
- 内容：六个扩展目录各配一句「放什么 + 入口在哪」——`.agent/plugins/`（引擎级插件：dock 按钮/侧滑面板/HTTP 路由/工具/钩子；随包示例 open-folder·feedback·webide）、`.agent/agents/`（子 Agent 声明）、`.agent/workflows/`（工作流 XML）、`.agent/skills/`（技能包）、`.agent/tools/`（脚本工具）、`.agent/nodes/`（工作流节点插件）
- 效果：新 repo 的主 Agent 从 SYSTEM 就知道「给引擎/界面加能力前，先读对应目录的 README」——扩展点发现从「靠文件系统漫游碰运气」变成「声明式索引」；生效：当前实例 /restart 后生效，种源对新 repo 天然生效

## 插件路由 422 根治：future annotations × FastAPI 注解 resolve（2026-10-15/16，隔离实例实锤）

> 播种 + 文档收官后的**端到端首验**（隔离实例真跑四插件）暴露：三个插件的 HTTP 路由 POST **一律 422 Unprocessable Content**（detail `loc: ["query","request"]`）。本地复现三层剥根因，真凶是一个隐蔽的 Python 语义组合拳。至此插件化全链路（种源/播种/路由/前端/图标/文档/main.yml 索引）端到端闭环。

### 三层剥根因

1. **表层**：包装层 `async def _handler(request):` 无类型注解 → FastAPI 把 request 当**必填 query 参数**，POST 无此参数一律 422。第一直觉修复：函数体内 `from fastapi import Request` + `request: Request` 补注解。
2. **二层打脸**：补了注解**仍 422**（本地最小复现锁定——不是隔离实例环境问题，是写法本身有问题）。
3. **真凶**：src/plugins.py 顶部有 `from __future__ import annotations`——全文件注解**字符串化**。FastAPI 用 `get_type_hints` 按函数所属**模块 globals** resolve 字符串注解，而 Request 是在**函数体内** import 的——名字不在模块 globals，resolve 失败被**静默吞掉** → 注解等于没写 → 照旧当 query 参数。对照实验：裸 FastAPI 文件（无 future import）同款「函数体内 import + 注解」写法 200——差异变量唯一锁定。

### 修复（两层）

| 层 | 改动 | 说明 |
|---|---|---|
| 引擎（根治） | `from fastapi import Request as _FastAPIRequest` 提**模块级**；包装层注解改 `request: "_FastAPIRequest"` | 字符串注解按模块 globals resolve——模块级 import 的名字**可达**。SDK 契约不要求插件懂 FastAPI：引擎包装层统一补注解，插件侧 `fn(request)` 无注解照写 |
| 种源插件（防御） | open-folder / feedback / webide 三个 backend.py 补 `request: 'Request'` + 各自文件内 import | 双保险：即使引擎旧版（不补注解）也工作 |

### 终验（隔离实例五项全绿）

- `/api/plugins` 清单 4 enabled（example-hello / open-folder / feedback / webide）
- feedback meta + submit 双 200（submit **真提交成功**——真推了一条「冒烟测试」反馈到飞书，用户可见）
- webide 图标 vscode.png 200（静态资产路由）
- open-folder 200
- 本机 9000 实例 `/restart` 后：dock 上 📁/💬/VS Code 三图标均来自插件注入——内置退役 + 插件承载实机闭环

### 注意事项

- **future annotations × 框架运行时读注解是通用坑**：任何运行时 `get_type_hints` 解析注解的框架（FastAPI / Pydantic / typer 等）都有同款雷——字符串注解的 resolve 只看模块 globals，函数体内 import 不可达
- 插件作者无感知：SDK 契约是 `fn(request)` 裸写，注解由引擎包装层负责；种源插件里的注解纯属防御
- 与「验证」节中既有 11 组单测的互补：单测覆盖路由挂载幂等，但当时未覆盖 POST 真调用——端到端首验的价值正在于此

## 关联


- 实装核心：src/plugins.py（引擎层接线 5 处：agent.py / chat.py / commands.py / server.py / index.html，见上文「落地」节）
- 开发指南：[docs/plugins.md](../../docs/plugins.md)（插件目录约定 + PluginAPI + AgtPlugin + 安全模型声明）；设计全文：[docs/plugin-sdk-design.md](../../docs/plugin-sdk-design.md)
- [workspace 播种](../features/workspace-seeding.md) — seed_dir_docs 播种管道：随包插件 README（src/dir_docs/plugins.md，见上文「开发文档播种」节）与其余 5 份目录说明同管道分发到各 repo；main.yml【扩展点·特殊目录】索引同见上文
- demo 插件：[.agent/plugins/example-hello/](../../.agent/plugins/example-hello/manifest.yml)
- [节点插件化](node-plugins.md) — 单切面先行示范（工作流节点「写两个文件即得」）；本方案是聚合层推广
- [工具外置](../features/tool-externalization.md) — `.agent/tools/` 协议被插件 register_tool 同款复用
- [工作流引擎与钩子](workflow-hooks.md) — 钩子=固定挂点的工作流；插件 on_tool_pre/post=代码级 waterfall 拦截链
- [fabDock](../features/fab-dock.md) — addButton 的首个挂载席位；[WebUI 工具表单](../features/tool-form.md) — 插件 register_tool 的工具箱消费端
- [架构总览](overview.md)

