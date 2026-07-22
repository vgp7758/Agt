"""verify_events.py —— event-sourcing 持久化验证：事件流/重放/不丢弃/restore/旧格式迁移。

FakeLLM（计数器命名避免同 workspace 撞名）；临时 workspace 不污染真实库。
"""
import sys
import json
import tempfile
import shutil
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from session import Session, Step, ToolCall, _read_events  # noqa: E402
from session import _repo_sessions_dir  # noqa: E402


class FL:
    """FakeLLM：chat 返回递增 name（让每个 session 命名不撞）；summary 占位。"""
    _n = 0

    def chat(self, msgs):
        type(self)._n += 1
        class _R: content = f"test{type(self)._n}"; reasoning = ""
        return _R()


def _call(s, name, args, result):
    """record 一条 toollog 详情，返回 call_id（不 add_step）。"""
    cid = s.toollog.next_id()
    s.toollog.record(cid, name, args, result)
    return cid


def main():
    tmp = Path(tempfile.mkdtemp(prefix="agt_ev_test_"))
    print(f"临时 workspace: {tmp}")
    try:
        # ===== 1. 写两轮 + append 不重写 + 重放一致 =====
        s = Session("sys", llm=FL(), workspace=tmp)
        s.start_turn("第一轮"); s.record_snapshot("sha1")
        c1 = _call(s, "run_python", {"code": "1+1"}, "2")
        s.add_step(Step(reasoning="算一下", tool_calls=[ToolCall(call_id=c1)]))
        s.finish_turn("答案是2")
        s.start_turn("第二轮")
        c2 = _call(s, "grep", {"pattern": "x"}, "命中3处")
        s.add_step(Step(reasoning="搜一下", tool_calls=[ToolCall(call_id=c2)]))
        s.finish_turn("找到了")
        name = s.name
        rsd = _repo_sessions_dir(tmp)
        ev, tl = rsd / f"{name}.events.jsonl", rsd / f"{name}.toollog.jsonl"
        assert ev.exists() and tl.exists(), "events/toollog 文件应已建立"
        events = _read_events(ev)
        # 2×(turn_start+step+turn_end)=6 + 1 snapshot = 7
        assert len(events) == 7, f"应有7事件，实际{len(events)}"
        print(f"[append 不重写] events.jsonl {len(events)} 行（第二轮未覆盖第一轮）✓")

        s.save(name)
        s2 = Session.load(name, llm=FL(), workspace=tmp)
        assert len(s2.turns) == 2 and s2.turns[0].answer == "答案是2"
        assert s2.turns[1].steps[0].tool_calls[0].call_id == c2
        assert s2.toollog.view(c1) == ("run_python", {"code": "1+1"}, "2")
        # load 后 messages_for_llm 能组装（读 turns + toollog 召回）
        msgs = s2._steps_to_messages(s2.turns[1].steps)
        assert any(m["role"] == "tool" for m in msgs), "load 后能组装 tool 消息"
        print(f"[重放一致] load 后 2 轮 + toollog 召回 + 组装 OK ✓")

        # ===== 2. 未完成 turn 不丢弃（模拟中断）=====
        s3 = Session("sys", llm=FL(), workspace=tmp)
        s3.start_turn("r1完"); s3.record_snapshot("s1")
        cc = _call(s3, "run_python", {"code": "x"}, "y")
        s3.add_step(Step(reasoning="r", tool_calls=[ToolCall(call_id=cc)]))
        s3.finish_turn("a1")                  # name 就绪 → events 文件建立
        n3 = s3.name
        s3.start_turn("r2中断")               # 第二轮：开始但不 finish
        cc2 = _call(s3, "run_shell", {"cmd": "ls"}, "file1\nfile2")
        s3.add_step(Step(reasoning="rs", tool_calls=[ToolCall(call_id=cc2)]))
        s3.save(n3)                            # _current 不进 session.json，但事件已 append
        ev3 = _repo_sessions_dir(tmp) / f"{n3}.events.jsonl"
        assert len(_read_events(ev3)) == 6, "r1(4) + r2(turn_start+step=2) = 6"
        s4 = Session.load(n3, llm=FL(), workspace=tmp)
        assert len(s4.turns) == 2, f"未完成 turn 应进 turns，实际{len(s4.turns)}"
        assert s4.turns[1].user_message == "r2中断" and s4.turns[1].answer == "", "未完成 turn 无 answer"
        assert len(s4.turns[1].steps) == 1 and s4.turns[1].steps[0].tool_calls[0].call_id == cc2
        print(f"[未完成 turn 不丢弃] r2中断 作为无 answer 的 turn 进 turns，steps 保留 ✓")

        # ===== 3. restore 事件重放截断 =====
        s.restore_to_snapshot("sha1")          # sha1 是第0轮 → keep=0 → 全截断
        s.save(name)
        s5 = Session.load(name, llm=FL(), workspace=tmp)
        assert len(s5.turns) == 0, f"restore keep=0 后应0轮，实际{len(s5.turns)}"
        print(f"[restore 重放截断] restore(keep=0) 事件 → 重放得 0 轮 ✓")

        # ===== 4. 旧格式迁移：0.7.4（turns + toollog 字段，ToolCall 只 call_id）=====
        old074 = {
            "name": "old074", "system": "sys", "global_summary": "",
            "recent_window_turns": 4, "max_steps_per_turn": 80,
            "turns": [{"user_message": "旧问", "images": [], "snapshot_sha": "", "answer": "旧答",
                       "answer_reasoning": "", "summary": "旧摘", "steps": [
                {"reasoning": "r", "tool_calls": [{"call_id": "c1"}]}]}],
            "toollog": [{"call_id": "c1", "name": "read_file", "arguments": {"path": "a"}, "result": "内容"}],
            "extra_state": {},
        }
        (_repo_sessions_dir(tmp) / "old074.json").write_text(json.dumps(old074, ensure_ascii=False), encoding="utf-8")
        so = Session.load("old074", llm=FL(), workspace=tmp)
        assert (_repo_sessions_dir(tmp) / "old074.events.jsonl").exists(), "迁移应建立 events.jsonl"
        assert (_repo_sessions_dir(tmp) / "old074.toollog.jsonl").exists(), "迁移应建立 toollog.jsonl"
        assert len(so.turns) == 1 and so.turns[0].steps[0].tool_calls[0].call_id == "c1"
        assert so.toollog.view("c1") == ("read_file", {"path": "a"}, "内容")
        print(f"[0.7.4 迁移] turns+toollog字段 → events.jsonl + toollog.jsonl，详情可召回 ✓")

        # ===== 5. 更老格式迁移（ToolCall 自带 name/args/result）=====
        ancient = {
            "name": "ancient", "system": "sys", "global_summary": "",
            "recent_window_turns": 4, "max_steps_per_turn": 80,
            "turns": [{"user_message": "古问", "images": [], "snapshot_sha": "", "answer": "古答",
                       "answer_reasoning": "", "summary": "", "steps": [
                {"reasoning": "", "tool_calls": [
                    {"id": "call_x", "name": "run_python", "arguments": {"code": "old"}, "result": "old_out"}]}]}],
            "extra_state": {},
        }
        (_repo_sessions_dir(tmp) / "ancient.json").write_text(json.dumps(ancient, ensure_ascii=False), encoding="utf-8")
        sa = Session.load("ancient", llm=FL(), workspace=tmp)
        assert len(sa.turns) == 1
        tc = sa.turns[0].steps[0].tool_calls[0]
        assert tc.call_id and sa.toollog.view(tc.call_id) == ("run_python", {"code": "old"}, "old_out")
        assert (_repo_sessions_dir(tmp) / "ancient.events.jsonl").exists()
        print(f"[更老格式迁移] ToolCall(name/args/result) → 生成 call_id 入 toollog + events ✓")

        print("\n🎉 事件流验证全通过")
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
