# /ask · 无状态旁路询问指令（agent_ask 同款链路，对象=当前会话自己；2026-10-14 提案 → 2026-11-07 并入统一 ask 消费端）

> src/commands.py `_cmd_ask` + `reg.register("ask", …)`；src/chat.py 帮助命令清单。2026-10-14 用户提案初版（近 N 轮纯文本对 + `tools=[]` 单次裸调）；**2026-11-07 用户裁定改走 [agent_ask 同款链路](../architecture/multi-agent.md#ask-消费端统一临时-agent只读工具可用写类拒绝60s-无答清空工具2026-10-22用户提案)**——拷贝当前会话完整上下文投影，`ask_consumer.answer_ask` 单源临时 agent 回答（只读/召回类工具可自查，写类拒绝，60s 无收敛清空工具逼答），回答显示即弃（不进历史）。

## 职责与用法

把 [ask 消费端统一](../architecture/multi-agent.md#ask-消费端统一临时-agent只读工具可用写类拒绝60s-无答清空工具2026-10-22用户提案)（agent_ask / remote_ask 同源）的语义搬到**用户侧**——对象是当前会话自己：拷贝当前会话完整上下文投影 + 问题 → `ask_consumer.answer_ask` → 临时 agent 回答，回答显示即弃（不进历史）。

```
/ask -prompt 刚才改的渲染缓存键是什么格式？
```

| 参数 | 语义 |
|---|---|
| `-prompt <问题>` | 询问内容（其后所有参数拼为全文） |
| ~~`-turn N`~~ | **已退役（2026-11-07）**——ask 链路自带完整上下文投影，比旧版「近 N 轮 user/answer 纯文本对」更全；传入被忽略并提示 |

## 语义四要点

| 设计点 | 实现（2026-11-07 起） |
|---|---|
| **无状态** | 斜杠命令天然不进 `agent.run`——不产生对话轮、不写 events.jsonl/历史、不跑钩子/审批/注册 |
| **完整投影 + 临时 agent** | `ask_consumer.answer_ask(当前会话, …)` 同一单源：① 拷贝当前会话完整投影（`messages_for_llm()`，含 system/装配段）② new 临时 AskAgent（同款 llm + 工具箱）③ 60s 软预算到点追加「预算已到」+ `tools=[]` 清空工具逼答 |
| **只读工具可用、写类拒绝** | 工具三分类（[classify_tool](../architecture/multi-agent.md#工具三分类srcask_consumerpy-classify_tool)）：只读/召回真执行（可自查文件/记忆/知识库）、写类/执行类拒绝、未知 fail-closed |
| **只回应** | CLI 直接 print；Web 端以系统消息回显——system 事件不落盘，**刷新后消失**；回答不进历史 |

## 改版：并入统一 ask 消费端（2026-11-07，用户裁定）

用户裁定（2026-11-07）：「/ask 的逻辑调整为手动调用 agent_ask 吧」——从「效果同 agent_ask」的**平行实现**变成**真同一条链路**：ask 消费端任何后续改进（工具分类 / 预算 / 降级策略）三入口（agent_ask / remote_ask / /ask）自动同步，少一份要维护的平行代码。

| | 初版（2026-10-14） | 改版（2026-11-07，已推送生效） |
|---|---|---|
| 上下文 | 近 N 轮 user/answer **纯文本对**（`-turn N` 控制，不传=零上下文） | **完整上下文投影**（拷贝当前会话全量投影，含 system/装配段） |
| 回答方式 | `llm.chat` 一次裸调（`tools=[]`、`scene="ask"`） | **ask_consumer.answer_ask**——临时 agent，只读/召回类工具可自查，写类拒绝，60s 无收敛清空工具逼答 |
| `-turn N` | 支持 | **退役**（忽略并提示） |
| 不进历史 | ✓ | ✓（语义保留：不写 events.jsonl、系统消息回显刷新即消失） |
| Web 受理 | daemon 线程旁路 + 系统消息送达 | **原样保留**（改版后可能多轮工具调用，线程旁路更必要） |

实现侧 `_cmd_ask` 从自组 messages + 裸调 LLM 瘦成 `answer_ask` 一跳（帮助文案同步改写为「agent_ask 同款链路」），`scene="ask"` 统计标记与 Web 线程旁路两件基建沿用。

## 关键坑：Web 端同步执行会卡死事件循环

Web 端斜杠命令在 async handler 里**同步执行**——LLM 调用动辄数十秒，会把整个 Web 服务的事件循环卡死（期间所有请求/WS 无响应）。处理：

- 起 **daemon 线程**旁路执行 ask 链路，命令本身立即回「⏳ 已受理」
- 完成后经 `agent._emit`（内部 `call_soon_threadsafe`，线程安全）以 system 事件回显到对话区

## 注册与生效

- `reg.register("ask", _cmd_ask, …)`：简述「无状态旁路询问（agent_ask 同款链路，对象=当前会话）：拷贝完整上下文投影 + 临时 agent 回答，只回应不进历史」+ 用法示例（2026-11-07 改版文案，`-turn` 不再出现）；CLI/Web 两入口走同一 dispatch 表，**自动可用**
- src/chat.py `/help` 命令清单补 `/ask`

## 验证与注意事项

- 函数级 mock 单测 **5 项全过**（2026-11-07 改版）：接线走 answer_ask / asker 标注 / 回答回显 / `-turn` 忽略并提示 / 空询问用法提示（初版 16 项单测随旧实现退役）
- **需 `/restart` 生效**（命令注册在启动期）
- 旁路回答**不进历史**：后续轮次问「刚才 /ask 说了什么」模型是不知道的——要留下来的结论请让主对话自己回答或显式粘贴
- 60s 是软截止：临时 agent 先带只读工具自查，到点才清空工具直答——复杂自查类提问可能比旧版单次裸调慢

## 相关页面

- [ask 消费端统一](../architecture/multi-agent.md#ask-消费端统一临时-agent只读工具可用写类拒绝60s-无答清空工具2026-10-22用户提案) —— **语义单源**：agent_ask / remote_ask / /ask 三入口同一 `answer_ask` 实现（工具三分类 / 60s 软预算）
- [recall_turn · 会话历史轮次召回](recall-tools.md) —— 对照：进历史的检索式召回（含 tools 细节，消费在轮内）
- [用户交互 · 插话机制](user-interaction.md) —— 插话是进历史的；/ask 是不进历史的旁路
- [/pending · 排队用户消息](user-interaction.md#pending--排队用户消息忙时写好下一个需求不打断当前轮2026-11-06用户提案) —— 姊妹斜杠命令：忙时排队下一个需求、当前轮结束后按序开新轮（不打断当前任务）
- [/cmd · 消息框直接执行 shell](cmd-command.md) —— 姊妹斜杠命令（同为命令表注册、/restart 生效）；/cmd 有意不装审批门，/ask 天然无副作用
