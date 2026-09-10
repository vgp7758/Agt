"""desktop_entry.py —— 桌面版打包入口（PyInstaller Analysis 的入口脚本）。

职责（进入 chat.web_main 之前的桌面形态准备）：
1. AGT_DESKTOP=1：桌面窗口模式（web_desktop）+ 数据根 ~/.agt（与 CLI 共用，paths 解析）
2. workspace 锚定（AGT_WORKSPACE env > --workspace 参数 > exe 旁 workspace/；
   explorer 双击 cwd=exe 目录，但快捷方式/开始菜单启动 cwd 可能是 System32——
   显式 chdir 消除歧义；首启自动创建）
3. run_python 子进程兼容：PyInstaller 冻结下 sys.executable=Agt.exe——
   子进程起 Agt.exe 会再跑一遍 GUI 入口（套娃）。用 --pyrun 入口分发：
   冻结环境子进程带 --pyrun <file> 参数 → 本入口分流直接 execfile，不进 GUI。
4. --selftest：产物 import 链自检（核心模块 + 资源就位）——CI/发布前门禁。
   不 import 任何 GUI 依赖模块（CI runner 无桌面会话也能确定性退出，实测坑：
   pywebview/pythonnet 在无桌面会话环境会挂住不返回 → Actions 任务被取消）。

打包形态（spec s_d53311f8 修 #1）：Analysis pathex=src → 本文件及全部引擎模块以
【顶层名】收集（config/session/chat…，与 pip 运行时 src/__init__ 的 sys.path hack
同构）——所以这里 from chat import web_main（裸名），不是 from src.chat。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path


def _force_utf8_stdio() -> None:
    """冻结环境编码兜底（必须在任何引擎模块 import 之前跑）。

    CI/英文 Windows 的 ANSI=cp1252：引擎模块 import 时的中文/emoji print
    （config 首装引导、session/workflow 加载日志…）直接 UnicodeEncodeError →
    模块 import 失败（CI selftest 7 个模块 ❌ 的实测根因）。且 PyInstaller
    windowed bootloader 的模拟 stdio 不读 PYTHONIOENCODING（env 兜底无效，
    本地 UTF-8 系统无法复现）——唯一可靠位置是 Python 层强制 reconfigure。
    GUI 路径 stdio=None 由 _redirect_stdio 接管（utf-8 文件），此处自然跳过。"""
    for _s in (sys.stdout, sys.stderr):
        if _s is not None:
            try:
                _s.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass


_force_utf8_stdio()


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
    """产物自检。**用途是 CI/发布前门禁**——不需要任何 GUI 依赖，必须确定性退出。

    历史坑：曾 import web_desktop（pywebview）——CI runner 上 pywebview 的
    pythonnet/.NET 初始化在某些环境会挂住不返回（本地有 WebView2 就没事），
    表现为 selftest 步骤永远不结束 → Actions 任务被取消。改为**只 import 无
    GUI 依赖的核心模块**：真产物的问题（模块漏收集 / 资源缺 / 插件加载失败）
    照样全暴露，但永远不会吊死流水线。
    """
    ok = True
    checks = []
    for mod in ("config", "paths", "session", "chat", "server", "workflow_node_api",
                "workflow", "multiagent", "llm_client", "tools"):
        try:
            __import__(mod)
            checks.append((mod, True, ""))
        except Exception as e:
            checks.append((mod, False, f"{type(e).__name__}: {e}"))
            ok = False
    base = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    for res in ("static/index.html", "static/agents.html", "assets/models.preset.json",
                "assets/nodes_builtin", "assets/tools_builtin", "assets/manifest.json"):
        feat = (base / res).exists()
        checks.append((res, feat, ""))
        ok = ok and feat
    # 节点插件动态加载自检（_import_fresh 路径——漏收集 workflow_node_api 时会在这里暴露）
    try:
        import importlib.util as _iu
        from pathlib import Path as _P
        np = _P(getattr(sys, "_MEIPASS", _P(sys.executable).parent)) / "assets" / "nodes_builtin"
        n_ok = 0
        errs = []
        for py in sorted(np.glob("*.py")):
            m = f"agent_node_sf_{py.stem}"
            try:
                spec = _iu.spec_from_file_location(m, py)
                mod = _iu.module_from_spec(spec)
                spec.loader.exec_module(mod)
                n_ok += 1
            except Exception as e:
                errs.append(f"{py.stem}:{type(e).__name__}")
        checks.append((f"节点插件动态加载 {n_ok} 个", n_ok > 0, ",".join(errs[:3])))
        ok = ok and n_ok > 0
    except Exception as e:
        checks.append(("节点插件动态加载", False, f"{type(e).__name__}: {e}"))
        ok = False
    # 外置工具脚本可编译自检（同属"随包数据被动态加载"的一类）
    try:
        import py_compile
        from pathlib import Path as _P2
        tp = _P2(getattr(sys, "_MEIPASS", _P2(sys.executable).parent)) / "assets" / "tools_builtin"
        t_ok, t_err = 0, []
        for py in sorted(tp.glob("*.py")):
            try:
                py_compile.compile(str(py), doraise=True, cfile=str(py) + ".pyc")
                t_ok += 1
            except Exception as e:
                t_err.append(f"{py.stem}:{type(e).__name__}")
        checks.append((f"外置工具脚本校验 {t_ok} 个", t_ok > 0, ",".join(t_err[:3])))
        ok = ok and t_ok > 0
    except Exception as e:
        checks.append(("外置工具脚本校验", False, f"{type(e).__name__}: {e}"))
        ok = False
    # 明细落文件（cwd/selftest_result.txt）：GUI 子系统的 exe 在 CI 管道重定向下
    # stdout 可能只出最后一行（实测：Write-Host $out 丢了全部 ✅/❌）——文件是
    # 确定性通道，CI 门禁 cat 它拿完整明细。
    lines = [(("✅" if good else "❌"), name, err) for name, good, err in checks]
    lines.append(("SUMMARY", "SELFTEST_" + ("PASS" if ok else "FAIL"), ""))
    try:
        import os as _os
        with open(_os.path.join(_os.getcwd(), "selftest_result.txt"), "w", encoding="utf-8") as f:
            for a, b, c in lines:
                f.write(f"{a} {b} {c}\n".rstrip() + "\n")
    except Exception:
        pass
    for a, b, c in lines:
        try:
            print(a, b, c, flush=True)
        except Exception:
            pass   # 输出编码问题不该让自检本身崩（CI 门禁看文件 + SELFTEST_*）
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


def _pick_workspace(exe_dir: Path) -> Path:
    """workspace 解析优先级：AGT_WORKSPACE env（launcher/看门狗传）> --workspace <path>
    参数 > exe 旁默认 workspace/。chdir 必须在 import 引擎之前（锚定机制）。
    env 不 pop——/restart 继承 env 时新进程保持同 workspace（重启不换区语义）。"""
    target = os.environ.get("AGT_WORKSPACE", "").strip()
    if not target:
        argv = sys.argv[1:]
        for i, a in enumerate(argv):
            if a == "--workspace" and i + 1 < len(argv):
                target = argv[i + 1]
                break
    ws = Path(target).expanduser().resolve() if target else exe_dir / "workspace"
    ws.mkdir(parents=True, exist_ok=True)
    return ws


def main():
    if _is_pyrun():
        return _run_py_child()
    if "--selftest" in sys.argv[1:]:
        return _selftest()
    os.environ.setdefault("AGT_DESKTOP", "1")
    exe_dir = Path(sys.executable).resolve().parent
    os.chdir(_pick_workspace(exe_dir))   # workspace 锚定（env/参数 > exe 旁默认；须在 import 引擎前）
    _redirect_stdio()                  # 无控制台兜底：必须在 uvicorn 前挂好（L86）
    from chat import web_main
    web_main()
    return 0


if __name__ == "__main__":
    sys.exit(main())
