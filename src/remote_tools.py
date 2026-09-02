# -*- coding: utf-8 -*-
"""remote_tools.py —— 多 agt 实例组网（server_id 工具路由）。

模型在任意工具调用的 arguments 里带 "server_id" 字段 → agent 工具循环 pop 出来
→ route_remote_call POST 到远程实例的 /api/tool/exec → 远程工具箱同步执行
→ 结果（前缀 [remote:id]）作为 tool result 回本地上下文。

连接管理（remote_connect/disconnect/list）+ settings.json remote_servers 持久化
（启动自动重连，失败标 offline 不炸启动）。

设计要点：
  - server_id 放 arguments 而非 tool_calls 顶层：顶层结构由 provider 解析，自定义字段被丢弃；
    arguments 是模型自由生成的 JSON，任何字段保真传输。
  - 工具级直执行（远程不跑 LLM、不进对方 session）——远程实例只当"手"用；
    与 WS 消息驱动（"脑"，带对方 session 上下文）互补。
  - file_version 跨实例语义：同一远程文件的连续 edit 必须持续带同一 server_id
    （远程乐观锁才对得上）——由 main.yml 的 {func:load_remote_instances()} 注入文案引导。
"""
from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request

from tools import Tool

_LOCK = threading.RLock()
REMOTE_SERVERS: dict[str, dict] = {}   # server_id → {url, status, tools_count, session_name, model, checked_at}
EXEC_TIMEOUT = 180                      # 工具直执行 HTTP 超时（远程可能跑长任务）
PROBE_TIMEOUT = 5                       # 连接探测超时


# ===================== 持久化（settings.json remote_servers） =====================

def _load_persisted() -> dict[str, str]:
    try:
        import config
        st = config.load_runtime_settings() or {}
        rs = st.get("remote_servers") or {}
        return {str(k): str(v) for k, v in rs.items() if k and v}
    except Exception:
        return {}


def _save_persisted(servers: dict[str, str]):
    try:
        import config
        st = config.load_runtime_settings() or {}
        st["remote_servers"] = servers
        config.save_runtime_settings(st)
    except Exception:
        pass


def _persist_current():
    """把 REMOTE_SERVERS 里 online 的连接落盘（offline 的保留配置下次再试？——
    连接失败通常是暂时的，保留配置；显式 disconnect 才删）。"""
    with _LOCK:
        servers = {sid: it["url"] for sid, it in REMOTE_SERVERS.items()}
    _save_persisted(servers)


# ===================== 探测与路由 =====================

