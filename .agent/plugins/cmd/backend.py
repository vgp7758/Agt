"""cmd —— /cmd <shell 命令>：消息框直接执行 shell（register_command 插件化示例）。

前世今生：2026-10-15 用户提案「免开终端，消息框直接敲 /cmd pip install -e .」，
当天作为内置命令落地（src/commands.py 的 _cmd_cmd）；同日用户追问"插件是不是也
支持 /customCommand"，确认支持后应用户要求**迁移为插件用法示例播种**——本文件即
api.register_command 的最小真实用例：一条斜杠命令如何从插件挂进系统——
WS（WebUI 即时分发）+ CLI 两入口自动生效、/help 自动列出、插件禁用/卸载自动摘除。

命令语义（与内置时期完全一致）：
· cwd=workspace；复用引擎 real_tools._run_subprocess_streaming：流式输出、GBK/UTF-8
  自适应解码、超时（TOOL_TIMEOUT 默认 10s）自动转后台不杀进程 + 完成推送通知回执
· 跳过 run_shell 审批门：用户在输入框亲手敲的命令即显式授权，无需二次确认
· 长输出截断（前 6000 + 后 2000，中间省略标注——防屏爆）
· 引号/管道/重定向保真：commands.dispatch 对命令名 "cmd" 有原始行直通特例
  （同 /call，见 commands.py dispatch 注释）——插件命令同名同样命中，shlex 不拆引号
· 安全边界：/cmd 只经 ("user",…) 队列形态触达（CLI/WebUI 输入框手敲）；
  callback/调度/子Agent 注入一律走 ("background",…) → 只会成为消息文本给 Agent，
  不会被分发为命令——外部注入无法借道执行 shell（与 /api/tool_call 同信任级）

改完本文件：/reload plugins 热生效（免重启）。
"""
import shlex


def _cmd_cmd(ctx, args):
    """/cmd <shell 命令>——在 workspace 里直接执行。"""
    from real_tools import _run_subprocess_streaming, WORKSPACE
    if isinstance(args, str):
        line = args.strip()   # dispatch 对 "cmd" 传原始行（引号/管道/重定向原样保真）
    else:
        # list 兜底（不经 dispatch 直接调用时）：Windows cmd 只认双引号——
        # shlex.join 的 POSIX 单引号在 cmd.exe 是字面字符（echo 'x' 原样带引号输出，2026-10-15 实测）
        import os as _os, subprocess as _sp
        argv = [str(x) for x in (args or [])]
        line = (_sp.list2cmdline(argv) if _os.name == "nt" else shlex.join(argv)).strip()
    if not line:
        print("用法：/cmd <shell 命令>——在 workspace 直接执行：")
        print('  /cmd pip install -e .          # 本地装为 editable')
        print("  /cmd git status -sb            # 看仓库状态")
        print('  /cmd dir /b dist\\*.whl        # 找文件')
        print("  /cmd start .                   # 打开资源管理器（真想开窗口时）")
        return
    print(f"💻 $ {line}    (cwd={WORKSPACE})")
    try:
        out = _run_subprocess_streaming(line, "cmd", shell=True)
    except Exception as e:
        print(f"❌ 执行失败：{type(e).__name__}: {e}")
        return
    # 长输出截断（前 6000 + 后 2000，中间省略标注——防屏爆）
    if len(out) > 9000:
        out = out[:6000] + f"\n…（中间省略 {len(out) - 8000} 字符）…\n" + out[-2000:]
    print(out)


def setup(api):
    """引擎装配期调用：注册 /cmd 命令（WS+CLI 两入口；禁用/卸载插件即自动摘除）。"""
    api.register_command(
        "cmd", _cmd_cmd,
        "<shell 命令>  在 workspace 直接执行（免开终端：pip install -e . / git status / dir …）",
        "/cmd pip install -e .\n"
        "/cmd git status -sb\n"
        "/cmd dir /b *.whl\n"
        "  cwd=workspace；超过工具超时（默认 10s）自动转后台不中断，完成时推送通知；\n"
        "  引号/管道/重定向原样保真（dispatch 原始行直通）；只能从输入框手敲触发\n"
        "  （callback/后台注入不会当命令执行）——本命令由插件 cmd 提供（register_command 示例）")
