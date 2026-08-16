"""restart_watchdog.py —— /restart 看门狗：父进程退出后拉起新 agt 进程（恢复 session/端口/消息）。

被 spawn_watchdog() 以独立进程启动（参数走 argv），与主程序解耦：
  1. 轮询等待父进程退出（pid 探测，跨平台；超时 90s 放弃）
  2. 若有 web 端口：轮询端口释放（最多 30s）
  3. 拉起新进程（cwd=原 workspace；AGT_RESTART_SESSION / AGT_RESTART_MESSAGE 环境变量
     传递恢复指令，由 chat.main/web_main 开头的 _recover_restart_env 消费）
  4. web 模式轮询 HTTP 就绪；cli 模式确认进程未秒退。就绪后看门狗退出。

纯标准库（不 import agt 任何模块——看门狗必须独立于被重启的代码存活）。
"""
from __future__ import annotations

import argparse
import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path


def _pid_alive(pid: int) -> bool:
    """探测进程是否存活。Windows 用 OpenProcess+WaitForSingleObject，POSIX 用 kill(pid,0)。"""
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        SYNCHRONIZE = 0x00100000
        WAIT_TIMEOUT = 0x102
        h = ctypes.windll.kernel32.OpenProcess(SYNCHRONIZE, False, int(pid))
        if not h:
            return False
        try:
            return ctypes.windll.kernel32.WaitForSingleObject(h, 0) == WAIT_TIMEOUT
        finally:
            ctypes.windll.kernel32.CloseHandle(h)
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _port_free(port: int) -> bool:
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", int(port)))
        return True
    except OSError:
        return False
    finally:
        s.close()


def _http_up(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=2) as r:
            return r.status == 200
    except Exception:
        return False


def run(parent_pid: int, mode: str, session: str, port: int, message: str, cwd: str, src_dir: str):
    log = lambda *a: print(time.strftime("[%H:%M:%S]"), *a, flush=True)
    log(f"watchdog 启动：parent={parent_pid} mode={mode} session={session!r} port={port} msg={message[:40]!r}")

    # 1) 等父进程退出（给 finally 清理留时间；先 sleep 1 再轮询）
    time.sleep(1.0)
    deadline = time.time() + 90
    while time.time() < deadline:
        if not _pid_alive(parent_pid):
            break
        time.sleep(0.5)
    else:
        log(f"❌ 等父进程 {parent_pid} 退出超时（90s），放弃重启")
        return
    log(f"父进程 {parent_pid} 已退出")

    # 2) web 模式：等端口释放（stop_server 后应很快；uvicorn 释放有延迟容忍）
    if mode == "web" and port:
        deadline = time.time() + 30
        while time.time() < deadline:
            if _port_free(port):
                break
            time.sleep(0.5)
        else:
            log(f"⚠️ 端口 {port} 30s 未释放，仍尝试启动（可能起在别的端口失败）")
        log(f"端口 {port} 已释放")

    # 3) 拉起新进程（环境变量传恢复指令；stdout 重定向日志文件防 detached 丢失输出）
    entry = "web_main(" + (str(port) if (mode == "web" and port) else "None") + ")" \
        if mode == "web" else "main()"
    code = (f"import sys; sys.path.insert(0, {src_dir!r}); "
            f"import chat; chat.{entry}")
    env = dict(os.environ)
    if session and session != "-":
        env["AGT_RESTART_SESSION"] = session
    if message and message != "-":
        env["AGT_RESTART_MESSAGE"] = message
    log_file = Path.home() / ".agt" / "restart.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)
    kwargs = {}
    if os.name == "nt":
        kwargs["creationflags"] = (getattr(subprocess, "DETACHED_PROCESS", 0)
                                   | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
    else:
        kwargs["start_new_session"] = True
    with open(log_file, "a", encoding="utf-8") as lf:
        lf.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} restart (mode={mode}) =====\n")
        proc = subprocess.Popen([sys.executable, "-c", code], cwd=cwd, env=env,
                                stdout=lf, stderr=subprocess.STDOUT, **kwargs)
    log(f"新进程已拉起 pid={proc.pid}")

    # 4) 确认就绪：web 轮询 HTTP 200（装配 MCP/RAG 可能慢，容忍 90s）；cli 确认 3s 内没秒退
    if mode == "web" and port:
        deadline = time.time() + 90
        while time.time() < deadline:
            if proc.poll() is not None:
                log(f"❌ 新进程提前退出 (code={proc.returncode})，详见 {log_file}")
                return
            if _http_up(port):
                log(f"✅ 新进程就绪 http://127.0.0.1:{port}/ ，看门狗退出")
                return
            time.sleep(1.0)
        log(f"⚠️ 新进程 90s 未就绪（可能仍在装配），pid={proc.pid}，看门狗退出（详见 {log_file}）")
    else:
        time.sleep(3)
        if proc.poll() is None:
            log("✅ 新进程运行中，看门狗退出")
        else:
            log(f"❌ 新进程 3s 内退出 (code={proc.returncode})，详见 {log_file}")


def spawn_watchdog(parent_pid: int, mode: str, session: str, port: int, message: str, cwd: str) -> tuple:
    """主程序侧调用：以独立进程启动看门狗。返回 (ok, 描述)。"""
    script = Path(__file__).resolve()
    cmd = [sys.executable, str(script),
           "--parent-pid", str(parent_pid), "--mode", mode,
           "--session", session or "-", "--port", str(port or 0),
           "--message", message or "-", "--cwd", cwd,
           "--src-dir", str(script.parent)]
    log_file = Path.home() / ".agt" / "restart.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)
    kwargs = {}
    if os.name == "nt":
        kwargs["creationflags"] = (getattr(subprocess, "DETACHED_PROCESS", 0)
                                   | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
    else:
        kwargs["start_new_session"] = True
    try:
        with open(log_file, "a", encoding="utf-8") as lf:
            subprocess.Popen(cmd, cwd=cwd, stdout=lf, stderr=subprocess.STDOUT, **kwargs)
        return True, (f"🔁 看门狗已启动：退出后自动重启（mode={mode}"
                      + (f"，端口 {port}" if port else "")
                      + (f"，恢复会话「{session}」" if session else "")
                      + "）")
    except Exception as e:
        return False, f"❌ 看门狗启动失败：{type(e).__name__}: {e}"


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--parent-pid", type=int, required=True)
    ap.add_argument("--mode", choices=["cli", "web"], required=True)
    ap.add_argument("--session", default="-")
    ap.add_argument("--port", type=int, default=0)
    ap.add_argument("--message", default="-")
    ap.add_argument("--cwd", required=True)
    ap.add_argument("--src-dir", required=True)
    a = ap.parse_args()
    run(a.parent_pid, a.mode, a.session, a.port, a.message, a.cwd, a.src_dir)
