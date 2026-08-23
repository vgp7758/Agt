#!/usr/bin/env python3
"""test_context_panic.py —— 实测 token 校准 + 超窗/panic 判阈（session.observe_llm_usage）验证。
运行：python test/test_context_panic.py（在仓库根执行）

五场景：
① 校准：中文重投影 observe 一次 → _chars_per_token 向实测比率收敛，_estimate_tokens 不再 chars/4
② 超窗标记：total > win 未超 panic → _over_window_mark=True + jsonl 落 over 记录；start_turn 消费复位
③ panic 立即压缩：轮边界计划没超、轮内 steps 膨胀后 observe 超 panic → 立即 _plan_fold（折叠/升档生效）
④ 落盘/回读：临时目录写 jsonl → 新 Session init 读到同模型记录的比率初值（跨 session 校准）
⑤ 回归：win 未配置（窗口模式）observe 完全 no-op
"""
import json
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import config
import session as session_mod
from session import Session, Turn, Step, ToolCall

PASS, FAIL = 0, 0


def check(name, cond, detail=""):
    global PASS, FAIL
    mark = "✅" if cond else "❌"
    print(f"{mark} {name}" + (f"  {detail}" if detail and not cond else ""))
    PASS, FAIL = PASS + (1 if cond else 0), FAIL + (0 if cond else 1)


class FakeLLM:
    """假 client：只暴露 Session 用到的面（model_name / max_effective_context_window / chat）。"""
    model_name = "fake-model"
    vision_supported = False

    def __init__(self, win=None):
        self.max_effective_context_window = win

    def chat(self, msgs, **kw):
        return SimpleNamespace(content="测试会话", usage=None, reasoning="", tool_calls=[])


def patched_env(tmp: Path):
    """把全局落盘位置指到临时目录（TOKEN_USAGE_FILE），并把 panic 阈值固定（0=跟随 win）。"""
    session_mod.TOKEN_USAGE_FILE = tmp / "token_usage.jsonl"
    orig_panic = config.load_panic_window
    config.load_panic_window = lambda: 0
    return orig_panic


def mk_session(win) -> Session:
    return Session(system="测试人设", llm=FakeLLM(win), workspace=tempfile.gettempdir())


