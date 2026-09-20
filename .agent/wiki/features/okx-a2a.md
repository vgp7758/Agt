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

## 容器实例升级 + 镜像重烧 + start_agt.sh 加固（2026-09-19 · 二，v0.29.5）

**背景**：上一轮（本页上文）容器部署后，brick 实例跑的是**手工 patch 版**（未随正式发布走）；v0.29.5 发布（运行形态自我认知 + 端口预检 SO_REUSEADDR + callback AGT_HOME 修复）后统一换正式版：

| 动作 | 内容 |
|---|---|
| 实例升级 | `pip install -U --break-system-packages agt-agent==0.29.5` → 已装 0.29.5；`/start_agt.sh` 重启（新加固逻辑生效：kill 后等端口真空闲再起） |
| 镜像重烧 | `:okx` 新镜像 digest `sha256:d4dcfdbd…` 已推制品库；容器内验证三件套：agt-agent **0.29.5** ✓ / `_runtime_form` 存在 ✓ / SO_REUSEADDR 存在 ✓ |
| start_agt.sh 加固 | kill 旧进程后**循环探测端口 bind 可用**再启动——修重启 TIME_WAIT 误判占用（见 [ops · /restart 端口预检](../guides/ops.md#restart-端口预检-soreuseaddrtime_wait-误判占用实例掉线根因2026-09-19随-v0295)）；已提交进 image-gen2 两分支 |
| 长效机制 | Dockerfile `pip3 install agt-agent` **不带版本 pin** → **18h 重建即最新版**，无需手工升级 |

- **运行形态自我认知在容器里的实际效果**：brick 的 `_runtime_form` 段描述自身为 **WebUI 服务 + 公网入口（`https://adk2zs60ym-8000.cnb.run/`）+ 容器生命周期**——用户可直连直达链接问它「你现在的运行形态是什么」验证。详见 [运行形态自我认知](runtime-form.md)。
- **build 机已停**（省额度）；重建由 18h 生命周期自动触发。
- 顺带清理 workspace 垃圾文件（`grep.exe.stackdump` / 截图 / 乱码目录）。

### 尚未做（本轮之后）

- **身份注入 #13789**：注入前须**先停本机 daemon**（防 XMTP 同身份双实例抢消息）
- **错峰编排**：A/B 间隔 9h 启动，互拉逻辑已在 `heartbeat.sh` 写好，待实测

> 与 [SCNet 异步生产流水线](scnet-async-pipeline.md) 同族：外部平台容器化承载业务栈；差别是 OKX 这边容器要常驻接单（daemon 生命周期）+ 商业闭环。

### 后记：brick 升 v0.29.6——版本检查双修正是从这里误报钓出来的（2026-09-19 · 三）

### 后记：brick 升 v0.29.6——版本检查双修正是从这里误报钓出来的（2026-09-19 · 三）

- **误报现场**：brick 的 WebUI 横幅显示「🆕 新版本 **v0.29.4** 可用（当前 v0.29.5）」——用户一眼看穿版本号倒挂。根因两连（详见 [v0.29.6 发布记录](../../.agent/wiki/releases/v0.29.6.md) / [desktop-mode · /api/latest](../../.agent/wiki/features/desktop-mode.md)）：① 版本源查 GitHub Releases latest（0.29.5 只推 PyPI 未建 Release → latest 停在 v0.29.4）；② 比较兜底 `!=`（容器缺 packaging → `Version()` 抛异常 → except → `"0.29.4" != "0.29.5"` → True）。
- **修复 + 升级**：`_ver_gt` 语义版本比较 + 版本源按形态分流（pip → PyPI JSON API）；brick 已 `pip install -U --break-system-packages agt-agent==0.29.6` 并重启，实测 `/api/latest` → `{"current":"0.29.6","latest":"0.29.6","update_available":false}` ✅ 横幅消失。
- **start_agt.sh v3 加固**（本轮的排查衍生）：kill → SIGTERM 等 12s → SIGKILL 兜底 → 确认死透 + 端口真空闲才启动——修「kill 后探活探到旧进程误报 ready、实例一直跑旧代码」（正因如此修复一度"看起来没生效"）；已提交进 image-gen2 两分支。

## 容器双双被回收 → A 容器复活 + 钱包跨容器失效实锤 + 本机保活根治（2026-09-21，用户实锤）

用户发现 CNB 把 A/B 两个 workspace 容器**同时回收**——`heartbeat.sh` 的 A/B 互拉没能保住。**互保活的盲区**：它防的是「单边死亡、另一边拉起」，防不了平台把两个一起收走（A 容器自 15:00 起仅 ~10h 即被回收，远没到 18h 生命周期上限——更像对无交互/无 Web 活动的 workspace 提前回收）。**根治只有一条路：本机（常开）兜底**，见下「保活巡检」。

### A 容器复活：新短链 + 全套自愈验证（2026-09-21）

| 验证项 | 结果 |
|---|---|
| 新短链 | `https://nai0dl67kj-8000.cnb.run`（200 ✓ Agt 页面）；旧 `adk2zs60ym-8000.cnb.run` 随容器销毁失效——**cnb.run 短链跟容器实例走，硬编码该地址的消费端都要跟着换** |
| agt-web | ✓（138 工具）· session=A2ABrickStudioBusiness · model=deepseek |
| 台账 | ✓（9 单 delivered 状态保留） |
| 巡检 bootstrap | ✓ 重注入（容器内 agent 重启确认 2h 巡检 + 5min patrol 运作中） |
| brick 组网 | ✓ `remote_disconnect` + `connect` 新地址重连（见 [多实例组网](../architecture/multi-instance.md)） |
| agent_watch | REMOTE_WATCHES brick 地址已更新为新短链（见 [agent-watch](agent-watch.md)）；顺带确认剧组 4 实例 + SCNet 运维 9300 都在 |

### ⚠️ 钱包登录态跨容器失效：saTeeId（TEE 实例绑定）实锤

新容器从 git 恢复了全部凭证文件（keyring / machine-identity / session.json——**文件都在**），但 `wallet status` 仍 `loggedIn: false`。此前担心的 **saTeeId 风险实锤**：凭证绑定原 TEE 实例，跨容器重建 = 新实例 → 会话失效。**平台设计，配置救不了，必须重新登录**（用户浏览器点 OKX sociallogin 授权链接完成）：

- 登录一次 ≈ **99 天有效**
- 未登录期间 A2A 消息仍可**手动应答**，只是不能自动接单/验收

> 教训：凭证「文件在」≠「登录态在」——TEE 绑定的会话材料跨实例不可迁移；容器重建的标准恢复步骤要把「用户重新登录一次」列为必做项（依赖用户一次点击，Agent 代不了）。

### 根治：本机保活巡检 cnb-brick-keepalive（每 30 分钟）

```
每 30 分钟探新短链：
  通   → 静默
  不通 → CNB API 重启容器 → 等 agt 就绪 → playwright 转发端口拿新短链
        → 更新 agent_watch REMOTE_WATCHES + remote_connect 重连 brick
        → 邮件通知用户（含新短链）
```

从此 CNB 就算把容器全收走，**30 分钟内本机自动拉起并邮件通知**——A/B 互拉降级为第一道快线，本机巡检才是最终兜底。巡检属本机 schedule（见 [定时任务调度](background-scheduler.md)），与 [agent-watch](agent-watch.md) 的「远程静态清单无法自动发现」正好互补：后者只报状态变化，前者负责把容器救活并同步地址。

### 保活巡检首次实战：CNB 再回收 → 自动复活闭环（2026-09-22，03:53）

**巡检第一战就兑现了价值**：03:53 巡检发现短链 `nai0dl67kj-8000.cnb.run` → 401（转发失效）、SSH 也被拒 → 判定**容器又一次被回收**（即 A 容器复活后再次被平台收走，第三次换短链）。全程无人工自动走完 SOP ②：

| 步骤 | 结果 |
|---|---|
| CNB API 重启 | 新容器 `cnb-rag-1k2tg8crc`（SSH 15 秒就绪） |
| 等自动装配 | agt-web 200 · 心跳 ✓ · daemon ✓——期间 agt-web 曾**异常退出一次**，被加固版 `start_agt.sh`（v3：kill → 等 12s → SIGKILL 兜底 → 端口真空闲才启动）**立即恢复**——瞬时故障也能自愈 |
| 转发端口拿新短链 | **`https://iqhxsci1es-8000.cnb.run/`**（旧 `nai0dl67kj` 随容器销毁失效） |
| 更新监视/组网 | agent_watch `REMOTE_WATCHES` 已更新新短链（`tools/agent_watch.py`）；remote_connect 组网重连**待 /restart**——诊断出 9000 进程内是旧代码、探测超时值过短 |
| 邮件通知 | ✓ 已发 foxmail（新短链 + 登录链接 + WebIDE 入口） |
| 钱包检查 | `loggedIn: false` → 生成登录链接再发（见下） |

**复活后全绿验证**：`GET /` 200（Agt 页面）· `POST /api/status` 200（ready=true · 138 工具 · session=A2ABrickStudioBusiness · deepseek）· `GET /api/tools` 200（61KB 工具表）。

**钱包登录态跨容器失效二次实锤**（saTeeId TEE 实例绑定）：即便上个容器也是自动复活体，**每次重建登录态都跨不过去**——`loggedIn:false` 已成每次回收的必然伴随项。登录链接已随邮件发出（`https://web3.okx.com/account/sociallogin?authSessionId=…`，vgp123@foxmail.com 的 OKX 账号，≈99 天有效），等用户 30 秒点击；下一轮巡检会自动确认恢复。

**教训升级**：「容器重建的标准恢复步骤 = 用户重新登录一次」从「列为必做项」升级为**每次回收都必然触发**——巡检可自动救活容器、同步地址、发通知，但**点登录链接这一步永远依赖用户**（Agent 代不了）；保活巡检的价值 = 把「发现死亡 → 复活 → 通知」全程自动化，剩下唯一人工动作收敛为每周一次的点登录。

### 巡检补跑三绿 + 钱包登录态恢复（2026-09-22 晨）

08:08 被中断那轮的保活巡检补跑，三项全绿：

| 检查 | 结果 |
|---|---|
| ① agt（claw :50051） | ready=true · 161 工具 · 61 轮 · 空闲 · inbox=0 ✓ |
| ② 本机 daemon | running（pid 24348）✓ |
| ③ 钱包 | **loggedIn: true** ✓——上轮邮件里等用户点击的登录链接已兑现，**登录态恢复**（自动接单能力回归） |

另两项确认：**CNB 容器仍活着**（转算力待命中，未再被回收）；brick 的 agt 在线但其 **daemon 未跑**——与本机生产 daemon 无抢消息冲突（agent_watch 修复后首封「真实邮件」报的正是 brick 上线，见 [agent-watch](agent-watch.md)）。

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
| 📧 重要情况邮件通知通道（未装） | 目标：容器内实例在断连/接单/交付等节点自动发信到 `vgp123@foxmail.com`（正文带 agt 聊天界面直达链接），用来兜住「容器掉了没人知道」的空白。SMTP 授权码入口已定位（在 QQ 邮箱**安全设置** tab，非会员功能），**卡在前置门槛：需用户先绑手机号 → 开启 POP3/SMTP → 生成 16 位授权码**；详见 [home](../home.md) 快速事实增补（2026-09-19 · 二） |

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
