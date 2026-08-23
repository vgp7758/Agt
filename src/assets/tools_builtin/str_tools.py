"""str_tools.py —— 字符串类脚本工具（原 LIGHT_TOOLS 纯函数型外置件）。

agt_register() 返回描述符列表（script_tools.py 扫描注册约定）。
改完本文件用 /reload tools 热加载（不需要重启）。
"""


def contains(text: str, keyword: str) -> bool:
    """判断 text 是否包含 keyword，返回 true/false。"""
    return keyword in (text or "")


def starts_with(text: str, prefix: str) -> bool:
    """判断 text 是否以 prefix 开头，返回 true/false（如按扩展名/协议前缀分流）。"""
    return (text or "").startswith(prefix)


def ends_with(text: str, suffix: str) -> bool:
    """判断 text 是否以 suffix 结尾，返回 true/false（如按扩展名分流 .cs/.py）。"""
    return (text or "").endswith(suffix)


def to_ascii(text: str) -> str:
    r"""把字符串里的非 ASCII 字符（中文等）转成 \uXXXX 转义，ASCII 字符保留。
    用于生成 ASCII 安全文本（JSON 传输/存储）。"""
    return "".join(ch if ord(ch) < 128 else "\\u%04x" % ord(ch) for ch in (text or ""))


def join(items: list, separator: str = ",") -> str:
    """用分隔符把字符串列表拼接成一个字符串（类似 string.join）。items: 字符串列表；separator: 分隔符。"""
    return separator.join(str(x) for x in (items or []))


def split(text: str, separator: str = ",") -> list:
    """按分隔符把字符串切成列表（类似 string.split）。text: 原文；separator: 分隔符。"""
    return text.split(separator) if text else []


def agt_register():
    return [
        {"name": "contains", "func": contains, "hidden": True, "group": "light", "version": 1},
        {"name": "starts_with", "func": starts_with, "hidden": True, "group": "light", "version": 1},
        {"name": "ends_with", "func": ends_with, "hidden": True, "group": "light", "version": 1},
        {"name": "to_ascii", "func": to_ascii, "hidden": True, "group": "light", "version": 1},
        {"name": "join", "func": join, "hidden": True, "group": "light", "version": 1},
        {"name": "split", "func": split, "hidden": True, "group": "light", "version": 1},
    ]
