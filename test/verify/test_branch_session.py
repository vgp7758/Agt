# -*- coding: utf-8 -*-
"""test_branch_session.py —— session 分支机制（用户提案 2026-10-08）全生命周期验证。

五场景：
① /branch 创建：目录+meta 字段（branch_of/inherit_lines/base_hash）正确
② load 分支：基底+分支 turns 数正确；recall 能命中基底内容；toollog counter 续号
③ 写隔离：分支新增轮只进分支 events.jsonl，主线文件不变
④ 主线缺失降级：删主线 events → 纯分支流可加载
⑤ list_sessions：分支以 '主线名 ⇢ 分支名' 列出（branch 标记）

隔离：临时 workspace + 临时 repo 目录（~/.agt/repos/<tmp-key>），跑完清理。
"""
import io
import json
import shutil
import sys
import tempfile
import time
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "src"))

import session as S
from session import Session, _events_fingerprint, _read_events, list_sessions
from session import REPOS_DIR, _repo_key
from commands import _cmd_branch

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'✅' if cond else '❌'} {name}" + (f"  [{detail}]" if detail and not cond else ""))


class _FakeResp:
    def __init__(self, text):
        self.content = text


class _FakeLLM:
    """测试桩：chat 不联网，返回固定文本（summary/_ensure_name 等短调用全走它）。"""
    def chat(self, msgs, **kw):
        return _FakeResp("（测试摘要）")


