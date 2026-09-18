# 行级视图白名单阈值随投影形态验证（用户裁定 2026-09-16）：
#   recent_file 完整投影 = 模型带行号看过全文 → _note_view 注册，replace_lines 前置校验放行。
#   阈值：施工内嵌 ≤RF_MAX_CHARS(100K) 全文行号化（append-only 定型，投出去的就是模型收到的）；
#         非施工段式 ≤RF_SEG_MAX_CHARS(15K)，超限只投 outline 不算。md 恒不算（摘要态）。
#   防线回归：写后版本变即作废、每轮清零。
import sys
import shutil
import types as types_mod
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from session import Session, Step, ToolCall, RF_MAX_CHARS, RF_SEG_MAX_CHARS
from real_tools import (_file_version, _view_covers, _note_view, reset_line_views,
                        replace_lines, WORKSPACE)
import agent_config
from agent import Agent

ok = True
def check(name, cond, detail=""):
    global ok
    print(("✅" if cond else "❌"), name, detail)
    ok = ok and cond

TMP = WORKSPACE / "tmp_line_view_gate"
if TMP.exists():
    shutil.rmtree(TMP)
TMP.mkdir()
mid = TMP / "mid.py"                      # 30K：15K < 30K < 100K（阈值拆分区间的锚点尺寸）
mid.write_text("x = 1\n" + "y = 2\n" * 7_499, encoding="utf-8")
mid_lines = len(mid.read_text(encoding="utf-8").splitlines())
md = TMP / "note.md"                      # md：两形态都是摘要态，恒不注册
md.write_text("# t\n" + "正文\n" * 200, encoding="utf-8")

def set_construction(sess, on: bool):
    agent_config._RUNTIME_AGENT = (types_mod.SimpleNamespace(
        session=sess, active_plan={"steps": [{"status": "pending"}]})
        if on else None)

def collect(sess, path):
    """跑一遍 _collect_file_snapshots（Agent 未绑定方法 + session stub）→ 该文件的快照采集+视图记账。"""
    s = sess
    s.start_turn("行级视图")
    cid = "lv1"
    s.toollog.record(cid, "edit", {"path": str(path)}, "done")
    step = Step(tool_calls=[ToolCall(call_id=cid)])
    step.file_snapshots = Agent._collect_file_snapshots(types_mod.SimpleNamespace(session=s), step)
    return step.file_snapshots

# —— 1. 施工模式：30K 文件注册全文件视图（阈值=RF_MAX_CHARS 100K） ——
s1 = Session(system="t", llm=None)
set_construction(s1, True)
check("施工判定：True", s1._construction_mode() is True)
reset_line_views()
snaps = collect(s1, mid)
check("施工：快照已采集（30K 全文）", "lv1" in snaps and len(snaps["lv1"]["text"]) > RF_SEG_MAX_CHARS,
      f"keys={list(snaps)} len={len(snaps.get('lv1', {}).get('text', ''))}")
cov, spans = _view_covers(mid, 1, mid_lines)
check("施工：30K 文件注册全文件视图（≤100K 放行）", cov, f"spans={spans}")
# replace_lines 真实放行（拿注册视图 + 当前 version，无需先 read_file）
ver = _file_version(mid)
r = replace_lines(str(mid), [{"range": [1, 1], "content": "x = 100  # replaced"}], ver)
check("施工：replace_lines 直接放行（tool result 全文即视图）", r.startswith("✅"), r[:80])

# —— 2. 非施工：同一 30K 文件不注册（段式 >15K 只投 outline） ——
s2 = Session(system="t", llm=None)
set_construction(s2, False)
reset_line_views()
collect(s2, mid)
cov2, _ = _view_covers(mid, 1, mid_lines)
check("非施工：30K 文件不注册（>15K 段式只投 outline）", not cov2)
r2 = replace_lines(str(mid), [{"range": [1, 1], "content": "x = 1"}], _file_version(mid))
check("非施工：replace_lines 拒绝并要求 read_file", r2.startswith("[缺行级视图]"), r2[:60])

# —— 3. 小文件（≤15K）两形态都注册（原行为不变） ——
small = TMP / "small.py"
small.write_text("a = 1\nb = 2\n", encoding="utf-8")
reset_line_views()
collect(s2, small)
cov_s, _ = _view_covers(small, 1, 2)
check("非施工：小文件（≤15K）注册视图照旧", cov_s)

# —— 4. md 恒不注册（摘要态非原文行号） ——
reset_line_views()
collect(s2, md)
cov_m, _ = _view_covers(md, 1, 10)
check("md：恒不注册（_md_snapshot 摘要态）", not cov_m)

# —— 5. 防线回归：写后版本变 → 视图作废 ——
reset_line_views()
collect(s2, small)
small.write_text("a = 9\nb = 2\nc = 3\n", encoding="utf-8")   # 外部改（版本变）
cov_v, _ = _view_covers(small, 1, 2)
check("版本作废：文件改后旧视图失配", not cov_v)

# —— 6. 防线回归：每轮清零 ——
reset_line_views()
cov_r, _ = _view_covers(small, 1, 3)
check("每轮清零：reset 后无视图", not cov_r)

set_construction(s2, False)
agent_config._RUNTIME_AGENT = None
shutil.rmtree(TMP)
print("\n" + ("ALL PASS ✅" if ok else "SOME FAILED ❌"))
sys.exit(0 if ok else 1)
