# 多 agt 实例组网（remote_instance_id 工具路由）

> v0.20.x 引入。一台机器上的 agt 实例可以把另一台机器上的 agt 实例当"远程工具箱"用——
> 模型在任意工具调用的 arguments 里带 `remote_instance_id` 字段（2026-09-06 前名 `server_id`，旧名仍兼容），调用即路由到对应实例执行。

## 一图流

```
本地实例（9000，自我迭代）                     远程实例（192.168.1.2:8000，ComfyUI）
─────────────────────────                    ─────────────────────────
模型输出：                                      POST /api/tool/exec
edit({"remote_instance_id":"comfy",   ──────▶  {name:"edit", arguments:{path,...}}
       "path":"src/x.py", ...})                 ↓ 查工具箱 → 同步执行
  ↓ Agent._exec_tool 拦截                        （file_version / py_auto_diag
  ↓ pop("remote_instance_id") → route_remote_call  在远程侧自然生效）
  ← [remote:comfy] ✅ 已替换 1 处        ◀────── {ok:true, result:"✅..."}
  （作为 tool result 进本地上下文）
```

## 设计决策

| 决策 | 理由 |
|---|---|
| **路由标记放 arguments 而非 tool_calls 顶层** | 顶层结构由 provider 的结构化输出头解析，自定义字段被静默丢弃；arguments 是模型自由生成的 JSON，任何字段保真传输 |
| **参数名 remote_instance_id（2026-09-06 改名，用户提案）** | 旧名 server_id 极易与工具自身/MCP 同名参数撞名——remote_* 管理族正是实证（`remote_connect(server_id=..., url=...)` 被路由拦截吃掉曾致死循环，见下）；新名与任何已知工具/MCP 参数不重名，且**全工具 schema 自动注入**让模型每次调用都看得见，撞名隐患从机制上根除 |
| **工具级直执行**（新 REST 端点）而非消息驱动 | 远程不跑 LLM、不进对方 session——纯"手"（要对方带上下文干活用 WS 消息驱动，二者互补） |
| **路由标记是元数据** | pop 后不进远程参数、不进 toollog 存档（记录纯工具参数） |
| **file_version 跨实例语义** | file_version 由远程进程内部 read→edit 自动配对跟踪——对同一远程文件持续带同一 remote_instance_id，乐观锁天然成立（写进 schema 注入描述与 SYSTEM 注入文案） |
| **信任模型** | 与 /api/status、WS 一致（局域网）；/api/tool/exec 无增量风险——WS 消息本就能驱动任意行为 |

## 路由参数改名 remote_instance_id + 全工具 schema 自动注入（2026-09-06，用户提案）

**动机**：`server_id` 作路由标记与工具自身同名参数撞名风险早有实证——`_REMOTE_ADMIN` 豁免就是为它打的补丁（管理族 server_id 是管理语义，曾被路由拦截吃掉致 "[未知 server_id]" 死循环）。改名 `remote_instance_id` 后**每个工具的 schema 都自动带这个参数**（含 MCP），模型看得见、不撞名、不再依赖 SYSTEM 提示兜底。

三层交付：

| 层 | 位置 | 内容 |
|---|---|---|
| 路由标记改名 | agent.py `_exec_tool` | arguments 带 `remote_instance_id` → pop → 路由（新主通道）；带旧名 `server_id` → 仍路由（历史投影兼容）；**remote_* 管理族**带旧名 → 透明规范化成新名再本地执行（老习惯不 TypeError） |
| schema 自动注入 | agent.py `_llm_tool_schemas()`（新方法） | 发给 LLM 的 schema 视图里**每个工具（含 MCP）**自动多一个可选参数 `remote_instance_id`（description 带路由用法 + 同文件须恒同 id 的 file_version 提醒）；`remote_*` 族豁免（本身就是跨实例工具，再路由即套娃） |
| 管理族签名同步 | remote_tools.py + agent_config.py | `remote_connect / remote_disconnect / remote_message / remote_ask` 形参全部改名 `remote_instance_id`（值仍传实例 id）；连接成功文案、`remote_list` 提示、错误文案（`[未知实例 id]`）、SYSTEM 注入（`_func_remote_instances`，`{func:load_remote_instances()}`）全部同步 |

**关键设计——只动 LLM 视图**：`_llm_tool_schemas` 对每条 schema **deepcopy** 后注入，**toolbox 原 schema 零污染**——工作流 plugin 节点、WebUI 工具表单、编辑器看到的还是干净参数（路由标记是纯 LLM 交互面的元数据，不扩散到执行面）。三个组装点统一换用：轮初 / 每步刷新 / `/debug prompt`。可选参数不进 required，本地执行照旧省略即可。

**兼容矩阵**：

| 调用形态 | 行为 |
|---|---|
| 普通工具带 `remote_instance_id` | 路由到远程执行（结果前缀 `[remote:id]`），pop 干净后发送 |
| 普通工具带 `server_id`（旧名） | 仍路由（历史投影/旧习惯兼容） |
| `remote_message(server_id="x", ...)`（旧习惯） | 透明规范化成 `remote_instance_id` 再本地执行，不因签名改名报错 |
| `remote_*` 族带任一名 | 均豁免路由（管理语义：连谁/发给谁） |

