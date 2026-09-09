# spec 工具集 · spec 施工五件套 + 探索前置（explore 外置接管）

> `src/spec_tools.py`（支撑层模块）。spec 施工流程的工具集，现为五件套：create_spec / commit_spec / regenerate_spec / list_specs / recall_spec。流程第一步「探索（可选）」**不再是 spec 工具的一部分**——2026-09-09 起由外置探索工具 explore（`tools/builtin/explore_tools.py`）承担（原内置同步工具 explore_subagent 同日删除，见下）。

## 探索入口演进：explorer / explore_subagent / explore（2026-09-09 收敛为二）

三个同族工具一度并存（名字相近，两回事）——2026-08 澄清过 explorer vs explore_subagent，2026-09-09 新增 explore 外置工具后同日删除 explore_subagent：

| | **explorer**（声明式子 Agent） | ~~explore_subagent~~（spec 工具，**2026-09-09 已删**） | **explore**（外置探索工具） |
|---|---|---|---|
| 身份 | `.agent/agents/` 里的声明，`agent_prompt("explorer", ...)` 派活 | ~~`spec_tools.py` 里的同步工具~~（已删除，见下节） | `tools/builtin/explore_tools.py` 工厂工具（经 `ctx["agent"]` 注入主 Agent 引用） |
| 同步性 | **异步**——answer 入 inbox 唤醒主 Agent | ~~同步阻塞~~（报告作为工具结果返回） | **同步**——主 Agent 直接调工具，小上下文 react 循环探索 |
| 用途 | 通用只读搜索定位 | ~~spec 流程前置探索~~（从未被实际调用） | 找相关代码的多轮 grep/read 外包 + 嫁接回上下文；spec 施工前摸模块同样适用 |
| 团队可见 | registry 注册（看板可见） | 不注册——一次性实例用完即弃 | 不注册（无子 Agent 实例） |
| 工具面 | 声明自由 | 硬编码只读白名单 | 只读五件（grep/read_file/glob_files/find_function/list_dir）+ 执行白名单双闸 |

## explore_subagent 删除（用户裁定 2026-09-09，commit ce6de5f）

**背景（用户观察）**：explore_subagent 工具「从来没被调用过，应该是多余的」；更常规的操作是**同一步并行调用多个 explore** 各查不同目标——探索职责从「派一次性子 Agent」整体让位给「外置 react 探索」。

**删除的六处分布**：

| 位置 | 处理 |
|---|---|
| `src/spec_tools.py` | 函数 + `Tool(explore_subagent)` 注册删除，留注释说明被 explore 取代——小上下文 react 探索 + Step 嫁接回主上下文，比一次性子 Agent 更轻（无独立实例开销、结果直接进投影）；spec 工具集回归五件套 |
| `src/chat.py` + 播种源 `src/assets/main.yml` | SYSTEM 引导句：「先 explore_subagent 派探索子 Agent」→「先**用 explore 外置探索**摸清相关模块（**同一步可并行多个 explore 各查不同目标**），再用 create_spec 制定施工方案」 |
| `src/tool_briefs.py` | `explore_subagent` 简介 → `explore: 外置探索：小上下文 react 摸清代码，结果嫁接回上下文` |
| `src/server.py` + `src/static/index.html` | 注释示例措辞（answer 分页特例的「（explore_subagent 等）」去掉）——**特判逻辑本身保留**，继续服务 update_wiki 等仍存的同步工具型子 Agent |

