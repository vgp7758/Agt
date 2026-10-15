# dashboard · agt-web --dashboard 多实例统一看板（spec s_95425415，2026-10-15，commit 99addf8）

> `src/dashboard.py`（服务主体，939 行）× `src/static/dashboard.html`（前端）× `src/chat.py`（`--dashboard` 分流）× `src/server.py`（`GET /api/interactions` 历史回填端点）。组网底座见 [多 agt 实例组网](../architecture/multi-instance.md)——dashboard 是其端点的**批量消费端**，组网侧零改动。

## 职责

把多个 agt 实例（本机多端口 / 局域网 / cpolar 公网隧道）收进**一张独立管理面**：

| 能力 | 实现 |
|---|---|
| 状态总览 | 卡片网格：🟢空闲 / 🟡忙碌 / 🔴离线 + recap + 已连 chips（3s 轮询，探测后台做、请求零阻塞） |
| 本地实例启停 | Popen 拉起 / taskkill 杀树 / netstat 反查接管（孤儿进程也能停） |
| 自动发现 | netstat LISTEN → 逐候选 POST /api/status 探测 → 未纳管实例列表；cpolar 域名自动推断公网地址 |
| 拉网线 | 对 A 调 `/api/remote/add`（wire）——A 获得调 B 工具/发消息的能力；拆线走 `/api/remote/remove` |
| 互动群聊流 | InteractionHub 旁听各实例 WS 事件流，采集 remote_* / notify 调用成群聊记录 |
| 拓扑邻接表 | 逐在线实例 GET /api/dash 提 remotes[]——看板画连接关系 |

## 定位：纯管理面

`agt-web --dashboard [port]`（默认 9100，src/chat.py 尽早分流到 `dashboard.serve()`）——**不装配 Agent**：不占 session、不消耗 LLM、崩了不影响任何被管实例。`serve()` = FastAPI + uvicorn 前台 + 自动开浏览器。

## 数据文件（AGT_HOME，默认 ~/.agt）

| 文件 | 内容 |
|---|---|
| `dashboard.json` | 实例注册表（instances + pids）——**独立于实例的 `extra_state.remote_servers`**（见下「已知语义」） |
| `dashboard_interactions.jsonl` | 互动流追加日志——重启不丢，启动时读尾部重建指纹集合（回填幂等） |

## 模块结构（src/dashboard.py，按 spec 施工顺序）

| 组件 | 职责 |
|---|---|
| `DashboardStore` | 注册表读写（原子写 tmp+replace；CRUD + set_pid/get_pid） |
| `probe_instance` | POST {url}/api/status（4s 超时；401/403 带 X-Cb-Token 重试）→ 状态快照 |
| `start_instance` / `stop_instance` | Popen 拉起（后台线程轮 /api/status 等 60s 就绪）/ taskkill 杀树 + 端口死确认；netstat 反查 pid 接管（dashboard 重启后仍能停掉注册表记过的实例） |
| `discover_local` | netstat LISTEN → 排除已知非 agt 端口（lsp/pydevd/pprof/es/clash：4040/6060/9200/7897）→ 逐候选探测；诊断存 `DISCOVER_DIAG`（/api/discover 带 `_diag` 字段） |
| `cpolar_domain()` | 从 cpolar 配置/日志读当前隧道域名 → 发现结果附 `public_url = https://<dom>/port-<port>`（复用 [agent_watch 的 /port-N 路由](agent-watch.md)） |
| `wire` / `unwire` / `topology` | 组网三件：对 A POST /api/remote/add（server_id=B.name, url=B 对外 url）/ remove / 逐实例提邻接 |
| `InteractionHub` | 互动采集（见下节） |
| `DashboardRuntime` | store + hub + 状态缓存 + 后台轮询线程（probe 每 3s，每 2 轮刷一次拓扑） |

## InteractionHub：互动群聊流（发送方视角）

per-instance 一条 WS（/ws）**旁听线程**（daemon，断线 10s 重连），过滤 `type=tool_call` 且 `name ∈ WATCHED_TOOLS`——发送方参数里带完整 to+text（探索实证）：

```
WATCHED_TOOLS = {remote_message, remote_ask, remote_call_tool, agent_notify, agent_prompt}
→ {ts, from(实例名), to, kind(工具名), text(截 2000)}   # remote_call_tool 展开 name+arguments
```

