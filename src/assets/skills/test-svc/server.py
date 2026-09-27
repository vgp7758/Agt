#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""test-svc server.py —— 技能服务协议演示（skill_equip / skill_use，2026-09-27）。
协议：启动打印能力清单到 ===CAPS_END===；每条 stdin 命令输出结果到 ===DONE===。"""
import shlex
import sys

# —— 能力清单（skill_equip 原样返回给 Agent）——
print("add N:        # 计数器加 N（N=整数）")
print("show:         # 显示当前计数")
print("echo MSG...:  # 原样返回后面的文本")
print("===CAPS_END===")

count = 0
for line in sys.stdin:
    parts = shlex.split(line.strip())
    if not parts:
        print("(空命令)")
    else:
        cmd, args = parts[0], parts[1:]
        if cmd == "add" and args:
            count += int(args[0])
            print(f"count={count}")
        elif cmd == "show":
            print(f"count={count}")
        elif cmd == "echo":
            print(" ".join(args))
        else:
            print(f"未知命令: {cmd}")
    print("===DONE===")
    sys.stdout.flush()
