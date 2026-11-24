"""test_toollog_shared.py —— ToolLog 进程级共享（内存治理 2026-11-24）回归。

覆盖：多 session 共享一份数据 / counter 按前缀分区 / 并发 record 不交错 /
search 与 list_tool_logs 过滤不变 / 旧 cN 与分支锚点 id 兼容 / 同 store 只整表 load 一次 /
Session 三条加载路径（主线 buffer→绑定、分支 bind、子 Agent 预设目录直绑）。

运行：python -m pytest test/test_toollog_shared.py -q
"""
import json
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

import toollog as tl_mod
from toollog import ToolLog, make_tool_log_tools, store_for, _split_call_id


@pytest.fixture(autouse=True)
def _fresh_stores():
    """每个用例独占进程级 store 表（_STORES 是模块全局，防跨用例串数据）。"""
    tl_mod._STORES.clear()
    yield
    tl_mod._STORES.clear()


def _lines(p: Path) -> list:
    return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()]


def _tools_for(toollog):
    agent = SimpleNamespace(session=SimpleNamespace(toollog=toollog))
    return {t.name: t.func for t in make_tool_log_tools(agent)}


# ========== 1. 多 session 共享一份数据 / 落盘路径 ==========

def test_shared_data_and_path(tmp_path):
    root = tmp_path / "sessions"
    a = ToolLog.shared(root, "a")
    b = ToolLog.shared(root, "b")
    assert a._data is b._data                       # 同一份 dict（N 份全量副本 → 1 份）
    p = root / "toollog.jsonl"
    a.set_path(p)
    b.set_path(p)
    assert a._path == b._path == p                  # server.py dashboard 读 _path：同一落盘路径
    a.record("a-1", "grep", {"q": "x"}, "结果A")
    assert b.get("a-1")["result"] == "结果A"         # 跨实例可见
    assert len(_lines(p)) == 1                      # 只 append 一行
    assert store_for(root) is store_for(root / ".." / "sessions")   # 键按 resolve 归一
    assert store_for(root) is not store_for(tmp_path / "other")     # 不同 root 各自一份


# ========== 2. counter 按前缀分区 ==========

def test_counter_partitioned_by_prefix(tmp_path):
    root = tmp_path / "sessions"
    a1 = ToolLog.shared(root, "a")
    b = ToolLog.shared(root, "b")
    a2 = ToolLog.shared(root, "a")                  # 同前缀第二实例（重 load / 切回）
    assert [a1.next_id(), a1.next_id()] == ["a-1", "a-2"]
    assert b.next_id() == "b-1"                     # 别的前缀不顶号
    assert a2.next_id() == "a-3"                    # 同前缀连续编号
    c = ToolLog.shared(root, "")                    # 无前缀：退 cN 兼容
    assert c.next_id() == "c1"
    assert store_for(root).counters == {"a": 3, "b": 1, "": 1}
    # 未绑定态走实例私有 counter；绑定后并入 store（buffer 记录自身前缀）
    buf = ToolLog("z")
    assert buf.next_id() == "z-1"
    buf.record("z-1", "t", {}, "r")
    buf.set_path(root / "toollog.jsonl")
    assert store_for(root).counters["z"] == 1
    assert ToolLog.shared(root, "z").next_id() == "z-2"
    assert buf.get("z-1") is not None               # buffer 并入后仍可查


# ========== 3. 并发 record 不交错 ==========

def test_concurrent_record_no_interleave(tmp_path):
    p = tmp_path / "sessions" / "toollog.jsonl"
    logs = [ToolLog.shared(p.parent, f"s{i}") for i in range(8)]
    for lg in logs:
        lg.set_path(p)
    N = 50

    def work(lg):
        for j in range(N):
            lg.record(lg.next_id(), "tool", {"j": j}, "x" * 300)

    ths = [threading.Thread(target=work, args=(lg,)) for lg in logs]
    for t in ths:
        t.start()
    for t in ths:
        t.join()
    rows = _lines(p)                                # 交错 = 半行拼接 → json 解析失败
    assert len(rows) == 8 * N
    assert len({r["call_id"] for r in rows}) == 8 * N
    assert len(logs[0]) == 8 * N                    # 共享数据齐全（任一实例视角）
    # 同前缀并发 next_id：锁内自增 → id 不重复
    same = [ToolLog.shared(p.parent, "same") for _ in range(4)]
    got = [[] for _ in same]

    def gen(k):
        for _ in range(200):
            got[k].append(same[k].next_id())

    ths = [threading.Thread(target=gen, args=(k,)) for k in range(4)]
    for t in ths:
        t.start()
    for t in ths:
        t.join()
    ids = [x for g in got for x in g]
    assert len(ids) == 800 and len(set(ids)) == 800
    assert max(int(x.split("-")[1]) for x in ids) == 800


