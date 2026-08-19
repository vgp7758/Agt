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
| 编辑器 | `src/static/workflow_editor.html` | meta 面板 async 复选框；保存时随 meta 一起提交 |

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
- **XML schema 往返**：list\<object> 的 field 子元素 / list 基础类型 itemType / 坐标幂等（编辑器保存不再丢结构）
- **git_commit 节点**：git 专用提交节点，内部以 **subprocess 列表参数**传参（不经 shell 字符串拼接），多行/特殊字符 commit message 安全；配合快照/diff 子工作流按变更清单提交。实例见 [wiki_auto_maintenance 的 commit_wiki](../features/wiki-auto-maintenance.md#commit_wiki-核心逻辑git_commit-节点)
- **dir_snapshot / diff_snapshots 子工作流**：引擎级快照能力——`dir_snapshot(path)` 对目录取文件快照（mtime 映射 JSON，排除 .git/__pycache__，path 留空=整个 workspace）；`diff_snapshots(before, after)` 对比两份快照输出变更清单（`files` 逗号分隔 + `count` + `changed` 结构化对象），供 git_commit 或选择器/聚合节点消费。**通用复用**：详见 [dir_snapshot / diff_snapshots 通用子工作流](snapshot-diff.md)，首个消费方为 [wiki_auto_maintenance 的快照重构](../features/wiki-auto-maintenance.md#snap_before--diff_wiki快照与变更清单重构为子工作流2026-08)
- **subworkflow 节点 literal 属性约定（2026-08）**：subworkflow(9) 节点调用子工作流传字面量参数时，**literal 必须用属性形式 `literal="值"`**，不能用于子元素形式（`<literal>值</literal>`）。子元素形式会导致参数传递失败（子工作流收不到字面量）→ path 空 → 快照全盘 → WinError 206。实例：wiki_auto_maintenance 的 snap_before 传 `path=".agent/wiki/"`（见 [快照重构中的 literal 坑](../features/wiki-auto-maintenance.md#subworkflow-节点-literal-属性坑2026-08-修复)）
- **工具节点输出是 dict（2026-08 修复，commit edd9851）**：`wf_diff_snapshots` 等**工具节点** raw 返回 **dict**（Tool.run() 把 end 的 dict json.dumps 成字符串 → `_handle_plugin._try_parse` 又解析回 Python dict），消费端必须引用**具体字段**（`files` / `count` / `changed`，`_dotted_get` 直接取）并**补 `<out>` 声明**；把整个 dict 当字符串 `.split(",")` 会报 `'dict' object has no attribute 'split'`。实例见 [wiki_auto_maintenance 的 dict split 修复](../features/wiki-auto-maintenance.md#dict-split-报错修复2026-08commit-edd9851)
- **subworkflow 节点输入框字面量保存后重开变空（2026-08 修复，commit 910fc1b）**：type 9 节点的输入框（画布节点内，非右侧属性面板）手动输入字面量保存后，重新打开工作流输入框变空——**根因在前端 `syncSubworkflowNode`**（openWf 时对 type 9 节点重跑 schema 同步），只保留连线且默认值造出 `blockID=''` 的空 ref → `makeInputControl` 的 `cur=(v?.type==='literal')?v.content:''` 恒空 → 输入框空白。修复后连线 ref 完整保留 + 非空字面量保留；默认值改空 literal（可编辑空输入框）。**生效需 `/restart` + Ctrl+F5 强刷编辑器**。实例见 [wiki_auto_maintenance 的 path 字面量保存问题](../features/wiki-auto-maintenance.md#path-字面量保存后重开变空2026-08-修复-commit-910fc1b)

## 生命周期钩子

| hook | 时机 | 约定返回 | async 可选 |
|------|------|---------|-----------|
| before_turn | 每轮 run 开头（检索注入） | inject+result → system 旁注 | ✅（注入被忽略，仅副作用） |
| before_tool / after_tool | 工具前后（含并行分支） | 同上；after_tool 收 changed_files | ✅ |
| before_answer | 最终回答前（可打回重写）；context 含 **changed_calls**（引擎快照 diff 检出的变更调用原文数组，2026-08-19 新） | inject → 重写循环（封顶5次） | ❌（需同步反馈） |
| turn_end | 轮结束（验收） | 同 before_answer（封顶3次） | ✅（仅副作用，不验收） |

钩子内 LLM 走 `utility_client`（scene=hook:xxx 标注）；assembly DSL 关 hooks 则整个 Agent 不跑钩子。

before_turn 实例：**wiki_auto_query**（默认关闭）——三档漏斗（LLM1 意图识别 → wiki 搜索 → LLM2 精排），related=False 短路零搜索零 LLM2；四场景全链路已验证，详见 [wiki_auto_query](../features/wiki-auto-query.md)。

before_answer 消费 changed_calls 实例：**wiki_auto_maintenance**——引擎把本轮有文件变更的工具调用原文（edit 的 old/new、write 的 content 等）透传给工作流，子 Agent 无需 read_file 重读源文件。详见下方 [changed_calls 变更调用收集](#changed_calls-变更调用收集before_answer-透传2026-08-19) 与 [wiki_auto_maintenance 推理减负](../features/wiki-auto-maintenance.md#推理减负changed_calls-直供2026-08-19commit-16d6832)。

### 同步钩子异常边界（2026-08 修复，commit c563ca7）

**背景**：同步钩子（`_run_one`）此前未包 try，`run_hook` 抛异常会从 `ThreadPoolExecutor.fut.result()` 冒泡，炸掉整个 `_run_hooks`——同 hook 其他钩子结果全丢 + 主循环报错。

**修复**：在 `_run_one` 内层补 try/except，单个钩子失败只发 `auto_wf_error` 事件 + 返回 `(False, "", "")`（不注入），同 hook 其他钩子照常并行执行、结果照常合并。

```python
def _run_one(hw):
    # src/agent.py L1020-L1040
    ...
    try:
        try:
            inject, result, message = run_hook(...)
        finally:
            hook_llm._scene_override = _ov
        return hw["name"], inject, result, message
    except Exception as e2:
        # 单个钩子失败不拖垮并行组：发错误事件 + 返回空结果（不注入）
        self._emit({"type": "auto_wf_error", ...})
        return hw["name"], False, "", ""
```

**三层异常边界**（当前代码已齐）：

| 位置 | 异常处理 |
|------|----------|
| async 钩子 `_async_hook` | ✅ 有 try（发 `auto_wf_error`） |
| 同步钩子 `_run_one` | ✅ **本次补上**（发 `auto_wf_error` + 空结果） |
| 外层 `_run_hooks` | ✅ 有 try（`_LOG.error` 兜底） |

**设计意图**：与 async 钩子对称——单个钩子失败不影响同 hook 其他钩子并行执行，主循环稳定继续。错误通过 `auto_wf_error` 事件透出（可在 WebUI 观测），不注入主上下文。

**生效**：当前进程跑旧代码，`/restart` 后生效。

## 快照副作用检测（after_tool 闭环）

通过 `dir_snapshot` + `diff_snapshots` 通用子工作流，在 after_tool 钩子中检测工具执行产生的文件变更（如 wiki 更新、配置改写），并将变更清单注入上下文供后续决策使用。**不依赖工具返回值**，纯被动检测。

**实现示例**（伪代码）：

```python
# after_tool 钩子（伪）
def after_tool(ctx, changed_files):
    before = ctx["snap_before"]
    after = dir_snapshot(".agent/wiki/")
    diff = diff_snapshots(before, after)
    if diff["count"] > 0:
        ctx["wiki_changes"] = diff["changed"]
        ctx["wiki_files"] = diff["files"]
```

注意：钩子工作流自行调 dir_snapshot 是**目录级**的补充手段；引擎内部还有自己的 **workspace 快照**（`_fs_snap`），详见下方 changed_calls 章节。

## changed_calls 变更调用收集（before_answer 透传，2026-08-19）

**commit `16d6832`**。设计意图：**引擎已经知道的东西，不再让子 Agent 重新探索**——降低钩子工作流（如 wiki-updater 子 Agent）的推理负担，省掉 read_file 重读源文件的探索阶段。

### 引擎链路（src/agent.py）

```
逐 call 工具执行
  → 快照条件扩展：after_tool **或** before_answer 任一钩子存在即拍 workspace 快照
      （之前只有 after_tool 钩子在时才拍——before_answer 消费场景下拿不到变更信息）
  → diff 非空 → _turn_changed_calls.append({
        call_id, name,
        arguments,              ← edit 的 old/new、write 的 content 原文
        result_preview[:800],   ← 工具结果预览（截断防爆上下文）
        changed_files           ← 引擎快照 diff 检出的清单
  })
  → before_answer context 加 changed_calls（数组原样透传给钩子工作流）
```

**关键实现点**：

| 点 | 说明 |
|----|------|
| `_fs_snap` | 工具执行前 workspace 快照（真实副作用检测）；**链式复用上次 after 快照**，省一半扫描 |
| `_turn_changed_calls` | 本轮变更调用列表；每轮 run 开头与 `_hook_notes` 一起重置 |
| 三条执行路径全覆盖 | 逐 call（钩子模式）/ 单 call / 并行（**整批 diff 归属批内每个调用**——多报不漏报的取舍） |
| 快照触发条件 | after_tool 或 before_answer 任一钩子存在（2026-08-19 扩展，之前仅 after_tool） |

### 工作流消费（wiki_auto_maintenance）

```
start.changed_calls → fmt_calls(code 渲染原文) → text(拼接模板) → update_wiki(summary)
```

子 Agent 收到的任务描述直接包含变更调用原文，格式示例：

```
changed tool calls（引擎快照 diff 检出的文件变更调用原文——以此为准，一般无需再 read_file）:
1. edit({"path": "src/agent.py", "old_string": "...", "new_string": "..."})
   变更: src/agent.py(modified)
   结果: ✅ 已替换 1 处
```

详见 [wiki_auto_maintenance 推理减负](../features/wiki-auto-maintenance.md#推理减负changed_calls-直供2026-08-19commit-16d6832)。

**注意事项**：
- `changed_calls` 仅在快照 diff 检出文件变更时非空；无变更为空数组（工作流需容错空值/JSON 字符串输入）
- 子 Agent 仍可按需 read_file 补充上下文（引擎提供的原文一般足够理解改动）
- 引擎快照是 workspace 级 mtime 检测，粒度是"哪些文件变了"，不含内容——内容来自**调用参数原文**（edit/write 的参数本身就是 diff/全文）

## workflow.py 排障速查（2026-08）

| 症状 | 排查方向 |
|------|---------|
| 子工作流调用失败（type 9） | 检查 `workflowId` 是否存在、参数传递是否正确（字面量用 `literal="值"` 属性形式） |
| 工具节点返回 dict 消费报错 | 确认引用的是具体字段（`NODE.field`），补 `<out>` 声明 |
| selector 条件不命中 | 检查左值类型（string/数组/对象）、条件值是否直传（零序列化） |
| git_commit 提交失败 | 检查 `files` 是否为逗号分隔 string（不是 dict）、message 是否含特殊字符 |
| 钩子不执行 | 检查 `hidden=true` 是否误设、`async=true` 时 inject 不会被注入 |
| 前端输入框变空 | type 9 节点字面量保存问题——`syncSubworkflowNode` 重建 inputParameters 时只保留 blockID 连线，字面量被清空且默认值造出 `blockID=''` 的空 ref（见上方"subworkflow 节点输入框字面量保存后重开变空"） |
| before_answer 拿不到 changed_calls | 检查快照触发条件（after_tool/before_answer 任一钩子需 enabled）、工具是否真的改了文件（mtime diff 为空则不收集） |

## 编辑器注意

- **强刷新生效**：改 `workflow_editor.html` 后需 `/restart` + Ctrl+F5 强刷编辑器，否则运行旧代码
- **节点内输入框 vs 属性面板**：画布节点内的输入框（`makeInputControl`）与右侧属性面板（`setInputLiteral`）渲染逻辑不同——字面量保存问题仅影响**节点内输入框**
- **schema 同步时机**：openWf 时对 type 9 节点重跑 `syncSubworkflowNode`（获取子工作流 schema 并重建 `inputParameters`），此时若只保留连线会丢失字面量

## 相关页面

- [dir_snapshot / diff_snapshots 通用子工作流](snapshot-diff.md)
- [wiki_auto_maintenance · wiki 自动维护与提交](../features/wiki-auto-maintenance.md)
- [多 Agent 体系](multi-agent.md)
- [上下文引擎与缓存优化](context-engine.md)
- [运维、可观测性与排障](../guides/ops.md)
