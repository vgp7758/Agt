# -*- coding: utf-8 -*-
"""dashboard 冒烟·驱动器（spec s_95425415 Step10 端到端）：

等 3 进程就绪 → WS 打招呼（触发 session 命名）→ /api/discover 发现 18001/18002
→ 导入注册表 → /api/wire 拉线 A→B → WS 驱动 A 调 remote_message 给 B
→ 轮询 dashboard /api/interactions 验证群聊记录 → /api/stop + /api/start 启停验证
→ 结果写 smoke/smoke_result.json。
"""
import json
import socket
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import websocket  # websocket-client

sys.stdout.reconfigure(encoding="utf-8")
SMOKE = Path(__file__).resolve().parent
DASH = "http://127.0.0.1:19100"
A_PORT, B_PORT = 18001, 18002
R = {"steps": {}, "ok": True}


def step(name, ok, detail=""):
    R["steps"][name] = {"ok": bool(ok), "detail": str(detail)[:400]}
    R["ok"] = R["ok"] and bool(ok)
    print(f"[{'✅' if ok else '❌'}] {name} {str(detail)[:200]}")


def http(method, url, body=None, timeout=10):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        return {"_http": e.code, "body": e.read().decode("utf-8", "replace")[:300]}
    except Exception as e:
        return {"_err": f"{type(e).__name__}: {e}"}


def port_dead(port) -> bool:
    s = socket.socket()
    s.settimeout(1.0)
    try:
        s.connect(("127.0.0.1", port))
        return False
    except OSError:
        return True
    finally:
        s.close()


def wait_ready(port, deadline_s=240):
    t0, last = time.time(), {}
    while time.time() - t0 < deadline_s:
        last = http("POST", f"http://127.0.0.1:{port}/api/status", {}, timeout=6)
        if last.get("ready"):
            return last
        time.sleep(2.5)
    return last


def ws_talk(port, message, timeout=180):
    """WS 发消息，收事件直到 answer；返回 (answer, tool_names)。"""
    ws = websocket.create_connection(f"ws://127.0.0.1:{port}/ws", timeout=15)
    ws.send(message)
    answer, tools, t0 = "", [], time.time()
    try:
        while time.time() - t0 < timeout:
            try:
                raw = ws.recv()
            except (websocket.WebSocketTimeoutException, socket.timeout):
                ws.ping() if hasattr(ws, "ping") else None
                continue
            if not raw:
                break
            try:
                ev = json.loads(raw)
            except Exception:
                continue
            t = ev.get("type")
            if t == "tool_call":
                tools.append(ev.get("name"))
            if t in ("answer", "wrap_answer"):
                answer = str(ev.get("text") or ev.get("answer") or "")
                break
    finally:
        try:
            ws.close()
        except Exception:
            pass
    return answer, tools


