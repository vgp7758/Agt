"""misc_tools.py —— 算术/等待/评分类脚本工具（原 LIGHT_TOOLS 纯函数型外置件）。

agt_register() 返回描述符列表（script_tools.py 扫描注册约定）。
改完本文件用 /reload tools 热加载（不需要重启）。
"""
import time


def add(a: float, b: float) -> float:
    """两个数相加，返回和。"""
    return a + b


def subtract(a: float, b: float) -> float:
    """a 减 b，返回差。"""
    return a - b


def multiply(a: float, b: float) -> float:
    """两个数相乘，返回积。"""
    return a * b


def divide(a: float, b: float) -> float:
    """a 除以 b，返回商。b 为 0 返回错误提示。"""
    if b == 0:
        return "[错误] 除数不能为 0"
    return a / b


def sleep(seconds: float, wake_on_message: bool = True) -> str:
    """等待指定秒数后返回；等待中收到后台消息/插话会立即提前醒来并附消息摘要（不会睡过头）。
    适用：轮询间隔/限速/等待外部事件（如后台任务/实例就绪）。inline 执行不受 tool_timeout
    影响；期间阻塞当前轮，引擎维持心跳（不会被看门狗判卡死）。提前醒来后仍需等待，按返回的
    「剩余约 N 秒」再睡；被主 Agent 没收工具、或用户停止本轮同样立即中断。
    seconds: 秒数（0~3600）。
    wake_on_message: 等待中收到新消息是否提前结束（默认 True）；纯冷却/限速不想被打断时传 False。
    不会睡过头（2026-11-27·用户提案）：0.5s 一片循环分片睡，每片看执行者 owner 的消息水位
    （插话队列 + 后台 inbox）——涨了就叫醒并附首批消息摘要（后台消息本轮结束后才消费、
    插话下一步边界注入，摘要让模型能直接判断要不要接着等）。"""
    try:
        s = float(seconds)
    except (TypeError, ValueError):
        return f"[错误] seconds 需为数字，收到 {seconds!r}"
    if not (0 <= s <= 3600):
        return f"[错误] seconds 需在 0~3600 之间，收到 {s:g}"
    try:
        from real_tools import get_bg_owner
        owner = get_bg_owner()
    except Exception:
        owner = None
    wake = bool(wake_on_message)
    pend_m0, inb_m0 = _msg_marks(owner) if (wake and owner is not None) else ((0, None), (0, None))
    slept = 0.0
    while slept < s:
        if owner is not None:
            if getattr(owner, "_revoked", False):
                return (f"[已中断] 等待被主 Agent 没收中断（已等 {slept:g}/{s:g} 秒）"
                        f"——工具已收回，任务中止。")
            if getattr(owner, "_stop_flag", False):
                return f"[已停止] 用户停止了本轮，等待提前结束（已等 {slept:g}/{s:g} 秒）。"
            if wake and _has_new(owner, pend_m0, inb_m0):
                return _woke_text(owner, pend_m0[0], inb_m0[0], slept, s)
        piece = min(0.5, s - slept)
        time.sleep(piece)
        slept += piece
    return f"已等待 {s:g} 秒"


def _msg_marks(owner):
    """执行者 Agent 的消息水位（sleep 起点/每片对比）：((插话队列长度, 末条对象), (inbox 长度, 末条对象))。
    长度 + 末条对象双判据：inbox 被后台泵/清队命令并发弹出导致长度回落的瞬态也能识别
    （末条对象变了=有新消息入过队）；列表为空时末条记 None。"""
    pend = getattr(owner, "pending_messages", None) or []
    inb = getattr(owner, "inbox", None) or []
    try:
        p = (len(pend), pend[-1] if len(pend) else None)
    except Exception:
        p = (0, None)
    try:
        i = (len(inb), inb[-1] if len(inb) else None)
    except Exception:
        i = (0, None)
    return p, i


def _has_new(owner, pend_m0, inb_m0) -> bool:
    pend_m, inb_m = _msg_marks(owner)
    return bool((pend_m[0] > pend_m0[0]) or (pend_m[0] and pend_m[1] is not pend_m0[1])
                or (inb_m[0] > inb_m0[0]) or (inb_m[0] and inb_m[1] is not inb_m0[1]))


def _clip(x, n=140) -> str:
    return " ".join(str(x).split())[:n]


def _woke_text(owner, pend_n0, inb_n0, slept, s) -> str:
    """提前醒来的回执：等了多久、来了几条（插话/后台分列）、各自何时生效、首批摘要、剩余秒数。"""
    pend = list(getattr(owner, "pending_messages", None) or [])[pend_n0:]
    inb_l = list(getattr(owner, "inbox", None) or [])
    inb = inb_l[inb_n0:]
    if not inb and inb_l and inb_n0 > 0:
        inb = inb_l[-1:]   # 长度没涨但末条变了（新消息入队后旧条被后台泵弹出的瞬态）：至少报出末条
    lines = [f"已等待 {slept:g}/{s:g} 秒后提前结束：等待期间收到 {len(pend) + len(inb)} 条新消息"
             f"（插话 {len(pend)} / 后台 {len(inb)}）。"]
    for x in pend[:2]:
        lines.append(f"- 〔插话·下一步即注入〕{_clip(x)}")
    for it in inb[:2]:
        if isinstance(it, (tuple, list)) and len(it) >= 2:
            src = f"·{it[0]}" if it[0] else ""
            lines.append(f"- 〔后台{src}·本轮结束后消费〕{_clip(it[1])}")
        else:
            lines.append(f"- 〔后台·本轮结束后消费〕{_clip(it)}")
    lines.append(f"仍需继续等待可再 sleep(约 {max(1, int(round(s - slept)))})。")
    return "\n".join(lines)


def kw_score(keywords: list = None, text: str = "") -> float:
    """关键词命中数评分（embedding 不可用时的降级重排）：keywords 中出现在 text 里的比例（0~1，
    与 cosine 量纲对齐，下游阈值/排序逻辑可复用）。keywords 空/None → 0.0（安全降级：rerank 全 0 分排后）。"""
    kws = [str(k).strip() for k in (keywords or []) if str(k).strip()]
    if not kws or not text:
        return 0.0
    hits = sum(1 for k in kws if k in text)
    return round(hits / len(kws), 4)


def agt_register():
    return [
        {"name": "add", "func": add, "hidden": True, "group": "light", "version": 1},
        {"name": "subtract", "func": subtract, "hidden": True, "group": "light", "version": 1},
        {"name": "multiply", "func": multiply, "hidden": True, "group": "light", "version": 1},
        {"name": "divide", "func": divide, "hidden": True, "group": "light", "version": 1},
        {"name": "sleep", "func": sleep, "hidden": False, "group": "light", "version": 3,
         "params": {"seconds": "秒数（0~3600）",
                    "wake_on_message": "等待中收到后台消息/插话是否提前结束（默认 True，不会睡过头）；纯冷却/限速不想被打断时传 False"}},
        {"name": "kw_score", "func": kw_score,
         "params": {
             "keywords": "关键词数组（通常接 kv_cache_read.value = extract_keywords 的产物）",
             "text": "被评分文本（批处理时接 loop-item）",
         },
         "outputs": [{"name": "raw", "type": "number", "description": "命中比例 0~1（与 cosine 量纲对齐）"}],
         "hidden": True, "group": "light", "version": 1},
    ]