def main():
    tmp = Path(tempfile.mkdtemp(prefix="agt_token_test_"))
    orig_panic = patched_env(tmp)
    try:
        # ========== ① 校准：chars/token 比率收敛，估算口径跟着变 ==========
        s = mk_session(win=100_000)
        msgs = [{"role": "user", "content": "中" * 6000}]
        before = s._estimate_tokens(msgs)          # 初值比率 4.0 → 1500
        s.observe_llm_usage(msgs, {"prompt_tokens": 3000, "completion_tokens": 500,
                                   "total_tokens": 3500})
        # ratio = 6000/3000 = 2.0；EMA(0.5)：4.0 → 3.0
        check("① 实测比率 EMA 收敛（4.0 → 3.0）", abs(s._chars_per_token - 3.0) < 1e-6,
              f"实际 {s._chars_per_token}")
        check("① _estimate_tokens 改用校准比率（1500 → 2000）",
              before == 1500 and s._estimate_tokens(msgs) == 2000,
              f"before={before} after={s._estimate_tokens(msgs)}")
        rec = json.loads(session_mod.TOKEN_USAGE_FILE.read_text(encoding="utf-8").splitlines()[-1])
        check("① 落盘记录字段完整", rec["model"] == "fake-model" and rec["prompt_tokens"] == 3000
              and rec["chars_per_token"] == 3.0 and rec["over"] is False and rec["panic"] is False,
              f"rec={rec}")

        # ========== ② 超窗标记：win < total ≤ panic ==========
        config.load_panic_window = lambda: 2000   # panic=2000 > total=1500 > win=1000
        s2 = mk_session(win=1000)
        s2.observe_llm_usage([{"role": "user", "content": "x" * 100}],
                             {"prompt_tokens": 100, "total_tokens": 1500})
        check("② 超窗未超 panic → 置标记", s2._over_window_mark is True)
        rec2 = json.loads(session_mod.TOKEN_USAGE_FILE.read_text(encoding="utf-8").splitlines()[-1])
        check("② jsonl 记 over=True/panic=False", rec2["over"] is True and rec2["panic"] is False,
              f"rec={rec2}")
        s2.start_turn("下一轮")
        check("② start_turn 消费标记复位", s2._over_window_mark is False)

        # ========== ③ panic 立即压缩：轮内膨胀后 observe 超 panic → 立即 _plan_fold ==========
        config.load_panic_window = lambda: 0      # panic = win = 4000
        s3 = mk_session(win=4000)
        for i in range(4):                        # 4 轮 × 800 字历史：轮边界计划判定不超（est≈800 < 3000）
            s3.turns.append(Turn(user_message=f"第{i}轮历史 " + "史" * 800, answer="答"))
        s3.start_turn("当前轮")
        check("③ 轮边界计划未折叠（起点 planned_fold=0）", s3._planned_fold == 0,
              f"planned_fold={s3._planned_fold}")
        # 轮内 steps 膨胀：2 步 × 20000 字工具结果（当前轮全量披露，进 cur_est）。
        # 注意不先 messages_for_llm()——build 会触发估算保命阀先折一步，干扰 observe 归因
        for k in range(2):
            st = Step(reasoning="思考")
            cid = s3.toollog.next_id()
            s3.toollog.record(cid, "read_file", {"path": f"f{k}.py"}, "结果" * 10000)
            st.tool_calls.append(ToolCall(call_id=cid))
            s3.add_step(st)
        s3.observe_llm_usage(s3._seg_msgs_user_message() + s3._seg_msgs_steps(),
                             {"prompt_tokens": 9000, "total_tokens": 9500})
        check("③ 超 panic → 立即折叠（planned_fold 0 → >0）", s3._planned_fold > 0,
              f"planned_fold={s3._planned_fold}")
        built = s3.messages_for_llm()
        check("③ 投影含折叠摘要（4 轮历史已被压掉）",
              any("已折叠的早期轮次" in (m.get("content") or "") for m in built))
        folded_hist = session_mod.Session._count_chars(s3._render_tiered_history(s3._planned_fold))
        full_hist = session_mod.Session._count_chars(s3._render_tiered_history(0))
        check("③ 折叠后历史段远小于未折叠", folded_hist < full_hist / 2,
              f"折叠={folded_hist} 未折叠={full_hist}")
        rec3 = json.loads(session_mod.TOKEN_USAGE_FILE.read_text(encoding="utf-8").splitlines()[-1])
        check("③ jsonl 记 panic=True", rec3["panic"] is True, f"rec={rec3}")

        # ========== ④ 落盘/回读：新 Session 读到同模型比率初值 ==========
        with open(session_mod.TOKEN_USAGE_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": 1, "model": "fake-model", "chars": 5000,
                                "prompt_tokens": 2000, "total_tokens": 2500,
                                "chars_per_token": 2.5}) + "\n")
        s4 = mk_session(win=100_000)
        check("④ init 回读同模型比率初值（2.5）", abs(s4._chars_per_token - 2.5) < 1e-6,
              f"实际 {s4._chars_per_token}")
        with open(session_mod.TOKEN_USAGE_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": 2, "model": "别的模型", "chars_per_token": 6.0}) + "\n")
        s5 = mk_session(win=100_000)
        check("④ 不同模型记录不串扰（仍 2.5）", abs(s5._chars_per_token - 2.5) < 1e-6,
              f"实际 {s5._chars_per_token}")

        # ========== ⑤ 回归：win 未配置（窗口模式）observe 完全 no-op ==========
        s6 = mk_session(win=None)
        ratio_before, lines_before = (s6._chars_per_token,
                                      len(session_mod.TOKEN_USAGE_FILE.read_text(
                                          encoding="utf-8").splitlines()))
        s6.observe_llm_usage([{"role": "user", "content": "x" * 100}],
                             {"prompt_tokens": 100, "total_tokens": 999999})
        lines_after = len(session_mod.TOKEN_USAGE_FILE.read_text(encoding="utf-8").splitlines())
        check("⑤ 窗口模式 no-op（比率/标记/落盘都不动）",
              s6._chars_per_token == ratio_before and s6._over_window_mark is False
              and lines_after == lines_before,
              f"ratio {ratio_before}→{s6._chars_per_token} lines {lines_before}→{lines_after}")
    finally:
        config.load_panic_window = orig_panic
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n{'='*40}\n{'全部通过 ✅' if FAIL == 0 else '有失败 ❌'}：PASS={PASS} FAIL={FAIL}")
    sys.exit(0 if FAIL == 0 else 1)


if __name__ == "__main__":
    main()
