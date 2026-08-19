# Agt 知识库导航

> An agent framework that builds itself —— 本仓库绝大多数迭代由 Agt 用自己的工具完成。
> 本 wiki 是【当前架构的浓缩知识库】：导航 + 设计意图 + 关键决策。
> 更完整的模块级细节见 [docs/architecture/](../../../docs/architecture/)（6 份正式架构文档）。

## 地图

| 页面 | 内容 | 什么时候看 |
|------|------|-----------|
| [architecture/overview](architecture/overview.md) | 系统总览：模块地图 + 一轮对话的完整数据流 | 新人入门 / 找模块归属 |
| [architecture/context-engine](architecture/context-engine.md) | 分层上下文引擎：分档投影 + 轮边界统一重排（升档+折叠）+ 分组衰减 + 折叠实证 + 前缀缓存三层优化 | 改投影/token 优化 |
| [architecture/multi-agent](architecture/multi-agent.md) | 多 Agent 体系：registry + 通信 + reuse/复活 + assembly DSL + system_append + 唤醒链路验证状态与观测点 | 派子 Agent / 改协作机制 |
| [architecture/workflow-hooks](architecture/workflow-hooks.md) | 工作流引擎 + 生命周期钩子 + async 元信息 + **运行观测（run registry 接入点 + 节点全文预算）** + 快照副作用检测 + **changed_calls 变更调用收集（before_answer 透传）** + git_commit 节点 + subworkflow literal 属性约定 + **LIGHT_TOOLS 隐藏工具（diff_lines/get_list_item/pass_through）** + **run_python args 参数** | 写工作流 / 加钩子 / async 钩子 / 快照变更 |
| [architecture/snapshot-diff](architecture/snapshot-diff.md) | dir_snapshot / diff_snapshots 通用子工作流：目录快照 + 变更清单生成（files/count/changed）| 需要精确检测目录变更 / 复用快照能力 |
| [features/wf-monitor](features/wf-monitor.md) | 工作流运行观测：run registry（线程安全，最近 50 次）+ /wf/monitor 实时节点甘特时间线（对话中「执行中」行可点击）+ **节点全文 text/plain 纯文本路由（单节点 200K / 总预算 20M）** | 看工作流跑到哪 / 调钩子卡点 / 看节点完整输出 |
| [features/api-status](features/api-status.md) | /api/status 端点：实例运行时状态快照（18+3 字段），跨实例诊断 | 查运行时状态 / 多实例运维 |
| [features/user-interaction](features/user-interaction.md) | 用户交互：插话机制与消息路由（步边界注入 / answer 后 inbox+pending_messages 双队列兜底自动开轮）+ 并行钩子「执行中」UI Map 跟踪（行可点击观测）+ 实测 8 条现象对照 | 改插话 / 消息队列 / 钩子 UI 状态 |
| [features/wiki-auto-maintenance](features/wiki-auto-maintenance.md) | wiki_auto_maintenance：判官 llm → snap_before（dir_snapshot）→ **fmt_calls（变更调用原文渲染）** → update_wiki → diff_wiki（code 拍 after + diff_snapshots）→ commit_wiki（git_commit 节点），自动维护并 git 提交推送 wiki | 改 wiki 维护流程 / 调 commit 节点 |
| [features/wiki-auto-query](features/wiki-auto-query.md) | wiki_auto_query：before_turn 自动 wiki 检索，三档漏斗 + related=False 短路 + 四场景验证 | 开自动检索 / 调钩子工作流 |
| [features/bubble-interaction](features/bubble-interaction.md) | 气泡交互：系统气泡默认折叠点击切换；user/answer 气泡 hover 复制按钮（挂宿主防 innerHTML 重写） | 改前端气泡 / 调交互 |
| [features/diff-files](features/diff-files.md) | diff_files 工具：Myers Diff 对比两文件，unified 风格 hunk 输出（沙箱路径 / 读写不对称 / hunk 分组 / **range_a/range_b 分段对比** / 回溯层错位 bug 教训） | 需要行级文件对比 / 大文件分段精比 / 复查 diff 算法 |
| [features/diff-lines](features/diff-lines.md) | diff_lines 工具（LIGHT_TOOLS，hidden）：Myers Diff 对比两个文本块，unified 风格 hunk 输出（无需落盘，与 diff_files 共享渲染） | 工作流节点间文本比较 |
| [features/get-list-item](features/get-list-item.md) | get_list_item 工具（LIGHT_TOOLS）：从列表取单个元素，支持正/负索引、越界安全、outputs=any | 工作流列表操作 |
| [features/run-python](features/run-python.md) | run_python 工具：code/file 双模式子进程执行，args 参数化（PY_ARGS 环境变量注入，与 run_script PAYLOAD 同机制），流式输出+心跳 | 写脚本工具 / 参数化复用脚本 |
| [releases/v0.18.2](releases/v0.18.2.md) | v0.18.2 发布记录：唤醒链路根因修复、stdin 通道、/api/status、async 元信息、气泡折叠、wiki 自动提交（提交成功闭环） | 查版本交付内容 / 发布流程 |
| [guides/config-and-models](guides/config-and-models.md) | 配置体系：models.json / settings.json / utility_model / token_rotate | 配模型 / 调优 |
| [guides/ops](guides/ops.md) | 运维与排障：可观测性(/stats/scene/api-status/**wf-monitor 实时观测+节点全文**/观测点日志) / 常见错误 / 存档布局 | 查问题 / 看统计 |

