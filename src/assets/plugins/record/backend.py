"""record —— /record <start|pause|resume|stop|save|flush>（无参=状态）：WebUI 页面录屏插件。

定位（用户提案 2026-10-15：「复杂一点的示例插件」）——比 screenshot 高一档的三桥范式示例，
新增四个复杂度维度：
  · 子命令状态机：idle → recording ⇄ paused → blobReady（save 可多次存不同路径）→ flush 清
  · 前端长生命周期对象：MediaRecorder 跨命令存活（命令只是发令，对象在前端）
  · 浏览器激活限制：getDisplayMedia 需要 user activation——WS 事件回调不算，命令桥
    start 时前端弹「点击确认」浮条拿激活再唤起选屏（标准 workaround，注释见 frontend.js）
  · 大 blob 回传 + 动态容器：视频 MB 级 POST；容器按浏览器能力定（mp4/avc1 优先，
    回退 webm），save 时扩展名与实际格式自动对齐（保真实可播放）

三桥（同 screenshot 范式，见该插件注释）：
  ① 命令桥：register_command("/record") → emit(record_request{action, path})
  ② 前端桥：frontend.js 状态机驱动 MediaRecorder（pause/resume 原生映射）
  ③ 落盘桥：POST /api/record/save?path=&fmt= → raw body 落盘（防穿越）→
     emit(record_done) → 前端聊天卡片「🎥 已保存 …（N MB）」

特性：命令在哪个浏览器敲就录那个页面（emit 广播、执行端=敲命令的页面）；
音频默认关（聊天页通常静音，开音频在 getDisplayMedia 里自选）。

改 backend：/reload plugins 热生效；改 frontend：刷新页面即生效。
"""
from pathlib import Path

_API = None

_USAGE = ("用法：/record <start|pause|resume|stop|save|flush> [保存相对路径]\n"
          "/record start   开始录制（前端会弹浏览器选屏授权）\n"
          "/record pause | resume   暂停 / 恢复\n"
          "/record stop    结束录制（内容暂存内存，待 save）\n"
          "/record save tmp/myRecord.mp4   落盘（扩展名按实际容器自动对齐 webm/mp4）\n"
          "/record flush   丢弃暂存的录制\n"
          "/record         查看录制状态（在前端 toast 显示）")

_HINT = {
    "start": "🎬 已请求前端开始录制（浏览器将弹出选屏授权；dock 🔴 按钮可免命令直启）",
    "pause": "⏸ 已请求暂停", "resume": "▶ 已请求恢复录制",
    "stop": "⏹ 已请求结束录制——内容暂存内存，用 /record save <路径> 落盘",
    "flush": "🗑 已请求丢弃暂存录制",
}


_VALID_ACTIONS = {"start", "pause", "resume", "stop", "save", "flush", "status"}


def _cmd_record(ctx, args):
    argv = args if isinstance(args, list) else str(args or "").split()
    action = (argv[0] if argv else "status").lower()
    path = (argv[1] if len(argv) > 1 else "").strip()
    # 判据用显式合法集（2026-10-15 用户实锤：save 不在 _HINT 键里——_HINT 只是"提示文案
    # 表"，save 的文案在下方 elif 单独处理——按 _HINT 成员资格判定会把 save 误报未知子命令）
    if action not in _VALID_ACTIONS:
        print(f"❌ 未知子命令 /record {action}\n{_USAGE}")
        return
    if action == "save" and not path:
        print("❌ save 需要相对路径，如：/record save tmp/myRecord.mp4")
        return
    _API.emit({"type": "record_request", "action": action, "path": path})
    if action == "status":
        print("📊 已请求前端回报录制状态（在前端页面 toast 显示；跨页面各自独立录制会话）")
    elif action == "save":
        print(f"💾 已请求前端上传暂存录制 → {path}（完成后弹卡片）")
    else:
        print(_HINT[action])


async def _save(request):
    """POST /api/record/save?path=xx&fmt=webm|mp4 —— 收前端上传的录制 blob 落盘。"""
    body = await request.body()
    from real_tools import WORKSPACE
    p = (request.query_params.get("path") or "").strip().replace("\\", "/")
    fmt = (request.query_params.get("fmt") or "webm").lower()
    if not p or not body:
        return 400, {"ok": False, "error": "缺少 path 或 body 为空"}
    import os as _os
    root, ext = _os.path.splitext(p)
    if ext.lower() != "." + fmt:            # 容器对齐：.mp4 请求但实际 webm → 改扩展名（保可播放）
        p = root + "." + fmt
    ws = Path(WORKSPACE).resolve()
    dst = (ws / p).resolve()
    if not str(dst).startswith(str(ws)):
        return 400, {"ok": False, "error": f"路径越界（限制 workspace 内）：{p}"}
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_bytes(body)
    _API.emit({"type": "record_done", "path": p, "size": len(body)})
    return 200, {"ok": True, "path": p, "size": len(body), "abs": str(dst)}


def setup(api):
    """引擎装配期调用：命令（发令）+ 路由（落盘）——状态机在前端 frontend.js。"""
    global _API
    _API = api
    api.register_command(
        "record", _cmd_record,
        "<start|pause|resume|stop|save|flush>  WebUI 页面录屏（前端 MediaRecorder；stop 后 save 落盘）",
        _USAGE + "\n  本插件是三桥范式的进阶示例：子命令状态机 + 浏览器激活处理 + 大文件回传")
    api.register_route("POST", "/api/record/save", _save)
