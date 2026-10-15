"""screenshot —— /screenshot <相对路径.png>：前端页面截图落盘（「以前端为主」插件范式示例）。

问题（用户提问 2026-10-15）：逻辑主要在前端 JS 的功能（如浏览器截图），插件怎么写？
范式：**后端命令只当触发器，重活在前端，后端路由当落点**——三座桥：

  ① 命令桥：api.register_command("screenshot") → handler 不干活，
     只 api.emit({"type":"screenshot_request","path":...}) 广播给前端（WS）
  ② 前端桥：frontend.js 里 AgtPlugin.ws.on("screenshot_request") →
     html2canvas 截图（库随包 vendor/ 经 /plugins-assets/screenshot/... 离线加载）
     → blob POST 回后端路由
  ③ 落盘桥：api.register_route(POST /api/screenshot/save) → 收 raw body 落盘
     WORKSPACE/<path>（防 ../ 穿越）→ api.emit(screenshot_done) → 前端聊天卡片+toast

安全：路径限制 workspace 内；命令只经输入框触发（dispatch 语义）；路由无鉴权
（与 /api/tool_call 同信任级：局域网内）。

改完本文件：/reload plugins 热生效（免重启）；frontend.js 改完刷新页面即生效。
"""
from pathlib import Path

_API = None   # setup(api) 时注入的 PluginAPI（命令 handler 里要用 api.emit）


def _cmd_screenshot(ctx, args):
    """/screenshot <相对路径> —— 请求前端截图（命令在哪个浏览器敲，就截那个页面的图）。"""
    p = ((args[0] if isinstance(args, list) and args else str(args or "")) or "").strip()
    if not p:
        print("用法：/screenshot captures/myShot.png   ← 截当前页面存到 workspace 相对路径")
        print("      （命令经 WS 广播给前端执行——那个页面上会弹出完成卡片）")
        return
    _API.emit({"type": "screenshot_request", "path": p})
    print(f"📸 已请求前端截图 → {p}（页面在线则稍后落盘并弹卡片；dock 的 📸 按钮可免命令直接截）")


async def _save(request):
    """POST /api/screenshot/save?path=xxx —— 收前端上传的 PNG blob 落盘（raw body）。"""
    body = await request.body()
    from real_tools import WORKSPACE
    p = (request.query_params.get("path") or "captures/screenshot.png").strip().replace("\\", "/")
    ws = Path(WORKSPACE).resolve()
    dst = (ws / p).resolve()
    if not str(dst).startswith(str(ws)):
        return 400, {"ok": False, "error": f"路径越界（限制 workspace 内）：{p}"}
    if not body:
        return 400, {"ok": False, "error": "空 body"}
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_bytes(body)
    _API.emit({"type": "screenshot_done", "path": p, "size": len(body)})
    return 200, {"ok": True, "path": p, "size": len(body), "abs": str(dst)}


def setup(api):
    """引擎装配期调用：①命令（触发器）②路由（落点）——前端部分在 frontend.js。"""
    global _API
    _API = api
    api.register_command(
        "screenshot", _cmd_screenshot,
        "<相对路径.png>  截当前 WebUI 页面存图（前端执行：html2canvas → 落盘）",
        "/screenshot captures/myShot.png\n"
        "  命令经 WS 广播给前端那个页面执行截图（所以截的是【你敲命令的那个浏览器】的界面）；\n"
        "  库随包 vendor/ 离线加载；dock 📸 按钮可直接截（自动命名 shot_<时间戳>.png）；\n"
        "  本插件是「后端命令触发 + 前端 JS 干活 + 后端路由落盘」三桥范式示例")
    api.register_route("POST", "/api/screenshot/save", _save)
