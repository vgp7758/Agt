"""dashboard.py —— agt-web --dashboard 多实例统一看板（spec s_95425415）。

用途：把多个 agt 实例（本机多端口 / 局域网 / cpolar 公网隧道）收进一张管理面：
状态总览（🟢空闲/🟡忙碌/🔴离线）、本地实例启停、拉网线（wire=对 A 调 /api/remote/add）、
互动群聊流（发送方视角 WS 采集 remote_*/notify 调用）。

启动：`agt-web --dashboard [port]`（默认 9100，chat.web_main 分流到本模块 serve()）。
**不装配 Agent**——纯管理面：不占 session、不消耗 LLM、崩了不影响任何被管实例。

数据文件（AGT_HOME 走 paths.AGT_DIR，默认 ~/.agt）：
  dashboard.json               实例注册表（instances + pids）
  dashboard_interactions.jsonl 互动流追加日志（重启不丢，加载时指纹去重回填）

模块结构（按 spec 施工顺序）：
  DashboardStore     注册表读写（原子写 tmp+replace）
  probe_instance     POST /api/status（4s 超时；401/403 带 X-Cb-Token 重试）
  start/stop/discover 本地实例生命周期（Popen 拉起 / taskkill 杀树 / netstat 反查接管）
  wire/unwire/topology 组网（复用现有端点，零改动）与邻接表
  InteractionHub     per-instance WS 监听线程（断线 10s 重连）→ 群聊记录
  serve()            FastAPI + uvicorn 前台 + 开浏览器
"""
from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
import threading
import time
import uuid
import webbrowser
from collections import deque
from pathlib import Path
from typing import Any, Optional

if __package__ in (None, ""):   # 直接 python dashboard.py 调试时补 src 入 sys.path
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from paths import AGT_DIR   # noqa: E402

DEFAULT_PORT = 9100
REG_PATH = AGT_DIR / "dashboard.json"
INTERACTIONS_PATH = AGT_DIR / "dashboard_interactions.jsonl"
STATUS_TIMEOUT = 4.0            # probe /api/status 超时
START_WAIT = 60                 # start_instance 轮就绪上限（秒）
HUB_RECONNECT = 10              # WS 断线重连间隔（秒）
RING_SIZE = 2000                # 互动流内存环
TEXT_MAX = 2000                 # 互动 text 截断
PROBE_INTERVAL = 3.0            # 状态轮询周期
TOPO_EVERY = 2                  # 每 N 轮 probe 刷一次拓扑
DISCOVER_DIAG: dict = {}        # 最近一次 discover_local 的诊断（/api/discover 带 _diag）

# 互动采集关注的工具（发送方视角——参数里带完整 to + text，探索实证）
WATCHED_TOOLS = {"remote_message", "remote_ask", "remote_call_tool",
                 "agent_notify", "agent_prompt"}

# netstat 自动发现排除端口（已知非 agt 的常驻监听：lsp/pydevd/pprof/es/clash）
EXCLUDE_PORTS = {4040, 6060, 9200, 7897}


def _now() -> float:
    return time.time()


def _short_id() -> str:
    return uuid.uuid4().hex[:8]


# ---------------------------------------------------------------- HTTP 小助手

def _http(method: str, url: str, body: Optional[dict] = None,
          token: str = "", timeout: float = STATUS_TIMEOUT) -> tuple[int, Any]:
    """极简 HTTP 客户端（标准库，零依赖）：返回 (status_code, 解析后 body)。
    body dict → JSON POST；None → GET。非 2xx 抛 urllib.error.HTTPError（调用方处理）。"""
    import urllib.request
    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if token:
        headers["X-Cb-Token"] = token
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", "replace")
        code = resp.getcode()
    try:
        return code, json.loads(raw)
    except Exception:
        return code, raw


def _token_retry(e: Exception) -> bool:
    """401/403 → 值得带 token 重试一次。"""
    code = getattr(e, "code", 0)
    return code in (401, 403)


# ================================================================ Step1 Store

