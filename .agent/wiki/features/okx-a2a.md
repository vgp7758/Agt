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
| ⚠️ daemon 无开机自启 | **关机即掉线接不到单**；装自启需管理员权限（须本机 owner 操作） |

**下一步（已提出，待用户拍板）**：① `a2a_bridge.py` 收编——它是通用基础设施（任何 agt 实例接 OKX A2A 都可复用），现散落在 claw workspace，宜收进本仓或独立 repo；② daemon 自启 + 挂巡检 schedule 盯验收/审核状态。

## 与其他模块的关系

- [多实例组网](../architecture/multi-instance.md)：claw（:50051）即组网实例之一，主 Agent 用 `remote_instance_id` 路由跨实例查探其 workspace（本轮诊断即此用法；`tools/` 子目录路由被误挡是个待查边界）
- [跨实例客户端](../features/remote-client.md)：a2a_bridge 本质是**外部 WS 客户端**向 agt 注入消息——与「agt 作为别家 WS 客户端」同一协议面的反向用法
- [用户交互](../features/user-interaction.md)：注入消息走 inbox 唤醒轮语义
- [SCNet 异步生产流水线](../features/scnet-async-pipeline.md)：同族形态——外部平台 ↔ agt 的自动接单/回执（SCNet 容器回调 `/api/callback`，OKX daemon 经桥注入 WS）；**区别是 OKX 这边多了商业闭环**（钱包、定价、验收、评分）

## 注意事项

- **营收尚未验证**：2 单均为象征价测试单，真实付费订单 = 0；技术链路（注册→上架→接单→交付→回执）已闭环，商业面等验收与审核结果。
- **敏感材料明文散落 workspace 根目录**（`asp_identity.json` / `login_session.json` / 钱包材料）：收编/迁移时切勿提交进公开 repo。
- `tools/` 子目录跨实例路由读取报「拒绝访问 workspace 外的路径」——list_dir 根目录正常、子路径被挡，疑似路由 workspace 边界判定的边界 case，收编 bridge 时顺带核。
