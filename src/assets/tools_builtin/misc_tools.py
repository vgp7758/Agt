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


def sleep(seconds: float) -> str:
    """等待指定秒数后返回（工作流 wait 节点：轮询间隔/限速等用）。seconds: 秒数（0~300）。"""
    try:
        s = float(seconds)
    except (TypeError, ValueError):
        return f"[错误] seconds 需为数字，收到 {seconds!r}"
    if not (0 <= s <= 300):
        return f"[错误] seconds 需在 0~300 之间，收到 {s:g}"
    time.sleep(s)
    return f"已等待 {s:g} 秒"


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
        {"name": "sleep", "func": sleep, "hidden": True, "group": "light", "version": 1},
        {"name": "kw_score", "func": kw_score,
         "params": {
             "keywords": "关键词数组（通常接 kv_cache_read.value = extract_keywords 的产物）",
             "text": "被评分文本（批处理时接 loop-item）",
         },
         "outputs": [{"name": "raw", "type": "number", "description": "命中比例 0~1（与 cosine 量纲对齐）"}],
         "hidden": True, "group": "light", "version": 1},
    ]
