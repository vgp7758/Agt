# -*- coding: utf-8 -*-
"""translator 专属工具示例：报当前时间（仅该子 Agent 可见）。
subprocess 工具形态：agt_register 声明 func=函数名，框架以 `python now.py <func> --参数` 调用。"""
import datetime
import sys

def now():
    """返回当前本地时间字符串。"""
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def agt_register():
    return [{
        "name": "now",
        "description": "返回当前本地时间（translator 专属工具演示）",
        "parameters": {},
        "mode": "subprocess",
        "func": "now",
    }]


if __name__ == "__main__":
    # 极简 dispatch：本脚本只有 now 一个动作，直接输出（subprocess shim 约定：stdout=结果）
    print(now())
