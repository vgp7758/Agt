# -*- coding: utf-8 -*-
"""SSH 隧道（常驻，双向）：
  反向（-R）：容器 :19999 -> 本机 llm_relay(19999) —— 给无网容器出 LLM API
  正向（-L）：本机 :18080 -> 容器 agt-web :8080 —— 给本机 remote_connect 用（不暴露公网）
断线自动重连（30s 心跳 + 循环重建）。
"""
import paramiko, socket, threading, time, select

REMOTE_HOST, REMOTE_PORT = "ssh.zzai.scnet.cn", 10300
USER, PWD = "root", "XUAL4AB7QL9YPAN"
LOCAL_TARGET = ("127.0.0.1", 19999)   # 反向：本机 llm_relay
REMOTE_BIND_PORT = 19999              # 反向：容器侧监听
FWD = [(18080, ("127.0.0.1", 8080))]  # 正向：本机端口 -> 容器侧目标

CUR_TP = None
_lock = threading.Lock()

def pump(a, b):
    try:
        while True:
            r, _, _ = select.select([a, b], [], [], 60)
            if not r:
                break
            for s in r:
                data = s.recv(65536)
                if not data:
                    return
                (b if s is a else a).sendall(data)
    except Exception:
        pass
    finally:
        for s in (a, b):
            try: s.close()
            except Exception: pass

def serve_reverse(chan):
    try:
        sock = socket.create_connection(LOCAL_TARGET, timeout=10)
    except Exception:
        chan.close(); return
    pump(chan, sock)

def serve_forward(csock, dest):
    with _lock:
        tp = CUR_TP
    if tp is None:
        csock.close(); return
    try:
        chan = tp.open_channel("direct-tcpip", dest, csock.getpeername())
    except Exception:
        csock.close(); return
    pump(csock, chan)

def start_forward_listeners():
    def run():
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", FWD[0][0]))
        srv.listen(8)
        print(f"[tunnel] forward local:{FWD[0][0]} -> container:{FWD[0][1][1]}", flush=True)
        while True:
            try:
                c, _ = srv.accept()
            except OSError:
                return
            threading.Thread(target=serve_forward, args=(c, FWD[0][1]), daemon=True).start()
    threading.Thread(target=run, daemon=True).start()

def main():
    global CUR_TP
    started_fwd = False
    while True:
        try:
            cli = paramiko.SSHClient()
            cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            cli.connect(REMOTE_HOST, port=REMOTE_PORT, username=USER, password=PWD,
                        timeout=30, banner_timeout=30)
            tp = cli.get_transport()
            tp.set_keepalive(30)
            with _lock:
                CUR_TP = tp
            tp.request_port_forward("", REMOTE_BIND_PORT)
            if not started_fwd:
                start_forward_listeners()
                started_fwd = True
            print("[tunnel] up: reverse container:19999->local:19999", flush=True)
            while True:
                chan = tp.accept(60)
                if chan is None:
                    if not tp.is_active():
                        raise ConnectionError("transport dead")
                    continue
                threading.Thread(target=serve_reverse, args=(chan,), daemon=True).start()
        except Exception as e:
            with _lock:
                CUR_TP = None
            print(f"[tunnel] down: {e}; retry in 15s", flush=True)
            time.sleep(15)

if __name__ == "__main__":
    main()