def _write_lines(path: Path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def main():
    tmpws = Path(tempfile.mkdtemp(prefix="agt_branch_test_"))
    repo_key = None
    try:
        # —— 造主线存档：3 轮（第2轮带工具调用 c1/c2，便于验证 toollog 合载）——
        ts_dir = S._repo_sessions_dir(tmpws) / "20260101_120000"
        repo_key = _repo_key(tmpws)
        ts_dir.mkdir(parents=True, exist_ok=True)
        events = [
            {"event": "turn_start", "user": "主线第1轮：了解Agt框架架构", "images": []},
            {"event": "turn_end", "answer": "Agt框架是分层上下文引擎", "answer_reasoning": "", "summary": "架构介绍"},
            {"event": "turn_start", "user": "主线第2轮：读session.py源码", "images": []},
            {"event": "step", "reasoning": "先读文件", "call_ids": ["c1", "c2"], "changes": []},
            {"event": "turn_end", "answer": "Session.load 在 session.py:3565", "answer_reasoning": "", "summary": "源码阅读"},
            {"event": "turn_start", "user": "主线第3轮：加了个功能", "images": []},
            {"event": "turn_end", "answer": "功能完成", "answer_reasoning": "", "summary": "功能开发"},
        ]
        _write_lines(ts_dir / "events.jsonl", events)
        _write_lines(ts_dir / "toollog.jsonl", [
            {"call_id": "c1", "name": "read_file", "arguments": {"path": "src/session.py"}, "result": "def load...", "step": 1, "turn": 2},
            {"call_id": "c2", "name": "grep", "arguments": {"pattern": "def load"}, "result": "L3565", "step": 1, "turn": 2},
        ])
        _write_lines(ts_dir / "llm_calls.jsonl", [
            {"ts": time.time(), "model": "fake", "outcome": "success", "scene": "react", "usage": {"prompt_tokens": 100}},
        ])
        (ts_dir / "meta.json").write_text(json.dumps({
            "name": "主线测试", "created_at": time.mktime(time.strptime("20260101_120000", "%Y%m%d_%H%M%S")),
            "system": "你是测试Agent", "recent_window_turns": 4, "max_steps_per_turn": 80,
        }, ensure_ascii=False), encoding="utf-8")

        # 载入主线（重放验证 + 作为 /branch 的 ctx.session）
        main_sess = Session.load("主线测试", llm=_FakeLLM(), workspace=tmpws)
        check("主线重放 3 轮", len(main_sess.turns) == 3, f"实际 {len(main_sess.turns)}")

        set_session_calls = []
        ctx = SimpleNamespace(
            agent=SimpleNamespace(llm=_FakeLLM(),
                                  set_session=lambda s: set_session_calls.append(s)),
            session=main_sess,
        )

        # ===== 场景① /branch 创建（只带前 2 轮） =====
        print("\n[场景①] /branch 测试分支 2 —— 创建与 meta 字段")
        buf = io.StringIO()
        with redirect_stdout(buf):
            _cmd_branch(ctx, ["测试分支", "2"])
        out = buf.getvalue()
        bdir = ts_dir / "branches" / "测试分支"
        check("分支目录已创建", bdir.exists() and (bdir / "meta.json").exists())
        bm = json.loads((bdir / "meta.json").read_text(encoding="utf-8"))
        check("meta.branch 三字段齐", set(bm.get("branch", {}).keys()) >= {"branch_of", "inherit_lines", "base_hash"})
        check("branch_of=主线目录名", bm["branch"]["branch_of"] == "20260101_120000", str(bm.get("branch")))
        n_expect = 5  # 前2轮 = 前5行（2+3行）
        check("inherit_lines=前2轮行数", bm["branch"]["inherit_lines"] == n_expect, str(bm["branch"].get("inherit_lines")))
        check("base_hash 与指纹函数一致", bm["branch"]["base_hash"] == _events_fingerprint(events[:n_expect]))
        check("基础字段拷贝自主线(system)", bm.get("system") == "你是测试Agent")
        check("已切到分支(set_session)", len(set_session_calls) == 1 and "已切到分支" in out)

        # ===== 场景② load 分支：基底+分支 turns / recall / counter =====
        print("\n[场景②] load 分支 —— 合成重放")
        bs = set_session_calls[0]
        check("分支 turns=基底2轮", len(bs.turns) == 2, f"实际 {len(bs.turns)}")
        check("基底内容可 recall", "Session.load" in bs.recall("session.py"), "recall 未命中基底第2轮")
        check("基底 toollog 合载(c1)", bs.toollog.get("c1") is not None and "def load" in bs.toollog.get("c1")["result"])
        check("counter 续号(c3 起)", bs.toollog.next_id() == "c3", bs.toollog.next_id())
        check("branch_meta 已挂载", bs.branch_meta and bs.branch_meta.get("branch_of") == "20260101_120000")

        # ===== 场景③ 写隔离：分支新增轮 =====
        print("\n[场景③] 分支写隔离")
        main_lines_before = len((ts_dir / "events.jsonl").read_text(encoding="utf-8").splitlines())
        bs.toollog.record("c3", "write_file", {"path": "简历.md"}, "已写入", step=1, turn=3)
        bs.start_turn("支线任务：帮我写简历")
        bs.finish_turn("简历初稿完成")
        time.sleep(0.3)             # 等 daemon _autosave 写完（避免与下一步读文件/rename 竞态）
        bs.save()   # 同步落盘双保险（meta 的 branch 字段等此刻写回）
        check("分支内存 turns=3(基底2+新1)", len(bs.turns) == 3, f"实际 {len(bs.turns)}")
        b_lines = (bdir / "events.jsonl").read_text(encoding="utf-8").splitlines()
        check("分支 events 已落盘(+2行 turn)", len(b_lines) == 2, f"实际 {len(b_lines)} 行")
        main_lines_after = len((ts_dir / "events.jsonl").read_text(encoding="utf-8").splitlines())
        check("主线 events 未变", main_lines_after == main_lines_before, f"{main_lines_before}→{main_lines_after}")
        bmeta_saved = json.loads((bdir / "meta.json").read_text(encoding="utf-8"))
        check("分支 save 写回 branch 字段", bmeta_saved.get("branch", {}).get("branch_of") == "20260101_120000")

        # ===== 场景②补：重新 load（纯分支名 / 主线名/分支名 两种形式） =====
        print("\n[场景②补] 重新 load —— 两种定位形式")
        bs2 = Session.load("测试分支", llm=_FakeLLM(), workspace=tmpws)
        check("纯分支名 load：3 轮", len(bs2.turns) == 3, f"实际 {len(bs2.turns)}")
        check("纯分支名 load：新轮在", any("写简历" in (t.user_message or "") for t in bs2.turns))
        bs3 = Session.load("主线测试/测试分支", llm=_FakeLLM(), workspace=tmpws)
        check("'主线/分支' load：3 轮", len(bs3.turns) == 3, f"实际 {len(bs3.turns)}")
        check("重新 load 后 counter 续号(c4 起)", bs3.toollog.next_id() == "c4", bs3.toollog.next_id())

        # ===== 场景④ 主线缺失降级 =====
        print("\n[场景④] 主线缺失降级")
        tmp_main = ts_dir.with_name(ts_dir.name + "_hidden")
        ts_dir.rename(tmp_main)
        bs4 = Session.load(str(tmp_main / "branches" / "测试分支" / "meta.json"),
                           llm=_FakeLLM(), workspace=tmpws)
        check("降级纯分支流：1 轮", len(bs4.turns) == 1, f"实际 {len(bs4.turns)}")
        check("降级仍可读分支新轮", any("写简历" in (t.user_message or "") for t in bs4.turns))
        tmp_main.rename(ts_dir)

        # ===== 场景⑤ list_sessions =====
        print("\n[场景⑤] list_sessions 显示分支")
        items = list_sessions(tmpws)
        brs = [it for it in items if it.get("branch")]
        check("分支已列出", len(brs) == 1)
        if brs:
            it = brs[0]
            check("显示名 '主线 ⇢ 分支'", it["name"] == "主线测试 ⇢ 测试分支", it["name"])
            check("id 复合可定位", it["id"] == "20260101_120000/测试分支", it["id"])
            check("turns=分支自身1轮", it["turns"] == 1, str(it.get("turns")))

        # ===== 嵌套分支拦截 =====
        print("\n[附加] 分支上再分叉拦截")
        ctx2 = SimpleNamespace(agent=ctx.agent, session=bs2)
        buf = io.StringIO()
        with redirect_stdout(buf):
            _cmd_branch(ctx2, ["嵌套", "1"])
        check("嵌套分支被拦截", "不支持嵌套" in buf.getvalue() and not (bdir / "branches").exists())

    finally:
        shutil.rmtree(tmpws, ignore_errors=True)
        if repo_key:
            shutil.rmtree(REPOS_DIR / repo_key, ignore_errors=True)

    print("\n" + "=" * 48)
    print(f"结果：{len(PASS)} 通过 / {len(FAIL)} 失败")
    if FAIL:
        for f in FAIL:
            print(f"  ❌ {f}")
        sys.exit(1)
    print("🎉 全部通过")


if __name__ == "__main__":
    main()
