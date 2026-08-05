"""background.py —— 后台长服务 + 定时/到点调度器，推送消息触发 Agent 推理。

两类 producer 都把消息推进 `agent.inbox`（带锁 deque），由 chat/web 的串行消费者 +
`Agent.run()` 内部循环消费触发 `agent.run()`。**任何时候只有一个 run 在跑**（agent.run
非线程安全，多 run 并发会踩 session._current 等共享状态）。

- `ServiceManager`：Popen 长进程（Agent 写的后端服务等），后台读日志线程 + 滚动 deque 缓冲，
  start/stop/list/logs/status_lines。进程【自行退出】时（stdout 关闭）由读线程抓 rc，经 on_exit
  回调（Agent 注入）把退出事件推 inbox 唤醒 Agent；手动 stop_service 不重复通知。
- `Scheduler`：interval（每 N 秒）/ at（到某时刻），静态 message 或动态（到点执行某工具拿结果）；
  后台线程到点 produce → `agent.push_message`。持 agent 引用。

两类 producer 都把消息推进 `agent.inbox`（带锁 deque）。`status_lines()` 供 Agent 把
"当前有哪些服务在跑/已断"实时注入 system prompt。
"""
from __future__ import annotations

import collections
import os
import signal
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

_LOG_CAP = 1000  # 每个服务的滚动日志行数上限
_POLL = 0.5      # 调度器轮询间隔（秒）


