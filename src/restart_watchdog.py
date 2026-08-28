"""restart_watchdog.py —— /restart 看门狗：父进程退出后拉起新 agt 进程（恢复 session/端口/消息）。

被 spawn_watchdog() 以独立进程启动（参数走 argv），与主程序解耦：
  1. 轮询等待父进程退出（pid 探测，跨平台；优雅退出=当前轮跑完才退，超时 300s 放弃）
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

    # 1) 等父进程退出（优雅退出=当前轮跑完才退，长轮/自动工作流可超 90s，容忍 300s；
    #    超时则强杀——/restart 是用户显式重启请求，卡死进程不应阻塞重启）
    time.sleep(1.0)
    deadline = time.time() + 300
    last_note = time.time()
    while time.time() < deadline:
        if not _pid_alive(parent_pid):
            break
        if time.time() - last_note >= 20:
            log(f"… 父进程 {parent_pid} 仍在收尾（当前轮未完），继续等待")
            last_note = time.time()
        time.sleep(0.5)
    else:
        log(f"⚠️ 父进程 {parent_pid} 300s 未退出（可能卡死），/restart 显式请求——强杀后继续")
        try:
            if os.name == "nt":
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(parent_pid)],
                               capture_output=True, timeout=15)
            else:
                import signal
                os.kill(parent_pid, signal.SIGKILL)
        except Exception as e:
            log(f"⚠️ 强杀失败（{type(e).__name__}: {e}），仍尝试拉起新进程")
        time.sleep(2.0)
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
    # 日志按实例分离（mode+port）：多实例共写单文件会互相混杂——9000 与 8000 的 stdout
    # 交错 16 万行，排障时找不到彼此的段（实测教训）；新实例日志可独立 tail
    log_file = Path.home() / ".agt" / f"restart-{mode}-{port or 'cli'}.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)

    kwargs = {}
    if os.name == "nt":
        kwargs["creationflags"] = (getattr(subprocess, "DETACHED_PROCESS", 0)
                                   | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
    else:
        kwargs["start_new_session"] = True
    with open(log_file, "a", encoding="utf-8") as lf:
        lf.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} restart (mode={mode}) =====\n")
        # -u：新进程 unbuffered——装配日志实时落盘（块缓冲会把输出困住几十分钟，
        # 事后无法判断新进程卡在哪个阶段）
        proc = subprocess.Popen([sys.executable, "-u", "-c", code], cwd=cwd, env=env,
                                stdout=lf, stderr=subprocess.STDOUT, **kwargs)
    log(f"新进程已拉起 pid={proc.pid}")
    # 4) 确认就绪：web 轮询 HTTP 200（装配 MCP/RAG 冷启动可超 90s——曾见 HF 联网探测挂起
    #    装配数分钟，故容忍 300s 且每 20s 报进度）；cli 确认 3s 内没秒退
    if mode == "web" and port:
        deadline = time.time() + 300
        last_note = time.time()
        while time.time() < deadline:
            if proc.poll() is not None:
                log(f"❌ 新进程提前退出 (code={proc.returncode})，详见 {log_file}")
                return
            if _http_up(port):
                log(f"✅ 新进程就绪 http://127.0.0.1:{port}/ ，看门狗退出")
                return
            if time.time() - last_note >= 20:
                log(f"… 新进程仍在装配（pid={proc.pid} 活着），继续等待")
                last_note = time.time()
            time.sleep(1.0)
        if proc.poll() is None:
            log(f"⚠️ 新进程 300s 未就绪但没死（pid={proc.pid}，仍在装配），看门狗退出——"
                f"服务稍后应自行就绪，装配日志见 {log_file}")
        else:
            log(f"❌ 新进程已退出 (code={proc.returncode})，详见 {log_file}")
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
    log_file = Path.home() / ".agt" / f"restart-{mode}-{port or 'cli'}.log"   # 与 run() 同名分离
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
