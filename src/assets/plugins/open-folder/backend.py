from fastapi import Request
"""open-folder —— 插件后端：POST /api/open-folder/open（在跑 agt 的机器上弹文件管理器）。
原 server.py 内置端点 /api/open-workspace 的插件化搬迁（用户提案 2026-10-15：
内置 dock 功能抽成随包播种示例）。逻辑照搬：win=os.startfile / mac=open / linux=xdg-open。

2026-10-27 扩展（用户提案：资产控件悬停小按钮）：body 可带 {path, resolve_only}——
  · path 空/缺省 → 原 workspace 行为（不变）；
  · path 非空（相对拼 workspace，绝对原样）→ 打开其所在目录：文件=父目录选中该文件
    （win: explorer /select, · mac: open -R · linux: xdg-open 父目录），目录=直接打开；
  · resolve_only=true → 只解析返回绝对路径不弹窗（前端「复制完整路径」按钮取值用）。
  路径不存在返回 ok:false（前端 toast 报错）。"""
import os
import sys
import subprocess
from pathlib import Path

def _open_dir_or_parent(p: Path):
    """打开目录；p 是文件则打开父目录并尽量选中它（三平台兜底）。"""
    if sys.platform.startswith("win"):
        if p.is_dir():
            os.startfile(str(p))
        else:
            # /select, 打开父目录并选中该文件；逗号必须并入同一参数（explorer 列表形态）
            subprocess.Popen(["explorer", "/select," + str(p)])
    elif sys.platform == "darwin":
        subprocess.Popen(["open", "-R", str(p)] if p.is_file() else ["open", str(p)])
    else:
        subprocess.Popen(["xdg-open", str(p if p.is_dir() else p.parent)])


async def _open(request: 'Request'):
    """打开 workspace 文件夹 / 任意路径所在目录；resolve_only 只解析绝对路径。"""
    try:
        try:
            body = await request.json()
        except Exception:
            body = {}
        raw = str((body or {}).get("path") or "").strip()
        resolve_only = bool((body or {}).get("resolve_only"))
        p = Path(raw) if raw else _WS
        if raw and not p.is_absolute() and _WS is not None:
            p = _WS / p
        p = p.resolve()
        if not p.exists():
            return 200, {"ok": False, "error": f"路径不存在: {p}"}
        if resolve_only:
            return 200, {"ok": True, "path": str(p)}
        _open_dir_or_parent(p)
        return 200, {"ok": True, "path": str(p)}
    except Exception as e:
        return 200, {"ok": False, "error": f"{type(e).__name__}: {e}"}


_WS = None   # setup 时注入 api.workspace（handler 闭包晚绑定）


def setup(api):
    global _WS
    _WS = api.workspace
    api.register_route("POST", "/api/open-folder/open", _open)
    api.log("open-folder 已装配（路由1：workspace 打开 + 资产路径打开/解析）")
