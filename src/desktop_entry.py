"""desktop_entry.py —— 桌面版打包入口（PyInstaller Analysis 的入口脚本）。

职责（进入 chat.web_main 之前的桌面形态准备）：
1. AGT_DESKTOP=1：web_desktop/paths 据此开窗口 + 数据目录迁 %APPDATA%\\Agt
2. workspace 锚定 exe 旁 workspace/（explorer 双击 cwd=exe 目录，但快捷方式/
   开始菜单启动 cwd 可能是 System32——显式 chdir 消除歧义；首启自动创建）
3. run_python 子进程兼容：PyInstaller 冻结下 sys.executable=Agt.exe——
   子进程起 Agt.exe 会再跑一遍 GUI 入口（套娃）。用 --pyrun 入口分发：
   冻结环境子进程带 --pyrun <file> 参数 → 本入口分流直接 execfile，不进 GUI。
4. --selftest：打包产物 import 链自检（chat/config/server/web_desktop/paths +
   static/assets 资源就位）——CI/发布前自动验证，替代难自动化的 GUI 冒烟。

打包形态（spec s_d53311f8 修 #1）：Analysis pathex=src → 本文件及全部引擎模块以
【顶层名】收集（config/session/chat…，与 pip 运行时 src/__init__ 的 sys.path hack
同构）——所以这里 from chat import web_main（裸名），不是 from src.chat。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path


def _is_pyrun() -> bool:
    return len(sys.argv) >= 2 and sys.argv[1] == "--pyrun"


def _run_py_child() -> int:
    """子进程模式：agt 的 run_python 以 [sys.executable, tmp.py, ...] spawn——
    冻结环境 sys.executable 是 Agt.exe，本函数把 tmp.py 当 __main__ 执行。"""
    target = Path(sys.argv[2])
    sys.argv = [str(target)] + sys.argv[3:]
    os.environ["AGT_FROZEN_CHILD"] = "1"
    import runpy
    runpy.run_path(str(target), run_name="__main__")
    return 0


def _selftest() -> int:
    ok = True
    checks = []
    for mod in ("config", "paths", "session", "web_desktop", "chat", "server", "workflow_node_api"):
        try:
            __import__(mod)
            checks.append((mod, True, ""))
        except Exception as e:
            checks.append((mod, False, f"{type(e).__name__}: {e}"))
            ok = False
    base = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    for res in ("static/index.html", "assets/models.preset.json", "assets/nodes_builtin"):
        checks.append((res, (base / res).exists(), ""))
        ok = ok and (base / res).exists()
    # 节点插件动态加载自检（_import_fresh 路径——漏收集 workflow_node_api 时会在这里暴露）
    try:
        import importlib.util as _iu
        from pathlib import Path as _P
        np = _P(getattr(sys, "_MEIPASS", _P(sys.executable).parent)) / "assets" / "nodes_builtin"
        n_ok = 0
        for py in sorted(np.glob("*.py")):
            m = f"agent_node_sf_{py.stem}"
            spec = _iu.spec_from_file_location(m, py)
            mod = _iu.module_from_spec(spec)
            spec.loader.exec_module(mod)
            n_ok += 1
        checks.append((f"节点插件动态加载 {n_ok} 个", n_ok > 0, "" if n_ok > 0 else "0 个=目录缺失/放错路径"))
        ok = ok and n_ok > 0
    except Exception as e:
        checks.append(("节点插件动态加载", False, f"{type(e).__name__}: {e}"))
        ok = False
    for name, good, err in checks:
        print(("✅" if good else "❌"), name, err)
    print("SELFTEST_" + ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


def _redirect_stdio() -> None:
    """GUI 子系统（console=False）下 sys.stdout/stderr 是 None——uvicorn 的
    DefaultFormatter.__init__ 会调 sys.stdout.isatty() 直接 AttributeError
    （Unable to configure formatter 'default' → 启动即崩），print 同样炸。
    重定向到数据目录 logs/desktop.log（spec 约定"日志写文件"），顺带成为
    桌面版排障第一现场。stdin 一并兜底（uvicorn 不碰，某些库会）。"""
    if sys.stdout is not None and sys.stderr is not None:
        return
    from paths import resolve_agt_home
    log_dir = resolve_agt_home() / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    f = open(log_dir / "desktop.log", "a", buffering=1, encoding="utf-8")
    if sys.stdout is None:
        sys.stdout = f
    if sys.stderr is None:
        sys.stderr = f
    if sys.stdin is None:
        try:
            sys.stdin = open(os.devnull, "r")
        except OSError:
            pass


def main():
    if _is_pyrun():
        return _run_py_child()
    if "--selftest" in sys.argv[1:]:
        return _selftest()
    os.environ.setdefault("AGT_DESKTOP", "1")
    exe_dir = Path(sys.executable).resolve().parent
    ws = exe_dir / "workspace"
    ws.mkdir(exist_ok=True)
    os.chdir(ws)                       # workspace 锚定（快捷方式启动 cwd 歧义消除）
    _redirect_stdio()                  # 无控制台兜底：必须在 uvicorn 前挂好（L86）
    from chat import web_main
    web_main()
    return 0


if __name__ == "__main__":
    sys.exit(main())
