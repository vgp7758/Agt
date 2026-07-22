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
        long_result = "X" * 3000       # 超 limit，必被摘要
        long_args = {"code": "Y" * 3000}

        c0 = _add_call(s, "run_python", long_args, long_result)   # distance 2
        c1 = _add_call(s, "run_python", long_args, long_result)   # distance 1
        c2 = _add_call(s, "run_python", long_args, long_result)   # distance 0
        msgs = s._steps_to_messages(s._current.steps)

        # —— 结果按距离衰减 ——
        tool_msgs = [m for m in msgs if m["role"] == "tool"]
        assert len(tool_msgs) == 3, f"应有3条tool消息，实际{len(tool_msgs)}"
        for m, cid, d in zip(tool_msgs, [c0, c1, c2], [2, 1, 0]):
            lim = detail_limit(d)
            content = m["content"]
            assert content.startswith("X" * 50), f"distance{d} 结果前缀不符"
            assert f"id={cid}" in content and "get_tool_detail" in content, f"distance{d} 缺 id 提示"
            assert len(content) < 3000, f"distance{d} 结果不该完整3000字"
            print(f"[结果·距离{d}] limit={lim} → 截断到{len(content)}字，含 id={cid} ✓")

        # —— 入参摘要（保 JSON 合法，截断长字符串值）——
        a_msgs = [m for m in msgs if m["role"] == "assistant" and m.get("tool_calls")]
        last_args = json.loads(a_msgs[-1]["tool_calls"][0]["function"]["arguments"])  # distance0 limit1500
        assert len(last_args["code"]) < 3000 and last_args["code"].startswith("Y" * 50), "入参 code 该被截断"
        assert "get_tool_detail" in last_args["code"], "入参截断应有 id 提示"
        print(f"[入参摘要] distance0 的 code: 3000 → {len(last_args['code'])}，JSON 仍合法 ✓")

        # —— get_tool_detail 当前步不摘要 ——
        s2 = Session(system="sys", llm=_FakeLLM(), workspace=tmp)
        s2.start_turn("拉详情")
        _add_call(s2, "get_tool_detail", {"call_id": "c0"}, "Z" * 3000)   # distance0
        g_content = [m for m in s2._steps_to_messages(s2._current.steps) if m["role"] == "tool"][0]["content"]
        assert g_content == "Z" * 3000, f"get_tool_detail 当前步应完整3000，实际{len(g_content)}"
        print(f"[get_tool_detail 当前步不摘要] 完整 {len(g_content)} 字 ✓")

        # —— get_tool_detail 工具拉完整 ——
        tools = make_tool_log_tools(FakeAgent(s))
        gtd = next(t for t in tools if t.name == "get_tool_detail")
        out = gtd.run(call_id=c0)
        assert "X" * 3000 in out and "完整详情" in out, "应拉回完整 3000 字详情"
        assert "无此 id" in gtd.run(call_id="zzz")
        print(f"[get_tool_detail 工具] 拉回 c0 完整详情 / 未知名报错 ✓")

        # —— save/load 持久化（先把进行中的轮收尾进 turns，save 才会落盘）——
        s._current.answer = "done"
        s._current.summary = "测试摘要"
        s.turns.append(s._current)
        s._current = None
        s.save("tl_test")
        s_loaded = Session.load("tl_test", llm=_FakeLLM(), workspace=tmp)
        assert len(s_loaded.toollog) == 3, f"load 后 toollog 应3条，实际{len(s_loaded.toollog)}"
        n, a, r = s_loaded.toollog.view(c0)
        assert n == "run_python" and r == "X" * 3000, "load 后详情应完整"
        # load 后仍能按距离衰减组装
        lm = s_loaded._steps_to_messages(s_loaded.turns[0].steps)
        assert len([x for x in lm if x["role"] == "tool"]) == 3
        print(f"[save/load] toollog {len(s_loaded.toollog)} 条完整恢复，组装正常 ✓")

        # —— 旧格式存档迁移（ToolCall 有 name/args/result，无 call_id/toollog）——
        old_data = {
            "name": "old_session", "system": "sys", "global_summary": "",
            "recent_window_turns": 4, "max_steps_per_turn": 80,
            "turns": [{"user_message": "hi", "answer": "yo", "summary": "", "steps": [
                {"reasoning": "", "tool_calls": [
                    {"id": "call_x", "name": "run_python", "arguments": {"code": "old"}, "result": "old_result"}
                ]}
            ]}],
            "extra_state": {},
        }
        from session import _repo_sessions_dir
        sd = _repo_sessions_dir(tmp)
        sd.mkdir(parents=True, exist_ok=True)
        (sd / "old_session.json").write_text(json.dumps(old_data, ensure_ascii=False), encoding="utf-8")
        s_old = Session.load("old_session", llm=_FakeLLM(), workspace=tmp)
        assert len(s_old.toollog) == 1, f"旧存档应迁移出1条详情，实际{len(s_old.toollog)}"
        tc = s_old.turns[0].steps[0].tool_calls[0]
        assert tc.call_id, "旧 ToolCall 应迁移成 call_id"
        assert s_old.toollog.view(tc.call_id) == ("run_python", {"code": "old"}, "old_result")
        om = s_old._steps_to_messages(s_old.turns[0].steps)
        assert any(x["role"] == "tool" for x in om), "旧存档迁移后能组装 tool 消息"
        print(f"[旧格式迁移] 旧 ToolCall → call_id={tc.call_id}，详情入 toollog，组装 OK ✓")

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
