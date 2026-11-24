# .agent/plugins/ —— 引擎级插件（P1 SDK）

给 WebUI/引擎加自定义能力的**最高层扩展点**：一个文件夹 = 一个插件，可含后端（HTTP 路由/工具/命令/钩子）+ 前端（dock 按钮/侧滑面板/聊天卡片）+ 静态资产（图标等）。随包已播种六个实用示例：`open-folder`（打开文件夹）、`feedback`（用户反馈）、`webide`（VS Code 工作台）、`cmd`（`/cmd <shell 命令>` 消息框直接执行——register_command 用法示例）、`screenshot`（`/screenshot <路径>` 前端页面截图——「后端命令触发 + 前端 JS 干活 + 后端路由落盘」三桥范式示例）、`record`（`/record <start|pause|resume|stop|save|flush>` 页面录屏——三桥进阶：子命令状态机 + 浏览器激活处理 + 大文件回传）。

## 目录结构（最小三件）

```
.agent/plugins/<name>/
  manifest.yml    # 声明（见下）
  backend.py      # 后端（可选）：模块级 setup(api) 装配
  frontend.js     # 前端（可选）：IIFE，用全局 AgtPlugin SDK
  vscode.png      # 任意静态资产（可选）：经 /plugins-assets/<name>/<file> 引用
```

## manifest.yml

```yaml
name: my-plugin
version: 1.0.0
description: 一句话用途（/plugins 页与设置页展示）
backend: backend.py      # 缺省=纯前端插件
frontend: frontend.js    # 缺省=纯后端插件
exposed_to_llm: false    # true 时插件的工具进 Agent 工具箱
```

## backend.py —— 后端装配协议

模块级 `setup(api)` 在加载时被调用（`api` = PluginAPI）：

```python
async def _my_route(request):
    return 200, {"ok": True}          # (status, body) 或直接 dict

def setup(api):
    api.register_route("POST", "/api/my-plugin/do", _my_route)   # HTTP 路由（挂 FastAPI）
    api.register_tool("my_tool", my_func, "工具描述")            # Agent 工具（类型注解自动推 schema）
    api.register_command("mycmd", lambda ctx, args: "done", "命令帮助")  # /mycmd（WS+CLI）
    api.on_tool_pre_execute(lambda name, args: None)   # 可返回 {"deny": "原因"} 拦截
    api.on_tool_post_execute(lambda name, args, result: result)  # 可改写结果
    api.emit({"type": "my_event", "detail": "..."})    # 广播 WS 事件给前端
```

只读引用：`api.workspace`（工作区 Path）/ `api.agent`（主 Agent 实例，可能为 None）/ `api.log()`。
全部注册记 undo：unload/`/reload plugins` 自动撤销、运行中注册即时生效。可直接 `import` 引擎模块（同进程，如 `from feedback import submit_feedback`）。

## frontend.js —— 前端 SDK（全局 AgtPlugin）

```js
(function () {
  // dock 按钮（第二列图标）
  AgtPlugin.ui.addButton({ icon: '📁', label: '打开', background: '#ca8a04',
                           onClick: async (ev) => { /* ev.currentTarget=按钮 */ } });
  // 侧滑面板（自动带 dock 入口；render 首次打开时调用，可放任意表单）
  AgtPlugin.ui.addPanel({ icon: '🔌', title: '我的面板', background: '#0f766e',
                          render: async (el) => { el.innerHTML = '...'; } });
  // 聊天卡片：拦截指定 type 的 WS 事件自绘气泡
  AgtPlugin.ui.addChatCard('my_event', (bubble, m) => { bubble.innerHTML = '...'; });
  // 事件订阅 / HTTP / 工具
  AgtPlugin.ws.on('answer', (m) => console.info(m.text));
  const d = await AgtPlugin.api.post('/api/my-plugin/do', { x: 1 });   // 或 api.get(path)
  AgtPlugin.util.toast('提示');  AgtPlugin.util.esc('<b>');            // 转义
})();
```

## 自定义图标/静态资产

文件放**插件目录内**（png/svg/jpg/css 均可），前端经 `/plugins-assets/<插件名>/<文件名>` 引用：

```js
AgtPlugin.ui.addButton({ icon: '<img src="/plugins-assets/my-plugin/logo.png" style="width:22px;height:22px;display:block">', ... })
```

安全边界：路径 resolve 后必须在插件目录内（防逃逸）。参考示例：webide 的 vscode.png。

## 开发调试

- 改 backend.py → `/reload plugins` 热生效（免重启）
- 改 frontend.js → 刷新浏览器即生效（no-cache 注入）
- 启用/禁用：设置页插件区 或 `/plugins` 页；两级目录：repo 级 `.agent/plugins/` 覆盖全局级 `~/.agt/plugins/` 同名
- 本目录随包播种的示例插件升级走 `/update-assets`（三方 hash：种源/基线/本地不一致才提示）

## 层间转移（repo ⇄ 全局）

已装插件可整目录在两层之间**移动**（不是复制——源目录移除）：

- **市场页**（/market）：插件卡片「⇪ 移到全局 / ⇩ 移到项目」按钮；目标层已有同名时二次确认，
  确认后用源这份覆盖（--force 语义）。不在市场索引的**本地插件**也在此清单中（标「本地」徽章），
  无安装/重装/卸载（无随包源），仅可层间转移。
- **会话命令**：`/plugins move <名> [--to repo] [--force]`——默认 → 全局；成功后自动热重载插件。
- **CLI**：`agt plugin move <名> [--to repo] [--force]`——纯文件操作（CLI 无 agent），提示重启或
  `/reload plugins` 生效。

安全语义（`market.move_plugin`）：copytree 到目标层落位目录下的临时名（防跨盘 EXDEV，
ignore `__pycache__`/`*.pyc`）→ 校验副本 manifest 可解析 → force 时旧目标先原子挪开（不直接删，
就位后再清）→ `os.replace` 原子就位 → 删源。就位前任一步失败：清理临时目录、源与旧目标保持
原样（`[失败]` 前缀）；就位后删源失败则如实提示手动清理（此时两层都在，repo 级仍生效）。
两层指向同一目录（如 AGT_HOME 落在 `<ws>/.agt`）时拒绝转移；中断残留的 `.<名>.moving-*`
不被当作插件，下次转移同名时顺手清掉。
全局层落位 `AGT_DIR/plugins/`（即 `~/.agt/plugins/`；AGT_HOME 隔离环境自动跟随），
HTTP 入口 `POST /api/marketplace/move {name, to, force?}`（成功带热重载，冲突返回 `conflict: true`）。

## 播种自己写好的插件（进随包分发包）

放到 `src/assets/plugins/<name>/`（pip 包内）→ 新 workspace 首启 `seed_default_plugins` 自动播种（已装同名跳过）。manifest.yml 是存在判据。
