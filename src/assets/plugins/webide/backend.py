"""webide —— 插件后端：POST /api/webide/open（拉起/复用 VS Code serve-web 工作台）。
原 server.py 内置 /api/ide/open 的插件化搬迁（用户提案 2026-10-15）。逻辑照搬：
- 端口潜规则：WebUI 端口 + 30000；被占（WinNAT 隐形保留/serve-web 孤儿）向后扫 +1..+5 选可 bind 口
- 等待上限 12s（首次下载组件不阻塞交互——返回 ready=false + hint，页签自己等下载自动刷新）
- --commit-id 钉缓存版本 + 后台滚动预热新版（浏览器双通道更新同款）
依赖适配：_workspace → api.workspace；_agent.services → api.agent.services。
图标资产：插件目录内 vscode.png，前端经 /plugins-assets/webide/vscode.png 引用
（plugins.plugin_asset 安全解析：resolve 后必须在插件目录内——任意 png/svg/css 同机制）。"""
from fastapi import Request
import asyncio
import time


async def _open(request: 'Request'):
    import urllib.request as _ur
    import socket as _sk
    try:
        _host = request.headers.get("host") or ""
        base_port = int(_host.rsplit(":", 1)[1]) + 30000 if ":" in _host else 38000
    except Exception:
        base_port = 38000

    def _alive(port):
        try:
            with _ur.urlopen(f"http://127.0.0.1:{port}/", timeout=3) as r:
                r.read()
                return True
        except Exception:
            return False

    def _ready(port):
        try:
            with _ur.urlopen(f"http://127.0.0.1:{port}/", timeout=3) as r:
                return len(r.read() or b"") > 500   # 下载占位页仅 146 字符
        except Exception:
            return False

    def _bindable(port):
        s = _sk.socket()
        try:
            s.bind(("0.0.0.0", port))
            return True
        except OSError:
            return False
        finally:
            s.close()

    # ① 潜规则口「端口还在」→ 直接复用（下载中(202)/工作台(200)都算服务在，页签自己等）
    if _alive(base_port):
        return _payload(_ready(base_port), base_port)
    # ② 选口：潜规则口可 bind 用它；被占向后扫 +1..+5
    PORT = next((p for p in [base_port + i for i in range(6)] if _bindable(p)), None)
    if PORT is None:
        return 200, {"ok": False, "error": f"端口 {base_port}..{base_port + 5} 均被占用且无可复用服务"
                     f"（疑似 serve-web 孤儿进程或 Windows 动态保留——重启系统可重置）"}
    # --default-folder 直开工作区（勿用 ?folder= URL 参数——serve-web 1.134 会误路由成
    # "远程代理"会话，标签页名变成 l10n 字面量，用户实锤）+ --commit-id 钉缓存版本
    _cid = ""
    _cached_now: set = set()
    try:
        import pathlib as _pl
        _sd = _pl.Path.home() / ".vscode" / "cli" / "serve-web"
        _cands = [d for d in _sd.iterdir() if d.is_dir() and (d / "node.exe").exists()]
        if _cands:
            _cached_now = {d.name for d in _cands}
            _cid = max(_cands, key=lambda d: d.stat().st_mtime).name
    except Exception:
        pass
    cmd = (f'code serve-web --host 0.0.0.0 --port {PORT} --without-connection-token '
           f'--accept-server-license-terms --default-folder "{_WS}"'
           + (f' --commit-id {_cid}' if _cid else ''))
    if _cid:   # 有缓存（秒起）→ 后台滚动预热新版
        asyncio.get_event_loop().create_task(_prefetch(_cached_now))
    _ag = _AGENT()
    if _ag is not None:
        try:
            r = _ag.services.start("webide", cmd)
            if "已存在" in str(r):   # 同名条目但探测不活——重建
                try:
                    _ag.services.stop("webide")
                    _ag.services.start("webide", cmd)
                except Exception:
                    pass
        except Exception:
            pass
    else:
        import subprocess as _sp
        _sp.Popen(cmd, shell=True, creationflags=getattr(_sp, "CREATE_NO_WINDOW", 0))
    t0 = time.time()
    while time.time() - t0 < 12:   # 12s 上限：非首次秒起；首次下载交给页签自动刷新
        if _ready(PORT):
            return _payload(True, PORT)
        await asyncio.sleep(2)
    return _payload(False, PORT)


async def _prefetch(cached_hashes: set):
    """组件滚动预热（浏览器双通道更新同款）：旧版秒起的同时后台拉最新 stable 到缓存，
    轮询出现新 hash 目录 → kill 预热进程 → 清理超出最近 2 个的旧版。"""
    import subprocess as _sp, shutil as _sh
    import pathlib as _pl
    import socket as _sk2
    try:
        sd = _pl.Path.home() / ".vscode" / "cli" / "serve-web"
        pport = None
        for p in range(39990, 39997):
            s = _sk2.socket()
            try:
                s.bind(("127.0.0.1", p)); pport = p; break
            except OSError:
                pass
            finally:
                s.close()
        if pport is None:
            return
        pre = _sp.Popen('code serve-web --host 127.0.0.1 --port %d --without-connection-token '
                        '--accept-server-license-terms' % pport, shell=True,
                        creationflags=getattr(_sp, "CREATE_NO_WINDOW", 0))
        t0 = time.time()
        while time.time() - t0 < 600:   # 10 分钟上限（689MB 慢网可容）
            await asyncio.sleep(15)
            try:
                done = any(d.is_dir() and (d / "node.exe").exists() and d.name not in cached_hashes
                           for d in sd.iterdir())
            except Exception:
                done = False
            if done:
                break
        try:
            pre.kill()
        except Exception:
            pass
        try:   # 保留最近 2 个（在用 + 新预热），更旧的删
            dirs = sorted([d for d in sd.iterdir() if d.is_dir() and (d / "node.exe").exists()],
                          key=lambda d: d.stat().st_mtime, reverse=True)
            for d in dirs[2:]:
                _sh.rmtree(d, ignore_errors=True)
        except Exception:
            pass
    except Exception:
        pass


def _payload(ready: bool, port: int):
    """路由返回值（元组修复 2026-10-15 用户实锤）：插件路由约定 (status, body)——
    原搬迁时照搬了内置端点的裸 dict 风格（FastAPI 端点可直接 return dict），漏包元组，
    _handler 的 `status, body = fn(...)` 把 dict 解包成键流 → too many values to unpack。"""
    out = {"ok": True, "port": port, "ready": ready}
    if not ready:
        out["hint"] = "VS Code Server 组件首次下载中（一次性）——页签保持打开会自动刷新，或稍后再点"
    return 200, out


_WS = None
_AGENT = None


def setup(api):
    global _WS, _AGENT
    _WS = api.workspace
    _AGENT = lambda: api.agent     # 每次现取（reload 后旧引用失效——属性即查即用）
    api.register_route("POST", "/api/webide/open", _open)
    api.log("webide 已装配（路由1）")
