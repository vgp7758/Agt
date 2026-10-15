from fastapi import Request
"""open-folder —— 插件后端：POST /api/open-folder/open（在跑 agt 的机器上弹文件管理器）。
原 server.py 内置端点 /api/open-workspace 的插件化搬迁（用户提案 2026-10-15：
内置 dock 功能抽成随包播种示例）。逻辑照搬：win=os.startfile / mac=open / linux=xdg-open。"""
import os
import sys
import subprocess


async def _open(request: 'Request'):
    """打开 workspace 文件夹（三平台兜底）。"""
    try:
        p = str(_WS.resolve())
        if sys.platform.startswith("win"):
            os.startfile(p)                       # Windows Explorer
        elif sys.platform == "darwin":
            subprocess.Popen(["open", p])
        else:
            subprocess.Popen(["xdg-open", p])
        return 200, {"ok": True, "path": p}
    except Exception as e:
        return 200, {"ok": False, "error": f"{type(e).__name__}: {e}"}


_WS = None   # setup 时注入 api.workspace（handler 闭包晚绑定）


def setup(api):
    global _WS
    _WS = api.workspace
    api.register_route("POST", "/api/open-folder/open", _open)
    api.log("open-folder 已装配（路由1）")
