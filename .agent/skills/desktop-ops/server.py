#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""desktop-ops 技能服务（stdin 行协议，2026-10-17 用户提案：键鼠操作全量走 stdin）。

协议：启动 stdout 打能力清单到 ===CAPS_END===；之后每读 stdin 一行命令，
输出结果若干行 + ===DONE=== 结束行。skill_use("desktop-ops", "click --x 500 --y 300")
即调本服务（agent_config.skill_use 写 stdin / 读 ===DONE===）。

坐标规范（两套路径，互斥，归一化优先）：
- 帧像素：(x,y) 与画面帧一致——帧按宽 1280 等比缩放，内部换算真实屏幕坐标
  （k=屏宽/1280，边界钳制）。视觉宿主在画面帧里量坐标用这条。
- 归一化：(nx,ny) 取值 0-1000，x/y 独立比例锚定屏幕全幅
  （kx=屏宽/1000, ky=屏高/1000，边界钳制）。非视觉宿主从代看描述
  （"坐标0-1000归一化"）零换算直点屏幕用它（2026-11-04 用户提案）。
"""
import shlex
import sys
import time

FRAME_W = 1280
NORM_MAX = 1000   # 归一化坐标上限：0-1000 锚定屏幕全幅（代看描述同款刻度）


# ---------- 底层操作（平移自 tools/desktop_tools.py，逻辑不变） ----------

def _scale(x, y):
    """帧像素 → 屏幕像素（帧宽 1280 等比缩放，k=屏宽/1280，无独立 y 比例）。"""
    import pyautogui
    sw, sh = pyautogui.size()
    k = sw / FRAME_W
    return (max(0, min(sw - 1, int(x * k))), max(0, min(sh - 1, int(y * k))))


def _scale_norm(nx, ny):
    """归一化坐标(0-1000) → 屏幕像素：x/y 独立比例锚定屏幕全幅，边界钳制。

    与 _scale 的区别：不依赖帧宽 1280 假定——非视觉宿主只拿到代看描述的
    0-1000 归一化坐标，直接按屏幕宽高各自比例换算，零中间换算。
    """
    import pyautogui
    sw, sh = pyautogui.size()
    kx, ky = sw / NORM_MAX, sh / NORM_MAX
    return (max(0, min(sw - 1, int(nx * kx))), max(0, min(sh - 1, int(ny * ky))))


def _click_move_xy(a, cmd):
    """click/move 坐标解析（两套路径互斥，归一化优先）。

    返回 (sx, sy, 文案坐标, 标注)；失败返回 (None, None, 缺参文案, "")。
    """
    if a.get("nx") is not None or a.get("ny") is not None:      # 归一化路径（优先）
        if a.get("nx") is None or a.get("ny") is None:
            return (None, None,
                    f"[缺必填参数] {cmd} 归一化坐标需要 --nx 和 --ny 成对给出。{USAGE.get(cmd, '')}",
                    "")
        nx, ny = int(a["nx"]), int(a["ny"])
        sx, sy = _scale_norm(nx, ny)
        return sx, sy, f"({nx},{ny})", "（归一化0-1000）"
    if a.get("x") is not None and a.get("y") is not None:       # 帧像素路径（旧，保留）
        x, y = int(a["x"]), int(a["y"])
        sx, sy = _scale(x, y)
        return sx, sy, f"({x},{y})", ""
    return (None, None,
            f"[缺必填参数] {cmd} 需要 --x --y（帧像素）或 --nx --ny（0-1000归一化）。{USAGE.get(cmd, '')}",
            "")


def _do_click(a):
    import pyautogui
    pyautogui.FAILSAFE = False
    sx, sy, shown, note = _click_move_xy(a, "click")
    if sx is None:
        return shown
    pyautogui.click(sx, sy, button=a.get("button", "left"),
                    clicks=2 if a.get("double") else 1, interval=0.1)
    return f"✅ 已点击 {shown}{note} {a.get('button', 'left')}{' 双击' if a.get('double') else ''}"


def _do_move(a):
    import pyautogui
    pyautogui.FAILSAFE = False
    sx, sy, shown, note = _click_move_xy(a, "move")
    if sx is None:
        return shown
    pyautogui.moveTo(sx, sy, duration=0.2)
    return f"✅ 已移动到 {shown}{note}"


def _do_drag(a):
    import pyautogui
    pyautogui.FAILSAFE = False
    _nkeys = ("nx1", "ny1", "nx2", "ny2")
    if any(a.get(k) is not None for k in _nkeys):                # 归一化路径（优先，需四参齐全）
        if any(a.get(k) is None for k in _nkeys):
            return ("[缺必填参数] drag 归一化拖拽需要 --nx1 --ny1 --nx2 --ny2 全部给出。"
                    + USAGE.get("drag", ""))
        x1, y1, x2, y2 = (int(a[k]) for k in _nkeys)
        p, q = _scale_norm(x1, y1)
        r, s = _scale_norm(x2, y2)
        note = "（归一化0-1000）"
    elif all(a.get(k) is not None for k in ("x1", "y1", "x2", "y2")):   # 帧像素路径（旧，保留）
        x1, y1, x2, y2 = int(a["x1"]), int(a["y1"]), int(a["x2"]), int(a["y2"])
        p, q = _scale(x1, y1)
        r, s = _scale(x2, y2)
        note = ""
    else:
        return ("[缺必填参数] drag 需要 --x1 --y1 --x2 --y2（帧像素）或 "
                "--nx1 --ny1 --nx2 --ny2（0-1000归一化）。" + USAGE.get("drag", ""))
    pyautogui.moveTo(p, q, duration=0.15)
    pyautogui.drag(r - p, s - q, duration=float(a.get("duration", 0.4)), button="left")
    return f"✅ 已拖拽 ({x1},{y1})→({x2},{y2}){note}"


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
# 坐标参数（x/y 帧像素 或 nx/ny 归一化）全放 opt、fn 内校验二选一——
# run_command 的 req 必填检查没法表达"或"，缺参文案由各 fn 返回（风格与旧一致）。

SPEC = {
    "click":   (_do_click,   {"req": {},
                              "opt": {"x": int, "y": int, "nx": int, "ny": int,
                                      "button": str, "double": bool}}),
    "move":    (_do_move,    {"req": {},
                              "opt": {"x": int, "y": int, "nx": int, "ny": int}}),
    "drag":    (_do_drag,    {"req": {},
                              "opt": {"x1": int, "y1": int, "x2": int, "y2": int,
                                      "nx1": int, "ny1": int, "nx2": int, "ny2": int,
                                      "duration": float}}),
    "key":     (_do_key,     {"req": {"key": str}, "opt": {"presses": int}}),
    "type":    (_do_type,    {"req": {"text": str}, "opt": {"interval": float}}),
    "hotkey":  (_do_hotkey,  {"req": {"keys": [str]}}),
    "clip_r":  (_do_clip_r,  {}),
    "clip_w":  (_do_clip_w,  {"req": {"text": str}}),
    "wins":    (_do_wins,    {}),
    "focus":   (_do_focus,   {"req": {"title": str}}),
}

CAPS = """desktop-ops 键鼠服务可用命令（CLI 风格，--k v 传参）：
  click --nx 500 --ny 300 [--button left|right|middle] [--double True]  点击（0-1000归一化，直接锚定屏幕，推荐非视觉宿主用）
  click --x N --y N [...]                                           点击（帧像素路径保留：视觉宿主用，帧宽1280约定）
  move --nx 500 --ny 300  /  move --x N --y N                       移动（悬停；两套坐标同上，归一化优先）
  drag --nx1 N --ny1 N --nx2 N --ny2 N [--duration 0.4]             拖拽（归一化路径）
  drag --x1 N --y1 N --x2 N --y2 N [--duration 0.4]                 拖拽（帧像素路径）
  key <键名> [--presses N]                                          单键（enter/esc/tab/win/...）
  type <文本> [--interval 0.03]                                     输入文本（中文自动走剪切板）
  hotkey k1 k2 [k3...]                                              组合键（如 hotkey ctrl c）
  clip_r / clip_w --text <文本>                                     剪切板读/写
  wins / focus --title <关键词>                                     窗口列表/切换
坐标规范：nx/ny 取值 0-1000（代看描述同刻度，x/y 独立比例锚定屏幕全幅，含边界钳制）；
两套坐标同时给出时归一化优先（返回文案注明）。
===CAPS_END==="""

USAGE = {"hotkey": "hotkey 后跟键名序列，如：hotkey ctrl alt tab",
         "click": "坐标二选一：--nx --ny（0-1000归一化）或 --x --y（帧像素）。",
         "move": "坐标二选一：--nx --ny（0-1000归一化）或 --x --y（帧像素）。",
         "drag": "坐标二选一：--nx1 --ny1 --nx2 --ny2（0-1000归一化）或 --x1 --y1 --x2 --y2（帧像素）。"}


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
