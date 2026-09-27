#!/usr/bin/env python
"""skill_run_code 示例：greet.py <名字> [--times N] —— 按 N 次打印问候。"""
import sys


def main():
    args = sys.argv[1:]
    name = args[0] if args and not args[0].startswith("--") else "世界"
    times = int(args[args.index("--times") + 1]) if "--times" in args else 1
    for _ in range(max(1, times)):
        print(f"你好，{name}！")


if __name__ == "__main__":
    main()
