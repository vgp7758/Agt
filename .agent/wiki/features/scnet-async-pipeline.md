# SCNet 异步生产流水线 · 画布转 API + 容器主动回调（2026-09-14）

> 把 [SCNet 算力网](../guides/scnet.md) 的「本地批量客户端」从纸面推到**真出片 + 异步收货**：容器侧 monitor 主动回调本机 agt，关电脑也能等产物。
> 本轮 commit `18d7e7f`（server.py 回调端点 + 3 个工具脚本），生效需 `/restart`。

## 职责与全链路

```
本地 agt（:9000）                          SCNet 容器（K100_AI 68.7GB）
  │                                          ┌─ ComfyUI :8190（跑批）
  │  enqueue 工作流（HTTP /prompt） ────────▶│
  │                                          ├─ monitor.py :8191（反代 + 监控二合一，常驻）
  │                                          │   ├ /monitor        → 状态 JSON
  │                                          │   ├ /monitor/add    → 加任务（纯 HTTP）
  │                                          │   ├ 其余路径        → 反代 127.0.0.1:8190
  │                                          │   ├ 自检回调：每 60s 重试（等本机 restart）
  │                                          │   └ 轮询 /history → 完成后回调
  │  ◀── POST /api/callback（cpolar 隧道）────┘
  │       header 鉴权（X-Cb-Token）→ message 入 inbox 唤醒 Agent
  │       file → 落盘 scnet_inbox/
```

关键点：**容器是主动方**。本机不需要常驻轮询线程，也不需要公网 IP 之外的任何东西——cpolar 隧道已验证通路，容器侧只要拿到回调 URL 就能推。

**⚠️ 平台级约束（本轮实测踩到）**：SCNet 同一实例**只有一个公网代理端口**（`…:58043`），后启动的自定义服务会**顶掉先前服务的入口**。启 monitor(8191) 后 ComfyUI 的公网入口即失效（58043 返回 monitor 状态页，`/history` 查不到）。因此 monitor 必须做成**反代**：对外一个入口，内部按路径分流到 8190。反代为纯 HTTP（ComfyUI API 足够）；前端 WebSocket 不经此代理，进度条降级。

**⚠️ 隧道约束（2026-09-14 实测）**：cpolar 等隧道**丢弃 query string**，回调鉴权必须走 header；且可能返回 HTTP 200 + 业务 `ok=false` 的假成功，消费端须校验响应体。详见下节。

## 本机端点：POST /api/callback（src/server.py）

| 项 | 值 |
|---|---|
| 路径 | `POST /api/callback`（`src/server.py` L1019 起） |
| 鉴权 | `callback_token`（`~/.agt/settings.json`；cpolar 等隧道暴露公网，**未配置 token 则拒绝一切回调**） |
| 参数优先级 | **header（`X-Cb-Token` / `X-Cb-Type` / `X-Cb-Filename`）> query > body** |
| `message`（默认 type） | body JSON `{text, source}` → `agent.push_message(text, source=source)` 注入 inbox → 触发唤醒轮（后台通知语义，见 [user-interaction](user-interaction.md)） |
| `file` | raw body = 文件字节 → 落盘 `WORKSPACE/scnet_inbox/{HHMMSS}_{basename}`，并 push 一条通知消息（`source="callback:file"`） |
| 返回 | `{"ok":true,"saved":…,"size":…}` / `{"ok":true,"queued":true,"inbox_size":…}`；失败 `{"ok":false,"error":…}` |

**⚠️ 隧道丢 query string（2026-09-14 实测，重要）**：cpolar 等隧道转发时会**丢弃 query string**，导致 `?token=…` 形式的鉴权经隧道后必然失败。对照实验（`run_python` 直推 300 字节）：

| 路径 | 结果 |
|---|---|
| 直连 `127.0.0.1:9000/api/callback?token=…` | ✅ 200，落盘成功（`scnet_inbox/210956_selftest.bin` 300 bytes） |
| 经 cpolar 同一 URL | ❌ `{"ok":false,"error":"token 校验失败"}`（query 被隧道吃掉） |

**修法**：token/type/filename 改走**自定义 header**（`X-Cb-Token` 等），服务端 header 优先、query 兜底（`src/server.py` 四处整段替换，L1029-1053）。

