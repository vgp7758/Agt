# -*- coding: utf-8 -*-
"""计划视图读侧校验（2026-11-28 用户问诊「worker 更新进度后 main 看到的还是内存里自己那份」）。

计划文件是唯一事实源；agent.active_plan 只是缓存。worker 经 update_plan 写盘后，main 的
SYSTEM 进度块 / 计划面板必须跟上——view_active_plan 按文件 mtime/size 校验后替换内存视图。

覆盖：① worker 标进度 → main 的 _format_plan_block / plan_steps 文本与 agent.plan 镜像实时
跟上且只 emit 一次；② 文件没动零重读（同 stamp 直接返回）；③ 别的 Agent 只改 updated_at
（内容签名不变）不推 UI；④ 文件被删/损坏保留现有视图；⑤ 无活动计划返回 None；
⑥ 缓存说明：plan_steps 位于 history 之后（main.yml 装配序），进度变化不断 system/history 前缀。"""
import json
import os
from types import SimpleNamespace

import agent_config as ac
import plan_tools as pt


class _FA:
    pass


def _mk(ws, aid):
    a = _FA()
    a.agent_id = aid
    a.session = SimpleNamespace(workspace=ws)
    a.active_plan = None
    a.active_plan_id = None
    a.plan = []
    a.events = []
    a.on_event = a.events.append
    return a


def _funcs(a):
    """{工具名: 可调用}——直接取 Tool.func（绕过 Toolbox 的 dict 入参）。"""
    return {t.name: t.func for t in pt.make_plan_tools(a)}


def _setup(tmp_path):
    main = _mk(tmp_path, "_main_")
    worker = _mk(tmp_path, "worker-p_x")
    out = _funcs(main)["create_plan"]("标题", ["第一步", "第二步"])
    pid = out.split("已创建计划 ")[1].split("（")[0]
    assert "已加入计划" in _funcs(worker)["join_plan"](pid)
    return main, worker, pid


def test_worker_progress_is_visible_to_main_without_touching_plan_tools(tmp_path):
    main, worker, pid = _setup(tmp_path)
    upd = _funcs(worker)["update_plan"]
    assert "✅ 1." in upd(1, "completed")

    # main 没调任何计划工具：SYSTEM 块与装配函数读到的都应是新进度
    block = pt._format_plan_block(main)
    assert "已完成 1" in block and "✅ 1. 第一步" in block and "☐ 2. 第二步" in block
    ac._RUNTIME_AGENT = main
    ac._CURRENT_AGENT.set(None)
    try:
        steps = ac._func_plan_steps()
        assert "已完成 1" in steps and "✅" in steps
        assert [s["status"] for s in main.plan] == ["completed", "pending"]      # 旧读者镜像同步
    finally:
        ac._RUNTIME_AGENT = None
    # 内容真变了才推 UI：恰好 1 次 plan 事件，且是新进度
    plan_ev = [e for e in main.events if e.get("type") == "plan"]
    assert len(plan_ev) == 2            # create_plan 自己 1 次 + 视图刷新 1 次
    assert [s["status"] for s in plan_ev[-1]["plan"]] == ["completed", "pending"]


def test_unchanged_file_is_not_reread(tmp_path):
    main, worker, pid = _setup(tmp_path)
    pt.view_active_plan(main)                       # 首次：建立 stamp
    p0 = main.active_plan
    calls = []
    orig = pt._load_plan
    pt._load_plan = lambda ws, p: (calls.append(p), orig(ws, p))[1]
    try:
        for _ in range(5):
            assert pt.view_active_plan(main) is p0
    finally:
        pt._load_plan = orig
    assert calls == []                               # 文件没动：零重读


def test_touch_without_content_change_does_not_emit(tmp_path):
    main, worker, pid = _setup(tmp_path)
    pt.view_active_plan(main)
    n0 = len([e for e in main.events if e.get("type") == "plan"])
    f = pt._plan_path(tmp_path, pid)
    d = json.loads(f.read_text(encoding="utf-8"))
    d["design"] = d.get("design", "") + " "          # design 不在面板签名里
    pt._save_plan(tmp_path, d)
    os.utime(f, ns=(f.stat().st_atime_ns, f.stat().st_mtime_ns + 1_000_000))   # 保证 stamp 变化
    pt.view_active_plan(main)
    assert len([e for e in main.events if e.get("type") == "plan"]) == n0


def test_deleted_or_corrupt_file_keeps_view(tmp_path):
    main, worker, pid = _setup(tmp_path)
    pt.view_active_plan(main)
    p0 = main.active_plan
    f = pt._plan_path(tmp_path, pid)
    f.write_text("{not json", encoding="utf-8")
    assert pt.view_active_plan(main) is p0
    f.unlink()
    assert pt.view_active_plan(main) is p0


def test_no_active_plan_returns_none(tmp_path):
    a = _mk(tmp_path, "_main_")
    assert pt.view_active_plan(a) is None
