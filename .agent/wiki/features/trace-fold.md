# WebUI 过程区折叠 · 思考内容 + 工具调用 + 钩子行三级降噪

> 全部在 `src/static/index.html` 的 trace 渲染（`newTurn` 建的 `details.trace` 内），共享同一套 `toggleFold(head, body)` 基建 + `.tf-head`/`.tf-body` CSS。设计动机：思考模型一步几千字 reasoning 整段平铺、一轮几十步的对话把过程区刷成数屏——按「值得看的展开、其余收起」分层降噪。

## 三级降噪层级（当前）

| 层级 | 默认态 | 摘要行 | 上线 |
|------|--------|--------|------|
| 💭 思考内容（reasoning 流式段落） | **折叠** | `▸ 💭 思考（N 字，点击展开）`，字数随流式增长 | 2026-08，commit ee968be |
| 🔧 非 diff 工具调用 | **折叠** | `▸ 🔧 工具名(主参数)` | 同批（用户提案） |
| 文件 diff 类工具（edit/write_file/insert/replace_lines/delete/move） | **展开（不可折叠）** | 无——改动详情是一轮里最值得看的东西 | 同批 |
| 钩子组（同 hook 多工作流，auto_wf_start 起） | **折叠** | `▸ [每轮开始前]钩子 ×2 (1/2) ⏳ 12s`（组头计数+计时） | 2026-08，commit 4455503 |
| 钩子完成文本（组内行级二级折叠） | >160 字才折叠 | 截 110 字（换行处截断） | 2026-08，commit 4455503 |
| ⚡ 钩子注入记录（hook_note 事件，**历史读档不渲染**） | **折叠** | `⚡ [位置]钩子注入「name」· run=xxx（N字·点击展开）` | 2026-09-01，commit acc06f1 |

## 基建：toggleFold + .tf-head/.tf-body

```
function toggleFold(head, body){          // 通用折叠：body display 切换 + head 前缀 ▸/▾
  body.style.display = open ? 'none' : 'block';
  head.textContent = (open ? '▸ ' : '▾ ') + head.textContent.slice(2);
}
```

- CSS：`.ev.tool-fold` > `.tf-head`（蓝色，工具/钩子）+ `.tf-body`；思考版 `.tf-head.think-head` / `.tf-body.think-body`（**灰斜体 + pre-wrap**，视觉承袭原 `.ev.think` 思考行）
- 四个消费端同一折叠语义：思考组/工具组/钩子完成文本直接调 `toggleFold`；**钩子组**（auto_wf_start 建组）内联实现同款 toggle（额外调 `g.upd()` 刷新组头计数/前缀）。head 前缀约定 `▸ /▾ ` 两个字符供切换时改写

## 💭 思考内容折叠（commit ee968be，核心交付）

用户诉求：「reasoning_content 默认折叠，打开的时候也不用截断」。

### 流式累积 + 全文从不截断

`renderThinking(text, label)` + 模块级 `_curThinkFold`（当前思考折叠组）：

- 后端 thinking 事件是**流式分块** emit 的 → 每个分块 `_curThinkFold.buf += text` 累积，`body.textContent = buf` **全量写入**——不存在任何 slice/截断；此前行为是整段灰斜体平铺（也没截断，只是一步几千字平铺数屏看着像被截）
- head 摘要行实时更新字数：`▸ 💭 思考（2847 字，点击展开）`——折叠时也知道这段思考多大
- 无组或组已脱 DOM（`!_curThinkFold.body.isConnected`）→ 自动新建折叠组（`.ev tool-fold` + think-head/think-body），防御历史重渲染清掉 DOM 后的悬空引用

### 自动分组：归属关闭的三个时点

下一段思考拼不进上一段——在以下时点置 `_curThinkFold = null` 关闭归属：

| 时点 | 位置 | 效果 |
|------|------|------|
| `step` 事件（新一步） | onWSMessage case 'step' | 每步的 reasoning 各成一组 |
| `tool_call` 到达 | onWSMessage case 'tool_call' | 思考段与后续工具调用分离 |
| 历史渲染 | renderHistTurn：每步开始重置 + 整轮渲染完复位 | 防上一步 reasoning 拼进同组；防污染后续实时轮的归属 |

### 历史渲染同构（renderHistTurn）

历史读档与实时流**走同一个 `renderThinking`**——历史轮与实时轮折叠形态一致：

- 每步 `s.reasoning` → `renderThinking(s.reasoning, '💭 思考')`
- 轮末 `t.answer_reasoning` → `renderThinking(..., '💭(回答推理)')`（回答前推理单列一组）
- 顺带：历史 `s.tool_calls` 的 result 也经 `appendToolResult` 追进折叠组 body——历史与实时同构（此前历史 result 走独立行）

### 子 Agent 标识

