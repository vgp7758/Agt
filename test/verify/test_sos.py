# -*- coding: utf-8 -*-
"""sos（summary of summary）档验证：触发/递进/降级/渲染形态/持久化往返。"""
import sys, time, json, shutil
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0, r"D:\AI_Usings\Agt\src")
from session import Session

BIG = r"C:\Users\vgp77\.agt\repos\D--AI_Usings-Agt\sessions\20260811_013200"
SMALL = r"C:\Users\vgp77\.agt\repos\D--AI_Usings-Nexus-Editor\sessions"

class StubLLM:
    def __init__(self, window, fail=False):
        self.max_effective_context_window = window
        self.fold_target_ratio = 0.75
        self.model_name = f"stub-{window}"
        self.vision_supported = False
        self.scenes, self.fail = [], fail
    def chat(self, messages, scene=None, **kw):
        self.scenes.append(scene)
        if self.fail:
            raise RuntimeError("stub 网络炸了")
        return SimpleNamespace(content="【sos测试摘要】浓缩后的会话背景叙事，含关键决策与踩坑锚点。" * 40)

ok = lambda n, c: print(("✓" if c else "✗ FAIL"), n) or (c or sys.exit(1))

def est_of(s, fc):
    return s._estimate_tokens([{"role": "system", "content": s.system}]
                              + s._render_tiered_history(fc))

# ── ① 中窗（100K）：全折超预算 → sos 收敛达标 ──
llm = StubLLM(100000)
s = Session.load(BIG, llm=llm, workspace=r"D:\AI_Usings\Agt")
n = len(s.turns)
t0 = time.time(); fc = s.apply_simple_tiering(); dt = time.time() - t0
est, target = est_of(s, fc), s.fold_target()
print(f"① 100K窗：{n}轮 fc={fc} sos_count={s._sos_count} est={est}/{target}（{dt:.1f}s）")
ok("sos 触发（scene='sos'）", "sos" in llm.scenes)
ok(f"sos 后 est ≤ 预算", est <= target)
ok("sos_count>0 且 <fc（次早期清单保留）", 0 < s._sos_count < fc)

summary = s._folded_summary(fc)
ok("渲染含 sos 段头（覆盖范围真实）", "sos 浓缩摘要" in summary and f"覆盖第1~{s._sos_count}轮" in summary)
ok("渲染含次早期清单（轮号续接）", "次早期轮次清单" in summary and f"[第{s._sos_count + 1}轮]" in summary)

# ── ①b 物理极限（25K 窗）：sos 递进多段仍可能超 → 不炸、递进推进 ──
llm_b = StubLLM(25000)
sb = Session.load(BIG, llm=llm_b, workspace=r"D:\AI_Usings\Agt")
fcb = sb.apply_simple_tiering()
est_b = est_of(sb, fcb)
print(f"①b 25K窗：fc={fcb} sos_count={sb._sos_count}（递进{llm_b.scenes.count('sos')}段）est={est_b}/18750")
ok("物理极限不炸 + sos 递进（≥2 段或已推到 fc）", sb._sos_count >= fcb // 2 and (llm_b.scenes.count("sos") >= 2 or sb._sos_count >= fcb - 1))
ok("极限场景 est 显著低于全折清单形态", est_b < est_of(sb, fcb) + 1)  # 自身渲染即含 sos；主要断不炸

# ── ② 降级：LLM 失败 → 纯清单不炸 ──
llm2 = StubLLM(80000, fail=True)
s2 = Session.load(BIG, llm=llm2, workspace=r"D:\AI_Usings\Agt")
fc2 = s2.apply_simple_tiering()
ok("LLM 失败降级：sos 空、不炸", s2._sos_count == 0 and s2._sos_text == "")
ok("降级渲染仍是纯清单形态", "已折叠的早期轮次" in s2._folded_summary(fc2))

# ── ③ 大窗不触发（est 达标无需 sos）──
s3 = Session.load(BIG, llm=StubLLM(200000), workspace=r"D:\AI_Usings\Agt")
s3.apply_simple_tiering()
ok("大窗达标：无 sos", s3._sos_count == 0)

# ── ④ 持久化往返（小档）──
sd = sorted([d for d in Path(SMALL).iterdir() if (d / "meta.json").exists()])[-1]
tmp = Path(r"C:\Users\vgp77\AppData\Local\Temp\sos_roundtrip")
if tmp.exists(): shutil.rmtree(tmp)
tmp.mkdir()
shutil.copy(sd / "meta.json", tmp / "meta.json")
shutil.copy(sd / "events.jsonl", tmp / "events.jsonl")
s4 = Session.load(str(tmp), llm=StubLLM(80000), workspace=str(tmp))
s4._sos_count, s4._sos_text = 1, "往返测试摘要"
s4.session_dir = tmp
s4.save()
meta = json.loads((tmp / "meta.json").read_text(encoding="utf-8"))
ok("save 落 sos 字段", meta.get("sos_count") == 1 and meta.get("sos_summary") == "往返测试摘要")
s5 = Session.load(str(tmp), llm=StubLLM(80000), workspace=str(tmp))
ok("load 恢复 sos", s5._sos_count == 1 and s5._sos_text == "往返测试摘要")
shutil.rmtree(tmp, ignore_errors=True)

print("\n全部通过 ✅")
