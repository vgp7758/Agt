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
| [architecture/workflow-hooks](architecture/workflow-hooks.md) | 工作流引擎 + 生命周期钩子 + async 元信息 + 快照副作用检测 + **changed_calls 变更调用收集（before_answer 透传）** + git_commit 节点 + subworkflow literal 属性约定 | 写工作流 / 加钩子 / async 钩子 / 快照变更 |
| [architecture/snapshot-diff](architecture/snapshot-diff.md) | dir_snapshot / diff_snapshots 通用子工作流：目录快照 + 变更清单生成（files/count/changed）| 需要精确检测目录变更 / 复用快照能力 |
| [features/api-status](features/api-status.md) | /api/status 端点：实例运行时状态快照（18+3 字段），跨实例诊断 | 查运行时状态 / 多实例运维 |
| [features/wiki-auto-maintenance](features/wiki-auto-maintenance.md) | wiki_auto_maintenance：判官 llm → snap_before（dir_snapshot）→ **fmt_calls（变更调用原文渲染）** → update_wiki → diff_wiki（code 拍 after + diff_snapshots）→ commit_wiki（git_commit 节点），自动维护并 git 提交推送 wiki | 改 wiki 维护流程 / 调 commit 节点 |
| [features/wiki-auto-query](features/wiki-auto-query.md) | wiki_auto_query：before_turn 自动 wiki 检索，三档漏斗 + related=False 短路 + 四场景验证 | 开自动检索 / 调钩子工作流 |
| [features/bubble-interaction](features/bubble-interaction.md) | 气泡交互：系统气泡默认折叠点击切换；user/answer 气泡 hover 复制按钮（挂宿主防 innerHTML 重写） | 改前端气泡 / 调交互 |
| [releases/v0.18.2](releases/v0.18.2.md) | v0.18.2 发布记录：唤醒链路根因修复、stdin 通道、/api/status、async 元信息、气泡折叠、wiki 自动提交（提交成功闭环） | 查版本交付内容 / 发布流程 |
| [guides/config-and-models](guides/config-and-models.md) | 配置体系：models.json / settings.json / utility_model / token_rotate | 配模型 / 调优 |
| [guides/ops](guides/ops.md) | 运维与排障：可观测性(/stats/scene/api-status/观测点日志) / 常见错误 / 存档布局 | 查问题 / 看统计 |

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
- **缓存经济模型（commit 1e9af8f）**：轮内零调整，只在轮边界做一次全局重排——先升档到 75% 再折叠到 75%，`_planned_graduates` 记录计划，轮内 `_build` 以 `_planned_fold`/`_planned_graduates` 为起点零调整，保证轮内字节稳定、前缀缓存整段命中（见 [context-engine 轮边界统一计划](architecture/context-engine.md#升档graduate-与折叠轮边界统一计划2026-08commit-1e9af8f)）
- 折叠（fold）设计已实证（t206）：档梯满触发全档折叠，摘要 byte-stable——单步 ~98% miss 后命中率恢复 ~99.9%，一次性成本不破坏后续缓存（见 [context-engine 折叠实证](architecture/context-engine.md#折叠事件与缓存命中t206-实证2026-08)）
- **排障闭环：t{N}·s{M} 轮步标记**（commit 4aced81）：/stats 折线 tooltip 显示 `· t206 · s6`，与 `projections/` 转储文件名同源（`t206_s6_*.txt`）——异常点 hover 即得文件名，直接打开看当时完整投影；仅 scene=react 记录携带，老记录自动省略（见 [ops · /stats 页](guides/ops.md#stats-页webui-统计按钮)、[context-engine · t/s 标记](architecture/context-engine.md#投影转储文件名与-ts-标记commit-4aced81)）
- **v0.18.2 发布**（2026-08-18）：子 Agent 唤醒链路根因修复（registry 为 None → answer 未入队）、stdin 通道验证通过、/api/status 端点、async 元信息字段、气泡折叠、wiki 自动提交（commit_wiki 改 git_commit 节点）、唤醒链路诊断日志埋点（commit e0ae60b）——详见 [v0.18.2 发布记录](releases/v0.18.2.md)
- **wiki 提交失败修复并闭环**（2026-08）：commit_wiki 从 run_shell 改 **git_commit 节点**（subprocess 列表参数规避 shell 多行转义），快照逻辑重构为 **dir_snapshot / diff_snapshots 通用子工作流**（snap_before 打更新前基线 → update_wiki → diff_wiki 拍 after 并 diff 生成变更清单，无变更静默跳过），自动追加 Co-authored-by；**实战成功**（commit 1577693 / 0293eec）——详见 [wiki-auto-maintenance](features/wiki-auto-maintenance.md#提交失败问题2026-08-修复) 与 [快照子工作流](architecture/snapshot-diff.md)
- **变更调用原文收集→before_answer 直供**（2026-08-19，commit 16d6832）：引擎把快照 diff 检出的**有文件变更的工具调用原文**（edit old/new、write content、结果预览[:800]、changed_files）存入 `_turn_changed_calls` 并透传给 before_answer 钩子；wiki_auto_maintenance 新增 fmt_calls 渲染后拼进 update_wiki 任务文本——子 Agent 无需 read_file 重读源文件，显著降低推理负担与子工作流耗时；快照触发条件扩展为 after_tool **或** before_answer 任一钩子在（见 [workflow-hooks · changed_calls](architecture/workflow-hooks.md#changed_calls-变更调用收集before_answer-透传2026-08-19)、[wiki-auto-maintenance · 推理减负](features/wiki-auto-maintenance.md#推理减负changed_calls-直供2026-08-19commit-16d6832)）
- 唤醒链路端到端验证（三阶段）：阶段一 /api/status 跨实例调用通过；阶段二 9100 反复退出根因修正为端口被旧实例占用（已清理，非代码 bug）；阶段三 stdin 通道验证通过、观测点日志已埋，等待首轮完成闭环（见 [multi-agent 端到端验证状态](architecture/multi-agent.md#端到端验证状态2026-08-18三阶段)）

## 维护约定

- 重要功能落地后由 update_wiki（wiki-updater 子 Agent）增量维护
- wiki_auto_maintenance 工作流：判官 llm 判断是否值得维护 → snap_before（**dir_snapshot 子工作流**，对 `.agent/wiki/` 打更新前基线）→ **fmt_calls（code：把引擎透传的 changed_calls 渲染为变更调用原文摘要）** → 拼接模板（user_msg + turn_ctx + 变更调用原文 + answer 草稿）→ update_wiki（plugin，任务文本已含改动原文，子 Agent 一般无需 read_file）→ diff_wiki（**code 节点**：拍 after + 调 **diff_snapshots 子工作流**生成变更清单）→ has_changes?（count>0）→ build_msg（text，注入判官摘要）→ commit_wiki（**git_commit 节点**，列表参数规避 shell 多行转义，按清单 add/commit/push，无变更跳过，自动追加 Co-authored-by）——详见 [wiki-auto-maintenance](features/wiki-auto-maintenance.md)
- wiki 页面可自由链接 docs/ 与源码相对路径
- 组织原则：按【业务/技术逻辑】，不镜像仓库目录
