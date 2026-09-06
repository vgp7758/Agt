# 检查点快照与回溯 · /snapshot restore / /rewind + git 撞车检测

## 职责

每轮开始时对工作区打**快照检查点**（`Turn.snapshot_sha`），用户可回溯：还原工作区文件树 + 截断对话到检查点时刻。三个入口：`/snapshot restore <序号|sha>`、`/rewind [N]`、WebUI 气泡回溯按钮——全部收敛到 `restore_snapshot` 统一入口（src/chat.py）。

## 快照机制（src/snapshots.py）

- `SnapshotManager`：工作区旁路 git 仓库（`~/.agt/repos/<repo>/.agt/snapshots/`，`core.worktree` 指向 workspace，info/exclude 掉 `.agt/` `.git/`）——`add -A` → `write-tree` → `commit-tree` 得 sha，挂 `refs/agt/snap/<sha>` 防 GC
- `snapshot()` 每轮开始打点；`restore(sha)` 还原文件树（read-tree + checkout-index）+ `clean -fd -e .agt` 删快照之后新建的文件
- 回溯**不动用户真仓库 `.git`**——这是撞车问题的根源（见下）

## 撞车：检查点之后有 git 提交（用户提案 2026-09-06）

回溯只还原工作区 + 截对话，HEAD 不受影响。若检查点之后用户真仓库有提交 →「session 在过去、git 历史在未来」分裂：之后任何 `add -A` 都会把回溯差异整笔提交。

### 记录：每轮打点顺带记真仓库 HEAD

- `Turn.git_head`（src/session.py）：该轮发送前真仓库 HEAD；`record_snapshot(sha, git_head)` 写入 + snapshot 事件带 `git_head` 字段，turns 序列化/重放两处同步（旧档无此字段 → 空串 = 不拦）
- `git_head_at_snapshot(sha)`：按 sha 查对应轮记录的 HEAD
- agent.py 打快照时顺带 `user_repo_head(workspace)`（snapshots.py；非 git 仓库/失败返回空串 = 不启用检测）

### 检测：restore_snapshot 统一入口（src/chat.py）

`restore_snapshot(agent, sha, git_policy="block")`：目标轮 `git_head` ≠ 当前 HEAD → 撞车。

- **`block`（默认）**：抛 `RuntimeError`——带提交清单（`git log old..new` oneline）+ 三条处置指引，回溯不生效
- **`reset`**：`user_repo_reset_hard` → `git reset --hard` 到检查点时刻的 HEAD，随后正常回溯（被退提交 reflog 可找回；已 push 过需 `push --force` 覆盖远端）
- 旧档轮无 git_head 记录 / 非 git 仓库 → 不拦（行为不变）

### 入口三处与 --git reset 语法

| 入口 | 撞车处置 |
|---|---|
| `/snapshot restore <序号\|sha> [--git reset]` | 默认 block；`--git reset` / `--git-reset` → reset（src/commands.py） |
| `/rewind [N] [--git reset]` | 同上（src/commands.py） |
| WebUI 回溯按钮 | action `restore` 传 `git_reset: true` → reset（src/server.py，前端默认不传 = block 拦截） |

### .agt 防护（测试实测踩中的真炸弹）

真仓库若没 ignore `.agt/`，`add -A` 会把**快照仓库本身**（`.agt/snapshots/`）提交进去 → 之后 `reset --hard` 会把整个快照仓库当「新增跟踪文件」**连带删除**，回溯能力当场报废。修复：`user_repo_reset_hard` reset 前先 `git rm -r --cached .agt`（变 untracked，`--hard` 不动 untracked，工作区保留）。

## 顺带修复：WebUI 回溯失败广播

此前 WebUI 回溯失败只 print 到 CLI（前端只看到「回溯中…」后无声失败）→ 现在失败原因 `_broadcast` 成 system 消息（src/server.py）。

## 验证（12/12）

无提交正常回溯 / 有提交默认拦截（RuntimeError + 提交清单 + 指引 + HEAD 未动）/ `--git reset` HEAD 退回检查点时刻 + 文件内容随 `--hard` 回退 + reflog 找回被退提交 / 旧档无记录与非 git 仓库不拦。

## 注意事项

- git_head 自 2026-09-06 起才开始记录——**存量检查点无 git_head，检测逐步覆盖**：/restart 后每轮开始记录，积累起来才全覆盖
- `git reset --hard` 是破坏性操作：被退提交只在本地 reflog，已 push 过需 `push --force`
- 撞车拦截的提交清单（`git log old..new`）可直接判断「被退提交里有没有要保留的东西」，再决定走 reset 还是放弃回溯

## 相关页面

- [系统总览 · 一轮对话数据流](../architecture/overview.md)——② 快照 workspace（回溯检查点）
- [diff_files](diff-files.md)——典型用途含「快照回溯点 vs 当前」对比
- [dir_snapshot / diff_snapshots](../architecture/snapshot-diff.md)——目录级 mtime 快照（工作流子能力，与本页检查点机制是两回事）