**假成功陷阱**：隧道/网关可能返回 **HTTP 200 但业务 `ok=false`**。消费端必须校验响应体 `ok==true` 才算成功——monitor 此前只看 HTTP 状态码，导致「pushed=2 但文件没落盘」被计成功（见下节 monitor v3）。

**修复后复验（2026-09-14，`/restart` 后，header 通道）**：

| 路径 | 结果 |
|---|---|
| 直连 + header | ✅ 200 `{"ok":true,"saved":"…211448_hdr_ok.bin","size":680}` |
| **cpolar + header** | ✅ 200 `{"ok":true,"saved":"…211450_hdr_ok.bin","size":680}` ← **隧道丢 query 的坑彻底绕过** |

`scnet_inbox/` 现存 3 个实验文件（`210956_selftest.bin` / `211448_hdr_ok.bin` / `211450_hdr_ok.bin`）。

- 本地模拟回调实测：**HTTP 404**——精确命中预期（当前进程还是旧代码，路由不存在）；`/restart` 后即 200。
- 未鉴权/错 token 行为未做额外容错：宁可 404/401 也不静默吞。

## MCP 封装：scnet_notebook（纯 HTTP 直调控制台后端）

- **凭据链**：AK/SK → 区域 token → 直接作 `token` header 调 `cancon.hpccube.com:65011/acx/containermgt/*`（与控制台共用同一 JWT，见 [SCNet 算力网 · 纯 API 通道](../guides/scnet.md)）。
- **实测样例**：`scnet_notebook(action="url", cluster="昆山", payload={"id": "2099459694942883841"})` → `{"status":"active","url":"https://n-2099459694942883841.ksai.scnet.cn:58043/jupyter-forward/.../lab/tree/root/?token=sothisai_..."}`——**纯 HTTP 拿到 Jupyter URL，零浏览器**。
- **写操作**（创建/停止/服务配置）端点已从前端 JS 定位（`/aimgt/notebook`、`/containermgt/notebook/task/actions/*`），payload 待补，docstring 已标注。
- **⚠️ 框架缺口（2026-09-14 实测确认）**：`reload_mcp_server` 只重连 session（`reconnect_from_config_one` 只更新 `mcp_mgr.sessions[name]`），**不重新注册工具到 `agent.tools`** ——新增工具（如 `scnet_notebook`）调用时报「工具箱里没有」。**必须 `/restart`** 才能让新工具进工具箱。修法方向：重连后调 `mcp_mgr.sync_to_toolbox(agent.tools)`（该 API 已存在，目前只有 [ensure_lsp](../architecture/tool-externalization-criteria.md) 在用，见 [MCP 配置页](mcp-config.md)）。

## 容器侧 monitor.py（:8191）· v3 常驻 + 反代 + HTTP 加任务

**v3 版（2026-09-14，`tools/scnet_monitor.py`，304 行）**——在 v2 反代基础上修掉「假成功」，并完成**常驻化 + 任务 HTTP 化**改造。**已实际部署并跑通**（见下「首次无人值守闭环」）。

### 形态：常驻 + 任务持久化 + HTTP 管理

| 项 | v1/v2（旧） | **v3（当前）** |
|---|---|---|
| 进程寿命 | 监控 N 单跑完即退 | **常驻**（`while True`，`HTTPServer.serve_forever()`） |
| 任务来源 | `--ids` 启动参数（一次性） | `--ids` 初始 + **`POST /monitor/add {"ids":[...]}` 随时追加**（也支持 `?ids=a,b` query 兜底） |
| 任务存储 | 内存 | `/root/monitor_tasks.json`（`load_tasks`/`save_tasks` 持久化，重启不丢） |
| 状态查询 | 只读 | `/monitor` 状态 JSON；`/monitor/remove` 删任务 |

**核心收益**：**加任务不再需要在容器里执行命令**——本机经容器公网 URL 直接 `POST /monitor/add` 即可（例如 `https://c-{id}.ksai.scnet.cn:58043/monitor/add`）。于是「在容器里执行命令」这一步从「每批一次」降为「**只做一次**」（首次拉起进程）。

### 路由与状态机

- **路由**（`Handler._handle`）：`/monitor*` → 状态/管理 JSON（`_LOCK` 保护 `STATE`）；**其余全部反代** `COMFY = http://127.0.0.1:8190`。
  - `/monitor` → `STATE` 全量（`boot` / `tasks` / `callback` / `pushed` / `recent`）
  - `/monitor/add` → `{"ok":true,"added":[...],"total":N}`
  - `/monitor/remove` → `{"ok":true,"total":N}`
