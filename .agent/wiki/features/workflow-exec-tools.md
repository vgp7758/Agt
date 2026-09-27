# exec_workflow · 工作流成为 LLM 一等执行单元（正式语义 + 超时转后台 + 技能携带工作流）

> 2026-09-27，commits `6fc2a89` + `9633de8`（已推送）。实现在 `src/workflow_debug_tools.py`（264→275 行），工具简介在 `src/tool_briefs.py`。
> 此前 LLM 想跑工作流只有 `debug_workflow`（调试语义：逐节点 trace 回显）——**没有"正式跑一个工作流拿结果"的入口**。本批让工作流升级为与 run_python 同级的一等执行单元。

## 职责：exec 与 debug 的分工

| 工具 | 语义 | 返回 |
|---|---|---|
| `exec_workflow(name, inputs, timeout=0)` | **正式执行**（业务产出） | end 节点输出的 JSON（业务结果）；长流程超时自动转后台 |
| `debug_workflow(name, inputs)`（**已转 hidden**） | 调试伴生（逐节点看输出） | trace 四件套 `list_workflow_outputs` / `eval_node_output` / `hotswap_workflow_node` 继续挂它名下 |

## exec_workflow 语义

```python
exec_workflow(name="pbridge_learn", inputs='{"name":"orders","samples":"@samples.json"}', timeout=0)
# name:   工作流名（不含路径）
# inputs: JSON 字符串（开始节点入参）；值可写 @文件路径 引用 workspace 内文件全文
# timeout: 超时秒数（0=当前工具超时，默认 10s）；超时自动转后台
```

- **超时自动转后台**（与 run_python / run_shell 同一张 bg 表，复用同一套基建）：返回 `[工作流超过 Ns 未完成，已转后台运行（任务ID: bg_…）。完成时会自动推送通知唤醒你…]`——后续 `check_bg_task` 查进度 / 收结果，完成自动唤醒
- 同步完成路径 → 直接返回 end 节点输出 JSON

## 技能携带工作流：`_load_wf_canvas` 二级加载

按名加载 canvas 的查找顺序：

1. 本 repo `.agent/workflows/`（`.json` / `.xml` 都认）——原有路径，优先
2. **已激活技能的 `workflows/`**——新增：技能包可以自带工作流

- 仅 `exec_workflow` / `debug_workflow` 可见——**不进编辑器工作流列表、不参与钩子发现**（保持"Agent 主动调用"与"编辑器管理"两个世界互不干扰）
- 技能携带的工作流是技能 SOP 的一部分：SKILL.md 教 Agent「样本够了就 `exec_workflow('xxx', …)`」

## 首个实例：pbridge_learn（随 pbridge 技能携带）

`pbridge` 技能（proto 学习服务）的四节点确定性流水线：

```
探活桥（skill_equip 未启动则明确提示先 skill_equip('pbridge')）
  → POST /learn（name/root/samples）
  → 解析 warnings / assumed
  → 结构化摘要输出
```

实测 `exec_workflow("pbridge_learn", {"name":"orders","root":"OrderRequest","samples":"@samples.json"})` → **1.9s** 返回 `{ok:true, schema:"ev_demo2", detail:"assumed=0；样本 2 条", warnings:"[]", proto:"syntax = \"proto3\";\nmessage OrderRequest { … }"}`——samples 参数支持 `@samples.json` 引用（`[{plain, data_b64}]` 格式）。

## 三层分工最终形态

```
SKILL（pbridge）          = SOP + 能力入口 + 工作流载体（"样本够了就 exec_workflow('pbridge_learn', …)"）
WORKFLOW（pbridge_learn） = 确定性流水线（探活→学习→摘要，可观测/可复用）
REACT 主循环              = 开放式决策（注入 hook、造流量、看 warnings 决定补样本或收工）
```

技能 SOP 写策略与时机，工作流写确定性步骤，react 循环保留开放判断——三层各司其职。

## 验证（mock agent + 临时工作流，全绿）

1. **同步完成路径**：3 节点工作流 → `{"result": 2}` ✓
2. **超时转后台**：2s 超时工作流 → 返回 bg_id → 9s 后 `check_bg_task` → `finished=True rc=0` 且 output 含完整业务结果 ✓
3. `check_bg_task` 与工作流后台任务同表可查 ✓
4. 技能携带加载：repo 无此工作流、技能 `workflows/` 有 → 正常执行 ✓

收尾：临时测试工作流 `tmp_exec_test.xml` 已删；`tool_briefs.py` 补 `exec_workflow` 简介、`debug_workflow` 简介改为「调试工作流：逐节点看输出（配 list/eval/hotswap 四件套）」。

## 注意事项

- **/restart 生效**：新工具进工具箱、`debug_workflow` 转 hidden 需重启实例（工具 schema 构建期装配）
- exec 走正式执行路径（真实 client、有 llm_calls 记录）——与 debug 路径不同，见 [workflow-debug · debug 全绿 ≠ 真实执行正常](workflow-debug.md) 的教训
- 技能携带的工作流名与 repo 本地同名时**本地优先**（与技能 shadow 语义一致）

## 相关页面

- [工作流调试页](workflow-debug.md)：debug_workflow 的 trace 四件套与画布白框（调试侧视图；注意其 debug 执行路径不产生 llm_calls）
- [技能体系](skills.md)：`skill_equip` / `skill_use`（技能 server.py 常驻服务）——pbridge_learn 探活桥节点即与它配合
- [定时/后台任务](background-scheduler.md)：超时转后台的 bg 表 + check_bg_task + 完成自动通知
- [run_python](run-python.md)：超时转后台基建的首个使用者，exec_workflow 复用同款
- [工作流引擎与钩子](../architecture/workflow-hooks.md)：节点类型与执行引擎
