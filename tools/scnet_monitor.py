#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""SCNet 容器内 ComfyUI 产物监控 + 主动回调（部署于容器 /root/monitor.py）。

适配平台「访问自定义服务」模型：本进程 listen --port 提供状态页（JSON），
同时后台线程轮询 ComfyUI history，完成的任务自动下载产物并推回调、发通知。

回调端点（agt /api/callback）两种形态：
    type=message → {text, source}       唤醒 Agent
    type=file    → raw body + filename  落盘本机 scnet_inbox/
"""
import argparse
import base64
import json
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

COMFY = "http://127.0.0.1:8190"

STATE = {"boot": time.strftime("%m-%d %H:%M:%S"), "tasks": {}, "callback": "init", "pushed": 0}
_LOCK = threading.Lock()


def _get(path, timeout=60):
    with urllib.request.urlopen(COMFY + path, timeout=timeout) as r:
        return json.loads(r.read().decode())


def _notify(cb, token, text, source="scnet_monitor"):
    q = urllib.parse.urlencode({"token": token})
    req = urllib.request.Request(f"{cb}?{q}", data=json.dumps(
        {"type": "message", "text": text, "source": source}).encode(),
        method="POST", headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.status


def _push_file(cb, token, filename, data):
    q = urllib.parse.urlencode({"token": token, "type": "file", "filename": filename})
    req = urllib.request.Request(f"{cb}?{q}", data=data, method="POST")
    with urllib.request.urlopen(req, timeout=600) as r:
        return r.status


def _collect_files(entry):
    out = []
    for _nid, nd in (entry.get("outputs") or {}).items():
        for kind in ("images", "gifs", "videos"):
            for it in (nd.get(kind) or []):
                out.append((it.get("filename"), it.get("subfolder", ""), it.get("type")))
    return out


def monitor_loop(args, ids):
    cb, token = args.cb, args.token
    # 1) 回调自检：等本机 server /api/callback 就绪（可能要等 /restart），最多重试 12h
    for attempt in range(720):
        try:
            _notify(cb, token, f"📡 SCNet monitor 上线自检 OK：监控 {len(ids)} 个任务（{','.join(i[:8] for i in ids)}）")
            with _LOCK:
                STATE["callback"] = "ok"
            break
        except Exception as e:
            with _LOCK:
                STATE["callback"] = f"wait({attempt}): {str(e)[:100]}"
            time.sleep(60)
    else:
        return

    # 2) 轮询各任务
    done = set()
    t0 = time.time()
    while len(done) < len(ids) and time.time() - t0 < 24 * 3600:
        for pid in ids:
            if pid in done:
                continue
            try:
                h = _get("/history/" + pid)
            except Exception:
                continue
            if pid not in h:
                with _LOCK:
                    STATE["tasks"][pid[:8]] = "queued"
                continue
            entry = h[pid]
            st = (entry.get("status") or {}).get("status_str", "?")
            with _LOCK:
                STATE["tasks"][pid[:8]] = st
            if st == "error":
                done.add(pid)
                try:
                    _notify(cb, token, f"⚠️〔SCNet monitor〕任务 {pid} 执行出错（status=error），跳过")
                except Exception:
                    pass
                continue
            if not (entry.get("status") or {}).get("completed"):
                continue
            # 完成：推送产物 + 通知
            files = _collect_files(entry)
            ok_files = []
            for fn, sub, typ in files:
                try:
                    q = urllib.parse.urlencode({"filename": fn, "subfolder": sub, "type": typ})
                    with urllib.request.urlopen(COMFY + "/view?" + q, timeout=600) as r:
                        data = r.read()
                    if not args.no_push_file:
                        _push_file(cb, token, fn, data)
                    ok_files.append(f"{fn}({len(data)/1e6:.1f}MB)")
                    with _LOCK:
                        STATE["pushed"] += 1
                except Exception as e:
                    ok_files.append(f"{fn}(推送失败:{str(e)[:40]})")
            done.add(pid)
            try:
                _notify(cb, token, f"🎬〔SCNet monitor〕任务 {pid} 完成，产物已推送：{'; '.join(ok_files) or '无产物'}")
            except Exception:
                pass
        time.sleep(args.interval)

    # 3) 全部完成
    try:
        _notify(cb, token, f"🏁〔SCNet monitor〕全部 {len(ids)} 个任务完成（产物推送 {STATE['pushed']} 个）。"
                           f"请检查产物并决定是否关机（playwright 控制台或通知我）。")
    except Exception:
        pass


class StatusHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        with _LOCK:
            body = json.dumps(STATE, ensure_ascii=False, indent=1).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ids", required=True, help="逗号分隔 prompt_id")
    ap.add_argument("--cb", required=True, help="回调 URL（agt /api/callback）")
    ap.add_argument("--token", required=True)
    ap.add_argument("--port", type=int, default=8191)
    ap.add_argument("--interval", type=int, default=15)
    ap.add_argument("--no-push-file", action="store_true")
    args = ap.parse_args()
    ids = [x.strip() for x in args.ids.split(",") if x.strip()]

    t = threading.Thread(target=monitor_loop, args=(args, ids), daemon=True)
    t.start()
    HTTPServer(("0.0.0.0", args.port), StatusHandler).serve_forever()


if __name__ == "__main__":
    main()
