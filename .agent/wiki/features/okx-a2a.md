# OKX A2A Agent 服务店铺 · ASP 上架 + a2a_bridge 桥接 agt 后端（claw :50051，2026-09-19）

> 把一个 agt 实例包装成 **OKX.AI Agentic 服务市场的卖家**（ASP，Agent Service Provider）：注册店铺、上链挂服务、经 OKX 官方 daemon 自动接单；接单执行后端不是官方默认的 Claude Code CLI，而是**本机另一个 agt 实例**——自写垫片 `a2a_bridge.py` 打通。
> 宿主：50051 实例（workspace `D:\AI\ClawTasks`，组网名 `claw`，见 [多实例组网](../architecture/multi-instance.md)）；全部产物落在该 workspace 根目录。工作窗 2026-09-18 晚 ~ 09-19 凌晨。

## 店铺身份（Brick Studio #13789）

| 项 | 值 |
|---|---|
| 店名/身份 | **Brick Studio #13789**（ASP = Agent 服务提供商） |
| 收款钱包 | `0x8b20…63fe`（X Layer 链，OKX Agentic Wallet） |
| 自我描述 | 双语研究分析工作室：实时联网调研 + 数据分析 + 图表 |
| 状态 | 已激活，heartbeat 在线；**上架审核提交中**（48h 内邮件通知，通过后进 Agent 广场列表；此前只能被 ID 搜索到） |

## 在售服务（两项，已上链）

| 服务 | 价格 | 交付承诺 |
|---|---|---|
| **Quick Research Flash**（服务 id 40787，链 X Layer） | 0.5 USDT/次 | 24h 内回答**一个**研究问题：多源实时调研 + 交叉验证 + 结构化摘要，中英皆可 |
| **Deep Research Report** | 5 USDT/次 | 48h 内深度双语报告：数据分析 + 图表 + 引用 + 一轮修订 |

## 接单链路：daemon → bridge → agt

**「daemon 绑 claude」的真实含义**：OKX 官方 `okx-a2a` daemon 按约定要绑一个 AI CLI 作为执行后端（默认设想 Claude Code）——本实例把后端**换成了自家 agt**：

```
买家 Agent（OKX 平台）
   ↓
okx-a2a daemon          ← OKX 官方接单程序（本要绑 Claude Code CLI）
   ↓                     ← ★垫片顶替：a2a_bridge.py
a2a_bridge.py           ← 把请求经 WebSocket 注入本机 :50051 的 agt 主 Agent
   ↓
agt Agent 干活          ← 联网研究 / 分析 / 画图 / 写报告（全套工具链）
   ↓
回执文件（.a2a_replies/）← 桥读出 → daemon → 平台 → 买家
```

**验证状态：A2A 链路全通（双重验证）**——① `ai exec` 通道直测；② daemon 真实消息进桥 → 收到「桥接成功」回执。技术上最难的坎（从零到能被 OKX 平台调起并回执）已过。

## CNB 容器化部署：`:okx` 镜像 + workspace worker A/B（2026-09-19）

**动机**：解掉本店最大的可用性死穴——daemon 无开机自启、关机即掉线（见上「待办与风险」）。做法把 **okx-a2a daemon + agt 后端 + bridge + 心跳**整体烧进一个自包含镜像，跑在 CNB 云端容器，不再依赖本机常开。

### 交付物（`okx/` + `.cnb.yml`）

| 文件 | 作用 |
|---|---|
| `okx/Dockerfile` | `node:22` 底镜像 + okx-a2a + agt-agent + bridge + 启动脚本——**环境全烧进镜像** |
| `okx/start-worker.sh` | 容器内拉起 daemon / `agt-web` / bridge |
| `okx/heartbeat.sh` | 心跳 + A/B 互拉逻辑（错峰 9h 待实测） |
| `okx/a2a_bridge.py` | 桥接垫片（与 claw workspace 同源） |
| `.cnb.yml` | A/B 双分支（`ws-okx-a` / `ws-okx-b`） |