class DashboardStore:
    """实例注册表：~/.agt/dashboard.json。
    schema {"instances":[{id,name,kind,url,public_url,repo,port,start_args,token,note}],
            "pids":{port:pid}}（json key 必须字符串）。"""

    def __init__(self, path: Path = REG_PATH):
        self.path = Path(path)
        self._lock = threading.RLock()
        self.data = self._load()

    def _load(self) -> dict:
        try:
            d = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(d, dict):
                d.setdefault("instances", [])
                d.setdefault("pids", {})
                return d
        except FileNotFoundError:
            pass
        except Exception as e:
            print(f"⚠️ dashboard.json 读取失败（用空注册表顶上）：{type(e).__name__}: {e}")
        return {"instances": [], "pids": {}}

    def save(self) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=2),
                           encoding="utf-8")
            os.replace(tmp, self.path)   # 原子写：进程被杀也不会留半截 json

    def list(self) -> list[dict]:
        with self._lock:
            return [dict(e) for e in self.data["instances"]]

    def get(self, entry_id: str) -> Optional[dict]:
        with self._lock:
            for e in self.data["instances"]:
                if e.get("id") == entry_id:
                    return dict(e)
        return None

    def add(self, entry: dict) -> dict:
        with self._lock:
            e = dict(entry)
            e["id"] = e.get("id") or _short_id()
            if not e.get("name"):
                e["name"] = f"agt-{e.get('port') or e['id']}"
            self.data["instances"].append(e)
            self.save()
            return dict(e)

    def update(self, entry_id: str, patch: dict) -> Optional[dict]:
        with self._lock:
            for e in self.data["instances"]:
                if e.get("id") == entry_id:
                    for k, v in patch.items():
                        if k in ("id",):
                            continue
                        if v is None:
                            e.pop(k, None)
                        else:
                            e[k] = v
                    self.save()
                    return dict(e)
        return None

    def remove(self, entry_id: str) -> bool:
        with self._lock:
            before = len(self.data["instances"])
            self.data["instances"] = [e for e in self.data["instances"]
                                      if e.get("id") != entry_id]
            if len(self.data["instances"]) != before:
                self.save()
                return True
        return False

    # —— pids（port→pid，stop 时杀树用；json 要求字符串 key）——
    def set_pid(self, port: int, pid: int) -> None:
        with self._lock:
            if pid:
                self.data["pids"][str(port)] = int(pid)
            else:
                self.data["pids"].pop(str(port), None)
            self.save()

    def get_pid(self, port: int) -> int:
        with self._lock:
            return int(self.data["pids"].get(str(port)) or 0)


def probe_instance(entry: dict, timeout: float = STATUS_TIMEOUT) -> dict:
    """探测一个实例：POST {url}/api/status（4s 超时）。
    401/403 → 带 X-Cb-Token 重试一次（公网隧道开了 callback_token 的部署）。
    返回 {"alive":True, ready/busy/model/session_name/session_turns/recap, "checked_at":ts}
    或 {"alive":False, "error":..., "checked_at":ts}。"""
    url = (entry.get("url") or entry.get("public_url") or "").rstrip("/")
    out: dict = {"checked_at": _now()}
    if not url:
        out.update(alive=False, error="no url")
        return out
    token = entry.get("token") or ""
    try:
        try:
            code, data = _http("POST", url + "/api/status", {}, timeout=timeout)
        except Exception as e:
            if _token_retry(e) and token:
                code, data = _http("POST", url + "/api/status", {},
                                   token=token, timeout=timeout)
            else:
                raise
        out["alive"] = True
        out["http"] = code
        if isinstance(data, dict):
            for k in ("ready", "busy", "model", "session_name",
                      "session_turns", "recap"):
                if k in data:
                    out[k] = data[k]
        return out
    except Exception as e:
        out["alive"] = False
        err = getattr(e, "code", None)
        out["error"] = f"HTTP {err}" if err is not None else f"{type(e).__name__}: {e}"
        if _token_retry(e):
            out["error"] += "（需 token）"
        return out


# ============================================================ Step2 生命周期

def _pid_alive(pid: int) -> bool:
    """进程存活校验。Windows 不用 os.kill(pid,0)（其它 sig 值=TerminateProcess，会误杀）——
    ctypes OpenProcess(SYNCHRONIZE) 只读探测。"""
    if not pid:
        return False
    if os.name == "nt":
        try:
            import ctypes
            SYNCHRONIZE = 0x00100000
            h = ctypes.windll.kernel32.OpenProcess(SYNCHRONIZE, False, int(pid))
            if not h:
                return False
            ctypes.windll.kernel32.CloseHandle(h)
            return True
        except Exception:
            return False
    try:
        os.kill(int(pid), 0)
        return True
    except OSError:
        return False


