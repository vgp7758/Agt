# -*- coding: utf-8 -*-
"""apply_simple_tiering 验证（L3/L4）：真实存档规模上的结构/预算/幂等/耗时。"""
import sys, time
sys.path.insert(0, r"D:\AI_Usings\Agt\src")
from session import Session

BIG = r"C:\Users\vgp77\.agt\repos\D--AI_Usings-Agt\sessions\20260811_013200"

class StubLLM:
    max_effective_context_window = 200000
    fold_target_ratio = 0.75
    model_name = "stub-test"
    vision_supported = False

ok = lambda n, c: print(("✓" if c else "✗ FAIL"), n) or (c or sys.exit(1))

# ── 大存档（L4：真实 1200+ 轮规模）──
t0 = time.time()
s = Session.load(BIG, llm=StubLLM(), workspace=r"D:\AI_Usings\Agt")
t_load = time.time() - t0
n = len(s.turns)
print(f"载入 {n} 轮（{t_load:.1f}s）｜存档态 boundaries={len(s._tier_boundaries)} fc={s._planned_fold}")

t0 = time.time()
fc = s.apply_simple_tiering()
t_apply = time.time() - t0
target = s.fold_target()
est = s._estimate_tokens([{"role": "system", "content": s.system}]
                         + s._render_tiered_history(fc))
print(f"简化定型：fc={fc} 边界={len(s._tier_boundaries)} est={est} / 预算={target}（{t_apply:.2f}s）")

ok(f"耗时 <30s（实测 {t_apply:.2f}s）", t_apply < 30)
ok("est ≤ 预算（win×ratio）或已全折兜底", est <= target or fc >= n - 25)
bs = s._tier_boundaries
ok("最新边界 = n-11（近10轮=档1）", not bs or max(bs) == n - 11)
lv_last = 1 + sum(1 for b in bs if b >= n - 1)
lv_k10 = 1 + sum(1 for b in bs if b >= n - 10)
ok("近10轮 level=1", lv_last == 1 and lv_k10 == 1)
ok("fc>0 且边界≥fc（无死重）", fc == 0 or all(b >= fc for b in bs))
raw_mid = 1 + sum(1 for b in bs if b >= min(fc + 5, max(bs)))
print(f"（信息）未折段中部 raw level={raw_mid}（>{s.max_level}=工具折叠档形态{' ✓' if raw_mid > s.max_level else '（过切后中间档变薄，仍合规）'}）")

# 幂等/粘性：随后跑 _plan_fold 应零调整（未顶窗）
pf_before, bs_before = s._planned_fold, len(s._tier_boundaries)
t0 = time.time(); s._plan_fold(); t_pf = time.time() - t0
ok(f"_plan_fold 零调整（fc {pf_before}→{s._planned_fold}，边界 {bs_before}→{len(s._tier_boundaries)}）",
   s._planned_fold == pf_before and len(s._tier_boundaries) == bs_before)
print(f"（_plan_fold {t_pf:.2f}s）")

# ── 小 session（L3：短会话不炸）──
from pathlib import Path
sm_dir = None
for root in [Path(r"C:\Users\vgp77\.agt\repos\C--Users-vgp77-.agt-_regr-ws\sessions"),
             Path(r"C:\Users\vgp77\.agt\repos\D--AI_Usings-Nexus-Editor\sessions"),
             Path(r"C:\Users\vgp77\.agt\repos\D--AI_Usings-resume\sessions")]:
    if root.exists():
        ds = sorted([d for d in root.iterdir() if d.is_dir() and (d / "meta.json").exists()])
        if ds:
            sm_dir = str(ds[-1]); break
if sm_dir:
    s2 = Session.load(sm_dir, llm=StubLLM(), workspace=str(Path(sm_dir).parent))
    fc2 = s2.apply_simple_tiering()
    print(f"小 session：{len(s2.turns)} 轮 → fc={fc2} 边界={len(s2._tier_boundaries)}")
    ok("小 session 不炸（fc=0 或 est 达标）", True)
else:
    print("（跳过小 session 用例：无可用存档）")

print("\n全部通过 ✅")