thinking 事件带 `agent_id`（`_emit` 统一打标，见 [气泡交互](bubble-interaction.md#answer-多-agent-分页indexhtml--agentpy2026-08-21)）：文本加 `[agent_id] ` 前缀、label 显示 `💭 agent_id 思考`——子 Agent 的思考不裸混进主 trace。

## step 事件行：累计 token 数字格式化（2026-08，commit 962b7cf）

**动机（长 session 实测）**：累计 token 到百万级后 step 分隔行 `— 第 N 步 · 累计 4872345 token · by proxy —` 原始数字不可读。

**修复**（src/static/index.html）：新增 `fmtTokens()`，step 事件行累计 token 改走它：

```javascript
function fmtTokens(n){ n=Number(n)||0; if(n>=1e6) return (n/1e6).toFixed(1)+'M';
  if(n>=1e3) return (n/1e3).toFixed(1)+'K'; return String(n); }
// step 行：— 第 1 步 · 累计 4.8M token · by proxy —
```

格式化规则：≥1M → `x.xM`、≥1K → `x.xK`、否则原数。`fmtArgs` 同处新增（本轮顺带），供其它参数字符串化复用（`JSON.stringify` 失败兜底 `String`）。纯前端，Ctrl+F5 刷新即生效。

## 🔧 工具调用折叠（同批基建）

- `_DIFF_TOOLS = new Set(['edit','write_file','insert','replace_lines','delete','move'])` → diff 类**不折叠**（改动详情值得直视）；其余工具建折叠组
- head 摘要 = 工具名 + 主参数（args.path/query/name/command 截 60 字）；body = 完整调用渲染（`toolCallHTML`）+ result + 流式
- `appendToolResult`：result 追加进 `_curToolFold.body`（`→ ` 前缀灰行）；无折叠组回退独立行
- `tool_stream`：流式输出优先追加进折叠组 body（折叠着也在累积）
- **并行批不追踪归属**（`_inParallel`/`_parLeft`）：并行结果无法与具体 call 配对 → 独立行，不进 body

## 钩子行折叠

**组折叠（auto_wf_start 起，commit 4455503）**：同 hook 位置的多个工作流收进一个组头 `▸ [每轮开始前]钩子 ×2 (1/2) ⏳ 12s`——**默认收起**，点击展开看各工作流执行详情。组头带计数（done/total）+ 组级秒表，运行中脉冲动画、全完成/有失败停表定格；行内工作流保留观测页跳转 / 完成态。实现细节与跨轮兜底见 [用户交互 · 钩子组折叠显示](user-interaction.md#钩子组折叠显示同-hook-位置收进一个组头2026-08commit-4455503)。

**完成文本二级折叠（auto_wf）**：组内行完成文本 `✅ 「name」完成（Ns）：…` 全文 >160 字时折叠（head 截 110 字 + 换行截断，`…（点击展开）`）——组已折叠的前提下展开组还能再展开长文本（两级降噪）。跨轮迟到的完成（async 钩子，组已脱 DOM）回退 `addTrace` 独立行。

## hook_note 注入折叠（2026-09-01，commit acc06f1）

**事件源**：`case 'hook_note'`——钩子注入落盘事件（后端 `_run_hooks` 每项 inject 结果写 events.jsonl，见 [workflow-hooks · hook_note 落盘](../architecture/workflow-hooks.md#钩子注入-merge-化--hook_note-落盘2026-09-01用户提问触发)）的前端消费端：实时视图折叠成一条记录提示，默认收起，点击展开看注入全文（「当轮模型看到了什么注入」实时可查，读档/实时同路径）。

**渲染形态**（复用 toggleFold + .tf-head/.tf-body 基建，纯前端消费、后端零改动）：

- 摘要行：`⚡ [位置]钩子注入「name」· run=xxx（N字·点击展开）`——`wfHookTag(m)` 取位置标签；`run_id` 存在时附 `· run=<rid>`；紫 #7c3aed 区分钩子注入
- body：`white-space:pre-wrap` 注入全文，`display:none` 默认收起

**实时 vs 历史分工**：

| 场景 | 行为 |
|------|------|
| 实时（本轮进行中） | 折叠组渲染——「这轮注入了什么」可见 |
| 历史读档 | **不渲染**——`renderHistTurn` 只遍历 steps 工具链 + answer（与 auto_wf 钩子完成事件同约定） |

**为什么历史不渲染**：历史轮 UI 保持简洁（用户气泡 + 工具链 + answer），运行时噪声只出现在实时视图；事后可追溯靠 events.jsonl 磁盘证据（`"type":"hook_note"`）——「UI 不堆砌历史噪声、磁盘保留完整真相」。

## 与其他模块的关系

- **后端零改动**：纯前端消费既有事件流（thinking/step/tool_call/tool_stream/tool_result/auto_wf）——折叠是渲染层投影，不动协议
- agent_id 打标链路见 [气泡交互 · answer 多 Agent 分页](bubble-interaction.md)（`_emit` setdefault）
- 与气泡级折叠（editor.html 系统气泡）是**两套独立机制**：前者是 trace 内行级折叠，后者是气泡面板级展开——勿混

## 注意事项

- **「打开不截断」的实现口径**：body 里存的始终是完整 buf（textContent 全量），没有截断这回事；用户感知的「截断」来自此前整段平铺太长。摘要只出现在折叠 head（字数提示），不在 body
- 折叠状态**不持久化**：跨轮/刷新重置为默认收起（与气泡折叠同约定）
- `renderThinking` 会 `scrollBottom()`——流式期间即使折叠着也持续贴底，与工具进度行行为一致
- 新增消费端（如以后的计划行折叠）直接复用 `toggleFold`，head 文本前缀必须保持 `▸ /▾ ` 两字符格式（切换逻辑按 `slice(2)` 改写）

## 相关页面

- [气泡交互](bubble-interaction.md)：answer 多 Agent 分页与 agent_id 打标（思考组 label 同源）、气泡级复制
- [用户交互](user-interaction.md)：并行钩子「执行中」行（auto_wf 系列 UI）
- [运维与排障](../guides/ops.md)：过程区之外的可观测性（/stats、🐞 日志面板、/wf/monitor）
- [系统总览](../architecture/overview.md)：事件流 _emit → broadcast → 前端渲染链路