**边界澄清**：改名只动**路由标记参数名**与 remote_* 工具形参名——`REMOTE_SERVERS` 注册表键、auto id 推导（`_auto_server_id`）、`[remote:id]` 结果前缀里的**实例标识仍是 server_id 概念**，未改。跨版本组网：对端实例跑旧版不影响接收普通工具路由（路由标记发送端已 pop）；仅对端自己调它自己的 remote_message(server_id=...) 旧签名才会 TypeError——各实例 `/update-assets` 或升级对齐即可。

**验证**：四文件 py_compile + 管理族三态（新名/旧名规范化/双名均不路由）+ 普通工具三态（新名路由 pop 干净/旧名路由/本地直通）+ schema 四断言（普通与 MCP 注入、remote_* 豁免、不进 required、原 schema 零污染）全绿。引擎层改动，`/restart` 后生效。

### schema 瘦身 + 运行时缺参提示（2026-09-14·二轮，用户裁定）

**用户反馈**：「每个工具都带这样的参数描述吗？那会不会显得很重复。。我觉得在少传参数的时候给个提示就行」——2026-09-06 版每个工具 description ~200 字，50 工具 × ~350 字符增量无谓膨胀 schema。

**两段式改造**（commit `b56af39`）：

| 层 | 做法 |
|---|---|
| **schema 瘦身**（`_llm_tool_schemas`） | 长描述 → **一句话 26 字**「执行实例：self=本机（默认，可不传）；或已连远程实例 id」+ **enum 数组**（`["self","comfy",…]`——枚举值本身自带提示）；**撤掉 required**（可选，本地执行省略即可）。每工具 schema 增量 ~350 → **<120 字符**；单机（无连接）不注入——零路由噪声 |
| **教育下沉运行时**（`_exec_tool`） | 本地执行（未带 remote_instance_id）且组网非空 → 结果尾附一行提示：「本次在本机执行。已组网实例：cloud、comfy——操作它们那边的文件/命令时在工具参数里带 remote_instance_id 即可路由过去执行」。**防噪三则**：同轮只提示一次（轮指纹 `_rid_hint_fp`）/ 新轮重提一次 / **显式传 `self` 不再提示**（模型已表现出路由意识，不再教育）；单机零提示 |

