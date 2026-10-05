#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""image_feed PoC —— 桌面实时画面服务（实时画面装配段 · 2026-10-06 用户提案配套）

用法：
  python tools/image_feed_poc.py [port]        # 默认 8765

Agent 侧配合（.yml 的 assembly，声明在 steps 段之后）：
  assembly:
    - text: |
        （人设……）
    - user_message
    - steps
    - image_feed: http://127.0.0.1:8765/frame   # 每步取最新桌面帧挂投影末尾

行为：
  · PIL.ImageGrab 抓全屏 → 缩放（默认宽 1280 控 token）→ JPEG q70 → 内存缓存
  · GET /frame 返回缓存帧（0.5s 节流：间隔内的请求复用同一帧，防高频抓屏）
  · 零落盘：帧只驻内存，不写任何文件
依赖：Pillow（pip install Pillow）
"""
import io
import sys
import time
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
MAX_W = int(sys.argv[2]) if len(sys.argv) > 2 else 1280
THROTTLE = 0.5   # 秒：缓存帧有效期

_lock = threading.Lock()
_cache = {"jpeg": b"", "ts": 0.0}


def _grab() -> bytes:
    """抓屏 → JPEG bytes（缩放 + 质量控制）。失败抛异常（上层转 503）。"""
    from PIL import ImageGrab
    img = ImageGrab.grab()
    if img.width > MAX_W:
        img = img.resize((MAX_W, int(img.height * MAX_W / img.width)))
    buf = io.BytesIO()
    img.convert("RGB").save(buf, "JPEG", quality=70)
    return buf.getvalue()


class H(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.split("?")[0] not in ("/frame", "/"):
            self.send_response(404); self.end_headers(); return
        with _lock:
            fresh = (time.time() - _cache["ts"]) < THROTTLE and _cache["jpeg"]
            if not fresh:
                try:
                    _cache["jpeg"] = _grab()
                    _cache["ts"] = time.time()
                except Exception as e:
                    self.send_response(503)
                    self.send_header("Content-Type", "text/plain; charset=utf-8")
                    self.end_headers()
                    self.wfile.write(f"grab failed: {e}".encode("utf-8"))
                    return
            data = _cache["jpeg"]
        self.send_response(200)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    srv = HTTPServer(("127.0.0.1", PORT), H)
    print(f"🖼️ image_feed 桌面画面服务 http://127.0.0.1:{PORT}/frame "
          f"(宽≤{MAX_W} · 节流{THROTTLE}s · 内存帧零落盘)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
