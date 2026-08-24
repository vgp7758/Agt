# glob_files · 文件名模式查找工具（外置工具首例）

> `tools/builtin/fs_tools.py`（+ 随包副本 `src/assets/tools_builtin/fs_tools.py`）。2026-08 新增（commit eafed25），补齐文件查找缺口：`list_dir` 只列单层、`grep` 只搜内容——此前**没有文件名模式查找工具**。hidden=False，主 Agent 直接可见。

## 职责与用法

按通配模式匹配**文件名**（不搜内容——搜内容用 grep）：

```
glob_files("**/*.py", "src")              → glob '**/*.py' @ src，命中 62 个文件 + 路径列表
glob_files("*.xml", ".agent/workflows")   → 22 个工作流
glob_files("**/*.pyc")                    → 无匹配（__pycache__ 已排除）
```

参数：
- `pattern`：`**` 递归 / `*` 单层通配 / `?` 单字符 / `[abc]` 字符集。如 `docs/**/*.md`、`test_*.py`、`agents/*.yml`
- `path`：起始目录，留空 = workspace 根

## 关键实现（fs_tools.py）

- `_EXCLUDE_DIRS`：遍历时自动排除 `.git / __pycache__ / node_modules / .venv / venv / dist / build / .idea / .vscode`
- `_MAX_RESULTS = 500`：命中条数上限，防止巨型匹配爆屏
- 输出含头部统计行（pattern + 命中数）+ 路径列表

## 验证插曲（排除逻辑确认）

场景 3 的检查输出 `.pyc in out3 = True` 是**误报**——那来自头部统计行回显的 pattern 字符串本身（`'**/*.pyc'`）被子串检查命中，实际匹配列表为空，`__pycache__` 排除逻辑工作正常。**教训：检查工具输出时区分「统计行回显」与「数据行」**。

## 文件查找工具分工

| 需求 | 工具 |
|------|------|
| 按名字模式找文件（跨层递归） | **glob_files** |
| 按内容搜文件 | grep |
| 列某目录一层 | list_dir |
| 定位函数/类定义 | find_function |

## 外置工具体系首例

fs_tools.py 是[工具外置](tool-externalization.md)体系的第一个实例（**纯函数整体外置**：实现与注册都在外置件里）：一个 .py 文件 + `agt_register()` 返回描述符列表，零框架改动；`/reload tools` 热加载即生效，随包副本同步进 `src/assets/tools_builtin/`。第二例 [rag_tools.py](rag.md)（rag_query）是另一种形态——注册外置、实现留框架（共享单例 embedder），见 [判别标准 · rag 边界裁剪](../architecture/tool-externalization-criteria.md#rag-外置的边界裁剪混合形态2026-08-commit-71e0b90)。

## 注意事项

- 只匹配文件名不匹配内容；模式本身要合法（如 `[abc]` 字符集语法）
- 500 条截断：超大匹配集建议收窄 pattern 或指定 path

## 相关页面

- [工具外置](tool-externalization.md) —— tools/builtin 体系（本工具的载体，两实例两种形态）
- [rag](rag.md) —— 外置第二例（注册外置 + 实现留框架）
- [diff_lines](diff-lines.md) / [get_list-item](get-list-item.md) —— 同期新增工具（LIGHT_TOOLS 隐藏；本工具相反，hidden=False 主 Agent 可见）