# ========== 4. search / list_tool_logs / get_tool_detail 过滤不变 ==========

def test_search_and_list_filter(tmp_path):
    root = tmp_path / "sessions"
    p = root / "toollog.jsonl"
    a = ToolLog.shared(root, "a")
    a.set_path(p)
    b = ToolLog.shared(root, "b")
    b.set_path(p)
    a.record("a-1", "grep", {"pattern": "alpha"}, "命中 alpha 一次")
    a.record("a-2", "read_file", {"path": "x.py"}, "内容")
    b.record("b-1", "grep", {"pattern": "beta"}, "命中 beta 一次")
    # search：共享可见范围 = 整表（与旧「每实例整表 load」一致，跨 session 可搜到）
    assert [h[0]["call_id"] for h in a.search(["alpha"])] == ["a-1"]
    assert [h[0]["call_id"] for h in b.search(["alpha"])] == ["a-1"]
    assert {h[0]["call_id"] for h in a.search(["grep"])} == {"a-1", "b-1"}
    assert a.search([]) == []
    # list_tool_logs：只列本前缀
    ta = _tools_for(a)
    out_a = ta["list_tool_logs"]()
    assert "共 2 条" in out_a and "a-1" in out_a and "a-2" in out_a and "b-1" not in out_a
    out_b = _tools_for(b)["list_tool_logs"]()
    assert "共 1 条" in out_b and "b-1" in out_b and "a-1" not in out_b
    # get_tool_detail：按 id 直查（跨前缀 id 也可查——与旧行为一致）；缺失给占位
    d = ta["get_tool_detail"]("a-1,b-1,nope-9")
    assert "[完整详情·a-1]" in d and "[完整详情·b-1]" in d and "[无此 id] nope-9" in d
    assert a.view("nope-9") == ("(详情已失效)", {}, "")


# ========== 5. 旧 cN / 分支锚点 id 兼容 + 同 store 只整表 load 一次 ==========

def test_legacy_ids_and_load_once(tmp_path, monkeypatch):
    root = tmp_path / "sessions"
    p = root / "toollog.jsonl"
    root.mkdir(parents=True)
    p.write_text("\n".join(json.dumps(e, ensure_ascii=False) for e in [
        {"call_id": "c1", "name": "read_file", "arguments": {}, "result": "旧主线1"},
        {"call_id": "c2", "name": "grep", "arguments": {}, "result": "旧主线2"},
        {"call_id": "a-1", "name": "edit", "arguments": {}, "result": "a 的第 1 次"},
        {"call_id": "m1000-c1", "name": "grep", "arguments": {}, "result": "分支锚点时代"},
        {"call_id": "bad", "name": "x", "arguments": {}, "result": "无序号 id"},
    ]) + "\n", encoding="utf-8")
    calls = {"n": 0}
    orig = json.loads

    def counting(s, *args, **kw):
        calls["n"] += 1
        return orig(s, *args, **kw)

    monkeypatch.setattr(tl_mod.json, "loads", counting)
    legacy = ToolLog(prefix="")                      # 旧无前缀会话 load
    legacy.load_from_jsonl(p)
    legacy.set_path(p)
    assert calls["n"] == 5
    assert legacy.get("c1")["result"] == "旧主线1"
    assert legacy.get("m1000-c1")["result"] == "分支锚点时代"   # 锚点 id 仍可召回
    assert legacy.get("bad") is not None
    assert legacy.next_id() == "c3"                  # cN 续号
    a = ToolLog.shared(root, "a")                    # 同 store 第二实例：不重读，counter 已恢复
    a.load_from_jsonl(p)
    a.set_path(p)
    assert calls["n"] == 5                           # 仍只有首次 load 的 5 次 loads
    assert a.next_id() == "a-2"
    m = ToolLog.shared(root, "m1000")                # 锚点前缀：m1000-c1 尾段非数字 → 不顶号
    assert m.next_id() == "m1000-1"
    assert store_for(root).loaded is True
    assert store_for(root).counters == {"": 3, "a": 2, "m1000": 1}
    assert len(_lines(p)) == 5                       # 文件存在 → set_path 不重写


