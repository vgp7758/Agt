# LLM 客户端韧性 · 分级超时 / 断网等网 / 切换纪元 / 超时诊断 / 端点拒图自愈 / 回退策略落地（src/llm_client.py）

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

## 五、端点拒图自愈：ImageUnsupportedError——vision 卡片配置错不再炸轮（2026-09-30，glm-5.3 实锤，commit 5c852fc）

### 现象与定性

用户切 glm-5.3（glm-official）后，历史轮含图片的会话持续 400：`Error code: 400 - {'error': {'code': '1210', 'message': "messages.content.type 参数非法，取值范围 ['text']"}}`。llm_calls 时间线钉死定性：

```
08:58  t1217 s0  400（vision=False 时代，当时误判为别的问题）
09:27  t1219 s5  success / s6 400    ← 上轮误改卡片 vision=True 后
09:41  t1221 s0  400
09:53  t1222 s0  success / s1 400
10:24  t1223 s0  400                 ← 重启 + 投影全量重刷后依然炸
```

结论：**glm-5.3 的 openai 兼容 chat completions 端点就是不收图**（`content.type` 只允许 `['text']`；视觉是 glm-4v / glm-4.5v 系列的活）。卡片 vision=True 只是让门控放行了图片，端点照样拒收——门控/投影重刷层怎么修都无解。

### 双层修复（随 v0.30.11，commit 5c852fc）

| 层 | 内容 |
|---|---|
| 配置纠正 | models.json `glm-official` vision **改回 False**——门控立即生效：历史图降级文字占位，glm-5.3 文本能力正常用 |
| 框架自愈（防配置错/端点能力未知再炸轮） | llm_client 失败时 `_is_img_reject(e)` 识别拒图特征 → 降 `vision_supported` 位 + 抛 `ImageUnsupportedError`（**跳过回退链**、不记冷却）→ agent 捕获：`invalidate_projection`（历史图降为文字占位重投影）→ **同模型重试**（非换模型），计数上限 3 防异常循环 |

`_is_img_reject` 特征匹配（各 provider 措辞不一，按已实测 + 常见形态）：`1210` + `content.type` / `取值范围 ['text']` / `image_url` + `not supported|unsupported` / `invalid image` + `type`。普通错误不误伤。

### 语义：与回退链的分工

回退链解决「**模型坏了**换一个」；本机制解决「**模型好但不吃图**」——不换模型，换图片形态（图 → `[图片 文件名]` 文字占位 + WebUI 提示「⚠️ xx 端点不支持图片输入，已降级为文字占位」，需要看图由 Agent 委托 vision 子 agent）。用户选 glm-5.3 要的是它的文本能力，不被一张历史图踢去 deepseek。

用户侧表现与全链路图见 [图片输入链路 · 端点拒图自动降级](image-input.md)；`invalidate_projection` 机制见 [上下文引擎 · 手动切模型投影重刷](../architecture/context-engine.md)。

## 六、回退策略 reset 落地：_maybe_reset_to_head 空壳方法补实现（2026-10-09，用户实锤，commit cea4d09）

### 现象与根因

用户实锤：「模型回退策略选的是 reset，但发生过回退之后看上去一直没有重置回来」——回退到后备模型后永远停在那，reset 语义从未生效。

**根因（相当离谱）**：`_maybe_reset_to_head()` 是个**空壳方法**——只有 docstring、没有实现体；而 `chat()` 与 `chat_stream()` **每次请求前都在调它**。空函数调了等于没调：无论用户选 reset 还是 sticky，实际行为都恒等于 sticky（回退后停在后备模型，永不切回）。

### 实现（补上的方法体）

| 条件 | 行为 |
|---|---|
| 策略 = reset 且 当前 `model_name` ≠ `_user_model` | `switch_model(切回用户首选, _user_initiated=False)`——只换 profile，**不动回退链 / 不动切换纪元** |
| 策略 = sticky，或未偏离首选 | 不动作 |
| 切回失败 | 保持现状（warning，不炸轮） |

- **与 provider 冷却的配合**：首选模型刚失败时仍在冷却期（默认 300s，链上继续剔除它）→ 冷却期间照常用后备、不白撞故障端点；**冷却过期后每次调用都先试首选**，成功即恢复——「下一轮重新从首选尝试」的 reset 语义由此成立
- **可观测**：切回时打 `[reset] 回退策略 reset 生效：{旧模型} → 切回用户首选 {新模型}` 一行（日志面板可追溯）

