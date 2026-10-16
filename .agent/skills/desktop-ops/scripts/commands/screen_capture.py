# -*- coding: utf-8 -*-
"""desktop-ops 命令口帧源：截取全屏 → 覆盖写固定临时文件 → 打印路径。
被 image_feed tool: 表达式按步调用（每步一帧）；也可 skill_command 手动取帧。"""
import sys
from pathlib import Path

def main() -> int:
    import pyautogui
    out = Path.home() / ".agt" / "snapshots" / "desktop_ops_frame.png"
    out.parent.mkdir(parents=True, exist_ok=True)
    shot = pyautogui.screenshot()
    shot.save(str(out))
    print(out)
    return 0

if __name__ == "__main__":
    sys.exit(main())