def _http_json(url: str, payload: dict | None, timeout: float) -> tuple[bool, dict | str]:
    """POST JSON → (ok, parsed)。GET 探测传 payload=None。异常转 (False, 错误文案)。"""
    try:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else b"{}"
        req = urllib.request.Request(url, data=data, method="POST",
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return True, json.loads(r.read().decode("utf-8", errors="ignore"))
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode("utf-8", errors="ignore")[:300]
        except Exception:
            body = ""
        return False, f"HTTP {e.code}: {body or e.reason}"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def probe_server(url: str) -> dict | None:
    """探测远端（POST /api/status）→ 摘要 dict；失败返回 None。"""
    url = (url or "").strip().rstrip("/")
    if not url:
        return None
    if not url.startswith(("http://", "https://")):
        url = "http://" + url
    ok, st = _http_json(f"{url}/api/status", None, PROBE_TIMEOUT)
    if not ok or not isinstance(st, dict) or not st.get("ready"):
        return None
    return {"url": url, "status": "online",
            "tools_count": st.get("tools_count") or 0,
            "session_name": st.get("session_name") or "",
            "model": st.get("model") or "",
            "checked_at": time.time()}


def _auto_server_id(url: str, existing: set) -> str:
    """从 url 推导默认 server_id（remote_connect 未传时的自动生成）。
    127.0.0.1/localhost → agt-{port}（本地隧道场景端口是唯一区分维度——如 SSH 隧道 8300→远端 8000）；
    远程主机 → agt-{host}-{port}（清洗非法字符）。冲突时 -2/-3 递增。"""
    import re as _re
    from urllib.parse import urlparse as _up
    try:
        u = _up(url if "//" in url else "http://" + url)
        host, port = (u.hostname or "remote"), u.port
    except Exception:
        host, port = "remote", None
    if host in ("127.0.0.1", "localhost", "::1", "0.0.0.0"):
        base = f"agt-{port}" if port else "agt-local"
    else:
        h = _re.sub(r"[^0-9a-zA-Z-]+", "-", host).strip("-")
        base = f"agt-{h}-{port}" if port else f"agt-{h}"
    sid, n = base, 2
    while sid in existing:
        sid = f"{base}-{n}"
        n += 1
    return sid


def connect(server_id: str, url: str) -> str:
    """注册一个远程实例（探测成功才入表）。server_id 留空时自动生成（推荐）：
    本地 url → agt-{端口}（隧道场景）；远程 → agt-{host}-{端口}；同 url 重复连接幂等复用。"""
    server_id = (server_id or "").strip()
    info = probe_server(url)
    if info is None:
        return f"[连接失败] {url} 探测无响应或 Agent 未就绪（检查 URL/端口/服务是否在跑）"
    with _LOCK:
        if not server_id:
            # 幂等：同 url 已在表 → 复用其 id（offline 恢复/重复连接场景）
            for k, v in REMOTE_SERVERS.items():
                if v.get("url") == info["url"]:
                    server_id = k
                    break
            if not server_id:
                server_id = _auto_server_id(url, set(REMOTE_SERVERS))
        else:
            # url 与 id 一对一：显式改名时移除同 url 的旧 id 条目（防同 url 双 id 并存混乱）
            for k in [k for k, v in REMOTE_SERVERS.items()
                      if v.get("url") == info["url"] and k != server_id]:
                del REMOTE_SERVERS[k]
        REMOTE_SERVERS[server_id] = info
    _persist_current()
    return (f"✅ 已连接 '{server_id}' → {info['url']}"
            f"（{info['tools_count']} 工具 · session={info['session_name']} · model={info['model']}）。"
            f"现在可在任意工具调用的 arguments 里带 \"server_id\": \"{server_id}\" 路由执行。")


def disconnect(server_id: str) -> str:
    with _LOCK:
        it = REMOTE_SERVERS.pop(server_id, None)
    if it is None:
        return f"[未找到] 没有名为 '{server_id}' 的远程连接（remote_list 查看现有连接）"
    _persist_current()
    return f"✅ 已断开 '{server_id}'（{it['url']}）并从持久化配置移除"


def list_servers() -> str:
    with _LOCK:
        items = list(REMOTE_SERVERS.items())
    if not items:
        return ("当前无远程实例连接。用 remote_connect(server_id, url) 注册——"
                "例如 remote_connect(\"comfy\", \"http://192.168.1.2:8000\")")
    lines = ["已连接的远程 agt 实例（工具调用 arguments 带 server_id=<id> 即路由到该实例）："]
    for sid, it in items:
        age = int(time.time() - (it.get("checked_at") or 0))
        lines.append(f"- {sid}: {it['url']} [{it['status']}] · {it.get('tools_count', '?')} 工具 · "
                     f"session={it.get('session_name', '?')} · model={it.get('model', '?')} · {age}s 前探测")
    return "\n".join(lines)


def route_remote_call(server_id: str, name: str, args: dict) -> str:
    """server_id 路由执行：POST /api/tool/exec → 结果文本（前缀 [remote:id]）。"""
    with _LOCK:
        it = REMOTE_SERVERS.get(server_id)
    if it is None:
        known = ", ".join(sorted(REMOTE_SERVERS)) or "无已连接实例"
        return f"[未知 server_id] '{server_id}'——remote_list 查看已连接实例（{known}）"
    ok, resp = _http_json(f"{it['url']}/api/tool/exec", {"name": name, "arguments": args}, EXEC_TIMEOUT)
    if not ok or not isinstance(resp, dict):
        # 连接失败：标 offline 保留配置（下次 connect 或 reconnect 恢复）
        with _LOCK:
            if server_id in REMOTE_SERVERS:
                REMOTE_SERVERS[server_id]["status"] = "offline"
        return f"[remote:{server_id}] 连接失败：{resp if isinstance(resp, str) else '响应异常'}（已标 offline）"
    if not resp.get("ok"):
        return f"[remote:{server_id}] {resp.get('error') or '执行失败'}"
    return f"[remote:{server_id}] {resp.get('result')}"


def reconnect_all(background: bool = False):
    """启动时自动重连持久化配置（探测更新在线状态；失败标 offline 不炸启动）。"""
    def _do():
        for sid, url in _load_persisted().items():
            info = probe_server(url)
            with _LOCK:
                if info is not None:
                    info_checked = {**info, "checked_at": time.time()}
                    REMOTE_SERVERS[sid] = info_checked
                else:
                    # 保留已知字段，仅标 offline（url 来自持久化）
                    REMOTE_SERVERS.setdefault(sid, {"url": url, "status": "offline", "tools_count": 0,
                                                    "session_name": "", "model": "", "checked_at": 0})
                    REMOTE_SERVERS[sid]["status"] = "offline"
    if background:
        threading.Thread(target=_do, daemon=True).start()
    else:
        _do()


# ===================== 跨实例消息通信（WS 消息级——"脑"通道） =====================
# 与工具级路由（/api/tool/exec，"手"通道）互补：消息发给对方 agent，它带自己的 session
# 上下文跑一轮 LLM 处理（消耗它的 token）。remote_message 异步送达即返；remote_ask
# 挂流等 _done 聚合 answer。

def _ws_endpoint(url: str) -> str:
    """http(s) url → ws(s) 端点（/ws）。"""
    u = url.strip().rstrip("/")
    if u.startswith("https://"):
        u = "wss://" + u[8:]
    elif u.startswith("http://"):
        u = "ws://" + u[7:]
    elif not u.startswith(("ws://", "wss://")):
        u = "ws://" + u
    return u + "/ws"


def _ws_send_collect(url: str, text: str, wait_done: bool, timeout: float,
                     ack_timeout: float = 10.0) -> tuple[str, list[str]]:
    """WS 客户端：发一条消息；wait_done=True 时挂流收 answer 直到 _done（或 timeout）。
    返回 (状态说明, answer 文本段列表)。异常转错误文案不抛。"""
    try:
        import websocket   # websocket-client（run_python 沙箱已验证环境可用）
    except ImportError:
        return "[缺依赖] websocket-client 未安装（pip install websocket-client）", []
    import threading as _th

    answers, state = [], {"ack": False, "done": False, "err": ""}
    done_ev = _th.Event()

    def _on_msg(_ws, raw):
        try:
            m = json.loads(raw)
        except Exception:
            return
        t = m.get("type")
        if t == "user":                      # 对方 session 已记录消息 = 送达回执
            state["ack"] = True
        elif t == "answer":
            answers.append(m.get("text") or m.get("content") or "")
        elif t == "_done":
            state["done"] = True
            try:
                _ws.close()
            except Exception:
                pass
            done_ev.set()
        elif t == "message_queued":          # 对方正忙：消息进了它的插话队列（也算送达）
            state["ack"] = True

    ws_app = websocket.WebSocketApp(
        _ws_endpoint(url), on_message=_on_msg,
        on_error=lambda w, e: (state.update(err=str(e)), done_ev.set()))

    _th.Thread(target=ws_app.run_forever, daemon=True,
               kwargs={"ping_interval": 20}).start()
    # 等连接建立（初始事件到达）——用短轮询近似
    deadline = time.time() + 5
    while time.time() < deadline and not state["err"]:
        try:
            if ws_app.sock and ws_app.sock.connected:
                break
        except Exception:
            pass
        time.sleep(0.1)
    if state["err"] or not (ws_app.sock and ws_app.sock.connected):
        return f"[连接失败] {url}/ws（{state['err'] or '超时'}）", []
    try:
        ws_app.send(json.dumps({"text": text, "images": []}))
    except Exception as e:
        return f"[发送失败] {type(e).__name__}: {e}", []
    if not wait_done:
        # 异步语义：等送达回执（user/message_queued），最多 ack_timeout
        ack_dl = time.time() + ack_timeout
        while time.time() < ack_dl and not state["ack"] and not state["err"]:
            time.sleep(0.1)
        try:
            ws_app.close()
        except Exception:
            pass
        return ("已送达（对方将异步处理）" if state["ack"]
                else "已发送（未收到送达回执——连接已建立，消息应已入队）"), []
    done_ev.wait(timeout=timeout)
    try:
        ws_app.close()
    except Exception:
        pass
    if state["done"] or answers:
        return ("完成" if state["done"] else f"超时（>{int(timeout)}s，收到部分回答）"), answers
    return (f"[无回答] 对方 {int(timeout)}s 内未完成本轮（可能在忙长任务——"
            f"稍后用工具级调用 remote_ask 或看对方 session）" if not state["err"]
            else f"[错误] {state['err']}"), answers


def send_message(server_id: str, message: str) -> str:
    """异步消息：发给对方 agent 后立即返回（fire-and-forget）。它将带着自己的 session
    上下文异步跑一轮处理（消耗它的 LLM）。送达确认 = 收到 user/message_queued 回执。"""
    with _LOCK:
        it = REMOTE_SERVERS.get(server_id)
    if it is None:
        known = ", ".join(sorted(REMOTE_SERVERS)) or "无已连接实例"
        return f"[未知 server_id] '{server_id}'（remote_list 查看：{known}）"
    status, _ = _ws_send_collect(it["url"], message, wait_done=False, timeout=10, ack_timeout=10)
    return f"[remote:{server_id}] {status}：{message[:80]}" + ("…" if len(message) > 80 else "")


def ask(server_id: str, question: str, timeout: int = 120) -> str:
    """同步问答：发消息并挂流等对方跑完本轮，聚合它的最终回答返回。
    消耗对方一次 LLM 调用（带它的全量投影）。适合"问它一个它才知道的问题"。"""
    with _LOCK:
        it = REMOTE_SERVERS.get(server_id)
    if it is None:
        known = ", ".join(sorted(REMOTE_SERVERS)) or "无已连接实例"
        return f"[未知 server_id] '{server_id}'（remote_list 查看：{known}）"
    status, answers = _ws_send_collect(it["url"], question, wait_done=True, timeout=timeout)
    body = "\n".join(a for a in answers if a).strip()
    if body:
        return f"[remote:{server_id}] {body}"
    return f"[remote:{server_id}] {status}"


# ===================== 工具（make_remote_tools） =====================

def make_remote_tools(agent) -> list[Tool]:
    """远程实例管理三件套。agent 参数保留签名一致性（当前不需要 agent 状态）。"""

    def remote_connect(server_id: str = "", url: str = "") -> str:
        """连接一个远程 agt 实例（server_id 路由的注册入口）。url 如 http://192.168.1.2:8000
        （探测 /api/status 成功才注册）。server_id 可省略——自动生成（本地 url→agt-{端口}，
        远程→agt-{host}-{端口}，重复连接同 url 幂等复用），返回消息里带最终 id。
        连接后任意工具调用的 arguments 里带 "server_id": "<id>" 即路由到该实例执行
        （结果前缀 [remote:id]）。配置持久化到 settings.json 的 remote_servers（重启自动重连）。"""
        return connect(server_id, url)

    def remote_disconnect(server_id: str) -> str:
        """断开并移除一个远程实例连接（从持久化配置删除）。"""
        return disconnect(server_id)

    def remote_list() -> str:
        """列出已连接的远程 agt 实例（id/url/状态/工具数/session）。"""
        return list_servers()

    def remote_message(server_id: str, message: str) -> str:
        """向远程实例的 agent 异步发一条消息（fire-and-forget）：送达即返回，它带自己的
        session 上下文异步处理（消耗它的 LLM）。适合通报/派活——如告知框架修复、
        让它开始一个任务。要拿回答用 remote_ask。"""
        return send_message(server_id, message)

    def remote_ask(server_id: str, question: str, timeout: int = 120) -> str:
        """向远程实例的 agent 提问并等待它的最终回答（挂流到本轮完成，默认 120s）。
        消耗对方一次 LLM 调用。适合问"只有它才知道"的事（它的环境/它的进度）。
        对方正忙时消息进它的插话队列，回答可能超时——可加大 timeout 或改用 remote_message。"""
        return ask(server_id, question, timeout)

    return [Tool(remote_connect), Tool(remote_disconnect), Tool(remote_list),
            Tool(remote_message), Tool(remote_ask)]