### 验证（真实 config 四场景全绿）

```
① reset 偏离后：deepseek → qwen ✅ 切回首选
② sticky 偏离后：deepseek → deepseek ✅ 保持不动（sticky 语义未破坏）
③ reset 未偏离：qwen → qwen ✅ 幂等无动作
④ 链首=qwen · _user_model=qwen ✅（链结构不被扰动）
```

`/restart` 生效。**教训**：docstring 写满行为的空壳方法比没有这个方法更隐蔽——调用点看起来防线齐全（两个主调用路径都调了），实际什么都不做；审查时「方法被调用」≠「方法生效」，行为承诺必须对着实现体核一遍。

## 七、坏图自愈：_strip_image_parts——400 图解码失败剥图重试（2026-10-16，worker 施工线实锤）

### 事故背景

worker-p_76f6623b（技能中间件施工线）**连续 3 轮起步即断**：请求被端点以 `400 BadRequest: image data 0 failed: Unable to decode the image … corrupted or incomplete` 拒绝 → 回退链 glm-official-flash → glm-official → deepseek 三模型**连锁记冷却**（各 ~300s）→ 施工派发钩子重派 → 撞同一张坏图 → 再冷却……重派循环空转（400 不扣费，代价是通知噪音 + 节奏被 300s 冷却拖慢）。

**定性**：这不是「端点不支持图」（那走本页第五章端点拒图自愈——图是好的、端点能力位错，重刷投影即可降级），而是**图片数据本身坏了**——重刷投影也没用，图还是那张坏图，剥掉才能通。

### 机制（src/llm_client.py，commit 本地 · /restart 生效）

| 环节 | 设计 |
|---|---|
| 触发 | 400 且错误文本含图解码失败特征（`decode the image` / `image data` + `corrupt`/`incomplete`） |
| 动作 | `_strip_image_parts(messages)` 剔除消息里**全部 image_url 部件**（文本部件保留；全图 content 退空串）→ **同模型重试** |
| 三不 | 不记冷却、不回退、不换 token——模型没毛病，坏的是请求里携带的图 |
| 防循环 | 每次 chat 调用最多自愈一次 |
| 可观测 | 自愈时打日志「剔除 N 个 image_url 部件」——**N 就是根因线索**：worker 施工请求本不该带任何图，N>0 即装配源头混入了图，顺 N 追溯 |

单测四例全绿：剔除计数 3 ✓ / 纯文本消息原样保留 ✓ / 全图 content 退空串 ✓ / 触发特征正负例 ✓。

### 注意

- 自愈是**绕过不是根治**：坏图根因（谁往 worker 请求里塞了坏图）未定位——重启后若日志出现自愈行，顺 N 查装配源头
- 与第五章的分工：**拒图 = 能力位错 → 重刷投影降文字；坏图 = 数据坏 → 剥图重试**。触发特征不同（InvalidParameter 图解码 vs 能力拒图），两条路径不重叠、互不抢
- **重启后护航生效（2026-10-16 /restart 后首验）**：worker-p_76f6623b 施工线重启后起步正常（此前连续 3 轮起步即断于坏图 400），推进 4/7 步未见 400 再炸、未见回退链连锁冷却——自愈解套成立。根因（坏图来源）仍未定位，「剔除 N 个」的 N 值线索保留待用

## 八、失败现场存档点：last_failed_messages——doctor 的数据源（2026-10-16，commit 656193a）

回退链整链耗尽、`raise RuntimeError` 之前：

```python
self.last_failed_messages = copy.deepcopy(messages)   # 冻结副本
```

- **深拷贝冻结**：后续任何重试 / 改动不污染现场
- **成功后不清除**：保留最近一次失败现场（[doctor](doctor.md) 随时可查；再次失败自然覆盖）
- 普通单次失败（还有后备可退）不写——只有整链耗尽才产生现场
- 消费端：doctor 的 req_inspect / req_probe / req_patch 全部读它——**上两章 L1 自愈是绕过，doctor 是根治线**：定位坏数据所在轮次/步骤、修在源头（存档消息 + 当前轮 toollog 结果）、恢复原流程（顺带清 provider 冷却）
- 单测：真实 LLMClient + patched `_chat_inner` 验证冻结性与深拷贝隔离（test_doctor_tools.py）

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
- [doctor](doctor.md) — BadRequest 诊断修复 Agent：消费本页存档点 `last_failed_messages`；L1 自愈（本页五/七章）的根治搭档

