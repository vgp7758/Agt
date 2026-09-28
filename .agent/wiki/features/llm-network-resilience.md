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

## 三、轮进行中用户切换模型：_user_switch_epoch——回退循环立刻让位新链首（2026-09-28，用户实锤，commit b03fd80）

### 现象与根因

`_main_` 轮进行中在 WebUI 下拉框切了模型，进行中轮的回退链索引不重置——下一请求没有立刻应用切换后首位的模型。根因：`_chat_with_fallback` 持有**调用开头的链快照**（局部 `chain`），而 sticky 挂在 `self.model_name` 上；`switch_model` 虽重建实例链，旧循环对切换无感知，两种竞态：

- 新模型**不在**旧链快照 → `_advance` 的 `chain.index(...)` 抛 ValueError → `RuntimeError: 当前模型 X 不在回退链中` **直接炸本次调用**（测试真实复现）
- 新模型**恰在**旧链 → `_advance` 照常推进 `switch_model(next)` → **踩掉刚切的模型**（下一个请求又回到旧行）

### 机制：纪元四件套

1. `__init__`：`self._user_switch_epoch = 0`
2. `switch_model(_user_initiated=True)`（WebUI 下拉框 / /model）：epoch += 1——回退链内部的切换不动纪元
3. `_advance` 开头：纪元 ≠ 本次调用开头快照 → **不推进直接 return**（model_name 已是新链首，旧链 index 会炸/会踩）
4. 循环顶部：纪元变了 → 重建链（react override 优先，否则实例链）+ `tried` 清零，从新链首重试——旧失败成员的 provider 冷却仍在，`_cooled` 照常跳过

### 验证

修复前：切换场景 `RuntimeError` 复现；修复后：尝试顺序 `[proxy → glm-official]`——新链首立刻接管、成功返回；无切换对照组 proxy → deepseek 普通回退行为完全不变。

## 四、超时阶段诊断 + connect/write 可配（2026-09-28，用户问诊「34.7s 就 APITimeoutError」，commit 83c732d）

## 四、超时阶段诊断 + connect/write 可配（2026-09-28，用户问诊「34.7s 就 APITimeoutError」，commit 83c732d）

### 现象辨析：34.7s 超时 ≠ read 240s

用户问诊：日志显示 34.7s 就 `APITimeoutError`，read 不是 240s 吗？——**超时不是一个数，是四段各自计时**（httpx.Timeout）：connect=10 / read=240 / write=30 / pool=10。34.7s 撞的是 connect / write / pool 之一，而非 read。且 **SDK `max_retries=1`**——单次 `chat` 内部最多两次尝试，时间可累计。34.7s 的常见构成：

- **write 卡**（大请求体最常见）：上下文 324K 字符 + tools schema 3.6 万 token ≈ 数百 KB~1MB 请求体，家庭网络上行慢时**发送请求体**卡满 30s → 归零重试再来一次 ≈ 30~60s
- **connect 卡**：10s × 2 次 + backoff ≈ 20~21s
- 混合：connect 慢几秒 + write 30s ≈ 35s（与 34.7s 吻合）

旧日志盲区：只记 `APITimeoutError` 类名，看不到阶段——「明明没到 240s」的困惑即此。

### `_timeout_stage()`：沿异常链识别阶段

httpx 的阶段信息在 `ConnectTimeout / WriteTimeout / PoolTimeout / ReadTimeout` 四个子类上，但被 openai SDK 包装——`_timeout_stage(e)` 沿 `__cause__ / __context__` 链回找子类名并翻译成中文阶段（连不上/发不出/等不到/等不到响应）。消费两处：

- **日志 warning**：`provider xxx 失败，进入 300s 冷却：APITimeoutError（超时阶段：WriteTimeout/发送请求体）`
- **last_failures**：msg 尾附 `[WriteTimeout/发送请求体]`——回退链中断后充值入口/失败摘要可辨阶段

实测：构造四类子类 + cause 链包装全对；无 cause 链（阶段未知）不误报、留空。

### connect / write 可配（read 三级取值的同款三级）

```
profile.connect_timeout / write_timeout（models.json 模型卡片）
  > settings llm_connect_timeout / llm_write_timeout
    > 默认 10 / 30
```

`_openai_client()` 改传完整四段：`httpx.Timeout(connect=_ct, read=_rt, write=_wt, pool=10.0)`。实测 profile 覆盖生效：`Timeout(connect=25.0, read=240.0, write=90.0, pool=10.0)`。家庭网络上行慢 / 端点抖，给该 provider 配大即可；`/restart` 生效。配置键详见 [配置体系 · 网络韧性配置](../guides/config-and-models.md#网络韧性配置分级超时readconnectwrite--断网等网2026-09-26--09-28-扩)。

### 顺带：`_extract_url()` 抽函数

错误消息内嵌链接提取（flatkey 403 的 `Add credits at https://...`——充值入口三级来源的第一级，见 [配置体系 · 回退链中断一键充值](../guides/config-and-models.md#回退链中断一键充值preset-recharge_url--401403404-纳入回退2026-09-08用户提案)）抽成独立函数 `_extract_url(msg)`，供 last_failures 的 `url` 字段复用。

### 排障口诀

`/restart` 后等下一次 timeout 看阶段标注：**WriteTimeout** → 上行慢/请求体大 → 配 `write_timeout: 90`；**ConnectTimeout** → 网络抖 → 配 `connect_timeout: 25`（或靠[断网检测](#二断网检测与等网重试commit-c5b57cd)等网重试）；**PoolTimeout** → 并发抢连接池，另一类问题。

## 配置键（详见 [配置体系](../guides/config-and-models.md)）

配置键速查（`connect`/`write` 为 2026-09-28 新增，commit 83c732d；详见 [配置体系 · 网络韧性配置](../guides/config-and-models.md)）：

| 键 | 位置 | 说明 |
|---|---|---|
| `read_timeout` | models.json 模型卡片 | 读超时秒数；快端点配小，本地慢模型配大 |
| `llm_read_timeout` | settings.json | 全局默认读超时（默认 240） |
| `connect_timeout` | models.json 模型卡片 | 连接超时秒数（默认 10）；端点抖/网络慢配大（如 25） |
| `write_timeout` | models.json 模型卡片 | 发送请求体超时秒数（默认 30）；大请求体+上行慢配大（如 90） |
| `llm_connect_timeout` | settings.json | 全局默认连接超时 |
| `llm_write_timeout` | settings.json | 全局默认写超时 |
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
