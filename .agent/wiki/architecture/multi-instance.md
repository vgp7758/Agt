# 多 agt 实例组网（server_id 工具路由）

> v0.20.x 引入。一台机器上的 agt 实例可以把另一台机器上的 agt 实例当"远程工具箱"用——
> 模型在任意工具调用的 arguments 里带 `server_id` 字段，调用即路由到对应实例执行。

## 一图流

```
本地实例（9000，自我迭代）                     远程实例（192.168.1.2:8000，ComfyUI）
─────────────────────────                    ─────────────────────────
模型输出：                                      POST /api/tool/exec
edit({"server_id":"comfy",            ──────▶  {name:"edit", arguments:{path,...}}
       "path":"src/x.py", ...})                 ↓ 查工具箱 → 同步执行
  ↓ Agent._exec_tool 拦截                        （file_version / py_auto_diag
  ↓ pop("server_id") → route_remote_call          在远程侧自然生效）
  ← [remote:comfy] ✅ 已替换 1 处        ◀────── {ok:true, result:"✅..."}
  （作为 tool result 进本地上下文）
```

## 设计决策

| 决策 | 理由 |
|---|---|
| **server_id 放 arguments 而非 tool_calls 顶层** | 顶层结构由 provider 的结构化输出头解析，自定义字段被静默丢弃；arguments 是模型自由生成的 JSON，任何字段保真传输 |
| **工具级直执行**（新 REST 端点）而非消息驱动 | 远程不跑 LLM、不进对方 session——纯"手"（要对方带上下文干活用 WS 消息驱动，二者互补） |
| **server_id 是路由元数据** | pop 后不进远程参数、不进 toollog 存档（记录纯工具参数） |
| **file_version 跨实例语义** | file_version 由远程进程内部 read→edit 自动配对跟踪——对同一远程文件持续带同一 server_id，乐观锁天然成立（写进 SYSTEM 注入文案） |
| **信任模型** | 与 /api/status、WS 一致（局域网）；/api/tool/exec 无增量风险——WS 消息本就能驱动任意行为 |

## 组件清单

| 组件 | 位置 | 职责 |
|---|---|---|
| `/api/tool/exec` 端点 | server.py | `{name, arguments}` → 工具箱执行 → `{ok, result}`；异步壳 + run_in_threadpool（长工具不占事件循环）；不进 agent.run/不碰 session |
| `remote_tools.py` | src/ | `REMOTE_SERVERS` 注册表 + settings.json `remote_servers` 持久化（启动自动重连/失败标 offline）+ `route_remote_call`（HTTP 执行，结果前缀 `[remote:id]`，180s 超时） |
| `Agent._exec_tool` | agent.py | 工具执行统一入口（逐 call/并行两条路径）：arguments 带 server_id → pop → 路由；未带 → 本地执行（现状零变化） |
| `{func:load_remote_instances()}` | agent_config.py | SYSTEM 注入：已连接实例清单 + 使用规则；**无连接渲染为空串不注入**（零噪声） |
| 三件套工具 | remote_tools.py | `remote_connect(server_id, url)`（探测+注册+落盘）/ `remote_disconnect` / `remote_list` |

## 使用

```
对话里："连接一下 192.168.1.2 的实例"
Agent 调：remote_connect("comfy", "http://192.168.1.2:8000")
  → ✅ 已连接 'comfy'（135 工具 · session=xxx）
  → SYSTEM 出现【远程 agt 实例】清单（含 server_id 使用规则）

之后任意工具调用带 server_id 即路由：
read_file({"path": "assets/scene.unity", "server_id": "comfy"})
run_python({"code": "...", "server_id": "comfy"})   ← 远程 CPU/GPU
```

连接落盘 settings.json（重启自动重连；远程关机标 offline，恢复后探测通过自动转 online）。

## 已验证（E2E 8001 mock 实例）

connect 探测注册 / 远程 read（前缀+内容+file_version）/ server_id pop 副作用 /
远程 edit 改文件 / 复核新 version / 未知工具模型可读错误 / 本地无 server_id 直通 /
SYSTEM 注入 / disconnect 清理——全链路过。

## 边界与后续

- 远程工具箱以对方 `/api/status` 报告的 tools_count 为信息展示（具体工具 schema 未拉取投影——模型按通用工具语义调用，未知工具错误文案兜底）
- 消息级驱动（remote_agt：发消息让对方带上下文干活）是互补的另一半，未实现
- 公网使用需隧道 + 鉴权（本期与全服务同信任模型）
