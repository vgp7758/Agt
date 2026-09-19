# -*- coding: utf-8 -*-
"""
agent_watch.py —— 本地 Agent 实例状态监视 + 变化邮件通知（2026-09-20·用户提案）

作用：每 15 分钟轮询各实例 /api/status，对比指纹（session/轮数/busy/inbox/存活），
     有变化才发邮件（附局域网 + 公网地址）；过去一轮无任何变化的实例不出现在邮件里，
     全员无变化则整封跳过。首轮只建基线不发信。

用法：
  python agent_watch.py            # 常驻循环（默认 900s）
  python agent_watch.py --once     # 跑一轮退出（测试）
  python agent_watch.py --baseline # 强制重建基线（不发信）

配置都在下方 CONFIG 区；状态文件 ~/.agt/agent_watch_state.json。
"""
from __future__ import annotations
import json
import os
import smtplib
import socket
import ssl
import sys
import time
import datetime
import urllib.request

# ═══════════════ CONFIG ═══════════════
INTERVAL = 900          # 轮询间隔（秒）
STATE_FILE = os.path.expanduser("~/.agt/agent_watch_state.json")

MAIL = {
    "from": "vgp123@foxmail.com",
    "to": "vgp123@foxmail.com",
    "auth": "ovdcofzwccoxcaec",     # QQ 邮箱 SMTP 授权码
    "host": "smtp.qq.com",
    "port": 465,
}

# 本地实例：自动发现（netstat 扫 LISTEN 端口 → /api/status 验证是 agt）——下表仅提供别名/备注
STATIC_META = {
    9000:  {"name": "main-9000", "note": "自我迭代（本机主 Agent）"},
    8000:  {"name": "agt-8000", "note": "多媒体专家"},
    50051: {"name": "claw-50051", "note": "ClawTasks 备用"},
}
# 远程实例（无法自动发现，静态维护）
REMOTE_WATCHES = [
    {"name": "brick", "note": "Brick Studio（CNB 云容器 #13789）", "url": "https://iqhxsci1es-8000.cnb.run",
     "token": "2bc435c58e08fdb3013ff84569a78659"},   # /api/status 若 401 时带 X-Cb-Token 重试
]
# ═══════════════ /CONFIG ═══════════════


def cpolar_domain() -> str:
    """从 cpolar 服务日志提取最新公网域名（用户提示 2026-09-20：域名可从日志读）。
    扫 ~/.cpolar/logs/cpolar_service.log*（mtime 新→旧）；免费版重连会换域名，每轮重扫。"""
    import re as _re
    import glob as _glob
    def _safe_mtime(f):
        try:
            return os.path.getmtime(f)
        except OSError:
            return 0.0
    files = [f for f in _glob.glob(os.path.expanduser("~/.cpolar/logs/cpolar_service.log*")) if os.path.exists(f)]
    for f in sorted(files, key=_safe_mtime, reverse=True):
        try:
            t = open(f, encoding="utf-8", errors="replace").read()
        except Exception:
            continue
        urls = _re.findall(r"https?://([\w.-]+\.cpolar\.(?:top|cn))", t)
        if urls:
            return urls[-1]      # 日志里最后出现≈最新一次建立
    return ""


def local_agt_ports() -> list:
    """自动发现本机 agt-web 实例：netstat LISTEN 端口 → POST /api/status 验证含 session_name。"""
    import subprocess as _sp
    try:
        r = _sp.run("netstat -ano", shell=True, capture_output=True, text=True, timeout=15)
        lines = r.stdout.splitlines()
    except Exception:
        return []
    ports = set()
    for line in lines:
        if "LISTENING" not in line:
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        local = parts[1]
        p = local.rsplit(":", 1)[-1] if ":" in local else ""
        if p.isdigit():
            ports.add(int(p))
    out = []
    for p in sorted(ports):
        if p in (4040, 6060, 9200, 7897):      # cpolar 自身/代理等非 agt
            continue
        try:
            req = urllib.request.Request(f"http://127.0.0.1:{p}/api/status", method="POST", data=b"{}")
            req.add_header("Content-Type", "application/json")
            with urllib.request.urlopen(req, timeout=3) as r2:
                d = json.loads(r2.read().decode())
            if "session_name" in d:
                out.append(p)
        except Exception:
            continue
    return out


