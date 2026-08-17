# Agt 知识库导航

> An agent framework that builds itself —— 本仓库绝大多数迭代由 Agt 用自己的工具完成。
> 本 wiki 是【当前架构的浓缩知识库】：导航 + 设计意图 + 关键决策。
> 更完整的模块级细节见 [docs/architecture/](../../../docs/architecture/)（6 份正式架构文档）。

## 地图

| 页面 | 内容 | 什么时候看 |
|------|------|-----------|
| [architecture/overview](architecture/overview.md) | 系统总览：模块地图 + 一轮对话的完整数据流 | 新人入门 / 找模块归属 |
| [architecture/context-engine](architecture/context-engine.md) | 分层上下文引擎：分档投影 + 毕业升档 + 分组衰减 + 前缀缓存三层优化 | 改投影/token 优化 |
| [architecture/multi-agent](architecture/multi-agent.md) | 多 Agent 体系：registry + 通信 + reuse/复活 + assembly DSL + system_append | 派子 Agent / 改协作机制 |
| [architecture/workflow-hooks](architecture/workflow-hooks.md) | 工作流引擎 + 生命周期钩子 + async 元信息 + 快照副作用检测（py_auto_diag 闭环） | 写工作流 / 加钩子 / async 钩子 |
| [features/wiki-auto-query](features/wiki-auto-query.md) | wiki_auto_query：before_turn 自动 wiki 检索，三档漏斗 + related=False 短路 + 四场景验证 | 开自动检索 / 调钩子工作流 |
| [features/bubble-interaction](features/bubble-interaction.md) | 气泡交互：系统气泡默认折叠、用户气泡默认展开、点击切换 | 改前端气泡 / 调交互 |
| [guides/config-and-models](guides/config-and-models.md) | 配置体系：models.json / settings.json / utility_model / token_rotate | 配模型 / 调优 |
| [guides/ops](guides/ops.md) | 运维与排障：可观测性(/stats/scene) / 常见错误 / 存档布局 | 查问题 / 看统计 |

## 快速事实（2026-08 状态）

- 版本 0.18.x；`pip install agt-agent`；CLI=`agt`，WebUI=`agt-web`
- 38 个 Python 模块 ~17000 行，零 LangChain 依赖
- 主 Agent id=`_main_`；子 Agent 声明在 `.agent/agents/*.md`（frontmatter DSL）
- 工作流：`.agent/workflows/*.xml|json`，13 类节点，XML 为推荐写作格式
- 工作流 meta 字段：name/description/hook/enabled/hidden/async/auto/coze_url（全链路读写）
- 存档：`~/.agt/repos/<fixed-cwd>/`（sessions/memories/plans/specs/images/rag）
- LLM 调用流水：每 session `llm_calls.jsonl`（含 resp_model/scene/usage 归一化）
- 前端气泡：系统自动触发默认折叠，用户指令默认展开，点击切换

## 维护约定

- 重要功能落地后由 update_wiki（wiki-updater 子 Agent）增量维护
- wiki 页面可自由链接 docs/ 与源码相对路径
- 组织原则：按【业务/技术逻辑】，不镜像仓库目录
