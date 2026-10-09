# diff_paths · 路径级差异盘点工具（REAL_TOOLS · 引擎内置）

> 源码：`src/real_tools.py`（`diff_paths` 函数 + `REAL_TOOLS` 注册，紧邻 [diff_files](diff-files.md)，2026-10 落地，commit c486c06）
> 职责：对比两个路径（目录或文件），返回有差异的文件清单——只列差异、不进内容。用户提案：「除了 diff_files，还可以有一个 diff_paths 工具，对比两个路径，返回有差异的文件列表」。

## 职责与用法

`diff_paths(path_a, path_b, max_items=200)`：

- **path_a / path_b**：两个路径，相对 workspace 或绝对路径；目录递归对比，目录↔文件亦可
- **max_items**：每组列出上限（默认 200，超出折叠为计数）
- **典型用途**：两份代码树 / 备份 / 版本的差异盘点——先 diff_paths 看哪些文件变了，再对可疑文件用 [diff_files](diff-files.md) 看内容

**输出形态**（冒烟实测）：

```
对比 treeA ↔ treeB（A 侧 3 文件 / B 侧 4 文件 · 0.00s）

仅在 A 有: 1 项
  only_a.md

仅在 B 有: 2 项
  only_b.md
  src/deep/new.py

内容不同: 1 项
  src/mod.py

共 4 处差异
```

- 三组：**仅在 A 有 / 仅在 B 有 / 内容不同**；头部带两侧文件总数与耗时
- 完全一致 → `✅ 两路径完全一致` 一行收尾；路径不存在 → `[不存在] <绝对路径>`

## 关键实现（src/real_tools.py）

- **相对路径对齐**：`_walk` 递归收集两侧行为 `rel_path → Path` 字典（分隔符归一 `/`），交集按相对路径配对——`A/src/x.py ↔ B/src/x.py` 自动对上，深层子目录无需拍平
- **差异判定两段式**：size 快筛（不等即差异）→ 同 size 才 sha1 精判（二进制同样适用；size=0 直接视为相同省 hash）；OSError 读取失败按差异计入并标注 `(读取出错)`
- **跳过目录**：`_SKIP_DIRS` = `.git` / `__pycache__` / `node_modules` / `.idea` / `.vs` / `.agt` / `.venv`（相对路径任一层命中即跳）
- **沙箱语义**：相对路径按 workspace 解析，绝对路径直接用——纯只读盘点、无写侧，与 [diff_files 的读放行](diff-files.md#读写不对称越界路径放行2026-08新) 同精神

## 配套三件

- **`REAL_TOOLS` 注册**：`max_items` 带参数描述；注册后需 `/restart` 才在当前进程工具箱可见
- **tool_briefs.py 简介**：`"对比两个目录/路径，列出哪边多了/少了/改了哪些文件（不进内容）"`——工具卡片与模型选型依据（见 [工具表单模式](tool-form.md)）
- **远端路由白名单**（src/agent.py `_REMOTE_ROUTABLE` 文件系一组）：`diff_paths` 恒定注入 `remote_instance_id` 路由参数——可直接盘点远端实例机器上的两份代码树（如发版前对比远端部署 vs 本地仓库），机制见[多实例组网](../architecture/multi-instance.md)

## 与其他 diff 能力的关系

| 能力 | 粒度 | 用途 |
|------|------|------|
| [dir_snapshot / diff_snapshots](../architecture/snapshot-diff.md) | 目录 · mtime | 哪些文件变了（会话快照体系内） |
| **diff_paths**（本页） | 两路径（目录/文件） · 存在性 + 内容指纹 | 哪些文件多了/少了/改了（任意两路径即时盘点，不依赖快照体系） |
| [diff_files](diff-files.md) | 单文件 · 行级内容（可分段） | 具体改了什么（内容审计） |
| [diff_lines](diff-lines.md) | 内存文本块 · 行级内容 | 两个文本块按行 Myers diff（无需落盘） |
| 引擎 `_workspace_snapshot` / `_diff_snapshots` | 目录 · mtime | after_tool 副作用检测 → changed_files |

分工一句话：**diff_paths 管「哪些文件不一样」（盘点），diff_files 管「文件里哪里不一样」（审计）**。

## 注意事项

- 只列差异清单不进内容——看到「内容不同」后用 [diff_files](diff-files.md) 逐文件看差异
- `max_items` 折叠是**每组**上限，三组各自计数，超出显示 `…(还有 N 项未列)`
- size 相同才算 sha1——大文件对比先过 size 筛，成本低；size=0 视为相同
- 目录↔文件对比时文件侧以文件名作相对路径键（`_walk` 对 file root 只收 `root.name`）——只在顶层同名时能配上，主要形态还是目录↔目录

## 相关页面

- [diff_files 工具](diff-files.md)：文件级行级内容 diff（内容审计）——「diff_paths 盘点 → diff_files 审计」上下游组合
- [diff_lines 工具](diff-lines.md)：文本级 Myers Diff（外置件 diff_tools.py）
- [dir_snapshot / diff_snapshots](../architecture/snapshot-diff.md)：目录级 mtime 快照对比（会话内快照体系，与本工具的任意两路径即时盘点互补）
- [多实例组网](../architecture/multi-instance.md)：`_REMOTE_ROUTABLE` 白名单机制
- [工具表单模式](tool-form.md)：tool_briefs.py 简介的消费端
