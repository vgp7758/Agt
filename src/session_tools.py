"""session_tools.py —— 会话管理工具（绑定到 Agent 的 session）。

工厂 make_session_tools(agent) 仿 memory_tools / plan_tools 惯例，返回绑定到
agent.session 的工具列表：
  - rename_session：Agent 可调，发现自动命名不准时改成更贴切的名字
  - get_session_history：hidden，供工作流节点编排检索/重排/投影用，返回全量未截断结构化历史
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

    def get_session_history(max_turns: int = None) -> list:
        """返回当前 session 的全量结构化历史（turns + 工具调用，result 未截断）。
        max_turns 非空时只返回最近 N 轮（None=全部）。
        仅供工作流节点编排检索/重排/投影用——不投影给 Agent LLM（hidden=True）。"""
        return agent.session.to_history_full(max_turns)

    return [
        Tool(rename_session),                       # Agent LLM 可调
        Tool(get_session_history, hidden=True),      # 只工作流 plugin 节点能调
    ]