**同源作废**：explore_subagent 专属的两段历史随删除自然失效——① token_budget 残留修复（2026-08，commit eafed25：独立构造路径漏改仍为 20000，对齐 0 且 `max_steps=12` 保留）② 串台根因链里的「spec_tools.py L482 构造 SubAgent 传 on_event」现场（该机制的现行载体 = update_wiki 等仍存同步子 Agent，见 [多 Agent 体系 · 事件流打标](../architecture/multi-agent.md#事件流-agent_id-打标与-webui-串台修复2026-08-21commit-ba0940b)）。

**教训**：从没被调用的工具是维护负资产，删除比保留更省心；「找代码」类多步探索的正确打开方式 = 外置 explore 一步多路并行（见下）。

## explore（外置探索工具，2026-09-09）—— spec 前置探索的现行入口

2026-09-09（spec s_54a1eb86，commit 4bcd144，用户提案）新增**通用外置探索工具** `explore(goal, max_steps=8, budget_seconds=120, model='')`（`tools/builtin/explore_tools.py`，经 `ctx["agent"]` 注入主 Agent 引用；实现细节见 [工具外置 · explore 行](tool-externalization.md)）——探索职责的现行承载：

| | explore（外置探索工具） |
|---|---|
| 触发 | 主 Agent 直接调工具（同步） |
| 载体 | **小上下文 react 循环**（默认 utility 模型，system(探索者人设)+user(goal)）——不建子 Agent 实例 |
| 结果 | **结构化摘要 + 工具调用嫁接回主 agent steps**（复用 `_seed_steps`：toollog.record + Step + add_step——events.jsonl/读档重放/步距衰减全走既有管线，零新落盘代码；reasoning 标注 `[外置探索]`，随轮内分组自然衰减可追溯） |
| 白名单 | 只读五件（grep/read_file/glob_files/find_function/list_dir）+ 执行白名单双闸 |
| 降级 | 无 agent 引用（纯工具箱构建/测试）**不注册**，不炸主程序 |

设计动机：纯探索过程（多轮 grep/read_file 定位代码）对历史会话依赖低——丢进小上下文循环完成（单结果 6000 字截断防投影膨胀；终止=模型主动收口 / 步数 / 墙钟预算），主 Agent 只拿结构化摘要（不衰减）继续干活。recent-file 语义澄清：`_FILE_SNAP_TOOLS` 只挂写工具（edit/write…），探索全只读——嫁接步不进 rf_map 是与主循环一致的正确行为（rf 管「本轮变更文件速览」）。

e2e 四场景全绿（test/test_explore_tool.py）：嫁接 2 步落盘 + `_replay_events` 读档重放重建树 / 超时降级（`[外置探索·步数上限]` 部分结果）/ edit 白名单双闸拒绝（schema 不含 + 执行白名单）/ 白名单只读。详见 [tool-externalization](tool-externalization.md)。

⚠️ **降级边界误触（2026-09-09，commit 371db5e）**：降级分支只该在「纯工具箱构建/测试」触发，但 `/reload tools`（`reload_script_tools`）曾漏传 agent 误触发它——启动 47（含 explore），reload 摘 46 添 46，explore 被静默摘除不注册。已修复（reload 路径补 `agent=agent` 透传）。**判断 explore 在不在：对比启动装配与 reload 是否同参（reload 结果数 ≠ 启动数即异常），reload 显示的「摘 N 添 N」配对本身看不出丢**。

### 并行 explore：同一步多路 + 嫁接锁（2026-09-09，commit ce6de5f，用户裁定）

explore_subagent 删除的同时确立并行语义——docstring 补明：**在【同一步】发起多个 explore（各查不同目标）即多路并行探索**——比逐个串行调用快得多；SYSTEM 引导句同步此说法。

实现配套（`tools/builtin/explore_tools.py`）：

```python
_RESULT_CAP = 6000   # 单次工具结果进探索上下文/嫁接记录的字符上限（防投影膨胀）

# 嫁接锁——探索循环本身并行，只有 _seed_steps 嫁接段串行化：
# session.add_step→_emit_event 追加写 events.jsonl 无锁，并发嫁接可能行交错；
# 锁内做 seed 逐条落盘，开销可忽略。
import threading
_SEED_LOCK = threading.Lock()
```

- 嫁接段 `with _SEED_LOCK: agent._seed_steps(seeds)`（toollog + Step + add_step 自动落 events.jsonl；此时 explore 调用步尚未归档 → 嫁接步自然在前）
- **验证**：4 线程并行 explore → 嫁接 steps=4 恰好（预期 4），events.jsonl 6 行全部合法 JSON 无交错 ✓

## 注意事项

- spec 工具集现状 = 五件套（create/commit/regenerate/list/recall_spec）——「先探索」不再由 spec 工具承担，走外置 explore
- 探索并行用法：同一步发多个 explore（各查不同目标）；嫁接段有锁串行化，探索循环本身并行
- 同步子 Agent 现仅 update_wiki 等（explore_subagent 已删）；answer 分页特例 / 串台打标机制保留，服务前者——长程调研仍应走 `agent_prompt` 异步派活（answer 走 inbox，见 [多 Agent 体系 · 三层消费机制](../architecture/multi-agent.md)）

## 相关页面

- [工具外置 · explore 行](tool-externalization.md) —— explore_tools.py 的 ctx["agent"] 注入 / 降级误触教训
- [多 Agent 体系](../architecture/multi-agent.md) —— SubAgent 构造 / registry / 串台修复 / explorer 声明式子 Agent
- [气泡交互 · answer 多 Agent 分页](bubble-interaction.md) —— agent_id 打标的前端兜底
- [系统总览](../architecture/overview.md) —— spec_tools.py 属支撑层
