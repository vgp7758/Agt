# .agent/workflows/ — 工作流目录

`.xml`（推荐，画布可直接转 JSON）或 `.json`（Coze 原生画布）两种形态，一文件一工作流。
Agent 框架在对话里执行它们（钩子自动触发 / `exec_workflow` 主动调用），工作流由节点组成：
start / end / code / LLM / http / selector / loop / batch / subworkflow / plugin(调工具箱工具) 等。

## 最小骨架

```xml
<workflow name="my_flow" description="做什么的">
  <node id="100001" type="start"><out name="x" type="string" required="true"/></node>
  <node id="500001" type="code">
    <in name="x" ref="100001.x"/>
    <code><![CDATA[
async def main(args):
    return {"y": args.params["x"] + "!"}
    ]]></code>
    <out name="y" type="string"/>
  </node>
  <node id="900001" type="end"><out name="result" ref="500001.y"/></node>
  <edge from="100001" to="500001"/><edge from="500001" to="900001"/>
</workflow>
```

## 调用与调试

- **正式执行**：`exec_workflow("my_flow", {"x": "hi"})` —— 长流程超时自动转后台（check_bg_task 收结果）
- **调试**：`debug_workflow("my_flow")` → `list_workflow_outputs` / `eval_node_output` / `hotswap_workflow_node` 逐节点看与热替换
- **钩子挂载**：meta `<workflow ... hooks="before_turn">` 或设置页 / agent .yml 的 hooks 段
- `<workflow>` 上可带 `hidden="true"`（不进工具）/ `async="true"`（钩子异步放行）

## 深入

- 完整节点/字段/引用规则：`read_workflow_spec`；官方示例：`read_workflow_demo`
- 可视化编辑器：`/wf`（debug 页可逐节点观测执行）
