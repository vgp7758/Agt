#!/usr/bin/env python
"""proj_simulator.py —— 投影形状模拟器（用户提案 2026-09-14）。

按 session 的 events.jsonl 从头重放，逐轮跑 _plan_fold()，输出上下文压缩形状的演化：

    轮号 | fc(已折叠轮数) | 边界数 | 档1..N 各几轮 | 工具折叠档轮数 | 投影 tok | 本轮动作

形状 = 「压缩阶梯」在时间轴上的展开：

    档1..N（文字档，工具结果按 detail_base 逐级截断）
      → 工具折叠档（raw_level > max_level：工具调用折成一行标注、保留 answer/reasoning 原文）
      → 结构摘要（fc 折叠，细节靠 recall 召回）

用途：
  · 一眼看出规则跑下来整个上下文长成什么形状（阶梯是否按「升档 → 工具折叠 → 摘要」走）；
  · --rule old|new 对照：同一条 events 流下两套规则的形状差异；
  · 与真实存档对照（meta.json 的 fold_count / tier_boundaries）。

用法：
    python tools/proj_simulator.py <session_dir>
        [--rule new|old]          old=禁用 deepen / 折叠按整档吃 / 不 prune 死边界
        [--win 700000]            最大有效窗口（默认取 proj_stats.json 反推，否则 700000）
        [--ratio 0.5]             折叠目标比例（目标线 = win×ratio）
        [--max-level 6]
        [--detail-base 1500]
        [--fold-deep-tools on|off]
        [--chars-per-token 2.0]   估算比率（默认取 proj_stats.json 的实测校准值）
        [--every N]               每 N 轮也打一行快照（默认只打「形状变化」的行）
        [--limit N]               只模拟前 N 轮
        [--csv out.csv]           全量导出逐轮快照
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import config                                                    # noqa: E402
from session import Session, _read_events, _replay_events         # noqa: E402


# ---------------- 环境读取 ----------------

def _read_json(p: Path) -> dict:
    try:
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    except Exception:
        return {}


def build_session(args, sdir: Path, cpt: float) -> Session:
    """构造无 LLM 的模拟 Session（不写盘、不调模型）。"""
    s = Session("(simulated persona)", llm=None)
    s.max_effective_context_window = args.win
    s.fold_target_ratio = args.ratio
    s.max_level = args.max_level
    s._detail_base = args.detail_base
    s._chars_per_token = cpt
    tl = sdir / "toollog.jsonl"
    if tl.exists():
        s.toollog.load_from_jsonl(tl)      # 工具结果：_steps_to_messages 渲染需要
    if args.rule == "old":
        # —— 旧规则复现：无 deepen、折叠按整档吃 ——
        s._deepen_oldest_tier = lambda *a, **k: False

        def _old_next_fold_target(fold_count, est_fn=None, target=0):
            for b in sorted(s._tier_boundaries):
                if b + 1 > fold_count:
                    return b + 1
            return None

        s._next_fold_target = _old_next_fold_target
    return s


# ---------------- 形状快照 ----------------

def shape_of(s: Session, fold_deep: bool) -> dict:
    """当前上下文形状：fc / 边界数 / 各档轮数 / 工具折叠档轮数 / 投影 tok。"""
    fc = s._planned_fold
    tiers: dict[int, int] = {}
    deep = 0
    for i in range(fc, len(s.turns)):
        if fold_deep and s._raw_tier_level(i) > s.max_level:
            deep += 1
        else:
            lv = s._tier_level(i)
            tiers[lv] = tiers.get(lv, 0) + 1
    body = s._render_tiered_history(fc)
    extra = s._seg_msgs_user_message() + s._seg_msgs_steps()   # 模拟时无进行中轮 → 空
    tok = s._estimate_tokens([{"role": "system", "content": s.system}] + body + extra)
    return {"fc": fc, "nb": len(s._tier_boundaries), "tiers": tiers,
            "deep": deep, "tok": tok}


def _describe(prev: dict | None, cur: dict, cnt: dict) -> str:
    parts = []
    if cnt.get("grad"):
        parts.append(f"升档{cnt['grad']}刀")
    if cnt.get("deep"):
        parts.append(f"推老档{cnt['deep']}刀")
    if prev is not None and cur["fc"] != prev["fc"]:
        d = cur["fc"] - prev["fc"]
        parts.append(f"折叠{'回退' if d < 0 else '+'}{abs(d)}轮→{cur['fc']}")
    if not parts:
        parts.append("纯追加")
    return "+".join(parts)


def _key(sh: dict) -> tuple:
    return (sh["fc"], sh["nb"], tuple(sorted(sh["tiers"].items())), sh["deep"])


# ---------------- 主流程 ----------------

def main():
    ap = argparse.ArgumentParser(description="投影形状模拟器（按 events.jsonl 从头重放）")
    ap.add_argument("session_dir", help="session 目录（含 events.jsonl / toollog.jsonl）")
    ap.add_argument("--rule", choices=["new", "old"], default="new")
    ap.add_argument("--win", type=int, default=None)
    ap.add_argument("--ratio", type=float, default=None)
    ap.add_argument("--max-level", type=int, default=None)
    ap.add_argument("--detail-base", type=int, default=None)
    ap.add_argument("--fold-deep-tools", choices=["on", "off"], default=None)
    ap.add_argument("--chars-per-token", type=float, default=None)
    ap.add_argument("--every", type=int, default=0, help="每 N 轮打一行快照（0=只打变化点）")
    ap.add_argument("--limit", type=int, default=0, help="只模拟前 N 轮（0=全部）")
    ap.add_argument("--csv", default=None, help="全量逐轮快照导出路径")
    ap.add_argument("--quiet", action="store_true", help="只打印汇总")
    args = ap.parse_args()

    sdir = Path(args.session_dir)
    ev = sdir / "events.jsonl"
    if not ev.exists():
        print(f"❌ 找不到 {ev}")
        sys.exit(1)

    ps = _read_json(sdir / "proj_stats.json")
    meta = _read_json(sdir / "meta.json")

    # 参数缺省：proj_stats.json（真实生效口径）> 引擎默认
    if args.win is None:
        if ps.get("fold_target") and ps.get("fold_ratio"):
            args.win = int(ps["fold_target"] / ps["fold_ratio"])
        else:
            args.win = 700_000
    if args.ratio is None:
        args.ratio = ps.get("fold_ratio") or 0.5
    if args.max_level is None:
        args.max_level = ps.get("max_level") or config.load_max_level()
    if args.detail_base is None:
        args.detail_base = 1500
    if args.chars_per_token is None:
        args.chars_per_token = ps.get("chars_per_token") or 4.0
    if args.fold_deep_tools is not None:
        _on = args.fold_deep_tools == "on"
        config.load_fold_deep_tools = lambda: _on      # 全局开关按模拟参数覆盖
    fold_deep = config.load_fold_deep_tools()

    turns = _replay_events(_read_events(ev))
    if args.limit:
        turns = turns[:args.limit]
    if not turns:
        print("❌ events 重放后没有任何 turn")
        sys.exit(1)

    s = build_session(args, sdir, args.chars_per_token)
    # 统计每次计划里各刀触发次数（区分升档 / 推老档）
    cnt = {"grad": 0, "deep": 0}
    _og, _od = s._graduate_once, s._deepen_oldest_tier

    def _g(*a, **k):
        r = _og(*a, **k)
        if r:
            cnt["grad"] += 1
        return r

    def _d(*a, **k):
        r = _od(*a, **k)
        if r:
            cnt["deep"] += 1
        return r

    s._graduate_once = _g
    s._deepen_oldest_tier = _d

    print(f"══ 投影形状模拟 ══  rule={args.rule}  win={args.win:,}  ratio={args.ratio}  "
          f"max_level={args.max_level}  fold_deep_tools={'on' if fold_deep else 'off'}  "
          f"chars/token={args.chars_per_token}")
    print(f"session: {sdir.name}  |  重放 {len(turns)} 轮"
          + (f"（events {len(_read_events(ev))} 条）" if args.limit else ""))
    if meta.get("fold_count") or meta.get("tier_boundaries"):
        print(f"真实存档: fold_count={meta.get('fold_count')}  边界={len(meta.get('tier_boundaries') or [])}")
    print()

    tiers_hdr = " ".join(f"档{i}" for i in range(1, args.max_level + 1))
    hdr = f"{'轮号':>6} {'fc':>6} {'边界':>5}  {tiers_hdr} {'折叠':>5} {'投影tok':>10}  动作"
    if not args.quiet:
        print(hdr)
        print("-" * len(hdr))

    rows = []
    prev_shape = None
    first_over = None
    for i, t in enumerate(turns):
        s.turns.append(t)                       # 该轮已完成（重放自带 steps/answer）
        cnt["grad"] = cnt["deep"] = 0
        before_bs = list(s._tier_boundaries)
        s._plan_fold()
        if args.rule == "old":
            # 旧规则没有 prune：把 < fc 的死边界恢复（否则 _fold_leap_target 的 bs[-max_level] 口径变了）
            mx = max(before_bs) if before_bs else -1
            s._tier_boundaries = before_bs + [b for b in s._tier_boundaries if b > mx]
        sh = shape_of(s, fold_deep)
        if first_over is None and sh["tok"] > args.win:
            first_over = i + 1
        act = _describe(prev_shape, sh, cnt)
        rows.append({"turn": i + 1, "fc": sh["fc"], "nb": sh["nb"],
                     "tiers": dict(sh["tiers"]), "deep": sh["deep"], "tok": sh["tok"], "act": act})
        changed = prev_shape is None or _key(sh) != _key(prev_shape)
        if not args.quiet and (changed or (args.every and (i + 1) % args.every == 0)):
            cells = " ".join(f"{sh['tiers'].get(k, 0):>3}" for k in range(1, args.max_level + 1))
            mark = "" if sh["tok"] <= args.win else "  ← 超窗"
            print(f"{i + 1:>6} {sh['fc']:>6} {sh['nb']:>5}  {cells} {sh['deep']:>5} "
                  f"{sh['tok']:>10,}  {act}{mark}")
        prev_shape = sh

    # ---------------- 汇总 ----------------
    last = rows[-1]
    print()
    print(f"── 汇总（rule={args.rule}）────────────────────────────")
    print(f"模拟轮数        : {len(rows)}")
    print(f"首次超窗        : {('第 ' + str(first_over) + ' 轮') if first_over else '未超窗'}")
    print(f"终态 fc         : {last['fc']}（{100 * last['fc'] / len(rows):.1f}% 轮已折叠）")
    print(f"终态边界数      : {last['nb']}")
    print(f"终态未折轮分布  : " + " ".join(f"档{k}={last['tiers'][k]}"
                                        for k in sorted(last["tiers"])) + f"  工具折叠档={last['deep']}")
    print(f"终态投影 tok    : {last['tok']:,}（win={args.win:,}，占比 {100 * last['tok'] / args.win:.1f}%）")
    if meta.get("fold_count"):
        print(f"与真实存档对照  : 真实 fold_count={meta['fold_count']} vs 模拟 fc={last['fc']}")
    print("─────────────────────────────────────────────────────")

    if args.csv:
        with open(args.csv, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow(["turn", "fc", "boundaries", "tokens", "deep_tier", "action"]
                       + [f"tier{k}" for k in range(1, args.max_level + 1)])
            for r in rows:
                w.writerow([r["turn"], r["fc"], r["nb"], r["tok"], r["deep"], r["act"]]
                           + [r["tiers"].get(k, 0) for k in range(1, args.max_level + 1)])
        print(f"✅ 逐轮快照已导出：{args.csv}")


if __name__ == "__main__":
    main()
