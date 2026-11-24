"""vector_hub.py —— 本地向量栈共享服务（用户设计 2026-11-24）。

给本机所有 agt 实例共用一份 embedding 模型（默认 bge-small-zh-v1.5，sentence_transformers）
——原来每个 agt 进程各自加载 ~560MB 向量栈（torch+bge+faiss+BLAS+numpy），N 实例 N 份；
hub 化后全机一份。实例侧首次用到向量检索时懒拉起本服务并经 HTTP 取向量（HubEmbedder）；
hub 不可达/超时 → 实例回退本地加载（功能保底，检索不中断）。

协议（HTTP 无状态，仅本机回环）：
  GET  /health                → {"ok": bool, "model": str, "dim": int}
  POST /embed {"texts": [...]} → {"vectors": [[float,...], ...]}（归一化向量）

启动：python vector_hub.py --port 8097 --model E:/AI/models/bge-small-zh-v1.5 --idle-timeout 600
空闲自关：超过 idle_timeout 秒无任何请求即自行退出——HTTP 无连接可探活，用「最近请求
时间」代理「所有连接实例离线」（实例每轮检索都会打 /embed，闲置即衰减退出，零运维）。
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

DEFAULT_MODEL_PATH = r"E:\AI\models\bge-small-zh-v1.5"
DEFAULT_PORT = 8097
DEFAULT_IDLE_TIMEOUT = 600

_LAST_REQ = time.time()
_LOCK = threading.Lock()
_MODEL = None
_MODEL_NAME = ""
_DIM = 0
_LOAD_ERR = ""


def _load_model(model_path: str):
    """加载 sentence_transformers 模型（后台线程；失败记 _LOAD_ERR 供 /health 暴露）。"""
    global _MODEL, _MODEL_NAME, _DIM, _LOAD_ERR
    import os
    # 环境隔离（与 rag._hf_local_offline 同款）：跳过 HF 联网探测 + 钉 torch 后端跳过 TF 探测
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
    os.environ.setdefault("USE_TORCH", "1")
    try:
        from sentence_transformers import SentenceTransformer
        m = SentenceTransformer(model_path)
        _MODEL = m
        _MODEL_NAME = model_path
        _DIM = int(m.get_sentence_embedding_dimension())
        print(f"[vector_hub] 模型就绪 {model_path} dim={_DIM}", flush=True)
    except Exception as e:
        _LOAD_ERR = f"{type(e).__name__}: {e}"
        print(f"[vector_hub] 模型加载失败：{_LOAD_ERR}", flush=True)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass   # 静默访问日志（空闲判定只看时间戳）

    def _touch(self):
        global _LAST_REQ
        with _LOCK:
            _LAST_REQ = time.time()

    def _send(self, code: int, obj: dict):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self._touch()
        if self.path.split("?")[0].rstrip("/").endswith("/health"):
            self._send(200, {"ok": _MODEL is not None, "model": _MODEL_NAME,
                             "dim": _DIM, "error": _LOAD_ERR})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        self._touch()
        if not self.path.split("?")[0].rstrip("/").endswith("/embed"):
            self._send(404, {"error": "not found"})
            return
        if _MODEL is None:
            self._send(503, {"error": _LOAD_ERR or "model not ready"})
            return
        try:
            n = int(self.headers.get("Content-Length") or 0)
            req = json.loads(self.rfile.read(n).decode("utf-8") or "{}")
            texts = [str(t) for t in (req.get("texts") or [])]
            if not texts:
                self._send(400, {"error": "texts required"})
                return
            vecs = _MODEL.encode(texts, normalize_embeddings=True, show_progress_bar=False)
            self._send(200, {"vectors": [[float(x) for x in v] for v in vecs]})
        except Exception as e:
            self._send(500, {"error": f"{type(e).__name__}: {e}"})


def main():
    ap = argparse.ArgumentParser(description="本地向量栈共享服务（多 agt 实例共用一份 embedding 模型）")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--model", default=DEFAULT_MODEL_PATH)
    ap.add_argument("--idle-timeout", type=int, default=DEFAULT_IDLE_TIMEOUT,
                    help="秒；超过该时长无任何请求即自行退出（默认 600）")
    args = ap.parse_args()

    threading.Thread(target=_load_model, args=(args.model,), daemon=True).start()  # 加载不阻塞端口就绪
    srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    srv.timeout = 1   # handle_request 无请求时 1s 返回 → 看门狗节拍
    print(f"[vector_hub] listening on 127.0.0.1:{args.port} model={args.model} "
          f"idle_timeout={args.idle_timeout}s", flush=True)
    while True:
        srv.handle_request()
        with _LOCK:
            idle = time.time() - _LAST_REQ
        if idle >= args.idle_timeout:
            print(f"[vector_hub] 空闲 {idle:.0f}s ≥ {args.idle_timeout}s → 自行退出", flush=True)
            break
    srv.server_close()


# ==================== agt 侧客户端（实例接入点） ====================

def hub_port_from_settings() -> int:
    """读 settings.json 的 vector_hub_port（0/缺省=禁用 hub，走本地加载）。"""
    try:
        from paths import AGT_DIR
        p = AGT_DIR / "settings.json"
        if p.exists():
            v = (json.loads(p.read_text(encoding="utf-8")) or {}).get("vector_hub_port")
            return int(v) if v else 0
    except Exception:
        pass
    return 0


class HubEmbedder:
    """经本地 vector_hub HTTP 服务取向量（多实例共享一份模型）。
    接口与 SentenceTransformer 对齐（get_sentence_embedding_dimension / encode），
    可被 rag._CachedEmbedder 直接包装。"""

    def __init__(self, port: int, dim: int = 0):
        self.port = int(port)
        self.base = f"http://127.0.0.1:{self.port}"
        self._dim = int(dim)

    def get_sentence_embedding_dimension(self) -> int:
        if self._dim:
            return self._dim
        with urllib.request.urlopen(f"{self.base}/health", timeout=5) as r:
            self._dim = int((json.loads(r.read().decode("utf-8")) or {}).get("dim") or 0)
        return self._dim

    def encode(self, texts, batch_size=32, normalize_embeddings=True,
               show_progress_bar=False):
        body = json.dumps({"texts": [str(t) for t in texts]}).encode("utf-8")
        req = urllib.request.Request(f"{self.base}/embed", data=body,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=120) as r:
            vecs = (json.loads(r.read().decode("utf-8")) or {}).get("vectors")
        if vecs is None:
            raise RuntimeError("vector_hub /embed 返回缺 vectors")
        import numpy as np
        return np.asarray(vecs, dtype="float32")


def _health_ok(port: int, timeout: float = 2.0) -> tuple:
    """/health 探活。返回 (模型就绪?, dim)。连不上 → (False, 0)。"""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=timeout) as r:
            d = json.loads(r.read().decode("utf-8")) or {}
        return bool(d.get("ok")), int(d.get("dim") or 0)
    except Exception:
        return False, 0


def ensure_hub_started(port: int, model_path: str = "",
                       idle_timeout: int = DEFAULT_IDLE_TIMEOUT,
                       wait_seconds: int = 120):
    """懒拉起 vector_hub 并返回 HubEmbedder；就绪探测失败/超时返回 None（调用方回退本地）。

    防多实例并发拉起竞态：先探活（别的实例可能已拉起）→ 不通才 Popen 拉起（bind 端口
    失败视为对方已拉起，不视为错误）→ 轮询 /health 直到模型就绪（≤wait_seconds）。"""
    ok, dim = _health_ok(port)
    if ok:
        return HubEmbedder(port, dim)
    exe = sys.executable
    script = str(Path(__file__).resolve())
    try:
        subprocess.Popen(
            [exe, script, "--port", str(port),
             "--model", model_path or DEFAULT_MODEL_PATH,
             "--idle-timeout", str(idle_timeout)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=(subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
                           if sys.platform == "win32" else 0))
    except OSError:
        pass   # 端口被占=别的实例正在拉起 → 落到下面的轮询
    deadline = time.time() + wait_seconds
    while time.time() < deadline:
        ok, dim = _health_ok(port)
        if ok:
            return HubEmbedder(port, dim)
        time.sleep(1.0)
    print(f"[vector_hub] 拉起/就绪探测超时（port={port}）→ 回退本地加载", flush=True)
    return None


def try_hub_embedder(cfg: dict):
    """rag.py / session_vec.py 的 embedder 获取点入口：
    settings.vector_hub_port>0 时懒拉起 hub 并返回 HubEmbedder；
    禁用（0/缺省）或拉起失败返回 None（调用方回退本地 SentenceTransformer）。"""
    port = hub_port_from_settings()
    if not port:
        return None
    try:
        return ensure_hub_started(port, (cfg or {}).get("embed_model_path") or "")
    except Exception as e:
        print(f"[vector_hub] 接入失败（回退本地）：{type(e).__name__}: {e}", flush=True)
        return None


if __name__ == "__main__":
    main()
