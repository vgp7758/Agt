# 外部事件注入 · 脚本 / 服务 / 其它机器 → Agt 实例（HTTP 回调通道）

> 一句话：`POST http://<实例地址>/api/callback` + header `X-Cb-Token: <callback_token>` + JSON `{"text": …, "source": …}`
> → 消息进该实例 **inbox** 并**唤醒它跑一轮**（等价于 `push_message(..., wake=True)`）。推文件加 `X-Cb-Type: file`。
> 2026-09-19 交付：对外文档 [docs/external-injection.md](../../../docs/external-injection.md) + Agent 自我认知指路（`{func:runtime_env()}`）+ 修掉「AGT_HOME 非默认环境下回调全被拒」的真 bug（commit `523f8ba`）。

**为什么需要它（用户提案 2026-09-19）**：「当前其它实例并不清楚如何让脚本通过 api 向自己发送消息吧」——回调端点 2026-09-14 就有了（见 [SCNet 异步生产流水线](scnet-async-pipeline.md)），但知识只散在 wiki/对话里，**Agent 自己不必然知道**。补法是「写一份仓库文档 + 在自我认知模块里指路」，让任意实例的模型在该用时能自己找到答案。

## 三层通道总览

| 通道 | 形态 | 触发方 | 语义 |
|---|---|---|---|
| **HTTP 回调**（本文主角） | `POST /api/callback` | 任意进程（脚本 / 服务 / 定时任务 / 另一台机器） | 消息或文件进对方 inbox，**对方带自己上下文跑一轮** |
| **Agent 侧工具** | `remote_message` / `remote_ask` / `remote_call_tool` / `agent_prompt` | 另一个 Agent（带 `remote_instance_id` 路由） | 见 [多实例组网](../architecture/multi-instance.md)——外部脚本用不了这些工具，走上一行 |
| **只读 / 取手脚** | `/api/status` `/api/tool/exec` `/api/dash` `/api/stats` `/api/wf/runs` | 任意进程 | `tool/exec` 借对方的「手」执行工具，**不进对方上下文**——与回调的消息驱动互补 |

**关键区分（写进文档的坑 3）**：**消息驱动 ≠ 工具路由**。`/api/callback` 进的是**对方上下文**（它会自己决策怎么处理）；`/api/tool/exec` 只是借用手脚（零 LLM 成本，`remote_call_tool` 的底层就是它）。

## 通道一：POST /api/callback（src/server.py `api_callback`）

| 项 | 值 |
|---|---|
| 鉴权 | 目标实例 `settings.json` 的 `callback_token`（32 位 hex）；**未配置 = 拒绝一切回调** |
| 传递位置 | **header `X-Cb-Token`（首选）** > query `?token=`（兜底）——cpolar 等隧道会丢 query string（2026-09-14 实测） |
| 形态 A：JSON 消息 | body `{"text": …, "source": …}` → 入 inbox（持久化 `inbox.jsonl`，重启不丢）+ `push_message(wake=True)`（空闲立刻开一轮，忙碌排队）；返回 `{"ok":true,"queued":true,"inbox_size":N}` |
| 形态 B：文件 | header `X-Cb-Type: file` + `X-Cb-Filename: …`，raw body = 字节 → 落盘 `<workspace>/scnet_inbox/{HHMMSS}_{filename}` + 自动推一条通知消息（`📥〔外部回调·文件〕…`）唤醒；返回 `{"ok":true,"saved":…,"size":…}` |
| 失败 | `{"ok":false,"error":…}` |

文档给出可直接抄的两段：curl 示例 + `push_event(base_url, token, text, source)` Python 函数（urllib 标准库，**token 走 header**）。

## 通道二 / 三：Agent 工具与只读端点（文档第二节 / 第三节）

| 端点 | 方法 | 用途 |
|---|---|---|
| `/api/status` | POST | 实例状态快照（就绪/模型/工具数/session/busy/work_q/inbox 深度/子进程树），详见 [api-status](api-status.md) |
| `/api/tool/exec` | POST | `{name, arguments}` → 在本实例执行工具（跨实例工具路由的落地端） |
| `/api/dash` · `/api/stats` · `/api/wf/runs` | GET | 团队看板 / LLM 调用统计 / 工作流运行列表 |

## 自我认知指路：runtime_env 追加【外部事件注入】（src/agent_config.py）

