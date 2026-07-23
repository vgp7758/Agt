"""plan_tools.py —— 跨 session 的 repo 级计划/清单工具（类 Claude Code 的 TodoWrite，但持久化为文件）。

计划存到 ~/.agt/repos/<hash>/plans/<plan_id>.json，每个计划一个文件、带稳定 id，
可跨 session 共享：session 的 extra_state 只记一个 plan_id（当前活动计划引用），
另一个 session 用 join_plan(id) 即可加入同一个计划继续推进。

加入计划后，计划的完整信息（标题 / 制定时的设计 / TodoList 及各步状态）每轮被动注入 SYSTEM
（见 agent._plan_system_block / session.messages_for_llm 的 _plan_provider 槽），让 Agent 始终
清楚在干哪一步；exit_plan() 退出后停止注入（文件仍保留）。

内存模型：agent.active_plan（完整 dict，单一事实源）+ agent.plan（steps 的镜像，兼容旧读者）
+ agent.active_plan_id。所有变更工具改 active_plan → _flush（同步镜像 + 原子落盘）→ emit。
"""
from __future__ import annotations

import json
import os
import uuid
from datetime import datetime
from pathlib import Path

from session import repo_plans_dir
from tools import Tool

_PLAN_ICON = {"pending": "☐", "in_progress": "▶", "completed": "✅"}
_PLAN_LABEL = {"pending": "待办", "in_progress": "进行中", "completed": "已完成"}
_VALID_STATUS = ("pending", "in_progress", "completed")


# ========== 文件 I/O ==========

def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _gen_plan_id() -> str:
    """本 repo 内足够唯一的计划 id：p_ + 8 位 hex。文件名即 <plan_id>.json。"""
    return "p_" + uuid.uuid4().hex[:8]


def _plan_path(workspace, plan_id: str) -> Path:
    return repo_plans_dir(workspace) / f"{plan_id}.json"


def _load_plan(workspace, plan_id: str):
    """按 id 读单个计划；不存在 / 损坏返回 None。"""
    p = _plan_path(workspace, plan_id)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def _save_plan(workspace, plan: dict) -> None:
    """原子落盘：写 .tmp 再 os.replace；刷新 updated_at。"""
    plan["updated_at"] = _now_iso()
    p = _plan_path(workspace, plan["id"])
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, p)


def _list_plans(workspace) -> list:
    """列出本 repo 全部计划，按 updated_at 倒序。"""
    out = []
    for f in repo_plans_dir(workspace).glob("p_*.json"):
        try:
            out.append(json.loads(f.read_text(encoding="utf-8")))
        except Exception:
            continue
    out.sort(key=lambda x: x.get("updated_at", ""), reverse=True)
    return out


# ========== 内存 ↔ 落盘 同步 ==========

def _sync_legacy(agent):
    """把 active_plan['steps'] 镜像到 agent.plan（供 wiki/commands/web 等旧读者）。"""
    if getattr(agent, "active_plan", None):
        agent.plan = [dict(s) for s in agent.active_plan.get("steps", [])]
    else:
        agent.plan = []


def _set_active(agent, plan: dict):
    """把一个计划 dict 设为当前活动计划（同步 id / 镜像）。"""
    agent.active_plan = plan
    agent.active_plan_id = plan.get("id")
    _sync_legacy(agent)


def _clear_active(agent):
    """清空活动计划（/reset、新 session、exit_plan 调用）。文件不动。"""
    agent.active_plan = None
    agent.active_plan_id = None
    agent.plan = []


def _flush(agent):
    """把内存里的活动计划同步镜像并原子落盘。"""
    if getattr(agent, "active_plan", None):
        _sync_legacy(agent)  # active_plan['steps'] 可能被工具就地改过 → 刷到 agent.plan
        _save_plan(agent.session.workspace, agent.active_plan)


