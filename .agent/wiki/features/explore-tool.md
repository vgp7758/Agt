# explore —— 外置探索工具

> 演进：spec s_54a1eb86（react 探索 + Step 嫁接）→ **s_7edebd56（2026-10-16）：嫁接改投影层内插（仅当前轮）+ run_id 旁车 + 断点续跑**。
> 实现：`.agent/tools/explore_tools.py`（运行时）＋ `src/assets/tools_builtin/explore_tools.py`（种源，双份同构）；
> 投影内插在 `src/session.py`，轮首恢复提示在 `src/agent.py`。

## 三个工具

| 工具 | 作用 |
|---|---|
| `explore(goal, model?, max_steps=8, budget_seconds=120)` | 外置探索：把多轮 grep/read 摸底外包给小上下文 react 循环（默认 utility 模型，不占主线步数），返回结构化摘要。**同一步可并行多个 explore 各查不同目标**（`_SEED_LOCK` 串行化共享面） |
| `explore_resume(run_id)` | 从断点续跑未完成的探索 run（旁车拼回 messages，剩余步数/预算按 meta 减已跑） |
| `explore_list()` | 列全部旁车 run：run 号/goal/状态（进行中｜完成｜半截）/步数 |

## 投影语义三态（s_7edebd56 核心）

| 场景 | 主线 LLM 看到什么 | 持久化面 |
|---|---|---|
| **当前轮** | steps 渲染之后、pending hint 之前，**内插** N 组 `assistant(tool_calls)+tool` 消息（tool_call_id=`exp-<run>-<seq>`，reasoning 挂 `reasoning_content`，带 `[外置探索]` 前缀）；结果已按 `_RESULT_CAP=6000` 截断，全量披露不二次衰减 | toollog 只有主线「调用 explore」一条 record |
| **下一轮（历史轮）** | 只剩「调用 explore（主线 step）+ 结果摘要」两条——过程内插消失 | events.jsonl **无**嫁接 step（回放干净） |
| **读档回放** | 同历史轮（`Turn._explore_seeds` 是运行时 dataclass 字段，events 重放不落盘，天然消失） | 同上 |

实现锚点：`Session._seg_msgs_steps` 尾部内插 → `Session._explore_seeds_to_msgs`（渲染）；
`Turn._explore_seeds`（运行时字段）；explore_tools 侧 `_attach_seeds`（`_SEED_LOCK` 下 append）。

## run_id 旁车与断点续跑

- **旁车文件**：`<session_dir>/explore/<run_id>.jsonl`（run_id=uuid4 前 8 位 hex，返回头标注 `[外置探索·run:xxx·<stop_reason>]` 可追溯）。
  - 首行 `{ev:"meta", goal, model, max_steps, budget, ts}`；每步 append `{ev:"assistant"|"tool", msg:{...}}`（消息本体，恢复拼用）；完成写 `{ev:"done", summary}` 终行。
  - 文件写失败**静默降级**为纯内存（行为不劣于无旁车——只丢续跑能力）；`_SEED_LOCK` 串行化 append（防并行 explore 行交错）。
- **中断发现**：`Session.load` 尾部 `_scan_pending_explores()`——无 done 终行的 run 记 `extra_state['_pending_explores']`（run/goal/步数）。
- **轮首提示**：`agent.run` 轮开始（`resumed` 旁注之后）一次性消费 `_pending_explores` → `_hook_notes` 注入 `<hook name="interrupted_explore">` 旁注（pop 清标记，不重复注入）；模型可 `explore_resume(run_id)` 续跑，也可忽略（旁车不投影，无副作用）。
- **续跑**（`explore_resume`）：读旁车拼回 messages（system+user+已完成组）→ 续循环（剩余步数 = meta.max_steps − 已跑，预算重置 budget 或剩量）→ 每步继续 append 旁车 → 完成写 done + seeds 进当前轮（与正常 explore 同路径，投影同款内插）。返回头 `[外置探索·续跑·run:xxx·完成] 已在 M 步基础上续跑 K 步`。
  - 损坏行（JSON 解析失败）→ 从最后完整行截断续跑并在返回头标注。
  - 重复 resume 已完成 run → 拒绝并返回既有摘要（幂等）。
  - `_current` 为 None（轮外直调）→ seeds 丢弃 + 返回头标注 `[过程未投影]`。

## 设计取舍（为什么改）

- 旧 _seed_steps 嫁接：探索步批量写 toollog/events/turns——读档回放与历史投影里混入大量探索过程步，且 toollog cid 计数、并行顺序都受主线状态牵制。
- 新方案：**过程只活在当前轮投影**（内插），持久化面只剩主线调用+摘要两条；旁车文件自带 run_id 命名空间，与主线 cid 零耦合（seeds 不再走 toollog → 无 call_id 分配/恢复问题）；缓存影响不劣化（原嫁接 add_step 同样断轮内前缀，旁车 append 等价且少写两份 jsonl）。
- `_seed_steps` 函数本身保留——bg_task/service_exit 通知通道仍共用（agent.py），只摘除 explore 这个调用方。

## 验证

- 单测 `test/test_explore_tool.py`（FakeLLM+临时真 Session）：旁车结构/events 零嫁接/内插投影/下轮消失/续跑 seq 接续/损坏截断/scan/超时降级/白名单——全 PASS。
- 冒烟（真 LLM、隔离 workspace）：①explore 真跑 ②旁车落盘 ③当前轮内插+events 干净 ④下轮只剩两条 ⑤半截→load 扫描→轮首提示→**模型自主 resume 至 done** ⑥resume 直调（返回头/seeds/投影）——18/18 PASS。