def _pid_by_port(port: int) -> int:
    """netstat 反查端口→pid（LISTEN 态）。dashboard 重启后 pids 失联时的接管手段。"""
    if not port:
        return 0
    try:
        if os.name == "nt":
            out = subprocess.run(["netstat", "-ano"], capture_output=True,
                                 text=True, timeout=15).stdout or ""
            for line in out.splitlines():
                parts = line.split()
                if len(parts) >= 5 and parts[0].upper() == "TCP" \
                        and parts[3].upper() == "LISTENING" \
                        and parts[1].rsplit(":", 1)[-1] == str(port):
                    try:
                        return int(parts[4])
                    except ValueError:
                        continue
            return 0
        out = subprocess.run(["ss", "-tlnp"], capture_output=True,
                             text=True, timeout=15).stdout or ""
        for line in out.splitlines():
            if re.search(rf":{port}\s", line):
                m = re.search(r"pid=(\d+)", line)
                if m:
                    return int(m.group(1))
        return 0
    except Exception:
        return 0


def start_instance(entry: dict, store: DashboardStore,
                   probe: Optional[dict] = None) -> dict:
    """拉起本地实例：Popen(['agt-web', port]+start_args, cwd=repo, 分离进程组)。
    pid 记入注册表；后台线程轮 /api/status 等 60s（probe dict 传引用则同步推进度）。"""
    if entry.get("kind") != "local":
        return {"ok": False, "error": "远程实例不能由 dashboard 启动"}
    port = int(entry.get("port") or 8000)
    repo = entry.get("repo") or None
    try:
        extra = shlex.split(entry.get("start_args") or "")
    except ValueError:
        extra = []
    # agt-web 入口：pip 装机有 console_script；源码形态回退当前解释器拉 web_main
    cmd = ["agt-web", str(port)] + extra
    kwargs: dict[str, Any] = {}
    if os.name == "nt":
        kwargs["creationflags"] = (getattr(subprocess, "DETACHED_PROCESS", 0)
                                   | getattr(subprocess, "CREATE_NO_WINDOW", 0)
                                   | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
    else:
        kwargs["start_new_session"] = True
    try:
        proc = subprocess.Popen(cmd, cwd=repo, stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                **kwargs)
    except FileNotFoundError:
        # PATH 上没有 agt-web（源码跑法）→ 用当前解释器拉 chat.web_main 同款入口
        code = (f"import sys; sys.path.insert(0, 'src'); "
                f"from chat import web_main; web_main({port})")
        try:
            proc = subprocess.Popen([sys.executable, "-c", code],
                                    cwd=repo, stdin=subprocess.DEVNULL,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                    **kwargs)
        except Exception as e:
            return {"ok": False, "error": f"找不到 agt-web 且源码拉起失败: {e}"}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    store.set_pid(port, proc.pid)
    if probe is not None:
        probe.update(alive=False, starting=True, pid=proc.pid)
    threading.Thread(target=_wait_ready, args=(entry, probe), daemon=True).start()
    return {"ok": True, "pid": proc.pid}


def _wait_ready(entry: dict, probe: Optional[dict], wait: float = START_WAIT) -> None:
    """start 后台线程：轮 /api/status 直到 ready（agent 装配完成）或超时。"""
    deadline = _now() + wait
    while _now() < deadline:
        st = probe_instance(entry, timeout=3.0)
        if probe is not None:
            probe.update({k: v for k, v in st.items()})
            if st.get("alive"):
                probe.pop("starting", None)
        if st.get("ready"):
            return
        time.sleep(2.0)


def stop_instance(entry: dict, store: DashboardStore) -> dict:
    """停本地实例：优先注册表 pid（存活校验），失联 → netstat 反查接管；
    Windows taskkill /T /F 杀进程树（agt 实例带 MCP 子进程），POSIX 杀进程组。"""
    if entry.get("kind") != "local":
        return {"ok": False, "error": "远程实例不能由 dashboard 停止"}
    port = int(entry.get("port") or 0)
    pid = store.get_pid(port)
    if pid and not _pid_alive(pid):
        pid = 0                    # 上一任 dashboard 记的 pid 已死
    if not pid:
        pid = _pid_by_port(port)   # netstat 接管（实例仍活则不误杀）
    if not pid:
        store.set_pid(port, 0)
        return {"ok": False, "error": f"未找到监听 {port} 的进程（可能已停止）"}
    try:
        if os.name == "nt":
            r = subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                               capture_output=True, text=True, timeout=15,
                               stdin=subprocess.DEVNULL)
            ok = r.returncode == 0
            msg = (r.stdout or r.stderr or "").strip()[:200]
        else:
            os.killpg(os.getpgid(pid), 15)
            ok, msg = True, f"SIGTERM → 组 {os.getpgid(pid)}"
    except ProcessLookupError:
        ok, msg = True, "进程已不存在"
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    store.set_pid(port, 0)
    return {"ok": ok, "pid": pid, "msg": msg[:200]}


