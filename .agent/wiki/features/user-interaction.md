# 用户交互 · 插话机制与消息路由

> src/agent.py（消息队列：inbox + pending_messages 双队列）+ src/static/index.html（UI）。涵盖插话（中途打断）、后台触发、并行钩子 UI 修复（2026-08-19 实测修复，commit fb115aa）、执行中行可点击观测（2026-08-20，commit 8aeb21a）、执行中实时计时与总耗时（2026-08-20，commit 6aa5903）。

## 职责

- **插话**：用户在 Agent 思考/生成 answer 期间发送消息，赶得上步边界则当步注入（`message_injected`），赶不上则暂存 `pending_messages`，待 answer 完成后自动开新轮（`background_trigger`·`user_insert`）
- **后台触发**：answer 完成后检查 `inbox`（后台队列）+ `pending_messages`（插话队列）**双队列**，有消息则自动触发新一轮处理（无需用户手动发送）
- **并行钩子 UI 状态**：多个 before_turn 钩子并行执行时，各自独立显示「执行中」状态，互不覆盖；执行中行带每秒跳动的实时秒表 `(Ns)`（commit 6aa5903）、可点击打开实时观测页，完成/失败行定格显示总耗时

## 插话全生命周期（2026-08-19 修复闭环，commit fb115aa）

**修复前问题（用户实测报告）**：answer 完成后的自动触发点只查 `inbox`（后台队列），不查 `pending_messages`（插话队列）——**两套队列漏了一半** → answer 期间发的插话滞留在队列里，直到用户手动发下一条消息才被注入消费。

**修复后闭环**：

```
忙时插话 → pending_messages（步边界检查）
  → 赶上步边界：message_injected 当步可见 ✓（原有）
  → 没赶上（answer 生成中）：answer 完成 → pop_inbox 检查 inbox
      → inbox 有消息：background_trigger 开新轮（原有，调度器/服务推送链路）
      → 【新增】inbox 空 → pending_messages 非空 → 立即开下一轮处理插话
        （background_trigger · source=user_insert）
```

**核心改动**（`src/agent.py`，answer/finish_turn 后的消息处理，摘录）：

```python
# 后台推送（调度器/服务）：消费 inbox 触发下一轮
item = self.pop_inbox()
if item:
    src, next_msg, seed = item
    self._emit({"type": "background_trigger", "source": src,
                "text": next_msg[:100], "seed": bool(seed)})
    msg, auto_flag, imgs, continue_loop = next_msg, False, None, True
    seeds = [seed] if seed else []   # 下一轮迭代预置该合成 Step
# else 分支（fb115aa 新增，语义）：inbox 空 → 检查 pending_messages，
# 非空则 emit background_trigger(source="user_insert")，取出首条开新一轮
```

**触发后时序**：插话在 answer 完成瞬间自动开新轮，**该轮 before_turn 钩子检索/注入的就是插话内容**——旧代码表现为"下一轮钩子已跑完、用户又发了新消息后，旧插话才姗姗注入"，根因即上述滞留。

**验收观测**：`/restart` 加载新代码后，answer 出现的瞬间 UI 应立即显示 `[后台触发·user_insert]` 并自动开新轮处理插话。

### 实测现象对照（2026-08-19 用户报告 8 条 → 结论）

| # | 现象 | 结论 |
|---|------|------|
| ① | 两个 before_turn 钩子紫色「执行中」并行闪烁 | 设计行为（ThreadPoolExecutor 并发，见 [钩子并行执行](../architecture/workflow-hooks.md)）✓ |
| ② | retrieval 完成后 wiki_auto_query 未完就开始第 1 步 | 旧代码行为；新代码 `as_completed` 等全部钩子完成 ✓ |
| ②b | retrieval 的「执行中」行永远闪烁不消失 | UI bug，已修（本页下节 Map 索引）✓ |
| ③ | wiki_auto_maintenance 与 answer 同时执行 | async 钩子设计行为 ✓ |
| ④ | answer_reasoning 期间插话入队 inbox 等待 | 正常（message_queued）✓ |
| ⑤⑥ | answer 后插话滞留队列、不触发下一轮 | 🔴 核心引擎 bug，已修（本节 pending_messages 兜底） |
| ⑦⑧ | 下条消息发出后旧插话才被注入 | ⑤ 的直接后果，随 ⑤ 闭环 |

## 并行钩子「执行中」状态跟踪修复（2026-08-19）

**问题**：两个 before_turn 钩子并行执行时，第二个「执行中」UI 覆盖第一个的引用 → 第一个永远闪烁不消失

**根因**：前端 `runningAutoWf` 为单数变量，`auto_wf_start` 事件处理时 `window._runningWf = rw` 直接覆盖

