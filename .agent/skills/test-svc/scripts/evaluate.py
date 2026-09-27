#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""skill_evaluate 示例：忽略 stdin，对本技能做自检，输出逐项 PASS/FAIL 与总评。"""
import os
import subprocess
import sys

d = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # 技能根
ok = True

def check(name, cond):
    global ok
    print(("PASS  " if cond else "FAIL  ") + name)
    ok = ok and cond

# ① server.py 协议标记齐全（skill_equip/skill_use 可用）
srv = open(os.path.join(d, "server.py"), encoding="utf-8").read()
check("server.py 协议标记（===CAPS_END=== / ===DONE===）",
      "===CAPS_END===" in srv and "===DONE===" in srv)

# ② scripts/greet.py 可执行且输出包含名字
g = os.path.join(d, "scripts", "greet.py")
r = subprocess.run([sys.executable, g, "评测", "--times", "2"], capture_output=True, text=True, timeout=10)
check("scripts/greet.py 执行（输出问候×2）", r.returncode == 0 and r.stdout.count("评测") == 2)

# ③ SKILL.md frontmatter（read_skill 依赖）
sk = open(os.path.join(d, "SKILL.md"), encoding="utf-8").read()
check("SKILL.md frontmatter（name: test-svc）", sk.startswith("---") and "name: test-svc" in sk[:200])

print("EVAL PASS" if ok else "EVAL FAIL")
sys.exit(0 if ok else 1)
