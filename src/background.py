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
import logging
import os
import re
import signal
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

_LOG = logging.getLogger("agt.background")
_LOG_CAP = 1000  # 每个服务的滚动日志行数上限
_POLL = 0.5      # 调度器轮询间隔（秒）


class ServiceManager:
    """后台长进程管理：Popen 不等待，后台线程收日志，可查状态/停止。"""

    def __init__(self, on_exit=None):
        self._services: dict = {}  # name -> {proc, command, cwd, started_at, pid, logs, manual_stop}
        self._lock = threading.Lock()
        self._on_exit = on_exit   # 进程自行退出回调 on_exit(name, entry, rc)，由 Agent 注入（可 None）

    def start(self, name: str, command: str, cwd: str = "", on_exit_wake: str = "notify",
              on_exit_style: str = "tool", watch_tail: int = 0) -> str:
        with self._lock:
            old = self._services.get(name)
            if old is not None and old["proc"].poll() is None:
                return f"[已存在同名服务] {name}，先 stop_service 再启动"
            # 同名但已退出（stop 过/自行崩过）→ 覆盖重建（用户实锤 2026-10-08：stop 保留
            # entry 做退出复盘，start 撞名被拒——stop→start 重启路径断了；声明 services 的
            # 死服务也无法在下次实例化时被 _ensure_agent_services 重新拉起）。保险补杀。
            if old is not None:
                try:
                    self._kill_tree(old["proc"])
                except Exception:
                    pass
                try:   # 旧句柄回收（防泄漏）
                    if old.get("_lf"):
                        old["_lf"].close()
                except Exception:
                    pass
        popen_kwargs = dict(shell=True, cwd=cwd or None,
                            stdin=subprocess.PIPE,    # 保留 stdin：service_stdin 可向服务写指令（REPL 型服务）
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, bufsize=1, encoding="utf-8", errors="replace")
        # 绑独立进程组/会话：stop 时能整树杀，避免 shell=True 下 terminate 只杀 shell、
        # 漏掉 shell 启的实际命令（孙进程）变孤儿；父进程异常退出也便于外部按组清理。
        # Windows 叠加 CREATE_NO_WINDOW：detached（看门狗重启）场景下服务子进程不弹终端窗。
        if sys.platform == "win32":
            popen_kwargs["creationflags"] = (subprocess.CREATE_NEW_PROCESS_GROUP
                                             | getattr(subprocess, "CREATE_NO_WINDOW", 0))
        else:
            popen_kwargs["start_new_session"] = True
        try:
            proc = subprocess.Popen(command, **popen_kwargs)
        except Exception as e:
            return f"[启动失败] {type(e).__name__}: {e}"
        logs: collections.deque = collections.deque(maxlen=_LOG_CAP)
        entry = {"proc": proc, "command": command, "cwd": cwd,
                 "started_at": time.time(), "pid": proc.pid,
                 "logs": logs, "manual_stop": False,
                 "on_exit_wake": on_exit_wake, "on_exit_style": on_exit_style,
                 "watch_tail": max(0, int(watch_tail))}   # >0：投影的 bg_services 段附日志尾部 N 行
        # 日志持久化（用户实锤 2026-10-08：restart 后完整日志 404——deque 是内存的）：
        # reader 线程 tee 到 ~/.agt/service_logs/<name>.log，实例重启后仍可读历史。
        try:
            from pathlib import Path as _P
            _lf_dir = _P.home() / ".agt" / "service_logs"
            _lf_dir.mkdir(parents=True, exist_ok=True)
            logfile = _lf_dir / (re.sub(r"[^A-Za-z0-9_.-]", "_", name) + ".log")
            entry["logfile"] = str(logfile)
            entry["_lf"] = open(logfile, "a", encoding="utf-8", errors="replace", buffering=1)
            entry["_lf"].write(f"\n===== start {time.strftime('%Y-%m-%d %H:%M:%S')} · pid={proc.pid} · {command[:200]} =====\n")
        except Exception:
            entry["_lf"] = None
        with self._lock:
            self._services[name] = entry

        def _reader():
            _lf = entry.get("_lf")   # tee 到持久化文件（restart 后完整日志可读）
            try:
                for line in proc.stdout:
                    logs.append(line.rstrip("\n"))
                    if _lf is not None:
                        try:
                            _lf.write(line)
                        except Exception:
                            _lf = None
            except Exception:
                pass
            finally:
                if _lf is not None:
                    try:
                        _lf.close()
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

    def send(self, name: str, text: str, expect: str = "", timeout: float = 10.0) -> str:
        """向服务的 stdin 写一行文本（服务须是 REPL 型、会读 stdin——如 agt-web 的 stdin 模式 /
        python REPL / 交互式 CLI）。非 REPL 服务（纯 HTTP server 等）会忽略，无副作用。
        expect（用户提案 2026-10-07）：非空时等 stdout 出现该【正则】才返回——工具结果=
        写入后的新增输出（REPL 往返：写入后等响应，不再盲目立即返回）；timeout=等待上限秒
        （默认 10，超时返回已有新增输出并标注未匹配）。expect 为空=旧行为（立即返回）。"""
        with self._lock:
            e = self._services.get(name)
        if not e:
            return f"[无此服务] {name}"
        proc = e["proc"]
        if proc.poll() is not None:
            return f"[已退出] {name}（rc={proc.poll()}），无法发送"
        stdin = getattr(proc, "stdin", None)
        if stdin is None:
            return f"[stdin 未开] {name} 启动时未接管道（旧版本启动的实例），重启服务后可用"
        logs = e["logs"]
        before = len(logs)   # deque 增量基准（写入前已有行数）
        try:
            stdin.write((text or "") + "\n")
            stdin.flush()
            e["repl_seen"] = True   # 交互即判定 REPL（用户提案 2026-10-08）：能接 stdin 的
                                    # 服务下轮投影 bg_services 段自动发 /status 带上输出
        except (BrokenPipeError, OSError) as ex:
            return f"[发送失败] {name}: {type(ex).__name__}（进程可能已关闭 stdin）"
        if not expect:
            return f"📤 已发送到「{name}」stdin：{(text or '')[:80]}"
        import re as _re, itertools as _it
        deadline = time.time() + max(0.5, float(timeout))
        new_lines = []
        while time.time() < deadline:
            time.sleep(0.15)
            with self._lock:
                new_lines = list(_it.islice(logs, before, None))
            if _re.search(expect, "\n".join(new_lines)):
                return (f"✅ 已发送并匹配到 /{expect}/（新增 {len(new_lines)} 行）：\n"
                        + ("\n".join(new_lines)[-4000:] or "(空)"))
        return (f"⏱ {timeout}s 内未匹配 /{expect}/（可加大 timeout 或确认服务真的回显）。"
                f"期间新增 {len(new_lines)} 行：\n" + ("\n".join(new_lines)[-4000:] or "(无新增输出)"))

    def _repl_status(self, name: str, e: dict, now: float) -> str:
        """repl: 服务的协议状态轮询：stdin 发 /status，收一行响应（用户提案 2026-10-08）。
        MCP 式请求-响应：一次 stdin 对应一次 stdout；响应截前 watch_tail 行取首行摘要。
        5s 节流：连续步进投影不重复打（_repl_cache[name] = (时刻, 响应行)）。"""
        cache = getattr(self, "_repl_cache", None)
        if cache is None:
            cache = self._repl_cache = {}
        hit = cache.get(name)
        if hit and now - hit[0] < 5.0:
            return hit[1]
        # 多行响应收集（用户提案 2026-10-08：前 N 行，不只首行）——发 /status 后等
        # 「静默窗口」（0.4s 无新行=响应收完）或 2s 上限，取前 N 行（N=watch_tail，默认 5）。
        proc = e.get("proc")
        stdin = getattr(proc, "stdin", None) if proc is not None else None
        if stdin is None:
            return ""
        logs = e["logs"]
        before = len(logs)
        try:
            stdin.write("/status\n")
            stdin.flush()
        except (BrokenPipeError, OSError):
            return ""
        import itertools as _it
        n_max = min(int(e.get("watch_tail") or 5) or 5, 5)   # 用户口径：前 5 行封顶
        deadline = time.time() + 2.0
        last_change = time.time()
        got = []
        while time.time() < deadline:
            time.sleep(0.12)
            cur = list(_it.islice(logs, before, None))
            if len(cur) != len(got):
                got = cur
                last_change = time.time()
            elif got and time.time() - last_change > 0.4:
                break   # 静默窗口：响应收完
        lines = [l.strip() for l in got if l.strip() and not l.strip().startswith(">")][:n_max]
        if not lines:
            return ""
        joined = "\n".join(lines)
        cache[name] = (now, joined)
        return joined

    def status_lines(self) -> list:
        """供 system prompt 注入：每个服务一行 name(状态, pid, 已跑 Ns)。已退出标'需重启'。"""
        repl_marks = []
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
                # repl 服务（用户提案 2026-10-08）：① 命名声明：repl: 前缀；② 交互自动判定：
                # 曾被 service_stdin 发过参数（repl_seen）——能接 stdin 的即 REPL 语义。
                # 每步投影自动发 /status 收前 N 行；5s 节流缓存。
                # 潜规则：repl 服务的 stdout 仅用于协议响应，过程日志写文件不污染 stdout。
                if (name.startswith("repl:") or e.get("repl_seen")) and rc is None:
                    repl_marks.append((len(lines), name))   # 占位，锁外轮询填充
                    lines.append(None)
                # watch_tail>0（用户提案 2026-10-07）：附日志尾部 N 行——每步投影可见服务实况
                # （零协议：不要求服务实现 /status，stdout 日志 deque 天然即状态）
                wt = int(e.get("watch_tail") or 0)
                if wt > 0 and not (name.startswith("repl:") or e.get("repl_seen")):
                    tail = list(e["logs"])[-wt:]
                    lines.extend("    │ " + l for l in tail) if tail else lines.append("    │ (暂无输出)")
        # 锁外做 repl 协议轮询——send/_repl_status 自己要拿 _lock（Lock 不可重入，锁内调用会死锁）
        for pos, name in repl_marks:
            line = self._repl_status(name, self._services.get(name, {}), time.time())
            lines[pos] = ("    │ " + line.replace("\n", "\n    │ ")) if line else None
        return [l for l in lines if l is not None]

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

    def snapshot(self, tail: int = 120) -> list:
        """结构化快照（WebUI 后台服务看板用）：每服务 {name, running, rc, pid,
        started_at, uptime, command, cwd, on_exit_wake, logs:[最近 tail 行]}。
        内部 _services 带锁读出——server 侧不直接摸内部结构。"""
        with self._lock:
            now = time.time()
            out = []
            for name, e in self._services.items():
                rc = e["proc"].poll()
                running = rc is None
                out.append({
                    "name": name,
                    "running": running,
                    "rc": rc,
                    "pid": e.get("pid"),
                    "started_at": e.get("started_at"),
                    "uptime": int(now - e["started_at"]) if running and e.get("started_at") else 0,
                    "command": e.get("command", ""),
                    "cwd": e.get("cwd", ""),
                    "on_exit_wake": e.get("on_exit_wake", ""),
                    "logs": list(e["logs"])[-max(1, int(tail)):],
                })
            return out

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
                               capture_output=True, timeout=5,
                               creationflags=(subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0))
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
    message: str = ""               # 静态推送文本（与 action/code 三选一）
    action: Optional[dict] = None   # {"tool":..., "args":...} 到点执行拿结果
    code: str = ""                  # 触发时跑的 Python 代码（2026-10-04·用户提案：autonomous 融合）——
                                    # stdout 尾部 + result 变量作为消息；空产物=该次静默
    deadline: float = 0.0           # 截止时间戳（2026-10-04）：过期任务自动删除（取代 autonomous 的 end_time）
    mode: str = "idle"              # 注入方式（2026-10-04）：idle=排队等空闲（默认，现状）|
                                    # immediate=busy 时打断当前轮（步边界插话）| skip=busy 时放弃本次
    repeat: bool = True             # interval 是否循环；at+daily 每日闹钟
    daily: str = ""                 # at 每日模式 "HH:MM[:SS]"（每日闹钟重算锚点）
    at_origin: str = ""             # at 原始参数（ISO）；at 未填的 interval 记创建时刻——
                                    # 持久化相位锚点（2026-09-18·用户提案：恢复时重算不漂移）
    next_fire: float = 0.0


