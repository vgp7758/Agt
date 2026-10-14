# Agt 插件开发指南

> 引擎级插件系统（2026-10-14 用户提案）：一个目录 + manifest.yml = 给引擎追加 DIY 能力——
> 注册工具/斜杠命令/HTTP 路由/工具执行钩子，前端加按钮/侧滑面板/自定义聊天卡片。
> 设计文档：[plugin-sdk-design.md](plugin-sdk-design.md)（DSH 调研与取舍）。

## 五分钟上手

一个插件 = 一个目录，放在：

| 位置 | 语义 | 生效 |
|---|---|---|
| `<repo>/.agent/plugins/<name>/` | repo 级（随仓库走，团队共享；**同名覆盖全局**） | 启动加载 / `/reload plugins` 热生效 |
| `~/.agt/plugins/<name>/` | 全局级（本机个人） | 同上；`agt plugin install` 装这里 |

最小目录（manifest 唯一必需）：

```
.agent/plugins/my-plugin/
    manifest.yml     # 清单
    backend.py       # 可选：后端入口（setup(api)）
    frontend.js      # 可选：前端入口（浏览器 AgtPlugin）
```

manifest.yml 字段：

```yaml
name: my-plugin          # 唯一必需（缺省用目录名）；仅字母/数字/_/-
version: 0.1.0
description: 一句话描述
backend: backend.py      # 后端入口文件名（相对插件根；缺省无后端）
frontend: frontend.js    # 前端入口（缺省无前端）
exposed_to_llm: true     # false=本插件注册的工具不投影给 LLM（hidden，仅工作流/编辑器可用）
```

完整示例：`.agent/plugins/example-hello/`（随 repo 附带，覆盖全部 seam）。

## 后端 SDK（backend.py）

约定：模块级 `setup(api)`，引擎装配期调用（`plugins.load_plugins`）。`api` 全部方法
**注册即生效并记撤销**——disable/reload 时本插件全部注册自动回滚。

```python
def setup(api):
    # —— 注册型 ——
    api.register_tool("my_tool", my_func, description="给 LLM 用的工具")
    #   my_func 需类型注解（schema 自动推断）；同名覆盖现有工具（覆写机制同 tools/builtin）
    #   工具名建议带插件前缀防撞（如 hello_greet）
    api.register_command("mycmd", handler, help_text="斜杠命令")
    #   handler(ctx, args)：CLI 与 WebUI 输入框两入口自动可用
    api.register_route("POST", "/api/my/echo", handler)
    #   handler(request) -> (status, body)；server 启动时统一挂载（CLI 模式挂起，/web 起服务即挂）

    # —— 工具执行管线钩子（waterfall）——
    api.on_tool_pre_execute(lambda name, args:
        {"deny": "拒绝原因"} if 想拦截(name, args) else None)   # deny → 工具不执行，模型可见拒绝文本
    api.on_tool_post_execute(lambda name, args, result:
        result + "\n附注" if name == "某工具" else None)          # 返回非 None 替换结果；None=观察

    # —— 输出与引擎访问 ——
    api.emit({"type": "my_event", "text": "..."})   # WS 广播（前端 addChatCard/ws.on 接）
    api.log("...")                                   # 引擎日志
    api.agent      # 主 Agent 只读引用（session/tools 等；勿持有跨 reload）
    api.workspace  # Path：当前 workspace
```

防御与隔离：
- backend.py 模块级 `while/sleep/input` 会被**拒绝加载**（任务脚本防御，同 tools/builtin）；
- setup 抛异常 → 该插件跳过并 warn，**绝不炸启动**；
- 改 backend.py → `/reload plugins`（或 `reload_hot(scope="plugins")`）热生效，免 /restart。

## 前端 SDK（frontend.js）

index.html 启动时 fetch `/api/plugins` → 为每个启用插件的 frontend.js 注入 `<script>`
（`/plugins-assets/<name>/frontend.js`，no-cache——改完**刷新页面即生效**）：

```js
// 聊天卡片：后端 api.emit({"type":"my_event",...}) → 自绘卡片（终止默认处理）
AgtPlugin.ui.addChatCard('my_event', (bubble, m) => {
  bubble.innerHTML = '<div style="...">' + AgtPlugin.util.esc(m.text) + '</div>';
});

// dock 按钮（右侧工具坞 🧰 展开列）：{icon,label,background,badge,onClick}
AgtPlugin.ui.addButton({ icon: '📊', label: '我的面板', onClick: () => panel.toggle() });

// 侧滑面板（与日志/团队抽屉同款）：{icon,title,render(el)}——首次打开时渲染
const panel = AgtPlugin.ui.addPanel({
  icon: '📊', title: '我的面板',
  render: async (el) => {
    const r = await AgtPlugin.api.post('/api/my/echo', {});   // 调后端路由（含插件路由）
    el.innerHTML = '<pre>' + AgtPlugin.util.esc(JSON.stringify(r)) + '</pre>';
  },
});

// WS 事件订阅（不渲染，纯观察）
AgtPlugin.ws.on('answer', m => console.info('answer 长度', (m.text||'').length));

// 工具
AgtPlugin.util.esc(s)   // HTML 转义（ XSS 防线——动态文本务必过它）
AgtPlugin.util.toast(m) // 轻提示
```

注意：插件卡片渲染**实时事件可靠**；页面刷新后的历史回放不保证（注入器晚于
current_history 下发）——需要回放的卡片请插件自行在 render 时补拉状态。

## 管理

会话内：

| 命令 | 作用 |
|---|---|
| `/plugins` | 清单（版本/来源 scope/启停/是否已加载/+前端） |
| `/plugins enable/disable <名>` | 启停（写 `.agent/plugins_state.json`；reload 后生效） |
| `/plugins reload` | 全量重载（全卸→重发现→重装，注册表重建） |
| `/reload plugins` | 同上（reload_hot(scope="plugins") 亦可，钩子工作流可调） |

命令行（免起实例）：

```
agt plugin list
agt plugin enable|disable <名>
agt plugin install <本地目录 | git-url>   # → ~/.agt/plugins/<名>/
agt plugin remove <名>                    # 仅全局级；repo 级随仓库 git 管理
```

## 安全模型（重要）

**本机自装代码全信任**（同 tools/builtin、.agent/workflows、Obsidian 插件）：
backend.py 在引擎进程内执行、frontend.js 在你的浏览器执行，**无沙箱、无权限声明**。
不要安装不受信来源的插件——`agt plugin install` 一个恶意仓库 = 交出本机。
远期若引入社区分发（PyPI/git 市场），会补权限声明与审批门禁（设计文档 P3）。
