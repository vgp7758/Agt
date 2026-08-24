# 工具外置 · 判别标准与分层（2026-08-25 定稿）

> 外置的不是"读得到数据的工具"，而是**拥有自己数据的工具**。

## 背景

工具外置第一步（v0.19.0，spec s_25352f88）把 15 个纯函数工具迁到了 `tools/builtin/`。后续讨论"还有哪些能迁"时，最初的口径是"能从磁盘重建状态的 = 低依赖"——后来被修正：**数据的写者才是归属的决定因素**。

## 判别标准（一句话）

> **如果文件系统的某些文件本身就是由这组工具写的、又由这组工具读的 → 对引擎依赖低，可外置；
> 如果文件是引擎写的、工具只是读 → 仍然是对引擎有依赖的工具，外置是伪外置。**

关键：重放能拿到数据 ≠ 独立。外置后 import 依赖没了，但换成**文件格式依赖**——引擎改事件结构（step 加字段、turn_end 改语义）时，外置工具的解析静默失效（解析不出→返回空→上游无察觉）。耦合没消除，只是从显式（import）变成隐式（格式契约），反而更危险。

## 四象限盘点（2026-08-25 全量 21 组）

### ✅ 自写自读——真限界上下文（外置完全自洽）

| 工具组 | 数据文件 | 说明 |
|---|---|---|
| **wiki 六件套** | `.agent/wiki/*.md` | wiki_write 自己写、wiki_read 自己读——引擎从头到尾没碰过 |
| **ltm 五件套** | `repos/<key>/memories/*.jsonl` | add/update/delete_memory 自己写 |
| **download** | `sessions/<name>/images/` | download_asset 自己写 |
| **rag**（大部分） | 向量 `.db` | 索引构建自己写（配置读 config，属轻依赖） |

性质：**文件格式契约归工具组自己**——外置后想改存储结构（wiki 加 frontmatter、ltm 换分段键），工具组自己说了算，引擎升级永远不破坏它。

### ❌ 引擎写、工具读——可观测性出口（永远内置）

| 工具组 | 数据文件 | 写者 |
|---|---|---|
| memory_tools (recall) | `events.jsonl` | 引擎 `_emit` 落盘 |
| toollog (list/get_detail) | `toollog.jsonl` | 引擎工具调度器写 |
| session_tools (history) | `meta.json` + events | 引擎 Session 写 |

它们是引擎的日志消费者，跟着引擎走才是正确位置。

### ⚠️ 半依赖——文件自写但流程归引擎

| 工具组 | 情况 |
|---|---|
| plan/spec CRUD | `plans|specs/*.json` 是工具写的，但 draft→committed→approved 状态机 + threading.Event 阻塞批阅是引擎流程；CRUD 半边可外置，流转半边永远引擎 |
| agent_config | settings.json 是全框架公共契约，不算工具组自有数据 |
| background 查询 | `/api/status` 快照由引擎序列化 |

### 🔒 纯进程内状态——永远内置

restart（work_q 退出哨兵+看门狗协议）/ spec+survey（threading.Event 交互阻塞）/ multiagent 全家（registry 活引用、inbox、push_message、bg 线程）/ wf_*（_emit、utility_client、动态注册）/ workflow_debug（_debug_ctx）。

## A 类：零引擎依赖（连 factory 都不需要）

feedback（纯 HTTP 上报）、agent_config（settings.json 约定读写）、rag 大部分。

## 运行时管理器的替代边界（background/lsp/mcp/reload_hot）

- **查询**有替代：`/api/status` HTTP 快照（仅 WebUI 模式可用）
- **操作**（start/stop/send/注册/挂载）无替代——工具箱本身就是引擎器官，但都是薄注入（一个 agent 参数）

## 双模式先例（代码库里已有三个）

`format_team(session_dir=...)` 磁盘兜底、`agent_query_events` lazy-load `Session.load`、`_restore_subagents` 扫 `agents/` 目录——**有 agent 走活视图，没有走磁盘约定**。外置工具可继承此模式（描述符 make / make_standalone 双工厂）。

## 迁移优先级（按本标准修正后）

1. wiki 六件套 + ltm + download + rag——数据主权本来就在工具组（真外置）
2. factory kind 机制（D 类描述热改收益仍在：工具 docstring 就是 LLM 看的 schema 描述）
3. plan/spec CRUD 半边
4. memory_tools/toollog **不迁**（判别标准下从旧名单划掉）

## 相关页面

- features/tool-externalization.md（外置体系：目录/装配/热加载）
- features/glob-files.md（外置首例演示）
- architecture/node-plugins.md（节点插件化——同一哲学）