**执行侧配套**：显式 `self`/`local` → 归一为本地执行（enum 含 'self'）；管理族旧名 `server_id` → 透明规范化成 `remote_instance_id`（历史投影兼容）；`_REMOTE_ADMIN` 五件套豁免不变（其 id 参数是管理语义，2026-08 事故补丁见[组件清单](#组件清单)）。

**边界不变**：只影响 LLM 请求视图（deepcopy 注入）——toolbox 原 schema（工作流 plugin 节点 / WebUI 工具表单 / 编辑器）依旧零感知。

**为什么运行时提示反而更好**：提示出现在**模型刚做完一次本地调用的上下文里**——正是「这活其实该去 comfy 那边干」的认知时机，教育精准投放；schema 常驻成本压到最低。

**验证**（11/11 全绿）：单机不注入+无提示 / 组网后无 required+一句话描述+enum / 增量<120 字符 / 首次缺参提示 / 同轮不重复 / 新轮重提 / 显式 self 不提示 / 管理族豁免。引擎层改动，`/restart` 生效。

#### 第三轮：静态化第一档——_REMOTE_ROUTABLE 白名单恒定注入（2026-09-17，用户设计）

**用户裁定**：「其它工具只对第一档追加 remote_instance_id 参数（MCP 不加了，因为远端实例的 MCP 可能和本地不同，需要调用的话走 remote_call_tool），这样需要添加该参数的工具数量减少了，可以不用根据是否有 remote agent instance 去动态调整工具 schema 了——连接远端实例前后工具 schema 保持不变」。

**三代演进**：

| 代 | schema 注入策略 | 缓存影响 |
|---|---|---|
| 2026-09-06 初版 | 全工具（含 MCP）动态注入——REMOTE_SERVERS 非空才注入 + enum 填实例 id | 连接/断开瞬间 **tools schema 变化 → 全序列缓存断**（tools 尾部变化断全部，见 [DeepSeek 缓存实证](context-engine.md#deepseek-缓存行为实证v3-位置敏感--v4-system-规范化2026-08-两代后端)） |
| 2026-09-14 瘦身 | 一句话 + enum 压到 <120 字符，仍单机不注入 | 连接前后仍跳变 |
| **2026-09-17 静态化（现行）** | **白名单恒定注入**（不看是否组网、无 enum） | 工具 schema 前缀跨连接 **byte-stable**，不断缓存 |

**`_REMOTE_ROUTABLE` 白名单（30 个，src/agent.py 类属性）——「远端调用有实质便利」的第一档**：

| 分组 | 数量 | 工具 |
|---|---|---|
| 文件系（远端仓库/工作区的文件操作） | 12 | read_file / write_file / edit / insert / delete / replace_lines / move / grep / glob_files / list_dir / diff_files / find_function |
| 进程/服务系（远端机器跑命令/服务） | 9 | run_shell / run_python / start_service / stop_service / list_services / service_logs / send_to_service / check_bg_task / run_script |
| 会话/上下文诊断（读远端实例的存档/投影） | 4 | list_tool_logs / get_tool_detail / recall_turn / cache_breakpoint |
| 团队（远端实例的子 Agent 团队） | 2 | list_team / agent_query_events |
| 调度（远端的定时器） | 3 | add_schedule / cancel_schedule / list_schedules |

**不注入（四类）**：

| 类 | 理由 |
|---|---|
| **MCP（`__mcp__` 前缀）** | 远端实例挂的 MCP 与本地可能不同——调远端 MCP 走 `remote_call_tool` |
| 中性工具（纯函数/检索/记忆/wiki 等） | 远端调用无实质便利，注入纯噪声 |
| remote_* 管理族 | 本身就是跨实例工具（且 `_REMOTE_ADMIN` 豁免路由） |
| 本轮状态类（plan/spec/ask_user） | **状态会落错实例**——本轮上下文在本地，写进远端的 plan/回答没人读 |

**注入形态**：恒定注入（单机也注入）+ 无 enum（实例 id 动态无法静态枚举）+ 一句话描述「执行实例：不填=self 本机执行；或已连接的远程实例 id（remote_list 可查）」。缺参时的实例教育仍由运行时提示承担（有实际组网才提示、单机零噪声——2026-09-14 二轮机制原样保留）；执行侧 `self`/`local` 显式归一本地（`_exec_tool`）不变。

**验证**：read_file/add_schedule 有 rid ✓；sleep/remote_list/`__mcp__scnet__job` 无 ✓；schema 两次生成幂等恒定（连接前后不变）✓。引擎层改动，`/restart` 生效。远端独有工具/MCP 的调用缺口由同日 [remote_call_tool](#remote_call_tool远端独有工具mcp-的统一通道2026-09-17用户提案) 补齐。

## 团队看板手动管理远程实例：＋添加 / ✕移除控件（2026-09-14，用户提案）

**用户提案**：「团队抽屉里，可以添加 remote 实例的手动移除、添加控件」——此前组网管理只有 Agent 侧 `remote_connect/remote_disconnect` 工具（模型代劳）或手改 settings.json，用户在 WebUI 没有直接操作入口。

**两层交付**（commit `0d5f167`，前端 `src/static/index.html` + 后端 `src/server.py`）：

| 层 | 内容 |
|---|---|
| 后端两端点 | `POST /api/remote/add`（server_id 可空 + url 必填 → **复用 `remote_tools.connect`**——探测 /api/status 成功才注册；auto id 规则同工具：本地→`agt-{端口}`、远程→`agt-{host}-{端口}`；返回 `{ok: msg.startswith("✅")}`）/ `POST /api/remote/remove`（复用 `remote_tools.disconnect`——断连 + 清持久化）；异常吞掉转 `{ok:false, msg}`，不炸进程 |
| 前端看板 | 团队抽屉「🌐 远程实例」分组**空态恒渲染**（去掉 `if(remotes.length)` 包裹——空态也给出「＋ 添加」入口）；组头「＋ 添加」→ `remoteAddToggle()` 内联表单展开（url 必填 + id 可空）；`remoteAddSubmit()` 提交连接；每实例行「✕ 移除」→ `remoteRemove()` confirm 后断连；结果 **toast 直出**（connect 的人性化文案原样展示：工具数/session/model 一目了然）；操作后 `renderTeamDash()` 即时刷新 |

**关键设计——单源**：端点不另写注册/移除逻辑，直接 import `remote_tools` 走 connect/disconnect——探测、幂等（同 url 复用 id / url↔id 一对一）、settings.json 落盘、启动自动重连等语义与 Agent 侧 `remote_connect` 工具**完全同源**，不存在两套状态。延续 `_REMOTE_ADMIN` 事故以来的原则：组网管理面永远收敛到 remote_tools 一处（WebUI 端点只是它的一个新消费端）。

**与看板数据面的关系**：团队抽屉远程分组读 status 的 `d.remotes`（`REMOTE_SERVERS` 快照）；`get_team_profiles` 的 SYSTEM 注入侧零改动——本次只动 WebUI 抽屉交互面，装配投影不受影响。

**验证**：前端 6/6（三函数存在 / 表单字段 / 移除按钮 / 空态恒渲染）+ JS 语法 ✓；后端 4/4（坏 url 探测失败 / 空 url 拒绝 / 未知 id 未找到 / 真实条目移除清表）✓。**生效方式**：前端 Ctrl+F5；`server.py` 新端点需 `/restart`。

### 失败提示加固：HTTP 非 2xx 显式提示「进程是旧版本」（2026-09-14 · 二轮，commit cb955c5）

**用户报告**：「已断开的远程实例点移除的时候貌似会报错？」——定性：**不是移除逻辑的 bug，是「页面新了、服务旧了」的进程错位**。

**根因链**：

```
Ctrl+F5 刷新页面 → 前端控件出现 ✓（index.html 是静态文件，刷新即得）
但 server.py 的 /api/remote/remove 端点 → 需 /restart 才装载 ✗（上一轮「生效方式」注记的原话即此坑）
点移除 → fetch 打到 404 → FastAPI 返回 {"detail":"Not Found"}（合法 JSON）
→ 旧前端 .json() 解析成功 → r.ok = undefined → toast 只显示干巴巴「❌ 失败」
```

curl 实测旧进程：`POST /api/remote/remove` → `{"detail":"Not Found"}`（404）——失败提示看不出 404，这就是「貌似报错」的全部真相。

**移除逻辑本身无恙**：offline 条目照常走 `remote_tools.disconnect`（断连 + pop + 清持久化），上一轮后端 4/4 已验。

**加固**（commit `cb955c5`，`remoteAddSubmit()`）：fetch 后先查 `resp.ok`，非 2xx 不再裸「❌ 失败」，直接提示——

```
❌ HTTP ${resp.status}——端点不存在：服务进程是旧版本，/restart 后重试
```

「前端新、服务旧」错位从此一眼定位。顺手验收路径：`/restart` 后移除 offline 条目（如 `agt-68j4mxgibv-8000-cnb-run`）→「＋ 添加」重新探测重连，即新控件完整闭环。

## remote_servers 连接表串台：全局 settings 共享 → 实例本地存储 + 自连过滤（2026-09-18，commit aa73942，用户实锤）

**用户实锤**：「本机启动的其它实例都会自动 remote 连接到上一个实例连接的 remote 实例，这个读取是不是串台了？」——9000 连了 director/scriptwriter，director 实例（8100）自己启动时也自动连上了 director（其中还是**自连**）。

**根因**：remote_servers 连接表持久化在【全局】`~/.agt/settings.json` 的 `remote_servers` 键——本机所有实例共享同一份，`_load_persisted` / `_save_persisted` 都读写它。后果两连：① A 实例连了谁，B 实例启动 `reconnect_all` 就自动连谁（**读取串台**）；② 表里有指向自己的 url 时造成**自连**。

**修复三层**：

| 层 | 内容 |
|---|---|
| **存储本地化** | 新增 `_local_store_path()` = `cwd/.agent/remote_servers.json`（cwd=workspace，各 repo 实例天然隔离；同 repo 多实例共享可接受——通常连的也是同一批）；`_save_persisted` 只写本地，不再碰全局 |
| **兼容迁移** | 本地份不存在 → 一次性读全局 settings 旧值（此后独立演化）——存量连接不丢 |
| **自连过滤** | `_is_self_url(url)`：用 `MY_URL`（server 启动时设 `http://<lan_ip>:<port>`）端口比对——host 为 localhost/127.0.0.1 时端口相同即自己，同 lan ip 同端口亦为自己；CLI 裸进程（MY_URL 空）不过滤。**三处生效**：加载两路出口都过滤（全局迁移路上的自连项别再进表）/ `reconnect_all` 跳过 / `connect()` 显式自连**直接拒绝**——文案引导「远程路由的本意是操作【另一个】实例；本机执行直接用工具（remote_instance_id 不填）」 |

`.gitignore` 加 `.agent/remote_servers.json`（实例状态不提交；`.agent/` 不能整目录忽略——workflows/agents/skills/wiki 要提交，精确加单条）。

与 [repo 级覆盖](#配置-repo-级覆盖角色实例认知配置双隔离的地基2026-08-31commit-10d717e)（10d717e）同族原则：**实例自己的状态存自己的 workspace，全局 `~/.agt/` 只是兜底**——组网连接表也归入这一隔离。

**验证（6 例全绿）**：① 自连判定 5 例（127.0.0.1:9000 / localhost:9000 / lan:9000 = 自己；:8000 / :8100 = 否）② CLI（MY_URL 空）不过滤 ③ 本地持久化往返 ④ 本地删 → 迁移读全局旧值 ⑤ director 实例读全局遗留表 → 自连项被过滤（只剩 `{agt-8000, scriptwriter}`）⑥ connect 自连 → `[拒绝自连]` 文案。

**生效**：`/restart`——本机各实例启动时各自读写自己的 `.agent/remote_servers.json`；首次启动从全局遗留表迁移（自连项迁移时即被过滤，不会再连自己）。

## 组件清单

| 组件 | 位置 | 职责 |
|---|---|---|
| `/api/tool/exec` 端点 | server.py | `{name, arguments}` → 工具箱执行 → `{ok, result}`；异步壳 + run_in_threadpool（长工具不占事件循环）；不进 agent.run/不碰 session |
| `/api/remote/add` · `/api/remote/remove` 端点 | server.py | 团队看板手动添加/移除远程实例（2026-09-14，用户提案）：**薄封装直接复用 remote_tools.connect/disconnect**——探测/幂等/持久化与 Agent 侧工具同一条链单源；server_id 可空自动生成。见 [看板手动管理章节](#团队看板手动管理远程实例添加--移除控件2026-09-14用户提案) |
| `remote_tools.py` | src/ | `REMOTE_SERVERS` 注册表 + **`cwd/.agent/remote_servers.json` 实例本地持久化**（启动自动重连/失败标 offline；2026-09-18 起弃全局 settings 共享——串台修复 + 自连过滤，见 [串台章节](#remote_servers-连接表串台全局-settings-共享--实例本地存储--自连过滤2026-09-18commit-aa73942用户实锤)）+ `route_remote_call`（HTTP 执行，结果前缀 `[remote:id]`，180s 超时）+ `_auto_server_id`（url → id 推导）+ `_ws_send_collect`（WS 消息客户端） |
| `Agent._exec_tool` | agent.py | 工具执行统一入口（逐 call/并行两条路径）：arguments 带 remote_instance_id → pop → 路由；显式 `self`/`local` 归一为本地；未带 → 本地执行（组网非空时每轮首次附一行缺参教育提示，2026-09-14·二轮——见[瘦身章节](#schema-瘦身--运行时缺参提示2026-09-14二轮用户裁定)）。⚠️ **`_REMOTE_ADMIN` 管理工具族豁免路由**（见下） |
| `Agent._llm_tool_schemas` | agent.py | LLM 视图 schema 注入 remote_instance_id——**静态化第一档**（2026-09-17）：只认 `_REMOTE_ROUTABLE` 白名单 30 个、**恒定注入**（不看是否组网、无 enum）——连接前后 schema byte-stable 不断缓存；deepcopy 不污染原件。见 [静态化第一档章节](#第三轮静态化第一档_remote_routable-白名单恒定注入2026-09-17用户设计) |
| `{func:load_remote_instances()}` | agent_config.py | SYSTEM 注入：已连接实例清单 + remote_instance_id 路由使用规则；**无连接渲染为空串不注入**（零噪声） |
| 六件套工具 | remote_tools.py | `remote_connect(remote_instance_id?, url)`（探测+注册+落盘，id 可省略自动生成）/ `remote_disconnect` / `remote_list` / **`remote_message(remote_instance_id, message, expect_reply=False)`**（异步 fire-and-forget；expect_reply=True=派活后期望对方完成时回发 answer——见 [expect_reply 章节](#expect_reply派活后对方完成时回发-answer2026-09-17用户提案commit-d98a36c)）/ **`remote_ask(remote_instance_id, question, timeout=120)`**（同步问答）/ **`remote_call_tool(remote_instance_id, name, arguments)`**（远端独有工具/MCP 的统一通道，2026-09-17——见 [remote_call_tool 章节](#remote_call_tool远端独有工具mcp-的统一通道2026-09-17用户提案)） |

**`_REMOTE_ADMIN` 豁免路由（2026-08，commit dfe9f89；2026-09-17 扩六件）**：`remote_connect/disconnect/list/message/ask/call_tool` 的 id 参数是**管理语义**（想用什么 id 连接 / 发给谁 / **调谁的工具**——remote_call_tool 内部自己 `route_remote_call`，走通用路由会把整次调用发到对端再弹回来——套娃），不是路由语义——实际事故：`remote_connect(server_id="cnb-agt", url=...)` 被路由拦截吃掉 → 连接注册从未本地执行 → `[未知 server_id]` 死循环（comfy session 三连败后模型放弃框架通道自己手写了 urllib 轮子）。修复：管理工具族 `name.startswith(_REMOTE_ADMIN)` 判定豁免路由。2026-09-06 改名后该族**双名均豁免**（旧名先规范化成 remote_instance_id 再本地执行，见改名章节兼容矩阵）——撞名同源隐患至此整类消除。

## 使用

```
对话里："连接一下 192.168.1.2 的实例"
Agent 调：remote_connect("http://192.168.1.2:8000")
  → ✅ 已连接 'agt-192-168-1-2-8000'（135 工具 · session=xxx）
  → SYSTEM 出现【远程 agt 实例】清单（含 remote_instance_id 使用规则）

之后任意工具调用带 remote_instance_id 即路由：
read_file({"path": "assets/scene.unity", "remote_instance_id": "agt-192-168-1-2-8000"})
run_python({"code": "...", "remote_instance_id": "agt-192-168-1-2-8000"})   ← 远程 CPU/GPU
```

连接落盘 `cwd/.agent/remote_servers.json`（2026-09-18 起实例本地存储，全局 settings 旧值仅作一次性迁移兜底——见[串台章节](#remote_servers-连接表串台全局-settings-共享--实例本地存储--自连过滤2026-09-18commit-aa73942用户实锤)；重启自动重连；远程关机标 offline，恢复后探测通过自动转 online）。

**auto server_id（2026-08，commit 7d7d1ab）**：实例 id 可省略（`remote_connect` 的 remote_instance_id 形参），从 url 自动生成——本地 url（127.0.0.1/localhost/::1）→ `agt-{port}`（**隧道场景端口是唯一区分维度**——如 SSH 隧道 `127.0.0.1:8300 → 远端容器:8000`，一个本地端口对一远端）；远程主机 → `agt-{host}-{port}`（清洗非法字符）；冲突递增 `-2/-3`。**幂等**：同 url 已在表 → 复用现有 id（offline 恢复/重复连接不再报错）；显式改名（`remote_instance_id="comfy"` 连已注册的 url）→ 移除同 url 旧 id 条目——url 与 id **一对一**，防双 id 并存混乱。工具 schema 里 remote_instance_id 不再必填，docstring 写明「可省略——自动生成」。

**三层组网通道（2026-08 定稿）**：工具级（任意调用带 remote_instance_id，远程零 LLM 成本）/ 消息级异步（`remote_message`，通报派活）/ 消息级同步（`remote_ask`，问它才知道的事）——详见 [跨实例消息通信](#跨实例消息通信remote_message--remote_ask2026-08)。

## 跨实例消息通信（remote_message / remote_ask，2026-08，commit 398a60a）

**用户提案**：跨实例工具调用直接调工具即可（工具级直执行），而跨实例通信（发消息让对方带上下文干活）还需要 run_python 手写 WS 客户端——为什么不做一个工具？直接传实例 id（提案时名 server_id，即今 remote_instance_id）和要发送的消息，异步继续。

**两件套（与工具级直执行互补的「消息级」通道）**：

| 工具 | 语义 | 成本 |
|---|---|---|
| `remote_message(remote_instance_id, message)` | **异步 fire-and-forget**——WS 送达即返（`user`/`message_queued` 回执），对方带自己的 session 上下文异步处理 | 对方异步跑一轮 |
| `remote_ask(remote_instance_id, question, timeout=120)` | **同步问答**——挂流收 answer 到 `_done`，聚合最终回答返回 | 对方一轮 LLM |

**实现**（remote_tools.py `_ws_send_collect`）：http→ws 端点转换、送达回执（对方正忙时 `message_queued` 进它的插话队列也算送达）、answer 聚合、超时降级文案（「对方在忙长任务——加大 timeout 或改用 remote_message」）。两工具加入 `_REMOTE_ADMIN` 豁免（remote_instance_id=发给谁，管理语义，防路由拦截——同 remote_connect 的坑）。

**remote_message 发消息即炸修复（2026-09-02，commit dc1918d）**：`_ws_send_collect(it["url"], message, wait_done=False, ack_timeout=10)` 调用**漏传必填 `timeout`**（只传了 `ack_timeout`）→ `remote_message` 首次真实使用即崩。修复：补 `timeout=10`。由 8000 实例（comfy repo）跨机巡检知会触发时实测捕获——该轮通知改走无此 bug 的 `remote_ask` 送达。教训：**新端点/新工具要有一次真实往返验证**（此前只测过 `remote_ask` 的同步路径，异步路径的必填参数缺口没暴露）。

**实测闭环**：`remote_connect("http://127.0.0.1:8000")` → auto id `agt-8000`（139 工具）→ `remote_ask("agt-8000", "你当前 session 的名字？")` → `[remote:agt-8000] 我当前 session 的名字是「在CNB上调用ComfyUI」`。替代了此前两次手写 WS 客户端场景（问环境那次、发修复通报那次——后者还得事后翻对方 events.jsonl 才拿到回答）。

**三层组网通道**：**工具级**（任意调用带 remote_instance_id，零远程 LLM，远程只是「手」）/ **消息级异步**（remote_message，通报派活）/ **消息级同步**（remote_ask，问它才知道的事）。`/restart` 后工具箱即有五件套。

### expect_reply：派活后对方完成时回发 answer（2026-09-17，用户提案，commit d98a36c）

**用户提案**：「remote_message 有时是给对方安排了特定任务，虽然是异步的，也会期望对方在完成时通知自己——给 remote_message 加一个参数 expect_reply 吧，期望得到回复消息时，对方 answer 时可以转发给那个 agent」。定位：**异步派活 + 完成回发**——补上「消息级」通道里派活（fire-and-forget，结果去向不明）与问答（remote_ask 同步挂起等结果）之间的中间态：不阻塞等待，但对方完成时主动打回来。

**签名**：`remote_message(remote_instance_id, message, expect_reply=False)`。

**完整链路**：

```
发起方（9000）                                对方（8000）
remote_message(id, 派活, expect_reply=True)
  │ 消息头注入 ⟨expect_reply:http://192.168.1.5:9000⟩
  └──────── WS 发送 ──────────────▶ 收到 → 剥掉协议行（模型看到干净任务文本）
                                   挂轮元数据 _reply_to（模型不可见）
                                   …… 异步跑任务（可能几十分钟）……
                                   answer 生成后：connect(MY_URL)（幂等复用）
◀──── [expect_reply 回执] 任务已完成 ──┘
  │ 回答经 inbox 唤醒发起方继续处理
```

**三个改动点**：

| 位置 | 内容 |
|---|---|
| `remote_tools.py` `send_message` | 加 `expect_reply` 形参——MY_URL 已设时消息头注入 `⟨expect_reply:{url}⟩` 协议行；**MY_URL 为空（CLI 裸进程无 WebUI 服务）明确报错 `[expect_reply 失败] 本机回发地址未知…` 不乱发**（收不到回执的假承诺比没这功能更糟）；返回提示带「（对方完成时将回发回答唤醒你）」 |
| `agent.py` `run()` | 轮初正则剥 `^⟨expect_reply:(https?://[^\s⟩]+)⟩` 挂 `_reply_to`——**模型看不到协议行**；`_reply_to` 每迭代重置（只对首条消息生效，自主续跑多轮不误发）；answer 生成后 `_reply_to` 非空 → 临时 `connect` 发起方（幂等复用已有连接）+ `send_message` 回发答案（截前 3000 字） |
| `server.py` 启动 | 设 `remote_tools.MY_URL = http://<lan_ip>:<port>`——lan ip 用 UDP connect 8.8.8.8 取本端地址（不真发包）+ 服务端口；CLI 裸进程不设 |

**边界与生效注意**：

- **对端也要新版**——剥协议行的代码在对端的 agent.py 里；旧版对端会把协议行当普通文本显示给它的模型（不致命但不干净）。本机 `/restart` 即用；跨实例（8000/9300）升级 agt-agent 后对它们用 `expect_reply` 才干净
- 协议行**带内传输**（搭 WS 消息顺风车）——对端无需新端点，旧版可读不炸只是不剥
- 回发依赖发起方 WebUI 服务在线——CLI 裸进程收不到回执，发起侧已挡（MY_URL 未设直接报错）

**验证（四环节单测全绿）**：① MY_URL 未设 → `[expect_reply 失败]` 报错不乱发；② 协议行组装 `⟨expect_reply:http://192.168.1.5:9000⟩`；③ 对端剥挂：`_reply_to` 提取 + 模型只见干净消息；④ connect 返回 id 解析（幂等复用/新连两形态）。

## remote_call_tool：远端独有工具/MCP 的统一通道（2026-09-17，用户提案）

**动机（用户提案 2026-09-17）**：「给 remote_* 系列工具加一个 remote_call_tool，参数就是 name / arguments / remote_instance_id——语义更明确，模型调用它的欲望应该会增加」；「MCP 不加路由参数，因为远端实例的 MCP 可能和本地不同，需要调用的话走 remote_call_tool」。配合路由参数静态化第一档（见[改名章节](#路由参数改名-remote_instance_id--全工具-schema-自动注入2026-09-06用户提案)第三轮），它补上「本地 schema 里根本没有的工具怎么调」的缺口——远端实例装了本地没有的 MCP/外置工具时，唯一通道。

**签名与语义**（src/remote_tools.py，remote_* 组**六件套**第六件，commit `73c143d`）：

```
remote_call_tool(remote_instance_id, name, arguments=None) -> str
# 在远端 agt 实例上调用它的工具——远端独有工具/MCP 的统一通道
# name 可传 "get_tool_schemas" 先探远端工具清单再调
```

**arguments 兼容层（模型写错兜底）**——「json 能不能写对就指望模型能力吧」，工具内部做一层容错：

| 传入形态 | 行为 |
|---|---|
| dict | 直通 |
| JSON 字符串（模型常见误写） | 自动 `json.loads` 反序列化为 dict |
| 字符串且解析失败 | `[错误] arguments 需为 JSON 对象（dict）；收到字符串且无法反序列化：…`（截 200 字符） |
| 其它非 dict | `[错误] arguments 需为 JSON 对象（dict），收到 {type}` |

错误提示即教育——模型看到提示下一轮自行纠正。

**⚠️ 必须加入 `_REMOTE_ADMIN` 豁免（套娃防护）**：remote_call_tool 的 remote_instance_id 是**直通参数**（工具内部自己 `route_remote_call`）。若走通用路由：`_exec_tool` pop 出 rid → 把**整次 remote_call_tool 调用**原样发到对端 → 对端又执行 remote_call_tool(同一个 rid) → 弹回来——无限套娃。2026-09-17 起 `_REMOTE_ADMIN` 六件套（原五件 + remote_call_tool）双名均豁免，注释写明豁免理由。

**验证**：remote_tools.py 末尾 return 列表六件套；`/restart` 生效。

## 已验证（E2E 8001 mock 实例）

单进程起 mock 实例（8001，避开在忙的 8000）完整链路：connect 探测注册 / 远程 read（`[remote:t8001]` 前缀 + 内容 + file_version）/ **路由标记 pop 副作用**（时名 server_id，不进远程参数）/ 远程 edit 改文件 / 复核新 version / 未知工具模型可读错误 / 本地无标记直通 / SYSTEM 注入 / disconnect 清理——全过。commit `6b5ca52`（spec 五步全绿）。

**两个调试插曲（复用价值）**：

- **main.yml 装配方插入坏块**：run_python 脚本往两份 main.yml（`~/.agt/main.yml` L37 与 `src/assets/main.yml` L22，runtime_env 段后）插 `{func:load_remote_instances()}` 行，第一次按"下一个同级 `- `"找块边界产生嵌套缩进坏行 → yaml 解析报错；第二次先删坏行重插才干净。教训：**脚本改 yml，收工前必须 `yaml.safe_load` 验证通过**。
- **mock `/api/status` 连续 500 两次**：手写 mock dict 相继缺 `_current`、缺 `_lock` 而炸——/api/status 的字段面比想象宽（依赖 AgentRegistry 内部状态），最终 mock 直接**继承真 `AgentRegistry`** 才过。对照：**`/api/tool/exec` 只依赖 `agent.tools`，依赖面比 status 轻得多**（connect 探测走 status，工具执行不走）。

## 配置 repo 级覆盖：角色实例认知/配置双隔离的地基（2026-08-31，commit 10d717e）

**游戏组网愿景第一步（用户裁定 2026-08-31）**：此前所有实例读的都是全局 `~/.agt/` 四件套（main.yml / models.json / settings.json / mcp.json）——角色实例无法各持配置。统一解析入口 `config.config_file(name)`（src/config.py）：

- `<cwd>/.agent/<name>` 存在 → 用它（**repo 级覆盖**，文件级整份生效、非字段合并）；否则 `~/.agt/<name>` 全局兜底
- **写侧跟随读到的那份**：本地被读 → 保存写本地；全局被读 → 写全局。`seed_main_agent` 播种仍写全局（不动本地独立主声明），读侧返回本地；/agents 管理页 `_main_` 保存与读侧同源（note 动态显示实际路径）
- cwd 在 import 时锚定（进程启动目录 = workspace）
- 接入六处：config.py 两常量（models/settings——加载/保存/mtime 惰性重载自动跟随）+ `seed_main_agent` + chat.py mcp 连接 ×2 + server.py `_main_` 保存

**角色实例组网用法**（每 repo 一个实例、各自 cwd 启动）：

```
D:\Games\Director\   .agent/{main.yml, models.json}    ← 导演 persona + 独立模型面（creative-kimi）
D:\Games\Media\      .agent/{main.yml, settings.json}  ← 多媒体专家 persona + 独立窗口/折叠参数
```

认知（persona/装配）与配置（模型/参数/MCP）**双隔离**、互不污染；`~/.agt/` 全局只是无本地时的兜底。**本 repo（自我迭代仓库）无 `.agent/models.json` 等本地配置 → 行为不变，纯增量能力**；`/restart` 后生效（当前进程仍是旧代码）。

验证：临时 cwd 带本地四件套 → MODELS 只见本地条目（default 本地）、settings 本地读、seed_main_agent 返回本地 main.yml、文件不存在名回退全局；无本地对照 → 全局路径（现状完全不变）。8+1 项全过，commit `10d717e`。解析规则速查与 `ensure_lsp` 持久化注意（LSP 条目仍固定写全局）见 [config-and-models · 配置文件解析](../guides/config-and-models.md)。

> 与组网工具链的分工：remote_* 五件套解决**实例间怎么通信**，本节解决**每个实例自己是谁**（认知与配置从哪来）——多实例角色化的两块地基，至此齐了（下一步：角色 persona 纯粹化 + 资产工作流封装）。

### UI 补全：设置页配置来源切换（2026-08-31，commit ad0f385）

repo 级覆盖（上）落地的是「读侧自动本地优先」；本节补上**显式选择**（commit ad0f385，用户裁定「设置页保存和读取时需要能选全局还是本地」）——WebUI 设置弹窗顶部新增「配置来源」切换条三按钮：

- **生效份**（默认，现状自动）：本地优先 + 「📦 本地覆盖生效中」徽章（`active_scope`）
- **🌐 全局 ~/.agt**：显式查看/编辑全局那份
- **📦 本地 .agent**：显式查看/编辑本 repo 覆盖份（不存在时提示「保存将新建」）

语义要点：显式模式下载入的是**该份原始文件内容**（非生效运行时视图）；**写非生效份只落盘不热应用**（改全局但本地覆盖生效中 → 存档备用，保存提示明确说明），写生效份照旧热应用（reload + apply_config）。实现三层（config.py 六个 scoped 函数 / server.py WS+REST scope 参数 / index.html 切换条与保存链路），语义表与函数细节见 [config-and-models · 设置页配置来源切换](../guides/config-and-models.md)。

角色实例（导演/多媒体等）至此可在**各自 repo 的 WebUI** 里直接管理自己的模型卡与运行参数，也能查看/编辑全局兜底份——10d717e（读侧自动优先）+ ad0f385（UI 显式选择）拼成完整闭环。

## 边界与后续

- 远程工具箱以对方 `/api/status` 报告的 tools_count 为信息展示（具体工具 schema 未拉取投影——模型按通用工具语义调用，未知工具错误文案兜底；2026-09-06 起 LLM 视图注入 remote_instance_id 路由参数，见 [改名章节](#路由参数改名-remote_instance_id--全工具-schema-自动注入2026-09-06用户提案)；**2026-09-17 起远端独有工具/MCP 有统一通道**——`remote_call_tool` 可先 name="get_tool_schemas" 探远端清单再直调，见 [remote_call_tool 章节](#remote_call_tool远端独有工具mcp-的统一通道2026-09-17用户提案)）
- 消息级驱动（remote_message / remote_ask）**已实现**（2026-08，见 [跨实例消息通信](#跨实例消息通信remote_message--remote_ask2026-08)）——与工具级直执行互补：工具级=远程纯「手」（零 LLM），消息级=让对方带自己上下文干活（对方一轮 LLM）
- 公网使用需隧道 + 鉴权（本期与全服务同信任模型）
- **新环境探索的引导**：README「Agent 上手指引」节（2026-08 新增）——`/status` 环境盘点 → AGENTS.md → workspace 结构 → .agent/ 三件套 → models.json → remote_connect 组网 → 框架参考；新实例首轮对话前读它就知道该探索什么（CNB 云容器部署场景的实测教训，见 [v0.22.0 发布记录](../releases/v0.22.0.md)）

