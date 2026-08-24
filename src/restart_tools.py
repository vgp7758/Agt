"""restart_tools.py —— restart_agent 工具：Agent 自服务重启（改完代码后使其生效）。

场景：Agent 用 edit/run_python 改了 agt 自身源码，但当前进程跑的还是旧代码——
调 restart_agent 后：看门狗拉起 → 本进程优雅退出 → 新进程载新代码 → 自动恢复
session / Web 端口 / 可选推送一条消息。配合 /restart 命令共用 restart_watchdog。
"""
from __future__ import annotations

import os

from tools import Tool


def make_restart_tools(agent) -> list:
    """生成绑定到 agent 的重启工具。agent._work_q 由 chat.main/web_main 注入。"""

    def restart_agent(message: str = "") -> str:
        """重启 agt 进程使代码修改生效（看门狗模式，自动恢复）。
        适用：你刚修改了 agt 自身源码（src/ 下），当前进程仍跑旧代码——重启后新代码生效。
        行为：启动看门狗 → 本进程优雅退出（当前回答会被用户看到）→ 看门狗拉起新进程
        → 自动恢复当前 session 与 Web 服务端口 → （可选）把 message 作为重启后第一条消息发送。
        message: 重启完成后自动发送的消息（如"代码已更新，继续 xxx"，留空=仅恢复会话）。"""
        from real_tools import WORKSPACE
        from restart_watchdog import spawn_watchdog
        wq = getattr(agent, "_work_q", None)
        if wq is None:
            return "[错误] work_q 未注入（agent._work_q），无法触发退出；请让用户手动 /restart"
        # web 判定：server 模块级状态（CLI 的 /web 起的服务也能恢复）
        port = 0
        try:
            import server as _srv
            if getattr(_srv, "_server", None) is not None:
                port = _srv._port or 0
        except Exception:
            pass
        ok, info = spawn_watchdog(
            parent_pid=os.getpid(),
            mode="web" if port else "cli",
            session=getattr(agent.session, "name", "") or "",
            port=port, message=(message or "").strip(),
            cwd=str(WORKSPACE))
        # 丢弃排队未处理的消息（哨兵排队尾等排空会把退出拖过看门狗等待窗口），
        # 只等当前轮（本工具所在轮的回答）完成。与 _cmd_restart 同款逻辑。
        import queue as _q
        dropped = 0
        while True:
            try:
                item = wq.get_nowait()
            except _q.Empty:
                break
            if item is not None:
                dropped += 1
        wq.put(None)   # 优雅退出哨兵：worker 处理完当前项（本工具所在轮的回答）后退出
        return f"⏳ {info}。本回答后进程将退出，新进程自动接管。" \
               + (f"（已丢弃 {dropped} 条排队消息）" if dropped else "")

    return [Tool(restart_agent)]
