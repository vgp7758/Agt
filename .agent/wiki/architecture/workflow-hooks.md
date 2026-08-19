# 工作流引擎与钩子

> src/workflow.py + workflow_xml.py + agent.py(_run_hooks)。节点细节见 [docs/architecture/04-workflow.md](../../../docs/architecture/04-workflow.md)，本页补充钩子链路、async 元信息与快照检测闭环。

## 双格式与热加载

- `.agent/workflows/<名>.xml`（推荐，CDATA 免转义）或 `.json`（Coze 原生画布）
- `.meta` 旁车 / XML 根属性：name/description/**hook**/enabled/**hidden**/**async**/auto/coze_url
- 每轮对话开始自动扫描注册为 `wf_*` 工具；`hidden=true` 不投影给 LLM（钩子/子工作流专用）
- meta.hidden 的 XML 往返已修复（api_wf_get 读根属性）——历史丢 hidden 的文件已补回

## before_turn 钩子并行执行（2026-08 新，v0.18.2 发布）

**设计**：同一 hook（如 before_turn）可挂多个工作流（如 `before_turn_retrieval` + `wiki_auto_query`），由 `ThreadPoolExecutor` 并发执行，**等待全部完成才返回**。

**实现**（`src/agent.py` `_run_hooks`）：

```python
with ThreadPoolExecutor() as pool:
    futures = {pool.submit(wf.run, **ctx): wf for wf in workhooks}
    for fut in as_completed(futures):
        result = fut.result()  # 异常抛到外层
        hooks.append((wf.name, result))
```

**关键保证**：
- **全部完成才返回**：`as_completed` 遍历完毕 → 所有 future 解析完毕 → 主循环才继续（不会出现「一个钩子未完成就开始第1步」的现象）
- **UI 并行跟踪**：前端 `runningAutoWf` 改为 `Map` 按 `hook::name` 索引，并行钩子各自独立显示「执行中」，结束独立移除（见 [user-interaction · UI 修复](../features/user-interaction.md#并行钩子执行中状态跟踪修复2026-08-19)）

**相关页**：[wiki_auto_query](../features/wiki-auto-query.md)（before_turn 典型实例）

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
- **diff_lines 工具**（LIGHT_TOOLS，hidden）：两个文本块按行 Myers diff（无需落盘），与 diff_files 共用 `_render_unified_diff` 渲染（详见 [diff_lines 页](../features/diff-lines.md)）
- **get_list_item 工具**（LIGHT_TOOLS，outputs=any）：从列表取单个元素，支持正/负索引、越界安全返回错误提示（详见 [get_list_item 页](../features/get-list-item.md)）
- **run_python 工具新增 args 参数**：`run_python(code="...", file="...", args="...")`，经环境变量 `PY_ARGS` 传递（code 和 file 两模式都生效），脚本内 `import os; a = os.environ.get("PY_ARGS", "")` 读取（详见 [run_python 页](../features/run-python.md)）
- **XML schema 往返**：list\<object> 的 field 子元素 / list 基础类型 itemType / 坐标幂等（编辑器保存不再丢结构）
- **git_commit 节点**：git 专用提交节点，内部以 **subprocess 列表参数**传参（不经 shell 字符串拼接），多行/特殊字符 commit message 安全；配合快照/diff 子工作流按变更清单提交。实例见 [wiki_auto_maintenance 的 commit_wiki](../features/wiki-auto-maintenance.md#commit_wiki-核心逻辑git_commit-节点)
- **dir_snapshot / diff_snapshots 子工作流**：引擎级快照能力——`dir_snapshot(path)` 对目录取文件快照（mtime 映射 JSON，排除 .git/__pycache__，path 留空=整个 workspace）；`diff_snapshots(before, after)` 对比两份快照输出变更清单（`files` 逗号分隔 + `count` + `changed` 结构化对象），供 git_commit 或选择器/聚合节点消费。**通用复用**：详见 [dir_snapshot / diff_snapshots 通用子工作流](snapshot-diff.md)，首个消费方为 [wiki_auto_maintenance 的快照重构](../features/wiki-auto-maintenance.md#snap_before--diff_wiki快照与变更清单重构为子工作流2026-08)
- **subworkflow 节点 literal 属性约定（2026-08）**：subworkflow(9) 节点调用子工作流传字面量参数时，**literal 必须用属性形式 `literal="值"`**，不能用于子元素形式（`<literal>值</literal>`）。子元素形式会导致参数传递失败（子工作流收不到字面量）→ path 空 → 快照全盘 → WinError 206。实例：wiki_auto_maintenance 的 snap_before 传 `path=".agent/wiki/"`（见 [快照重构中的 literal 坑](../features/wiki-auto-maintenance.md#subworkflow-节点-literal-属性坑2026-08-修复)）
- **工具节点输出是 dict（2026-08 修复，commit edd9851）**：`wf_diff_snapshots` 等**工具节点** raw 返回 **dict**（Tool.run() 把 end 的 dict json.dumps 成字符串 → `_handle_plugin._try_parse` 又解析回 Python dict），消费端必须引用**具体字段**（`files` / `count` / `changed`，`_dotted_get` 直接取）并**补 `<out>` 声明**；把整个 dict 当字符串 `.split(",")` 会报 `'dict' object has no attribute 'split'`。实例见 [wiki_auto_maintenance 的 dict split 修复](../features/wiki-auto-maintenance.md#dict-split-报错修复2026-08commit-edd9851)
- **subworkflow 节点输入框字面量保存后重开变空（2026-08 修复，commit 910fc1b）**：type 9 节点的输入框（画布节点内，非右侧属性面板）手动输入字面量保存后，重新打开工作流输入框变空——**根因在前端 `syncSubworkflowNode`**（openWf 时对 type 9 节点重跑 schema 同步：只保留连线、丢字面量，默认值造出 `blockID=''` 的空 ref → 输入框空白）。修复：连线 ref 完整保留 + 非空字面量保留，默认值改空 literal。生效需 `/restart` + Ctrl+F5。详见 [wiki-auto-maintenance · path 字面量坑](../features/wiki-auto-maintenance.md#path-字面量保存后重开变空2026-08-修复commit-910fc1b)
