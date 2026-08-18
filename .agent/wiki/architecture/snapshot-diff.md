# dir_snapshot / diff_snapshots · 通用快照与变更检测子工作流

> 工作流：`.agent/workflows/dir_snapshot.xml`、`.agent/workflows/diff_snapshots.xml`
> 职责：对目录打文件快照、对比两份快照生成精确变更清单。**通用引擎级能力**，供任意工作流复用，最早在 [wiki_auto_maintenance](../features/wiki-auto-maintenance.md) 中用于提交前变更检测。
> **v0.18.2 正式发布**（2026-08，从 wiki_auto_maintenance 内联节点重构拆分而来）。

## 为什么拆成通用子工作流

早期 wiki_auto_maintenance 的提交前变更检测是**内联节点**，仅服务 wiki 提交这一个场景。重构为两个**通用子工作流**后，任何需要"检测某目录变更"的工作流都能直接复用，不必重复实现快照/diff 逻辑。与引擎内部 `_workspace_snapshot` / `_diff_snapshots`（after_tool 钩子用）逻辑**同源**，两者共用同一套快照-diff 语义。

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

## 与 engine 内部快照的关系

引擎内 `_workspace_snapshot` / `_diff_snapshots`（after_tool 钩子检测副作用用）与这两个子工作流**逻辑同源**。区别：

| 维度 | 引擎内部（after_tool 钩子） | 子工作流（对外暴露） |
|------|------------------------------|----------------------|
| 用途 | ReAct 循环内工具副作用检测 → changed_files 注入钩子 | 任意工作流显式调用，按需快照/diff |
| 排除范围 | 更广：`.git`/`.agent`/`.agt` + gitignore 全模式 + 嵌套 git 仓库整棵剪枝 | 基础：`.git`/`__pycache__` |
| 输出 | `changed_files` 数组直传钩子 | `files`(逗号分隔) + `count` + `changed`(结构化) |

## subworkflow literal 属性坑（2026-08 修复）

调用子工作流传**字面量参数**（如 `path=".agent/wiki/"`）时，**必须用属性形式 `literal=".agent/wiki"`**，不能用子元素形式（`<literal>...</literal>`）。子元素形式会导致参数传递失败——子工作流收不到字面量，快照/diff 无法正确限定目录。这是本次重构踩到的关键坑，已在 wiki_auto_maintenance 的 snap_before / diff_wiki 节点修正。

## 使用示例（wiki_auto_maintenance 中的装配）

```
build_commit_msg → snap_before（dir_snapshot，path=.agent/wiki/，打提交前基线）
  → diff_wiki（diff_snapshots，before=snap_before 快照 + after=当前快照，生成变更清单）
  → commit_wiki（git_commit，files=diff_wiki.files，按清单 add+commit+push，无变更跳过）
```

详见 [wiki_auto_maintenance 快照重构](../features/wiki-auto-maintenance.md#snap_before--diff_wiki快照与变更清单重构为子工作流2026-08)。

## 复用建议

- 任何"改文件后要精确知道改了哪些"的场景都可复用：提交前变更检测、构建产物对比、配置漂移检测等
- `files`（逗号分隔）与 `git_commit` 的 files 参数天然衔接；`changed` 结构化对象适合需要按变更类型分支处理的场景
- `path` 留空扫描整个 workspace，文件量大时成本偏高；尽量传 `path` 缩小范围

## 相关页面

- [wiki_auto_maintenance](../features/wiki-auto-maintenance.md)：首个消费方——git_commit 按变更清单提交
- [工作流引擎与钩子](workflow-hooks.md)：git_commit 节点 + 引擎内部快照闭环 + subworkflow literal 属性约定
- [v0.18.2 发布记录](../releases/v0.18.2.md)：快照子工作流重构为本次交付项之一
