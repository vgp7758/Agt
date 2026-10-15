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

    _SW_MARKS = ("visual studio code", "serve-web", "vscode", "code-server")

    def _probe(port):
        """返回 (alive, ready, ours)：alive=口上有 HTTP 服务；ready=工作台就绪；
        ours=疑似我们的 serve-web（202 下载页/200 工作台都含 vscode 特征）。
        用户实锤 2026-10-15："Unauthorized access"——Unity Hub 固定占 39000，
        与「WebUI+30000」潜规则口撞车；192.x 上 401 也曾在 urllib 侧表现为断连。
        判据不能只看"连得上"：非 serve-web 的服务（401/403/无特征页）不算 ours → 不复用。"""
        try:
            with _ur.urlopen(f"http://127.0.0.1:{port}/", timeout=3) as r:
                body = (r.read() or b"").decode("utf-8", "ignore")
                low = body.lower()
                ours = any(m in low for m in _SW_MARKS)
                ready = len(body) > 500 and "unauthorized" not in low
                return True, ready, ours
        except _ur.HTTPError as e:     # 4xx/5xx：读 body 判特征，别一律当"别人的服务"
            # serve-web CLI 自己在【启动中】时会返回 503 "Error connecting to server:
            # hyper::Error(IncompleteMessage)"（server 子进程还在起）——这是我们的服务！
            # 而 Unity Hub 占口是 401 "Unauthorized access"→ 才算别人的（用户实锤 2026-10-15）。
            body = ""
            try:
                body = (e.read() or b"").decode("utf-8", "ignore")
            except Exception:
                pass
            low = body.lower()
            ours = (any(m in low for m in _SW_MARKS)
                    or "hyper::error" in low or "connecting to server" in low)
            return True, False, ours
        except Exception:              # 拒绝连接/断连：口空
            return False, False, False

    def _alive(port):
        return _probe(port)[0]

    def _ready(port):
        return _probe(port)[1]

    def _bindable(port):
        s = _sk.socket()
        try:
            s.bind(("0.0.0.0", port))
            return True
        except OSError:
            return False
        finally:
            s.close()

    # ① 潜规则口「有我们的 serve-web 在」→ 直接复用（下载中(202)/工作台(200)都算服务在，页签自己等）
    #    —— 仅当确认是本插件（或同款 serve-web）的服务才复用；别的程序占口（如 Unity Hub 401）→ 走 ② 扫口
    _a, _rd, _ours = _probe(base_port)
    if _a and _ours:
        return _payload(_rd, base_port)
    # ② 选口：潜规则口可 bind 用它；被占向后扫 +1..+5
    PORT = next((p for p in [base_port + i for i in range(6)] if _bindable(p)), None)
    if PORT is None:
        return 200, {"ok": False, "error": f"端口 {base_port}..{base_port + 5} 均被占用且无可复用服务"
                     f"（疑似 serve-web 孤儿进程或 Windows 动态保留——重启系统可重置）"}
    # --default-folder 直开工作区（勿用 ?folder= URL 参数——serve-web 1.134 会误路由成
    # "远程代理"会话，标签页名变成 l10n 字面量，用户实锤）+ --commit-id 钉缓存版本
    # 缓存体检（2026-10-15 事故修复）：只认【完整】安装（node.exe + out/server-main.js）。
    # 半残目录（并发安装被删剩 node.exe）会让 CLI spawn "bin\..\out\server-main.js"
    # 报 "The system cannot find the path specified." 并无限重试 → 当场清理，且不拿它当 --commit-id。
    _cid = ""
    _cached_now: set = set()
    try:
        import pathlib as _pl
        _sd = _pl.Path.home() / ".vscode" / "cli" / "serve-web"
        _dirs = [d for d in _sd.iterdir() if d.is_dir() and not d.name.startswith(".")]
        _good = [d for d in _dirs if _dir_ok(d)]
        for d in _dirs:
            if not _dir_ok(d):
                _drop_dir(d)          # rename 探测占用：使用中的删不掉，自然跳过
        if _good:
            _cached_now = {d.name for d in _good}
            _cid = max(_good, key=lambda d: d.stat().st_mtime).name
    except Exception:
        pass
    _exe = _cli_exe()
    _pre = f'"{_exe}"' if _exe else 'code'      # 有全路径就不经 shell（kill 才杀得干净）
    cmd = (f'{_pre} serve-web --host 0.0.0.0 --port {PORT} --without-connection-token '
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
        _sp.Popen(cmd, shell=(_cli_exe() is None),   # 全路径可用就不经 shell（杀得干净）
                  creationflags=getattr(_sp, "CREATE_NO_WINDOW", 0))
    t0 = time.time()
    while time.time() - t0 < 12:   # 12s 上限：非首次秒起；首次下载交给页签自动刷新
        if _ready(PORT):
            return _payload(True, PORT)
        await asyncio.sleep(2)
    # 超时：口空着 = 首次下载中（正常，页签自己等下载完刷新）；
    #       口上却是别的程序的服务 = 拉起的 serve-web 没起来、口被抢（如 Unity Hub 固定占）→ 报错，
    #       别让前端打开一个别人的页面（用户实锤 2026-10-15 "Unauthorized access"）
    _a2, _, _ours2 = _probe(PORT)
    if _a2 and not _ours2:
        return 200, {"ok": False, "error": f"端口 {PORT} 上的服务不是 VS Code（被其他程序占用）——"
                     f"常见于 Unity Hub 固定占用 {base_port}，请关闭该程序或更换 WebUI 端口后重试"}
    return _payload(False, PORT)


async def _prefetch(cached_hashes: set):
    """组件滚动预热（浏览器双通道更新同款）：旧版秒起的同时后台拉最新 stable 到缓存，
    轮询出现【完整】新版本 → kill 预热进程 → 清理超出最近 2 个的旧版。

    2026-10-15 事故修复（三个坑，用户实锤 WebIDE 无限 panic 日志）：
    ① shell=True 起进程 → pre.kill() 只杀 cmd.exe，code-tunnel.exe 变孤儿（曾堆 6 个，
       每个都在下载组件、抢占同一缓存目录）→ 改全路径直起 + 单例锁 _PREFETCHING；
    ② "下载完成"曾判 node.exe 存在 → node.exe 先落地就误判完成 → 改判 _dir_ok（含 out/）；
    ③ 清理曾 rmtree 到【正在使用】的版本 → 目录被删剩 node.exe（占用删不掉）→ 之后 CLI
       spawn 路径不存在 → 无限重试 → 改 _drop_dir（rename 探测，占用即跳过）。"""
    global _PREFETCHING
    if _PREFETCHING:          # 单例：同时只允许一路预热（防并发安装互踩）
        return
    _PREFETCHING = True
    import subprocess as _sp
    import pathlib as _pl
    try:
        sd = _pl.Path.home() / ".vscode" / "cli" / "serve-web"
        # 预热端口交给 CLI 随机挑（--port 0，见下）——不再扫 39990+ 固定段，免撞旧孤儿占位
        _exe = _cli_exe()          # 全路径直起（不经 shell → kill/taskkill 杀得干净）
        _pre = f'"{_exe}"' if _exe else 'code'
        pre = _sp.Popen(f'{_pre} serve-web --host 127.0.0.1 --port 0 --without-connection-token '
                        '--accept-server-license-terms',
                        shell=(_exe is None),
                        creationflags=getattr(_sp, "CREATE_NO_WINDOW", 0))
        t0 = time.time()
        while time.time() - t0 < 600:   # 10 分钟上限（慢网可容）
            await asyncio.sleep(15)
            try:
                done = any(_dir_ok(d) and d.name not in cached_hashes for d in sd.iterdir())
            except Exception:
                done = False
            if done:
                break
        try:                            # 真杀（全路径直起时 kill 即够；兜底 taskkill /t）
            pre.kill()
            pre.wait(timeout=10)
        except Exception:
            try:
                _sp.run(f"taskkill /f /t /pid {pre.pid}", shell=True, capture_output=True, timeout=20)
            except Exception:
                pass
        try:   # 保留最近 2 个【完整】版本；半残的顺手清（占用中的 _drop_dir 会跳过）
            dirs = sorted([d for d in sd.iterdir() if _dir_ok(d)],
                          key=lambda d: d.stat().st_mtime, reverse=True)
            for d in dirs[2:]:
                _drop_dir(d)
            for d in [x for x in sd.iterdir()
                      if x.is_dir() and not x.name.startswith(".") and not _dir_ok(x)]:
                _drop_dir(d)
        except Exception:
            pass
    except Exception:
        pass
    finally:
        _PREFETCHING = False


def _payload(ready: bool, port: int):
    """路由返回值（元组修复 2026-10-15 用户实锤）：插件路由约定 (status, body)——
    原搬迁时照搬了内置端点的裸 dict 风格（FastAPI 端点可直接 return dict），漏包元组，
    _handler 的 `status, body = fn(...)` 把 dict 解包成键流 → too many values to unpack。"""
    out = {"ok": True, "port": port, "ready": ready}
    if not ready:
        out["hint"] = "VS Code Server 组件首次下载中（一次性）——页签保持打开会自动刷新，或稍后再点"
    return 200, out


# ---- serve-web 缓存/进程工具（2026-10-15 事故修复）----
_PREFETCHING = False


def _dir_ok(d):
    """完整安装判据：node.exe + out/server-main.js 都在。
    只判 node.exe 会被"半残"目录骗过（并发安装/误删后只剩 node.exe）。"""
    try:
        return (d / "node.exe").exists() and (d / "out" / "server-main.js").exists()
    except Exception:
        return False


def _drop_dir(d):
    """安全删缓存目录：先 rename 再 rmtree——被占用（rename 失败）则放弃、留给下一轮，
    绝不硬删正在使用的版本（那会把目录删剩 node.exe，制造 spawn 失败死循环）。"""
    import shutil as _sh
    try:
        t = d.with_name("." + d.name + ".deleting")   # "." 前缀 → 被 _dirs 过滤，避免"删一半残留"被当候选
        if t.exists():
            _sh.rmtree(t, ignore_errors=True)
            if t.exists():
                return False
        d.rename(t)
    except Exception:
        return False
    _sh.rmtree(t, ignore_errors=True)
    return not t.exists()


def _cli_exe():
    """serve-web CLI 可执行文件全路径（有它就地直起、不经 shell → kill/taskkill 杀得干净）。"""
    import shutil as _sh, pathlib as _pl, os as _os
    for n in ("code-tunnel", "code-tunnel.exe"):
        p = _sh.which(n)
        if p:
            return p
    for n in ("code", "code.cmd"):
        c = _sh.which(n)
        if c:
            d = _pl.Path(c).parent
            for cand in ("code-tunnel.exe", "code-tunnel"):
                if (d / cand).exists():
                    return str(d / cand)
    la = _os.environ.get("LOCALAPPDATA") or ""
    for pat in ("Programs/Microsoft VS Code*/bin/code-tunnel.exe",):
        try:
            for p in _pl.Path(la).glob(pat):
                return str(p)
        except Exception:
            pass
    return None


_WS = None
_AGENT = None


def setup(api):
    global _WS, _AGENT
    _WS = api.workspace
    _AGENT = lambda: api.agent     # 每次现取（reload 后旧引用失效——属性即查即用）
    api.register_route("POST", "/api/webide/open", _open)
    api.log("webide 已装配（路由1）")