class ServiceManager:
    """后台长进程管理：Popen 不等待，后台线程收日志，可查状态/停止。"""

    def __init__(self, on_exit=None):
        self._services: dict = {}  # name -> {proc, command, cwd, started_at, pid, logs, manual_stop}
        self._lock = threading.Lock()
        self._on_exit = on_exit   # 进程自行退出回调 on_exit(name, entry, rc)，由 Agent 注入（可 None）

    def start(self, name: str, command: str, cwd: str = "") -> str:
        with self._lock:
            if name in self._services:
                return f"[已存在同名服务] {name}，先 stop_service 再启动"
        popen_kwargs = dict(shell=True, cwd=cwd or None,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, bufsize=1, encoding="utf-8", errors="replace")
        # 绑独立进程组/会话：stop 时能整树杀，避免 shell=True 下 terminate 只杀 shell、
        # 漏掉 shell 启的实际命令（孙进程）变孤儿；父进程异常退出也便于外部按组清理。
        if sys.platform == "win32":
            popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            popen_kwargs["start_new_session"] = True
        try:
            proc = subprocess.Popen(command, **popen_kwargs)
        except Exception as e:
            return f"[启动失败] {type(e).__name__}: {e}"
        logs: collections.deque = collections.deque(maxlen=_LOG_CAP)
        entry = {"proc": proc, "command": command, "cwd": cwd,
                 "started_at": time.time(), "pid": proc.pid,
                 "logs": logs, "manual_stop": False}
        with self._lock:
            self._services[name] = entry

        def _reader():
            try:
                for line in proc.stdout:
                    logs.append(line.rstrip("\n"))
            except Exception:
                pass
            # stdout 关闭 ≈ 进程已退出。手动 stop_service 时 stop() 已先置 manual_stop=True，
            # 这里直接跳过（那次是 Agent 主动调的工具、它已知，不再被动通知，避免双重处理）。
            if entry.get("manual_stop"):
                return
            try:
                rc = proc.wait()
            except Exception:
                rc = proc.poll()
                if rc is None:
                    rc = -1
            with self._lock:
                if name not in self._services:   # 退出期间被移除 → 不通知
                    return
            if self._on_exit is not None:
                try:
                    self._on_exit(name, entry, rc)
                except Exception:
                    pass

        threading.Thread(target=_reader, daemon=True).start()
        return f"✅ 后台服务「{name}」已启动 (pid={proc.pid})：{command}"

    def status_lines(self) -> list:
        """供 system prompt 注入：每个服务一行 name(状态, pid, 已跑 Ns)。已退出标'需重启'。"""
        with self._lock:
            now = time.time()
            lines = []
            for name, e in self._services.items():
                rc = e["proc"].poll()
                if rc is None:
                    up = int(now - e["started_at"])
                    lines.append(f"  {name}(运行中, pid={e['proc'].pid}, 已跑 {up}s)")
                else:
                    lines.append(f"  {name}(已退出 rc={rc}, 需重启)")
            return lines

    def list(self) -> str:
        with self._lock:
            if not self._services:
                return "(无后台服务)"
            now = time.time()
            rows = []
            for name, e in self._services.items():
                rc = e["proc"].poll()
                up = int(now - e["started_at"])
                st = f"运行中 pid={e['proc'].pid} 已跑{up}s" if rc is None else f"已退出 rc={rc}"
                rows.append(f"  {name:<16} {st}  | {e['command']}")
            return "\n".join(rows)

    def logs(self, name: str, lines: int = 50) -> str:
        with self._lock:
            e = self._services.get(name)
            if not e:
                return f"[无此服务] {name}"
            tail = list(e["logs"])[-max(1, int(lines)):]
        if not tail:
            return f"【{name}】暂无输出"
        return f"【{name} 最近 {len(tail)} 行】\n" + "\n".join(tail)

    def stop(self, name: str) -> str:
        with self._lock:
            e = self._services.get(name)
        if not e:
            return f"[无此服务] {name}"
        proc = e["proc"]
        if proc.poll() is not None:
            return f"「{name}」本就已退出"
        e["manual_stop"] = True   # 标记手动停：reader 跳过被动退出通知（Agent 这次是自己 stop 的）
        self._kill_tree(proc)   # 杀整棵树（shell + 其孙进程），而非只 terminate shell
        try:
            proc.wait(timeout=3)
        except Exception:
            pass   # _kill_tree 已强制杀，wait 只是确认
        return f"🛑 已停止「{name}」"

    @staticmethod
    def _kill_tree(proc) -> None:
        """跨平台杀整棵进程树。start 时已绑新进程组/会话，故按组杀能覆盖孙进程。"""
        pid = proc.pid
        if sys.platform == "win32":
            try:
                subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                               capture_output=True, timeout=5)
            except Exception:
                pass
        else:
            try:
                os.killpg(os.getpgid(pid), signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                return
            try:
                proc.wait(timeout=0.5)
            except Exception:
                try:
                    os.killpg(os.getpgid(pid), signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass

    def stop_all(self):
        """退出时清理所有服务，防孤儿进程。"""
        with self._lock:
            names = list(self._services.keys())
        for n in names:
            try:
                self.stop(n)
            except Exception:
                pass


@dataclass
class Schedule:
    id: str
    name: str
    kind: str                       # "interval" | "at"
    spec: float                     # interval=秒；at=触发时间戳
    message: str = ""               # 静态推送文本（与 action 二选一）
    action: Optional[dict] = None   # {"tool":..., "args":...} 到点执行拿结果
    repeat: bool = True             # interval 是否循环；at 恒为单次
    next_fire: float = 0.0


class Scheduler:
    """定时/到点调度器：到点产生消息（静态文本或执行工具的结果）→ agent.push_message。"""

    def __init__(self, agent):
        self._agent = agent
        self._schedules: dict = {}   # id -> Schedule
        self._by_name: dict = {}     # name -> id
        self._lock = threading.Lock()
        self._stop = False
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def add_interval(self, name, seconds, message="", action=None, repeat=True) -> str:
        seconds = float(seconds)
        if seconds <= 0:
            return "[every_seconds 必须 > 0]"
        sch = Schedule(id=uuid.uuid4().hex[:8], name=name, kind="interval", spec=seconds,
                       message=message, action=action, repeat=repeat,
                       next_fire=time.time() + seconds)
        with self._lock:
            self._schedules[sch.id] = sch
            self._by_name[name] = sch.id
        return f"✅ 定时任务「{name}」已加：每 {seconds:g}s 触发（{'循环' if repeat else '单次'}）"

    def add_at(self, name, dt_iso, message="", action=None) -> str:
        try:
            when = datetime.fromisoformat(dt_iso).timestamp()
        except Exception as e:
            return f"[时间格式错误] {dt_iso}（需 ISO 如 2026-07-20T17:30:00 或 2026-07-20 17:30:00）：{e}"
        if when <= time.time():
            return f"[时间已过] {dt_iso}"
        sch = Schedule(id=uuid.uuid4().hex[:8], name=name, kind="at", spec=when,
                       message=message, action=action, repeat=False, next_fire=when)
        with self._lock:
            self._schedules[sch.id] = sch
            self._by_name[name] = sch.id
        return f"✅ 定时任务「{name}」已加：到 {dt_iso} 触发一次"

    def cancel(self, name_or_id) -> str:
        with self._lock:
            sid = name_or_id if name_or_id in self._schedules else self._by_name.get(name_or_id)
            if not sid or sid not in self._schedules:
                return f"[无此任务] {name_or_id}"
            sch = self._schedules.pop(sid)
            self._by_name.pop(sch.name, None)
        return f"🗑 已取消任务「{sch.name}」"

    def list(self) -> str:
        with self._lock:
            items = list(self._schedules.values())
        if not items:
            return "(无定时任务)"
        now = time.time()
        rows = []
        for s in items:
            if s.action:
                payload = f"工具:{s.action.get('tool')}({s.action.get('args')})"
            else:
                payload = s.message or "(空)"
            if s.kind == "interval":
                rows.append(f"  {s.name:<16} 每 {s.spec:g}s {'循环' if s.repeat else '单次'} | {payload[:50]}")
            else:
                left = int(s.next_fire - now)
                rows.append(f"  {s.name:<16} {datetime.fromtimestamp(s.spec).strftime('%m-%d %H:%M:%S')}"
                            f" (还有{left}s) 单次 | {payload[:50]}")
        return "\n".join(rows)

    def _produce(self, sch: Schedule) -> str:
        """产生消息：静态 message，或执行 action 工具拿结果（动态消息）。"""
        if sch.action:
            tool = sch.action.get("tool", "")
            args = sch.action.get("args", {}) or {}
            try:
                result = self._agent.tools.call(tool, args)
                return f"[定时任务「{sch.name}」执行 {tool} 的结果]\n{result}"
            except Exception as e:
                return f"[定时任务「{sch.name}」工具 {tool} 失败] {type(e).__name__}: {e}"
        return sch.message or f"[定时任务「{sch.name}」触发]"

    def _loop(self):
        """后台轮询：到点 produce → push_message；interval 循环重算 next_fire，at 单次删除。"""
        while not self._stop:
            now = time.time()
            fire = []
            with self._lock:
                for sid, sch in list(self._schedules.items()):
                    if sch.next_fire <= now:
                        fire.append(sch)
                        if sch.kind == "interval" and sch.repeat:
                            sch.next_fire = now + sch.spec
                        else:  # at 或 interval 单次：触发后删除
                            self._schedules.pop(sid, None)
                            self._by_name.pop(sch.name, None)
            for sch in fire:
                try:
                    self._agent.push_message(self._produce(sch), source=sch.name)
                except Exception:
                    pass
            time.sleep(_POLL)

    def stop(self):
        self._stop = True