def cpolar_domain() -> str:
    """扫 ~/.cpolar/logs 里最新的 *.cpolar.top 域名（公网隧道地址推断用）。
    无目录/无命中返回空串。"""
    logs = Path.home() / ".cpolar" / "logs"
    if not logs.is_dir():
        return ""
    best, best_t = "", 0.0
    for f in logs.glob("*"):
        try:
            if not f.is_file():
                continue
            mtime = f.stat().st_mtime
            text = f.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        if mtime < best_t:
            continue
        doms = re.findall(r"[A-Za-z0-9][A-Za-z0-9-]*\.cpolar\.(?:top|io|cn)", text)
        if doms:
            best, best_t = doms[-1], mtime
    return best


def discover_local(dash_port: int = DEFAULT_PORT) -> list[dict]:
    """自动发现本机 agt 实例：netstat LISTEN → 逐候选 POST /api/status，
    应答含 session_name 即判定 agt（agent_watch 同款，Windows 优先）。
    排除已知非 agt 端口与 dashboard 自身端口。返回候选列表
    [{port,url,session_name,model,ready}]。诊断信息存 DISCOVER_DIAG（/api/discover 带 _diag）。"""
    ports: set[int] = set()
    DISCOVER_DIAG.update(err="", lines=0, listening=0, probed=0)
    try:
        out = subprocess.run(["netstat", "-ano"] if os.name == "nt"
                             else ["ss", "-tln"], capture_output=True, text=True,
                             timeout=15, stdin=subprocess.DEVNULL).stdout or ""
    except Exception as e:
        DISCOVER_DIAG["err"] = f"{type(e).__name__}: {e}"
        return []
    DISCOVER_DIAG["lines"] = len(out.splitlines())
    for line in out.splitlines():
        parts = line.split()
        if os.name == "nt":
            if len(parts) >= 5 and parts[0].upper() == "TCP" \
                    and parts[3].upper() == "LISTENING":
                DISCOVER_DIAG["listening"] += 1
                try:
                    ports.add(int(parts[1].rsplit(":", 1)[-1]))
                except ValueError:
                    pass
        else:
            m = re.search(r":(\d+)\s", line)
            if m and ("LISTEN" in line):
                ports.add(int(m.group(1)))
    skip = set(EXCLUDE_PORTS) | {int(dash_port)}
    found = []
    for p in sorted(ports - skip):
        if p < 1024 or p > 65535:
            continue
        # 2.5s：刚跑完一轮对话的实例事件循环可能被落盘短暂阻塞（冒烟实证），放宽一点
        DISCOVER_DIAG["probed"] += 1
        st = probe_instance({"url": f"http://127.0.0.1:{p}"}, timeout=2.5)
        if st.get("alive") and st.get("session_name"):
            found.append({
                "port": p, "url": f"http://127.0.0.1:{p}",
                "session_name": st.get("session_name") or "",
                "model": st.get("model") or "", "ready": bool(st.get("ready")),
            })
    return found


# ========================================================== Step3 组网与拓扑

def _entry_url(entry: dict) -> str:
    """实例的对外 url：remote 用 public_url 优先（跨机访问要隧道地址）。"""
    return (entry.get("public_url") or entry.get("url") or "").rstrip("/")


