# /cmd · 消息框直接执行 shell 命令（免开终端；2026-10-15 内置落地，同日迁插件 cmd 播种）

> **现址 `.agent/plugins/cmd/backend.py`**（种源 `src/assets/plugins/cmd/`，纯后端无 frontend；commit b3fb607）。2026-10-15 用户提案：「有时候我们还是会需要打开文件夹、打开一个终端去敲指令，感觉不如在消息里直接敲 `/cmd pip install -e .` 来的方便」——当天内置落地（src/commands.py `_cmd_cmd`）；同日用户追问「插件是不是也支持 /customCommand」，确认支持后**迁移为插件用法示例播种**——`api.register_command` 的最小真实用例（命令语义零变化，函数体原样搬家）。

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

首版用 `shlex.join` 重组参数——它的 **POSIX 单引号在 cmd.exe 是字面字符**，实测 `echo 'x'` 原样带引号输出、`python -c 'print(1+1)'` 直接语法错误。修复分两层（主路径在 src/commands.py 的 dispatch；list 兜底随函数迁入插件 backend.py）：

1. **主路径：dispatch 原始行直通**——`/call` 参数含 JSON 早有「shlex 会破坏引号 → 传原始字符串」的特例，`/cmd` 并入同款（`if name == "call" or name == "cmd"`）：shell 命令的引号/管道/重定向**你敲什么就执行什么**。特例按**命令名**匹配——迁插件后命令同名同样命中；插件禁用后 dispatch 找不到命令自然报「未知命令」，特例无害
2. **list 兜底**（不经 dispatch 直接调本函数时）：Windows 用 `subprocess.list2cmdline`（cmd 只认双引号），POSIX 才用 `shlex.join`

修复后实测：`python -c "print(40+2)"` → `42`、`echo A & echo B` → `A`/`B`（cmd 连接符保真）。

## 安全边界：只认 ("user", …) 队列形态

探查整条消息链路后确认——`/cmd` **只能从 CLI/WebUI 输入框手敲触发**：

- 所有 `("user", …)` 队列投递点都在用户输入通道上
- **callback 注入 / 定时任务 / 子 Agent 回执一律走 `("background", …)`** → 只会成为消息文本发给 Agent，**不会被分发为命令**
- 即：[外部注入](external-injection.md) 无法借道 `/cmd` 执行 shell——与既有 `/api/tool_call` 同信任级（仅限能连到本实例输入通道的人），不新增攻击面

## 注册与生效（迁插件后）

- **插件注册**：`backend.py` 的 `setup(api)` 里 `api.register_command("cmd", _cmd_cmd, help, detail)`——`append_commands` 并入 registry，CLI / WebUI / WS 三处构建点自动生效
- `/help` 自动列出；**插件禁用/卸载自动摘除**（`/plugins disable cmd` 后敲 `/cmd` 即报「未知命令」）
- **热生效**：改 backend.py 后 `/reload plugins` 即生效（内置时期需 `/restart`——迁插件顺带把这条生效惯例改了）
- 本 repo 已实播 `.agent/plugins/cmd/`（`seed_default_plugins` 播种 + seed_state 基线记账，`/update-assets` 可对账）；种源在 pip 包内 `src/assets/plugins/cmd/`，新 workspace 首启自动播种

## 同日三迁的第三步：内置 → 插件（register_command 用法示例播种，commit b3fb607）

> 用户追问（同日）：「插件是不是也支持 /customCommand」——确认 `register_command` 支持后，应用户要求「把 /cmd 迁移为一个插件的这种用法的示例播种」。命令语义零变化（函数体原样搬家），变化只在注册方式与代码位置。

- **种源**：`src/assets/plugins/cmd/`（manifest.yml + backend.py，纯后端无 frontend）；**现场**：`.agent/plugins/cmd/` 已实播
- **内置退役**：src/commands.py 删 `_cmd_cmd` 函数 + `reg.register("cmd", …)` 注册块（-46 行）→ 改由 backend.py `api.register_command` 挂载
- **dispatch 特例保留**（见上「关键坑」节）：按命令名匹配而非注册来源——插件命令同名同样命中，这也是「插件命令深度定制解析」的活示例
- **文档同步**：种源 `src/dir_docs/plugins.md` + 现场 `.agent/plugins/README.md` 示例清单「三个实用示例」→「四个」（+cmd，与 open-folder / feedback / webide 并列）
- **价值**：`register_command` 从文档假想变成随包可跑的实物模板——backend.py docstring 完整记录前世今生 / 命令语义 / 安全边界 / 热生效方式，想给插件加自定义斜杠命令直接抄它

## 验证

**内置时期**（2026-10-15）：五场景全过——引号保真（python -c）/ cmd 连接符（`&`）/ 超时转后台 + 完成通知 / 中文编码 / 长输出截断。

**迁插件后**（2026-10-16，commit b3fb607）：`/reload plugins` 热生效——插件 5 个加载（cmd / example-hello / feedback / open-folder / webide）；py_diag 语法过；无需 `/restart`。

## 相关页面

- [插件 SDK · 随包播种示例](../architecture/plugin-sdk.md) —— cmd 现为五例播种插件之一；`register_command` 协议描述见该页「PluginAPI」节
- [run_python · Python 子进程执行](run-python.md) —— `_run_subprocess_streaming` 的产地：流式输出 / 编码自适应 / 超时转后台 + 完成自动通知 全部同源
- [工具执行审批](tool-approval.md) —— run_shell / run_python 的三按钮放行；/cmd 有意绕过（亲手敲=已授权）
- [/ask · 无状态旁路询问](ask-command.md) —— 姊妹斜杠命令先例（同为命令表注册，但仍是内置）
- [用户交互 · @name 直派子 Agent](user-interaction.md) —— 输入框的另一个「直给」入口：`/cmd` 执行命令、`@name <任务>` 派活子 Agent（同为「用户亲手敲 = 显式授权/显式派活」，不走 LLM 转手）
- [外部事件注入](external-injection.md) —— background 队列形态的来源；正是它进不了命令分发才保住 /cmd 的安全边界

