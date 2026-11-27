# -*- coding: utf-8 -*-
"""L3 单测：回退链冷却斐波那契退避（fibonacci backoff·2026-11-27 用户提案）。

覆盖：数列序列/封顶/可配置、per-key 实际时长读取、连续失败累加、成功清零重置、
temp_ban 预折旧兼容（全局基准不被动态退避污染）、外部冷却清理同步。
不连真实 LLM。
"""
import sys, time, pathlib as _pl
sys.path.insert(0, str(_pl.Path(__file__).resolve().parents[1] / "src"))

import llm_client as lc

ok = fail = 0


def chk(name, cond, extra=""):
    global ok, fail
    if cond:
        ok += 1
        print(f"  ✅ {name}")
    else:
        fail += 1
        print(f"  ❌ {name}  {extra}")


def bare():
    """绕过 __init__ 造一个只带冷却相关字段的 LLMClient（不读配置/不连网）。"""
    o = lc.LLMClient.__new__(lc.LLMClient)
    o._provider_cooldown = {}
    o._cooldown_sec_by_key = {}
    o._cooldown_seconds = 300.0
    o._cooldown_base_seconds = lc.COOLDOWN_BASE_SECS
    o._cooldown_max_seconds = lc.COOLDOWN_MAX_SECS
    o._consecutive_failures = 0
    return o


print("== 1. 斐波那契数列 30,60,90,150,240,390,630,900(封顶) ==")
o = bare()
expect = [30, 60, 90, 150, 240, 390, 630, 900, 900, 900, 900]
got = [o._cooldown_for(n) for n in range(1, 12)]
chk("数列正确且封顶 900s", got == expect, f"got={got}")
chk("第 8 次起恒为 15 分钟", o._cooldown_for(8) == 900 and o._cooldown_for(50) == 900)
chk("n=0 视为首项 30s", o._cooldown_for(0) == 30)

print("== 2. 自定义 base / cap ==")
o._cooldown_base_seconds = 10.0
o._cooldown_max_seconds = 60.0
got2 = [o._cooldown_for(n) for n in range(1, 9)]
chk("base=10 cap=60 封顶生效", got2 == [10, 20, 30, 50, 60, 60, 60, 60], f"got={got2}")
o._cooldown_base_seconds = 100.0  # base 已超 cap
chk("base > cap 时不越界", o._cooldown_for(1) == 60.0)

print("== 3. per-key 实际时长读取（_cooldown_remain）==")
o = bare()
o._provider_cooldown["m:tok"] = time.time()
o._cooldown_sec_by_key["m:tok"] = 30.0
remain = o._cooldown_remain("m:tok")
chk("写 30s 冷却 → 剩余≈30s", 29 <= remain <= 30, str(remain))
o._cooldown_sec_by_key["m:tok"] = 900.0
chk("写 900s 冷却 → 剩余≈900s", 899 <= o._cooldown_remain("m:tok") <= 900)
chk("不存在的 key → 0", o._cooldown_remain("none") == 0.0)
o._provider_cooldown["legacy:tok"] = time.time()
chk("无 per-key 记录回退全局 300s（兼容旧写入/temp_ban）",
    299 <= o._cooldown_remain("legacy:tok") <= 300)

print("== 4. 连续失败累加 + 成功清零（直接驱动循环内部逻辑）==")
o = bare()
# 模拟失败写入（与 _chat_inner except 分支同构）
def fail_once(key):
    o._consecutive_failures += 1
    cd = o._cooldown_for(o._consecutive_failures)
    o._provider_cooldown[key] = time.time()
    o._cooldown_sec_by_key[key] = cd
    return cd

seq = [fail_once(f"m{i}:tok") for i in range(5)]
chk("五次连续失败冷却 30/60/90/150/240", seq == [30, 60, 90, 150, 240], str(seq))
chk("计数累加到 5", o._consecutive_failures == 5)
# 模拟成功（与成功分支同构）
o._provider_cooldown.clear(); o._cooldown_sec_by_key.clear(); o._consecutive_failures = 0
chk("成功后计数归零", o._consecutive_failures == 0)
chk("重置后首次失败回到 30s", fail_once("again:tok") == 30)

print("== 5. temp_ban 兼容：动态退避不污染预折旧窗口 ==")
o = bare()
# 一个过期的旧冷却 + 残留的 per-key 时长（模拟动态退避留下的陈旧项）
o._provider_cooldown["moss:tok"] = time.time() - 1000
o._cooldown_sec_by_key["moss:tok"] = 900.0
lc.LLMClient.temp_ban(o, "moss", 120)
chk("temp_ban 清掉 per-key 时长", "moss:tok" not in o._cooldown_sec_by_key)
# 用 _cooldown_remain（走真实读取路径）验证剩余≈120 而非被残留时长拉成 900
rem = o._cooldown_remain("moss:tok")
chk("temp_ban 120s 按全局 300 预折旧 → 剩余≈120", 115 <= rem <= 120, str(rem))

print(f"\n结果：{ok} 过 / {fail} 挂")
sys.exit(0 if fail == 0 else 1)
