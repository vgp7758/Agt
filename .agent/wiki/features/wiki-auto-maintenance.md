# wiki_auto_maintenance · wiki 自动维护与提交

> 工作流：`.agent/workflows/wiki_auto_maintenance.xml`
> 职责：主 Agent 完成开发任务后，自动维护 `.agent/wiki/` 知识库页面并**自动 git 提交推送**，形成"改代码 → 更新文档 → 提交"的闭环。
> **v0.18.2 正式发布**。

## 背景：为什么需要 commit_wiki

主 Agent 在开发迭代中会调用 `update_wiki`（wiki-updater 子 Agent）增量更新 wiki 页面，但主 Agent 自身**不提交 wiki 文件**——它只改代码和文档，git add/commit/push 由其他机制负责。这导致 wiki 改动经常滞留在工作区，未被版本控制跟踪，知识库与代码长期脱节。

**commit_wiki 节点**解决了这一矛盾：在 update_wiki 之后自动 `git add .agent/wiki/` 并检测变更，有变更则 commit+push，无变更则静默跳过。

### 提交失败问题（2026-08 修复）

**旧方案根因**：commit_wiki 此前用 `run_shell`（shell 命令）拼接 `git commit -m "<msg>"`。当 commit message 含**多行文本**（update_wiki 报告摘要换行）时，shell 转义会破坏引号/换行，导致 git 提交失败。

**新方案**：将 commit_wiki 从 `run_shell` 改为 **`git_commit` 节点**，并通过 subprocess **列表参数**传递 commit message（不经过 shell 字符串拼接），彻底规避多行/特殊字符的转义问题。同时自动追加 `Co-authored-by` 署名。

## 工作流结构

```
start
  → update_wiki          （wiki-updater 子 Agent / LLM 节点，增量更新 .agent/wiki/ 页面）
  → build_commit_msg      （text 节点，将 update_wiki 报告摘要注入 commit message 模板）
  → snap_before           （快照节点，记录提交前 .agent/wiki/ 的 git 状态）
  → diff_wiki             （diff 节点，对比快照生成变更清单）
  → commit_wiki           （git_commit 节点，按变更清单自动 git add + commit + push）
  → end
```

| 节点 | 类型 | 职责 |
|------|------|------|
| update_wiki | LLM / subworkflow | 分析本次改动，调用 wiki CRUD 工具更新/新建受影响 wiki 页面；输出报告摘要 |
| build_commit_msg | **text（节点 ID 868393）** | 接收 update_wiki 的报告摘要，拼装为语义化 commit message，供 commit_wiki 使用 |
| snap_before | 快照节点 | 记录提交前 `.agent/wiki/` 的 git 状态（变更基线） |
| diff_wiki | diff 节点 | 对比 snap_before 快照与当前状态，生成本次变更清单（新增/修改/删除的文件） |
| commit_wiki | **git_commit（节点）** | 按 diff_wiki 变更清单 `git add` 相关文件 → `git commit`（列表参数传 message）→ `git push`；无变更跳过 |

### build_commit_msg：动态 commit message

**新增背景**（2026-08-18）：此前 commit_wiki 使用固定文案 `chore(wiki): auto-update wiki after task`，git log 中无法区分每次 wiki 维护改了什么。新增 `build_commit_msg`（text 节点，ID 868393）将 update_wiki 的报告摘要注入 commit message，使每条提交都带有具体内容摘要。

- **节点类型**：text（type 15），纯文本拼装，不走 LLM 调用，零额外 token 成本
- **输入**：update_wiki 节点输出的报告摘要（本次更新了哪些页面、关键改动点）
- **输出**：形如 `chore(wiki): update <摘要内容>` 的 commit message 字符串
- **边连接**：update_wiki → build_commit_msg → snap_before → diff_wiki → commit_wiki

### snap_before / diff_wiki：变更清单生成

