# serve_endpoint · 自定义 HTTP 端点三件套——Agent 给运行中服务动态挂 /custom/* 路由（src/endpoint_tools.py，2026-11-16，用户提案，commit 0fd4d2b）

> 一句话：`serve_endpoint(method, path, code, auth, desc)` 让 Agent 给**自己正在运行的 Web 服务**动态挂一条 HTTP 端点（与框架端点同进程同端口，无需重启即时生效），处理代码在请求到达时纯代码执行——**零 token**；需要 Agent 参与时代码里调 `push()` 一行注入 inbox 唤醒一轮。

## 职责与场景

**动机（用户 2026-11-16）**：「有没有办法让 agent 能够给自己正在工作的服务附加新的端点，编写代码消费这种端点上的请求？」

典型场景：GitHub/支付 webhook、外部系统回调、定时器触发、给第三方脚本提供的查询接口——共同特征是**处理逻辑可纯代码完成**（校验、转换、记账），只有必要时才唤醒 Agent。

| 决策 | 值 | 理由 |
|---|---|---|
| 路径前缀 | 强制 `/custom/` 开头 | 与 `/api/*` 框架端点物理隔离，永不冲突 |
| 挂载机制 | FastAPI 运行时路由（`app.add_api_route`） | 与[插件路由](plugin-sdk.md)同通道（plugins.py 先例），免重启 |
| 持久化 | `<workspace>/.agent/endpoints.json` | 服务/实例重启自动重挂（`restore_all`），零丢失 |
| 鉴权 | 默认 `token`（`X-Cb-Token`/`?token=` == settings.json 的 `callback_token`）；`auth="none"` 显式豁免 | 与 [/api/callback](external-injection.md) 同一凭据体系；公开 webhook 自行验签 |

## 三件套（`make_endpoint_tools(agent)`，src/chat.py 注册「自定义端点」）

| 工具 | 签名 | 语义 |
|---|---|---|
| `serve_endpoint` | `(method="POST", path, code, auth="token", desc="")` | 挂端点。method ∈ GET/POST/PUT/DELETE/PATCH；path 必须 `/custom/` 开头；**重复注册同 method+path = 更新**（旧 handler 真卸载） |
| `remove_endpoint` | `(method, path)` | 从运行中服务卸载 + 清持久化记录 |
| `list_endpoints` | `()` | 清单：方法/路径/auth/desc/挂载状态（✅已挂载/⏸服务未启动/❌未挂载）+ 命中计数（内存态，重启清零） |

**注册即验证**：serve_endpoint 当场 `exec(code)`——语法错误/缺 `main` 立刻报错返回，不存脏数据。CLI（无 Web 服务）场景只存盘，服务启动后自动挂载生效。

## code 契约

```python
def main(req):            # sync 或 async def 均可；>60s 超时 → 504
    ev = req["json"] or {}
    push(f"[gh] PR #{ev.get('pull_request',{}).get('number')} {ev.get('action')}", "github")
    return {"ok": True}   # dict/list → 200 JSON；返回 (status, body) → 定制状态码；其它 → {"text": str}
```

| 项 | 语义 |
|---|---|
| `req` | `{"method","path","query","headers"(键小写),"json"(body 解析失败为 None),"text"(原始文本截 64KB),"client"}` |
| `push(text, source="")` | code 内置（绑本 Agent）：`push_message(wake=True)`——事件进 inbox 持久化 + 唤醒一轮（与 [/api/callback](external-injection.md) 同语义） |
| 安全边界 | body >10MB → 413；执行异常 → 500 带 `traceback[-1500:]`；async 超 60s → 504 |

**纯代码消费 = 零 token**：不调 `push` 的端点对每个请求只花 CPU，这是与 `/api/callback`（必然唤醒一轮）的关键分工——高频/可自动处理的流量留在代码层，只有需要决策的事件才进 Agent。

## 挂载与重放机制

- **挂载**：`app.add_api_route(path, handler, methods=[method], name="custom_<m>_<path>")`；更新注册时**先摘旧再挂新**——FastAPI 同 path 两条路由时旧的优先匹配，不摘旧则更新不生效
- **摘除**：`app.router.routes[:]` 按 `name` 前缀 `custom_` 过滤（不碰框架路由）
- **重放**：`server.start_server` 尾部调 `restore_all(app)`——读 endpoints.json 逐条 exec + 注册 + 挂载，失败单条跳过不炸启动；启动日志打印 `[endpoints] 已重挂 N 条自定义端点`

## 施工中抓到的两个坑（L3 实测 2026-11-16，均已修）

1. **handler 注解 422**：`future annotations` 下 handler 的类型注解按**模块 globals** resolve——`Request` 局部 import 时 resolve 失败，FastAPI 把 request 当必填 query 参数 → **一切请求 422**。修复：模块级 `from fastapi import Request as _FastAPIRequest` + 字符串注解。**plugins.py L408 同款坑**（2026-10-15 实锤），见 [插件 SDK · 路由 422 根治](plugin-sdk.md#插件路由-422-根治future-annotations--fastapi-注解-resolve2026-101516隔离实例实锤)
2. **重放后 push() 失绑**：`restore_all` 不传 agent 时重放出的 `push()` 是 None → 端点代码调用 NameError → 500。修复：`restore_all` 缺省从 `server._agent`（start_server 注入态）取

## 验证（L3 TestClient，十九场景全过）

挂载即时生效 / GET·POST·PUT·DELETE·PATCH / token 401·未配置 503 / auth=none 豁免 / json·text·query·headers 传递 / dict·(status,body)·str 返回三态 / async + 60s 超时 / 异常 500 traceback / 10MB→413 / 重复注册=更新（旧 handler 失效）/ remove 卸载+清盘 / 重放恢复 + push 绑定 / 路径与 method 校验拒绝 / 缺 main 拒绝。

## 注意事项

- `auth="none"` = 完全公开——公开 webhook 必须在 code 里自行验签（如 GitHub 的 HMAC signature 校验）
- token 未配置（settings.json 无 `callback_token`）时 token 模式端点**拒绝一切请求**（503）——与 `/api/callback` 的安全策略一致
- 命中计数是内存态，重启清零；持久化的只有端点清单本身
- 端点 code 在服务进程内执行——别放阻塞长任务（60s 上限）与死循环；重活转 `push()` 交给 Agent/后台

## 相关页面

- [外部事件注入](external-injection.md) —— `/api/callback` 固定回调通道（本工具是其「Agent 自定义端点」扩展）；token 凭据体系同源
- [插件 SDK](plugin-sdk.md) —— 挂载同通道（FastAPI 运行时路由）与注解 422 同款坑的原始记录
- [/api/status](api-status.md) —— 实例地址确认（端点 URL = `http://<实例地址>/custom/...`）
- [ask-user](ask-user.md) 之外的 Agent 交互入口对照：`push()` 走 [用户交互](user-interaction.md) 的 inbox 唤醒语义
