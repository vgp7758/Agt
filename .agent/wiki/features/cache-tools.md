# cache_breakpoint · 缓存断点分析工具（外置件 cache_tools.py）

> 用户提案（2026-09-02，commit 8f9a6c6）：给定 turn/step，对比该次 LLM 调用与它上一次调用的投影 dump，定位**缓存前缀在哪条消息断裂**——断点段位、字符位置、变化前后窗口。

## 职责

前缀缓存命中掉点时的第一手诊断工具：读两次连续 LLM 调用的投影转储，逐条消息比对找出第一个不同处，回答「这次调用缓存断在哪？」——SYSTEM/人设区、折叠摘要区、历史档位（档1/档2-3/深档）、当前轮第几步、当前轮 user_message。

消费 `session_dir/projections/t{N}_s{M}_{ts}.json`（投影转储 JSON 化后格式，与 [/stats 折线 tooltip 的 t/s 标记](../guides/ops.md#stats-页webui-统计按钮)同源），加载其中 messages（顶层 list 或 `messages` 字段），按 json 全签名逐条对比找第一个不同消息。

## 签名与用法

```python
cache_breakpoint(turn=582, step=2, session_dir="", context_chars=120) -> str
```

- `turn/step`：目标调用轮步，与**它上一次调用**（按 (turn, step) 排序的紧邻前一条）对比
- `step=0`：上一次自动取**上一轮最后一步**（轮边界场景：`t588_s0` 对比 `t587_s9`）
- `session_dir`：显式指定存档目录；留空 → 自动扫 `~/.agt/repos/*/sessions/*/projections` 中**最近活跃**（投影 mtime 最新）的 session（数据根经 `AGT_HOME`/`AGT_DESKTOP` 环境变量解析——桌面版统一数据根适配，2026-09-12 双层对账同步进本 repo 份，commit 971535a）
- `context_chars`：变化前后对比窗口字符数（默认 120；字符 diff 半径 = `max(30, context_chars//2)`）
- 返回纯文本报告，工具 schema 声明 `outputs: raw:string`

## 关键实现（tools/builtin/cache_tools.py，303 行）

- **定位 `_find_projections_dir`**：session_dir 显式优先；否则数据根 `repos` 全局扫最近活跃——数据根经 `AGT_HOME`/`AGT_DESKTOP` 环境变量解析、缺省 `~/.agt`（桌面版统一数据根适配；此前仅随包份有，2026-09-12 md5 对账同步进本 repo 份，commit 971535a）——**零框架依赖** → `agt_register()` 无参即可（不消费 ctx），见 [ctx 注入](tool-externalization.md#ctx-通用上下文注入2026-08commit-fd06c48) 的无参向后兼容形态
- **比对**：`_msg_sig` = `json.dumps(sort_keys)` 全消息签名逐条对比；命中前缀字符量累加进 `cached_chars`
- **段位识别 `_identify_zone`**：
  - 内容特征优先：折叠摘要 markers（折叠摘要/已折叠/结构摘要）；`role=system && idx<3` → SYSTEM/人设区
  - `_find_cur_user_idx`：从后往前找当前轮**裸 user**（跳过 `<system-reminder`/`<retrieval-hint` 前缀与轮内中途补充；启发——轮首 user 前驱须为 system 或 assistant）→ 定位当前轮起点
  - 当前轮区（idx ≥ cur_user）按 assistant(tool_calls) 计数估**步号**（`当前轮步骤·第 N 步的 assistant(tool_calls)/tool 结果`）
  - 历史区（idx < cur_user）按位置比例粗估档位：`ratio>0.7` 近端档1 / `>0.3` 中段档2-3 / 其余远端深档；辅以内容 marker（`第N轮 user` / 步骤省略提示 / 档位 system 标注）
- **字符 diff `_char_diff`**：首不同字符位置 + 半径窗口（截断安全，绝不整条灌出）

## 实测两场景（2026-09-02 开发轮，本 session 真实投影验证）

| 场景 | 结果 | 结论 |
|---|---|---|
| 步内 `t587_s8 → t587_s9` | `messages[0..399]`（400 条 ~859,181 字符）全命中 ✓；断 `messages[400]` = 当前轮步骤·第 8 步 assistant(tool_calls)，字段 `reasoning_content` 变化 | 正常步进的**预期行为**——新增一步总断末尾 |
| 轮边界 `step=0`（`t587_s9 → t588_s0`） | 缓存命中区 ❌ 无——`messages[0]` SYSTEM/人设区第 1,050 字符起变化（远程实例清单消失，comfy8000 断开致人设段内容位移） | **「头部动态内容断缓存」典型场景的直接可视化**——正是 [三区重构](../architecture/context-engine.md) 与 DeepSeek 铁律一（任何 system 变化全断）要治的病 |

## 元组解包修复：_identify_zone tool 分支漏第二项（2026-09-15，commit 3cf55e9，用户实测 t877_s51 抓到）

用户手动 `/call cache_breakpoint({"turn":877,"step":51})` 报 `ValueError: too many values to unpack (expected 2)`——根因在 `_identify_zone` 的 `role == "tool"` 分支：该分支此前 `return` **单个字符串**（函数其余分支均返回 2 元组），调用方 `zone, note = _identify_zone(...)` 对字符串解包 = 按字符流塞两个变量 → 直接炸。

- **修复**（commit 3cf55e9）：tool 分支补元组第二项 `""`——`return (f"当前轮步骤·第 {N} 步的 tool 结果" if step_est > 0 else "当前轮步骤·tool", "")`，代码注释留痕（⚠️ 元组修复）
- **为什么一直没炸**：只有断点消息恰好是「当前轮步骤区的 **tool 结果**」才走该分支；此前分析过的断点全落在 SYSTEM/折叠/档位/assistant(tool_calls) 区——第一次命中即踩雷
- **验证三层**：①单测构造 tool 消息断言返回 2 元组 ✓；②函数全分支 return 扫描，无其它漏网单串 ✓；③原参数重放 `cache_breakpoint(877, 51)` 正常出报告——断点 = messages[359] 当前轮步骤·第 0 步 tool 结果，内容变化为该轮 edit 引发的 llm_client.py recent-file 快照更新（正常断点，非异常断裂）
- **生效方式**：外置件热加载——`/reload tools` 即用（当前进程加载的仍旧版），或下次 `/restart` 自动带新

### 诊断链闭环：断点本身正常，根因 = 全局 detail_step=15 的轮内小毕业（2026-09-15 同日二轮，commit 4ad2812）

用户在元组修复 + 原参数重放（确认 s51 断点 =「正常断点，非异常断裂」）之后点破真正根因：「我知道了，是因为 proxy 的步距衰减是全局的 15，s51 发生了**轮内小毕业**」——proxy 未在模型卡片配 `detail_step`，回落全局 settings 15 → 轮内组边界（≥2 组号差）持续回缩，组边界即轮内缓存断点。用户裁定删除全局字段（**detail_step 只认模型卡片、未填默认 0**，commit `4ad2812`，落地与语义见 [context-engine · 全局 detail_step 字段删除](../architecture/context-engine.md)）。

本工具的诊断价值在此闭环中再次体现：把「缓存断了」定位到**段位坐标**后，才能区分「异常断裂」（工具层 bug）与「配置性常态断裂」（衰减参数）——两类断点对应两种修法，坐标是分流的前提。

## 与其他模块的关系

- **消费方**：projections 转储——[上下文引擎 · 投影转储文件名与 t/s 标记](../architecture/context-engine.md#投影转储文件名与-ts-标记commit-4aced81)（JSON 化格式，commit 2dc64f2 起）；t/s 命名与 [/stats 页](../guides/ops.md#stats-页webui-统计按钮) 折线 tooltip 同源，排障闭环（stats 看到异常点 → 打开 dump → cache_breakpoint 对拍）
- **语义支撑**：[前缀缓存三层优化](../architecture/context-engine.md#前缀缓存三层优化详见-blog03) 与 [DeepSeek v4 缓存实证（system/tools 变化全断三铁律）](../architecture/context-engine.md#deepseek-缓存行为实证v3-位置敏感--v4-system-规范化2026-08-两代后端)——本工具把「缓存断了」从抽象结论变成可定位的**段位坐标**
- **装配**：tools/builtin 外置件，`/reload tools` 热加载即用；随包副本已同步 `src/assets/tools_builtin/cache_tools.py`（2026-09-12 md5 双层对账确认双向一致——此前本 repo 份缺桌面数据根适配、随包份先行，已补齐，见 [双层一致性对账](tool-externalization.md#双层一致性对账workspace-层-vs-assets-层2026-09-12commit-971535a用户提问触发)）

## 注意事项

- 只做**内容签名比对**，不做语义 diff——断点 = 第一个 json 签名不同的消息，其后默认全断（前缀匹配语义，与 [DeepSeek 三铁律](../architecture/context-engine.md) 的缓存模型一致）
- 依赖投影转储存在：scene=react 的 dump 才落盘；非 react scene 或转储关闭 → 无可对比（报错提示可用范围）
- 历史档位只按位置比例**粗估** + 内容 marker，档号非精确（精确档位边界在旁车 `proj_stats.json` 的 `tier_boundaries`，见 [段统计旁车持久化](../architecture/context-engine.md#段统计旁车持久化proj_statsjson--context-三级读取2026-08-29commit-e703c67)）
- `step=0` 的「上一次」= 投影文件序紧邻前一条（上一轮最后一步），与引擎调用序一致；最早的投影无上一次可对比

## 相关页面

- [工具外置体系](tool-externalization.md) —— 外置件清单（12 文件）+ ctx 注入 + 热加载
- [上下文引擎 · 投影与缓存实证](../architecture/context-engine.md) —— 投影转储 / 折叠摘要 / 档位折叠 / DeepSeek 缓存三铁律
- [运维排障 · 轮边界缓存观测](../guides/ops.md) —— /stats 页 t/s 标记、proj_stats.json 旁车