def wire(a_id: str, b_id: str, store: DashboardStore) -> dict:
    """拉网线 A→B：对 A POST /api/remote/add {server_id:B.name, url:B对外url}。
    A 侧 remote_tools.connect 探测成功才入表——完全复用现有端点，零改动。"""
    a, b = store.get(a_id), store.get(b_id)
    if not a or not b:
        return {"ok": False, "error": "实例不存在"}
    b_url = _entry_url(b)
    a_url = (a.get("url") or "").rstrip("/")
    if not a_url or not b_url:
        return {"ok": False, "error": "A 或 B 缺 url"}
    try:
        try:
            code, data = _http("POST", a_url + "/api/remote/add",
                               {"server_id": b.get("name"), "url": b_url},
                               token=a.get("token") or "")
        except Exception as e:
            if _token_retry(e) and a.get("token"):
                code, data = _http("POST", a_url + "/api/remote/add",
                                   {"server_id": b.get("name"), "url": b_url},
                                   token=a["token"])
            else:
                raise
        if isinstance(data, dict) and data.get("ok"):
            return {"ok": True, "msg": data.get("msg") or f"{a['name']} → {b['name']} 已连线"}
        return {"ok": False, "error": str((data or {}).get("error") or data)[:200]}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def unwire(a_id: str, b_id: str, store: DashboardStore) -> dict:
    """拆线 A→B：对 A POST /api/remote/remove {server_id:B.name}。"""
    a, b = store.get(a_id), store.get(b_id)
    if not a or not b:
        return {"ok": False, "error": "实例不存在"}
    a_url = (a.get("url") or "").rstrip("/")
    try:
        code, data = _http("POST", a_url + "/api/remote/remove",
                           {"server_id": b.get("name")}, token=a.get("token") or "")
        if isinstance(data, dict) and data.get("ok"):
            return {"ok": True, "msg": f"{a['name']} ✕ {b['name']} 已拆线"}
        return {"ok": False, "error": str((data or {}).get("error") or data)[:200]}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def topology(store: DashboardStore, states: dict[str, dict]) -> dict[str, list[str]]:
    """拓扑邻接表：逐在线实例 GET /api/dash 提 remotes[].server_id
    → {实例id: [它认识的实例名, ...]}（有向）。挂了/旧版的实例不给条目。"""
    adj: dict[str, list[str]] = {}
    for e in store.list():
        st = states.get(e.get("id") or "") or {}
        if not st.get("alive"):
            continue
        url = (e.get("url") or "").rstrip("/")
        try:
            code, data = _http("GET", url + "/api/dash", token=e.get("token") or "")
            remotes = [r.get("server_id") for r in ((data or {}).get("remotes") or [])
                       if isinstance(r, dict) and r.get("server_id")]
            if remotes:
                adj[e["id"]] = remotes
        except Exception:
            continue
    return adj


# ====================================================== Step4 InteractionHub

