# ask_user · 结构化问卷工具（_normalize_survey 宽容归一：对象选项 / 整段字符串 / 逐词拆分三防御）

> src/survey_tools.py（`make_survey_tools` → `ask_user` + `_normalize_survey`，注册于 src/chat.py「用户交互」组）+ src/static/index.html（`renderSurveyBubble`）。2026-10-05 宽容归一修复：20048 实例实锤。

## 职责与契约

`ask_user(questions)` 让 Agent 把「已知要问什么」结构化为问卷：**一次工具调用 = 阻塞等用户答完全部题目 → answers JSON 返回，Agent 同轮继续**（与 [human_step](human-step.md) 同款 `threading.Event` 阻塞模式，但**无超时**——一直等到用户回应或程序关闭）。

```python
questions: [{"id": "q1", "title": "题面", "options": ["A","B","C"], "multi_select": false, "allow_custom": true}]
answers:   {"q1": "Python", "q2": ["日志", "监控"]}
```

四层实现（与 spec/human_step 同底座）：

| 层 | 位置 | 内容 |
|---|---|---|
| 阻塞 | src/survey_tools.py `ask_user` | pending 落 `session.extra_state["_pending_survey"]`（持久化，重启后 `check_pending_survey` re-emit 恢复）→ `Event().wait()` 无限等待 → `resolve_survey` 置 result + `event.set()` |
| 事件 | `_emit_survey` | `survey_pending` 广播（questions 随事件下发）；WS action `survey_decision`（server.py，与 approval/human_step 同址）回传 answers |
| 前端 | src/static/index.html `renderSurveyBubble`（L3032） | 青色问卷卡片：单选/多选 + 可自定义输入，提交发 WS |
| 归一 | `_normalize_survey`（模块级，L30） | **2026-10-05 新增**——新问卷入口与重启恢复路径共用的宽容归一单源，见下节 |

## 20048 实锤：两种坏形态一路绿灯（2026-10-05）

跨实例 20048（E:\Project\GameFramework\Project）发来的问卷两处渲染翻车：

| 症状 | 根因 |
|---|---|
| 第 4 问两个选项都是 `[object Object]` | agent 把 options 传成**对象数组**（如 `[{label:"A"},{label:"B"}]`）——前端 `esc(opt)` 对对象做 `String()` → "[object Object]"；旧版 ask_user 对 options **零校验直接透传** |
| 第 5 问上百个单词每词一个选项 | agent 把**整句逐词拆成单词数组**当选项（LLM 生成问卷的常见失误），同样静默通过渲染 |

共性：工具入口不设防，坏形态一路透传到渲染层才炸——且用户被迫面对一份垃圾问卷。

## 修复一：`_normalize_survey` 宽容归一（后端单源）

`ask_user` 入口先过归一再落 pending，四种坏形态全部接住（能救则救，救不了也记录）：

| 输入形态 | 归一动作 | 警告 |
|---|---|---|
| options 是**整段字符串** | 按 `、,，;；/|\n` 分隔符拆成多项 | 「已按分隔符拆成 N 项」；拆不动按单选项 + 「只有一个字符串选项」警告 |
| options 项是**对象 dict** | 依次找 `text/label/value/name/option/content/title` 7 键取字符串 | 提不到则 JSON 化兜底（不告警） |
| **疑似逐词拆分** | —（照常渲染） | 「N 项且平均长度过短——疑似整句被逐词拆开」（>10 项且均长 <6 触发） |
| **题面缺失** | 依次找 `title/question/prompt/label` 键；全空补「（第N题未提供题面）」占位 | — |

设计要点：**宽容归一 + 警告记录**而非硬拒——问卷照样能发能答，坏形态不会白跑一轮。

## 修复二：前端对象选项 JSON 化 + 归一警告回注返回值

- **前端**（src/static/index.html `renderSurveyBubble`）：`opts` 构造改为 `(typeof o === 'object' && o !== null) ? JSON.stringify(o) : String(o)`——即使后端旧版本/未重启漏归一，前端也**永不再出 [object Object]**（双保险的显示层）。
- **返回值**：`ask_user` 返回的 answers JSON 尾部附归一警告：

```python
return _j.dumps(result, ensure_ascii=False, indent=2) + ('\n⚠️ 归一警告：' + '；'.join(_warns) if _warns else '')
```

用户答完卷 Agent 拿到结果时**连警告一起看到**——「啊我上次的 options 传成对象了」，下次调用自愈（自愈闭环靠工具结果反馈，不靠用户骂）。`[错误] 用户未提供任何回答` 分支同样附警告。

## 修复三：归一提升模块级 + 恢复路径（重启 re-emit）也过归一（2026-10-05 · 二）