def build_watches(prev_state: dict | None = None) -> list:
    """每轮重建监视清单：自动发现的本地实例（附 cpolar /port-N 公网路由）+ 静态远程。
    prev_state 非空时**沿用上轮发现过的端口**——netstat 偶发失败/端口瞬时抖动也不丢实例
    （否则实例"消失一轮又出现"会被误判为「首次纳入（基线）」→ 每轮误发邮件）。
    2026-09-20 用户实锤修复。"""
    import re as _re
    dom = cpolar_domain()
    ports = set(local_agt_ports())
    for k in (prev_state or {}):
        m = _re.match(r"^port-(\d+)$", k)
        if m:
            ports.add(int(m.group(1)))
            continue
        for _p, _meta in STATIC_META.items():
            if _meta["name"] == k:
                ports.add(_p)
    watches = []
    for p in sorted(ports):
        meta = STATIC_META.get(p, {})
        w = {"name": meta.get("name", f"port-{p}"), "note": meta.get("note", "本地 agt 实例（自动发现）"),
             "url": f"http://127.0.0.1:{p}"}
        # cpolar 隧道支持 /port-N 路径路由到本机任意端口（用户提示 2026-09-20，实测 9000/8000 均通）
        w["public"] = f"https://{dom}/port-{p}" if dom else ""
        watches.append(w)
    return watches + REMOTE_WATCHES


def lan_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


def probe(w: dict) -> dict:
    """探一个实例：alive / session_name / session_turns / busy / inbox_size。"""
    out = {"alive": False, "err": ""}
    for use_token in (False, True) if w.get("token") else (False,):
        try:
            req = urllib.request.Request(w["url"].rstrip("/") + "/api/status", method="POST", data=b"{}")
            req.add_header("Content-Type", "application/json")
            if use_token and w.get("token"):
                req.add_header("X-Cb-Token", w["token"])
            with urllib.request.urlopen(req, timeout=8) as r:
                d = json.loads(r.read().decode())
            out.update({
                "alive": True,
                "session": d.get("session_name", "?"),
                "turns": d.get("session_turns"),
                "busy": bool(d.get("busy")),
                "inbox": d.get("inbox_size"),
                "model": d.get("model", ""),
            })
            return out
        except Exception as e:
            out["err"] = f"{type(e).__name__}: {str(e)[:70]}"
    return out


def fingerprint(p: dict) -> tuple:
    return (p.get("alive"), p.get("session"), p.get("turns"), p.get("busy"), p.get("inbox"))


def diff_events(prev: dict | None, cur: dict) -> list[str]:
    """对比上一轮指纹，产出人类可读的变化事件列表。"""
    ev: list[str] = []
    if prev is None:
        return ["👀 首次纳入监视（基线）"]
    if not prev.get("alive") and cur["alive"]:
        ev.append(f"🟢 上线（此前不可达）")
    if prev.get("alive") and not cur["alive"]:
        return [f"🔴 不可达：{cur['err']}"]
    if not cur["alive"]:
        return []   # 持续离线不重复报
    if prev.get("session") != cur.get("session"):
        ev.append(f"🔁 会话切换：{prev.get('session')} → {cur.get('session')}")
    pt, ct = prev.get("turns"), cur.get("turns")
    if isinstance(pt, int) and isinstance(ct, int):
        if ct > pt:
            ev.append(f"💬 新增 {ct - pt} 轮回答（{pt} → {ct}）")
        elif ct < pt:
            ev.append(f"🔁 轮数回退（{pt} → {ct}，可能是重开会话/回溯）")
    if prev.get("busy") != cur.get("busy"):
        ev.append("⏳ 开始忙碌" if cur["busy"] else "✅ 空闲")
    pi, ci = prev.get("inbox"), cur.get("inbox")
    if isinstance(pi, int) and isinstance(ci, int) and ci > pi:
        ev.append(f"📥 inbox +{ci - pi}（有排队消息）")
    return ev