class InteractionHub:
    """互动群聊采集：对每个已注册实例维持一条 WS（/ws），旁听广播事件流。
    过滤 type=tool_call 且 name∈WATCHED_TOOLS（发送方视角，参数带完整 to+text）
    → {ts,from,to,kind,text} 记入内存环（deque 2000）+ 追加落盘 jsonl（重启不丢）。"""

    def __init__(self, path: Path = INTERACTIONS_PATH):
        self.path = Path(path)
        self.records: deque = deque(maxlen=RING_SIZE)
        self._seen: set = set()          # 指纹去重（回填 vs 实时可能重叠）
        self._lock = threading.Lock()
        self._threads: dict[str, threading.Thread] = {}
        self._stop_flags: dict[str, threading.Event] = {}
        self.dirty = False               # 新记录置脏，供水轮询快速跳过
        self._load_seen()

    # —— 指纹：回填与实时可能交叠，重复事件只记一笔 ——
    @staticmethod
    def _fp(rec: dict) -> tuple:
        return (round(float(rec.get("ts") or 0), 1), rec.get("from") or "",
                rec.get("to") or "", rec.get("kind") or "",
                str(rec.get("text") or "")[:100])

    def _load_seen(self) -> None:
        """启动时读 jsonl 尾部（≤RING_SIZE 行）重建指纹集合——回填幂等。"""
        if not self.path.exists():
            return
        try:
            lines = self.path.read_text(encoding="utf-8", errors="replace").splitlines()
            for ln in lines[-RING_SIZE:]:
                try:
                    self._seen.add(self._fp(json.loads(ln)))
                except Exception:
                    continue
        except Exception as e:
            print(f"⚠️ dashboard_interactions.jsonl 读取失败：{e}")

    def record(self, rec: dict) -> bool:
        """登记一条互动（指纹去重）；写盘失败不影响内存。"""
        fp = self._fp(rec)
        with self._lock:
            if fp in self._seen:
                return False
            self._seen.add(fp)
            self.records.append(dict(rec))
            self.dirty = True
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except Exception as e:
            print(f"⚠️ 互动流落盘失败：{e}")
        return True

    def backfill(self, entry_name: str, items: list[dict]) -> int:
        """历史回填（Step10 端点，404 旧版静默）：items → record（ts 缺失给 0）。"""
        n = 0
        for it in items or []:
            rec = {"ts": float(it.get("ts") or 0),
                   "from": it.get("from") or entry_name,
                   "to": it.get("to") or "", "kind": it.get("to_kind") or "?",
                   "text": str(it.get("text") or "")[:TEXT_MAX],
                   "hist": True}
            if self.record(rec):
                n += 1
        return n

    def query(self, since: float = 0.0, sender: str = "", to: str = "",
              limit: int = 200) -> list[dict]:
        """since 之后的记录按时间正序返回（ts=0 的历史回填条目恒最先给齐）。"""
        with self._lock:
            items = [dict(r) for r in self.records
                     if (float(r.get("ts") or 0) > since or float(r.get("ts") or 0) == 0.0)
                     and (not sender or r.get("from") == sender)
                     and (not to or r.get("to") == to)]
        items.sort(key=lambda r: float(r.get("ts") or 0))
        return items[-int(limit):] if limit else items

    # —— WS 监听线程 ——
    def attach(self, entry: dict) -> None:
        """为一个实例启动 WS 旁听线程（幂等：已在线则跳过）。"""
        eid = entry.get("id") or ""
        if not eid or (eid in self._threads and self._threads[eid].is_alive()):
            return
        stop = threading.Event()
        self._stop_flags[eid] = stop
        t = threading.Thread(target=self._run, args=(entry, stop),
                             daemon=True, name=f"hub-{entry.get('name')}")
        self._threads[eid] = t
        t.start()

    def detach(self, entry_id: str) -> None:
        stop = self._stop_flags.pop(entry_id, None)
        if stop:
            stop.set()
        self._threads.pop(entry_id, None)

    @property
    def listening(self) -> list[str]:
        return [eid for eid, t in self._threads.items() if t.is_alive()]

    @staticmethod
    def _ws_url(entry: dict) -> str:
        url = _entry_url(entry)
        if url.startswith("https://"):
            return "wss://" + url[len("https://"):] + "/ws"
        if url.startswith("http://"):
            return "ws://" + url[len("http://"):] + "/ws"
        return "ws://" + url + "/ws"

    def _run(self, entry: dict, stop: threading.Event) -> None:
        """断线 10s 重连的 WS 旁听循环（daemon）。
        recv 30s 静默属正常（旁听者无话可说）——ping 探活后继续，不当断线处理
        （冒烟实证：短超时会把「静默」误判成「断开」，占空比仅 1/3，漏采事件）。"""
        ws_url = self._ws_url(entry)
        try:
            import websocket   # websocket-client
        except ImportError:
            print("⚠️ 未安装 websocket-client（pip install websocket-client）——互动流采集停用")
            return
        while not stop.is_set():
            ws = None
            try:
                ws = websocket.create_connection(ws_url, timeout=5)   # 连接超时
                ws.settimeout(30)                                     # recv 静默容忍
                while not stop.is_set():
                    try:
                        raw = ws.recv()
                    except websocket.WebSocketTimeoutException:
                        try:
                            ws.ping()      # 探活：对端死连接这里会抛
                            continue
                        except Exception:
                            break
                    except Exception:
                        break     # 断开：内层退出走重连
                    if isinstance(raw, bytes):
                        raw = raw.decode("utf-8", "replace")
                    self._on_message(entry, raw)
            except Exception:
                pass
            finally:
                try:
                    if ws is not None:
                        ws.close()
                except Exception:
                    pass
            stop.wait(HUB_RECONNECT)

    def _on_message(self, entry: dict, raw: str) -> None:
        """事件过滤与映射：tool_call(remote_*/notify/prompt) → 群聊记录。"""
        try:
            ev = json.loads(raw)
        except Exception:
            return
        if not isinstance(ev, dict) or ev.get("type") != "tool_call":
            return
        name = str(ev.get("name") or "")
        if name not in WATCHED_TOOLS:
            return
        args = ev.get("arguments") or {}
        if not isinstance(args, dict):
            args = {}
        to = (args.get("remote_instance_id") or args.get("target_id")
              or args.get("name") or "")
        text = (args.get("message") or args.get("question")
                or args.get("prompt") or "")
        if not text and name == "remote_call_tool":
            text = f"{args.get('name','')} {json.dumps(args.get('arguments') or {}, ensure_ascii=False)}"
        self.record({
            "ts": _now(),
            "from": entry.get("name") or entry.get("id") or "?",
            "to": str(to)[:120],
            "kind": name,
            "text": str(text)[:TEXT_MAX],
        })


# ===================================================== Step5 FastAPI + serve

