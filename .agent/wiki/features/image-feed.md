# image_feed · 实时画面装配段（每步最新帧 + 上一步对比帧；挂载点在 tool result content 数组）

## 职责

让 Agent（典型：子 Agent desktop-operator）**每步实时看到画面源**：assembly 里声明 `image_feed:` 动作项，每次装配都从画面服务取**最新帧** → base64 组装成 `image_url` 块并入**末条 tool result 的 content 数组**（2026-10-11 前为投影末尾的 user 消息）。与 [services 依赖声明](../architecture/multi-agent.md)（yml 声明依赖服务、实例化幂等拉起）配合 = 声明自包含：拉起 Agent 即自带眼睛。

## 声明形态（首个实例 desktop-operator）

### 声明形态（首个实例 desktop-operator）

```yaml
# .agent/agents/desktop-operator/desktop-operator.yml（节选）
assembly:
  - system
  - file: .agent/agents/desktop-operator/desktop-operator.md
  - user_message
  - steps
  - image_feed: http://127.0.0.1:8765/frame   # 必须在 steps 之后（区3 尾部）
services:
  - desktop-frame: python .agent/agents/desktop-operator/tools/image_feed_poc.py 8765
```

> services 命令 2026-10-16 起指 agent 目录真身（`_ensure_agent_services` 按 **workspace 相对路径**执行命令；旧 `tools/…` 写法靠 workspace 根巧合同名文件才能跑、**播种必坏**，commit 0c9033e，详见 [multi-agent · services 声明 · 路径坑](../architecture/multi-agent.md)）。

## 求值语义（src/session.py，`kind == "image_feed"`）

| 维度 | 行为 |
|---|---|
| src | `http(s)://`（内存流零落盘，推荐）或文件路径（workspace 相对） |
| 视觉门控 | 非视觉模型（`vision_supported=False`）整段**静默跳过** |
| 失联/空帧 | 降级一行文字（`[image_feed 不可用：...]` / `[image_feed 空帧：...]`），**不炸轮** |
| 哨兵 | 命中返回 `@@IMGFEED@@data:<mime>;base64,<b64>@@`——桶收集处抽走，不进文本桶 |

## 位置约束与图片通道（tail_images，src/session.py 装配）

- **必须声明在 steps 段之后**（区3 尾部）：段产出的哨兵由 merge 桶抽取进 `tail_images`；写进 system run 的会被 warning 跳过（「image_feed 项应声明在 steps 段之后」）
- base64 **不进统计文本桶**（token 统计不含图）
- 组装层把 data URL 变 `image_url` 块，图块前统一附**帧前说明块** `[image_feed · 实时画面 · HH:MM:SS]`；挂载点**三级优先**（2026-10-11 用户裁定迁移，见下节）：① **最后一条 tool result** 的 content 数组（与工具结果 / system-reminder 同条）→ ② 末条 user content 数组化追加（无 tool result 的纯文本步）→ ③ 独立 user 消息承载（末条 assistant）
- **瞬态语义**：组装层注入，不落 events.jsonl / step 存档；每步求值 = 每步最新帧

## 帧前说明块：[image_feed · 实时画面 · HH:MM:SS]（2026-10-08，用户裁定「带上吧」）

image_url 块裸注入时模型可能把这帧误当成自己历史操作的截图——注入前统一补一个**文字说明块**（src/session.py 组装层 tail_images 通道，2026-10-08 落地）：

```json
{"role": "user", "content": [
  {"type": "text", "text": "[image_feed · 实时画面 · 10:52:31]"},
  {"type": "image_url", "image_url": {"url": "data:image/png;base64,..."}}
]}
```

- **三条承载路径同改**（2026-10-11 起：末条 tool result → 末条 user → 独立 user 消息，见下节）：说明块恒紧邻在图块**之前**，顺序 = 原内容（保序在前）+ 说明块 + 图块
- **浅拷贝纪律**：所有分支都 `{**原消息}` 复制后重组 content——绝不就地改共享引用（tool result 可能来自冻结渲染缓存，末条 user 被投影多段共享）
- 说明块带**捕获时刻**（`time.strftime('%H:%M:%S')`），模型可区分新旧帧、明确知道这是 image_feed 喂的实时画面
- token 代价一行文字可忽略；口径与失联/空帧降级行（`[image_feed 不可用：...]`）一致
- 瞬态语义不变：不落 events.jsonl / step 存档

