"""background_tools.py —— 后台服务 + 定时调度工具（绑定 Agent）。

让 Agent 能：
  - 后台启动它写的长服务（后端服务等）做前后端联调，查日志/停止；
  - 定时/到点推送消息（静态文本，或到点执行某工具拿结果）自动触发自己跑一轮推理。

工厂 make_background_tools(agent) 仿 plan_tools.py / memory_tools.py 惯例，闭包绑定
agent.services / agent.scheduler。docstring 第一行是模型判断"该不该调"的依据。
"""
from __future__ import annotations

from tools import Tool


def make_background_tools(agent) -> list:
    """生成绑定到指定 Agent 的后台服务/调度工具。"""
    svc = agent.services
    sch = agent.scheduler

    def start_service(name: str, command: str, cwd: str = "", on_exit_wake: str = "never") -> str:
        """后台启动一个长运行的服务（不阻塞）。用于把你写的后端跑起来做联调，
        如 `python app.py` / `npm run dev` / `python -m http.server 8000`。
        启动后其状态会自动出现在每轮系统提示里；用 service_logs 看输出、stop_service 停止。
        name 自取一个易记的名字，command 是 shell 命令。
        on_exit_wake（自行退出时是否唤醒你处理，默认 never=仅登记、下次交互时并入）：
        crash=异常退出(rc≠0)唤醒一轮处理、5分钟内同名连续崩溃自动退避为登记（防套娃）；
        always=任何退出都唤醒（含正常退出，如单次任务跑完即报）。常驻关键服务建议 crash。"""
        return svc.start(name, command, cwd, on_exit_wake=on_exit_wake)

    def stop_service(name: str) -> str:
        """停止指定的后台服务（先 terminate，3 秒不退则 kill）。
        注意：服务【自行退出】时（非你主动 stop），系统也会以同名 stop_service 工具结果的形式，
        把退出码+尾部日志+启动参数回传唤醒你——那条记录是系统注入的、会标注"自行退出"。"""
        return svc.stop(name)

    def list_services() -> str:
        """列出所有后台进程：分两段——【后台服务】（start_service 创建：运行中 pid/已运行时长 或 已退出）
        +【后台任务】（run_python/run_shell 超时自动转后台的一次性任务：bg_id/工具名/状态/已跑时长）。"""
        base = svc.list()
        try:
            from real_tools import _bg_tasks
        except Exception:
            _bg_tasks = {}
        if not _bg_tasks:
            return base
        import time as _t
        rows = []
        for bid, t in _bg_tasks.items():
            state = "运行中" if not t.get("finished") else f"已结束 rc={t.get('returncode')}"
            rows.append(f"  {bid} [{t.get('name')}] {state}, 已跑 {int(_t.time()-t.get('started_at',0))}s")
        head = "" if base.endswith("\n") else "\n"
        return (f"{base}{head}后台任务 (run_python/run_shell 超时转后台, check_bg_task 查详情):\n"
                + "\n".join(rows))

    def check_bg_task(task_id: str = "") -> str:
        """查询 run_python/run_shell 超时自动转后台的一次性任务。不传 task_id = 列出全部
        （bg_id/工具名/运行状态/已跑时长）；传 task_id = 该任务状态 + 尾部输出（≤2000 字）。
        任务完成时系统会自动推送通知，本工具用于中途查进度、补看结果或 bg_id 丢失后枚举找回。"""
        import time as _t
        try:
            from real_tools import _bg_tasks
        except Exception:
            _bg_tasks = {}
        if not _bg_tasks:
            return "(无后台任务)"
        def _row(bid, t):
            state = "运行中" if not t.get("finished") else f"已结束 rc={t.get('returncode')}"
            return f"  {bid} [{t.get('name')}] {state}, 已跑 {int(_t.time()-t.get('started_at',0))}s"
        if not task_id:
            return f"后台任务 共 {len(_bg_tasks)} 个:\n" + "\n".join(_row(b, t) for b, t in _bg_tasks.items())
        t = _bg_tasks.get(task_id)
        if not t:
            return f"[错误] 后台任务 '{task_id}' 不存在。当前登记: {', '.join(_bg_tasks)}"
        state = "运行中" if not t.get("finished") else f"已结束 rc={t.get('returncode')}"
        out = "".join(t.get("output", []))
        return (f"[后台任务 {task_id}] [{t.get('name')}] {state}, "
                f"已跑 {int(_t.time()-t.get('started_at',0))}s, 累计 {len(t.get('output', []))} 行输出\n"
                f"尾部输出:\n{out[-2000:]}")

    def service_logs(name: str, lines: int = 50) -> str:
        """查看某个后台服务最近 N 行输出日志。name 是 start_service 时起的名字。"""
        return svc.logs(name, lines)

    def add_schedule(name: str, every_seconds: float = 0, at: str = "",
                     message: str = "", tool: str = "", tool_args: dict = None,
                     repeat: bool = None) -> str:
        """添加定时/到点任务，到时自动推送一条消息触发 Agent 跑一轮。
        触发方式二选一：every_seconds>0 = 每 N 秒（repeat 控制是否循环，默认循环）；
        at = 完整 ISO（如 '2026-07-20T17:30:00'，单次）或短格式 'HH:MM'（如 '17:30'，每日闹钟，
        repeat=False 则只响下一个该时刻一次；ISO + repeat=True 也可每日循环）。
        推送内容二选一：message = 静态文本；tool(+tool_args) = 到点执行该工具（如 web_search），结果作为消息（动态消息）。
        例：每 60 秒提醒 → add_schedule('tick', every_seconds=60, message='该检查进度了')；
        每日 9 点闹钟 → add_schedule('morning', at='09:00', message='早会时间')；
        到点搜索 → add_schedule('news', at='2026-07-20T18:00:00', tool='web_search', tool_args={'query':'AI最新进展'})。"""
        tool_args = tool_args or {}
        if every_seconds > 0 and at:
            return "[只能选一种触发] every_seconds 与 at 不要同时给"
        action = {"tool": tool, "args": tool_args} if tool else None
        if not message and not action:
            return "[需提供 message 或 tool 作为推送内容]"
        if every_seconds > 0:
            return sch.add_interval(name, every_seconds, message=message, action=action,
                                    repeat=(True if repeat is None else bool(repeat)))
        if at:
            return sch.add_at(name, at, message=message, action=action, repeat=repeat)
        return "[需提供 every_seconds 或 at 之一作为触发方式]"

    def cancel_schedule(name: str) -> str:
        """取消指定名字(或 id)的定时任务。"""
        return sch.cancel(name)

    def list_schedules() -> str:
        """列出所有定时任务（触发方式/剩余时间/推送内容）。"""
        return sch.list()

    def send_to_service(name: str, message: str) -> str:
        """向后台服务的 stdin 发送一行文本（服务须为 REPL 型——如另一个 agt 实例/交互式 CLI/REPL）。
        用于驱动 start_service 启动的 agt：发任务 prompt、发 /restart 等命令。非 REPL 服务会忽略 stdin，无副作用。
        name 是 start_service 时起的名字；message 是要发送的一行文本。"""
        return svc.send(name, message)

    return [Tool(start_service), Tool(stop_service), Tool(list_services),
            Tool(service_logs), Tool(send_to_service), Tool(check_bg_task),
            Tool(add_schedule), Tool(cancel_schedule), Tool(list_schedules)]
