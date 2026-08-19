# diff_lines · 文本级 Myers Diff 工具（LIGHT_TOOLS）

> 源码：`src/real_tools.py`（`_myers_diff` 纯算法 + `_render_unified_diff` 公共渲染，工具入口 `diff_lines` 注册于 `LIGHT_TOOLS`，hidden）
> 职责：对比两个文本块，Myers Diff 最优编辑脚本 + unified 风格 hunk 输出。2026-08 落地（commit 9fb00de）。

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

## 实现：与 diff_files 共享渲染

- **`_myers_diff(a_lines, b_lines)`**：纯算法函数（见 [diff_files 页](diff-files.md#实现纯算法--工具入口分层)）
- **`_render_unified_diff`**：公共渲染函数——`diff_files` 和 `diff_lines` 共用，将 Myers ops 转为 unified diff 文本。2026-08-20 起（commit 096fcbe）带 `a_offset`/`b_offset` 参数供 diff_files 分段对比时行号还原绝对行号；diff_lines 不传（默认 0，全文对比行号本就绝对）
- **`diff_lines`**：工具入口——收文本字符串 → 按行 split → `_myers_diff` → `_render_unified_diff`

## 与其他 diff 能力的关系

| 能力 | 粒度 | 用途 |
|------|------|------|
| [dir_snapshot / diff_snapshots](../architecture/snapshot-diff.md) | 目录 · mtime | 哪些文件变了（files/count/changed） |
| [diff_files](diff-files.md) | 单文件 · 行级内容 | 具体改了什么（需落盘；支持 range_a/range_b 分段对比） |
| **diff_lines**（本页） | 内存文本块 · 行级内容 | 两个文本块按行 Myers diff（无需落盘） |
| 引擎 `_workspace_snapshot` / `_diff_snapshots` | 目录 · mtime | after_tool 副作用检测 → changed_files |

典型组合：diff_snapshots 定位变更清单 → diff_files 逐个看内容；或工作流节点间文本对比直接用 diff_lines。脚本产两份产物再对比可用 [run_python args](run-python.md) 跑脚本 + diff_lines 比较。

## LIGHT_TOOLS 隐藏工具

- 注册于 `LIGHT_TOOLS`，`hidden=True`——不投影给 LLM，仅工作流节点可用（见 [workflow-hooks 钩子与隐藏工具](../architecture/workflow-hooks.md#每轮对话开始自动扫描注册为-wf_-工具hiddentrue-不投影给-llm钩子子工作流专用)）
- 配合 plugin 节点（type 4），可直接接上游节点输出比较

## 注意事项

- 工具注册后需 `/restart` 才在当前进程工具箱可见
- 与 diff_files 输出格式一致（同用 `_render_unified_diff`）
- 变更行带行号（`-N│`/`+N│`），上下文行仅两空格前缀——解析输出时注意区分
- 文本块对比无分段能力（无 range 参数）——大文本分段对比需先落盘走 [diff_files 的 range_a/range_b](diff-files.md#分段对比range_arange_b2026-08-20新commit-096fcbe)

## 相关页面

- [diff_files 工具](diff-files.md)：文件级 diff（沙箱路径/读写不对称/range_a 分段对比/回溯层错位 bug）
- [run_python 工具](run-python.md)：脚本参数化执行，常与 diff_lines 配合
- [dir_snapshot / diff_snapshots](../architecture/snapshot-diff.md)：目录级 mtime 快照对比，与本工具互补
- [系统总览](../architecture/overview.md)：能力层 real_tools.py
- [工作流引擎与钩子](../architecture/workflow-hooks.md)：LIGHT_TOOLS / hidden 工具机制