## 挂载点迁移：画面并入末条 tool result 的 content 数组（2026-10-11，用户提案，commit fa28d13）

**动机（用户提案）**：原挂载点（末条 user / 独立 user 消息）在有工具结果的步里把画面挂在**工具结果之后的新消息**上——模型读到的是「工具结果 → 又一个 user（画面）」，叙事被切成两段；且与同轮 system-reminder 分居两条消息。用户裁定：「投影到 tool result 里吧，和 system-reminder 在同一条 message 的 content 数组里」——一条消息读完「调用了 X → 结果 + reminder + 当前画面」。

**三级挂载优先级（src/session.py `messages_for_llm` 装配尾部，image_feed 块）**：

| 优先级 | 落点 | 适用 | 分段统计文案 |
|---|---|---|---|
| ① | **最后一条 tool 消息**（content 数组追加） | 有工具调用的步（绝大多数） | 并入末条 tool result（瞬态·不落档） |
| ② | 末条 user（content 数组化追加） | 无 tool result 的纯文本步 | 末条追加image_url（瞬态·不落档） |
| ③ | 独立 user 消息 | 末条 assistant（或 msgs 为空）——部分端点不允许 assistant 挂图 | 同上 |

实现要点（①）：

- 倒序扫 `msgs` 找 `role == "tool"` 的最后一条；命中即改写该条，未命中回落 ②/③
- **浅拷贝**：`_t2 = {**msgs[_ti]}`——tool result 可能来自冻结渲染缓存（施工缓冲 / 分段定型渲染），**绝不就地改共享引用**
- content **三态归一**：`str` → `[{"type":"text","text":原内容}]`（空串 → 空列表）；`list`（工具结果带图时 `_project_imgs` 已转块数组）→ 原样复制；其它 → 空
- 拼接：`原 blocks + [帧前说明块] + [image_url 块…]`

**实测投影形态**（真 Session 全装配路径）：

```json
{"role": "tool", "tool_call_id": "a-1",
 "content": [
   {"type": "text", "text": "点击执行完毕（远程）\n<system-reminder>…git_diff…</system-reminder>"},
   {"type": "text", "text": "[image_feed · 实时画面 · 18:0x:xx]"},
   {"type": "image_url", "image_url": {"url": "data:image/png;base64,…"}}
 ]}
```

瞬态语义不变（组装层注入，不落 events.jsonl / step 存档）；byte-stable 影响面 = 当前轮 steps 段（本就在未命中区）。

### 后记：kimi「content 必须数组」记忆实测不成立——五形态探针全 200（2026-10-11）

用户提约束「kimi 必须按 content 数组样式发送，不支持 content 字符串」。当日对 kimi 端点五形态探针实测，**全部 200 通过**：

| 形态 | 结果 |
|---|---|
| user content **字符串** | ✅ |
| user content text 数组 | ✅ |
| tool content 字符串 | ✅ |
| tool content text 数组 | ✅ |
| tool content 数组 + image_url | ✅ |

结论：该约束可能来自**旧版 kimi API 或特定端点**；不据此建 per-model 分支，统一走「能数组则数组、否则字符串」的形态归一（即上表三态归一逻辑）。日后若真遇 400 再按报错原文定向处理；本轮一并校准了此前按该记忆写的 provider 差异相关注释。详见 [LLM 客户端韧性](llm-network-resilience.md)。

## 双帧对比：上一步帧挂倒数第二条 tool result（2026-10-11，随 v0.34.6 发布）

**动机**：桌面操作类 Agent 要判断「我上一步的动作有没有生效」（窗口切了没、按钮点了没）——只给最新帧时模型无从对比，要么反复截图（多花工具调用）、要么凭记忆猜。双帧对比让每步投影自带「上一帧 vs 本帧」：视觉模型直接对比两帧即可判断画面是否变化。

