# LLM 网络韧性 · 分级超时 + 断网检测与等网重试（src/llm_client.py，2026-09-26，用户提案）

## 职责与背景

家庭网络波动频繁时的两类故障，同日双修（2026-09-26）：

1. **请求 hang 死**：网卡一下时正在进行的 API 请求十分钟没动静——openai SDK 默认 `timeout=600s`，TCP 未断、对端不响应就不报错，太钝
2. **断网空转回退链**：真断网时回退链上一个一个往后试毫无意义——换哪个 provider 都卡，还把全链打进冷却

真实调用链：`chat → _chat_with_fallback`（`chat_stream` 是死代码，无调用方——一处集成全覆盖）。

## 一、分级超时：connect=10s / read=240s（commit c2b3d60）

`_openai_client()` 构造 SDK 客户端时传分级超时：

| 档位 | 默认 | 语义 |
|---|---|---|
| connect | **10s** | 连不上（断网/路由抖）秒级发现 → 快速失败进回退链 |
| read | **240s** | **流式 = 相邻 chunk 最大间隔**（防"卡一下后无限 hang"）；非流式 = 响应体读窗 |
| write / pool | 30s / 10s | 上行 / 池等待 |

- **read 三级取值**：模型卡片 `read_timeout`（models.json，秒）> settings `llm_read_timeout` > 240
- **为什么默认 240**：长思考模型（DeepSeek 类）思考期 2-3 分钟不发 chunk，太短会误杀正常思考
- **max_retries=1**：外层已有回退链 + 全链冷却，SDK 内层少重试一拍，失败更快浮上来
- switch_model / token 轮换重建客户端时超时自动跟随 profile；`/restart` 生效

## 二、断网检测与等网重试（commit c5b57cd）

### 判定（保守，两层条件同时满足才触发）

1. 失败为**网络类**：`APITimeoutError` / `APIConnectionError`（限流、鉴权、404 等照走原逻辑）
2. **锚点探测确认本机断网**：`_probe_anchors()` TCP connect 三锚点（`223.5.5.5` / `www.baidu.com` / `1.1.1.1`:443）**全部不通**才算——任一通视为网络正常（provider 自己挂了不会误触发白等）；判定后 30s 置信窗口（`_net_down_until`）内复用，不每个 provider 失败都探测一轮

### 触发后的行为

```
[网络] 确认本机断网（锚点全不通），暂停回退、等网恢复（本次调用预算 300s）…
   （_wait_net_recover：每 15s 重探锚点，日志面板可见"等网"）
[网络] 已恢复（等待 45s），重试 glm-5.3
```

- **恢复 → 重试同一个 provider**：不记冷却、不 `_advance`——链上其它成员不被无谓打进冷却
- **预算耗尽**：本次调用累计等网（`net_waited`）超过 `net_wait_max`（settings，默认 300s，`<=0` 关闭机制）→ 落回原逻辑（记冷却、走回退链）

## 配置键（详见 [配置体系](../guides/config-and-models.md)）

| 键 | 位置 | 说明 |
|---|---|---|
| `read_timeout` | models.json 模型卡片 | 读超时秒数；快端点配小，本地慢模型配大 |
| `llm_read_timeout` | settings.json | 全局默认读超时（默认 240） |
| `net_wait_max` | settings.json | 断网等待预算（默认 300；0 = 关闭断网等待） |

## 验证

- **超时**（子进程新代码）：默认 read=240 / connect=10 ✓；模型卡片覆盖 60 ✓；switch_model 跟随 ✓；断网实测（不可路由地址、空回退链）**21.3s 抛超时 vs 旧 600s——改善 28 倍**
- **断网等待**（mock 探测函数）：网络正常（2/3 锚点通）不误判 ✓；锚点全不通判定断网 ✓；等网循环预算耗尽返回 ✓；端到端 network 失败 + 断网 → 先等网（预算）才报错、**调用次数 = 1**（等网期间没在回退链上瞎试）✓

## 注意事项

- 本地 CPU 慢模型（非流式长输出）可能撞 240s 读窗——给该 profile 配大（如 600）
- `_classify_err` 把网络类失败归"其它"——与 [回退链充值按钮](../features/bubble-interaction.md) 的 quota/auth 归类不冲突
- 修改 settings 键后 `/restart` 生效

## 相关页面

- [配置体系与模型调优](../guides/config-and-models.md) — settings / 模型卡片键、回退链与失败归类
- [多 Agent 体系](../architecture/multi-agent.md) — 回退链职责分离（react 认 .yml / 非 react 认 settings）
- [运维与排障](../guides/ops.md) — llm_calls 观测、网络类故障定位
- [agent_watch](agent-watch.md) — 同日网络韧性批：10061 静默跳过