def send_mail(subject: str, body: str) -> bool:
    from email.mime.text import MIMEText
    from email.header import Header
    from email.utils import formataddr
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"] = formataddr((str(Header("Agent Watch", "utf-8")), MAIL["from"]))
    msg["To"] = MAIL["to"]
    try:
        with smtplib.SMTP_SSL(MAIL["host"], MAIL["port"], context=ssl.create_default_context(), timeout=25) as s:
            s.login(MAIL["from"], MAIL["auth"])
            s.sendmail(MAIL["from"], [MAIL["to"]], msg.as_string())
        return True
    except Exception as e:
        print(f"[mail] 发送失败: {type(e).__name__} {e}", file=sys.stderr)
        return False


def load_state() -> dict:
    try:
        return json.load(open(STATE_FILE, encoding="utf-8"))
    except Exception:
        return {}


def save_state(st: dict):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    json.dump(st, open(STATE_FILE, "w", encoding="utf-8"), ensure_ascii=False, indent=1)


def run_once(force_baseline: bool = False) -> bool:
    """跑一轮。返回是否发了邮件。"""
    prev_state = load_state()
    lan = lan_ip()
    sections, new_state = [], {}
    watches = build_watches(prev_state)
    for w in watches:
        cur = probe(w)
        new_state[w["name"]] = cur
        prev = prev_state.get(w["name"])
        ev = [] if force_baseline else diff_events(prev, cur)
        if not ev:
            continue   # 无变化：不出现在邮件里
        # 地址块
        lan_addr = w["url"].replace("127.0.0.1", lan) if "127.0.0.1" in w["url"] else w["url"]
        pub = w.get("public") or "（无公网隧道）"
        lines = [f"### {w['name']} · {w['note']}"]
        for e in ev:
            lines.append(f"- {e}")
        if cur["alive"]:
            lines.append(f"- 状态：session={cur.get('session')} · 轮数 {cur.get('turns')} · "
                         f"{'忙碌中' if cur.get('busy') else '空闲'} · inbox {cur.get('inbox')} · 模型 {cur.get('model')}")
        lines.append(f"- 局域网：{lan_addr}")
        lines.append(f"- 公网：{pub}")
        sections.append("\n".join(lines))
    # ★ 修复（2026-09-20 用户实锤"全实例轮数无变化仍收邮件"）：
    #   自动发现的实例集合每轮可能不同（netstat 偶发失败/端口瞬时抖动），直接覆盖 state 会让
    #   未探到的实例下一轮又成「首次纳入（基线）」→ 每轮误发邮件。
    #   修法：本轮未探到的实例保留上一轮指纹（"暂时没扫到" ≠ "实例下线"）。
    for _k, _v in prev_state.items():
        if _k not in new_state:
            new_state[_k] = _v
    save_state(new_state)
    try:   # 每轮落一行诊断日志（便于排查"为什么发/没发"）
        with open(os.path.expanduser("~/.agt/agent_watch.log"), "a", encoding="utf-8") as _f:
            _f.write(f"[{datetime.datetime.now():%m-%d %H:%M:%S}] 探到 {len(watches)} 个 · state {len(new_state)} 条 · 事件段 {len(sections)}\n")
    except Exception:
        pass
    if not sections:
        print(f"[{datetime.datetime.now():%H:%M}] 无变化，跳过邮件")
        return False
    now = datetime.datetime.now()
    body = (f"【Agent Watch】{now:%Y-%m-%d %H:%M} 检测到 {len(sections)} 个实例有变化\n"
            f"（无变化的实例已省略；局域网 IP {lan}）\n\n" + "\n\n".join(sections) +
            f"\n\n— agent_watch.py · 每 {INTERVAL // 60} 分钟轮询 · 回复本邮件无效")
    ok = send_mail(f"【Agent Watch】{len(sections)} 项变化 · {now:%H:%M}", body)
    print(f"[{now:%H:%M}] {len(sections)} 项变化 → 邮件{'已发' if ok else '发送失败'}")
    return ok


def main():
    force_baseline = "--baseline" in sys.argv
    if "--once" in sys.argv or force_baseline:
        run_once(force_baseline=force_baseline)
        return
    print(f"[agent_watch] 常驻启动 · 每 {INTERVAL}s 轮询 · 本地实例自动发现 + 远程静态 {len(REMOTE_WATCHES)} 个")
    while True:
        try:
            run_once()
        except Exception as e:
            print(f"[loop] 异常: {type(e).__name__} {e}", file=sys.stderr)
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
