# dir_snapshot / diff_snapshots · 通用快照与变更检测子工作流

> 工作流：`.agent/workflows/dir_snapshot.xml`、`.agent/workflows/diff_snapshots.xml`（均标 **`hidden="true"`**，2026-08，commit d59dcbd）
> 职责：对目录打文件快照、对比两份快照生成精确变更清单。**通用引擎级能力**，供任意工作流复用，最早在 [wiki_auto_maintenance](../features/wiki-auto-maintenance.md) 中用于提交前变更检测。
> **v0.18.2 正式发布**（2026-08，从 wiki_auto_maintenance 内联节点重构拆分而来）。

## 为什么拆成通用子工作流

早期 wiki_auto_maintenance 的提交前变更检测是**内联节点**，仅服务 wiki 提交这一个场景。重构为两个**通用子工作流**后，任何需要"检测某目录变更"的工作流都能直接复用，不必重复实现快照/diff 逻辑。与引擎内部 `_workspace_snapshot` / `_diff_snapshots`（after_tool 钩子用）逻辑**同源**，两者共用同一套快照-diff 语义。

## hidden 归类（2026-08，commit d59dcbd）

两子工作流标 `hidden="true"`——**主 Agent 不应直接调用**它们（不投影 `wf_dir_snapshot` / `wf_diff_snapshots` 工具），仅供其他工作流以 subworkflow(9) 子工作流方式复用。**hidden 不影响调用**：`_find_local_workflow` 按名字取子工作流不看 hidden，wiki_auto_maintenance 的快照装配照常工作。收益：主 LLM 工具表少 2 个 wf_* 工具、schema 更小、折叠估算更准（详见 [workflow-hooks · hidden 归类](../architecture/workflow-hooks.md#demo--子工作流-hidden-归类2026-08commit-d59dcbd)）。

## 两个子工作流

### dir_snapshot：目录文件快照

- **入参**：`path`（可选；留空 = 整个 workspace）
- **行为**：对指定目录取文件快照，输出 **JSON 字符串**（文件路径 → mtime 映射）
- **排除**：`.git` / `__pycache__`（避免噪声）
- **用途**：作为"提交前 / 工具执行前"的基线，供后续 diff 对比

### diff_snapshots：快照对比 → 变更清单

- **入参**：`before`、`after`（两份 dir_snapshot 快照字符串）
- **行为**：对比两份快照，输出变更文件清单
- **输出**：
  - `files`：逗号分隔的变更文件路径——**可直接喂给 git_commit 节点的 files 参数**，实现按清单提交
  - `count`：变更文件数
  - `changed`：结构化对象列表（含 file / change 类型 new|modified|deleted 等），供 selector/loop/aggregator 进一步处理
- **无变更**：清单为空 → 消费端（如 commit_wiki）静默跳过，不产生空提交

> **消费端注意（dict split 坑，commit edd9851 修复）**：`wf_diff_snapshots` 作为**工具节点**被调用时，其输出是**原始 dict**。消费端必须引用其**具体字段**（`files` / `count` / `changed`），并**补 `<out>` 声明**；把整个 dict 当字符串 `.split(",")` 会报 `'dict' object has no attribute 'split'`。详见 [wiki_auto_maintenance 的 dict split 修复](../features/wiki-auto-maintenance.md#dict-split-报错修复2026-08commit-edd9851)。

**为什么输出是 dict 而非字符串**：`wf_diff_snapshots` 走 **Tool.run()** 执行——它把 end 的 `{files, count, changed}` json.dumps 成字符串，随后 `_handle_plugin` 的 `_try_parse` 又解析回 **Python dict**。于是 `1400227.raw` 是 `{files:"...", count:3, changed:[...]}` 这样的 dict 对象，而非逗号分隔字符串。

**如何正确消费**：
1. 引用**具体字段**：`1400227.files`（`_dotted_get` 按点路径直接取 dict 字段）——解析为逗号分隔 string，可直接喂 git_commit
2. **补 `<out>` 声明**：`<out name="files" type="string"/>` + `count`——`_extract_field` 按声明字段填充，编辑器下拉也能选到 files 端口，避免误连到整个 raw dict

## 与 engine 内部快照的关系

引擎内 `_workspace_snapshot` / `_diff_snapshots`（after_tool 钩子检测副作用用）与这两个子工作流**逻辑同源**。区别：

| 维度 | 引擎内部（after_tool 钩子） | 子工作流（对外暴露） |
|------|------------------------------|----------------------|
| 用途 | ReAct 循环内工具副作用检测 → changed_files 注入钩子 ∪ **ToolCall.changed 回流（恒开）** | 任意工作流显式调用，按需快照/diff |
| 排除范围 | 更广：`.git`/`.agent`/`.agt` + gitignore 全模式 + 嵌套 git 仓库整棵剪枝 | 基础：`.git`/`__pycache__` |
| 输出 | `changed_files` 数组直传钩子 + **ToolCall.changed（前端折叠判定 + events 持久化）** | `files`(逗号分隔) + `count` + `changed`(结构化) |

### 快照恒开：副作用双消费（2026-09-02，commit 2bd25be，用户请求）

快照 diff 此前**只在钩子活跃时做**（省开销）；用户提议「其它步如果工具调用前后发生了文件 diff，前端也需要展开工具调用渲染」后改为**恒开**——每次工具调用前后各扫一次 mtime（**链式复用上次 after 快照作本次 before**，成本可控），结果种进 `ToolCall.changed`（session.py dataclass 新字段）：

- **消费一（原有）**：钩子活跃时 `changed_files` 注入钩子上下文
- **消费二（新增）**：`tool_result` 事件带 changed（实时前端判定展开）+ `step` 事件 `changes` 字段序列化持久化（读档/rewind 后历史渲染同构恢复）

前端折叠判定与效果矩阵见 [trace-fold · 工具调用折叠](../features/trace-fold.md#🔧-工具调用折叠同批基建)。

## 与 diff_files 工具的分工（2026-08-19 新）

本页两子工作流是 **目录级 · mtime 级**检测（只回答"哪些文件变了"，不看内容）；新工具 [diff_files](../features/diff-files.md)（`src/real_tools.py`）是 **单文件 · 行级内容** diff（Myers Diff + unified hunk 输出，回答"具体改了什么"）。典型组合：diff_snapshots 生成变更清单 → 对关注的文件逐个 `diff_files` 看内容差异（如快照回溯点 vs 当前、备份 vs 改后）。

## subworkflow literal 属性坑（2026-08 修复）

调用子工作流传**字面量参数**（如 `path=".agent/wiki/"`）时，**必须用属性形式 `literal=".agent/wiki"`**，不能用子元素形式（`<literal>...</literal>`）。子元素形式会导致参数传递失败——子工作流收不到字面量，快照/diff 无法正确限定目录。**后果**：`path` 为空 → 快照拍了整个 workspace → `files` 清单超长 → **WinError 206（文件名或扩展名太长）**。这是重构时踩到的关键坑，已在 wiki_auto_maintenance 的 snap_before 节点修正。

> **后端链路正常，前端缓存问题（2026-08 验证）**：path 字面量保存后，**后端链路验证正常**（保存→读取→执行均正确传递 path），问题定位为**前端缓存**——编辑器保存后浏览器未刷新导致展示旧值，并非后端参数传递缺陷。若保存后行为异常，先**硬刷新浏览器**（Ctrl+Shift+R）排除缓存再排查后端。

## workflow.py 缺少 Toolbox import → NameError 修复（2026-08）

**现象**：运行涉及快照/工具链的工作流时，`workflow.py` 抛 **NameError**（`Toolbox` 未定义）。

**根因**：`src/workflow.py` 中使用了 `Toolbox` 类但**缺少对应的 import 语句**，运行时解析不到符号。

**修复**：在 `src/workflow.py` 文件头补上 `Toolbox` 的 import（与 `src/agent.py` 中 Toolbox 的引入方式一致），使符号解析正常。

**经验**：涉及跨模块符号（如 Toolbox）时，改动后**先确认 import 完整**再跑工作流；NameError 与业务逻辑无关，纯符号解析问题。

## 使用示例（wiki_auto_maintenance 中的装配）

```
判官 llm → snap_before（子工作流 dir_snapshot，path=.agent/wiki/，打【更新前】基线）
  → update_wiki（plugin，更新 wiki 页面）
  → diff_wiki（code 节点：本节点执行时拍 after 快照 + 调 diff_snapshots 对比，生成变更清单）
  → has_changes?（count>0）
      ├─ true  → build_msg → commit_wiki（git_commit，files=diff_wiki.files，按清单 add+commit+push）
      └─ false → 静默跳过
```

**关键时序**：`snap_before` 必须在 `update_wiki` **之前**执行（打更新前基线）；`after` 快照必须在 `update_wiki` **之后**由 diff_wiki 节点执行时拍（拍早了 diff 不到变更）。所以 diff_wiki 是 **code 节点**——内部自己 walk 一遍 `.agent/wiki/`（与 dir_snapshot 同逻辑）拍 after，再调 `diff_snapshots` 子工作流对比；子工作流不可用则走本地兜底 diff。

详见 [wiki_auto_maintenance 快照重构](../features/wiki-auto-maintenance.md#snap_before--diff_wiki快照与变更清单重构为子工作流2026-08)。

## 复用建议

- 任何"改文件后要精确知道改了哪些"的场景都可复用：提交前变更检测、构建产物对比、配置漂移检测等
- `files`（逗号分隔）与 `git_commit` 的 files 参数天然衔接；`changed` 结构化对象适合需要按变更类型分支处理的场景
- `path` 留空扫描整个 workspace，文件量大时成本偏高；尽量传 `path` 缩小范围
- **注意时序**：before 快照在改动前拍，after 快照在改动后拍，由调用方（code 节点或子工作流编排）保证顺序
- **工具节点输出是 dict**：消费 `wf_diff_snapshots` 必须引用具体字段（`_dotted_get`）并补 `<out>` 声明，见上文"消费端注意"
- 想看**文件内容行级差异**（而非仅文件清单），接 [diff_files 工具](../features/diff-files.md)

## 相关页面

- [wiki_auto_maintenance](../features/wiki-auto-maintenance.md)：首个消费方——git_commit 按变更清单提交；含 dict split 报错修复 + path 前端缓存问题
- [diff_files 工具](../features/diff-files.md)：行级内容 diff（Myers Diff + unified 输出），与本页 mtime 级目录快照互补
- [工作流引擎与钩子](workflow-hooks.md)：git_commit 节点 + 引擎内部快照闭环 + subworkflow literal 属性约定 + hidden 归类
- [v0.18.2 发布记录](../releases/v0.18.2.md)：快照子工作流重构为本次交付项之一