**新增背景**（2026-08 修复）：此前 commit_wiki 靠 `git diff --cached --quiet` 判断是否有变更，无法精确知道改了哪些文件。引入 **snap_before + diff_wiki** 机制，先对 `.agent/wiki/` 打快照，再 diff 出精确的变更清单，供 commit_wiki 按清单提交。

- **snap_before**：提交前记录 `.agent/wiki/` 下所有文件的 git 状态（路径 + hash）作为基线
- **diff_wiki**：对比基线快照与提交前实际状态，输出变更清单（`新增/修改/删除` 的文件列表），无变更则清单为空
- **变更清单驱动提交**：commit_wiki 只 add 清单中的文件，避免误提交无关文件；清单为空时静默跳过

### commit_wiki 核心逻辑（git_commit 节点）

```python
# 伪代码示意：git_commit 节点内部以 subprocess 列表参数执行，不经 shell 字符串
if not diff_wiki.changes:      # 变更清单为空
    log("No wiki changes to commit.")
else:
    subprocess.run(["git", "add", ".agent/wiki/"])
    msg = build_commit_msg.output + "\n\nCo-authored-by: ..."   # 自动追加署名
    subprocess.run(["git", "commit", "-m", msg])               # 列表参数，多行安全
    subprocess.run(["git", "push"])
```

- **git_commit 节点**：替代原 run_shell，内部用 subprocess **列表参数**传参，commit message 多行/特殊字符不再被 shell 转义破坏
- **自动 Co-authored-by**：提交时自动追加 `Co-authored-by` 署名（标识 wiki 由主 Agent + wiki-updater 协作维护）
- **只提交 `.agent/wiki/` 目录**：不触碰代码文件，避免与主 Agent 的代码提交产生冲突
- **变更清单驱动**：diff_wiki 生成的变更清单为空则不产生空 commit；非空则按清单 add+commit+push
- **自动 push**：commit 后立即推送，确保远程仓库 wiki 始终最新

## 与其他模块的关系

| 模块 | 关系 |
|------|------|
| [update_wiki / wiki-updater](../home.md#维护约定) | 前置节点，负责 wiki 内容更新并输出报告摘要；build_commit_msg 消费其摘要 |
| [工作流引擎与钩子](../architecture/workflow-hooks.md) | 本工作流为普通工具工作流（非钩子），由主 Agent 显式调用或编排触发；git_commit 为引擎提供的 git 专用节点 |
| [wiki_auto_query](wiki-auto-query.md) | 读侧：before_turn 钩子自动检索 wiki；本页是写侧：自动维护并提交 wiki |
| [v0.18.2 发布记录](../releases/v0.18.2.md) | wiki 自动提交为本次交付项之一 |

## 注意事项

- **触发时机**：应在主 Agent 完成代码改动并提交代码后触发，避免 wiki 描述了尚未提交的代码
- **commit message 演进**：从固定文案 → 动态摘要（build_commit_msg 注入）→ 自动追加 Co-authored-by，git log 可追溯每次 wiki 维护的具体内容与协作方
- **git_commit 节点 vs run_shell**：涉及多行/特殊字符的 commit message **必须走 git_commit 节点的列表参数**，不要退回 shell 字符串拼接（shell 转义是原失败根因）
- **text 节点零成本**：build_commit_msg 为 text 节点（纯文本拼装），不触发 LLM 调用，不增加 token 开销
- **并发安全**：若多 Agent 并发运行，commit_wiki 的 git 操作可能冲突；建议同一仓库同一时刻只有一个 wiki_auto_maintenance 在跑
- **snap/diff 开销**：snap_before + diff_wiki 只扫描 `.agent/wiki/` 目录（文件量小），成本可忽略

## 相关页面

- [工作流引擎与钩子](../architecture/workflow-hooks.md)：工作流节点类型（含 text type 15、git_commit 节点）
- [wiki_auto_query](wiki-auto-query.md)：wiki 读侧——before_turn 自动检索注入
- [知识库导航](../home.md)：维护约定中提及 update_wiki
- [v0.18.2 发布记录](../releases/v0.18.2.md)：版本交付内容总览
