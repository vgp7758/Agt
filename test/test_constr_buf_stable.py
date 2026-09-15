# 施工投影缓冲（_constr_buf）字节稳定性验证（2026-09-15·用户提案，治 t861_s38-s76 命中率上蹿下跳）：
#   turn 级 append-only——每 step 定型一次（full 形态 + 内嵌快照渲染），投影只拼不改。
#   核心断言：早期 step 的投影字节在 step 数增长（跨组边界 GROUP_STEPS=10）后不变；
#   前一次投影恒为后一次的字节前缀（LCP == 旧全长 → 前缀缓存只增长不失配）。
#   旧实现病灶：_steps_to_messages 每次投影按组差动态衰减 limit 重渲染早期 tool result
#   （full→_summarize_text 形态跳变 + limit 递减字节变化），施工内嵌又把快照全文焊进
#   前缀区——衰减重写时振幅放大。
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import types as types_mod

from session import Session, Step, ToolCall
import agent_config

ok = True
def check(name, cond, detail=""):
    global ok
    print(("✅" if cond else "❌"), name, detail)
    ok = ok and cond

def serialize(msgs: list[dict]) -> str:
    """投影 → 字节串（含 tool_calls.arguments / reasoning——衰减漂移的全部载体）。"""
    parts = []
    for m in msgs:
        piece = [f"{m.get('role', '?')}:__CONTENT__{m.get('content') or ''}"]
        for tc in (m.get("tool_calls") or []):
            piece.append(f"__TC__{tc.get('id')}|{tc['function']['name']}|{tc['function']['arguments']}")
        if m.get("reasoning_content"):
            piece.append(f"__RS__{m['reasoning_content']}")
        parts.append("\n".join(piece))
    return "\n".join(parts)

def set_construction(sess, on: bool):
    agent_config._RUNTIME_AGENT = (types_mod.SimpleNamespace(
        session=sess, active_plan={"steps": [{"status": "pending"}]})
        if on else None)

def mk_step(s: Session, i: int, path: str, ver: str, result_len: int = 5000) -> Step:
    """第 i 步：edit path（快照版本 ver）+ 长 result（>detail_base，旧实现衰减组必截：
    实测 detail_base=4000/detail_step=20 下，5000 字 result 在 group_diff≥2 时被重截
    → 前缀断裂于 step21——本测试锁定缓冲路径在同场景下前缀单调）。"""
    cid = f"c{i}"
    s.toollog.record(cid, "edit", {"path": path, "old": f"x{i}", "new": f"y{i}"},
                     f"ok {i} " + "z" * result_len)
    return Step(reasoning=f"思考{i}",
                tool_calls=[ToolCall(call_id=cid)],
                file_snapshots={cid: {"path": path, "version": ver,
                                      "text": "\n".join(f"line{j}-{ver}" for j in range(20))}})

# ========== 场景 1：前缀单调（核心）——逐步 append + 投影，旧投影恒为新投影的字节前缀 ==========
s = Session(system="t", llm=None)
set_construction(s, True)
s.start_turn("施工轮：逐步定型")
prev_text = ""
snapshots = []   # 各次投影串（跨组断言复用）
N = 24   # 覆盖衰减区：step0 的 group_diff 在 21 步时 =2（旧实现 limit 衰减重截、实测前缀断裂
         # 于 step21；缓冲路径定型不衰减——本测试锁定的正是这一点）
for i in range(N):
    s.add_step(mk_step(s, i, f"f{i % 3}.py", f"v{i}.{i}"))   # 3 个文件轮着写（多文件快照并行）
    cur = serialize(s._seg_msgs_steps())
    snapshots.append(cur)
    if prev_text:
        check(f"step{i}：前缀单调（旧投影是新投影的字节前缀）",
              cur.startswith(prev_text),
              f"LCP {'==' if cur.startswith(prev_text) else '<'} 旧全长 {len(prev_text)}")
    prev_text = cur

# 跨组边界显式断言：step0 的块在 24 步后（group_diff=2 衰减区——旧实现 limit 重截点）字节与首次定型一致
first_block = snapshots[0]
last_snap = snapshots[-1]
check("跨组边界（24步·group_diff=2 衰减区）：step0 定型块字节不变",
      last_snap.startswith(first_block),
      f"first={len(first_block)} last={len(last_snap)}")

# ========== 场景 2：幂等——同状态连投两次字节相同（缓冲不重复 append / 不改写） ==========
a1 = serialize(s._seg_msgs_steps())
a2 = serialize(s._seg_msgs_steps())
check("幂等：同状态连投两次字节相同", a1 == a2)
check("幂等：缓冲长度 == steps 数（不重复 append）",
      len(s._constr_buf) == len(s._current.steps))

# ========== 场景 3：重启回填——清缓冲（模拟进程重启）后投影与清前字节一致 ==========
s._constr_buf = []
b1 = serialize(s._seg_msgs_steps())
check("重启回填：清缓冲后投影与清前字节一致", b1 == a1)

# ========== 场景 4：同文件后续写不回溯改写前序块（t861 病灶直接回归） ==========
# step12 再改 f0.py（新版本快照）——step0/3/9 里 f0 的旧版本内嵌块必须原样
s.add_step(mk_step(s, 99, "f0.py", "v99.new"))
cur99 = serialize(s._seg_msgs_steps())
check("同文件后续写：前缀仍单调（旧块不回溯改写）", cur99.startswith(a1))
check("同文件后续写：新块含新版本 v99.new", 'version="v99.new"' in cur99)
check("同文件后续写：旧版本 v0.0 仍在原位（各挂各的当时快照）", 'version="v0.0"' in cur99)

# ========== 场景 5：生命周期——start_turn 清空 + 非施工轮零缓冲开销 ==========
s.finish_turn("done")
check("归档清空：finish_turn 后缓冲为空", s._constr_buf == [])
set_construction(s, False)
s.start_turn("非施工轮")
s.add_step(mk_step(s, 0, "x.py", "vx"))
_ = s._seg_msgs_steps()   # 非施工投影走原路
check("非施工零开销：投影后缓冲仍空（惰性 sync 不触发）", s._constr_buf == [])
set_construction(s, True)
cur = serialize(s._seg_msgs_steps())   # 施工激活：回填当前轮已有 step
check("施工激活回填：explore step（激活前归档）进缓冲",
      len(s._constr_buf) == 1 and 'version="vx"' in cur)

agent_config._RUNTIME_AGENT = None
print("\n" + ("ALL PASS ✅" if ok else "SOME FAILED ❌"))
sys.exit(0 if ok else 1)
