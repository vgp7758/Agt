# 插件 SDK · 引擎级全栈插件方案（草案 v0 · 2026-10-14 · 未实施）

> 2026-10-14 用户提案：「支持更多引擎层的可拓展插件，允许用户只需要追加一些自己的代码放在指定位置，
> 就可以给引擎添加一些 DIY 功能（比如在前端加个按钮？给后端增加某种规则？），可能需要定义 install
> 版可用于动态处理的 SDK」。
> 参照系：DeepSeek Harness（dsh，tmp/deepseek-harness 本地源码调研）。
> **状态：设计草案，4 个开放问题待用户裁定后开工**（见文末）。设计全文：[docs/plugin-sdk-design.md](../../docs/plugin-sdk-design.md)。

## 定位：不重写内核，加聚合层

agt 已有 7 类「放对位置即生效」的扩展资产——[工具外置](../features/tool-externalization.md)（tools/builtin）/ 工作流+钩子 / 技能 / 子 Agent / 规则 / 服务 / MCP，另有[节点插件化](node-plugins.md)的「写两个文件即得新节点」路线——但都是**单切面**扩展。三个结构性缺口：

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

## 实施切分（5 步 ~1200 行，每步独立可交付）

| 步 | 内容 | 规模 |
|---|---|---|
| 1 | 插件发现器 + manifest 解析 + plugins_state + 启动装配（provides 静态资产聚合） | ~200 行 |
| 2 | PluginAPI：register_tool/command/route + on_tool_pre/post_execute + emit；注册表可逆撤销 | ~300 行 |
| 3 | /api/plugins + 静态服务 + 前端 AgtPlugin 注入器 + addButton/addPanel/addChatCard | ~250 行 |
| 4 | `agt plugin` CLI 子命令 + 设置页插件 tab | ~200 行 |
| 5 | 插件开发指南 + 模板插件 example/ + wiki | 文档 |

依赖 1→2→3→4；每步后均有可验证交付（示例插件随步构建）。

## 开放问题（待用户裁定，裁定后开工）

1. **工具投影污染**：register_tool 默认进 LLM 工具表会占 SYSTEM token——要不要 `exposed_to_llm: false` 选项（只给前端/命令用）？
2. **repo 级 vs 全局级优先级**：同名插件谁赢（建议 repo 覆盖全局，同 main.yml 双份管理语义）？
3. **插件文案 i18n**：frontend.js 文案是否纳入 dict.json 体系（建议先不纳，插件自带）？
4. **信任模型显式化**：backend.py 有全部引擎权限，要不要 SAFETY.md 式显式安全声明？

## 关联

- 设计全文：[docs/plugin-sdk-design.md](../../docs/plugin-sdk-design.md)
- [节点插件化](node-plugins.md) — 单切面先行示范（工作流节点「写两个文件即得」）；本方案是聚合层推广
- [工具外置](../features/tool-externalization.md) — tools/builtin 协议被插件 provides.tools 原样复用
- [工作流引擎与钩子](workflow-hooks.md) — 钩子=固定挂点的工作流；插件 on_tool_pre/post=代码级 waterfall 拦截链
- [fabDock](../features/fab-dock.md) — P1 addButton 的首个挂载席位
- [架构总览](overview.md)
