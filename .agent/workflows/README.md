# .agent/workflows/ —— Coze 工作流目录

每个工作流是一份 **Coze 原生画布 JSON**（从 Coze Studio 导出或手写），引擎只读不改。

## 文件

- `<名>.json`      —— Coze 画布：`{nodes, edges, versions}`
- `<名>.json.meta` —— 旁车（可选但建议）：
  ```json
  {
    "name": "greet",                       // 工具名 → wf_greet
    "description": "一句话说明做什么",        // Agent 据此判断该不该调用
    "enabled": true,                        // false 则跳过
    "coze_url": "https://www.coze.com/...", // WebUI「🧪工作流」按钮打开的编辑链接
    "inputs": [...]                         // 可选：覆盖开始节点的入参声明
  }
  ```

## 画布约定（Coze Studio 源码确认）

- **开始节点** id 恒为 `100001`（type `"1"`），**结束节点** id 恒为 `900001`（type `"2"`）。
- **工作流入参** = 开始节点的 `data.outputs`（外部输入作为它的"输出"暴露给下游）。
- **工作流出参** = 结束节点的 `data.inputs.inputParameters`；`terminatePlan`：
  - `returnVariables` → 把 inputParameters 求值成键值返回；
  - `useAnswerContent` → 渲染 `content` 模板字符串返回。
- **变量引用**：`value.type` 为 `literal`(字面量/`{{var}}`模板) / `ref`(引用) / `object_ref`(逐字段组装)。
  - `ref` 的 `content`：`{source: "block-output", blockID: "<节点id>", name: "<输出名, 可点号取子字段>"}`，
    或 `{source: "global_variable_app"|"global_variable_system"|"global_variable_user", path: ["变量名"]}`。
- **边**：`{sourceNodeID, targetNodeID, sourcePortID}`；分支节点用 `sourcePortID` 区分出口。

## 已支持的节点 type（随实现进度更新）

| type | 节点 | 状态 |
|---|---|---|
| 1 / 2 | 开始 / 结束 | ✅ |
| 3 | LLM | ✅ |
| 5 / 8 / 15 / 32 / 40 / 58 / 59 | 代码/选择器/文本/聚合/赋值/JSON | 🚧 S2 |
| 21 / 28 | 循环 / 批处理 | 🚧 S3 |
| 22 / 45 / 9 / 4 | 意图/HTTP/子工作流/插件 | 🚧 S4 |

未支持的节点被执行到时会**明确报错**（不静默），便于发现待补的能力。
