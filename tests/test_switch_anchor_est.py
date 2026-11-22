# -*- coding: utf-8 -*-
"""L3 单测：切模型 adopt 超窗判定的锚定外推（2026-11-22 用户方案）。不连真实 LLM。

覆盖：
  1. _est_shape_tokens 两级：无锚点 → chars 老路径；有锚点 → 实测存量 + Δ 换算（正/负增量、cpt clamp）
  2. _save_tier_shape 锚点三级取值：内存实测快照（est_chars 现算）> 旧建档成对沿用 > 0
  3. observe_llm_usage 更新 _last_react_usage（净 prompt 刨 rf）
  4. _decide_switch_shape 集成：锚点让大体量建档不再被全场字符换算误判超窗
"""
import sys, json, types, pathlib as _pl
sys.path.insert(0, str(_pl.Path(__file__).resolve().parents[1] / "src"))

import session as ss

ok = fail = 0


def chk(name, cond, extra=""):
    global ok, fail
    if cond:
        ok += 1
        print(f"  ✅ {name}")
    else:
        fail += 1
        print(f"  ❌ {name}  {extra}")


def mk_turn(user, answer="", reasoning="", steps=()):
    return types.SimpleNamespace(user_message=user, answer=answer,
                                 answer_reasoning=reasoning, steps=list(steps))


def mk_step(reasoning="", calls=()):
    return types.SimpleNamespace(reasoning=reasoning, tool_calls=list(calls))


def mk_call(cid):
    return types.SimpleNamespace(call_id=cid)


def mk_sess(turns=None, cpt=4.0, schema_chars=0):
    s = ss.Session.__new__(ss.Session)
    s.turns = turns or []
    s._chars_per_token = cpt
    s._tools_schema_chars = schema_chars
    s.toollog = {}                       # dict.get(cid) → None → result 记 0
    s.system = "SYS"                     # _tier_prefix_msgs 默认路径只读 system
    s._detail_base = 1500                # detail_base property 的缓存（property 无 setter）
    s.max_level = 4
    return s


print("== 1. _shape_chars_now / _est_shape_chars 口径一致 ==")
# 带 steps+大 tool result 的轮：档位渲染受 cap 截断，折叠（fc>0）收益才显著；
# 纯文本轮折进摘要只省 cap 内 steps 体量、还多 60/轮包装（不是 bug，是口径真实行为）
turns = [mk_turn(f"问题{i}号" * 30, answer="答" * 200,
                 steps=[mk_step(reasoning="思" * 100, calls=[mk_call(f"c{i}")])]) for i in range(20)]
s = mk_sess(turns, cpt=4.0, schema_chars=12000)
s.toollog = {f"c{i}": {"result": "R" * 3000} for i in range(20)}
c1 = s._shape_chars_now([], 0)
c2 = s._shape_chars_now([5, 10], 8)
chk("有边界+fc 时体量显著小于全量形态", c2 < c1 * 0.7, f"{c2} vs {c1}")
chk("est_shape_chars = chars_now / cpt（老行为不变）", s._est_shape_chars([], 0) == int(c1 / 4.0))

print("== 2. _est_shape_tokens 两级 ==")
# 2.1 无锚点 → chars 老路径
est, mode = s._est_shape_tokens({"tier_boundaries": [], "fold_count": 0})
chk("无锚点 → chars 模式（= 老口径）", mode == "chars" and est == int(c1 / 4.0), f"{est},{mode}")

