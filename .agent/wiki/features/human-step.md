# human_step · 人在环步骤工具（Agent 指挥人类操作 GUI/物理界面）

> src/survey_tools.py（`get_human_step_tools` / `_emit_human_step` / `resolve_human_step`）+ src/server.py + src/chat.py + src/static/index.html。2026-09-29 用户提案，commit `02cb2bd`。

## 职责与动机

用户提案（2026-09-29）：Agent 的愿望是「一口气把任务做完」，但图形界面软件是**为人设计的**——Agent 缺 GUI 工具时，旧路径只能是「Agent answer 告诉人类怎么做 → 人类照做 → 把结果再说一遍」。这个过程本质上是 **ReAct 的一环**（人成了 LLM 的手脚），而不是一次对话轮换：拆成两轮 answer/等待既多烧 token 又丢推理现场。

`human_step` 把它工具化：**一次工具调用 = 阻塞式「指挥人类做一个操作 + 收取反馈文本」，Agent 在同一轮继续推理**。

```python
human_step(
  instruction="你现在需要扫码完成登录千牛，然后告诉我登录后的页面布局",
  expect="登录后的页面布局",
)
# → WebUI 渲染引导卡片，Agent 阻塞等你操作
# → 你完成后在卡片文本框里描述，提交 → 工具返回「人类反馈：\n…」
# → Agent 同一轮继续（不是 answer 后等新轮）
```

## 签名与语义

| 参数 | 语义 |
|---|---|
| `instruction` | 给人类的操作指引——**具体、有步骤感**（如「扫码完成登录 xxx」「勾选最下方的同意协议，然后点击安装按钮」） |
| `expect` | 期望人类回报什么（如「登录后的页面布局」「安装进度」）——帮人类知道该反馈什么信息 |

- **返回**：`人类反馈：\n<文本>`（用户提交的原文）
- **超时**：`[超时] 30 分钟内未收到人类反馈——操作可能未完成，请决定重试或改变策略`（`threading.Event.wait(timeout=1800)`——人类操作比程序慢得多，但 Agent 不能无限挂死）
- **阻塞期**：工具自身阻塞（`Event`），不经过 subprocess，与 [TOOL_TIMEOUT](run-python.md) 的计时无关

## 四层实现

| 层 | 位置 | 内容 |
|---|---|---|
| 工具 | src/survey_tools.py L131-181 | `get_human_step_tools(agent)` 返回 `[Tool(human_step, param_schemas={instruction, expect})]`；`_emit_human_step(agent, event_type)` 从 `session.extra_state["_pending_human_step"]` 读回并发 `human_step_pending` 事件；`resolve_human_step(agent, sid, text)` 置 result + `event.set()` 解除阻塞（server 侧调用）；**无阻塞线程**（重启后恢复场景）则 pop pending 兜底 |
| 注册 | src/chat.py L240-241 | `_reg(get_human_step_tools(agent), "人在环")`——工具箱独立分组「人在环」 |
| WS | src/server.py L2402-2406 | `action: "human_step_response"` → `resolve_human_step(agent, id, text)`（与 `approval_response` / `survey_decision` 同款同址） |
| 前端 | src/static/index.html | `human_step_pending` → `renderHumanStepCard(id, instruction, expect)`；`human_step_resolved` → `resolveHumanStepCard(id, text)`；`submitHumanStep(sid)` 读 `#hs_text_<sid>` 发 WS action（卡片 DOM `#humanstep_<sid>`） |

## 提交后只读化：控件移除 + 回报文字渲染（2026-10，用户提案）

用户提案（2026-10）：提交后的卡片是「半残留」观感——旧实现 `resolveHumanStepCard` 只把 `textarea/button` 置 `disabled + opacity 0.5`：按钮还看得见、文本框还占着位，像没提交干净。

新形态（src/static/index.html，前端改动**刷新页面即生效**）：

| 项 | 行为 |
|---|---|
| 控件 | 提交后 `textarea / button / input` **直接移除**（不是灰掉） |
| 卡片 | 边框转绿 + 浅绿底（保留） |
| 回执 | `✅ 已完成，Agent 继续执行中` + `📌 我的回报：<原文>` 绿色文字卡 |
| 文本 | `esc()` 转义 + `white-space:pre-wrap` 保留换行 |
| 幂等 | 重复回执（刷新补发等）不叠加文字卡 |

同轮顺带：survey 卡片同款只读化——提交后答案摘要保留可回看，见 [ask_user · 提交后答案摘要](ask-user.md)。

## 与既有交互工具的三角分工

| 工具 | 形态 | 用途 |
|---|---|---|
| [`ask_user`](ask-user.md) | 结构化问卷（题目 + 选项，`survey_pending`；2026-10-05 起入口宽容归一防坏形态透传） | 让用户**选 / 填**——Agent 已知道要问什么 |
| **`human_step`** | 自由指令 + 自由文本反馈 | **指挥人类操作 GUI / 物理世界**，取其对现场的观察 |
| 审批卡片（[tool-approval](tool-approval.md)） | 是 / 否 放行（安全门） | 敏感操作的**授权**，不是任务步骤 |

三者的共同底座：都走 WS action 通道 + `threading.Event` 阻塞 + **pending 落 `session.extra_state`**（同款模式，互相参照修复）。差异点：ask_user 无超时、human_step 30 分钟超时、审批卡随 pending 审批重发机制刷新恢复。

## 注意事项

- **生效方式**：工具层 `/restart`（新工具进工具箱）+ 前端 Ctrl+F5
- **CLI / 无前端连接**：事件无接收端 → 只能靠 30 分钟超时兜底（Agent 收到超时文本自行决策）
- **刷新 WebUI 会丢卡片**：`_pending_human_step` 已落 extra_state（恢复地基已备），但 `current_history` 的补发目前只覆盖审批卡（见 [tool-approval · 刷新恢复](tool-approval.md)）——刷新后 human_step 卡片暂不重发，需重新触发或等超时
- 反馈文本原样进工具结果，但会随投影层的步距衰减规则被压缩

## 相关页面

- [ask_user · 结构化问卷](ask-user.md) — 同文件同阻塞模式（结构化问卷 vs 自由指令；入口宽容归一）
- [工具执行审批](tool-approval.md) — 同款 Event 阻塞 + WS action + pending 持久化（安全门 vs 任务步骤）
- [用户交互](user-interaction.md) — WS action 通道与插话机制
- [气泡交互](bubble-interaction.md) — 卡片渲染所在的气泡区
- [多 Agent 体系](../architecture/multi-agent.md) — 子 Agent 同样带 human_step（"人在环"组随工具箱装配）