## 快速事实（2026-08 状态）

- 版本 **0.18.2**；`pip install agt-agent`；CLI=`agt`，WebUI=`agt-web`
- 38 个 Python 模块 ~17000 行，零 LangChain 依赖
- 主 Agent id=`_main_`；子 Agent 声明在 `.agent/agents/*.md`（frontmatter DSL）
- 工作流：`.agent/workflows/*.xml|json`，13 类节点，XML 为推荐写作格式
- 工作流 meta 字段：name/description/hook/enabled/hidden/async/auto/coze_url（全链路读写）
- 存档：`~/.agt/repos/<fixed-cwd>/`（sessions/memories/plans/specs/images/rag）
- LLM 调用流水：每 session `llm_calls.jsonl`（含 resp_model/scene/turn·step 轮步标记/usage 归一化）
- 前端气泡：系统自动触发默认折叠，用户指令默认展开，点击切换；聊天面板 user/answer 气泡 hover 浮现复制按钮（innerText 复制，clipboard→execCommand 降级，commit 3a7e9de）
- 运行时状态：POST `/api/status` 返回实例快照（18 顶层字段 + 3 嵌套数组），用于跨实例诊断
- **工作流运行实时观测**（2026-08-20，commit 8aeb21a）：进程内 run registry（`_WF_RUNS`，线程安全，最近 50 次）记录每次工作流执行的节点 start/end/error 事件；对话中「⏳ 执行中…」行可点击 → `/wf/monitor?run=<id>` 节点甘特时间线 2s 轮询；同步/async 钩子 + wf_* 工具三路径全覆盖（详见 [wf-monitor](features/wf-monitor.md)）
- **观测页节点全文查看**（2026-08-20，commit bb56a82）：节点预览截 200 字，`has_full` 时预览可点击（📄）→ 新标签打开 `GET /api/wf/runs/<id>/node/<nid>` **text/plain 纯文本页**（页面文本即节点完整输出，非 HTML 无样式）；`_full_str` 保留换行/JSON 结构，单节点 200K 截断标注，总预算 20M 字符防爆内存（耗尽只存预览）；轮询视图剥离 full 只传 has_full（详见 [wf-monitor · 节点全文查看](features/wf-monitor.md#节点全文查看2026-08-20commit-bb56a82)）
- **缓存经济模型（commit 1e9af8f）**：轮内零调整，只在轮边界做一次全局重排——先升档到 75% 再折叠到 75%，`_planned_graduates` 记录计划，轮内 `_build` 以 `_planned_fold`/`_planned_graduates` 为起点零调整，保证轮内字节稳定、前缀缓存整段命中（见 [context-engine 轮边界统一计划](architecture/context-engine.md#升档graduate-与折叠轮边界统一计划2026-08commit-1e9af8f)）
- 折叠（fold）设计已实证（t206）：档梯满触发全档折叠，摘要 byte-stable——单步 ~98% miss 后命中率恢复 ~99.9%，一次性成本不破坏后续缓存（见 [context-engine 折叠实证](architecture/context-engine.md#折叠事件与缓存命中t206-实证2026-08)）
- **排障闭环：t{N}·s{M} 轮步标记**（commit 4aced81）：/stats 折线 tooltip 显示 `· t206 · s6`，与 `projections/` 转储文件名同源（`t206_s6_*.txt`）——异常点 hover 即得文件名，直接打开看当时完整投影；仅 scene=react 记录携带，老记录自动省略（见 [ops · /stats 页](guides/ops.md#stats-页webui-统计按钮)、[context-engine · t/s 标记](architecture/context-engine.md#投影转储文件名与-ts-标记commit-4aced81)）
- **v0.18.2 发布**（2026-08-18）：子 Agent 唤醒链路根因修复（registry 为 None → answer 未入队）、stdin 通道验证通过、/api/status 端点、async 元信息字段、气泡折叠、wiki 自动提交（commit_wiki 改 git_commit 节点）、唤醒链路诊断日志埋点（commit e0ae60b）——详见 [v0.18.2 发布记录](releases/v0.18.2.md)
- **wiki 提交失败修复并闭环**（2026-08）：commit_wiki 从 run_shell 改 **git_commit 节点**（subprocess 列表参数，commit message 多行安全），配合 dir_snapshot / diff_snapshots 生成变更清单，提交成功率从 0% → 100%
- **LIGHT_TOOLS 隐藏工具增强**（2026-08，commit 9fb00de）：新增 `diff_lines`（文本级 Myers diff，与 diff_files 共享渲染）、`get_list_item`（列表元素取值，outputs=any，越界安全），配合 plugin 节点工作流节点间文本比较/列表操作无需落盘
- **run_python 工具新增 args 参数**（2026-08，commit 9fb00de）：`run_python(code="...", file="...", args="...")`，经环境变量 `PY_ARGS` 传递（code 和 file 两模式都生效），脚本内 `import os; a = os.environ.get("PY_ARGS", "")` 读取，让已保存脚本可参数化复用（详见 [run_python 页](features/run-python.md)）
- **diff_files 读放行（读写不对称）**（2026-08，commit 9fb00de）：新增 `_resolve_read`，越界（绝对路径 / `../` 逃逸）放行为直接路径——现在可以对比 workspace 外的备份/参照文件，写操作仍走严格沙箱
- **diff_files 分段对比 range_a/range_b**（2026-08-20，commit 096fcbe）：大文件截断（20k）时先全文看大致范围再逐段精比；只传 range_a 时 range_b 默认同值，两文件行号错位各传各的；**输出行号仍为文件内绝对行号**（`_render_unified_diff` 加 a_offset/b_offset 还原），diff 结果可直接喂 edit/replace_lines。附教训：`_parse_range` 成功返回 (a,b) 元组被 `if err:` 当 truthy 错误——错误通道只放错误（详见 [diff-files 页](features/diff-files.md#分段对比range_arange_b2026-08-20新commit-096fcbe)）
- **执行时序修复：插话不滞留 + 并行钩子 UI**（2026-08-19，commit fb115aa，用户实测 8 条现象闭环）：① answer 完成后旧版只查 inbox、漏 pending_messages（插话队列）→ 插话滞留至用户下次发消息；现 inbox 空时兜底消费插话队列，自动 `background_trigger·user_insert` 开新轮（新轮 before_turn 检索的即插话内容）。② 同 hook 挂多个 before_turn 工作流时，前端「执行中」行由单变量改 Map 按 `hook::name` 索引，互不覆盖（详见 [user-interaction](features/user-interaction.md)）

## 维护约定

- 新功能落 wiki：features/ 目录下按功能建页，home.md 加入口
- 架构变动：architecture/ 目录下对应页更新，home.md 同步
- 版本发布：releases/ 目录下 v*.*.*.md 记录，home.md 快速事实同步
- 修复闭环：对应页修复说明 + releases/ 发布记录
- 工具增强：features/ 新工具页 + workflow-hooks 更新 LIGHT_TOOLS 章节
