# diff_files · 文件级 Myers Diff 工具

> 源码：`src/real_tools.py`（`_myers_diff` 纯算法 + `_render_unified_diff` 公共渲染，工具入口 `diff_files` 注册于 `REAL_TOOLS`）
> 职责：对比两个文件内容，Myers Diff 最优编辑脚本 + unified 风格 hunk 输出。2026-08-19 落地（commit b869939）；同批新增共享渲染供 [diff_lines](diff-lines.md) 复用（commit 9fb00de）。

## 职责与用法

`diff_files(file1, file2, context=2)`：

- **file1 / file2**：支持完整路径或相对路径，经 `_resolve_read` 沙箱解析——越出 workspace 放行（可对比 workspace 外的备份/参照文件），写操作仍走严格沙箱（读写不对称：读放行、写拦截）。
- **context**：每个 hunk 前后的上下文行数（默认 2）
- **典型用途**：对比「快照回溯点 vs 当前」「备份 vs 改后」——配合 snapshots 的 `/rewind` 或外部生成的备份文件，一眼看清改了什么

**输出（unified 风格）**：

- 头部：`[diff a (N行) vs b (M行) | -x +y | h 处差异]`
- hunk 头：`@@ -起,数 +起,数 @@`
- 变更行：`-N│ ...`（A 侧删除）/ `+N│ ...`（B 侧行插入），**行号语义与 read_file 一致**
- 上下文行：两空格前缀（不带行号）
- 相同文件 → `[无差异] ...`；文件不存在 → `[文件不存在] <path>`；输出超 20k 截断

实测输出示例：

```
[diff _d1.py (7行) vs _d2.py (7行) | -3 +3 | 1 处差异]
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

## 实现：纯算法 + 工具入口分层

- **`_myers_diff(a_lines, b_lines)`**：纯算法函数。输入两个行列表，输出差异列表 `[(action, line)]`，action 为 `'-'`（删除，A 侧行）/ `'+'`（插入，B 侧行）/ `' '`（相同）。标准 Myers 贪心 + trace 快照回溯；`trace[d]` 存 d 轮开始前（= d-1 轮结束后）的 V 快照，供回溯还原编辑链
- **`_render_unified_diff`**：公共渲染（Myers ops → unified diff 文本），diff_files / diff_lines 共用
- **`diff_files`**：工具入口——收路径 → 读两文件 → `_myers_diff` → hunk 分组（变更段间隔 > 2×context+1 分组，各扩 context 行上下文）→ `_render_unified_diff`
- 已注册 `REAL_TOOLS`，`context` 带参数描述

## 回溯层错位 bug（copy 代码的教训）

用户 copy 的标准 Myers 线性空间版思路正确，但回溯有一处真 bug：

```python
# copy 版（错）：
prev_x = trace[d - 1].get(prev_k, 0)   # ❌ 取了 d-1 层快照
# 修复：prev_x 与 prev_k 判断同层（trace[d]，即 d 轮开始前的 V 快照）
```

**语义**：`prev_k` 的判断基于 `trace[d]`（第 d 步编辑的出发点），`prev_x` 却从 `trace[d-1]` 取——**错一层**，回溯沿着错误编辑链走，产生非法 diff 序列（重放重建乱序、`B[y]` IndexError 越界）。

**验证手段**（可作同类算法的标准校验套件）：

- 随机重放重建 2000/2000 通过：按 diff 序列从 A 重建 B
- 最优性 DP 对照 500/500 通过：diff 长度 = 经典 DP 编辑距离
- 边界 case 全过：空文件 / 完全相同 / 全删 / 全增
- 沙箱：越界路径正确拒绝

## 与其他 diff 能力的关系

| 能力 | 粒度 | 用途 |
|------|------|------|
| [dir_snapshot / diff_snapshots](../architecture/snapshot-diff.md) | 目录 · mtime | 哪些文件变了（files/count/changed） |
| **diff_files**（本页） | 单文件 · 行级内容 | 具体改了什么 |
| **diff_lines**（LIGHT_TOOLS，2026-08） | 内存文本块 · 行级内容 | 两个文本块按行 Myers diff（无需落盘） |
| 引擎 `_workspace_snapshot` / `_diff_snapshots` | 目录 · mtime | after_tool 副作用检测 → changed_files |

典型组合：diff_snapshots 定位变更清单 → diff_files 逐个看内容；或工作流节点间文本对比直接用 diff_lines。

## 读写不对称：越界路径放行（2026-08 新）

新增 `_resolve_read`：先按 workspace 沙箱解析，**越界（绝对路径 / `../` 逃逸）放行为直接路径**——现在可以对比 workspace 外的备份/参照文件。写操作仍走 `_resolve` 严格沙箱，**读写不对称**：读放行、写拦截。

注意边界：仅纯只读工具用 `_resolve_read`；`run_python(file=...)` 的 file 参数仍走 `_resolve` 严格沙箱（workspace 外脚本须经 code 模式或 args 间接传入，见 [run_python](run-python.md)）。

## 注意事项

- 工具注册后需 `/restart` 才在当前进程工具箱可见
- 与 mtime 快照语义不同：diff_files 直接读文件内容对比，两文件都不在快照体系内（如外部备份）也可用
- 变更行带行号（`-N│`/`+N│`），上下文行仅两空格前缀——解析输出时注意区分

## 相关页面

- [diff_lines 工具](diff-lines.md)：文本级 Myers Diff（LIGHT_TOOLS，共享渲染，无需落盘）
- [run_python 工具](run-python.md)：file 模式严格沙箱 vs 本工具读放行
- [dir_snapshot / diff_snapshots](../architecture/snapshot-diff.md)：目录级 mtime 快照对比，与本工具互补
- [系统总览](../architecture/overview.md)：能力层 real_tools.py
