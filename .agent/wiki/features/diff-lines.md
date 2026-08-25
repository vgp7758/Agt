# diff_lines · 文本级 Myers Diff 工具（外置件 diff_tools.py）

> 源码：`tools/builtin/diff_tools.py`（+ 随包副本 `src/assets/tools_builtin/`）——2026-08 纯函数批从 LIGHT_TOOLS 外置（commit 17312eb）：本体与注册整体迁入外置件，Myers 三件套是框架 `diff_files` 实现的**同源副本**（复制实现而非 import 框架，外置件零框架依赖约定；随机重放 200/200 回归）。
> 职责：对比两个文本块，Myers Diff 最优编辑脚本 + unified 风格 hunk 输出，无需落盘。2026-08 落地（commit 9fb00de）。

## 职责与用法

`diff_lines(a_text, b_text, context=2)`：

- **a_text / b_text**：两个文本块字符串（通常接上游节点输出，如两个 LLM 节点的改前改后草稿）
- **context**：每个 hunk 前后的上下文行数（默认 2）
- **典型用途**：工作流节点间比较文本无需落盘——两个 LLM 节点的输出、改前改后草稿、两段代码，直接 plugin 节点接上游 ref 就能比

注册带 param_descriptions（a_text「改前文本（接上游节点输出）」/ b_text「改后文本」/ context「每个 hunk 前后的上下文行数（默认 2）」），编辑器节点浮窗可见用途提示。

**输出（unified 风格）**：

- 头部：`[diff a (N行) vs b (M行) | -x +y | h 处差异]`
- hunk 头：`@@ -起,数 +起,数 @@`
- 变更行：`-N│ ...`（A 侧行删除）/ `+N│ ...`（B 侧行插入），**行号语义与 read_file 一致**
- 上下文行：两空格前缀（不带行号）
- 相同文件 → `[无差异] ...`；输出超 20k 截断

实测输出示例：

```
[diff a (7行) vs b (7行) | -3 +3 | 1 处差异]
@@ -1,7 +1,7 @@
-1│ def add(a, b):
-2│     return a + b
+1│ def add(a, b, c=0):
+2│     return a + b + c
  
  
  def main():
      print(add(1, 2))
-7│     print('done')
+7│     print('bye')
```

## 实现：与 diff_files 同源副本（外置件零依赖约定）

- **Myers 三件套（`_myers_diff` 纯算法 / unified 渲染 / 工具入口）**：与框架 `src/real_tools.py` 的 diff_files 实现**同源副本**——外置件不 import 框架（零框架依赖约定），Myers 是稳定经典算法，双份各自带回归（外置侧随机重放 200/200 全过）；两侧注释互指路，**改动时两处同步**
- `_render_unified_diff` 带 `a_offset`/`b_offset` 参数（diff_files 分段对比时行号还原绝对行号用）；diff_lines 不传（默认 0，全文对比行号本就绝对）
- `diff_lines`：工具入口——收文本字符串 → 按行 split → Myers → unified 渲染；输出格式与 diff_files 完全一致

## 与其他 diff 能力的关系

| 能力 | 粒度 | 用途 |
|------|------|------|
| [dir_snapshot / diff_snapshots](../architecture/snapshot-diff.md) | 目录 · mtime | 哪些文件变了（files/count/changed） |
| [diff_files](diff-files.md) | 单文件 · 行级内容 | 具体改了什么（需落盘；支持 range_a/range_b 分段对比；算法原件在本体） |
| **diff_lines**（本页） | 内存文本块 · 行级内容 | 两个文本块按行 Myers diff（无需落盘） |
| 引擎 `_workspace_snapshot` / `_diff_snapshots` | 目录 · mtime | after_tool 副作用检测 → changed_files |

典型组合：diff_snapshots 定位变更清单 → diff_files 逐个看内容；或工作流节点间文本对比直接用 diff_lines。脚本产两份产物再对比可用 [run_python args](run-python.md) 跑脚本 + diff_lines 比较。

## 注册形态与热加载（2026-08 外置，commit 17312eb）

- 原注册于 `LIGHT_TOOLS`（整箱 hidden，不投影给 LLM，仅工作流节点可用）；外置后注册随 `diff_tools.py` 的 `agt_register` 走，工具语义不变
- 配合 plugin 节点（type 4），可直接接上游节点输出比较
- 改完外置件用 `/reload tools` 热加载，**不需要重启**（src 内注册时代需 `/restart`，外置后降一档）；随包副本 `src/assets/tools_builtin/diff_tools.py` 需同步
- 外置背景：判别标准全量过筛 real_tools，LIGHT_TOOLS 13→5（见 [工具外置](tool-externalization.md)、[判别标准 · 纯函数批](../architecture/tool-externalization-criteria.md)）

## 注意事项

- 外置件改动走 `/reload tools` 热加载；随包副本需同步
- 输出格式与 diff_files 一致（同源渲染副本）
- 变更行带行号（`-N│`/`+N│`），上下文行仅两空格前缀——解析输出时注意区分
- 文本块对比无分段能力（无 range 参数）——大文本分段对比需先落盘走 [diff_files 的 range_a/range_b](diff-files.md#分段对比range_arange_b2026-08-20新commit-096fcbe)
- 算法与框架 diff_files 是**双份副本**——改渲染/算法两处同步（互指路注释）

## 相关页面

- [diff_files 工具](diff-files.md)：文件级 diff（沙箱路径/读写不对称/range_a 分段对比/回溯层错位 bug）——本工具算法的同源原件
- [工具外置](tool-externalization.md)：外置体系（diff_tools.py 是纯函数批成员）
- [工具外置判别标准](../architecture/tool-externalization-criteria.md)：LIGHT_TOOLS 13→5 全量过筛
- [run_python 工具](run-python.md)：脚本参数化执行，常与 diff_lines 配合
- [dir_snapshot / diff_snapshots](../architecture/snapshot-diff.md)：目录级 mtime 快照对比，与本工具互补
- [系统总览](../architecture/overview.md)：能力层
- [工作流引擎与钩子](../architecture/workflow-hooks.md)：plugin 节点 / 隐藏工具机制
