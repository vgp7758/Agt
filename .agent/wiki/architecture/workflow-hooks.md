# 工作流引擎与钩子

> src/workflow.py + workflow_xml.py + agent.py(_run_hooks)。节点细节见 [docs/architecture/04-workflow.md](../../../docs/architecture/04-workflow.md)，本页补充钩子链路、async 元信息与快照检测闭环。

## 双格式与热加载

- `.agent/workflows/<名>.xml`（推荐，CDATA 免转义）或 `.json`（Coze 原生画布）
- `.meta` 旁车 / XML 根属性：name/description/**hook**/enabled/**hidden**/**async**/auto/coze_url
- 每轮对话开始自动扫描注册为 `wf_*` 工具；`hidden=true` 不投影给 LLM（钩子/子工作流专用）
- meta.hidden 的 XML 往返已修复（api_wf_get 读根属性）——历史丢 hidden 的文件已补回

## async 元信息字段（2026-08 新，v0.18.2 正式发布）

钩子工作流可标记 `async=true`，使其**异步执行不阻塞主循环**。全链路读写：

| 层 | 文件 | 职责 |
|----|------|------|
| 运行时 | `src/agent.py` `_run_hooks` | 读 meta.async → 若 true 则后台线程执行钩子，主循环不等返回、不注入 inject |
| 引擎 | `src/workflow.py` | WorkflowMeta dataclass 含 `async_` 字段；`run_workflow` 不感知 async（由调用方决定同步/异步） |
| XML 解析 | `src/workflow_xml.py` | XML 根属性 `async="true"` → WorkflowMeta.async_；序列化时写回根属性（往返幂等） |
| API | `src/server.py` | `api_wf_get` / `api_wf_save` 读写 async 字段（与 hidden 同级，根属性往返） |
| 编辑器 | `static/editor.html` | meta 面板 async 复选框；保存时随 meta 一起提交 |

**设计意图**：部分钩子（如通知、日志、后台索引）不需要将结果注入当前轮上下文，同步等待会拖慢主循环响应。async 钩子在后台线程跑完即丢弃返回值（或写日志/副作用文件），主循环零等待。

**注意事项**：
- async 钩子**不参与 inject 注入**——即使返回 `{inject: ...}` 也会被忽略（主循环已继续）
- async 钩子内 LLM 仍走 `utility_client`（scene=hook:xxx），可在 llm_calls.jsonl 观测
- 同一钩子工作流不要同时被 async 和非 async 调用——行为不确定

## 13 类节点速查

start(1)/end(2)/llm(3)/plugin(4)/code(5)/selector(8)/subworkflow(9)/text(15)/loop(21)/intent(22)/batch(28)/aggregator(32)/assigner(40) + tojson/fromjson/http/break/continue/setvar/output。

新能力（2026-08）：
- **selector 左值**：`NODE.field.length`（string 也有）；条件值支持 `changed_files` 数组直传（零序列化）
- **pass_through 工具**（LIGHT_TOOLS）：input=Any（schema 空）→ 编辑器 any 类型不锁，可改 object 逐字段连线组装结构透传
- **starts_with/ends_with**：LIGHT_TOOLS 字符串前后缀判断（扩展名分流）
- **XML schema 往返**：list\<object\> 的 field 子元素 / list 基础类型 itemType / 坐标幂等（编辑器保存不再丢结构）

## 生命周期钩子

| hook | 时机 | 约定返回 | async 可选 |
|------|------|---------|-----------|
| before_turn | 每轮 run 开头（检索注入） | inject+result → system 旁注 | ✅（注入被忽略，仅副作用） |
| before_tool / after_tool | 工具前后（含并行分支） | 同上；after_tool 收 changed_files | ✅ |
| before_answer | 最终回答前（可打回重写） | inject → 重写循环（封顶5次） | ❌（需同步反馈） |
| turn_end | 轮结束（验收） | 同 before_answer（封顶3次） | ✅（仅副作用，不验收） |

钩子内 LLM 走 `utility_client`（scene=hook:xxx 标注）；assembly DSL 关 hooks 则整个 Agent 不跑钩子。

before_turn 实例：**wiki_auto_query**（默认关闭）——三档漏斗（LLM1 意图识别 → wiki 搜索 → LLM2 精排），related=False 短路零搜索零 LLM2；四场景全链路已验证，详见 [features/wiki-auto-query](../features/wiki-auto-query.md)。

## 快照副作用检测（after_tool 闭环）

```
工具前 _workspace_snapshot()（复用上次 after 快照省一半扫描）
  → 工具执行（任何方式改文件都算：edit/run_python/MCP/子Agent）
工具后快照 → _diff_snapshots → changed_files=[{file, change:new|modified|deleted}]（数组直传）
排除：.git/.agent/.agt + .gitignore 全模式 + 嵌套 git 仓库整棵剪枝（性能 15012→124 文件）
```

**py_auto_diag**（随包播种，编辑器可开）：changed_files → loop 遍历 → ends_with(".py") → py_diag（ast+jedi）→ contains 判 ERROR → **改了 .py 必注入**（通过/报错都反馈，非 .py 短路零打扰）。cs_auto_diag 同构（.cs/cs_diag）。旧版 edit/write_file 内联 _py_check 已删除——职责统一由钩子接管。

## 编辑器注意

- 子画布（loop/batch）编辑后保存曾丢内容：根因 exitComposite 的 findN 在子画布找不到父层节点 → 已改从栈顶帧父层查找写回；saveWd 也显式带节点级 blocks/edges
- Ctrl+V 曾失效：按 Ctrl 触发 renderAll 重绘剪断焦点 → 焦点在表单控件时不重绘
- 聚合节点分组类型可选 + 字面量变量按类型出控件（checkbox/number/text）
- meta 面板 async 复选框与 hidden/enabled 同组；保存时随 meta 一起提交（server.py api_wf_save 统一处理）
- 气泡消息交互（展开/折叠）见 [features/bubble-interaction](../features/bubble-interaction.md)

## 相关页面

- [系统总览](overview.md)：模块地图与一轮对话数据流
- [wiki_auto_query](../features/wiki-auto-query.md)：before_turn 钩子实例
- [气泡交互](../features/bubble-interaction.md)：编辑器系统气泡展开/折叠
- [v0.18.2 发布记录](../releases/v0.18.2.md)：async 元信息为本次交付项之一
