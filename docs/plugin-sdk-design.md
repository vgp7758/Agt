# Agt 插件 SDK 设计方案（草案 v0）

> 2026-10-14 用户提案：「支持更多引擎层的可拓展插件，允许用户只需要追加一些自己的代码放在指定位置，
> 就可以给引擎添加一些 DIY 功能（比如在前端加个按钮？给后端增加某种规则？），可能需要定义
> install 版可用于动态处理的 SDK」。
> 参照系：DeepSeek Harness（dsh，tmp/deepseek-harness 本地源码）。

## 一、DSH 是怎么做的（调研结论）

### 1. 核心理念：一切皆插件，无特权内核

dsh 构建在 Cordis（DI/组合框架，论文 *A Programming Paradigm for Spatiotemporal Composability*）之上：
**产品的每一部分都是插件**——模型适配器、工具注册表、会话日志、agent loop 本身。
扩展方式不是给内核打补丁，而是把插件**挂载到其它插件旁边**；插件 `apply(ctx)` 向共享上下文贡献
服务、类型化事件和**可逆副作用**（注册自动记录，插件卸载时全部撤销）。

```ts
export const name = 'permission-gate'
export function apply(ctx: Context) {
  ctx.on('tools/pre-execute', async (exec, next) => {
    if (!(await isAllowed(exec))) return { kind: 'deny', reason: '...' }
    return next()          // waterfall：调 next() 才委托给下一个监听器
  })
}
```

### 2. 三种扩展点形态

