"""web_desktop.py —— 桌面窗口壳（spec s_d53311f8 Step 1）。

pywebview 薄壳：系统 WebView（Win: WebView2 / mac: WKWebView / Linux: gtkwebkit），
非 Electron。对 web_main 的侵入只有两个分支点：
    open_browser(port)  →  web_desktop.open_window(port)      # 注册窗口（立即返回）
    _render_loop(...)   →  web_desktop.run_loop()             # webview.start() 阻塞至窗口关闭
窗口关闭 → web_main 现有 finally 链（work_q None → worker join → stop_server →
agent.shutdown → mcp shutdown）——优雅退出零新代码；正在跑的轮由 session 中断轮
防御（t150/t272）在读档时兜底恢复。

pywebview 为可选依赖：pip install agt-agent[desktop]；缺失时给出安装指引退出。
打包形态（Step 2）由 PyInstaller 入口设 AGT_DESKTOP=1 走同一路径。
"""
from __future__ import annotations

import os
import sys
import socket
import tempfile
from pathlib import Path


def is_desktop() -> bool:
    """桌面模式判定：--desktop 参数 或 AGT_DESKTOP=1（打包 exe 入口设置）。"""
    if os.environ.get("AGT_DESKTOP", "").strip() in ("1", "true", "yes"):
        return True
    return "--desktop" in sys.argv[1:]


def _lock_file() -> Path:
    base = os.environ.get("APPDATA") or tempfile.gettempdir()
    return Path(base) / "Agt" / "instance.lock"


def _acquire_single_instance() -> bool:
    """单实例锁：锁文件记 pid。已有活实例（pid 存活且是 Agt 桌面进程）→ False。
    跨进程聚焦现有窗口需要平台 API（MVP 简化为提示后退出）。"""
    lf = _lock_file()
    try:
        if lf.exists():
            old = lf.read_text(encoding="utf-8").strip()
            if old and _pid_alive(old) and _looks_like_agt(old):
                return False
    except OSError:
        pass
    lf.parent.mkdir(parents=True, exist_ok=True)
    try:
        lf.write_text(str(os.getpid()), encoding="utf-8")
    except OSError:
        pass   # 写不了锁（权限/只读）→ 放行（不因锁失败拒绝启动）
    return True


def _pid_alive(pid_s: str) -> bool:
    try:
        pid = int(pid_s)
    except ValueError:
        return False
    if pid <= 0:
        return False
    try:
        if sys.platform == "win32":
            # 0=存在探测；权限不足抛错也算存活（别的用户的进程）
            import ctypes
            k32 = ctypes.windll.kernel32
            h = k32.OpenProcess(0x1000, False, pid)   # PROCESS_QUERY_LIMITED_INFORMATION
            if not h:
                return False
            k32.CloseHandle(h)
            return True
        os.kill(pid, 0)
        return True
    except (OSError, Exception):
        return False


def _looks_like_agt(pid_s: str) -> bool:
    """锁上的 pid 是否还像 Agt（防 pid 复用误判）：Win 上比对进程名。非 Win 放行判定（保守）。"""
    if sys.platform != "win32":
        return True
    try:
        out = os.popen(f'tasklist /fi "PID eq {pid_s}" /fo csv /nh', "r").read()
        low = out.lower()
        return "agt" in low
    except OSError:
        return True


def pick_port(preferred: int) -> int:
    """端口退让：preferred 被占用 → +1 逐个试（上限 +50），全占则随机高位。"""
    def free(p: int) -> bool:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            return s.connect_ex(("127.0.0.1", p)) != 0
    for p in range(preferred, preferred + 51):
        if free(p):
            return p
    return 0   # 调用方 os.getsockname 兜底；0 = uvicorn 自选


_WINDOW = None
_PORT = 0


def open_window(port: int):
    """注册桌面窗口（立即返回；真正显示在 run_loop 的 webview.start()）。
    服务此刻已就绪（调用点在 start_server 之后，与 open_browser 同位）。"""
    global _WINDOW, _PORT
    try:
        import webview
    except ImportError:
        print("❌ 桌面模式需要 pywebview：pip install agt-agent[desktop]")
        print("   （浏览器模式不受影响：直接 agt-web 即可）")
        sys.exit(2)
    url = f"http://127.0.0.1:{port}/"
    if not _acquire_single_instance():
        # 已有桌面实例：**只开一个指向它的窗口，不再启一套引擎**（多开支持，
        # 2026-09-10 用户裁定）：单实例锁退化为进程内的窗口去重，引擎/端口/引擎
        # 数据（AGENTS.md/rules/.agent）都是新的，互不干扰。
        print(f"ℹ️  已有 Agt 桌面实例在运行——本窗口改为多开模式（服务 {url}）")
    _WINDOW = webview.create_window(
        "Agt", url,
        width=1280, height=860, min_size=(900, 600),
        text_select=True,      # 允许选中复制（聊天应用刚需）
    )


def run_loop():
    """GUI 主循环（阻塞）：webview.start() 返回 = 所有窗口已关闭 → web_main 的
    finally 链接管优雅退出。agent 正在跑的轮：daemon worker 被强停，轮记录由
    session 的中断轮防御在读档时归档恢复（t150/t272 已验证的兜底）。

    降级链（2026-09-10 CI 实测两连坑后加）：pythonnet 新版/异构建的产物里
    Python.Runtime.dll 加载失败（Failed to resolve ...Loader.Initialize）
    → webview 两个 Windows 后端全灭 → 崩弹窗。webview 起不来时降级为
    系统默认浏览器打开（服务还在，Agent 完全可用），进程保持运行直到
    用户关闭（任务栏 python 进程/Ctrl+C）。桌面体验降级但不死。"""
    url = f"http://127.0.0.1:{_PORT}/" if _PORT else None
    try:
        import webview
        webview.start()
    except Exception as e:
        import webbrowser
        print(f"⚠️  桌面窗口启动失败（{type(e).__name__}: {e}），降级为系统浏览器模式")
        if url:
            webbrowser.open(url)
        print("   （进程继续在后台服务；关闭此进程退出）")
        try:
            import time as _t
            while True:
                _t.sleep(3600)   # 阻塞保活：无窗口可等，服务由 worker/子进程持续运行
        except KeyboardInterrupt:
            pass
    # 释放单实例锁（优雅路径；强杀由 pid 存活检测自愈）
    try:
        _lock_file().unlink(missing_ok=True)
    except OSError:
        pass
