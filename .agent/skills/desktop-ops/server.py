#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""desktop-ops 技能服务（stdin 行协议，2026-10-17 用户提案：键鼠操作全量走 stdin）。

协议：启动 stdout 打能力清单到 ===CAPS_END===；之后每读 stdin 一行命令，
输出结果若干行 + ===DONE=== 结束行。skill_use("desktop-ops", "click --x 500 --y 300")
即调本服务（agent_config.skill_use 写 stdin / 读 ===DONE===）。

坐标规范：与画面帧一致——帧按宽 1280 等比缩放，命令里的 (x,y) 给画面像素即可，
内部自动换算真实屏幕坐标（k=屏宽/1280，边界钳制）。
"""
import shlex
import sys
import time

FRAME_W = 1280


# ---------- 底层操作（平移自 tools/desktop_tools.py，逻辑不变） ----------

def _scale(x, y):
    import pyautogui
    sw, sh = pyautogui.size()
    k = sw / FRAME_W
    return (max(0, min(sw - 1, int(x * k))), max(0, min(sh - 1, int(y * k))))


def _do_click(a):
    import pyautogui
    pyautogui.FAILSAFE = False
    x, y = int(a["x"]), int(a["y"])
    sx, sy = _scale(x, y)
    pyautogui.click(sx, sy, button=a.get("button", "left"),
                    clicks=2 if a.get("double") else 1, interval=0.1)
    return f"✅ 已点击 ({x},{y}) {a.get('button', 'left')}{' 双击' if a.get('double') else ''}"


def _do_move(a):
    import pyautogui
    pyautogui.FAILSAFE = False
    x, y = int(a["x"]), int(a["y"])
    sx, sy = _scale(x, y)
    pyautogui.moveTo(sx, sy, duration=0.2)
    return f"✅ 已移动到 ({x},{y})"


def _do_drag(a):
    import pyautogui
    pyautogui.FAILSAFE = False
    x1, y1, x2, y2 = int(a["x1"]), int(a["y1"]), int(a["x2"]), int(a["y2"])
    p, q = _scale(x1, y1)
    r, s = _scale(x2, y2)
    pyautogui.moveTo(p, q, duration=0.15)
    pyautogui.drag(r - p, s - q, duration=float(a.get("duration", 0.4)), button="left")
    return f"✅ 已拖拽 ({x1},{y1})→({x2},{y2})"


def _do_key(a):
    import pyautogui
    pyautogui.FAILSAFE = False
    key = a["key"]
    pyautogui.press(key, presses=int(a.get("presses", 1)), interval=0.1)
    return f"✅ 已按 {key}×{a.get('presses', 1)}"


def _do_type(a):
    text = a["text"]
    if all(ord(c) < 128 for c in text):
        import pyautogui
        pyautogui.FAILSAFE = False
        pyautogui.typewrite(text, interval=float(a.get("interval", 0.03)))
    else:   # 中文等走剪切板（输入法拦截规避）
        import pyperclip
        import pyautogui
        pyautogui.FAILSAFE = False
        old = pyperclip.paste()
        pyperclip.copy(text)
        pyautogui.hotkey("ctrl", "v")
        time.sleep(0.15)
        pyperclip.copy(old)
    return f"✅ 已输入 {len(text)} 字符"


def _do_hotkey(a):
    import pyautogui
    pyautogui.FAILSAFE = False
    keys = a["keys"]
    pyautogui.hotkey(*[k.lower() for k in keys])
    return f"✅ 已按 {'+'.join(keys)}"


def _do_clip_r(a):
    import pyperclip
    return pyperclip.paste() or "（剪切板为空）"


def _do_clip_w(a):
    import pyperclip
    pyperclip.copy(a["text"])
    return f"✅ 已写 {len(a['text'])} 字符到剪切板"


def _do_wins(a):
    import pygetwindow as gw
    out = []
    for w in gw.getAllWindows():
        if w.title.strip() and w.visible:
            out.append(f"{'👉' if w.isActive else '　'} {w.title[:60]} | "
                       f"x{w.left},y{w.top} {w.width}x{w.height}")
    return "\n".join(out[:40]) or "（无可见窗口）"


def _do_focus(a):
    import pygetwindow as gw
    kw = a["title"]
    cands = [w for w in gw.getAllWindows()
             if kw.lower() in w.title.lower() and w.visible and w.title.strip()]
    if not cands:
        return f"[未找到窗口] 标题不含 {kw!r}"
    w = cands[0]
    try:
        if w.isMinimized:
            w.restore()   # 最小化窗口 activate() 静默无效——先还原再激活
            time.sleep(0.3)
        w.activate()
    except Exception:
        w.minimize(); w.restore()
    time.sleep(0.3)   # 等前台切换稳定
    return f"✅ 已切换窗口：{w.title[:60]}"


# ---------- 参数规格与分发 ----------

SPEC = {
    "click":   (_do_click,   {"req": {"x": int, "y": int}, "opt": {"button": str, "double": bool}}),
    "move":    (_do_move,    {"req": {"x": int, "y": int}}),
    "drag":    (_do_drag,    {"req": {"x1": int, "y1": int, "x2": int, "y2": int}, "opt": {"duration": float}}),
    "key":     (_do_key,     {"req": {"key": str}, "opt": {"presses": int}}),
    "type":    (_do_type,    {"req": {"text": str}, "opt": {"interval": float}}),
    "hotkey":  (_do_hotkey,  {"req": {"keys": [str]}}),
    "clip_r":  (_do_clip_r,  {}),
    "clip_w":  (_do_clip_w,  {"req": {"text": str}}),
    "wins":    (_do_wins,    {}),
    "focus":   (_do_focus,   {"req": {"title": str}}),
}

CAPS = """desktop-ops 键鼠服务可用命令（CLI 风格，--k v 传参）：
  click --x N --y N [--button left|right|middle] [--double True]   点击（坐标=画面帧像素）
  move --x N --y N                                                  移动（悬停）
  drag --x1 N --y1 N --x2 N --y2 N [--duration 0.4]                 拖拽
  key <键名> [--presses N]                                          单键（enter/esc/tab/win/...）
  type <文本> [--interval 0.03]                                     输入文本（中文自动走剪切板）
  hotkey k1 k2 [k3...]                                              组合键（如 hotkey ctrl c）
  clip_r / clip_w --text <文本>                                     剪切板读/写
  wins / focus --title <关键词>                                     窗口列表/切换