| 形态 | 机制 | agt 对应物 |
|---|---|---|
| **服务 seam** | `ctx.tools / ctx.llm / ctx.sessions / ctx.settings…` 能力注册表（capability-seams 文档全量列出） | 工具表/回退链/session——但 agt 无注册表抽象 |
| **事件** | 三域：会话持久事件（session/event 落盘广播）/ agent 实时事件（agent/*：inbox、步骤、流、轮边界）/ 能力事件（fs/*、tools/*、telemetry/*）；**waterfall 事件**（pre-step/request/llm-stream/tools-*）= 拦截链，必须 `next()` | 钩子工作流只覆盖轮生命周期固定挂点 |
| **UI slot** | 浏览器插件注册 `ConversationNodeDefinition`（自定义聊天卡片渲染）+ keyed renderer + `sidebar.footer.action` 席位（面板挂载点）+ `tool.view.cordis` slot（工具结果业务视图） | **完全没有**（index.html 单文件静态） |

### 3. 分发与组装：profile / bundle / patch 层叠

- 运行中的 dsh = **一棵插件树**，启动时按序叠加：profile 列出的 bundle → profile 级 `cordis.patch.yml` → home 级 patch → `--patch` overlay。patch 按 id 定位条目**整体替换**或**插入新条目**。
- bundle（组合包）= Cordis 配置项+挂载代码的分发格式，`package.json` 的 `dsh` 字段声明；profile（web/headless/sdk/sdk-minimal/acp）= 具名组装模板。
- `dsh --profile web --dump-config` 打印完整配置树——**打印出的任何条目都可被自己的 patch 替换**（可发现性）。
- `dsh plugin` 命令安装持久外部插件；桌面版 `$DSH_HOME/profiles/desktop` 存外部插件，升级保留插件文件只刷新链接。
- web profile 支持实时重载 patch；headless/sdk 启动时应用一次（生命周期所限）。

### 4. 两个激进设计

- **模型动态定义插件**：`cordis_define/run/stop/undefine` 工具——模型在会话里写插件源码、阻塞等待人批准（框架级审批面板）、运行（进程内，刷新即失）。模型自己扩展自己。
- **SDK = profile**：`sdk` profile 起 JSON-RPC host 子进程，Python SDK 与 TS SDK 同构；不引入第二个可执行入口。

### 5. 值得抄的与不该抄的

**抄**：插件=目录+manifest 的分发形态；`apply(ctx)`+注册即副作用可逆的生命周期；waterfall 拦截链（pre-execute 拒绝/post-execute 变换）；UI slot 协议（席位+自定义卡片）；dump-config 式可发现性；审批门禁。

**不抄**（现阶段）：一切皆插件的 DI 内核（agt 是 Python 单体+海量现有扩展资产，大爆炸重写风险极高）；profile/bundle/patch 层叠组装（agt 用户量级用不上多 profile 组装，enable/disable 足够）；模型动态 define（待 SDK 稳定后再作为受限工具引入）。

## 二、agt 现状盘点：7 类扩展资产，3 个结构性缺口

已有（各自独立约定，全部「放对位置即生效」）：

| 扩展点 | 位置 | 生效方式 |
|---|---|---|
| 工具 | `tools/builtin/*.py`（repo） | 启动扫描注册 + reload_hot 热重载 |
| 工作流（含钩子） | `.agent/workflows/*.xml` | 每轮扫描成工具；钩子挂 before_turn/before_answer/turn_end 等固定挂点 |
| 技能 | `.agent/skills/<name>/` | SKILL.md+scripts，可 equip 成服务 |
| 子 Agent | `.agent/agents/<name>/` | 声明即读即用 |
| 规则/知识 | `.agent/rules/*.md`、wiki | 拼 SYSTEM |
| 服务 | main.yml services / start_service | 实例级后台进程 |
| MCP | mcp.json | 外部工具 server |

缺口：
1. **前端零扩展点**——无法加按钮/卡片/面板（用户点名需求）；
2. **无统一插件包**——想同时提供「前端按钮 + 后端路由 + 一个工具」要散落 4 个目录，无清单、无生命周期、无分发；
3. **无引擎 seam**——工具执行管线（pre/post）、HTTP 路由、WS 事件、斜杠命令均不可被用户代码挂载。

## 三、设计：三层渐进（不重写内核，加聚合层）

### 总形态

```
<repo>/.agent/plugins/<name>/          # repo 级（随仓库走，团队共享）
~/.agt/plugins/<name>/                 # 全局级（本机个人插件）
    manifest.yml        # 唯一必需文件：声明入口与贡献物
    backend.py          # 可选：后端入口（Python，SDK API）
    frontend.js         # 可选：前端入口（浏览器，AgtPlugin API）
    tools/              # 可选：外置工具（复用现有 tools/builtin 协议）
    workflows/          # 可选：工作流 XML（复用现有协议）
    agents/             # 可选：子 Agent 声明（复用现有协议）
    skills/             # 可选：技能包
```

**原则**：现有 7 类扩展协议**原样复用**（插件目录只是聚合壳）；backend.py / frontend.js 是新增的两个一等入口；不碰引擎内核。

### P0：插件发现 + 后端 SDK（引擎 seam 最小集）

manifest.yml：

```yaml
name: my-plugin
version: 0.1.0
agt: ">=0.35"           # 兼容范围
description: 一句话
backend: backend.py     # 可选入口
frontend: frontend.js   # 可选入口
provides:               # 声明式贡献（无代码也能挂静态资产）
  tools: [tools]        # 目录相对插件根
  workflows: [workflows]
  agents: [agents]
  skills: [skills]
```

backend.py（稳定 API 面，`PluginAPI`）：

```python
class MyPlugin:
    def setup(self, api):          # 唯一约定方法；api 由引擎注入
        # —— 注册型 seam ——
        api.register_tool(name, fn, schema)          # 并入工具表（进 LLM 工具投影）
        api.register_command("mycmd", handler, help) # 斜杠命令（CLI/Web 两入口自动可用）
        api.register_route("POST", "/api/my/echo", handler)   # FastAPI 动态挂载
        # —— 事件型 seam（waterfall 拦截链，同 DSH）——
        api.on_tool_pre_execute(lambda call: None | {"deny": "原因"})   # 返回 deny 即拒绝
        api.on_tool_post_execute(lambda call, result: result | 改写)   # 结果变换/观察
        api.on_turn_start(lambda ctx: None)
        api.on_turn_end(lambda ctx: None)
        # —— 输出 ——
        api.emit({"type": "my_event", "text": "..."})   # WS 广播（前端 addChatCard 接）
        api.log("...")
```

- **生命周期**：setup 时注册即记录（注册表带 owner=插件名）→ disable/卸载按 owner 撤销全部注册（DSH 可逆副作用同款）；`reload_hot` 扩展 scope=plugins 支持热重载。
- **状态持久化**：`.agent/plugins_state.json`（repo 级）+ `~/.agt/plugins_state.json`（全局）：`{"my-plugin": {"enabled": true}}`。
- **失败隔离**：单个插件 setup 抛异常 → 记 warn 事件 + 跳过，绝不炸启动（蜜月窗口静默）。

### P1：前端 slot 协议（「前端加个按钮」的最小闭环）

- server 新增 `GET /api/plugins`：返回已启用插件的 `{name, frontend_js}` 清单。
- index.html 启动时 fetch 清单，为每个 frontend.js 注 `<script src="/plugins/<name>/frontend.js">`
  （server 开静态路由 `~/.agt/plugins` 与 repo plugins 目录，带 no-cache）。
- 浏览器侧极小 SDK（`window.AgtPlugin`，由宿主在插件脚本前注入）：

```js
AgtPlugin.ui.addButton({ icon: '📊', title: '我的面板', dock: 'fabbar', onClick: () => {...} })
AgtPlugin.ui.addPanel({ title: '...', render(el) {...} })            // 侧滑面板（与日志/团队抽屉同款）
AgtPlugin.ui.addChatCard('my_event', (el, m) => {...})               // 自定义 WS 事件的聊天卡片（DSH ConversationNode 同款）
AgtPlugin.ws.on('my_event', handler)                                  // 订阅任意 WS 事件（复用现有广播）
AgtPlugin.api.post('/api/my/echo', {...})                             // 带回调 token 的后端调用
AgtPlugin.util.esc(s) / toast(msg)                                    // 复用宿主工具
```

- **挂载席位**：fabDock（右下浮动按钮坞）/ 工具 dock（右侧抽屉 tab）/ 聊天卡片渲染表 / 设置页 tab——四个声明式席位，够覆盖 95% 场景。
- **信任模型**：同 DSH/Obsidian——本机自装代码全信任，不做 JS 沙箱；manifest 不设权限声明（YAGNI，远期可加）。
- **刷新即回放**：卡片渲染基于 WS 事件 + 事件重放（current_history 里自定义事件原样下发），与 DSH「会话卡片回放已记录调用」同语义。

### P2：install 分发

```
agt plugin install <git-url | 本地路径 | pypi:agt-plugin-xxx>
agt plugin list / enable / disable / uninstall / reload
```

- install = 克隆/复制到 `~/.agt/plugins/<name>/` + 可选 `pip install -r requirements.txt`（隔离 venv 远期）；
- 卸载=目录删除 + 撤销注册；版本兼容由 manifest `agt:` 字段在加载时校验。
- WebUI 设置页新增「插件」tab（列出/开关/装/卸）——**用 P1 的 addPanel 自举实现**（吃自己的狗粮）。

### P3（远期）：模型动态插件

给 Agent 一个 `define_plugin` 工具（写 backend.py 源码 → 人审批 → 临时挂载，进程内、重启即失）——
DSH `cordis_define` 的保守版，待 P0/P1 协议稳定后引入。

## 四、实施切分（每步独立可交付）

| 步 | 内容 | 规模预估 |
|---|---|---|
| 1 | 插件发现器 + manifest 解析 + plugins_state + 启动装配（provides 静态资产聚合） | ~200 行 |
| 2 | PluginAPI：register_tool/command/route + on_tool_pre/post_execute + emit；注册表可逆撤销 | ~300 行 |
| 3 | /api/plugins + 静态服务 + 前端 AgtPlugin 注入器 + addButton/addPanel/addChatCard | ~250 行 |
| 4 | `agt plugin` CLI 子命令 + 设置页插件 tab | ~200 行 |
| 5 | 文档（插件开发指南 + 模板插件 example/）+ wiki | ~文档 |

依赖关系：1→2→3→4；每步后均有可验证交付（示例插件随步构建）。

## 五、开放问题（需用户裁定）

1. **工具投影污染**：register_tool 的工具默认进 LLM 工具表会占 SYSTEM token——是否需要 `exposed_to_llm: false` 选项（只给前端/命令用）？
2. **repo 级 vs 全局级优先级**：同名插件谁赢（建议 repo 级覆盖全局级，与 main.yml 双份管理同语义）？
3. **frontend.js 的 i18n**：插件文案是否纳入 dict.json 体系（建议先不纳，插件自带文案）？
4. **沙箱边界**：backend.py 有全部引擎权限（信任模型），是否需要在文档中显式安全声明（SAFETY.md 同款）？
