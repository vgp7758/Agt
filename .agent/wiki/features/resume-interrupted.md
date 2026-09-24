# 中断轮恢复 · resume_interrupted + _INTERRUPT_MARKS 中断标注识别

> src/session.py（`_INTERRUPT_MARKS` 前缀集合 + `resume_interrupted` / `abort_current_turn`）+ src/static/index.html（前端 `isInterrupted` 判定）。轮被中断（用户停止 / run() 异常崩溃）后的防御性归档与恢复——WebUI「▶ 继续」按钮背后的机制。

## 职责与机制链

三条链路协作，让"中断轮"可识别、可恢复：

| 环节 | 位置 | 语义 |
|---|---|---|
| **防御性收尾归档** | start_turn | 上一轮没写 answer 就开新一轮（用户停止 / 崩溃）→ 补一个中断标注 answer 归档，轮不"消失"（旧数据读档可见） |
| **异常中断标注** | run() 异常逃出路径 | `abort_current_turn(f"（异常中断：{type(e).__name__}）")`——异常轮归档时带异常类型 |
| **中断识别** | `session._INTERRUPT_MARKS` 前缀集合 | `resume_interrupted` 判定可恢复轮；前端 `isInterrupted` 判定中断轮渲染——**双端据此识别，集合是唯一真源** |

`_INTERRUPT_MARKS` 现行五个前缀（2026-09-22 修复后）：

```python
_INTERRUPT_MARKS = ("（中断，本轮未完成", "（被中断", "（被用户停止", "（被用户中断", "（异常中断：")
```

- `（被用户中断）` 是旧 KeyboardInterrupt 路径的文案（已统一为`（被用户停止）`），保留兼容历史存档
- **前缀一律不带尾括号**——既匹配裸文案`（被中断）`，也匹配带原因后缀的`（中断，本轮未完成——LLM 502: ...）`/`（异常中断：RuntimeError）`（见下文修复，尾括号曾是连环 bug）

## resume 语义：断点续跑，不是重发投影

（纠正常见误解——用户 2026-09-22 提问「点继续只需要把最后一次投影重新往 api 里发送一遍就可以了吧」）

- `resume_interrupted` 做的是**断点续跑**：把中断轮 pop 回 `session._current` 进行态、清中断标注、**已完成的 steps 原样保留在投影里**，ReAct 从断点接着做剩余工作——不重做已完成步骤，比重发整个投影省得多
- 「把最后一次投影重发一遍」是 **`/debug prompt` 的语义**（诊断用），不是 resume
- 前提是「识别出这是个中断轮」——本页两次修复（含上表）都卡在这一步，识别对了后面全是现成链路

## /continue [分钟]——中断轮延时续跑（2026-09-23，用户提案，commit d90b2f2）

**用户提案（2026-09-23）**：「中断后出的继续按钮，有时候我不希望立刻点，可能想半小时之后再点——可以 `/continue` 立刻继续，`/continue 30` 半小时后继续。」

**用法**：

```bash
/continue            # 立刻续跑（等同 WebUI 中断轮的「▶ 继续」按钮）
/continue 30         # 30 分钟后自动续跑（分钟数，支持小数如 0.5）
```

**语义与按钮完全一致**：断点续跑原轮（**不新增 user_message**）、已完成步骤完整保留、走同一条 `agent.resume_interrupted()` 路径——含降级链路（中断轮被后台轮顶下去时自动找最后一个中断轮入队续跑）。CLI / WebUI 输入框均可敲，不必等按钮。

**实现**（src/commands.py `_cmd_continue`，commit `d90b2f2`）：

- 延时用 `threading.Timer`（**进程内**）→ 到点经 **work_q 投递 `("task", fn)`**——worker 串行执行，不与主循环并发抢 run（与按钮/命令同一队列语义，天然规避 [agent_notify 空闲唤醒](../architecture/multi-agent.md) 同类并发问题）
- 无 work_q 环境（纯 CLI 未起服务）→ 直接执行
- **局限（提示语已写明）**：定时器不持久化，重启后失效——届时再敲一次即可

