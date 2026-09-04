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
    agent_id: str            # 唯一标识："_main_" / "coder_3" / 自定义
    name: str                # 声明名："main" / "coder" / "explorer"
    role: str                # "main" | "subagent"
    model: str               # 模型名
    task: str = ""           # 当前任务摘要
    agent: object = None     # Agent 实例引用（可直接访问 .session / .llm）
    status: str = "running"  # "running" | "idle" | "done" | "failed"
    caller_id: str = ""      # 谁派的任务（_main_ / coder_2 / user）→ 完成后按此路由 answer
    recap: str = ""          # 最近一轮的 recap（队友可见，不进入自己的上下文）
    registered_at: float = field(default_factory=time.time)


class AgentRegistry:
    """全局共享 Agent 注册表。所有 Agent（主+子）的元信息 + 通信端点。"""

    def __init__(self):
        self._agents: dict[str, AgentEntry] = {}
        self._lock = threading.RLock()
        self._on_change: list[tuple[str, object]] = []   # (订阅者 agent_id, 回调)：注册表变化时触发

    def add_on_change(self, subscriber_id: str, cb):
        """订阅注册表变化（如刷新自己工具 schema 的 target_id enum）。
        同 subscriber_id 覆盖（重连/重新装配不叠列表）；unregister 时自动清除。"""
        with self._lock:
            self._on_change = [(i, c) for (i, c) in self._on_change if i != subscriber_id]
            self._on_change.append((subscriber_id, cb))

    def _fire_change(self):
        """锁外触发全部订阅回调（回调异常不炸注册路径）。"""
        with self._lock:
            cbs = list(self._on_change)
        for _, cb in cbs:
            try:
                cb()
            except Exception:
                pass

    def register(self, agent_id: str, name: str, role: str, model: str,
                 agent: object, task: str = "", status: str = "running",
                 caller_id: str = "", recap: str = "") -> AgentEntry:
        """注册一个 Agent。同 agent_id 覆盖（重新派活时复用 id）。"""
        with self._lock:
            entry = AgentEntry(
                agent_id=agent_id, name=name, role=role, model=model,
                agent=agent, task=task, status=status, caller_id=caller_id,
                recap=recap,
            )
            self._agents[agent_id] = entry
        self._fire_change()   # 子 Agent 创建/读档恢复后刷新通信工具的 target_id enum
        return entry

    def unregister(self, agent_id: str):
        """注销一个 Agent（主 Agent 退出时清理用）。"""
        with self._lock:
            self._agents.pop(agent_id, None)
            self._on_change = [(i, c) for (i, c) in self._on_change if i != agent_id]
        self._fire_change()

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

    def update_recap(self, agent_id: str, recap: str):
        """更新某 Agent 的 recap（最近一轮一句话总结）。"""
        with self._lock:
            entry = self._agents.get(agent_id)
            if entry:
                entry.recap = recap

    def format_team(self, exclude_id: str = "") -> str:
        """格式化团队清单，供 SYSTEM 注入。exclude_id 的 Agent 不列自己。
        子 Agent 由 Agent._restore_subagents() 在 set_session 时扫描 session_dir/agents/ 注册到 registry，
        所以这里只需读 registry——不再扫磁盘。"""
        with self._lock:
            entries = [e for e in sorted(self._agents.values(),
                                         key=lambda x: x.registered_at) if e.agent_id != exclude_id]
        if not entries:
            return ""
        lines = ["【当前 Agent 团队】"]
        for e in entries:
            icon = {"running": "🏃", "idle": "💤", "done": "✅", "failed": "❌"}.get(e.status, "?")
            # 优先显示 recap（最近一轮总结），无 recap 则显示 task
            info = (e.recap or e.task or "(无任务)")[:60]
            caller = f" → {e.caller_id}" if (e.role == "subagent" and e.caller_id) else ""
            lines.append(f"- {e.name} [{e.agent_id}] ({e.model}) {icon} — {info}{caller}")
        lines.append("  ↳ 可用 agent_ask / agent_notify / agent_query_events / agent_query_tool_detail 与队友通信")
        return "\n".join(lines)