class DashboardRuntime:
    """运行时聚合体：store + hub + probe 状态缓存 + 后台轮询线程。
    GET /api/state 一次读齐（探测在后台做，请求零阻塞）。"""

    def __init__(self, dash_port: int = DEFAULT_PORT,
                 store_path: Optional[Path] = None,
                 hub_path: Optional[Path] = None):
        self.dash_port = dash_port
        self.store = DashboardStore(store_path or REG_PATH)
        self.hub = InteractionHub(hub_path or INTERACTIONS_PATH)
        self.states: dict[str, dict] = {}      # id → probe dict（后台线程写，请求线程只读）
        self.adjacency: dict[str, list[str]] = {}
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # —— probe 轮询 ——
    def start_probe_loop(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._probe_loop, daemon=True,
                                        name="dash-probe")
        self._thread.start()

    def _probe_loop(self) -> None:
        n = 0
        while not self._stop.is_set():
            for e in self.store.list():
                eid = e.get("id") or ""
                self.states[eid] = probe_instance(e)
                st = self.states[eid]
                # 实例在线 → hub 旁听线程保活（增删实例时随注册表启停）
                if st.get("alive"):
                    self.hub.attach(e)
            n += 1
            if n % TOPO_EVERY == 0:
                try:
                    self.adjacency = topology(self.store, self.states)
                except Exception:
                    pass
            self._stop.wait(PROBE_INTERVAL)

    def state_payload(self) -> dict:
        instances = []
        for e in self.store.list():
            it = dict(e)
            it["probe"] = dict(self.states.get(e.get("id") or "") or {"alive": False})
            wired = list(self.adjacency.get(e.get("id") or "") or [])
            it["wired"] = wired
            it["cpolar_public"] = _cpolar_public_url(e)
            instances.append(it)
        return {"ok": True, "now": _now(), "port": self.dash_port,
                "instances": instances,
                "hub": {"listening": self.hub.listening,
                        "records": len(self.hub.records)}}


def _cpolar_public_url(entry: dict) -> str:
    """公网 url：注册表里显式填的 public_url 优先；本地实例没填则按 cpolar 域名推断。"""
    if entry.get("public_url"):
        return entry["public_url"]
    if entry.get("kind") == "local" and entry.get("port"):
        dom = cpolar_domain()
        if dom:
            return f"https://{dom}/port-{entry['port']}"
    return ""