修复一的补刀：归一只挂在 `ask_user` **入口**，而 pending 问卷是**存档态**——坏问卷以原样躺在 `extra_state["_pending_survey"]`，重启后 `check_pending_survey` re-emit 时**绕过归一**原样渲染。用户追问「那我重启再加载能渲染吗」暴露的缺口，两处改动闭合：

1. **`_normalize_survey` 提升模块级**（src/survey_tools.py L30，原嵌套在 `make_survey_tools` 内）——恢复路径（模块级 `check_pending_survey`）与新问卷入口同用一份归一，单源坐实。
2. **恢复路径过归一并写回**：`check_pending_survey` re-emit 前 `sid, _w = _normalize_survey(sid)` → 归一结果**存回** `extra_state["_pending_survey"]`（坏形态只修一次，此后读档即净）→ try/except 容错，归一异常不阻塞恢复。

### 重启后两题的不同命运（20048 对照）

| 题 | 重启后 | 原因 |
|---|---|---|
| 第 4 问（`[object Object]`） | ✅ **存档自愈** | 对象选项在读档 re-emit 时提取文本键转字符串——归一救得了的「形态错」，重启即净 |
| 第 5 问（百词选项） | ⚠️ 照旧 | 存档里已是**合法**字符串数组（每词一项）——归一不猜意图合并，需让 agent 重发问卷 |

分界线：归一救「形态错」（对象 / 整段字符串），不救「语义错」（真把单词当选项）——后者靠修复二的警告回注让 agent 自觉重发。

### 验证

模块级直调 `['甲','乙']` 原样通过零警告 ✓；site-packages 已同步，20048 重启即三层齐活（入口归一 + 恢复归一 + 前端兜底）。commit 已落本地（GitHub 推送当晚被网络卡住，push-retry 定时重试中，不影响本机功能）。

## 验证：四态全绿

| # | 场景 | 预期 | 结果 |
|---|---|---|---|
| ① | 对象选项 `[{label:...}]` | 提取文本键转字符串 | ✓ |
| ② | 整段字符串「A、B、C」 | 按分隔符拆 + 警告 | ✓ |
| ③ | 正常数组 `["A","B"]` | 原样不动、无警告 | ✓ |
| ④ | 百词数组 | 检出疑似逐词拆分 + 警告 | ✓ |

## 提交后答案摘要：整卡替换 → 摘要保留可回看（2026-10，用户提案）

旧形态：`renderSurveyBubble` 提交后**整卡替换**成一行「✅ 已提交」——答了什么无处回看（刷新前想核对选项都不行）。与 [human_step 提交后只读化](human-step.md)同轮改造（前端 src/static/index.html，刷新页面即生效）：

| 项 | 行为 |
|---|---|
| 控件 | 提交后按钮 / 选项全部移除（延续原有清理，只是不再整卡替换） |
| 回执 | `✅ 已提交，等待 Agent 继续…` |
| 摘要 | 逐题列出：`· q1：选项A`、`· q2：选项B、选项C`（多选顿号连接，自定义输入原样带上） |
| 文本 | `esc()` 转义 + `white-space:pre-wrap`（含换行的自定义答案保真） |

价值：用户可回看自己刚答了什么；Agent 侧不受影响（answers JSON 照旧走 WS action `survey_decision`）。

## 注意事项

- **生效方式**：后端归一需 `/restart`（site-packages 已同步）；前端 JSON 化防御刷新页面即见。20048 旧问卷两题命运不同：对象选项题重启**存档自愈**（恢复路径归一），百词选项题数据本身已合法、需让 agent 重发。
- **警告只进工具返回值**（给 Agent 复盘自愈），不进问卷卡片——用户看到的已是归一后的正常问卷。
- `_normalize_survey` 现为**模块级函数**（2026-10-05 · 二从 `make_survey_tools` 内整块提升、去一层缩进）——恢复路径 `check_pending_survey` 也要调它；施工时曾两次因缩进不符被 ast.parse 拦截，提升时一次到位。
- 与 [human_step](human-step.md) 同文件（survey_tools.py）同阻塞底座：human_step 30 分钟超时兜底，ask_user **无超时**（问卷可以放一晚上）。

## 相关页面

- [human_step · 人在环步骤](human-step.md) — 同文件同阻塞模式（自由指令 vs 结构化问卷）
- [工具执行审批](tool-approval.md) — 三按钮安全门（survey_pending 补发同款哲学：实时事件必须有持久化的补发源）
- [用户交互](user-interaction.md) — WS action 通道（survey_decision / approval_response / human_step_response 同址）
- [多实例组网](../architecture/multi-instance.md) — 20048 事故的跨实例背景（本轮状态类工具状态落本地实例）
