#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""desktop-operator 专属工具——键鼠模拟 / 剪切板 / 窗口操作（pyautogui + pygetwindow）。

配套：桌面画面由装配段 image_feed 每步注入（tools/image_feed_poc.py 起在 8765），
坐标基准即该画面帧（默认缩放到宽 1280——点击坐标按画面像素给即可，
mouse_* 会自动换算到真实屏幕坐标）。
"""
import time

# 画面服务宽度（与 tools/image_feed_poc.py 的 MAX_W 一致）：坐标换算用
_FRAME_W = 1280


def _screen_size():
    import pyautogui
    return pyautogui.size()


def _scale(x, y):
    """画面帧坐标 → 真实屏幕坐标。画面服务按宽 1280 等比缩放（宽高比保持），
    还原系数 k=屏幕宽/帧宽；y 用同一系数（等比）。DPI 缩放（125%/150%）下
    pyautogui 的逻辑坐标与物理帧成同一比例，换算依然成立。边界钳制防越界。"""
    import pyautogui
    sw, sh = pyautogui.size()
    k = sw / _FRAME_W
    return (max(0, min(sw - 1, int(x * k))), max(0, min(sh - 1, int(y * k))))


def mouse_click(x: int, y: int, button: str = "left", double: bool = False) -> str:
    """点击屏幕坐标（x,y 为 image_feed 画面里的像素位置）。button=left/right/middle；double=True 双击。"""
    import pyautogui
    pyautogui.FAILSAFE = False
    sx, sy = _scale(x, y)
    pyautogui.click(sx, sy, button=button, clicks=2 if double else 1, interval=0.1)
    return f"✅ 已点击 ({x},{y}) {button}{' 双击' if double else ''}"


def mouse_move(x: int, y: int) -> str:
    """移动鼠标到 (x,y)（不点击）——悬停展开菜单等。"""
    import pyautogui
    pyautogui.FAILSAFE = False
    sx, sy = _scale(x, y)
    pyautogui.moveTo(sx, sy, duration=0.2)
    return f"✅ 已移动到 ({x},{y})"


def mouse_drag(x1: int, y1: int, x2: int, y2: int, duration: float = 0.4) -> str:
    """从 (x1,y1) 按住拖拽到 (x2,y2)——拖文件/选区/滑块。"""
    import pyautogui
    pyautogui.FAILSAFE = False
    a, b = _scale(x1, y1); c, d = _scale(x2, y2)
    pyautogui.moveTo(a, b, duration=0.15)
    pyautogui.drag(c - a, d - b, duration=duration, button="left")
    return f"✅ 已拖拽 ({x1},{y1})→({x2},{y2})"


def key_press(key: str, presses: int = 1) -> str:
    """按单个键：enter/tab/esc/delete/up/down/left/right/f1-f12/space/win 等。"""
    import pyautogui
    pyautogui.FAILSAFE = False
    pyautogui.press(key, presses=presses, interval=0.1)
    return f"✅ 已按 {key}×{presses}"


def key_type(text: str, interval: float = 0.03) -> str:
    """输入一段文字（到当前焦点输入框）。中文输入法可能拦截——复杂文本优先 clipboard_write+key_hotkey('ctrl','v')。"""
    import pyautogui
    pyautogui.FAILSAFE = False
    pyautogui.typewrite(text, interval=interval) if all(ord(c) < 128 for c in text) else _type_via_clipboard(text)
    return f"✅ 已输入 {len(text)} 字符"


def _type_via_clipboard(text: str):
    import pyperclip
    old = pyperclip.paste()
    pyperclip.copy(text)
    import pyautogui
    pyautogui.hotkey("ctrl", "v")
    time.sleep(0.15)
    pyperclip.copy(old)


def key_hotkey(*keys: str) -> str:
    """组合键：key_hotkey("ctrl","c") / ("alt","tab") / ("win","d") 等。"""
    import pyautogui
    pyautogui.FAILSAFE = False
    pyautogui.hotkey(*[k.lower() for k in keys])
    return f"✅ 已按 {'+'.join(keys)}"


def clipboard_read() -> str:
    """读剪切板文本（当前内容原样返回）。"""
    import pyperclip
    return pyperclip.paste() or "（剪切板为空）"


def clipboard_write(text: str) -> str:
    """写文本到剪切板。"""
    import pyperclip
    pyperclip.copy(text)
    return f"✅ 已写 {len(text)} 字符到剪切板"


def window_list() -> str:
    """列出所有可见窗口（标题 | 位置尺寸 | 是否前台）——找窗口/核对标题用。"""
    import pygetwindow as gw
    out = []
    for w in gw.getAllWindows():
        if w.title.strip() and w.visible:
            out.append(f"{'👉' if w.isActive else '　'} {w.title[:60]} | "
                       f"x{w.left},y{w.top} {w.width}x{w.height}")
    return "\n".join(out[:40]) or "（无可见窗口）"


def window_focus(title_contains: str) -> str:
    """按标题关键词切换窗口（模糊匹配，激活并置前）。"""
    import pygetwindow as gw
    cands = [w for w in gw.getAllWindows()
             if title_contains.lower() in w.title.lower() and w.visible and w.title.strip()]
    if not cands:
        return f"❌ 没有标题含「{title_contains}」的可见窗口（window_list 查全量）"
    w = cands[0]
    try:
        if w.isMinimized:
            w.restore()
        w.activate()
    except Exception:
        pass
    time.sleep(0.3)
    return f"✅ 已切换到「{w.title[:50]}」"

def agt_register():
    """专属工具注册（同 tools/builtin 约定——scan_script_tools 消费）。"""
    specs = [
        ("mouse_click", mouse_click), ("mouse_move", mouse_move), ("mouse_drag", mouse_drag),
        ("key_press", key_press), ("key_type", key_type), ("key_hotkey", key_hotkey),
        ("clipboard_read", clipboard_read), ("clipboard_write", clipboard_write),
        ("window_list", window_list), ("window_focus", window_focus),
    ]
    return [{"name": n, "func": f, "hidden": False, "group": "desktop", "version": 1}
            for n, f in specs]