- **任务状态机**：`pending` → `queued`（`/history` 里查不到）→ `running` → `done` / `push-failed` / `error`。
  - 修复（本轮）：`STATE["tasks"][pid] = st if completed else "running"` —— 旧写法 `f"{st}/running"` 会**污染状态字符串**，导致 pending 过滤与 `left` 判定失准。
- **反代实现**：`urllib.request` 转发 method + body + 请求头（剔除 `host/content-length/connection/accept-encoding`），回写响应头（剔除 `transfer-encoding/connection/content-length/content-encoding`）；`HTTPError` 原样透传状态码与 body，其他异常 → 502。`do_GET/POST/PUT/DELETE/HEAD` 全部绑到 `_handle`，`log_message` 静默。
- **回调**：`_notify()` / `_push_file()` 用 `X-Cb-Token` / `X-Cb-Type` / `X-Cb-Filename` 携带鉴权与类型（**不再依赖 query string**——cpolar 等隧道会丢 query）；并**校验响应体** `ok==true`，服务端 HTTP 200 但业务 `ok=false` 时抛 `RuntimeError("回调被拒: …")`，假成功不再计数。
- **自检回调**：启动后每 60s 重试一次回调（等本机 `/restart` 激活端点），成功后转正常轮询（`STATE["callback"]` 记录 `ok` / `wait(n): …`）。
- **监控循环**：20s 一轮轮询 `/history`；完成 → `_collect_files`（images/gifs/videos）→ `/view` 取字节 → `_push_file`；`error` 任务单独回调告警；**全部处理完（无 pending/queued/running）** 时额外推一条「🏁 全部任务已处理，请决定是否关机」。
- **日志**：`/root/monitor_pushed.log`（`_log` 追加推送记录与 notify 失败原因）。
- **CLI**：`--ids`（**已非必填**，可空）`--cb`（必填）`--token`（必填）`--port 8191`（`--interval` / `--no-push-file` 已移除）。
- **部署通道**：容器内用 **JupyterLab 开终端**（该镜像未装 SSH）；首次也可经 Jupyter Contents API `PUT /api/contents/root/monitor.py` 上传脚本，再在「访问自定义服务」里配启动指令。

**v3 启动命令（容器内 JupyterLab 终端，2026-09-14）**：

```bash
pkill -f root/monitor.py
nohup python3 /root/monitor.py \
  --cb http://75a28242.r21.cpolar.top/port-9000/api/callback \
  --token 2bc435c58e08fdb3013ff84569a78659 --port 8191 &
```

> 注：`--ids` 可省——起来后本机 `POST /monitor/add` 加任务即可。

### 无人值守闭环持续运行：5 单批量 4/5 已自动回传（2026-09-14 21:30 起）

**第一个视频全程零人工自动回家**：`scnet_inbox/213053_MiniMax_H3_00004_.mp4`（0.84 MB），boot 21:30:46。

| 环节 | 状态 |
|---|---|
| monitor v3 常驻进程 | ✅ 已接管（`boot 21:30:46`，容器内 `pkill -f root/monitor.py` 后重启为 v3） |
| **callback 自检（header 通道，即生产链路）** | ✅ `"ok"` —— **一次就通**（本机已 `/restart` 装载端点） |
| 任务表 | ✅ 5 单全在表 |
| 自动推送 | ✅ `pushed: 4`，`recent` 含 00004~00007 四单 |
| 剩余 1 单 | 🔄 `fe94db7b` queued（ComfyUI `running=1 pending=0`），约 7.4 分钟/单，预计 21:59 前后收齐 |

**四单实测回传（第四次通知轮 21:52 复验，`monitor: 4/5 done | pushed=4 | callback=ok`）**：