===CAPS_END==="""

USAGE = {"hotkey": "hotkey 后跟键名序列，如：hotkey ctrl alt tab"}


def _coerce(v, ty):
    if ty is bool:
        return str(v).lower() in ("1", "true", "yes", "on")
    if ty is int:
        return int(float(v))
    if ty is float:
        return float(v)
    return v


def run_command(line: str) -> str:
    try:
        toks = shlex.split(line or "")
    except ValueError as e:
        return f"[命令解析失败] {e}"
    if not toks:
        return "[空命令]"
    cmd, raw = toks[0], toks[1:]
    if cmd not in SPEC:
        return f"[未知命令] {cmd}。可用：{'/'.join(SPEC)}"
    fn, spec = SPEC[cmd]
    req, opt = spec.get("req", {}), spec.get("opt", {})
    a, i = {}, 0
    while i < len(raw):
        t = raw[i]
        if t.startswith("--"):
            name = t[2:]
            if name not in req and name not in opt:
                return f"[未知参数] --{name}（{cmd} 的参数：{list(req) + list(opt)}）"
            if i + 1 >= len(raw):
                return f"[缺参数值] --{name}"
            ty = req.get(name) or opt.get(name)
            if isinstance(ty, list):          # hotkey：吞掉全部剩余 token
                a["keys"] = raw[i + 1:]
                break
            a[name] = _coerce(raw[i + 1], ty)
            i += 2
        else:                                  # 位置参数：按 req 声明顺序填
            left = [k for k in req if k not in a]
            if not left:
                if cmd == "hotkey":
                    a.setdefault("keys", []).append(t)
                    i += 1
                    continue
                return f"[多余参数] {t}"
            k = left[0]
            ty = req[k]
            if isinstance(ty, list):
                a[k] = raw[i:]
                break
            a[k] = _coerce(t, ty)
            i += 1
    missing = [k for k in req if k not in a]
    if missing:
        return f"[缺必填参数] {cmd} 需要 {' '.join('--' + k for k in missing)}。{USAGE.get(cmd, '')}"
    return fn(a)


def main():
    sys.stdout.write(CAPS + "\n")
    sys.stdout.flush()
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        if line in ("/quit", "quit"):
            print("bye")
            break
        try:
            print(run_command(line))
        except Exception as e:
            print(f"[执行失败] {type(e).__name__}: {e}")
        print("===DONE===")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
