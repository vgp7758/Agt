"""desktop_entry.py —— 桌面版打包入口（PyInstaller Analysis 的入口脚本）。

职责（进入 src.chat.web_main 之前的桌面形态准备）：
1. AGT_DESKTOP=1：web_desktop/paths 据此开窗口 + 数据目录迁 %APPDATA%\\Agt
2. workspace 锚定 exe 旁 workspace/（explorer 双击 cwd=exe 目录，但快捷方式/
   开始菜单启动 cwd 可能是 System32——显式 chdir 消除歧义；首启自动创建）
3. run_python 子进程兼容：PyInstaller 冻结下 sys.executable=Agt.exe——
   子进程起 Agt.exe 会再跑一遍 GUI 入口（套娃）。用 AGT_PY_RUN 入口分发：
   冻结环境子进程带 --pyrun <file> 参数 → 本入口分流直接 execfile，不进 GUI。
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


def main():
    if _is_pyrun():
        return _run_py_child()
    os.environ.setdefault("AGT_DESKTOP", "1")
    exe_dir = Path(sys.executable).resolve().parent
    ws = exe_dir / "workspace"
    ws.mkdir(exist_ok=True)
    os.chdir(ws)                       # workspace 锚定（快捷方式启动 cwd 歧义消除）
    from src.chat import web_main
    web_main()
    return 0


if __name__ == "__main__":
    sys.exit(main())