| 单 | prompt_id | 状态 | 产物（`scnet_inbox/`） |
|---|---|---|---|
| #1 | ca9055d5-2890-4c6e-a258-b98c96af4d99 | done | `213053_MiniMax_H3_00004_.mp4`（0.84 MB） |
| #2 | dac0b572-42f1-4b14-8e99-91933ab962d8 | done | `213701_MiniMax_H3_00005_.mp4`（0.77 MB） |
| #3 | feb418b4-569f-4037-822b-f7a97d245f82 | done | `214429_MiniMax_H3_00006_.mp4`（0.8 MB） |
| #4 | e3b00747-a52d-438f-9f3d-d3fa7016f2bb | done | `215157_MiniMax_H3_00007_.mp4`（0.8 MB） |
| #5 | fe94db7b-0847-4d26-afd2-7d342a9bf1be | queued | — |

- 每单节奏 ≈ 7 分钟，与热态基线（~7.4 分/单）一致；`callback: "ok"`、`pushed` 计数与实际落盘文件一一对应（v3 的响应体校验生效——不存在「200 假成功」虚增）。
- 同一产物另有 `scnet_outputs/` 副本（本机兜底轮询 `scnet_watch_batch` 下载），两条收货通道并行无冲突。
- **状态查询口径**：`GET /monitor` → `tasks` 表逐 pid 状态 + `pushed` 计数 + `callback`；`GET /queue` → ComfyUI `queue_running` / `queue_pending`（判断「是否还有单在跑」最直接）。

**意义**：此前所有环节都是「分头验证过」，本轮是**第一次端到端串起来自己跑**——enqueue → 容器 monitor 轮询发现完成 → 抓产物 → 经 cpolar 推回本机 → 落盘 `scnet_inbox/` → 唤醒 Agent 一轮。**关电脑等收货**从设计变成事实，且连续四单零人工、零失败。

**部署动作（本轮实际执行）**：
1. 本地 `tools/scnet_monitor.py` → 复制为 `~/.agt/mcp/scnet/monitor_template.py`（11917 bytes，供 MCP 工具 `scnet_monitor` 的 deploy action 读取）
2. `scnet_mcp.py` 加 `scnet_monitor` 工具（`monitor_action`，py_compile OK，备份 `scnet_mcp.py.bak_20260914_212929`）
3. 容器内 `pkill` 旧 monitor → `nohup` 拉起 v3

**收工三选一（跑完 5 单后的动作，待用户裁定）**：① 自动关机省余额（届时约 ¥7.5）；② 保留实例续跑更多单；③ 整理 `scnet_inbox/` 产物清单（附提示词/seed 记录）便于挑素材。

### monitor 的三步生命周期（谁在哪做）

```
本地 tools/scnet_monitor.py（仓库文件，随 commit 走）
   │  ① Jupyter Contents API：PUT /api/contents/root/monitor.py   ← 纯 API，可脚本化
   │     （或 MCP scnet_monitor deploy：读 ~/.agt/mcp/scnet/monitor_template.py 上传）
   ▼
容器 /root/monitor.py（副本）
   │  ② 拉起进程：「访问自定义服务」填端口 8191 + 启动指令（首次）
   │              ／ JupyterLab 终端 nohup（后续重启）
   │              ／ **Jupyter terminals WebSocket API（纯 API，已打通）** ← 见下节
   ▼
容器内常驻进程 → 轮询 ComfyUI → 推产物 + 回调本机
   │  ③ 加任务：POST https://c-{id}...:58043/monitor/add           ← 纯 HTTP，随时可做
   │     查状态：GET  https://c-{id}...:58043/monitor
```

**结论（本轮更新）**：①③ 已可纯 API/工具化（MCP `scnet_monitor` 的 deploy/add/status）；② 曾被认为是唯一卡点，**本轮用 Jupyter terminals WS 打通**——三步现已全部可脚本化，只差封装成工具。

### Jupyter terminals WebSocket API：容器内执行任意命令（2026-09-14 打通）

自动化链路的最后一块拼图——**纯 API 在容器内执行命令**（比 browser 里的 JupyterLab 终端更可靠、可脚本化）：

```
wss://<host>/jupyter-forward/{实例ID}/terminals/websocket/{name}?token=<jupyter token>
   → 发 ["stdin", "命令\r"]   # 实测 echo / pkill / nohup 全部可用
```

- 连接参数：`host` = `n-{id}.ksai.scnet.cn:58043`，`token` = `sothisai_{id}`（Jupyter URL 的 query 里就有）。
- `sslopt={"cert_reqs": ssl.CERT_NONE}`（自签证书），`websocket-client` 库。
- 实测：`echo WS_OK_PROBE` 回显正常；`pkill -f root/monitor.py` 生效。
- **意义**：monitor 的**首次拉起也能全自动**了——此前只能靠人点 JupyterLab 终端或「访问自定义服务」表单。