# 2.2 有锚点 + 正增量：est = lt + Δ/cpt_model（cpt 用该模型实测 lc/lt=2.0，不是全局 4.0）
#     建档时体量 lc=20轮基线、实测 lt=lc/2；此后新增 1 轮 → Δ>0，锚定式=实测存量+增量/2
s2 = mk_sess(turns + [mk_turn("新增" * 500)], cpt=4.0, schema_chars=12000)
s2.toollog = s.toollog
base_c = s._shape_chars_now([], 0)          # 建档时体量（20 轮）
shape = {"tier_boundaries": [], "fold_count": 0, "est_chars": base_c, "est_tokens": base_c // 2}
chars_now2 = s2._shape_chars_now([], 0)
est2, mode2 = s2._est_shape_tokens(shape)
_cpt2 = base_c / (base_c // 2)                 # 同式复算（lc/lt 非精确 2.0，期望值须同式）
expect2 = max(0, int(base_c // 2 + (chars_now2 - base_c) / _cpt2))
chk("锚点 → anchor 模式，est=lt+Δ/实测cpt", mode2 == "anchor" and est2 == expect2,
    f"{est2} vs {expect2} ({mode2})")
chk("实测比率低于全局cpt时，全局口径会低估（锚定式更准）",
    est2 > int(chars_now2 / 4.0) and chars_now2 > base_c, f"{est2} vs {int(chars_now2/4.0)}")

# 2.3 负增量（rewind 后体量变小）：est < lt 且 ≥ 0
est3, mode3 = s._est_shape_tokens({"tier_boundaries": [], "fold_count": 0,
                                   "est_chars": 500_000, "est_tokens": 250_000})
chk("负增量 → est < lt（rewind 场景）", mode3 == "anchor" and 0 <= est3 < 250_000, f"{est3},{mode3}")

# 2.4 cpt clamp：lc/lt=0.5（坏数据）→ clamp 1.0；lc/lt=16 → clamp 8.0
est_a, _ = s._est_shape_tokens({"tier_boundaries": [], "fold_count": 0,
                                "est_chars": 100, "est_tokens": 200})   # lc/lt=0.5
chk("cpt 下限 clamp 1.0", est_a == max(0, int(200 + (c1 - 100) / 1.0)), str(est_a))
est_b, _ = s._est_shape_tokens({"tier_boundaries": [], "fold_count": 0,
                                "est_chars": 1600, "est_tokens": 100})   # lc/lt=16
chk("cpt 上限 clamp 8.0", est_b == max(0, int(100 + (c1 - 1600) / 8.0)), str(est_b))


print("== 3. _save_tier_shape 锚点三级取值 ==")
saved = {}


def mk_save_sess():
    s = mk_sess(turns)
    s.session_dir = _pl.Path(".")          # 只要不 None（_sidecar_write 已 mock）
    s.llm = types.SimpleNamespace(model_name="glm")
    s.tiering_mode = ""
    s._tier_boundaries = [5, 10]
    s._planned_fold = 8
    s._sos_count = 0
    s._sos_text = ""
    s.global_summary = ""
    s._profile_fingerprint = lambda: "glm|0|131072"
    s._sidecar_write = lambda p, d: (saved.update(d) or True)
    s._shape_chars_now = lambda bs, fc: 424242              # 现算口径固定值（观察是否被采用）
    return s


s3 = mk_save_sess()
s3._read_tier_shape = lambda p: {"est_tokens": 9000, "est_chars": 18000}   # 旧建档有锚点
s3._last_react_usage = {"net_prompt": 12345, "prompt": 13000, "rf_tok": 655, "ts": 0, "turns": 20,
                        "model": "glm"}
s3._save_tier_shape(provider="glm")
chk("内存快照优先（model 匹配）：est_tokens=net_prompt、est_chars=现算",
    saved.get("est_tokens") == 12345 and saved.get("est_chars") == 424242, str(saved.get("est_tokens")))

s3b = mk_save_sess()
s3b._read_tier_shape = lambda p: {"est_tokens": 9000, "est_chars": 18000}
s3b._last_react_usage = {"net_prompt": 12345, "prompt": 13000, "rf_tok": 655, "ts": 0, "turns": 20,
                         "model": "deepseek"}   # 快照属于别的模型（切到本模型未跑轮）
s3b._save_tier_shape(provider="glm")
chk("model 不匹配 → 不用快照，旧建档成对沿用（防张冠李戴）",
    saved.get("est_tokens") == 9000 and saved.get("est_chars") == 18000, str(saved))

s4 = mk_save_sess()
s4._read_tier_shape = lambda p: {"est_tokens": 9000, "est_chars": 18000}   # 旧建档有 → 成对沿用
s4._last_react_usage = {}                                                  # 重启后未跑轮
s4._save_tier_shape(provider="glm")
chk("无内存 → 旧建档成对沿用（lt=9000, lc=18000 原样）",
    saved.get("est_tokens") == 9000 and saved.get("est_chars") == 18000, str(saved))

s5 = mk_save_sess()
s5._read_tier_shape = lambda p: None                                        # 旧建档无锚点字段
s5._last_react_usage = {}
s5._save_tier_shape(provider="glm")
chk("全无 → est_tokens=0（锚点退化）", saved.get("est_tokens") == 0, str(saved))


print("== 4. observe_llm_usage 更新 _last_react_usage ==")
s6 = mk_sess([mk_turn("q")])
s6.max_effective_context_window = 200_000
s6.llm = types.SimpleNamespace(model_name="glm", profile_panic_window=None)
s6._rf_chars = lambda: 1200            # rf ≈ 300 tok（cpt=4）
s6._count_chars = lambda msgs: 100_000
_append_backup = ss.Session._append_token_usage
import config as _cfg2
_panic_backup = _cfg2.load_panic_window_opt
_cfg2.load_panic_window_opt = lambda: 0          # panic 三层链走层③：win×2（不受真 settings 影响）
ss.Session._append_token_usage = staticmethod(lambda rec: None)
try:
    s6.observe_llm_usage([{"role": "user", "content": "x"}],
                         {"prompt_tokens": 50_300, "completion_tokens": 100,
                          "total_tokens": 50_400}, extra_chars=0)
finally:
    ss.Session._append_token_usage = _append_backup
    _cfg2.load_panic_window_opt = _panic_backup
u = s6._last_react_usage
# rf_tok 用的是【校准后】比率（observe 先 EMA 更新 cpt 再算 rf）：同式复算期望值
_cpt_after = round(0.5 * 4.0 + 0.5 * min(max(100_000 / 50_300, 1.0), 8.0), 3)
_rf = int(1200 / max(0.1, _cpt_after))
chk("快照=净 prompt（刨 rf 估算，同式口径）", u.get("net_prompt") == 50_300 - _rf,
    f"rf={_rf} net={u.get('net_prompt')}")
chk("快照带 prompt/rf_tok/turns", u.get("prompt") == 50_300 and u.get("rf_tok") == _rf and u.get("turns") == 1)


print("== 5. _decide_switch_shape 集成（锚点救回被误判的 adopt）==")
import config as _cfg
_gp_backup = _cfg.get_profile
try:
    _cfg.get_profile = lambda n: {}
    # 大体量会话：全场字符 1.2M、全局 cpt=1.5（中文会话被污染/校准到低值）→ 老口径 est=800K 超 win
    big = mk_sess([mk_turn("问" * 800, answer="答" * 1200) for _ in range(300)], cpt=1.5, schema_chars=12000)
    big.session_dir = _pl.Path(".")
    big.llm = types.SimpleNamespace(model_name="deepseek", max_effective_context_window=65536,
                                    vision_supported=False)
    big._profile_fingerprint = lambda: "deepseek|0|65536"
    shape_ok = {"provider": "deepseek", "profile_fp": "deepseek|0|65536",
                "tier_boundaries": [50, 120, 200, 260], "fold_count": 240,
                "est_chars": 900_000, "est_tokens": 60_000}   # 该模型实测：900K 字→60K tok（cpt=15→clamp 8）
    big._read_tier_shape = lambda p: shape_ok
    est_old = big._est_shape_chars(shape_ok["tier_boundaries"], shape_ok["fold_count"])
    r = big._decide_switch_shape("deepseek")
    est_new, mode_new = big._est_shape_tokens(shape_ok)
    chk("老口径 est 超窗而锚定式通过（建档被救回）", est_old > 65536 and r is shape_ok and mode_new == "anchor",
        f"old={est_old} new={est_new}[{mode_new}] adopt={'hit' if r else 'miss'}")
    # 锚点也真超窗 → 重算
    shape_bad = dict(shape_ok, est_tokens=60_000, est_chars=30_000)   # 实测密度高：30K字→60K tok
    big._read_tier_shape = lambda p: shape_bad
    r2 = big._decide_switch_shape("deepseek")
    est_b2, _ = big._est_shape_tokens(shape_bad)
    chk("锚定式真超窗 → 仍重算（不误放行）", r2 is None and est_b2 > 65536, f"est={est_b2}")
finally:
    _cfg.get_profile = _gp_backup

print(f"\n结果：{ok} 过 / {fail} 挂")
sys.exit(1 if fail else 0)
