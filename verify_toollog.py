"""verify_toollog.py —— 工具详情库 + 距离衰减摘要 + get_tool_detail 当前步不摘要 + 持久化 + 旧格式迁移。

用 FakeLLM 避免真实 LLM 调用；临时 workspace 不污染真实库。
"""
import sys
import json
import tempfile
import shutil
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from session import Session, Step, ToolCall  # noqa: E402
from toollog import detail_limit, make_tool_log_tools  # noqa: E402


class _FakeLLM:
    """占位 LLM（验证不触发 finish_turn 的摘要，故不会被真正调用）。"""
    def chat(self, msgs):
        class _R: content = "(摘要)"; reasoning = ""
        return _R()


class FakeAgent:
    def __init__(self, session):
        self.session = session


def _add_call(session, name, args, result):
    """模拟 agent 三分支：next_id → record 详情 → ToolCall(call_id) 进新 step。"""
    step = Step()
    cid = session.toollog.next_id()
    session.toollog.record(cid, name, args, result)
    step.tool_calls.append(ToolCall(call_id=cid))
    if session._current is None:
        session.start_turn("test")
    session.add_step(step)
    return cid


def main():
    tmp = Path(tempfile.mkdtemp(prefix="agt_tl_test_"))
    print(f"临时 workspace: {tmp}")
    try:
        s = Session(system="sys", llm=_FakeLLM(), workspace=tmp)
        s.start_turn("跑几个工具")
        long_result = "X" * 3000       # 超 limit
        long_args = {"code": "Y" * 3000}

        c0 = _add_call(s, "run_python", long_args, long_result)   # distance 2（历史）
        c1 = _add_call(s, "run_python", long_args, long_result)   # distance 1（历史）
        c2 = _add_call(s, "run_python", long_args, long_result)   # distance 0（当前步）
        msgs = s._steps_to_messages(s._current.steps)

        # —— 当前步(distance0)完整披露 / 历史步按距离衰减 ——
        tool_msgs = [m for m in msgs if m["role"] == "tool"]
        assert len(tool_msgs) == 3, f"应有3条tool消息，实际{len(tool_msgs)}"
        for m, cid, d in zip(tool_msgs, [c0, c1, c2], [2, 1, 0]):
            content = m["content"]
            if d == 0:
                assert content == "X" * 3000, f"当前步所有工具结果应完整3000，实际{len(content)}"
                print(f"[结果·当前步完整] distance0 完整 {len(content)} 字（不限工具）✓")
            else:
                lim = detail_limit(d)
                assert f"id={cid}" in content and "get_tool_detail" in content, f"distance{d} 缺 id 提示"
                assert len(content) < 3000, f"distance{d} 历史步应截断"
                print(f"[结果·距离{d}] limit={lim} → 截断到{len(content)}字 ✓")

        # —— 入参：当前步完整 / 历史步摘要（保 JSON 合法）——
        a_msgs = [m for m in msgs if m["role"] == "assistant" and m.get("tool_calls")]
        cur_args = json.loads(a_msgs[-1]["tool_calls"][0]["function"]["arguments"])    # distance0 完整
        assert len(cur_args["code"]) == 3000, f"当前步入参应完整3000，实际{len(cur_args['code'])}"
        hist_args = json.loads(a_msgs[0]["tool_calls"][0]["function"]["arguments"])    # distance2 摘要
        assert len(hist_args["code"]) < 3000 and "get_tool_detail" in hist_args["code"], "历史步入参应截断"
        print(f"[入参] 当前步完整 {len(cur_args['code'])} / 历史 distance2 截断到 {len(hist_args['code'])} ✓")

        # —— get_tool_detail 工具：多 id（逗号/空格）+ 单 id 兼容 ——
        tools = make_tool_log_tools(FakeAgent(s))
        gtd = next(t for t in tools if t.name == "get_tool_detail")
        out = gtd.run(call_id=f"{c1},{c2}")                 # 逗号分隔多 id
        assert f"完整详情·{c1}" in out and f"完整详情·{c2}" in out and "X" * 3000 in out, "多 id 应都返回完整"
        out_sp = gtd.run(call_id=f"{c0} {c1}")              # 空格分隔也行
        assert f"完整详情·{c0}" in out_sp and f"完整详情·{c1}" in out_sp
        assert "完整详情" in gtd.run(call_id=c0)             # 单 id 兼容
        assert "无此 id" in gtd.run(call_id="zzz")
        print(f"[get_tool_detail 多id] 逗号/空格分隔批量返回 / 单id兼容 / 未知名报错 ✓")

        # 注：save/load 持久化 + 旧格式迁移在 verify_events.py 覆盖（event-sourcing 模型）；
        # 本脚本聚焦【距离衰减摘要 + get_tool_detail】的内存逻辑，不重复测持久化。

        print("\n🎉 验证全通过")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        try:
            from session import _repo_hash, REPOS_DIR
            h = _repo_hash(tmp)
            rd = REPOS_DIR / h
            if rd.exists():
                shutil.rmtree(rd, ignore_errors=True)
                print(f"(清理 ~/.agt/repos/{h})")
        except Exception:
            pass


if __name__ == "__main__":
    main()
