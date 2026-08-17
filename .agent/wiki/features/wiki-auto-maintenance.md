# wiki_auto_maintenance · wiki 自动维护与提交

> 工作流：`.agent/workflows/wiki_auto_maintenance.xml`
> 职责：主 Agent 完成开发任务后，自动维护 `.agent/wiki/` 知识库页面并**自动 git 提交推送**，形成"改代码 → 更新文档 → 提交"的闭环。

## 背景：为什么需要 commit_wiki

主 Agent 在开发迭代中会调用 `update_wiki`（wiki-updater 子 Agent）增量更新 wiki 页面，但主 Agent 自身**不提交 wiki 文件**——它只改代码和文档，git add/commit/push 由其他机制负责。这导致 wiki 改动经常滞留在工作区，未被版本控制跟踪，知识库与代码长期脱节。

**commit_wiki 节点**（2026-08-18 新增）解决了这一矛盾：在 update_wiki 之后自动 `git add .agent/wiki/` 并检测变更，有变更则 commit+push，无变更则静默跳过。

## 工作流结构

```
start
  → update_wiki          （wiki-updater 子 Agent / LLM 节点，增量更新 .agent/wiki/ 页面）
  → commit_wiki           （run_shell 节点，自动 git add + commit + push）
  → end
```

| 节点 | 类型 | 职责 |
|------|------|------|
| update_wiki | LLM / subworkflow | 分析本次改动，调用 wiki CRUD 工具更新/新建受影响 wiki 页面 |
| commit_wiki | run_shell（code/工具节点） | `git add .agent/wiki/` → 检测 diff → 有变更则 `git commit -m "chore(wiki): auto-update"` + `git push`；无变更跳过 |

### commit_wiki 核心逻辑

```bash
git add .agent/wiki/
if git diff --cached --quiet -- .agent/wiki/; then
    echo "No wiki changes to commit."
else
    git commit -m "chore(wiki): auto-update wiki after task"
    git push
fi
```

- **只提交 `.agent/wiki/` 目录**：不触碰代码文件，避免与主 Agent 的代码提交产生冲突
- **有变更才提交**：`git diff --cached --quiet` 检测暂存区是否有差异，无变更不产生空 commit
- **自动 push**：commit 后立即推送，确保远程仓库 wiki 始终最新

## 与其他模块的关系

| 模块 | 关系 |
|------|------|
| [update_wiki / wiki-updater](../home.md#维护约定) | 前置节点，负责 wiki 内容更新；commit_wiki 负责将更新落盘到 git |
| [工作流引擎与钩子](../architecture/workflow-hooks.md) | 本工作流为普通工具工作流（非钩子），由主 Agent 显式调用或编排触发 |
| [wiki_auto_query](wiki-auto-query.md) | 读侧：before_turn 钩子自动检索 wiki；本页是写侧：自动维护并提交 wiki |

## 注意事项

- **触发时机**：应在主 Agent 完成代码改动并提交代码后触发，避免 wiki 描述了尚未提交的代码
- **commit message 约定**：`chore(wiki): auto-update wiki after task`，便于 git log 中区分 wiki 维护提交与功能提交
- **并发安全**：若多 Agent 并发运行，commit_wiki 的 git 操作可能冲突；建议同一仓库同一时刻只有一个 wiki_auto_maintenance 在跑
- **run_shell 节点**：依赖工作流引擎的 shell/code 节点执行能力，工作目录须为仓库根目录

## 相关页面

- [工作流引擎与钩子](../architecture/workflow-hooks.md)：工作流节点类型、run_shell 执行机制
- [wiki_auto_query](wiki-auto-query.md)：wiki 读侧——before_turn 自动检索注入
- [知识库导航](../home.md)：维护约定中提及 update_wiki
