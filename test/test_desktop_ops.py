#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""desktop-ops 技能服务单测：归一化坐标（0-1000）路径 + 帧像素旧路径回归。

2026-11-04 用户提案：非视觉宿主从代看描述（"坐标0-1000归一化"）零换算直点屏幕——
nx/ny 按屏幕宽高独立比例换算（不依赖帧宽 1280 假定），与 x/y 帧像素路径互斥、归一化优先。

隔离：pyautogui 整个 mock 进 sys.modules（server.py 函数内 `import pyautogui`），
不动真实鼠标；模块从 assets 路径按文件加载（目录名带连字符不能普通 import）。
"""
import importlib.util
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SRV = REPO / "src" / "assets" / "skills" / "desktop-ops" / "server.py"


def _load_server():
    spec = importlib.util.spec_from_file_location("desktop_ops_server", SRV)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class FakeGUI:
    """pyautogui 替身：记录动作，size 可调。"""

    FAILSAFE = True

    def __init__(self, w=1920, h=1080):
        self._w, self._h = w, h
        self.calls = []

    def size(self):
        return (self._w, self._h)

    def click(self, x, y, **kw):
        self.calls.append(("click", x, y, kw))

    def moveTo(self, x, y, **kw):
        self.calls.append(("moveTo", x, y, kw))

    def drag(self, dx, dy, **kw):
        self.calls.append(("drag", dx, dy, kw))


def _setup(monkeypatch, w=1920, h=1080):
    gui = FakeGUI(w, h)
    monkeypatch.setitem(sys.modules, "pyautogui", gui)
    return _load_server(), gui


# ── ① _scale_norm：x/y 独立比例锚定屏幕全幅 + 边界钳制 ────────────

def test_scale_norm_independent_axes(monkeypatch):
    mod, _ = _setup(monkeypatch)                    # 1920x1080
    assert mod._scale_norm(500, 500) == (960, 540)  # 正中
    assert mod._scale_norm(0, 0) == (0, 0)
    assert mod._scale_norm(1000, 1000) == (1919, 1079)   # 端点钳到 sw-1/sh-1
    assert mod._scale_norm(1200, -50) == (1919, 0)       # 越界钳制
    # x/y 独立比例（非 1280 帧比例）：1920 屏 nx=128 → 245；1080 屏 ny=128 → 138
    assert mod._scale_norm(128, 128) == (245, 138)


def test_scale_norm_other_screen(monkeypatch):
    mod, _ = _setup(monkeypatch, 2560, 1440)
    assert mod._scale_norm(500, 500) == (1280, 720)
    assert mod._scale_norm(250, 750) == (640, 1080)


# ── ② click/move 归一化路径（nx 优先 / 返回文案注明） ─────────────

def test_click_norm(monkeypatch):
    mod, gui = _setup(monkeypatch)
    msg = mod.run_command("click --nx 500 --ny 500")
    assert gui.calls == [("click", 960, 540, {"button": "left", "clicks": 1, "interval": 0.1})]
    assert "✅ 已点击 (500,500)" in msg and "归一化0-1000" in msg


def test_move_norm(monkeypatch):
    mod, gui = _setup(monkeypatch)
    msg = mod.run_command("move --nx 250 --ny 750")
    assert gui.calls == [("moveTo", 480, 810, {"duration": 0.2})]
    assert "✅ 已移动到 (250,750)" in msg and "归一化" in msg


def test_nx_priority_over_xy(monkeypatch):
    """两套坐标同时给出：归一化优先，返回文案注明（帧像素值不生效）。"""
    mod, gui = _setup(monkeypatch)
    msg = mod.run_command("click --x 100 --y 100 --nx 500 --ny 500")
    assert gui.calls[0][:3] == ("click", 960, 540)   # 归一化换算值，非帧像素 150,150
    assert "归一化0-1000" in msg


def test_click_norm_button_double(monkeypatch):
    mod, gui = _setup(monkeypatch)
    mod.run_command("click --nx 0 --ny 0 --button right --double True")
    assert gui.calls[0][:3] == ("click", 0, 0)
    assert gui.calls[0][3]["clicks"] == 2 and gui.calls[0][3]["button"] == "right"


# ── ③ 帧像素旧路径回归（k=屏宽/1280） ────────────────────────────

def test_click_frame_pixels_regression(monkeypatch):
    mod, gui = _setup(monkeypatch)
    msg = mod.run_command("click --x 500 --y 300")
    assert gui.calls == [("click", 750, 450, {"button": "left", "clicks": 1, "interval": 0.1})]
    assert "✅ 已点击 (500,300)" in msg and "归一化" not in msg


def test_move_frame_pixels_regression(monkeypatch):
    mod, gui = _setup(monkeypatch)
    mod.run_command("move --x 640 --y 360")
    assert gui.calls == [("moveTo", 960, 540, {"duration": 0.2})]   # k=1.5


# ── ④ drag：归一化 / 帧像素两路 ──────────────────────────────────

def test_drag_norm(monkeypatch):
    mod, gui = _setup(monkeypatch)
    mod.run_command("drag --nx1 100 --ny1 100 --nx2 600 --ny2 400")
    # moveTo(192,108) → drag(1152-192, 432-108)
    assert gui.calls[0][:3] == ("moveTo", 192, 108)
    assert gui.calls[1][:3] == ("drag", 960, 324)


def test_drag_frame_pixels_regression(monkeypatch):
    mod, gui = _setup(monkeypatch)
    mod.run_command("drag --x1 10 --y1 10 --x2 200 --y2 200 --duration 0.6")
    assert gui.calls[0][:3] == ("moveTo", 15, 15)          # k=1.5
    assert gui.calls[1][:3] == ("drag", 285, 285)
    assert gui.calls[1][3]["duration"] == 0.6


# ── ⑤ 缺参文案（旧风格 / 成对校验） ──────────────────────────────

def test_missing_coords_message(monkeypatch):
    mod, gui = _setup(monkeypatch)
    for cmd in ("click", "move"):
        msg = mod.run_command(cmd)
        assert msg.startswith("[缺必填参数]") and "--nx" in msg and "--x" in msg
    msg = mod.run_command("click --nx 500")             # 归一化成对校验
    assert msg.startswith("[缺必填参数]") and "--ny" in msg
    msg = mod.run_command("drag --nx1 1 --ny1 1")       # 归一化四参齐全校验
    assert msg.startswith("[缺必填参数]") and "--nx2" in msg
    msg = mod.run_command("drag")                       # 两路全缺
    assert msg.startswith("[缺必填参数]") and "--x1" in msg and "--nx1" in msg
    assert gui.calls == []                              # 缺参不产生任何键鼠动作


# ── ⑥ CAPS 文案含归一化命令说明 ──────────────────────────────────

def test_caps_documents_norm(monkeypatch):
    mod, _ = _setup(monkeypatch)
    assert "--nx 500 --ny 300" in mod.CAPS and "0-1000" in mod.CAPS
    assert "--x N --y N" in mod.CAPS                    # 旧路径保留说明
