"""session_tools.py —— 会话管理工具（绑定到 Agent 的 session）。

工厂 make_session_tools(agent) 仿 memory_tools / plan_tools 惯例，返回绑定到
agent.session 的工具列表。当前含 rename_session：让 Agent 在发现首轮自动命名
不准、或对话主题已转变后，自主把当前会话改成更贴切的名字。

配合 SYSTEM 每步注入的「当前会话：<name>」让 Agent 始终知道自己叫什么，
从而判断是否需要改名。
"""
from __future__ import annotations

from tools import Tool


def make_session_tools(agent) -> list:
    """生成绑定到指定 Agent 的会话管理工具。"""

    def rename_session(new_name: str) -> str:
        """重命名当前会话。适用：首轮自动命名的名字不准确，或对话主题已转变，想换个更贴切的名字。
        new_name 可含空格（如「Unity 转 AI 求职」）。改名即时同步到 WebUI 标题与会话列表。
        无需主动用——只在当前会话名明显不合适时调用；改名不影响历史与上下文，可继续对话。"""
        new_name = (new_name or "").strip()
        if not new_name:
            return "[错误] new_name 不能为空"
        try:
            old = agent.session.name or "(未命名)"
            agent.session.rename(new_name)
        except ValueError as e:
            return f"[错误] {e}"
        # 通知 WebUI 同步标题（CLI 无 on_event 时跳过）
        if getattr(agent, "on_event", None):
            try:
                agent.on_event({"type": "session_renamed", "name": agent.session.name})
            except Exception:
                pass
        return f"✅ 会话已重命名：{old} → {agent.session.name}"

    return [Tool(rename_session)]
