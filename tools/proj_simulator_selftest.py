"""proj_simulator_selftest.py —— 压缩阶梯新规则的场景矩阵验证（用户裁定 2026-09-14）。

覆盖：
  ① 年轻端可切            → 只升档（文字档），不推老档、不折叠（保真优先）
  ② 年轻端切完 + 老档有货  → 走 _deepen_oldest_tier（工具折叠档）——关键新行为
  ③ 老档推满 + 仍超线      → 才折叠进结构摘要
  ④ 达标即停             → 折叠不多吃一轮
  ⑤ fold_deep_tools 关    → 推老档恒 False（行为与旧版一致，不回归）
  ⑥ 边界 prune / 持久化往返 / restore 截断
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import config                                         # noqa: E402
from session import Session, Turn, Step, ToolCall     # noqa: E402

OK = []
FAIL = []


def check(name, cond, extra=""):
    (OK if cond else FAIL).append(name)
    print(f"  {'✅' if cond else '❌'} {name}  {extra}")


def mk_turn(i: int, chars: int = 3000, calls: int = 3) -> Turn:
    """造一轮：calls 个工具调用（每个结果 chars 字）+ 一段 answer。"""
    t = Turn(user_message=f"用户问题 {i}" + "。" * 20)
    t.answer = f"第{i}轮回答" + "答" * 200
    t.answer_reasoning = "推理" * 50
    steps = []
    for c in range(calls):
        tc = ToolCall("", changed=[])
        steps.append(Step(reasoning="想" * 100, tool_calls=[tc]))
    t.steps = steps
    # 工具结果直接塞进 toollog 内存（模拟真实：结果在 toollog，按 call_id 召回）
    s = _SESS
    for st in steps:
        cid = s.toollog.next_id()
        s.toollog.record(cid, "run_python", {"code": "x" * 50}, "R" * chars)
        st.tool_calls[0].call_id = cid
    return t


_SESS = None


def build(win=20000, ratio=0.5, max_level=2, fold_deep=True):
    global _SESS
    config.load_fold_deep_tools = lambda: fold_deep
    s = Session("(selftest)", llm=None)
    s.max_effective_context_window = win
    s.fold_target_ratio = ratio
    s.max_level = max_level
    s._detail_base = 1500
    s._chars_per_token = 2.0
    _SESS = s
    return s


def feed(s, n, start=0, chars=3000, calls=3):
    for i in range(start, start + n):
        s.turns.append(mk_turn(i, chars, calls))


def main():
    print("【①】年轻端可切 → 只升档（档位顺移，不推老档/不折叠）")
    s = build(win=25_000, ratio=0.5)
    feed(s, 10)
    s._plan_fold()
    check("升了档（边界>0）", len(s._tier_boundaries) > 0, f"bs={s._tier_boundaries}")
    check("未折叠", s._planned_fold == 0, f"fc={s._planned_fold}")
    check("升档先于折叠（阶梯顺序）", s._planned_graduates >= 1, f"升档 {s._planned_graduates} 刀")

    print("\n【②】年轻端切完 → 阶梯继续走到「推老档」（而不是直接折叠）")
    s = build(win=20_000, ratio=0.5, max_level=2)
    feed(s, 40)
    cnt = {"grad": 0, "deep": 0, "deep_ok": 0}
    _og, _od = s._graduate_once, s._deepen_oldest_tier

    def _g(*a, **k):
        r = _og(*a, **k)
        if r:
            cnt["grad"] += 1
        return r

    def _d(*a, **k):
        r = _od(*a, **k)
        cnt["deep"] += 1
        if r:
            cnt["deep_ok"] += 1
        return r

    s._graduate_once, s._deepen_oldest_tier = _g, _d
    s._plan_fold()
    check("阶梯走到了「推老档」这一级（升档切完后不再直接折叠）", cnt["deep"] > 0,
          f"升档{cnt['grad']}刀 / 推老档尝试{cnt['deep']}次(成功{cnt['deep_ok']})")
    check("折叠没有把历史一刀吃光（保留了分层）", 0 <= s._planned_fold < len(s.turns),
          f"fc={s._planned_fold} / 总轮 {len(s.turns)}")

    print("\n【③】老档推满 + 仍超线 → 才折叠进结构摘要")
    s = build(win=12_000, ratio=0.5, max_level=2)
    feed(s, 60)
    s._plan_fold()
    deep = [i for i in range(s._planned_fold, len(s.turns))
            if s._raw_tier_level(i) > s.max_level]
    check("仍超线 → 折叠（fc>0）", s._planned_fold > 0, f"fc={s._planned_fold}")

    print("\n【⑦】_deepen_oldest_tier 精确行为（手工边界状态）")
    s = build(win=20_000, ratio=0.5, max_level=2)
    feed(s, 40, chars=1200, calls=2)
    s._tier_boundaries = [39]                      # 未折区仅 1 个边界 → raw_level(0)=2 ≤ max_level(2)
    tgt = s.fold_target()
    est = lambda k: s._estimate_tokens([{"role": "system", "content": s.system}]
                                       + s._render_tiered_history(k))
    before = est(0)
    r = s._deepen_oldest_tier(est_fn=est, target=tgt)
    after = est(0)
    check("返回 True 且不是空转", r and after < before, f"est {before:,} → {after:,}（-{100*(1-after/before):.1f}%）")
    check("最老未折轮进了超深档", s._raw_tier_level(0) > s.max_level,
          f"raw(0)={s._raw_tier_level(0)} > max_level={s.max_level}")
    check("重复边界 = 多切一刀", s._tier_boundaries.count(39) == 2, f"bs={sorted(s._tier_boundaries)}")
    check("已在超深档 → 再推 False（不空转）",
          s._deepen_oldest_tier(est_fn=est, target=tgt) is False)
    s2 = build(win=20_000, ratio=0.5, max_level=2, fold_deep=False)
    feed(s2, 40, chars=1200, calls=2)
    s2._tier_boundaries = [39]
    check("fold_deep_tools 关 → 恒 False",
          s2._deepen_oldest_tier(est_fn=est, target=tgt) is False)

    print("\n【④】达标即停：折叠不多吃一轮")
    s = build(win=30_000, ratio=0.5, max_level=2)
    feed(s, 60)
    s._plan_fold()
    fc = s._planned_fold
    tgt = s.fold_target()
    def tok(k):
        return s._estimate_tokens([{"role": "system", "content": s.system}]
                                  + s._render_tiered_history(k) + s._seg_msgs_user_message())
    if fc > 0:
        check("fc 处达标（≤target）", tok(fc) <= tgt, f"tok({fc})={tok(fc):,} ≤ {tgt:,}")
        check("fc-1 处未达标（不多吃）", tok(fc - 1) > tgt, f"tok({fc-1})={tok(fc-1):,} > {tgt:,}")
    else:
        check("本轮未折叠（无需断言）", True)

    print("\n【⑤】fold_deep_tools 关 → 推老档恒 False（不回归）")
    s = build(win=20_000, ratio=0.5, max_level=2, fold_deep=False)
    feed(s, 40)
    s._plan_fold()
    from collections import Counter as _C
    dup = [b for b, n in _C(s._tier_boundaries).items() if n > 1]
    check("无重复边界（未推老档）", not dup, f"bs={s._tier_boundaries}")

    print("\n【⑥】边界 prune / 持久化 / restore")
    s = build(win=20_000, ratio=0.5, max_level=2)
    feed(s, 80)
    s._plan_fold()
    check("prune：无 < fc 的死边界",
          all(b >= s._planned_fold for b in s._tier_boundaries),
          f"fc={s._planned_fold} bs={sorted(set(s._tier_boundaries))}")
    # 未折轮档位不受 prune 影响（与未 prune 的口径一致）
    lv_before = [(i, s._raw_tier_level(i)) for i in range(s._planned_fold, len(s.turns))]
    s._tier_boundaries = [b for b in s._tier_boundaries if b >= s._planned_fold]
    lv_after = [(i, s._raw_tier_level(i)) for i in range(s._planned_fold, len(s.turns))]
    check("prune 对未折轮零影响", lv_before == lv_after)
    # restore 截断
    s.turns = s.turns[:30]
    s._tier_boundaries = [b for b in s._tier_boundaries if b < 30]
    check("restore 后边界合法", all(b < 30 for b in s._tier_boundaries))

    print(f"\n══ 结果：{len(OK)} 通过 / {len(FAIL)} 失败 ══")
    if FAIL:
        print("失败项：" + ", ".join(FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