`_func_runtime_env()`（`{func:runtime_env()}`，本仓库 main.yml 的「【运行环境】」段引用）原输出只有「包名 + 版本 + 升级方式 + GitHub」，2026-09-19 追加一段：

```
【外部事件注入】需要让脚本/服务/其它机器（或你自己的后台任务）通过 HTTP 向本实例或队友
推送消息/文件/事件时，见 docs/external-injection.md（GitHub: vgp7758/Agt/blob/main/docs/external-injection.md）
——核心一句话：POST <实例地址>/api/callback + header X-Cb-Token（token=该实例 settings.json 的 callback_token）
+ JSON {"text":…, "source":…} → 进对方 inbox 并唤醒一轮；推文件加 X-Cb-Type: file。
```

**覆盖范围**：凡装配了 `runtime_env` 的实例（本机 / brick 容器 / 剧组五角色），`/restart`（或升到含该改动的包）后**下一轮起就知道这篇文档**——「怎么被外部推送」从 wiki/对话知识变成**模型自带的自认知**。文档本身在 GitHub 公开，任何实例的模型可自行读取。

**二轮（2026-09-19 · 二，v0.29.5）**：`_func_runtime_env()` 重写为 `_runtime_form()`（运行形态自我认知）——输出升级为**五维**：交互形态（CLI / WebUI 服务地址端口）/ 容器与隧道公网入口 / 消息桥接 / 数据目录 / 外部事件注入指路（本段保留其中）。**内容启动后恒定（缓存前缀友好）**；直接影响回答格式（网页气泡 vs 终端纯文本）与协作方式（daemon 桥接 → 回执协议）。详见 [运行形态自我认知](runtime-form.md)。

## AGT_HOME 读取 bug：所有回调被拒（2026-09-19，commit `523f8ba`）

**现象**：拿 brick（CNB 容器）实测回调 → `{"ok":false,"error":"未配置 callback_token（settings.json）——为安全考虑拒绝所有回调"}`，而该实例明明有 token。

**根因**：`api_callback` 硬编码 `json.load(open(expanduser("~/.agt/settings.json")))`——**CNB 容器的 `AGT_HOME=/workspace/.agent-data/agt`**，`~/.agt` 下没有 settings.json → 读不到 token → **该环境下一切外部回调被安全策略拒掉**（不是路由问题，是凭据读错路径）。

**修复两层**：

| 层 | 内容 |
|---|---|
| 代码 | `server.py` 改走 `config.load_runtime_settings()`（正确路径解析，支持 `AGT_HOME` 与 repo 级覆盖，见 [config-and-models · config_file](../guides/config-and-models.md)），异常兜底回旧路径 |
| 环境兜底 | 容器侧 `ln -s /workspace/.agent-data/agt ~/.agt`（已生效，不等代码升级） |

**实测（当场验证）**：正确 token → `{"ok":true,"queued":true,"inbox_size":1}`（消息真进 brick 队列）；错 token → `{"ok":false,"error":"token 校验失败"}`（鉴权有效）。

**教训**：凡「读全局配置」的代码都要过 `config` 的解析层——`~/.agt` 只是**默认值**，容器 / 桌面版 / repo 级覆盖都可能改道。

## 实战清单（文档第四节，五条）

1. **隧道场景必须走 header**：cpolar 免费版丢 query string；且必须**解析响应体确认 `ok==true`**（只看 HTTP 200 会误判「假成功」）。
2. **AGT_HOME 非默认环境**：见上一节（2026-09-19 已修；旧版本可软链兜底）。
3. **消息驱动 ≠ 工具路由**：回调进上下文、`tool/exec` 只借手脚。
4. **token 等同远程控制权**（可推消息、可执行工具）——别写进 query 或日志。
5. **无内置去重**：需幂等时在 `source` / `text` 里带业务幂等键，或由处理方查台账。

## 相关页面

- [SCNet 异步生产流水线](scnet-async-pipeline.md) — `/api/callback` 的诞生场景（容器产物回传唤醒），端点实现与隧道坑的原始记录
- [多实例组网](../architecture/multi-instance.md) — Agent 侧工具通道（`remote_message` / `remote_ask` / `remote_call_tool`）与工具级直执行
- [用户交互](user-interaction.md) — 消息进 inbox 的唤醒语义、后台通知轮标签
- [/api/status](api-status.md) — 只读状态快照端点
- [config-and-models](../guides/config-and-models.md) — `settings.json` 解析与 repo 级覆盖（本次 bug 的根源面）
