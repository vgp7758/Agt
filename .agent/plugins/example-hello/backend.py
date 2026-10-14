"""example-hello —— 插件 SDK 后端入口（全 seam 演示）。

约定：模块级 setup(api) 由引擎在装配期调用（plugins.load_plugins）；
api 为 PluginAPI（register_tool / register_command / register_route /
on_tool_pre_execute / on_tool_post_execute / emit / agent / workspace）。
改完本文件：/reload plugins 热生效（免重启）。
"""

_STATS = {"tool_calls": 0, "denied": 0, "greeted": []}


def _greet(name: str, style: str = "友好") -> str:
    """向指定对象打招呼（插件示例工具）。name: 名字；style: 风格（友好/正式/极客）。"""
    _STATS["greeted"].append(name)
    tpl = {"友好": "你好，{n}！很高兴见到你 😊",
           "正式": "您好，{n}。幸会。",
           "极客": "Hello, {n}. [handshake] 0x48 0x65 0x6c 0x6c 0x6f"}.get(style, "你好，{n}")
    return tpl.format(n=name or "陌生人") + f"（来自插件 example-hello，已累计招呼 {len(_STATS['greeted'])} 次）"


def _hello_ping():
    """演示插件的状态查询：返回插件统计（工具调用数/拦截数/招呼名单）。"""
    import json
    return json.dumps(_STATS, ensure_ascii=False)


def _cmd_phello(ctx, args):
    """/phello [名字] —— 插件命令演示（打招呼 + 广播一条自定义事件到前端卡片）。"""
    name = " ".join(args).strip() or "世界"
    msg = _greet(name)
    print(msg)
    try:
        _CMD_API.emit({"type": "example_hello_event", "title": f"招呼已发出 → {name}", "detail": msg})
    except Exception:
        pass
    print("（已同步广播 example_hello_event 事件——前端聊天区应出现插件卡片）")


_CMD_API = None   # setup 时注入（命令 handler 闭包晚绑定用）


async def _route_ping(request):
    """插件路由演示：POST /api/example-hello/ping → {"pong": true, ...}。"""
    return 200, {"pong": True, "plugin": "example-hello", "stats": _STATS}


def setup(api):
    """插件装配入口（引擎调用）。全部注册可逆：disable/reload 时自动撤销。"""
    global _CMD_API
    _CMD_API = api
    # ① 注册型 seam
    api.register_tool("hello_greet", _greet, description="向指定对象打招呼（插件 example-hello 提供）")
    api.register_tool("hello_ping", _hello_ping, description="查询插件 example-hello 的统计（招呼名单/拦截数）")
    api.register_command("phello", _cmd_phello,
                         help_text="<名字>  插件示例：打招呼并广播前端卡片事件")
    api.register_route("POST", "/api/example-hello/ping", _route_ping)
    # ② 事件型 seam（工具执行管线）
    def _pre(name, arguments):
        # 演示 deny：调用 hello_deny_me 会被本插件拒绝（管线护栏示例）
        if name == "hello_deny_me":
            _STATS["denied"] += 1
            return {"deny": "演示拒绝：hello_deny_me 是插件护栏示例工具，永远被拒"}
        _STATS["tool_calls"] += 1
        return None
    api.on_tool_pre_execute(_pre)

    def _post(name, arguments, result):
        # 观察 seam：返回 None = 不改写结果（纯观察）。这里给 hello_greet 记一条插件日志。
        if name == "hello_greet":
            api.log(f"观察到 hello_greet({arguments.get('name', '')}) 结果 {len(str(result))} 字")
        return None
    api.on_tool_post_execute(_post)
    api.log("example-hello 已装配（工具2/命令1/路由1/钩子2）")
