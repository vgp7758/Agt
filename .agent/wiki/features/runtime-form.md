# 运行形态自我认知 · _runtime_form（Agent 知道自己怎么跑、外界怎么和它说话）

> `_runtime_form()`（src/agent_config.py，FUNC_REGISTRY 的 `runtime_env` 底层实现）。2026-09-19 用户提案，随 [v0.29.5](../releases/v0.29.5.md) 发布。
> 用户原话：「环境信息里是不是也应该让它自己知道，比如『当前启动了一个服务在 localhost:8000，外界是通过这个服务与自己交互的』」。

## 职责

让 Agent（主 Agent 与任何装配 `runtime_env` 的实例 / 子 Agent）在 SYSTEM 里自带「**运行形态自我认知**」：

- **我在什么形态下跑**——CLI 交互 / WebUI 服务（含服务地址与端口）/ 云容器（含隧道公网入口与生命周期）/ 消息桥接（daemon 转发）
- **外界怎么和我说话**——浏览器开哪个地址；脚本/服务/其它机器走哪个端点推消息（[外部事件注入](external-injection.md)）
- **我的数据在哪**——workspace 与数据目录
- **我该怎么表现**——网页气泡场景要渲染友好（markdown/文件引用控件）、终端场景要纯文本紧凑；知道被 daemon 桥接（如 [OKX A2A](okx-a2a.md)）才知道回复要走回执协议

## 实现与设计约束

- **函数演进**：`_func_runtime_env()` → `_runtime_form()`（2026-09-19 重写为 87 行；「包名/版本/升级方式/GitHub + 外部事件注入指路」全部保留）
- **内容启动后恒定**（服务地址、端口、形态启动即定）——**缓存前缀友好**，不引入轮内字节变化（对照 [投影缓存三铁律](../architecture/context-engine.md#deepseek-缓存行为实证v3-位置敏感--v4-system-规范化2026-08-两代后端)：任何 system 变化全断缓存）
- 装配入口：main.yml / 子 Agent 声明的 assembly 里 `{func:runtime_env()}`（主 Agent 本仓 main.yml 的【运行环境】段）；看板/下拉 UI 见 [agents-admin · FUNC_REGISTRY](agents-admin.md)

## 消费端与行为影响

| 形态 | Agent 应该知道的 | 行为影响 |
|---|---|---|
| CLI | 终端交互中 | 纯文本紧凑回答 |
| WebUI | 服务地址 `http://localhost:{port}`（经 `remote_tools` 拼 URL） | markdown / 文件引用控件友好渲染 |
| 云容器 | 公网入口（CNB 直达链接）+ 生命周期（如 18h 重建） | 自知「我在远端、随时可能重建」 |
| 消息桥接 | 被 daemon（OKX A2A 等）转发 | 回复要走回执协议（`.a2a_replies/`） |
| 数据目录 | workspace / 数据根 | 文件操作路径正确性 |

容器实例实测形态（brick，:okx 镜像）：**WebUI 服务 + 公网入口 + 容器生命周期**——直连其直达链接问「你现在的运行形态是什么」即可验证。

## 相关页面

- [agents-admin · FUNC_REGISTRY / runtime_env](agents-admin.md) — 装配入口（`{func:runtime_env()}`）与下拉 UI、docstring tooltips
- [外部事件注入](external-injection.md) — runtime_env 输出里的指路目标（脚本/服务/机器 → `/api/callback`）
- [桌面版 · 数据根](desktop-mode.md) — 运行形态的另一种区分维度（`AGT_DESKTOP`，数据根差异；本页是「交互形态」维度，两者互补）
- [OKX A2A Agent 服务店铺](okx-a2a.md) — 消息桥接形态的实例（a2a_bridge → agt 后端）+ 容器内运行形态验证
- [v0.29.5 发布记录](../releases/v0.29.5.md) — 本功能随版发布