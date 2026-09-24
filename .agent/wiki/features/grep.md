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

## 扫描预算四层防护：.agt 硬排除 + 2MB 上限 + 二进制检测 + 25s/3 万文件预算（2026-09-23，用户实锤 8000 阻塞，commit bb89a97）

**事件（2026-09-23，8000 实例）**：整个实例「被阻塞」——HTTP/WS 假死、`/api/status` 超时、events/llm_calls 停摆 30+ 分钟。py-spy 线程栈实锤：agent 在 explore 里调了 `grep(pattern="assembly", path=".")`，卡在逐文件读盘（`dirpath: comfy\tools\builtin`，已扫过 `.agt`）。

**根因链**（四个「无」叠加）：

```
comfy\.agt\snapshots = 框架快照 git 对象库（3273 文件 / 12GB 二进制）
  ✗ 不在 _HARD 排除集（当时只有 .git/__pycache__/node_modules/.venv/venv）
  ✗ 无单文件大小上限 → 逐文件 read_text 全读
  ✗ 无二进制检测 → git 对象（无扩展名）当文本读
  ✗ 无扫描预算 → 无界扫盘
  → 单次 grep 跑 30+ 分钟；读盘+正则持续持 GIL → 事件循环 GIL 饥饿 → HTTP/WS 假死
```

讽刺的是 `.agt` 正是本框架自己的快照仓库——gitignore 通常 ignore 它，但 `.agt` 目录内容并不受嵌套 repo 剪枝保护（快照仓库在 workspace 内部）。

**修复四层防护**（src/real_tools.py，commit `bb89a97`）：

| 防护 | 内容 |
|---|---|
| 硬排除 | `_HARD` 增 **`.agt`**（+ `.mypy_cache`/`.pytest_cache`/`.ruff_cache`/`.cache`）；`dir_outline` 硬排除集同步补 |
| 单文件上限 | `_MAX_FILE_BYTES = 2MB`，超限跳过（源码搜索不需要大文件；防大媒体/对象库） |
| 二进制检测 | 文件首 4KB 含 `\x00` → 跳过（git 对象/媒体一网打尽） |
| 扫描预算 | `_GREP_MAX_SCAN = 30000` 文件 / `_GREP_DEADLINE_S = 25s` 墙钟 → 超限**返回已扫部分**（不空手而归） |

结果尾部**明确披露**（不再静默截断）：

```
...（扫描预算耗尽：扫描耗时超预算 25s；结果可能不全——收紧 path/glob 范围后重试）
（跳过 12 个 >2MB 大文件、3273 个二进制文件）
```

**验证**（临时 workspace 复刻现场）：`.agt` 二进制对象（内含 pattern 5000 次）不再进结果 ✓；3MB 大文件跳过并提示 ✓；真命中完整保留 ✓。

**8000 实例处置**（同轮）：taskkill 卡死旧进程 → detached 拉起 `agt-web 8000 --resume`——858 轮会话完整恢复、`/api/status` 0.1s 响应（修复前超时）、busy 自动续跑；重启后跑的就是带防护的新代码。

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