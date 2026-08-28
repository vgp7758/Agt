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
| `remote_tools.py` | src/ | `REMOTE_SERVERS` 注册表 + settings.json `remote_servers` 持久化（启动自动重连/失败标 offline）+ `route_remote_call`（HTTP 执行，结果前缀 `[remote:id]`，180s 超时）+ `_auto_server_id`（url → id 推导）+ `_ws_send_collect`（WS 消息客户端） |
| `Agent._exec_tool` | agent.py | 工具执行统一入口（逐 call/并行两条路径）：arguments 带 server_id → pop → 路由；未带 → 本地执行。⚠️ **`_REMOTE_ADMIN` 管理工具族豁免路由**（见下） |
| `{func:load_remote_instances()}` | agent_config.py | SYSTEM 注入：已连接实例清单 + 使用规则；**无连接渲染为空串不注入**（零噪声） |
| 五件套工具 | remote_tools.py | `remote_connect(url, server_id?)`（探测+注册+落盘）/ `remote_disconnect` / `remote_list` / **`remote_message(server_id, message)`**（异步 fire-and-forget）/ **`remote_ask(server_id, question, timeout=120)`**（同步问答） |

**`_REMOTE_ADMIN` 豁免路由（2026-08，commit dfe9f89）**：`remote_connect/disconnect/list/message/ask` 的 server_id 参数是**管理语义**（想用什么 id 连接 / 发给谁），不是路由语义——实际事故：`remote_connect(server_id="cnb-agt", url=...)` 被路由拦截吃掉 → 连接注册从未本地执行 → `[未知 server_id]` 死循环（comfy session 三连败后模型放弃框架通道自己手写了 urllib 轮子）。修复：管理工具族 `name.startswith(_REMOTE_ADMIN)` 判定豁免路由。

## 使用

```
对话里："连接一下 192.168.1.2 的实例"
Agent 调：remote_connect("http://192.168.1.2:8000")
  → ✅ 已连接 'agt-192-168-1-2-8000'（135 工具 · session=xxx）
  → SYSTEM 出现【远程 agt 实例】清单（含 server_id 使用规则）

之后任意工具调用带 server_id 即路由：
read_file({"path": "assets/scene.unity", "server_id": "agt-192-168-1-2-8000"})
run_python({"code": "...", "server_id": "agt-192-168-1-2-8000"})   ← 远程 CPU/GPU
```

连接落盘 settings.json（重启自动重连；远程关机标 offline，恢复后探测通过自动转 online）。

**auto server_id（2026-08，commit 7d7d1ab）**：`server_id` 可省略，从 url 自动生成——本地 url（127.0.0.1/localhost/::1）→ `agt-{port}`（**隧道场景端口是唯一区分维度**——如 SSH 隧道 `127.0.0.1:8300 → 远端容器:8000`，一个本地端口对一远端）；远程主机 → `agt-{host}-{port}`（清洗非法字符）；冲突递增 `-2/-3`。**幂等**：同 url 已在表 → 复用现有 id（offline 恢复/重复连接不再报错）；显式改名（`server_id="comfy"` 连已注册的 url）→ 移除同 url 旧 id 条目——url 与 id **一对一**，防双 id 并存混乱。工具 schema 里 server_id 不再必填，docstring 写明「可省略——自动生成」。