def restore_active_plan(agent, state: dict) -> None:
    """从 session 存档恢复活动计划：优先按 plan_id 从文件读回；
    旧存档只有 plan 列表（无 plan_id）则迁移成计划文件。供 agent.restore_runtime_state 调用。"""
    if not state:
        _clear_active(agent)
        return
    plan_id = state.get("plan_id")
    if plan_id:
        plan = _load_plan(agent.session.workspace, plan_id)
        if plan:
            _set_active(agent, plan)
        else:
            _clear_active(agent)  # 文件已丢失：视为无活动计划
        return
    legacy = state.get("plan")  # 旧格式：内联的 steps 列表
    if legacy:
        plan = {
            "id": _gen_plan_id(),
            "title": "迁移的计划",
            "design": "",
            "steps": [dict(s) for s in legacy],
            "created_at": _now_iso(),
            "updated_at": _now_iso(),
            "created_session": getattr(agent.session, "name", None),
        }
        _save_plan(agent.session.workspace, plan)
        _set_active(agent, plan)
    else:
        _clear_active(agent)


def clear_active_plan(agent) -> None:
    """清空活动计划（/reset、新 session 调用）。文件保留，可再次 join。"""
    _clear_active(agent)


# ========== 渲染 ==========

def _plan_text(agent) -> str:
    """计划 steps 的纯文本视图（工具返回值用）。"""
    if not agent.plan:
        return "(空计划)"
    return "\n".join(f"{_PLAN_ICON.get(s.get('status'), '?')} {i + 1}. {s.get('description', '')}"
                     for i, s in enumerate(agent.plan))


def _format_plan_block(agent) -> str:
    """活动计划的 SYSTEM 注入块。无活动计划 / 空步骤返回 ''（session 不注入）。"""
    p = getattr(agent, "active_plan", None)
    if not p or not p.get("steps"):
        return ""
    steps = p["steps"]
    total = len(steps)
    done = sum(1 for s in steps if s.get("status") == "completed")
    title = p.get("title", "")
    design = (p.get("design") or "").strip()
    lines = [f"【当前计划】{p.get('id', '')}" + (f" · {title}" if title else "")]
    lines.append("设计：" + (design or "（无）"))
    lines.append(f"进度（共 {total} 步，已完成 {done}）：")
    for i, s in enumerate(steps):
        st = s.get("status")
        lines.append(f"  {_PLAN_ICON.get(st, '?')} {i + 1}. {s.get('description', '')} ({_PLAN_LABEL.get(st, '')})")
    lines.append("推进时用 update_plan 更新状态、add_step 追加步骤、edit_plan 改标题/设计、exit_plan 退出。")
    return "\n".join(lines)


def _emit_plan(agent):
    if getattr(agent, "on_event", None):
        try:
            agent.on_event({"type": "plan", "plan": [dict(s) for s in agent.plan],
                            "plan_id": agent.active_plan_id,
                            "plan_title": (agent.active_plan or {}).get("title", "")})
        except Exception:
            pass


# ========== 工具 ==========