_DAILY_RE = re.compile(r"^\d{1,2}:\d{1,2}(:\d{1,2})?$")


def _next_daily_fire(hhmm: str) -> float:
    """每日时刻 → 下一个未来的本地时间戳（今天没到=今天，否则明天）。"""
    parts = [int(p) for p in hhmm.split(":")]
    h, m = parts[0], parts[1]
    sec = parts[2] if len(parts) > 2 else 0
    now = datetime.now()
    cand = now.replace(hour=h, minute=m, second=sec, microsecond=0)
    if cand.timestamp() <= time.time():
        cand += timedelta(days=1)
    return cand.timestamp()


class Scheduler:
    """定时/到点调度器：到点产生消息（静态文本或执行工具的结果）→ agent.push_message。"""

    def __init__(self, agent):
        self._agent = agent
        self._schedules: dict = {}   # id -> Schedule
        self._by_name: dict = {}     # name -> id
        # RLock：_loop 触发删除在锁内调 _persist（自身再拿锁）——Lock 不可重入会死锁
        self._lock = threading.RLock()
        self._stop = False
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        # 注：恢复不走 __init__（此时 session 还没 load——Agent.__init__ L296 建 Scheduler 早于
        # session 装载）——由 Agent.restore_runtime_state（set_session/load 后的标准恢复点）
        # 调 restore_state(items)（2026-09-18 修：原 _restore() 在此空跑，恢复时机错误）。

    def add_interval(self, name, seconds, message="", action=None, repeat=True, code="", deadline=0.0, mode="idle") -> str:
        seconds = float(seconds)
        if seconds <= 0:
            return "[every_seconds 必须 > 0]"
        # at 未填默认 now（2026-09-18·用户语义：假装第一次已在 now 触发过）——记为相位锚点，
        # 持久化恢复时按它重算下一个未来相位点（相位不漂移）
        at_origin = datetime.now().isoformat(timespec="seconds")
        sch = Schedule(id=uuid.uuid4().hex[:8], name=name, kind="interval", spec=seconds,
                       message=message, action=action, repeat=repeat, at_origin=at_origin,
                       code=code, deadline=float(deadline or 0), mode=mode,
                       next_fire=time.time() + seconds)
        with self._lock:
            old_id = self._by_name.get(name)
            if old_id:
                self._schedules.pop(old_id, None)   # 同名覆盖先摘旧（2026-09-18 修：残留=双任务各投一次，pre_post 重复投递实锤）
            self._schedules[sch.id] = sch
            self._by_name[name] = sch.id
        self._persist()
        return f"✅ 定时任务「{name}」已加：每 {seconds:g}s 触发（{'循环' if repeat else '单次'}）"

    @staticmethod
    def _phase_next(origin_iso: str, sec: float) -> float:
        """at_origin（ISO）+ 周期 → 下一个未来相位点；origin 缺失/解析失败 → now+sec 兜底。"""
        try:
            import math
            phase_ts = datetime.fromisoformat(str(origin_iso).replace("Z", "+00:00")).timestamp()
            now = time.time()
            if phase_ts < now:
                return phase_ts + math.ceil((now - phase_ts) / sec) * sec
            return phase_ts
        except Exception:
            return time.time() + sec

    def add_interval_at(self, name, seconds, at_iso, message="", action=None, repeat=True, code="", deadline=0.0, mode="idle") -> str:
        """组合模式（2026-09-18·用户提案）：every_seconds + at + repeat=True →
        at 为【首触发相位起点】，之后每 seconds 循环一次。首次触发 = at + N*seconds 中
        第一个未来时刻（at 已过去则自动对齐到下一个相位点；at 在未来则等到 at 到点）。"""
        seconds = float(seconds)
        if seconds <= 0:
            return "[every_seconds 必须 > 0]"
        s = (at_iso or "").strip()
        try:
            datetime.fromisoformat(s.replace("Z", "+00:00"))
        except Exception as e:
            return (f"[时间格式错误] 组合模式要求 at 为完整 ISO（如 2026-07-20T17:30:00，"
                    f"需含日期；每日闹钟短格式 HH:MM 请单独使用不配 every_seconds）：{e}")
        fire = self._phase_next(s, seconds)
        sch = Schedule(id=uuid.uuid4().hex[:8], name=name, kind="interval", spec=seconds,
                       message=message, action=action, repeat=bool(repeat), at_origin=s,
                       code=code, deadline=float(deadline or 0), mode=mode,
                       next_fire=fire)
        with self._lock:
            old_id = self._by_name.get(name)
            if old_id:
                self._schedules.pop(old_id, None)   # 同名覆盖先摘旧（2026-09-18 修：残留=双任务各投一次，pre_post 重复投递实锤）
            self._schedules[sch.id] = sch
            self._by_name[name] = sch.id
        self._persist()
        when = datetime.fromtimestamp(fire).strftime("%m-%d %H:%M:%S")
        return f"✅ 定时任务「{name}」已加：首次 {when}（每 {seconds:g}s {'循环' if repeat else '单次'}，相位 {s}）"

    def reschedule(self, name, every_seconds=None, at=None, deadline=None,
                   repeat=None, message=None) -> str:
        """部分更新已存在的定时任务（用户提案 2026-10-07：抽屉可改下次触发/every_seconds/
        deadline 等）——未提供的字段保持原值；id 不变（进行中的引用不断）。"""
        with self._lock:
            s = next((x for x in self._schedules.values() if x.name == name), None)
            if s is None:
                return f"[无此任务] {name}"
            if every_seconds is not None:
                sec = float(every_seconds)
                if sec <= 0:
                    return "[every_seconds 必须 > 0]"
                s.spec = sec
                s.kind = "interval"
                s.at_origin = datetime.now().isoformat(timespec="seconds")   # 新相位锚
                s.next_fire = self._phase_next(s.at_origin, sec)
            if at is not None and str(at).strip():
                s.at_origin = str(at).strip()
                if s.kind == "interval" and s.spec > 0:
                    s.next_fire = self._phase_next(s.at_origin, s.spec)
                else:
                    try:
                        s.kind = "at"
                        s.spec = datetime.fromisoformat(str(at).replace("Z", "+00:00")).timestamp()
                        s.next_fire = s.spec
                    except Exception as e:
                        return f"[时间格式错误] {e}"
            if deadline is not None:
                s.deadline = float(deadline or 0)
            if repeat is not None:
                s.repeat = bool(repeat)
            if message is not None:
                s.message = str(message)
        self._persist()
        return (f"✅ 「{name}」已更新：下次触发 "
                f"{datetime.fromtimestamp(s.next_fire).strftime('%m-%d %H:%M:%S')}"
                f"{('，每 ' + str(s.spec) + 's') if s.kind == 'interval' else ''}")

    def export_state(self) -> list:
        """序列化全部任务定义（capture_runtime_state 收集用——2026-09-18 修：extra_state 是
        provider 覆盖式重建，_persist 直写的值会被任意落盘抹掉；真源=本方法从 _schedules 收）。"""
        items = []
        with self._lock:
            for sch in self._schedules.values():
                items.append({"name": sch.name, "kind": sch.kind, "spec": sch.spec,
                              "at_origin": sch.at_origin, "message": sch.message,
                              "action": sch.action, "repeat": sch.repeat, "daily": sch.daily,
                              "code": sch.code, "deadline": sch.deadline, "mode": sch.mode})
        return items

    def _persist(self):
        """触发 session 落盘（meta.json 的 schedules 由 capture_runtime_state 从
        export_state() 收集——单一真源；本方法不再直写 extra_state，写了也会被
        provider 覆盖式重建抹掉，那是 meta.json 从未出现 schedules 的根因）。"""
        try:
            sess = getattr(self._agent, "session", None)
            if sess is None:
                return
            try:
                sess.save()   # save → _capture_state → provider 收集 schedules → meta.json
            except Exception as e:
                _LOG.warning("schedules meta 落盘失败：%s", e)
        except Exception as e:
            _LOG.warning("schedules 持久化失败：%s", e)

    def restore_state(self, items):
        """从 meta.json 的 schedules 定义恢复定时任务（Agent.restore_runtime_state 在
        set_session/load 后调——标准恢复点）。相位重算：interval 按 at_origin 对齐下一个
        未来相位点；at+repeat（每日）重算 _next_daily_fire；at 单次已过去的丢弃（触发过了）。"""
        if not items:
            return
        try:
            n = 0
            for it in items:
                try:
                    if it.get("kind") == "interval":
                        sec = float(it.get("spec") or 0)
                        if sec <= 0:
                            continue
                        origin = it.get("at_origin", "")
                        s = Schedule(id=uuid.uuid4().hex[:8], name=it.get("name", "task"),
                                     kind="interval", spec=sec, message=it.get("message", ""),
                                     action=it.get("action"), repeat=bool(it.get("repeat", True)),
                                     at_origin=origin, next_fire=self._phase_next(origin, sec),
                                     code=it.get("code", ""), deadline=float(it.get("deadline") or 0),
                                     mode=it.get("mode") or "idle")
                    else:   # kind == "at"：每日（repeat+daily）或单次
                        daily, rep = it.get("daily", ""), bool(it.get("repeat", False))
                        if rep and daily:
                            fire = _next_daily_fire(daily)
                            s = Schedule(id=uuid.uuid4().hex[:8], name=it.get("name", "task"),
                                         kind="at", spec=fire, message=it.get("message", ""),
                                         action=it.get("action"), repeat=True, daily=daily,
                                         at_origin=it.get("at_origin", ""), next_fire=fire,
                                         code=it.get("code", ""), deadline=float(it.get("deadline") or 0),
                                         mode=it.get("mode") or "idle")
                        else:
                            fire = float(it.get("spec") or 0)
                            if fire <= time.time():
                                continue   # 单次已触发过——不恢复
                            s = Schedule(id=uuid.uuid4().hex[:8], name=it.get("name", "task"),
                                         kind="at", spec=fire, message=it.get("message", ""),
                                         action=it.get("action"), repeat=False,
                                         at_origin=it.get("at_origin", ""), next_fire=fire,
                                         code=it.get("code", ""), deadline=float(it.get("deadline") or 0),
                                         mode=it.get("mode") or "idle")
                    with self._lock:
                        self._schedules[s.id] = s
                        self._by_name[s.name] = s.id
                    n += 1
                except Exception:
                    continue
            if n:
                _LOG.info("定时任务恢复 %d 个（meta.json extra_state.schedules）", n)
        except Exception as e:
            _LOG.warning("schedules 恢复失败：%s", e)

    def add_at(self, name, dt_iso, message="", action=None, repeat=None, code="", deadline=0.0, mode="idle") -> str:
        """到点任务。两种格式：
        - 完整 ISO（2026-07-20T17:30:00）→ 单次到点（repeat 不传时默认单次，显式 True 则每日该时刻循环）
        - 短格式 HH:MM[:SS]（17:30）→ 每日闹钟（repeat 默认 True；显式 False 只响下一个该时刻一次）"""
        s = (dt_iso or "").strip()
        if not s:
            return "[时间格式错误] at 不能为空（ISO 如 2026-07-20T17:30:00；每日闹钟用 HH:MM 如 17:30）"
        if _DAILY_RE.match(s):   # 短格式：每日闹钟（默认循环）
            if not (0 <= int(s.split(":")[0]) <= 23 and 0 <= int(s.split(":")[1]) <= 59):
                return f"[时间格式错误] {s}（HH:MM 的 HH≤23、MM≤59）"
            rep = True if repeat is None else bool(repeat)
            when = _next_daily_fire(s)
            daily = s
        else:                    # 完整 ISO：默认单次；显式 repeat=True → 每日该时刻
            try:
                when = datetime.fromisoformat(s).timestamp()
            except Exception as e:
                return f"[时间格式错误] {s}（需 ISO 如 2026-07-20T17:30:00 或 2026-07-20 17:30:00；每日闹钟用 HH:MM 如 17:30）：{e}"
            if when <= time.time():
                return f"[时间已过] {s}"
            rep = False if repeat is None else bool(repeat)
            if rep:
                daily = s.split("T")[-1].split(" ")[-1][:8]   # 取时刻部分做每日锚点
                when = _next_daily_fire(daily)
            else:
                daily = ""
        sch = Schedule(id=uuid.uuid4().hex[:8], name=name, kind="at", spec=when,
                       message=message, action=action, repeat=rep, daily=daily,
                       code=code, deadline=float(deadline or 0), mode=mode,
                       at_origin=s, next_fire=when)   # at_origin=at 原始参数（恢复重算锚点）
        with self._lock:
            old_id = self._by_name.get(name)
            if old_id:
                self._schedules.pop(old_id, None)   # 同名覆盖先摘旧（2026-09-18 修：残留=双任务各投一次，pre_post 重复投递实锤）
            self._schedules[sch.id] = sch
            self._by_name[name] = sch.id
        self._persist()
        disp = f"每天 {daily}" if rep else f"到 {s} 触发一次"
        return f"✅ 定时任务「{name}」已加：{disp}"

    def cancel(self, name_or_id) -> str:
        with self._lock:
            sid = name_or_id if name_or_id in self._schedules else self._by_name.get(name_or_id)
            if not sid or sid not in self._schedules:
                return f"[无此任务] {name_or_id}"
            sch = self._schedules.pop(sid)
            self._by_name.pop(sch.name, None)
        self._persist()
        return f"🗑 已取消任务「{sch.name}」"

    def snapshot(self) -> list:
        """结构化快照（WebUI 后台服务看板·定时任务分组用）：每任务 {id, name, kind,
        interval_secs, fire_at, next_fire_in, repeat, message, tool, tool_args}。
        schedule 与 service 是两个独立管理器（都是 Agent 后台 producer：
        service=长进程有日志可展开；schedule=定时器无日志只有触发时间线）。"""
        with self._lock:
            items = list(self._schedules.values())
        now = time.time()
        out = []
        for s in items:
            item = {
                "id": s.id,
                "name": s.name,
                "kind": s.kind,           # interval | at
                "repeat": bool(s.repeat),
                "next_fire_in": max(0, int(s.next_fire - now)) if s.next_fire else None,
                "message": s.message or "",
            }
            if s.action:
                item["tool"] = str(s.action.get("tool") or "")
                item["tool_args"] = s.action.get("args")
            if s.kind == "interval":
                item["interval_secs"] = float(s.spec)
            elif s.daily:
                item["daily"] = s.daily                 # 每日闹钟 HH:MM[:SS]
            else:
                item["fire_at"] = datetime.fromtimestamp(s.spec).strftime("%m-%d %H:%M:%S")
            out.append(item)
        return out

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
            elif s.daily:
                left = int(s.next_fire - now)
                rows.append(f"  {s.name:<16} 每天 {s.daily} (还有{left}s) | {payload[:50]}")
            else:
                left = int(s.next_fire - now)
                rows.append(f"  {s.name:<16} {datetime.fromtimestamp(s.spec).strftime('%m-%d %H:%M:%S')}"
                            f" (还有{left}s) 单次 | {payload[:50]}")
        return "\n".join(rows)

    def _produce(self, sch: Schedule):
        """产生消息：message / action 工具 / code 三通道【主从语义】（2026-10-05·用户裁定 v2）。
        - code / action 是【主通道】（干活/判定）；message 是【附言】。
        - 传了 code 或 action：主通道产物全空 → 该次静默（message 不单独发）；
          有产物 → 产物 + message 拼接注入（顺序 code → tool → message）。
        - 只传 message：正常注入（心跳场景）。
        code 失败算“有产物”（错误段入拼接，不静默）。全空 → None。"""
        parts = []          # 最终拼接（含 message）
        primary = []        # 主通道产物（code/action）
        if sch.code:
            import io as _io, contextlib as _cl
            buf, g = _io.StringIO(), {"agent": self._agent, "result": None}
            try:
                with _cl.redirect_stdout(buf):
                    exec(sch.code, {"__builtins__": __builtins__}, g)
            except Exception as e:
                primary.append(f"[{sch.name} · code 失败] {type(e).__name__}: {e}")
            else:
                out = (g.get("result") or "").strip() if isinstance(g.get("result"), str) else (
                    str(g["result"]).strip() if g.get("result") is not None else "")
                tail = buf.getvalue().strip()[-2000:]
                seg = "\n".join(x for x in (out, tail) if x)
                if seg:
                    primary.append(f"[{sch.name} · code 结果]\n" + seg)
        if sch.action:
            tool = sch.action.get("tool", "")
            args = sch.action.get("args", {}) or {}
            try:
                result = str(self._agent.tools.call(tool, args) or "").strip()
                if result in ("(无输出)", "(no output)"):
                    result = ""   # 工具层空输出占位符（real_tools L364/L1896）不算产物（用户实锤 2026-10-05）
                if result:   # 空返回不算产物（与 code 静默对齐，用户裁定 v2）
                    primary.append(f"[{sch.name} · {tool} 结果]\n{result}")
            except Exception as e:
                primary.append(f"[{sch.name} · 工具 {tool} 失败] {type(e).__name__}: {e}")
        if (sch.code or sch.action) and not primary:
            return None   # 主通道全空 → 全静默（message 是附言不单独发，用户裁定 v2）
        parts.extend(primary)
        if sch.message:
            parts.append(sch.message)
        if not parts:
            return None
        return "\n\n".join(parts)

    def _loop(self):
        """后台轮询：到点 produce → push_message；interval 循环重算 next_fire；
        at+daily（每日闹钟）触发后推到明天同一时刻；at 单次触发后删除。"""
        while not self._stop:
            now = time.time()
            fire = []
            with self._lock:
                for sid, sch in list(self._schedules.items()):
                    if sch.deadline and now > sch.deadline:   # 过期（2026-10-04·取代 autonomous end_time）
                        self._schedules.pop(sid, None)
                        self._by_name.pop(sch.name, None)
                        _LOG.info("定时任务「%s」已过 deadline，自动删除", sch.name)
                        continue
                    if sch.next_fire <= now:
                        fire.append(sch)
                        if sch.kind == "interval" and sch.repeat:
                            sch.next_fire = now + sch.spec
                        elif sch.kind == "at" and sch.repeat and sch.daily:
                            sch.next_fire = _next_daily_fire(sch.daily)   # 明天同一时刻
                        else:  # at 单次 或 interval 单次：触发后删除
                            self._schedules.pop(sid, None)
                            self._by_name.pop(sch.name, None)
            for sch in fire:
                try:
                    text = self._produce(sch)
                    if text is None:
                        continue   # 静默（code 空产物）
                    busy = getattr(getattr(self._agent, "session", None), "_current", None) is not None
                    if sch.mode == "skip" and busy:
                        continue   # busy 时放弃本次注入
                    if sch.mode == "immediate" and busy:
                        # 打断当前轮：塞插话队列（步边界注入，与用户插话同款——autonomous 原语义）
                        pm = getattr(self._agent, "pending_messages", None)
                        if pm is not None:
                            pm.append(text)
                            continue
                    self._agent.push_message(text, source=sch.name)   # idle（默认）：排队等空闲
                except Exception:
                    pass
            if fire:
                self._persist()   # 单次任务触发完删除——持久化同步消失（RLock 嵌套安全）
            time.sleep(_POLL)

    def stop(self):
        self._stop = True