**修复**（`src/static/index.html`）：Map 按 `hook::name` 索引——并行钩子（before_turn 同时挂两个工作流）时各自的 running 行独立跟踪。Map 值自 commit 6aa5903 起为 `{el, timer, t0}`（见下节计时扩展）：

```javascript
// auto_wf_start：Map 按 hook::name 索引，值为 {el, timer, t0}
(window._runningWf = window._runningWf || new Map())
  .set((m.hook||'')+'::'+m.name, {el: rw, timer: _timer, t0: _t0});

// auto_wf_end / auto_wf_error：clearInterval 停表 + 按 t0 算总耗时 + delete 对应 key
clearInterval(rec.timer);
(window._runningWf || new Map()).delete((m.hook||'')+'::'+m.name);
```

**效果**：每个钩子独立跟踪「执行中」状态，并行执行时各自独立显示、独立移除（含各自独立计时）。

### 执行中实时计时（Ns）+ 完成总耗时（2026-08-20，commit 6aa5903）

紫色闪烁的「执行中」行现在带每秒跳动的秒表，完成/失败时停表定格显示总耗时：

```
执行中（每秒跳动）：⏳ [每轮开始前]钩子工作流「wiki_auto_maintenance」执行中… (15s)
完成（停表定格）：✅ [最终回答前]钩子工作流「wiki_auto_maintenance」完成（共 23s）：…
失败（带耗时）　：❌ [每轮开始前]钩子工作流「wiki_auto_query」失败（8s 后失败）：RateLimitError…
```

实现要点（全部在 `src/static/index.html` 的 `auto_wf_start` / `auto_wf_end` / `auto_wf_error` 事件处理内）：

| 点 | 说明 |
|---|---|
| **本地计时** | `setInterval` 每秒更新 `(Ns)` 后缀——纯前端 `Date.now()-t0`，零后端开销（后端事件只有开始/结束两个时间点，没有过程心跳） |
| **Map 值扩展** | `_runningWf` 从存 `el` 改为 `{el, timer, t0}`——完成/失败时 `clearInterval` + 按 `t0` 算总耗时 |
| **计时器防泄漏** | 行被历史重渲染清掉时 `isConnected` 检测自动停表，孤儿 setInterval 不空转 |
| **迟到完成兜底** | 跨 turn 的完成事件（原 running 行已不在 DOM）走 `addTrace` 路径，同样带总耗时 |
| **并行钩子** | 各自独立计时（Map 按 `hook::name` 索引，上一节修复保证并行独立性） |

**生效方式**：纯前端改动，但 index.html 是服务启动时载入内存的——**Ctrl+F5 强刷即可生效，无需 /restart**；而执行中行的可点击 run_id（下节）依赖后端事件，旧进程仍需 `/restart`。

**验证**：JS 语法 + 5 断言（计时后缀 / 总耗时 / 失败耗时 / clearInterval / isConnected）全过。秒数可与[观测页](wf-monitor.md)的节点级甘特时间线实时对照，时间感完全对齐。

### 执行中行可点击 → 实时观测页（2026-08-20，commit 8aeb21a）

上述「执行中」行在事件携带 `run_id` 时**可点击**（虚线下划线 + pointer），`window.open('/wf/monitor?run='+encodeURIComponent(m.run_id))` 新标签打开观测页，实时查看该工作流的节点时间线甘特图（跑到哪个节点、卡了多久、输出预览）——解决"钩子在跑但完全是盲盒"的观测需求。

`run_id` 由 `src/agent.py` `_run_hooks` 生成（同步线程池 + async 后台线程全覆盖，`auto_wf_start`/`auto_wf`/`auto_wf_error` 事件均携带），注册表与观测页实现见 [工作流运行观测](wf-monitor.md)。旧进程的事件不带 run_id（不可点击），需 `/restart` 生效。

## before_turn 钩子并行执行保证

见 [工作流引擎与钩子](../architecture/workflow-hooks.md#before_turn-钩子并行执行2026-08-新v0182-发布)：

- **全部完成才返回**：`ThreadPoolExecutor` + `as_completed` 确保所有钩子跑完才进入 ReAct 主循环
- **不会出现「一个钩子未完成就开始第1步」的现象**（用户实测现象为旧代码行为）

## 相关页面

- [工作流引擎与钩子](../architecture/workflow-hooks.md)：before_turn 并行执行 / async 钩子 / 快照检测闭环
- [工作流运行观测](wf-monitor.md)：执行中行点击后的观测页（run registry、节点甘特时间线，与本页秒表计时对照）
- [多 Agent 体系](../architecture/multi-agent.md)：inbox 路由 / 三层消费机制（+ pending_messages 盲区补全）/ 子 Agent 唤醒
- [wiki_auto_query](../features/wiki-auto-query.md)：before_turn 自动检索实例（默认关闭）
