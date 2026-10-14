# /ask · 无状态旁路询问指令（同 agent_ask，对象=当前会话自己；2026-10-14，用户提案）

> src/commands.py `_cmd_ask` + `reg.register("ask", …)`；src/chat.py 帮助命令清单。用户提案：「加一个 /ask 指令，效果和 agent_ask 一样，无状态询问，只回应不进历史，带参数 -turn 表示需要保留近几轮上下文和 -prompt 发送询问内容」。

## 职责与用法

把 [agent_ask](../architecture/multi-agent.md#agent_ask-不装配工具克隆历史纯对话问答2026-10-08用户提案commit-16c1708) 的无状态语义搬到**用户侧**——对象是当前会话自己：直接用（近 N 轮）上下文 + 问题调 LLM 一次，回答显示即弃。

```
/ask -prompt 这个正则什么意思？              # 零上下文纯问
/ask -turn 3 -prompt 刚才改的缓存键格式？     # 附最近 3 轮对话上下文
```

| 参数 | 语义 |
|---|---|
| `-prompt <问题>` | 询问内容（其后所有参数拼为全文） |
| `-turn N` | 附最近 N 轮对话上下文（user/answer 纯文本对）；**不传 = 零上下文纯问**（`--turn` 变体同认） |

## 语义四要点

| 设计点 | 实现 |
|---|---|
| **无状态** | 斜杠命令天然不进 `agent.run`——不产生对话轮、不写 events.jsonl/历史、不触发工具循环 |
| **-turn N** | 只取近 N 轮 `(user_message, answer)` 纯文本对——旁路要的是轻量语境，**不走完整投影**（分档折叠/工具调用细节是主循环的事） |
| **只回应** | CLI 直接 print；Web 端以系统消息回显——system 事件不落盘，**刷新后消失** |
| **不装工具** | `llm.chat(msgs, tools=[], scene="ask")`——agent_ask 去工具化同款先例（无执行环境，装了工具只会诱发空转）；`scene="ask"` 让统计可辨 |

## 关键坑：Web 端同步执行会卡死事件循环

Web 端斜杠命令在 async handler 里**同步执行**——LLM 调用动辄数十秒，会把整个 Web 服务的事件循环卡死（期间所有请求/WS 无响应）。处理：

- 起 **daemon 线程**旁路执行 LLM 调用，命令本身立即回「⏳ 已受理」
- 完成后经 `agent._emit`（内部 `call_soon_threadsafe`，线程安全）以 system 事件回显到对话区

## 注册与生效

- `reg.register("ask", _cmd_ask, …)`：简述「无状态旁路询问（同 agent_ask，对象=当前会话）：只回应不进历史、不触发工具循环」+ 两行用法示例；CLI/Web 两入口走同一 dispatch 表，**自动可用**
- src/chat.py `/help` 命令清单补 `/ask`

## 验证与注意事项

- 函数级 mock 单测 **16 项全过**：参数解析（`--` 变体 / turn 非法值 / 空轮次）× 近 N 轮文本对组装 × `tools=[]` × scene × CLI print × Web 线程 + emit 回显 × 异常兜底
- **需 `/restart` 生效**（命令注册在启动期）
- 旁路回答**不进历史**：后续轮次问「刚才 /ask 说了什么」模型是不知道的——要留下来的结论请让主对话自己回答或显式粘贴

## 相关页面

- [agent_ask 不装配工具](../architecture/multi-agent.md#agent_ask-不装配工具克隆历史纯对话问答2026-10-08用户提案commit-16c1708) —— 语义同源：Agent 间无状态询问（克隆对方完整历史 + `tools=[]`）
- [recall_turn · 会话历史轮次召回](recall-tools.md) —— 对照：进历史的检索式召回（含 tools 细节，消费在轮内）
- [用户交互 · 插话机制](user-interaction.md) —— 插话是进历史的；/ask 是不进历史的旁路
