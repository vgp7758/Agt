# run_python · Python 子进程执行工具（code/file 双模式 + args 参数化）

> 源码：`src/real_tools.py`（`run_python` + `_run_subprocess_streaming`）
> 职责：独立子进程运行 Python，实时流式输出 + 30 秒心跳进度。2026-08 新增 `args` 参数（commit 9fb00de），已保存脚本可参数化复用。

## 签名与用法

```python
run_python(code="", file="", args="")
```

- **code / file 二选一**（何时选哪个见 [inline vs file 经济学](#inline-vs-file-经济学参数描述写入迭代决策指引2026-08commit-6caf290)）：
  - `code`：一段内联 Python 代码（写临时文件再跑）——一次性验证/分析首选
  - `file`：运行已存在的 .py 文件——调试迭代首选（edit 差量改 + 重跑），跑已保存脚本用这个，别再用 subprocess 包壳
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

## inline vs file 经济学：参数描述写入迭代决策指引（2026-08，commit 6caf290）

用户观察：**有了 run_python 后 agent 特别爱用它**——而 Claude Code 没有 inline 模式，每次都是「写本地文件 → 跑 → 删」三步。数据分析证实了偏好，也定位了真正的浪费点。

## 实测数据（单 session llm_calls 分析，2026-08）

| 指标 | 数值 |
|---|---|
| run_python 总调用 | 1139 次（该 session 最常用工具） |
| inline（code）vs file | **1116 : 23（98% inline）** |
| inline 总量 | 113 万字符，中位单次 740 字符 |
| 迭代重发（相邻两次相似度 >0.6 = 同一脚本改了重跑） | 122 次 |
| 迭代重发浪费 | 16.9 万字符 ≈ 4.2 万 tokens（占 inline 总量 15%） |

## 两个结论：偏好本身不是错，错在迭代场景

- **一次性验证/分析，inline 真优**：1 次调用搞定 vs Claude Code 的 write→run→delete 3 次工具往返；不污染 workspace、不用记得清理。98% inline 里大部分属于此类，不动。
- **迭代调试，inline 反而贵**：脚本报错 → 整段代码重发（实际只改几行）→ 又报错 → 又重发。file 路径下 `edit` 是**差量传输**，重发是**全量传输**。实测一对相似度 0.89 的相邻调用重发了 2711 字符——只改了几行。

```
inline 迭代：脚本报错 → 整段代码重发 → 又报错 → 又重发…
file 迭代：  脚本报错 → edit 只发改动那几行 → 重跑 file= → …
```

## 修复：决策指引写进 param_descriptions（最小干预）

不删 inline（会伤害约 85% 的正确场景），把「何时用哪个」写进参数描述——**LLM 决定怎么调的时候一定看得到的地方**（`src/real_tools.py`）：

- `code` 补充：*适合一次性验证/分析（1 次调用搞定）；预计要反复调试改跑的脚本，先 write_file 落盘再 run_python(file=...)，后续 edit 改+重跑——整段代码重发比 edit 差量贵得多*
- `file` 补充：*调试迭代场景首选：edit 精确改文件后重跑，不用整段重发*

这是**工具描述驱动行为**的路线：把最佳实践写在 Agent 决策时一定看得到的地方，而不是指望它自己悟（与 vision 派活带 `<img>` 标签同款手法）。

生效：`/restart` 后新 schema 生效。预期一次性验证照旧 inline，"会跑几轮的脚本"先落盘——122 次迭代重发（4.2 万 tokens）是未来最直接的缩减目标。

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