**机制（src/session.py 装配尾部，`tail_frames` 通道）**：

| 帧 | 挂载点 | 说明块 |
|---|---|---|
| 本步最新帧 | 最后一条 tool result（三级优先级 ①，见上节） | `[image_feed · 实时画面 · HH:MM:SS]` |
| 上一步帧（`_imgfeed_prev` 跨组装缓存） | **倒数第二条** tool result（`_tool_idx[-2]`） | `[image_feed · 上一步画面 · HH:MM:SS（对比帧）]` |

- **同消息幂等重挂**：倒数第二条 tool result 正是上一步组装时挂最新帧的那条消息——每步投影整体重建，上一步帧天然回到原位，叙事连贯（上一步画面 → 动作 → 本步画面）
- **帧序**：`tail_frames` 按 ts 升序排序，末位 = 本步最新帧；帧哨兵带抓取时刻 `@@IMGFEED@@<ts>@@data:<mime>;base64,<b64>@@`（ts 供排序与标注）
- **字节级相同 → 文本替代**：两帧 data URL 相等时对比帧不挂图，只写一行文字 `[image_feed · 上一步画面：与本帧字节级相同——画面无变化]`——不白花一份图片 token
- **边界**：无第二挂载点（首步 / 上一步是纯文本步，`len(_tool_idx) < 2`）则跳过对比帧；分段统计新增 `image_feed(上步画面·双帧对比)` 段（瞬态·不落档）
- **状态**：`self._imgfeed_prev` 记忆上一步帧 `(data_url, ts)`，本步装配末尾更新为最新帧——跨组装缓存，不落存档

**代价**：画面有变化时每步多一份图片 token（对比帧）；无变化时只多一行文字。换来模型可自主判断「动作是否生效」，省掉来回截图的工具调用。随 v0.34.6 发布。

## 顺手修复：工具结果数组化后的 content += str TypeError（2026-10-11）

**炸点组合**：视觉模型 × 工具结果文本带 `<img>` 标签（`_project_imgs` 把 content 转成**块数组**）× 施工模式 recent-file 内嵌（`content += self._rf_inline_block(snap)`）→ `TypeError: can only concatenate list (not "str") to list`——该投影路径**必炸**，此前无测试覆盖（三个条件同时成立的实战形态稀缺，但真实存在）。

**修复（src/session.py 两处构造点，数组感知追加）**：

```python
_rf_blk = self._rf_inline_block(snap)
if isinstance(content, list):   # _project_imgs 转数组（工具结果带图）——append text 块
    content.append({"type": "text", "text": _rf_blk})
else:
    content += _rf_blk
```

- 命中点：`_constr_step_msgs`（施工投影缓冲的定型渲染单元）+ `_steps_to_messages` 的 `constr` 分支（历史/档位渲染路径）——两处必须同改，否则半通
- 口径等价：数组形态下快照块作为**独立 text 块**保序追加在结果文本块之后（不合并、不改快照内容，`_RE_RF_BLOCK` 剥离口径照旧命中）
- 非施工模式不受影响（走独立 `recent_file` 段，不内嵌）；详见 [image-input · 投影返回类型契约](image-input.md)

**同类风险清单**（凡在 tool result content 上追加内容的代码都须先判类型）：施工快照内嵌、审批/越界提示行、钩子旁注、`image_feed` 挂载本身（已三态归一）。

## 画面服务：tools/image_feed_poc.py

- `python tools/image_feed_poc.py [port]`（默认 8765，第二参 = 最大宽度）
- PIL.ImageGrab 抓全屏 → 缩放（默认宽 **1280** 控 token）→ JPEG q70 → 内存缓存；`GET /frame` 返回缓存帧（**0.5s 节流**防高频抓屏）；抓屏失败转 503
- 零落盘：帧只驻内存；依赖 Pillow

## 首个实战：desktop-operator（2026-10-06 深夜四连发）