CLI 启动横幅命令表 + `/help` 详情已同步。**验证五连**（rc=0）：立刻续跑调 resume_interrupted ✓ / 0.02 分钟（1.2s）到点经 work_q 执行 ✓ / 非法输入与负数出用法提示 ✓ / 命令注册 ✓ / 无中断轮报「[错误] 没有可恢复的轮次」✓。`/restart` 生效。

## bug 修复（2026-09-22，用户实锤）：异常中断形态漏识别——点继续恒报"已正常完成"

**现象**（`~/.agt/repos/D--Programs-env/sessions/20260916_093550`）：轮中断后点「继续」弹 `无法恢复：[错误] 最后一轮已正常完成，历史中也没有可继续的中断轮`。

**根因**（读存档实锤，最后三轮状态）：

```
turn_end  answer="双协议路由已上线并真机验证通过…"   ← 正常完成 ✓
turn_start user="还有就是现在worker在拿到请求后…"
turn_end   answer="（异常中断：RuntimeError）"        ← 异常中断轮（归档了）
turn_start user="继续"
turn_end   answer="（异常中断：RuntimeError）"        ← 又中断一轮
```

`resume_interrupted` 判定链：answer 非空 → `_is_interrupt_mark`（前缀匹配 `_INTERRUPT_MARKS`）→ 集合里**没有`（异常中断：`这个前缀**（run() 异常逃出路径写入的形态此前一直漏登记）→ 异常中断轮被判成"已正常完成"拒绝恢复 → **点继续恒报错**。

**连带 bug（同日二轮发现）**：集合里的前缀**带尾括号**（如`（被中断）`），对带原因后缀的新文案`（中断，本轮未完成——LLM 502: ...）` `startswith` 恒 False——docstring 声称支持后缀形态，实际从未匹配上。两笔 edit 收敛：加`（异常中断：`前缀 + 全部前缀去尾括号。

**两层修复**：

| 层 | 改动 |
|---|---|
| src/session.py | `_INTERRUPT_MARKS` 加`（异常中断：` + 全部前缀去尾括号（裸文案与带后缀形态都匹配） |
| src/static/index.html | 前端 `isInterrupted` 同步对齐——原来是**精确等值**（只认`（中断，本轮未完成）`/`（被中断）`两种），连`（被用户停止）`都不认：该 session 的中断轮在读档时显示的还是普通气泡；改前缀 `.startsWith` 数组 some 匹配 |

**对实锤 session 的效果**：实例 `/restart` 后再点继续 → turns 尾部的`（异常中断：RuntimeError）`轮被正确识别 → pop 回 `_current` 断点续跑。

**验证**：10/10 全过。

## 排障口诀与注意事项

- **点继续报"已正常完成" = 先看存档里最后一轮 answer 的标注形态 vs `_INTERRUPT_MARKS` 集合**——新写入路径（如新的 abort 文案）必须同步登记进集合，前后端两处都要
- 前端判定与后端集合是**手工对齐**的（无共享常量）——改集合时记得同步 index.html 的前缀数组
- 生效方式：src/session.py 引擎层需 `/restart`；index.html 强刷（Ctrl+F5）即生效
- 与 [气泡交互 · answer 中断轮充值入口按钮](bubble-interaction.md#answer-中断轮充值入口按钮--回退链全失败一键打开2026-09-08用户提案) 的关系：那是"回退链全失败导致的中断"的充值补救入口；本页是"中断轮本体"的识别与续跑

## 相关页面

- [系统总览](../architecture/overview.md) —— start_turn 中断轮防御性收尾归档在一轮对话数据流中的位置
- [运维排障 · 常见错误对照](../guides/ops.md) —— 中断轮"消失"（防御归档修复）与本页同域
- [气泡交互](bubble-interaction.md) —— 中断轮的「▶ 继续」按钮与充值入口渲染