### MCP 工具 scnet_monitor（2026-09-14 落地）

`~/.agt/mcp/scnet/scnet_mcp.py` 新增 `scnet_monitor` 工具（`monitor_action` 参数）：

| action | 作用 |
|---|---|
| `deploy` | 读 `~/.agt/mcp/scnet/monitor_template.py` → Jupyter Contents API 上传为容器 `/root/monitor.py` |
| `add` | `POST /monitor/add {"ids":[...]}` 加任务（纯 HTTP） |
| `status` | `GET /monitor` 读状态 JSON |
| `start-command` | 拿/配自定义服务启动指令 |

模板文件同步：本地 `tools/scnet_monitor.py` → `~/.agt/mcp/scnet/monitor_template.py`（**两份需手工同步**，容器内改动不回写）。

### 至此 SCNet 全链路 API 化完成

```
scnet_notebook(list/info/url/config/start-command/ports)   ← 实例查询/URL（已封装）
scnet_monitor(deploy/add/status/start-command)             ← monitor 部署/加任务（已封装）
Jupyter Contents API（上传文件） + terminals WS（执行命令）  ← 通道已打通
ComfyUI API（enqueue/history/view）                        ← 批量生产（已封装）
本机 /api/callback + cpolar（header 鉴权）                  ← 产物回传 + 唤醒（已通）
```

**下一步方向（未实施）**：把 terminals WS 与上传也封装成 MCP 工具（`scnet_exec` / `scnet_upload`），则「一句 enqueue → 自动收片」成为**纯工具调用序列**，零手工。

## 兜底轮询：tools/scnet_watch_batch.py（本机主动拉，不依赖容器 monitor）

与容器侧 monitor **互补的第二条收货通道**：本机进程主动轮询容器 ComfyUI `/history`，完成即下载产物到 `scnet_outputs/`。

| 项 | 值 |
|---|---|
| 文件 | `tools/scnet_watch_batch.py`（68 行，纯标准库） |
| 输入 | `scnet_batch5.json`（`{"pids":[...]}`，enqueue 时落盘） |
| 目标 | `GBASE = https://c-{id}.ksai.scnet.cn:58043`（**必须走反代版 monitor**——否则 58043 是 monitor 状态页，`/history` 查不到） |
| 轮询 | 30s 一轮，最长 60 分钟；`/history/{pid}` → `status.completed` 判定 |
| 下载 | `/view?filename=…&subfolder=…&type=…` → `scnet_outputs/{filename}` |
| 输出 | 每单一行 `[n/N] pid 状态 → 文件(大小MB)`，末尾汇总耗时 |

**与容器 monitor 的分工**：monitor 走「容器推」+ 唤醒 Agent（关电脑也能收货）；`scnet_watch_batch` 走「本机拉」+ 后台任务（`run_python` 后台化），**用于 monitor 尚未重启到 v3、或不想依赖回调链时的兜底**。两条通道可同时开——`/history` 持久，重复拉取只是多下一份文件，不会互相干扰。

**本轮实测**：5 单入队（`scnet_batch5.json`，5 个 prompt_id），预计 ~37 分钟（7.4 分/单，串行）；兜底轮询以后台任务 `bg_1789392321338` 运行。

## 5 单批量入队（2026-09-14 本轮）

`run_python` 直发 5 单（同一 API JSON 模板换 seed 变体），prompt_id：

```
ca9055d5-2890-4c6e-a258-b98c96af4d99   → done（21:30 回传，seed=53225162）
dac0b572-42f1-4b14-8e99-91933ab962d8   → done（21:37 回传，seed=1886098512）
feb418b4-569f-4037-822b-f7a97d245f82   → done（21:44 回传，seed=2068805186）
e3b00747-a52d-438f-9f3d-d3fa7016f2bb   → done（21:52 回传）
fe94db7b-0847-4d26-afd2-7d342a9bf1be   → queued（ComfyUI running=1 / pending=0）
```

入队后状态 `running=1 / pending=4`，首单约 21:32 完成。pids 落盘 `scnet_batch5.json` 供两条收货通道共用；`/monitor` 的 `tasks` 表即以此 5 个 pid 为键（状态机 `queued → running → done`）。

