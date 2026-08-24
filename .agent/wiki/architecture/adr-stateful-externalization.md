# ADR：有状态系统（plan/spec 类）外置评估——否决，保持 build-in

> 状态：已裁决（2026-08-25）｜裁决人：用户｜讨论上下文：工具外置/节点插件化完成后的自然延伸提案

## 背景

工具外置（`tools/builtin` + `agt_register()`）与节点插件化（`nodes_builtin` + py/js 配对）完成后，
一个自然的延伸问题：**plan/spec 这类"有状态的系统"是否也能外置成插件**（.py 执行层 + .js 前端渲染控件）？
难点被明确定位为：引擎需要定义一个可拓展多种自定义系统的 SDK。

## 分析：有状态系统的五个触点

plan/spec 的"状态"不是一份 JSON 文件，散落在引擎五个面：

| 触点 | 现状实现 | 本质 |
|---|---|---|
| 内存态 | `agent.active_spec` / `active_plan` | Agent 生命周期级单例 |
| SYSTEM 注入 | `session._spec_provider = _spec_system_block` | **每轮改写 Agent 的决策上下文** |
| 持久化/恢复 | `capture_runtime_state` → `extra_state` → 读档 restore | 随 session 存档迁移 |
| 阻塞交互 | `commit_spec` 内 `threading.Event().wait()`；WS/CLI `resolve` | 跨线程/进程的流程控制 |
| 前端渲染 | spec_pending 气泡（通过/返工按钮内嵌对话流） | 宿主 UI 能力，非通用控件 |
| 跨系统引用 | spec approve → 自动建 plan | 业务级强耦合 |

核心判断：

> **工具是 Agent 的「手」，有状态系统是 Agent 的「记事本」。**
> 手是无状态的、用完即弃；记事本要持续存在于 Agent 的认知里、随存档迁移、
> 还能让 Agent 停下来等用户签字。外置记事本的 SDK，本质是定义
> "如何安全地往 Agent 的认知上下文塞东西、并让它跨界等回应"的协议——
> 开放它等于说"第三方可以定义 Agent 的记忆与待办"。

工具外置判据（"文件谁写就归谁"）在这里失效：它判定的是**数据归属**，
而 plan/spec 的耦合在**认知归属**（SYSTEM 注入改写每轮决策上下文）——比数据层深一层。

## 替代路线评估：MCP 化——三条不可调和的矛盾

| # | 矛盾 | 展开 |
|---|---|---|
| ① | **阻塞批阅 vs 工具 timeout** | 就算调到 7200s，MCP 的"请求→结果"模型也表达不了"结果决定分支"——approve/reject 决定 Agent 接下来建 plan 还是 regenerate，超时转后台解决"结果晚到"解决不了"结果控制流"。需要引擎级挂起原语，MCP 协议没有 |
| ② | **MCP server 自开批阅页面 vs 手机端** | PC 上弹新 tab 勉强可用；手机浏览器 tab 切换是灾难，批阅完还要手动切回。spec_pending 气泡体验好的根源是**长在对话流里**——宿主 UI 能力，MCP server 够不着 |
| ③ | **读档生命周期** | MCP server 独立进程的 pending 状态与 agt 的 session 存档没有共同事务边界：agt 被 /restart 杀掉后，MCP 侧 pending 无人问津（或反之）。`extra_state` 持久化是引擎器官，跨进程无法复刻 |

SYSTEM 注入是唯一 MCP 可行的面：assembly DSL 的 `- tool:` 动作项已支持
（如 `- tool: __mcp_spectoolkit_get_spec()`，wiki-updater 的 `wiki_tree()` 是先例）。

## 裁决

**否决外置，保持 build-in。**（"太复杂风险很高，搞这么费劲不如 MCP，而 MCP 又有上述矛盾，总之保持 build-in"）

## 留下的渐进折中（未来如需再动）

如果哪天需要部分松动：**只把数据读取面 MCP 化**（get_spec/list_specs 等只读查询，
经 assembly `tool:` 注入或普通工具调用），**流转面**（commit/approve/reject + 阻塞 + 恢复）
永远 build-in——与工具外置判别标准中"plan/spec 的 CRUD 半边可外置、流转半边永远引擎"同款切分。

## 关联

- `architecture/tool-externalization-criteria.md`——工具外置判据（数据归属）及其边界（认知归属）
- `features/tool-externalization.md`——工具外置实施
- `architecture/workflow-hooks.md`——节点插件化
