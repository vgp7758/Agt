# grep · 内容搜索工具（real_tools.py · 引擎内置）

> `src/real_tools.py`（引擎内置工具，非外置件）。与 glob_files（按名字找文件）互补；hidden=False，主 Agent 直接可见。

## 职责

按内容搜索文件（**默认正则** / 字面可选），返回带行号的匹配；每个文件头部附 `file_version`（传给 insert/delete/move 的 version 参数）。

## 用法与签名

```
grep(pattern, path=".", glob="", context=0, max_results=?, regex=True)
```

| 参数 | 说明 |
|------|------|
| `pattern` | 搜索模式，**默认按正则**（支持 a\|b 多选一、. * 等元字符，与 ripgrep 一致）；字面匹配传 `regex=False`（pattern 含特殊字符报正则错时用） |
| `path` | 文件或目录（默认 workspace 根） |
| `glob` | 文件名过滤，如 `'*.js'` |
| `context` | 每条命中前后各显示几行（默认 0=只显示匹配行） |
| `max_results` | 最多返回匹配数 |
| `regex` | True=正则（默认）；False=字面 |

## 排除语义：gitignore 跳过（2026-09-02，commit 725d257）

**此前 `rglob("*")` 裸递归——什么都不排**，`src/__pycache__/*.pyc` 二进制乱码常年污染结果。修复：`os.walk` 递归 + 目录级剪枝三件套：

- **硬清单**：`.git / __pycache__ / node_modules / .venv / venv` 等（何时都排）
- **workspace `.gitignore` 全模式**（与 git 工作区视角一致的排除）
- **嵌套 git 仓库整棵剪枝**（子 repo 不进外层搜索）

**显式进排除区 → 豁免**：默认搜索（path="."）跳过排除项；但**显式指定**（`path="blog"`、单文件路径）命中排除区时尊重意图照搜——只硬排 .git/__pycache__（本 repo 的 blog/、models.py、design/ 都在 gitignore 里，显式指定是唯一搜法）。`path` 是**文件**时也只搜该文件（显式意图不过滤）。

## 与其他搜索能力的分工

| 需求 | 工具 |
|------|------|
| 按内容搜文件 | **grep** |
| 按名字模式找文件（跨层递归） | glob_files |
| 列目录一层 | list_dir |
| 定位函数/类定义 | find_function |
| 目录变更检测（快照） | dir_snapshot / diff_snapshots（engine 内部 `_workspace_snapshot`） |

排除语义与 glob_files / 引擎快照 diff 同源收敛：grep 与 glob_files 同 commit（725d257）补齐；快照 diff 的 gitignore 剪枝更早（agent.py `_make_gitignore_filter`，第 128 轮）；glob_files 外置件自带同款语义的轻量复制版——详见 [glob_files 排除语义](glob-files.md)。

## 注意事项

- 正则默认可能误伤：搜 `a.py` 字面时 `.` 会被当通配——用 `regex=False`
- 排除项搜不到不是没文件：先确认路径是否在 .gitignore；显式指定 path 豁免
- max_results 截断大结果集；超大文件命中只返回片段行

## 相关页面

- [glob_files](glob-files.md)——文件名查找 + 同款 gitignore 排除语义
- [snapshot-diff](../architecture/snapshot-diff.md)——快照 diff 的 gitignore 剪枝（engine 侧同源）
- [tool-externalization](tool-externalization.md)——glob_files 外置载体（grep 留引擎内置）