- 内存环 deque(2000) + 追加落盘 jsonl；**指纹去重**（ts+from+to+kind+text[:100]）——启动回填与实时采集可能交叠，重复只记一笔
- **历史回填**：启动时对在线实例 GET /api/interactions?limit=500（server.py 只读端点，读该实例共享 toollog.jsonl 还原历史 remote_* 调用）——**旧版实例 404 静默降级**（只从启动时刻开始积累）；回填条目 ts 缺省 0，query 恒最先给齐（UI 标「更早」）

### 冒烟修出的关键 bug：短 recv 超时把「静默」误判成「断线」

旁听者无话可说时 recv 长时间无数据属**正常静默**；最初短超时把它当断开处理 → 反复重连，**事件采集占空比仅 1/3，漏采大量互动**。修复（现行）：`ws.settimeout(30)`——30s 静默 → `ws.ping()` 探活（对端死连接这里会抛 → 才真断线走重连），活着就 continue 继续听。

## API 端点（create_app）

| 端点 | 说明 |
|---|---|
| GET /api/state | 一次读齐：注册表 + 探测状态 + 拓扑（探测后台做） |
| GET/POST /api/instances · PUT/DELETE /api/instances/{id} | 注册表 CRUD（POST/PUT 支持 name/kind/url/public_url/repo/port/start_args/token/note 九字段） |
| POST /api/start · /api/stop | 本地实例启停（杀树 + 端口死确认） |
| POST /api/wire · /api/unwire | 拉线 / 拆线（复用组网端点） |
| POST /api/discover | 自动发现（附 cpolar_domain + `_diag` 诊断） |
| GET /api/interactions | 互动流查询（since/sender/to/limit；顺手清 dirty 标记） |

被管实例侧配套一个**只读回填端点**：`GET /api/interactions`（src/server.py，spec Step10）——dashboard 启动时逐实例拉一次。

## 冒烟套件（smoke/）

`smoke_launch.py`（隔离 AGT_HOME 起 3 进程：dashboard 19100 + 实例 A 18001 + 实例 B 18002）→ `smoke_drive.py`（**21 步驱动器**）→ `smoke_cleanup.py`（清理）；`smoke_result.json` 全 ✅ + Playwright 截图（`dashboard_smoke.png`）。隔离产物（home_*/ws_*，含 models.json 副本）已进 `.gitignore` 防误提交。

21 步覆盖：发现 → 导入 A/B → 拉线 A→B → WS 驱动 A 真调 `remote_message`（glm-official-flash 一轮）→ **互动流捕获 `{from, to, kind:remote_message, text:hello-from-A-smoke}`** → 过滤器 → 拆线 → 停止（杀树·端口死）→ 重启（重新就绪）。前端视觉验证：卡片 / 发现弹窗 / 添加弹窗（9 字段）/ 拉线弹窗 / 互动消息渲染；favicon 404 顺带修复。

冒烟另修两处健壮性：① `discover_local` 探测超时 1.5s 太紧 + DETACHED 进程 subprocess 需 `stdin=DEVNULL`（`_diag` 诊断字段定位）② 冒烟驱动器自身 stderr 与 capture_output 冲突。

## 已知语义与注意事项

- **拉线状态跟随 A 的 session**：wire 落在 A 的 `extra_state.remote_servers`（组网持久化的既有设计，见 [multi-instance · 串台后记二](../architecture/multi-instance.md)）——**A 重启后需重拉网线**（冒烟实证，非 bug）
- **注册表独立**：dashboard.json 与各实例的 remote_servers 互不同步——首次使用需在页面「发现」导入一次；「已连 chips」与拓扑读的是实例实时 /api/dash，与注册表解耦
- 自动发现只扫本机 LISTEN 端口；远程实例用「添加」手工录入（url + token）
- cpolar 域名会漂移（见 [agent-watch ⚠️](agent-watch.md)）——发现结果里的 public_url 只在当次有效

## 相关页面

- [多 agt 实例组网](../architecture/multi-instance.md) —— wire/unwire 复用的组网端点与注册表语义
- [agent_watch](agent-watch.md) —— cpolar 域名读取与 /port-N 隧道路由的同源机制
- [施工委托](construction-delegation.md) —— 本看板施工（plan p_c0542ede）是施工委托的首个实战线（worker 三断点续跑 + update_plan 标满 10/10）