def make_plan_tools(agent) -> list:
    """生成绑定到指定 Agent 的计划工具（创建/导航/查看/修改 共 7 个）。"""

    def create_plan(title: str, steps: list, design: str = "") -> str:
        """新建一个计划（新 id + 新文件，旧计划保留可被 list/join）并设为当前活动计划。
        title: 计划名称；steps: 步骤描述字符串数组；design: 可选，制定时的设计/目标/背景。
        建成后每轮自动注入 SYSTEM；用 update_plan 标记进度，add_step/edit_plan 修改，exit_plan 退出。"""
        if not isinstance(steps, list) or not steps:
            return "[错误] steps 必须是非空字符串数组"
        plan = {
            "id": _gen_plan_id(),
            "title": (title or "未命名计划").strip() or "未命名计划",
            "design": design or "",
            "steps": [{"description": str(s), "status": "pending"} for s in steps],
            "created_at": _now_iso(),
            "updated_at": _now_iso(),
            "created_session": getattr(agent.session, "name", None),
        }
        _save_plan(agent.session.workspace, plan)
        _set_active(agent, plan)
        _emit_plan(agent)
        return f"已创建计划 {plan['id']}（{len(plan['steps'])} 步）：\n" + _plan_text(agent)

    def join_plan(plan_id: str) -> str:
        """加入一个已存在的计划（按 plan_id 从本仓库加载），设为当前活动计划，每轮注入 SYSTEM。
        plan_id 可先用 list_plans() 查到；加入后即可继续推进同一计划（跨 session 共享）。"""
        pid = (plan_id or "").strip()
        if not pid:
            return "[错误] plan_id 不能为空；先用 list_plans() 查看可用计划"
        plan = _load_plan(agent.session.workspace, pid)
        if not plan:
            return f"[错误] 找不到计划 {pid}；用 list_plans() 查看可用计划"
        _set_active(agent, plan)
        _emit_plan(agent)
        return f"已加入计划 {pid}（当前 {len(plan.get('steps', []))} 步）：\n" + _plan_text(agent)

    def exit_plan() -> str:
        """退出当前活动计划：停止每轮 SYSTEM 注入、清空计划面板。计划文件仍保留，可再次 join。"""
        if not agent.active_plan_id:
            return "当前没有活动计划"
        pid = agent.active_plan_id
        _clear_active(agent)
        _emit_plan(agent)
        return f"已退出计划 {pid}（文件保留，可用 join_plan 重新加入）"

    def list_plans() -> str:
        """列出本仓库的全部计划：plan_id / 标题 / 完成进度，并标注当前活动计划。"""
        plans = _list_plans(agent.session.workspace)
        if not plans:
            return "本仓库还没有计划。用 create_plan 新建一个。"
        active = agent.active_plan_id
        rows = []
        for p in plans:
            steps = p.get("steps", [])
            done = sum(1 for s in steps if s.get("status") == "completed")
            mark = "   （当前活动）" if p.get("id") == active else ""
            rows.append(f"{p.get('id', '?')}   {p.get('title', '未命名')}   progress:{done}/{len(steps)}{mark}")
        return "\n".join(rows) + "\n\n用 join_plan(plan_id) 加入某个计划。"

    def update_plan(step: int, status: str = "", description: str = "") -> str:
        """更新当前计划某一步：status∈pending/in_progress/completed 标记进度；description 非空则改写该步描述。至少给一个。"""
        if not getattr(agent, "active_plan", None):
            return "[错误] 还没有活动计划，先用 create_plan 新建或 join_plan 加入"
        steps = agent.active_plan["steps"]
        if not (1 <= step <= len(steps)):
            return f"[错误] step 越界（共 {len(steps)} 步）"
        if not status and not description:
            return "[错误] 至少提供 status 或 description 之一"
        if status and status not in _VALID_STATUS:
            return "[错误] status 必须是 pending / in_progress / completed"
        if status:
            steps[step - 1]["status"] = status
        if description:
            steps[step - 1]["description"] = str(description)
        _flush(agent)
        _emit_plan(agent)
        return _plan_text(agent)

    def add_step(description: str) -> str:
        """给当前活动计划追加一步（状态 pending）。"""
        if not getattr(agent, "active_plan", None):
            return "[错误] 还没有活动计划，先用 create_plan 新建或 join_plan 加入"
        desc = (description or "").strip()
        if not desc:
            return "[错误] description 不能为空"
        agent.active_plan["steps"].append({"description": desc, "status": "pending"})
        _flush(agent)
        _emit_plan(agent)
        return f"已追加第 {len(agent.active_plan['steps'])} 步：\n" + _plan_text(agent)

    def edit_plan(title: str = "", design: str = "") -> str:
        """改写当前活动计划的标题（title）和/或设计（design）；至少给一个。"""
        if not getattr(agent, "active_plan", None):
            return "[错误] 还没有活动计划，先用 create_plan 新建或 join_plan 加入"
        if not title and not design:
            return "[错误] 至少提供 title 或 design 之一"
        if title:
            t = str(title).strip()
            if t:
                agent.active_plan["title"] = t
        if design:
            agent.active_plan["design"] = str(design)
        _flush(agent)
        _emit_plan(agent)
        return (f"计划 {agent.active_plan_id} 已更新："
                f"标题={agent.active_plan.get('title', '')}；设计长度={len(agent.active_plan.get('design', ''))}")

    return [Tool(create_plan), Tool(update_plan), Tool(add_step), Tool(edit_plan),
            Tool(join_plan), Tool(list_plans), Tool(exit_plan)]