def create_app(dash_port: int = DEFAULT_PORT,
               store_path: Optional[Path] = None,
               hub_path: Optional[Path] = None):
    """FastAPI app（独立于 server.py——dashboard 是主进程）。"""
    from fastapi import FastAPI
    from fastapi.responses import FileResponse, JSONResponse

    rt = DashboardRuntime(dash_port, store_path=store_path, hub_path=hub_path)
    static_dir = Path(__file__).resolve().parent / "static"
    app = FastAPI(title="agt dashboard", docs_url=None, redoc_url=None)
    app.state.dash = rt

    @app.get("/")
    def index():
        return FileResponse(static_dir / "dashboard.html")

    @app.get("/api/state")
    def api_state():
        return rt.state_payload()

    # —— 实例 CRUD ——
    @app.get("/api/instances")
    def api_instances():
        return {"ok": True, "instances": rt.store.list()}

    @app.post("/api/instances")
    def api_instance_add(body: dict):
        body = body or {}
        name = str(body.get("name") or "").strip()
        kind = str(body.get("kind") or "local")
        if kind not in ("local", "remote"):
            return {"ok": False, "error": "kind ∈ local|remote"}
        if not name:
            name = f"agt-{body.get('port') or _short_id()}"
        url = str(body.get("url") or "").strip()
        port = int(body.get("port") or 0) or None
        if not url and kind == "local" and port:
            url = f"http://127.0.0.1:{port}"   # 本地实例默认 url（wire/topology 依赖）
        entry = rt.store.add({
            "id": _short_id(), "name": name, "kind": kind,
            "url": url,
            "public_url": str(body.get("public_url") or "").strip(),
            "repo": str(body.get("repo") or "").strip(),
            "port": port,
            "start_args": str(body.get("start_args") or "").strip(),
            "token": str(body.get("token") or "").strip(),
            "note": str(body.get("note") or "").strip(),
        })
        return {"ok": True, "entry": entry}   # probe loop 下一轮自然接管

    @app.put("/api/instances/{entry_id}")
    def api_instance_put(entry_id: str, body: dict):
        patch = {k: body.get(k) for k in
                 ("name", "kind", "url", "public_url", "repo", "port",
                  "start_args", "token", "note") if k in (body or {})}
        e = rt.store.update(entry_id, patch)
        return {"ok": e is not None, "entry": e}

    @app.delete("/api/instances/{entry_id}")
    def api_instance_del(entry_id: str):
        rt.hub.detach(entry_id)
        rt.states.pop(entry_id, None)
        rt.adjacency.pop(entry_id, None)
        return {"ok": rt.store.remove(entry_id)}

    # —— 启停 ——
    @app.post("/api/start")
    def api_start(body: dict):
        e = rt.store.get(str((body or {}).get("id") or ""))
        if not e:
            return {"ok": False, "error": "实例不存在"}
        return start_instance(e, rt.store,
                              probe=rt.states.setdefault(e["id"], {}))

    @app.post("/api/stop")
    def api_stop(body: dict):
        e = rt.store.get(str((body or {}).get("id") or ""))
        if not e:
            return {"ok": False, "error": "实例不存在"}
        r = stop_instance(e, rt.store)
        if rt.states.get(e["id"]) is not None:
            rt.states[e["id"]].update(alive=False, error="stopped")
        return r

    # —— 组网 ——
    @app.post("/api/wire")
    def api_wire(body: dict):
        b = body or {}
        return wire(str(b.get("a") or ""), str(b.get("b") or ""), rt.store)

    @app.post("/api/unwire")
    def api_unwire(body: dict):
        b = body or {}
        return unwire(str(b.get("a") or ""), str(b.get("b") or ""), rt.store)

    # —— 发现 ——
    @app.post("/api/discover")
    def api_discover():
        found = discover_local(rt.dash_port)
        known = {e.get("url", "").rstrip("/") for e in rt.store.list()}
        dom = cpolar_domain()
        out = []
        for f in found:
            if f["url"] in known:
                continue
            f["public_url"] = f"https://{dom}/port-{f['port']}" if dom else ""
            out.append(f)
        return {"ok": True, "cpolar_domain": dom, "candidates": out,
                "_diag": dict(DISCOVER_DIAG)}

    # —— 互动群聊流 ——
    @app.get("/api/interactions")
    def api_interactions(since: float = 0.0, sender: str = "", to: str = "",
                         limit: int = 200):
        items = rt.hub.query(since=since, sender=sender, to=to, limit=limit)
        with rt.hub._lock:
            rt.hub.dirty = False
        return {"ok": True, "items": items, "now": _now()}

    @app.exception_handler(Exception)
    async def _unhandled(request, exc):   # 兜底：别让单点异常炸掉轮询前端
        return JSONResponse({"ok": False, "error": f"{type(exc).__name__}: {exc}"},
                            status_code=500)

    return app, rt


def _backfill_once(rt: DashboardRuntime) -> None:
    """启动时对在线实例拉一次互动历史（Step10 端点；404/旧版静默降级）。"""
    for e in rt.store.list():
        url = (e.get("url") or "").rstrip("/")
        if not url:
            continue
        try:
            code, data = _http("GET", f"{url}/api/interactions?limit=500",
                               token=e.get("token") or "", timeout=4.0)
            items = (data or {}).get("items") if isinstance(data, dict) else None
            if items:
                n = rt.hub.backfill(e.get("name") or "", items)
                if n:
                    print(f"↩ 历史回填 {e.get('name')}: {n} 条互动")
        except Exception:
            continue   # 旧版实例无此端点——静默（只从启动时刻开始积累）


def serve(port: int = DEFAULT_PORT, open_browser: bool = True) -> None:
    """dashboard 主入口（agt-web --dashboard 分流至此）：uvicorn 前台跑。"""
    import uvicorn
    print("=" * 64)
    print(f"📊 agt dashboard —— 多实例统一看板  http://127.0.0.1:{port}")
    print(f"   注册表 {REG_PATH}")
    print(f"   互动流 {INTERACTIONS_PATH}")
    print("=" * 64)
    app, rt = create_app(port)
    rt.start_probe_loop()
    threading.Thread(target=_backfill_once, args=(rt,), daemon=True,
                     name="dash-backfill").start()
    if open_browser:
        threading.Timer(1.2, lambda: webbrowser.open(f"http://127.0.0.1:{port}")).start()
    try:
        uvicorn.run(app, host="0.0.0.0", port=port, log_level="warning")
    except KeyboardInterrupt:
        return   # Ctrl+C：正常退出
    except Exception as e:
        print(f"❌ dashboard 异常退出：{type(e).__name__}: {e}")
        sys.exit(1)


if __name__ == "__main__":
    p = DEFAULT_PORT
    for a in sys.argv[1:]:
        if a.isdigit():
            p = int(a)
    serve(port=p)
