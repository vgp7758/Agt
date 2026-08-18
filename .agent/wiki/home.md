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
| [architecture/workflow-hooks](architecture/workflow-hooks.md) | 工作流引擎 + 生命周期钩子 + async 元信息 + 快照副作用检测（py_auto_diag 闭环）+ git_commit 节点 | 写工作流 / 加钩子 / async 钩子 / 快照变更 |
| [architecture/snapshot-diff](architecture/snapshot-diff.md) | dir_snapshot / diff_snapshots 通用子工作流：目录快照 + 变更清单生成（files/count/changed） | 需要精确检测目录变更 / 复用快照能力 |
| [features/api-status](features/api-status.md) | /api/status 端点：实例运行时状态快照（18+3 字段），跨实例诊断 | 查运行时状态 / 多实例运维 |
| [features/wiki-auto-maintenance](features/wiki-auto-maintenance.md) | wiki_auto_maintenance：update_wiki → build_commit_msg → snap_before（dir_snapshot）→ diff_wiki（diff_snapshots）→ commit_wiki（git_commit 节点），自动维护并 git 提交推送 wiki | 改 wiki 维护流程 / 调 commit 节点 |
| [features/wiki-auto-query](features/wiki-auto-query.md) | wiki_auto_query：before_turn 自动 wiki 检索，三档漏斗 + related=False 短路 + 四场景验证 | 开自动检索 / 调钩子工作流 |
| [features/bubble-interaction](features/bubble-interaction.md) | 气泡交互：系统气泡默认折叠、用户气泡默认展开、点击切换 | 改前端气泡 / 调交互 |
| [releases/v0.18.2](releases/v0.18.2.md) | v0.18.2 发布记录：唤醒链路根因修复、stdin 通道、/api/status、async 元信息、气泡折叠、wiki 自动提交（含提交失败修复、快照子工作流重构） | 查版本交付内容 / 发布流程 |
| [guides/config-and-models](guides/config-and-models.md) | 配置体系：models.json / settings.json / utility_model / token_rotate | 配模型 / 调优 |
| [guides/ops](guides/ops.md) | 运维与排障：可观测性(/stats/scene/api-status/观测点日志) / 常见错误 / 存档布局 | 查问题 / 看统计 |

## 快速事实（2026-08 状态）

- 版本 **0.18.2**；`pip install agt-agent`；CLI=`agt`，WebUI=`agt-web`
- 38 个 Python 模块 ~17000 行，零 LangChain 依赖
- 主 Agent id=`_main_`；子 Agent 声明在 `.agent/agents/*.md`（frontmatter DSL）
- 工作流：`.agent/workflows/*.xml|json`，13 类节点，XML 为推荐写作格式
- 工作流 meta 字段：name/description/hook/enabled/hidden/async/auto/coze_url（全链路读写）
- 存档：`~/.agt/repos/<fixed-cwd>/`（sessions/memories/plans/specs/images/rag）
- LLM 调用流水：每 session `llm_calls.jsonl`（含 resp_model/scene/usage 归一化）
- 前端气泡：系统自动触发默认折叠，用户指令默认展开，点击切换
- 运行时状态：POST `/api/status` 返回实例快照（18 顶层字段 + 3 嵌套数组），用于跨实例诊断
- **缓存经济模型（commit 1e9af8f）**：轮内零调整，只在轮边界做一次全局重排——先升档到 75% 再折叠到 75%，`_planned_graduates` 记录计划，轮内 `_build` 以 `_planned_fold`/`_planned_graduates` 为起点零调整，保证轮内字节稳定、前缀缓存整段命中（见 [context-engine 轮边界统一计划](architecture/context-engine.md#升档graduate-与折叠轮边界统一计划2026-08commit-1e9af8f)）
- 折叠（fold）设计已实证（t206）：档梯满触发全档折叠，摘要 byte-stable——单步 ~98% miss 后命中率恢复 ~99.9%，一次性成本不破坏后续缓存（见 [context-engine 折叠实证](architecture/context-engine.md#折叠事件与缓存命中t206-实证2026-08)）
- **v0.18.2 发布**（2026-08-18）：子 Agent 唤醒链路根因修复（registry 为 None → answer 未入队）、stdin 通道验证通过、/api/status 端点、async 元信息字段、气泡折叠、wiki 自动提交（build_commit_msg + commit_wiki）、唤醒链路诊断日志埋点（commit e0ae60b）——详见 [v0.18.2 发布记录](releases/v0.18.2.md)
- **wiki 提交失败修复**（2026-08）：commit_wiki 从 run_shell 改 **git_commit 节点**（subprocess 列表参数规避 shell 多行转义），快照逻辑重构为 **dir_snapshot / diff_snapshots 通用子工作流**（对 `.agent/wiki/` 打快照 → 对比生成变更清单，无变更静默跳过），自动追加 Co-authored-by——详见 [wiki-auto-maintenance](features/wiki-auto-maintenance.md#提交失败问题2026-08-修复) 与 [快照子工作流](architecture/snapshot-diff.md)
- 唤醒链路端到端验证（三阶段）：阶段一 /api/status 跨实例调用通过；阶段二 9100 反复退出根因修正为端口被旧实例占用（已清理，非代码 bug）；阶段三 stdin 通道验证通过、观测点日志已埋，等待首轮完成闭环（见 [multi-agent 端到端验证状态](architecture/multi-agent.md#端到端验证状态2026-08-18三阶段)）

## 维护约定

- 重要功能落地后由 update_wiki（wiki-updater 子 Agent）增量维护
- wiki_auto_maintenance 工作流在 update_wiki 后接 build_commit_msg（text 节点，将报告摘要注入 commit message）→ snap_before（**dir_snapshot 子工作流**，对 `.agent/wiki/` 打快照）→ diff_wiki（**diff_snapshots 子工作流**，对比快照生成变更清单）→ commit_wiki（**git_commit 节点**，列表参数规避 shell 多行转义，自动追加 Co-authored-by），按清单 git add/commit/push `.agent/wiki/`，每条提交携带具体内容摘要——详见 [wiki-auto-maintenance](features/wiki-auto-maintenance.md)
- wiki 页面可自由链接 docs/ 与源码相对路径
- 组织原则：按【业务/技术逻辑】，不镜像仓库目录
