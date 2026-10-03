# Agt 文档索引

> **Agt**：自己开发自己的 AI Agent 框架——多模型 ReAct + 分档缓存友好上下文 + 异步多 Agent + XML 工作流。CLI & WebUI，零 LangChain。
> 主文档入口 · [返回 README](../README.md)

## 快速导航

| 想了解 | 去哪 |
|---|---|
| **架构总览**（引擎/会话/工具/工作流/Web/扩展六篇） | [docs/architecture/](architecture/) |
| **工作流 XML 规范**（节点类型/字段/变量引用，写工作流前必读） | [workflow-spec.md](workflow-spec.md) |
| **外部事件注入**（脚本/服务/其它机器经 HTTP 向实例推消息/文件） | [external-injection.md](external-injection.md) |
| **知识库**（部署后 `.agent/wiki/`，含实战演化的最新设计） | 见仓库根 `.agent/wiki/` |
| **上手指引**（安装/配置/启动） | [README](../README.md#快速开始) |

## 架构六篇

1. [01-核心引擎](architecture/01-core-engine.md) — ReAct 主循环、模型回退链、子 Agent 调度
2. [02-会话与记忆](architecture/02-session-memory.md) — 分档投影、毕业压缩、三级记忆、投影转储
3. [03-工具体系](architecture/03-tools.md) — 内置工具、MCP 接入、外置工具、审批机制
4. [04-工作流引擎](architecture/04-workflow.md) — XML↔画布、节点插件化、钩子工作流、调试/观测
5. [05-Web 与命令](architecture/05-web-commands.md) — WebUI/WS 广播、命令体系、设置持久化
6. [06-扩展机制](architecture/06-extensions.md) — 节点/工具/技能/子 Agent 声明式扩展、随包资产

## 文档时效说明

架构六篇为 v0.1x 时代定稿的快照级长文；`.agent/wiki/`（项目部署后自动生成）记录
了此后所有设计演进（上下文引擎/缓存经济学/多实例组网等），两者互补——
**看设计哲学去 docs/，查最新行为去 wiki/**。
