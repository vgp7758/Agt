# /cmd · 消息框直接执行 shell 命令（免开终端；2026-10-15，用户提案）

> src/commands.py `_cmd_cmd` + `reg.register("cmd", …)`。用户提案：「有时候我们还是会需要打开文件夹、打开一个终端去敲指令，感觉不如在消息里直接敲 `/cmd pip install -e .` 来的方便」。

## 职责与用法

CLI / WebUI 输入框里直接执行 shell 命令，输出立即回显——`pip install -e .` / `git status` / `dir` 这类短命令不用再开终端/资源管理器：

```
/cmd pip install -e .        # cwd=workspace 安装当前包
/cmd git status -sb          # 看仓库状态
/cmd start .                 # 真想开资源管理器时也行
```

## 行为（复用 run_python 系的子进程基建）

| 特性 | 说明 |
|---|---|
| **cwd** | 固定 workspace（当前 repo），回显带 `(cwd=…)` |
| **超时转后台** | 复用 `real_tools._run_subprocess_streaming`——超过工具超时**不杀进程**，转后台 + 完成自动通知回执（`pip install` 不用干等） |
| **编码自适应** | utf-8 → gbk 探测解码（cmd 老工具中文输出不乱码，同 run_python） |
| **长输出截断** | 前 6000 + 后 2000 字符，中间标注省略 |
| **免审批** | 跳过 [run_shell 的审批门](tool-approval.md)——用户在输入框**亲手敲的命令即显式授权**，无需二次确认 |

## 关键坑：Windows 下 shlex 单引号是字面字符

首版用 `shlex.join` 重组参数——它的 **POSIX 单引号在 cmd.exe 是字面字符**，实测 `echo 'x'` 原样带引号输出、`python -c 'print(1+1)'` 直接语法错误。修复分两层（src/commands.py）：

1. **主路径：dispatch 原始行直通**——`/call` 参数含 JSON 早有「shlex 会破坏引号 → 传原始字符串」的特例，`/cmd` 并入同款（`if name == "call" or name == "cmd"`）：shell 命令的引号/管道/重定向**你敲什么就执行什么**
2. **list 兜底**（不经 dispatch 直接调本函数时）：Windows 用 `subprocess.list2cmdline`（cmd 只认双引号），POSIX 才用 `shlex.join`

修复后实测：`python -c "print(40+2)"` → `42`、`echo A & echo B` → `A`/`B`（cmd 连接符保真）。

## 安全边界：只认 ("user", …) 队列形态

探查整条消息链路后确认——`/cmd` **只能从 CLI/WebUI 输入框手敲触发**：

- 所有 `("user", …)` 队列投递点都在用户输入通道上
- **callback 注入 / 定时任务 / 子 Agent 回执一律走 `("background", …)`** → 只会成为消息文本发给 Agent，**不会被分发为命令**
- 即：[外部注入](external-injection.md) 无法借道 `/cmd` 执行 shell——与既有 `/api/tool_call` 同信任级（仅限能连到本实例输入通道的人），不新增攻击面

## 注册与生效

- `reg.register("cmd", _cmd_cmd, …)`：CLI/Web 两入口走同一 dispatch 表自动可用；src/chat.py `/help` 命令清单同步
- **需 `/restart` 生效**（命令表启动时构建）

## 验证

五场景实测全过：引号保真（python -c）/ cmd 连接符（`&`）/ 超时转后台 + 完成通知 / 中文编码 / 长输出截断。代码 `src/commands.py` 本地 commit 已做（push 当时撞网络受阻，挂静默重试）。

## 相关页面

- [run_python · Python 子进程执行](run-python.md) —— `_run_subprocess_streaming` 的产地：流式输出 / 编码自适应 / 超时转后台 + 完成自动通知 全部同源
- [工具执行审批](tool-approval.md) —— run_shell / run_python 的三按钮放行；/cmd 有意绕过（亲手敲=已授权）
- [/ask · 无状态旁路询问](ask-command.md) —— 姊妹斜杠命令先例（同为命令表注册、/restart 生效）
- [外部事件注入](external-injection.md) —— background 队列形态的来源；正是它进不了命令分发才保住 /cmd 的安全边界