def test_split_call_id_forms():
    assert _split_call_id("a-1024") == ("a", 1024)
    assert _split_call_id("a-coder-3") == ("a-coder", 3)
    assert _split_call_id("c7") == ("", 7)
    assert _split_call_id("m1000-c1") == ("", -1)
    assert _split_call_id("bad") == ("", -1)
    assert _split_call_id("") == ("", -1)
    assert _split_call_id("-5") == ("", -1)
    assert _split_call_id("a-") == ("", -1)


# ========== 6. 旧格式 load_list 迁移：buffer → set_path 并入 + 建立 jsonl ==========

def test_load_list_then_set_path(tmp_path):
    root = tmp_path / "sessions"
    p = root / "toollog.jsonl"
    tl = ToolLog(prefix="")
    tl.load_list([{"call_id": "c1", "name": "read_file", "arguments": {"path": "a"}, "result": "内容"},
                  {"call_id": "c2", "name": "grep", "arguments": {}, "result": "L1"}])
    assert tl.next_id() == "c3"                      # buffer 态 counter 已恢复
    tl.record("c3", "edit", {}, "ok")
    tl.set_path(p)                                   # 文件不存在 → flush 全量建立
    rows = _lines(p)
    assert [r["call_id"] for r in rows] == ["c1", "c2", "c3"]
    assert store_for(root).counters[""] == 3
    # 文件已由同 store 其它实例建立：buffer 记录补 append（旧实现会滞留内存不落盘）
    late = ToolLog(prefix="q")
    late.record("q-1", "t", {}, "r")
    late.set_path(p)
    assert [r["call_id"] for r in _lines(p)][-1] == "q-1"


# ========== 7. Session 三条加载路径 ==========

class _FakeResp:
    def __init__(self, text):
        self.content = text


class _FakeLLM:
    def chat(self, msgs, **kw):
        return _FakeResp("（测试）")


def test_session_paths(tmp_path, monkeypatch):
    import session as S
    monkeypatch.setattr(S, "REPOS_DIR", tmp_path / "repos")
    root = tmp_path / "sessions"
    # ① 子 Agent：预设 session_dir → __init__ 即绑共享 store + 前缀 <主线>-<aid>
    sub = S.Session(system="t", llm=_FakeLLM(), workspace=tmp_path,
                    session_dir=root / "main" / "agents" / "coder")
    assert sub.toollog._store is store_for(root)
    assert sub.toollog._prefix == "main-coder"
    peer = ToolLog.shared(root, "main")
    peer.set_path(root / "toollog.jsonl")
    sub.toollog.record(sub.toollog.next_id(), "grep", {}, "子 Agent 结果")
    assert peer.get("main-coder-1") is not None        # 主线实例视角可见
    # ② 主线：目录未定 → buffer 态；name 就绪 _bind_persistence_paths 绑定 REPOS_DIR 下的共享 store
    main = S.Session(system="t", llm=_FakeLLM(), workspace=tmp_path)
    assert main.toollog._store is None
    main.toollog.record("pre-1", "t", {}, "name 就绪前的记录")
    assert main.toollog.view("pre-1")[2] == "name 就绪前的记录"
    main.name = "主线测试"
    main._bind_persistence_paths()
    sroot = S._sessions_root_of(main.session_dir)
    assert main.toollog._store is store_for(sroot)
    assert main.toollog._prefix == main.session_dir.name
    assert main.toollog.get("pre-1") is not None       # buffer 并入共享 store
    assert (sroot / "toollog.jsonl").exists()
    assert [r["call_id"] for r in _lines(sroot / "toollog.jsonl")] == ["pre-1"]
    # ③ 分支：load 时 bind（不再换实例）——用 monkeypatch 模拟 load 内的分支段
    br = S.Session(system="t", llm=_FakeLLM(), workspace=tmp_path,
                   session_dir=sroot / "br1")
    br.branch_meta = {"branch_of": main.session_dir.name}
    br.toollog.bind(S._sessions_root_of(br.session_dir), S._call_prefix_of(br.session_dir))
    assert br.toollog._store is store_for(sroot)
    assert br.toollog._prefix == "br1"
    assert br.toollog.get("pre-1") is not None         # 共享数据对分支可见
    assert br.toollog.next_id() == "br1-1"
    # 重 load 同一会话：counter 续号（store 级，不因新实例归零）
    again = S.Session(system="t", llm=_FakeLLM(), workspace=tmp_path,
                      session_dir=sroot / "br1")
    assert again.toollog.next_id() == "br1-2"
