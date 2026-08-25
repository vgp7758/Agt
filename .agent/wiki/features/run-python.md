# run_python · Python 子进程执行工具（code/file 双模式 + args 参数化）

> 源码：`src/real_tools.py`（`run_python` + `_run_subprocess_streaming`）
> 职责：独立子进程运行 Python，实时流式输出 + 30 秒心跳进度。2026-08 新增 `args` 参数（commit 9fb00de），已保存脚本可参数化复用。

## 签名与用法

```python
run_python(code="", file="", args="")
```

- **code / file 二选一**：
  - `code`：一段内联 Python 代码（写临时文件再跑）
  - `file`：运行已存在的 .py 文件——跑已保存脚本用这个，别再用 subprocess 包壳
- **args**：传给脚本的参数字符串，子进程经环境变量 `PY_ARGS` 读取：

```python
import os
a = os.environ.get("PY_ARGS", "")   # 可放 JSON/CSV 等任意格式，脚本自行解析
```

典型用法（脚本参数化复用）：

```python
run_python(file="tools/analyze.py", args='{"src": "main.py", "dst": "backup.py"}')
```

## args 参数化设计（commit 9fb00de）

- **code 和 file 两模式都生效**——同类任务不同参数不必改脚本代码
- 实现机制：`_run_subprocess_streaming` 新增 `env` 参数（None=继承父进程环境）；`args` 非空时把 `PY_ARGS` 注入子进程环境变量，**不传则完全不注入**（脚本端 `os.environ.get("PY_ARGS", "")` 得空串）
- 与 `run_script` 的 `PAYLOAD` 环境变量同款机制——工具级约定：**参数不进 argv，走环境变量**，规避命令行转义/引号问题
- `file` 路径走 `_resolve` 严格沙箱（须在 workspace 内）；workspace 外路径可经 args 传入、脚本内自行读

## 流式执行基础设施

`_run_subprocess_streaming(args, name, shell=False, env=None)`：

- 实时流式输出 + 30 秒心跳进度（reader 线程兼容 Windows）
- 经 `_tool_emit` 回调推送 tool_stream / tool_progress 事件（前端气泡实时可见）
- `cwd` 固定 WORKSPACE
- Windows 加 CREATE_NO_WINDOW：detached（/restart 看门狗拉起）进程无控制台时，子进程默认各弹一个终端窗，闪退即此（见 [ops 常见错误对照](../guides/ops.md#常见错误对照)）

## 与其他模块的关系

- 工具箱真实工具（LLM 可直接调用），也可在工作流 plugin 节点（type 4）中使用
- 引擎副作用检测：run_python 在子进程里改文件绕过工具级跟踪，靠 mtime 快照 diff 兜底（见 [系统总览 · 关键设计决策](../architecture/overview.md)）
- 与 [diff_files](diff-files.md)（real_tools）/ [diff_lines](diff-lines.md)（外置件 diff_tools.py，2026-08 起）常配合使用（脚本产两份产物 → diff 对比）

## 注意事项

- 参数走环境变量而非 argv——脚本内读 `os.environ.get("PY_ARGS")`，不解析 sys.argv
- `args` 是字符串：复杂结构建议 JSON 序列化传入，脚本端 `json.loads` 还原
- 当前进程注册表更新需 `/restart` 后新参数才生效

## 相关页面

- [工作流引擎与钩子](../architecture/workflow-hooks.md)：plugin 节点调用工具 / LIGHT_TOOLS 隐藏工具
- [diff_files](diff-files.md) / [diff_lines](diff-lines.md)：文件级 / 文本级 Myers Diff
- [运维排障](../guides/ops.md)：子进程 CREATE_NO_WINDOW 与流式输出观测
- [系统总览](../architecture/overview.md)：能力层 real_tools.py