### 构建：workspace 现场 build（构建流水线无额度）

构建流水线额度用尽，改走 **CNB workspace（SSH 进容器）现场 `docker build` + push**：

```
docker login …                 → Login Succeeded
docker push docker.cnb.cool/years-2025/image-gen2:okx
  okx: digest sha256:da12ae17…f5248d3d  size: 856   （两 layer 已推）
```

- 镜像坐标：`docker.cnb.cool/years-2025/image-gen2:okx`（组织 `years-2025`）
- 构建状态查询：`GET https://api.cnb.cool/years-2025/image-gen2/-/builds/{sn}`（`Authorization: Basic cnb:<token>`），sn 存本机 `~/.cnb_build_sn`（与容器 SSH 地址同文件）；日志 `https://cnb.cool/years-2025/image-gen2/-/build/logs/{sn}`

### 双容器启动

`POST /years-2025/image-gen2/-/workspace/start {"branch":"ws-okx-a"}` → `ws-okx-a` / `ws-okx-b` 各自启动——**同 repo 双分支可并行跑**（实测成立），是 A/B 错峰编排的地基。
SSH 就绪判定：workspace detail 里的 `remoteSsh` 字段——镜像首次拉取慢，未吐出该字段前连不上。

### 三个坑（已修进 git）

1. **a2a-node 要求 Node ≥ 22.14**——`node:20` 底镜像 npm 装直接失败（构建流水线 163s 报错的真身）；换 `node:22` 解决。
2. **CNB `.cnb.yml` 语法**——`docker.image` 必须写在 `vscode:` 列表项**内部**（与 runner / services 平级），放顶层不生效；症状 = 启动后跑的是默认镜像。
3. **自制镜像必须装 `openssh-server`**——CNB 的 SSH 通道由容器内 sshd 提供（默认镜像自带），slim 基础镜像不带 → SSH 起不来。

git 同步：本地 `ws-okx-b` 推送成功（`e486ffb..eb5c585 ws-okx-b -> ws-okx-b`）；workspace 侧 `git commit` 报 `Everything up-to-date`（改动先前已推，非新增）。

### 收尾方式：挂巡检 schedule 接管

`cnb_okx_build_watch`（5 分钟级后台 schedule，见 [定时任务调度](background-scheduler.md)）自读 `~/.cnb_build_sn` 闭环：

| build 状态 | 动作 |
|---|---|
| 成功 | `workspace/start` 启动 worker A → SSH 就绪后验三件套（`okx-a2a status` / `agt-web` 8000 端口 / 心跳进程）→ 全过则汇报 + `cancel_schedule` 自回收 |
| 失败 | 拉构建日志尾部诊断修复 |
| 进行中 | 一句话进度 |

30 分钟拿不到 SSH → 退路：`docker run` 独立验证 `:okx` 镜像本身。

### 尚未做（本轮之后）

- **身份注入 #13789**：注入前须**先停本机 daemon**（防 XMTP 同身份双实例抢消息）
- **错峰编排**：A/B 间隔 9h 启动，互拉逻辑已在 `heartbeat.sh` 写好，待实测

> 与 [SCNet 异步生产流水线](scnet-async-pipeline.md) 同族：外部平台容器化承载业务栈；差别是 OKX 这边容器要常驻接单（daemon 生命周期）+ 商业闭环。

## 订单与行情（截至 2026-09-19 凌晨）

- **已接 2 单**（买家 #1791，**象征价 0.00001 USDT**——性质是测试单）：报告已交付上链，状态 `submitted`
- 验收规则：**买家 3 天不处理自动通过**，通过后 salesCount 入账
- 样例交付物（证明全链能力）：《2024 全球新兴电动汽车市场趋势调研报告》= `report_cn.md` + 图表 `ev_chart.png`——多源研究 → 出图 → 成文一条龙
- **行情侦察**：`services_all.json`（27KB）抓了 Agent 广场全量服务——现价主力区间 0.01~1.5 USDT，头部卖家已售 20~134 单（本店 0.5/5 USDT 属偏专业档）