**history 全量对账（8 条，含 2 条外部 error）**：除本机 5 单外，另有 `bc89599a` / `3aa63780` 两条 `error`（seed=None、无产物），疑为 ComfyUI 前端页面自动提交，与本批无关。

## 热态出片速度实测（2026-09-14）

| 任务 | 状态 | execution 时长 |
|---|---|---|
| e4bd2630（第一单·含首次权重加载） | ✅ | **607s**（10分07秒） |
| 5b4caea7（第二单·权重已热） | ✅ | **464s**（7分44秒） |
| 1b01d575（第三单） | ✅ | **444s**（7分24秒） |
| ca9055d5（第四单） | ✅ | **443s** |
| dac0b572（第五单） | ✅ | **444s** |
| feb418b4（第六单） | ✅ | **443s** |

**结论修正**：权重加载只占约 2.4 分钟，**真正瓶颈是推理本身 ~7-8 分钟/单**（480p/5s，4 步 Turbo + EasyCache，K100_AI）。**稳态基线收敛**：热态后连续三单 443/444/443s（≈7.4 分/单，几乎完全一致）——该配置产能可精确预估 **约 8 单/小时**。后续调优杠杆：`low_vram` 开关、EasyCache 参数、分辨率降档。

**异常观察**：history 里另有两个非本机提交的 `error` 任务（`bc89599a` / `3aa63780`，20:19、20:30），疑似 ComfyUI 前端页面自动提交，待查。

**产物**：`scnet_outputs/MiniMax_H3_00002_.mp4`（0.74 MB）等；本批已下载 `00002`~`00006`，另附 `scnet_outputs/manifest.json`（产物清单：prompt_id / seed / 时长）。

## 产物清单：scnet_outputs/manifest.json（2026-09-14）


等待期间顺手做的一份**产物清单**（收货侧旁车，便于复现/挑素材）：`scnet_outputs/manifest.json`，由 `run_python` 拉 `/history` 汇总生成。

字段：`prompt_id` → `seed` / `execution 时长` / `产物文件名` / `status`。

| 产物 | prompt_id | seed | 耗时 |
|---|---|---|---|
| MiniMax_H3_00001_.mp4 | e4bd2630 | 650007857835027 | 607s（含冷启动） |
| MiniMax_H3_00002_.mp4 | 5b4caea7 | 1941978295 | 464s |
| MiniMax_H3_00003_.mp4 | 1b01d575 | 266943330 | 444s |
| MiniMax_H3_00004_.mp4 | ca9055d5 | 53225162 | 443s |
| MiniMax_H3_00005_.mp4 | dac0b572 | 1886098512 | 444s |
| MiniMax_H3_00006_.mp4 | feb418b4 | 2068805186 | 443s |

- **seed 是复现的关键**——同 seed + 同 API JSON 可重出同一条片；清单把 seed 与产物绑定落盘，避免事后从 ComfyUI 前端翻记录。
- 生成方式：`GET /history` 全量 → 解析 `status.messages` 时间戳算 execution 时长 → 汇总落盘。可随时重跑刷新（新单追加即可）。
- 与 `scnet_batch5.json`（入队 pids）互补：前者记「提交了什么」，本清单记「出了什么、什么参数、多快」。

## 画布格式 → API 格式转换器：tools/wf_canvas2api.py

ComfyUI 的「画布 JSON」（编辑器导出，含 nodes/links/widgets_values）与 API 提交用的「prompt JSON」（`{node_id: {class_type, inputs}}`）格式不同。本工具做转换，需 `object_info` 作类型元数据（`scnet_objinfo.json`）。

```bash
python -c "import sys; sys.path.insert(0,'tools'); from wf_canvas2api import convert; \
  convert('scnet_wf_duotu.json','scnet_objinfo.json','scnet_wf_duotu_api.json')"
```

**四个对位坑（全部实测踩过并修掉）**

| # | 坑 | 正确做法 |
|---|---|---|
| 1 | 连接值写成输出名 | API 格式连接值是 `[源节点id(str), 源输出slot索引(int)]`（execution.py L935 `r[val[1]]`） |
| 2 | DYNAMICCOMBO 带点子参数 | `format.codec` 这类带点键要单独对位，不能只按 `format` 匹配 |
| 3 | widget 占位语义 | 画布 `inputs` 里 `link=null` 的端口顺序即 widget 顺序；**连接型类型不占位**、已连接残留占位丢弃 |
| 4 | 旧式 `COMBO` 类型名 | object_info 里类型名可能是旧式 `COMBO`，需归一 |

