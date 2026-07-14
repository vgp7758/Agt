"""plan_tools.py —— 计划/清单工具（类 Claude Code 的 TodoWrite）。

让 Agent 在动手前把复杂任务拆成步骤清单，每完成一步标记进度；
UI 实时显示清单推进。计划存在 agent.plan 上，create/update 时 emit 'plan' 事件。
"""
from __future__ import annotations

from tools import Tool

_PLAN_ICON = {"pending": "☐", "in_progress": "▶", "completed": "✅"}


def _plan_text(agent) -> str:
    if not agent.plan:
        return "(空计划)"
    return "\n".join(f"{_PLAN_ICON.get(s['status'], '?')} {i + 1}. {s['description']}"
                     for i, s in enumerate(agent.plan))


def _emit_plan(agent):
    if getattr(agent, "on_event", None):
        try:
            agent.on_event({"type": "plan", "plan": [dict(s) for s in agent.plan]})
        except Exception:
            pass


def make_plan_tools(agent) -> list:
    """生成绑定到指定 Agent 的计划工具（create_plan / update_plan）。"""

    def create_plan(steps: list) -> str:
        """制定计划清单（动手前把任务拆成若干步骤）。steps: 步骤描述字符串数组；会覆盖已有计划。
        之后用 update_plan(step, status) 逐个标记进度（status ∈ pending/in_progress/completed）。"""
        if not isinstance(steps, list):
            return "[错误] steps 必须是字符串数组"
        agent.plan = [{"description": str(s), "status": "pending"} for s in steps]
        _emit_plan(agent)
        return f"计划已制定（{len(agent.plan)} 步）：\n" + _plan_text(agent)

    def update_plan(step: int, status: str) -> str:
        """更新某一步的状态。step: 从 1 开始的序号；status: pending / in_progress / completed。"""
        if not agent.plan:
            return "[错误] 还没有计划，先用 create_plan 制定"
        if not (1 <= step <= len(agent.plan)):
            return f"[错误] step 越界（共 {len(agent.plan)} 步）"
        if status not in ("pending", "in_progress", "completed"):
            return "[错误] status 必须是 pending / in_progress / completed"
        agent.plan[step - 1]["status"] = status
        _emit_plan(agent)
        return _plan_text(agent)

    return [Tool(create_plan), Tool(update_plan)]