def main():
    # 1) 三进程就绪
    st_d = {}
    for _ in range(40):
        st_d = http("GET", DASH + "/api/state", timeout=5)
        if st_d.get("ok"):
            break
        time.sleep(1.5)
    step("dashboard_ready", st_d.get("ok"), st_d)

    st_a = wait_ready(A_PORT)
    step("instA_ready", bool(st_a.get("ready")), {k: st_a.get(k) for k in ("model", "session_name")})
    st_b = wait_ready(B_PORT)
    step("instB_ready", bool(st_b.get("ready")), {k: st_b.get(k) for k in ("model", "session_name")})

    # 2) 打招呼触发命名（各自一条廉价 LLM 轮）
    ans_a, _ = ws_talk(A_PORT, "请只回复两个字：就绪")
    step("instA_greet", bool(ans_a), ans_a[:80])
    ans_b, _ = ws_talk(B_PORT, "请只回复两个字：就绪")
    step("instB_greet", bool(ans_b), ans_b[:80])

    a_name = http("POST", f"http://127.0.0.1:{A_PORT}/api/status", {}).get("session_name") or ""
    b_name = http("POST", f"http://127.0.0.1:{B_PORT}/api/status", {}).get("session_name") or ""
    step("sessions_named", a_name and b_name and a_name != b_name, f"A={a_name!r} B={b_name!r}")

    # 3) dashboard 发现（重试 40s；_diag 打印 netstat/探测诊断——冒烟期偶发空结果定位用）
    import urllib.parse
    disc, found = {}, {}
    t0 = time.time()
    while time.time() - t0 < 40:
        disc = http("POST", DASH + "/api/discover", {}, timeout=30)
        found = {c.get("port"): c for c in (disc.get("candidates") or [])}
        if A_PORT in found and B_PORT in found:
            break
        print("   discover retry:", disc.get("_diag"), "found:", sorted(found))
        time.sleep(3)
    step("discover", A_PORT in found and B_PORT in found,
         f"ports={sorted(found)} diag={disc.get('_diag')} cpolar={disc.get('cpolar_domain')!r}")

    # 4) 导入（发现→导入）
    id_a = id_b = ""
    for port, ws_dir, name in ((A_PORT, "ws_a", a_name), (B_PORT, "ws_b", b_name)):
        c = found.get(port) or {}
        r = http("POST", DASH + "/api/instances", {
            "kind": "local", "name": name, "port": port,
            "url": f"http://127.0.0.1:{port}",
            "repo": str(SMOKE / ws_dir), "note": "smoke"})
        eid = ((r.get("entry") or {}).get("id")) or ""
        step(f"import_{port}", bool(r.get("ok") and eid), r)
        if port == A_PORT:
            id_a = eid
        else:
            id_b = eid

    # 5) probe loop + hub attach（等两轮探活）
    time.sleep(7)
    state = http("GET", DASH + "/api/state")
    insts = {i.get("id"): i for i in (state.get("instances") or [])}
    alive = {k: (v.get("probe") or {}).get("alive") for k, v in insts.items()}
    hub_n = (state.get("hub") or {}).get("listening") or []
    step("probe_alive", alive.get(id_a) and alive.get(id_b), f"alive={alive} hub_listening={hub_n}")
    step("hub_attached", len(hub_n) >= 2, f"listening={hub_n}")

    # 6) 拉线 A→B（复用 /api/remote/add）
    wire = http("POST", DASH + "/api/wire", {"a": id_a, "b": id_b})
    step("wire", bool(wire.get("ok")), wire)
    dash_a = http("GET", f"http://127.0.0.1:{A_PORT}/api/dash", timeout=8)
    remotes = [r.get("server_id") for r in ((dash_a or {}).get("remotes") or [])]
    step("wire_visible_on_A", b_name in remotes, f"A.remotes={remotes}")
    # 拓扑邻接由 probe loop 每 TOPO_EVERY 轮(6s)刷一次 → 重试等 15s
    topo = {}
    t0 = time.time()
    while time.time() - t0 < 15:
        topo = {i.get("id"): i.get("wired")
                for i in (http("GET", DASH + "/api/state").get("instances") or [])}
        if b_name in (topo.get(id_a) or []):
            break
        time.sleep(2)
    step("topology_adjacency", b_name in (topo.get(id_a) or []), f"adj={topo}")

    # 7) 驱动 A 调 remote_message 给 B（一轮廉价 LLM）
    drive = (f'请立即调用工具 remote_message（参数：remote_instance_id="{b_name}"，'
             f'message="hello-from-A-smoke"）。只调用这一次工具，完成后回复：已发送。')
    ans, tools = ws_talk(A_PORT, drive, timeout=180)
    step("drive_A_remote_message", "remote_message" in tools, f"tools={tools} answer={ans[:80]}")

    # 8) dashboard 互动流应出现 A→B 记录
    hit = None
    t0 = time.time()
    while time.time() - t0 < 30:
        ii = http("GET", DASH + "/api/interactions?since=0&limit=300")
        for it in (ii.get("items") or []):
            if (it.get("from") == a_name and it.get("kind") == "remote_message"
                    and b_name in str(it.get("to"))):
                hit = it
                break
        if hit:
            break
        time.sleep(2)
    step("interaction_captured", hit is not None,
         hit or "30s 内未见 A→B 的 remote_message 记录")
    q = urllib.parse.quote(a_name)
    ii2 = http("GET", DASH + f"/api/interactions?since=0&sender={q}&limit=300")
    items2 = ii2.get("items") or []
    step("interaction_filter", len(items2) >= 1 and all(it.get("from") == a_name for it in items2),
         f"filtered_n={len(items2)}")

    # 9) 拆线验证（趁 A 还活着：A 重启后组网表随 session 清空——重启前先验 unwire）
    unwire = http("POST", DASH + "/api/unwire", {"a": id_a, "b": id_b})
    step("unwire", bool(unwire.get("ok")), unwire)

    # 10) 启停验证（stop → 端口死 → start → 重新就绪）
    stp = http("POST", DASH + "/api/stop", {"id": id_a})
    time.sleep(2)
    dead = port_dead(A_PORT)
    step("stop_instance", bool(stp.get("ok")) and dead, f"{stp} port_dead={dead}")

    sta = http("POST", DASH + "/api/start", {"id": id_a})
    step("start_accepted", bool(sta.get("ok")), sta)
    st2 = wait_ready(A_PORT, deadline_s=150)
    step("start_ready_again", bool(st2.get("ready")),
         {k: st2.get(k) for k in ("model", "session_name", "error")})

    Path(SMOKE / "smoke_result.json").write_text(
        json.dumps(R, ensure_ascii=False, indent=1), encoding="utf-8")
    print("\n==== SMOKE", "PASS ✅" if R["ok"] else "FAIL ❌", "====")
    for k, v in R["steps"].items():
        print(f"  {'✅' if v['ok'] else '❌'} {k}: {v['detail'][:120]}")


if __name__ == "__main__":
    main()