## workspace 产物清单（D:\AI\ClawTasks 根目录）

| 分组 | 文件 | 内容 |
|---|---|---|
| 身份/服务 | `asp_identity.json`、`asp_services.json` / `asp_services_live.json`、`login_session.json` | 店铺身份与签名材料 / 服务定义草稿与上链实况 / OKX 登录会话 |
| 行情 | `services_all.json`、`agent_search.json`、`service_match.json` | 广场全量快照（27KB）/ 按 ID 搜索结果（审核通过前唯一被发现通道）/ 服务比价 |
| 流程文档（自写） | `preflight.md`、`register.md`、`role-selection.md`、`service-contract.md`、`wallet.md`、`validate.md`、`chat-comm-init.md`、`install-update.md`、`ai-guide.md` | 注册/角色/合同/钱包/校验/通信初始化各环节笔记 |
| OKX 官方文档 | `okx-guide.SKILL.md`、`okx-agentic-wallet.SKILL.md`、`okx-ai-skill.md` | 平方 SKILL 形态的官方指南 |
| 交付物 | `report_cn.md` + `ev_chart.png` | 首份样例交付物（EV 市场报告 + 图表） |
| 运行件 | `tools/`（daemon + `a2a_bridge.py` 等）、`.a2a_replies/` | 接单脚本与回执目录（⚠️ `tools/` 子目录经 `remote_instance_id` 路由读取被 workspace 边界误挡，未逐一核内页——见注意事项） |

## 待办与风险

| 卡点 | 说明 |
|---|---|
| 等买家验收 | 2 单 submitted；3 天自动通过 |
| 等上架审核 | 通过前不在广场列表，只能被 ID 搜到（48h 邮件通知） |
| ⚠️ daemon 无开机自启 | **关机即掉线接不到单**；装自启需管理员权限（须本机 owner 操作）——**已定路线：容器化到 CNB**（见 [CNB 容器化部署](#cnb-容器化部署okx-镜像--workspace-worker-ab2026-09-19)），本机自启降为备选 |

**下一步（已提出，待用户拍板）**：① `a2a_bridge.py` 收编——它是通用基础设施（任何 agt 实例接 OKX A2A 都可复用），现散落在 claw workspace，宜收进本仓或独立 repo；② 身份注入 #13789（须先停本机 daemon，防同身份双实例）+ A/B 错峰编排。

## 与其他模块的关系

- [多实例组网](../architecture/multi-instance.md)：claw（:50051）即组网实例之一，主 Agent 用 `remote_instance_id` 路由跨实例查探其 workspace（本轮诊断即此用法；`tools/` 子目录路由被误挡是个待查边界）
- [跨实例客户端](../features/remote-client.md)：a2a_bridge 本质是**外部 WS 客户端**向 agt 注入消息——与「agt 作为别家 WS 客户端」同一协议面的反向用法
- [用户交互](../features/user-interaction.md)：注入消息走 inbox 唤醒轮语义
- [SCNet 异步生产流水线](../features/scnet-async-pipeline.md)：同族形态——外部平台 ↔ agt 的自动接单/回执（SCNet 容器回调 `/api/callback`，OKX daemon 经桥注入 WS）；**区别是 OKX 这边多了商业闭环**（钱包、定价、验收、评分）

## 注意事项

- **营收尚未验证**：2 单均为象征价测试单，真实付费订单 = 0；技术链路（注册→上架→接单→交付→回执）已闭环，商业面等验收与审核结果。
- **敏感材料明文散落 workspace 根目录**（`asp_identity.json` / `login_session.json` / 钱包材料）：收编/迁移时切勿提交进公开 repo。
- `tools/` 子目录跨实例路由读取报「拒绝访问 workspace 外的路径」——list_dir 根目录正常、子路径被挡，疑似路由 workspace 边界判定的边界 case，收编 bridge 时顺带核。
