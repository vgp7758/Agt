# 工作流引擎与钩子

> src/workflow.py + workflow_xml.py + agent.py(_run_hooks)。节点细节见 [docs/architecture/04-workflow.md](../../../docs/architecture/04-workflow.md)，本页补充钩子链路与快照检测闭环。

## 双格式与热加载

- `.agent/workflows/<名>.xml`（推荐，CDATA 免转义）或 `.json`（Coze 原生画布）
- `.meta` 旁车 / XML 根属性：name/description/**hook**/enabled/**hidden**/auto/coze_url
- 每轮对话开始自动扫描注册为 `wf_*` 工具；`hidden=true` 不投影给 LLM（钩子/子工作流专用）
- meta.hidden 的 XML 往返已修复（api_wf_get 读根属性）——历史丢 hidden 的文件已补回

## 13 类节点速查

start(1)/end(2)/llm(3)/plugin(4)/code(5)/selector(8)/subworkflow(9)/text(15)/loop(21)/intent(22)/batch(28)/aggregator(32)/assigner(40) + tojson/fromjson/http/break/continue/setvar/output。

新能力（2026-08）：
- **selector 左值**：`NODE.field.length`（string 也有）；条件值支持 `changed_files` 数组直传（零序列化）
- **pass_through 工具**（LIGHT_TOOLS）：input=Any（schema 空）→ 编辑器 any 类型不锁，可改 object 逐字段连线组装结构透传
- **starts_with/ends_with**：LIGHT_TOOLS 字符串前后缀判断（扩展名分流）
- **XML schema 往返**：list\<object\> 的 field 子元素 / list 基础类型 itemType / 坐标幂等（编辑器保存不再丢结构）

## 生命周期钩子

| hook | 时机 | 约定返回 |
|------|------|---------|
| before_turn | 每轮 run 开头（检索注入） | inject+result → system 旁注 |
| before_tool / after_tool | 工具前后（含并行分支） | 同上；after_tool 收 changed_files |
| before_answer | 最终回答前（可打回重写） | inject → 重写循环（封顶5次） |
| turn_end | 轮结束（验收） | 同 before_answer（封顶3次） |

钩子内 LLM 走 `utility_client`（scene=hook:xxx 标注）；assembly DSL 关 hooks 则整个 Agent 不跑钩子。

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
