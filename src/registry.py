"""registry.py —— Agent 注册表：多 Agent 协作的寻址基础。

所有活跃 Agent（主+子）注册在此，通过 agent_id 寻址。
三种通信方式（ask/notify/query）都依赖此注册表查找目标 Agent。
线程安全（RLock）——多个子 Agent 可能在不同线程同时注册/查询。
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class AgentEntry:
    """注册表中的一条 Agent 记录。"""
    agent_id: str            # 唯一标识："main" / "coder_3" / 自定义
    name: str                # 声明名："main" / "coder" / "explorer"
    role: str                # "main" | "subagent"
    model: str               # 模型名
    task: str = ""           # 当前任务摘要
    agent: object = None     # Agent 实例引用（可直接访问 .session / .llm）
    status: str = "running"  # "running" | "idle" | "done" | "failed"
    registered_at: float = field(default_factory=time.time)


class AgentRegistry:
    """全局共享 Agent 注册表。所有 Agent（主+子）的元信息 + 通信端点。"""

    def __init__(self):
        self._agents: dict[str, AgentEntry] = {}
        self._lock = threading.RLock()

    def register(self, agent_id: str, name: str, role: str, model: str,
                 agent: object, task: str = "", status: str = "running") -> AgentEntry:
        """注册一个 Agent。同 agent_id 覆盖（重新派活时复用 id）。"""
        with self._lock:
            entry = AgentEntry(
                agent_id=agent_id, name=name, role=role, model=model,
                agent=agent, task=task, status=status,
            )
            self._agents[agent_id] = entry
            return entry

    def unregister(self, agent_id: str):
        """注销一个 Agent（主 Agent 退出时清理用）。"""
        with self._lock:
            self._agents.pop(agent_id, None)

    def lookup(self, agent_id: str) -> Optional[AgentEntry]:
        """按 agent_id 查找。找不到返回 None。"""
        with self._lock:
            return self._agents.get(agent_id)

    def list_all(self) -> list[AgentEntry]:
        """返回所有已注册的 Agent（按注册时间排序）。"""
        with self._lock:
            return sorted(self._agents.values(), key=lambda e: e.registered_at)

    def list_active(self) -> list[AgentEntry]:
        """返回所有状态为 running/idle 的 Agent。"""
        with self._lock:
            return [e for e in self._agents.values() if e.status in ("running", "idle")]

    def update_status(self, agent_id: str, status: str):
        """更新某 Agent 的状态。"""
        with self._lock:
            entry = self._agents.get(agent_id)
            if entry:
                entry.status = status

    def update_task(self, agent_id: str, task: str):
        """更新某 Agent 的当前任务摘要。"""
        with self._lock:
            entry = self._agents.get(agent_id)
            if entry:
                entry.task = task

    def format_team(self, exclude_id: str = "") -> str:
        """格式化团队清单，供 SYSTEM 注入。exclude_id 的 Agent 不列自己。"""
        with self._lock:
            entries = [e for e in sorted(self._agents.values(),
                                         key=lambda x: x.registered_at) if e.agent_id != exclude_id]
        if not entries:
            return ""
        lines = ["【当前 Agent 团队】"]
        for e in entries:
            icon = {"running": "🏃", "idle": "💤", "done": "✅", "failed": "❌"}.get(e.status, "?")
            task_short = (e.task or "(无任务)")[:60]
            lines.append(f"- {e.name} [{e.agent_id}] ({e.model}) {icon} — {task_short}")
        lines.append("  ↳ 可用 agent_ask / agent_notify / agent_query_events / agent_query_tool_detail 与队友通信")
        return "\n".join(lines)
