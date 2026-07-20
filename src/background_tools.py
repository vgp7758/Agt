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

    def start_service(name: str, command: str, cwd: str = "") -> str:
        """后台启动一个长运行的服务（不阻塞）。用于把你写的后端跑起来做联调，
        如 `python app.py` / `npm run dev` / `python -m http.server 8000`。
        启动后其状态会自动出现在每轮系统提示里；用 service_logs 看输出、stop_service 停止。
        name 自取一个易记的名字，command 是 shell 命令。"""
        return svc.start(name, command, cwd)

    def stop_service(name: str) -> str:
        """停止指定的后台服务（先 terminate，3 秒不退则 kill）。"""
        return svc.stop(name)

    def list_services() -> str:
        """列出所有后台服务及其运行状态（运行中 pid/已运行时长 或 已退出）。"""
        return svc.list()

    def service_logs(name: str, lines: int = 50) -> str:
        """查看某个后台服务最近 N 行输出日志。name 是 start_service 时起的名字。"""
        return svc.logs(name, lines)

    def add_schedule(name: str, every_seconds: float = 0, at: str = "",
                     message: str = "", tool: str = "", tool_args: dict = None,
                     repeat: bool = True) -> str:
        """添加定时/到点任务，到时自动推送一条消息触发 Agent 跑一轮。
        触发方式二选一：every_seconds>0 = 每 N 秒（repeat 控制是否循环）；at = 到某时刻一次性（ISO 如 '2026-07-20T17:30:00'）。
        推送内容二选一：message = 静态文本；tool(+tool_args) = 到点执行该工具（如 web_search），结果作为消息（动态消息）。
        例：每 60 秒提醒 → add_schedule('tick', every_seconds=60, message='该检查进度了')；
        到点搜索 → add_schedule('news', at='2026-07-20T18:00:00', tool='web_search', tool_args={'query':'AI最新进展'})。"""
        tool_args = tool_args or {}
        if every_seconds > 0 and at:
            return "[只能选一种触发] every_seconds 与 at 不要同时给"
        action = {"tool": tool, "args": tool_args} if tool else None
        if not message and not action:
            return "[需提供 message 或 tool 作为推送内容]"
        if every_seconds > 0:
            return sch.add_interval(name, every_seconds, message=message, action=action, repeat=repeat)
        if at:
            return sch.add_at(name, at, message=message, action=action)
        return "[需提供 every_seconds 或 at 之一作为触发方式]"

    def cancel_schedule(name: str) -> str:
        """取消指定名字(或 id)的定时任务。"""
        return sch.cancel(name)

    def list_schedules() -> str:
        """列出所有定时任务（触发方式/剩余时间/推送内容）。"""
        return sch.list()

    return [Tool(start_service), Tool(stop_service), Tool(list_services),
            Tool(service_logs), Tool(add_schedule), Tool(cancel_schedule), Tool(list_schedules)]