**三层组网通道（2026-08 定稿）**：工具级（任意调用带 server_id，远程零 LLM 成本）/ 消息级异步（`remote_message`，通报派活）/ 消息级同步（`remote_ask`，问它才知道的事）——详见 [跨实例消息通信](#跨实例消息通信remote_message--remote_ask2026-08)。

## 跨实例消息通信（remote_message / remote_ask，2026-08，commit 398a60a）

**用户提案**：跨实例工具调用直接调工具即可（工具级直执行），而跨实例通信（发消息让对方带上下文干活）还需要 run_python 手写 WS 客户端——为什么不做一个工具？直接传 server_id 和要发送的消息，异步继续。

**两件套（与工具级直执行互补的「消息级」通道）**：

| 工具 | 语义 | 成本 |
|---|---|---|
| `remote_message(server_id, message)` | **异步 fire-and-forget**——WS 送达即返（`user`/`message_queued` 回执），对方带自己的 session 上下文异步处理 | 对方异步跑一轮 |
| `remote_ask(server_id, question, timeout=120)` | **同步问答**——挂流收 answer 到 `_done`，聚合最终回答返回 | 对方一轮 LLM |

**实现**（remote_tools.py `_ws_send_collect`）：http→ws 端点转换、送达回执（对方正忙时 `message_queued` 进它的插话队列也算送达）、answer 聚合、超时降级文案（「对方在忙长任务——加大 timeout 或改用 remote_message」）。两工具加入 `_REMOTE_ADMIN` 豁免（server_id=发给谁，管理语义，防路由拦截——同 remote_connect 的坑）。

**实测闭环**：`remote_connect("http://127.0.0.1:8000")` → auto id `agt-8000`（139 工具）→ `remote_ask("agt-8000", "你当前 session 的名字？")` → `[remote:agt-8000] 我当前 session 的名字是「在CNB上调用ComfyUI」`。替代了此前两次手写 WS 客户端场景（问环境那次、发修复通报那次——后者还得事后翻对方 events.jsonl 才拿到回答）。

**三层组网通道**：**工具级**（任意调用带 server_id，零远程 LLM，远程只是「手」）/ **消息级异步**（remote_message，通报派活）/ **消息级同步**（remote_ask，问它才知道的事）。`/restart` 后工具箱即有五件套。

## 已验证（E2E 8001 mock 实例）

单进程起 mock 实例（8001，避开在忙的 8000）完整链路：connect 探测注册 / 远程 read（`[remote:t8001]` 前缀 + 内容 + file_version）/ **server_id pop 副作用**（不进远程参数）/ 远程 edit 改文件 / 复核新 version / 未知工具模型可读错误 / 本地无 server_id 直通 / SYSTEM 注入 / disconnect 清理——全过。commit `6b5ca52`（spec 五步全绿）。

**两个调试插曲（复用价值）**：

- **main.yml 装配方插入坏块**：run_python 脚本往两份 main.yml（`~/.agt/main.yml` L37 与 `src/assets/main.yml` L22，runtime_env 段后）插 `{func:load_remote_instances()}` 行，第一次按"下一个同级 `- `"找块边界产生嵌套缩进坏行 → yaml 解析报错；第二次先删坏行重插才干净。教训：**脚本改 yml，收工前必须 `yaml.safe_load` 验证通过**。
- **mock `/api/status` 连续 500 两次**：手写 mock dict 相继缺 `_current`、缺 `_lock` 而炸——/api/status 的字段面比想象宽（依赖 AgentRegistry 内部状态），最终 mock 直接**继承真 `AgentRegistry`** 才过。对照：**`/api/tool/exec` 只依赖 `agent.tools`，依赖面比 status 轻得多**（connect 探测走 status，工具执行不走）。

## 边界与后续

- 远程工具箱以对方 `/api/status` 报告的 tools_count 为信息展示（具体工具 schema 未拉取投影——模型按通用工具语义调用，未知工具错误文案兜底）
- 消息级驱动（remote_message / remote_ask）**已实现**（2026-08，见 [跨实例消息通信](#跨实例消息通信remote_message--remote_ask2026-08)）——与工具级直执行互补：工具级=远程纯「手」（零 LLM），消息级=让对方带自己上下文干活（对方一轮 LLM）
- 公网使用需隧道 + 鉴权（本期与全服务同信任模型）
- **新环境探索的引导**：README「Agent 上手指引」节（2026-08 新增）——`/status` 环境盘点 → AGENTS.md → workspace 结构 → .agent/ 三件套 → models.json → remote_connect 组网 → 框架参考；新实例首轮对话前读它就知道该探索什么（CNB 云容器部署场景的实测教训，见 [v0.22.0 发布记录](../releases/v0.22.0.md)）

