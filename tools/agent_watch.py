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

# 监视清单（public 留空 = 无公网隧道）
WATCHES = [
    {"name": "main-9000", "note": "自我迭代（本机主 Agent）", "url": "http://127.0.0.1:9000",
     "public": "（cpolar agt 隧道已写入配置，重启 cpolar 服务后生效）"},
    {"name": "agt-8000", "note": "多媒体专家", "url": "http://127.0.0.1:8000", "public": ""},
    {"name": "claw-50051", "note": "ClawTasks 备用实例", "url": "http://127.0.0.1:50051", "public": ""},
    {"name": "brick", "note": "Brick Studio（CNB 云容器 #13789）", "url": "https://adk2zs60ym-8000.cnb.run",
     "public": "https://adk2zs60ym-8000.cnb.run",
     "token": "2bc435c58e08fdb3013ff84569a78659"},   # /api/status 若 401 时带 X-Cb-Token 重试
]
# ═══════════════ /CONFIG ═══════════════


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
    for w in WATCHES:
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
    save_state(new_state)
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
    print(f"[agent_watch] 常驻启动 · 每 {INTERVAL}s 轮询 · 监视 {len(WATCHES)} 个实例")
    while True:
        try:
            run_once()
        except Exception as e:
            print(f"[loop] 异常: {type(e).__name__} {e}", file=sys.stderr)
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