- 三件套实测全过：看帧（image_feed 注入）/ 工具（键鼠模拟/剪切板/窗口切换）/ 剪切板
- **崩溃治理闭环**：desktop-frame 曾 rc=1 挂掉（无输出，疑似抓屏偶发异常）→ 服务化前忘了手工 start 就「睁眼瞎」；services 声明后派活自动拉起（幂等），挂了有 on_exit_wake 通知兜底
- model glm-official-flash（vision）；`tools: ""`——只带目录内专属工具

## 进包播种：新 repo 开箱即得（2026-10-06 · 二，commit 789d88e）

desktop-operator 四件（yml + md 人设 + desktop_tools.py + image_feed_poc.py）进 bundled 播种源 `src/agents/desktop-operator/`，随 wheel 分发；seed 改递归拷贝让 `tools/` 随行，services 命令同步自包含化（指向 `.agent/agents/desktop-operator/tools/`）。通用环境两个前提：**视觉模型**（非 vision 则本段门控静默跳过——agent 可起但看不到桌面）+ `pip install pyautogui pyperclip`（缺依赖仅 warning 空工具箱，不炸实例化）。详见 [workspace 播种](workspace-seeding.md)。

## 编辑器支持

agents 管理页类型下拉 `ACT_TYPES` 增 `image_feed`（src/static/agents.html）——装配动作可直接选型，值 = 画面服务 URL。见 [Agent 管理页](agents-admin.md)。

## 姊妹特性：bg_services watch_tail——服务日志尾部每步投影（2026-10-07，commit 9e1d523）

bg_services 装配段同日增 `start_service(watch_tail=N)`：服务 stdout 日志尾部 N 行随每步投影注入——与本段同属「每步实况注入」家族。分工：本段走**画面**通道（tail_images，vision 投影，看帧服务的画面）；watch_tail 走**文本**通道（零协议，stdout 日志即状态，看服务运行实况）——帧服务两者可同用。详见 [background-scheduler · watch_tail](background-scheduler.md)。

2026-10-08 家族再添协议成员：REPL 协议服务 = 每步自动发 `/status` 取**前 N 行协议摘要**（N = `watch_tail`，**5 行封顶**；stdout 只归协议响应、日志写文件），文本通道的第二种形态。判定两条路（或关系）：**`repl:` 前缀命名**，或**交互自动判定**——任何服务被 `service_stdin` 发过一次参数（`repl_seen`）即升级为 repl 语义，此时 watch_tail 的日志尾部模式自动让位。详见 [background-scheduler · repl: 协议服务](background-scheduler.md)。

## 同投影尾部家族：生成图自动可视——工具产物新增图伪造 read_file 对（2026-10-07，commit 218a757）

同投影尾部家族再添一员（2026-10-07，commit 218a757）：工具 changed_files 里的新增图片（png/jpg/jpeg/webp/gif，单图 ≤1.5MB、一次 ≤4 张）→ 投影尾部伪造「read_file 调用+结果(image_url)」对——视觉模型"以为"自己读过图，省一次真实调用。与本段同在投影尾部注入、同 `vision_supported` 门控；分工：本特性**事件驱动**（本轮有新图才有）、瞬态不落档，image_feed **每步恒注**（最新帧）。详见 [生成图自动可视](image-autoview.md)。

## 注意事项

- 每步一帧有 token 代价：服务端 1280 宽 + JPEG q70 是经济档，更高清改启动第二参（MAX_W）
- 端点挂了不炸轮但「看不见」——服务起停交给 [services 声明](../architecture/multi-agent.md) 管
- 非 vision 模型声明了也白声明（静默跳过）——选视觉模型（desktop-operator 用 glm-official-flash）

## 相关页面

- [services 依赖声明](../architecture/multi-agent.md) —— 服务生命周期随声明走
- [Agent 管理页](agents-admin.md) —— ACT_TYPES 编辑器支持
- [图片输入链路](image-input.md) —— `_project_imgs` 返回类型契约（str | 块数组）与数组感知消费
- [上下文引擎 · 施工模式投影](../architecture/context-engine.md) —— 施工期 recent-file 内嵌块的数组感知追加
- [生成图自动可视](image-autoview.md) —— 同投影尾部家族（工具产物新增图）
