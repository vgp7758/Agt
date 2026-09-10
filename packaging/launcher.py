"""launcher.py —— 桌面版瘦启动器（spec s_37494daf）：先选 workspace 再拉起 Agt.exe。

纯标准库（tkinter + subprocess + json），不 import 引擎任何模块——独立 onefile
打包（Launcher.spec，~11MB）。双击 launcher：最近工作区列表 + 浏览选目录 → 以
选定目录启动主程序（AGT_WORKSPACE env + cwd）→ launcher 自退。双击 Agt.exe 则
直接进默认 workspace（exe 旁 workspace/）——两入口共存。

recent 列表存 %APPDATA%\\Agt\\recent_workspaces.json（与 paths.resolve_agt_home
桌面分支同语义，launcher 不 import paths 保持零依赖）。
--auto <dir>：跳过 UI 直接启动指定目录（自动化验证 / 高级用法）。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

RECENT_MAX = 8


def _data_dir() -> Path:
    base = os.environ.get("APPDATA") or str(Path.home())
    d = Path(base) / "Agt"
    try:
        d.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return d


def _recent_file() -> Path:
    return _data_dir() / "recent_workspaces.json"


def _load_recent() -> list[str]:
    try:
        data = json.loads(_recent_file().read_text(encoding="utf-8"))
        items = [p for p in data.get("recent", []) if isinstance(p, str)]
        return [p for p in items if Path(p).is_dir()][:RECENT_MAX]   # 消失的目录静默滤除
    except Exception:
        return []


def _save_recent(ws: str) -> None:
    target = str(Path(ws).resolve())
    items = [p for p in _load_recent() if p.lower() != target.lower()]
    items.insert(0, target)
    try:
        _recent_file().write_text(
            json.dumps({"recent": items[:RECENT_MAX]}, ensure_ascii=False, indent=1),
            encoding="utf-8")
    except OSError:
        pass   # 写不了 recent 不阻塞启动


def _main_exe() -> Path:
    """定位主程序（三形态）：
    ① AGT_MAIN_EXE env 覆盖（测试）
    ② Launcher 旁 Agt.exe（平铺形态）
    ③ Launcher 旁 Agt\\Agt.exe（onedir 发布包形态——zip 解压后 Launcher 与
       Agt\ 文件夹平级；CI 实测坑：只认①时报 D:\tmp\dist\Agt\Agt.exe 找不到）
    ④ 源码态 repo 根 dist/Agt/Agt.exe（packaging/launcher.py 开发验证路径）。"""
    env = os.environ.get("AGT_MAIN_EXE", "").strip()
    if env:
        return Path(env)
    here = Path(sys.executable if getattr(sys, "frozen", False) else __file__).resolve().parent
    for cand in (here / "Agt.exe", here / "Agt" / "Agt.exe"):
        if cand.exists():
            return cand
    return here.parent / "dist" / "Agt" / "Agt.exe"


def _launch(ws: str) -> None:
    here = Path(sys.executable if getattr(sys, "frozen", False) else __file__).resolve().parent
    exe = _main_exe()
    if not exe.exists():
        messagebox.showerror("Agt 启动器", f"未找到主程序，合法位置（任选其一）：\n"
                                       f"  ① Launcher 旁 Agt.exe\n"
                                       f"  ② Launcher 旁 Agt\\Agt.exe（发布包默认布局）\n\n"
                                       f"当前在 {here} 找过都未命中。")
        return
    _save_recent(ws)
    env = dict(os.environ)
    env["AGT_WORKSPACE"] = str(Path(ws).resolve())
    env["AGT_DESKTOP"] = "1"
    flags = subprocess.DETACHED_PROCESS if os.name == "nt" else 0
    subprocess.Popen([str(exe)], cwd=env["AGT_WORKSPACE"], env=env,
                     creationflags=flags, close_fds=True)
    sys.exit(0)


class LauncherUI:
    def __init__(self, root: tk.Tk):
        self.root = root
        root.title("Agt — 选择工作区")
        root.geometry("620x340")
        root.minsize(480, 260)
        ttk.Label(root, text="选择一个工作区（session / 记忆 / wiki 按目录隔离）：",
                  padding=(12, 10, 12, 4)).pack(anchor="w")

        frame = ttk.Frame(root, padding=(12, 0, 12, 8))
        frame.pack(fill="both", expand=True)
        self.list = ttk.Treeview(frame, columns=("ws",), show="headings", selectmode="browse")
        self.list.heading("ws", text="最近使用的工作区")
        self.list.column("ws", stretch=True)
        sb = ttk.Scrollbar(frame, orient="vertical", command=self.list.yview)
        self.list.configure(yscrollcommand=sb.set)
        self.list.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.list.bind("<Double-1>", lambda e: self._open_selected())

        btns = ttk.Frame(root, padding=(12, 4, 12, 12))
        btns.pack(fill="x")
        ttk.Button(btns, text="打开", command=self._open_selected).pack(side="right", padx=(8, 0))
        ttk.Button(btns, text="浏览…", command=self._browse).pack(side="right")
        ttk.Label(btns, text="双击列表项可直接打开").pack(side="left")

        self.paths = _load_recent()
        for i, p in enumerate(self.paths):
            self.list.insert("", "end", values=("★ " + p if i == 0 else p,))
        if not self.paths:
            root.after(150, self._browse)   # 空列表：直接弹目录选择

    def _open_selected(self) -> None:
        sel = self.list.selection()
        if not sel:
            return
        idx = self.list.index(sel[0])
        self._go(self.paths[idx])

    def _browse(self) -> None:
        d = filedialog.askdirectory(title="选择工作区文件夹", parent=self.root)
        if d:
            self._go(d)
        elif not self.paths:
            self.root.destroy()   # 无 recent 且取消浏览 → 退出（无主程序可启）

    def _go(self, ws: str) -> None:
        if not Path(ws).is_dir():
            messagebox.showwarning("Agt 启动器", f"目录不存在：\n{ws}")
            return
        self.root.destroy()
        _launch(ws)


def main():
    # --auto <dir>：无 UI 直启（自动化验证 / 脚本用法）
    if len(sys.argv) >= 3 and sys.argv[1] == "--auto":
        ws = Path(sys.argv[2]).resolve()
        if not ws.is_dir():
            print(f"[launcher] 目录不存在：{ws}", file=sys.stderr)
            return 2
        _save_recent(str(ws))
        exe = _main_exe()
        if not exe.exists():
            print(f"[launcher] 未找到主程序：{exe}", file=sys.stderr)
            return 2
        env = dict(os.environ)
        env["AGT_WORKSPACE"] = str(ws)
        env["AGT_DESKTOP"] = "1"
        flags = subprocess.DETACHED_PROCESS if os.name == "nt" else 0
        subprocess.Popen([str(exe)], cwd=str(ws), env=env,
                         creationflags=flags, close_fds=True)
        print(f"[launcher] 已启动 {exe.name} @ {ws}")
        return 0
        # 注意：--auto 不 sys.exit(0)（走 return，供脚本断言输出）
    root = tk.Tk()
    try:
        from tkinter import font as tkfont
        default = tkfont.nametofont("TkDefaultFont")
        default.configure(size=10)
    except Exception:
        pass
    LauncherUI(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