- 转换时 `SKIP_TYPES = {MarkdownNote, Note}` 跳过展示型节点（本轮 26 节点 → 23 执行节点）。
- 修完后 `POST /prompt` 校验通过、真出片。

## 第一单真实出片（2026-09-14）

| 项 | 值 |
|---|---|
| 产物 | `MiniMax_H3_00001_.mp4`（0.77 MB · 5 秒 · 480p 竖屏） |
| 场景 | 参考图人物对话（多图参考 minimax + easycache + 4 步 lora 工作流） |
| 落盘 | 本地 `scnet_outputs/` |
| 第二批 | 2 单已 enqueue（不同 seed 变体，权重热，每单约 1-2 分钟） |

## 固定打法（批量生产）

1. 本地 `convert` 出 API JSON（或直接用 `tools/scnet_comfy_client.py` 的 `--batch`）
2. enqueue N 单（不同 seed / 提示词 / 镜头），prompt_id 落盘（如 `scnet_batch5.json`）
3. **加任务到常驻 monitor**：`POST https://c-{id}.ksai.scnet.cn:58043/monitor/add {"ids":[...]}`（v3 起纯 HTTP，无需进容器）
4. **兜底**（可选）：本机后台跑 `tools/scnet_watch_batch.py` 主动拉
5. **关电脑等收货**——回调唤醒 Agent，产物自动落 `scnet_inbox/`

> monitor 进程本身仍需**首次在容器内拉起一次**（「访问自定义服务」配端口 8191 + 启动指令），此后常驻；详见「monitor 的三步生命周期」。

## 注意事项

- `/api/callback` 是引擎层改动，**必须 `/restart` 才生效**；在此之前容器侧自检会一直 404 重试。
- **隧道会丢 query string**（2026-09-14 实测）：回调鉴权一律走 header（`X-Cb-Token` 等），不要依赖 `?token=`。
- **HTTP 200 ≠ 成功**：隧道/网关可能返回 200 + 业务 `ok=false`，消费端必须校验响应体 `ok==true`（monitor v3 已修）。
- **单端口约束**：同一实例的自定义服务入口唯一，后启动顶掉先启动——任何新服务上线前先想清楚是否要反代（本轮 monitor 已按此改造）。
- **monitor 是本地仓库文件**（`tools/scnet_monitor.py`），部署 = 上传为容器 `/root/monitor.py` + 拉起进程；①上传/③加任务已由 MCP `scnet_monitor` 覆盖，②拉起可经 **Jupyter terminals WS** 纯 API 完成。容器内改文件**不会**回写本地仓库，两边需手工同步（模板副本 `~/.agt/mcp/scnet/monitor_template.py` 亦然）。
- **v3 常驻进程与本地文件可能不同步**：本地已改 v3，容器内可能仍跑 v1——`/monitor` 返回里没有 `tasks` 键即说明是旧版，需重启（`pkill -f root/monitor.py` 后 nohup 拉起）。
- ComfyUI API 无鉴权，URL 即凭证（cpolar 隧道同理）——勿外泄。
- 实例按 ¥2.53/时计费，余额有限时记得收工关机；monitor 的「全部完成」通知会提示是否关机。
- 画布转换器依赖 `object_info` 快照（`scnet_objinfo.json`）——换镜像/换节点版本后要重新拉。
- 出片速度 ~7-8 分钟/单（热态），稳态基线 **443-444s/单**（≈7.4 分），产能约 **8 单/小时**——排产按此估时。
- 新增 MCP 工具后 `reload_mcp_server` **不会**注册进 `agent.tools`（框架缺口），需 `/restart` 或等修复（见「MCP 封装」节）。

## 相关页面

- [SCNet 算力网](../guides/scnet.md) — 平台通道、镜像、资源与价格、控制台纯 API 地图
- [MCP 配置页](mcp-config.md) — reload_mcp 热重连与「重连不注册工具」缺口
- [user-interaction](user-interaction.md) — 回调 message 注入 inbox 的唤醒语义
- [background-scheduler](background-scheduler.md) — 定时任务与后台服务机制

