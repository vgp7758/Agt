"""doctor 诊断工具族单测（2026-10-16 用户裁定）。

覆盖（全部 mock——FakeAgent 带 llm stub，除存档点外不打真 API）：
- req_inspect 报告形态：逐条 role / content 形态(str|list) / 长度 / image_url 头60字 /
  tool_calls 形态 / 可疑标记（空content数组、超长>5万、图但模型非视觉、畸形tool_calls）；
- req_probe：patch 应用在深拷贝上（存档本体不动）、max_tokens=16 最小探测、
  每 agent 计数、上限 5 次拒绝并提示"判定非数据问题"；
- req_patch 强制验证门：验证 probe 未通过拒绝落盘（存档/会话都不动）；通过才
  in-place 修存档 + 同步修 session 源头（toollog <img> 剔除）+ _autosave + 预算清零；
- req_resume：清 _provider_cooldown + resume_interrupted 状态透传（""/__queued__/错误）；
- _doctor_apply_patch：drop+truncate+strip_images+replace_text 消息级语义（drop 后索引用
  patch 前编号）；patch 入参 JSON 字符串归一化；
- 存档点：llm_client 回退链耗尽 raise 前冻结 last_failed_messages（真实 LLMClient +
  patched _chat_inner，深拷贝隔离原列表）。
"""
import sys
import copy as _copy
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import pytest

import multiagent as ma
from toollog import ToolLog


# ── 测试基建（FakeAgent + llm stub，同 test_403_fallback 的 patch 手法用于存档点） ──

BAD_IMG = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgbrokenpayload"


class FakeLLM:
    """llm stub：可配置失败条件（fail_while(msgs) 返回错误文本=失败）。记录每次探测。"""

    def __init__(self, archive=None, fail_while=None, vision=True, model="glm-flash"):
        self.model_name = model
        self.vision_supported = vision
        self._provider_cooldown = {"m1": 100.0, "m2": 200.0}
        self.last_failed_messages = archive
        self.fail_while = fail_while
        self.probe_calls = []   # [(深拷贝 messages, overrides)]

    def _chat_inner(self, messages, **overrides):
        self.probe_calls.append((_copy.deepcopy(messages), dict(overrides)))
        err = self.fail_while(messages) if self.fail_while else None
        if err:
            raise RuntimeError(f"BadRequestError: 400 {err}")
        return "ok"


class FakeStep:
    def __init__(self, call_ids):
        class _TC:
            def __init__(self, cid):
                self.call_id = cid
        self.tool_calls = [_TC(c) for c in call_ids]


class FakeTurn:
    def __init__(self):
        self.steps = []


class FakeSession:
    def __init__(self):
        self._current = FakeTurn()
        self.toollog = ToolLog()
        self.autosaves = 0
        self.invalidations = []

    def _autosave(self):
        self.autosaves += 1

    def invalidate_projection(self, why=""):
        self.invalidations.append(why)


class FakeRegistry:
    class _E:
        def __init__(self, a):
            self.agent = a

    def __init__(self, agents=None):
        self._agents = dict(agents or {})

    def lookup(self, aid):
        a = self._agents.get(aid)
        return FakeRegistry._E(a) if a is not None else None


class FakeAgent:
    def __init__(self, aid="_main_", llm=None, session=None, registry=None, resume_ret=""):
        self.agent_id = aid
        self.llm = llm
        self.session = session
        self.registry = registry
        self.resume_calls = 0
        self.resume_ret = resume_ret

    def resume_interrupted(self):
        self.resume_calls += 1
        return self.resume_ret


def _fail_on_image(msgs):
    """失败条件：任一消息含 image_url 部件 → 400 坏图（模拟真实 BadRequest）。"""
    for m in msgs:
        if ma._doctor_image_parts(m):
            return "Unable to decode the image"
    return None


def _archive():
    """典型失败请求存档：system + user + assistant(tool_calls) + tool(文字+坏图)。"""
    return [
        {"role": "system", "content": "你是施工助手"},
        {"role": "user", "content": "帮我修一下"},
        {"role": "assistant", "content": None,
         "tool_calls": [{"id": "a-1", "type": "function",
                         "function": {"name": "browser_snapshot", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "a-1",
         "content": [{"type": "text", "text": "页面快照如下"},
                     {"type": "image_url", "image_url": {"url": BAD_IMG}}]},
    ]


def _doctor():
    """(四件套函数 dict, 主 agent)。"""
    main = FakeAgent()
    tools = ma.make_doctor_tools(main)
    return {t.name: t.func for t in tools}, main


# ── 1. req_inspect 报告形态 ──

def test_inspect_report_shape():
    fns, main = _doctor()
    llm = FakeLLM(archive=_archive(), vision=False)
    main.llm = llm
    out = fns["req_inspect"]("")   # 空 id = 自己
    assert out.startswith("📋 '_main_' 失败请求体检：4 条消息")
    assert "[0] system | content=str 长度=6" in out
    assert "[1] user | content=str 长度=5" in out
    assert "[2] assistant" in out and "tool_calls×1[browser_snapshot]" in out
    # image_url 计数 + base64 头 60 字
    assert "image_url×1(data:image/png;base64,iVBORw0KGgoAAAANSUhEUgbrokenpayloa" in out
    assert "图但模型非视觉(×1)" in out
    assert "⚠️" in out and "[3]" in out.splitlines()[-2]

    # 空 content 数组 / 超长 / 畸形 tool_calls
    llm.last_failed_messages = [
        {"role": "system", "content": "s"},
        {"role": "user", "content": []},
        {"role": "assistant", "content": None,
         "tool_calls": [{"type": "function", "function": {"name": "no_id"}}]},
        {"role": "tool", "tool_call_id": "x", "content": "长" * 60000},
    ]
    out2 = fns["req_inspect"]("")
    assert "空content数组" in out2
    assert "超长(60,000>5万)" in out2
    assert "畸形tool_calls(缺id/name)" in out2
    assert "可疑消息：[1, 2, 3]" in out2

    # 干净存档 → 无可疑 + 建议语
    llm.last_failed_messages = [{"role": "user", "content": "hi"}]
    out3 = fns["req_inspect"]("")
    assert "未发现明显坏数据特征" in out3


def test_inspect_target_lookup():
    fns, main = _doctor()
    worker = FakeAgent(aid="worker_1", llm=FakeLLM(archive=None))
    main.registry = FakeRegistry({"worker_1": worker})
    assert "[目标不存在] agent_id='ghost'" in fns["req_inspect"]("ghost")
    assert "[无存档]" in fns["req_inspect"]("worker_1")
    # 无注册表的 agent 也能查自己
    main.registry = None
    main.llm = FakeLLM(archive=_archive())
    assert "失败请求体检" in fns["req_inspect"]("_main_")


# ── 2. req_probe：深拷贝应用 + 计数上限 ──

def test_probe_patch_applied_on_copy_and_cap():
    fns, main = _doctor()
    probe = fns["req_probe"]
    arch = _archive()
    main.llm = FakeLLM(archive=arch, fail_while=_fail_on_image)

    out1 = probe("", {"strip_images": [3]})
    assert "✅ probe 通过（第 1 次" in out1
    assert "strip_images[3] 剔除 1 个 image_url 部件" in out1
    # 存档本体未被改动（probe 只动深拷贝）
    assert len(ma._doctor_image_parts(arch[3])) == 1
    # 探测请求：图已剔 + max_tokens=16 最小探测
    sent, ov = main.llm.probe_calls[-1]
    assert sent[3]["content"] == [{"type": "text", "text": "页面快照如下"}]
    assert ov.get("max_tokens") == 16

    # 失败的 patch：返回 400 详情；计数照加
    out2 = probe("", {"truncate": {1: 10}})
    assert "❌ probe 失败（第 2/5 次）" in out2
    assert "Unable to decode the image" in out2
    assert "存档未被改动" in out2
    # patch 为 JSON 字符串也认（LLM 直传字符串的容错）
    out3 = probe("", '{"strip_images": []}')
    assert "第 3/5 次" in out3
    for _ in range(2):
        probe("", {})
    out6 = probe("", {"strip_images": [3]})
    assert "[probe 上限] '_main_' 已试修 5 次仍失败" in out6
    assert "判定非数据问题" in out6 and "禁止继续 probe" in out6
    # 上限后连好 patch 也拒绝——不再消耗 API
    n_before = len(main.llm.probe_calls)
    probe("", {"strip_images": [3]})
    assert len(main.llm.probe_calls) == n_before


# ── 3. req_patch 强制验证门 + 落盘 ──

def test_patch_gate_rejects_without_passing_probe():
    fns, main = _doctor()
    patch_fn = fns["req_patch"]
    arch = _archive()
    main.llm = FakeLLM(archive=arch, fail_while=_fail_on_image)   # 图没剔就恒 400

    out = patch_fn("", {"truncate": {1: 100}})   # 无效修法
    assert "🚫 拒绝落盘" in out and "必须 probe 200" in out
    # 存档/探测现场都不动
    assert len(ma._doctor_image_parts(arch[3])) == 1


def test_patch_persists_and_fixes_session_source():
    fns, main = _doctor()
    patch_fn = fns["req_patch"]
    sess = FakeSession()
    sess._current.steps.append(FakeStep(["a-1"]))
    sess.toollog.record("a-1", "browser_snapshot", {},
                        '快照<img src="data:image/png;base64,xx">图')

    worker = FakeAgent(aid="worker_1", session=sess,
                       llm=FakeLLM(archive=_archive(), fail_while=_fail_on_image))
    main.registry = FakeRegistry({"worker_1": worker})

    out = patch_fn("worker_1", {"strip_images": [3]})
    assert "✅ 已落盘到 'worker_1'" in out
    assert "session 已 _autosave" in out and sess.autosaves == 1
    assert sess.invalidations   # 触发了投影失效
    # session 源头：toollog 结果的 <img> 已剔（record 重记，view 读到的是修后值）
    assert "<img" not in sess.toollog.view("a-1")[2]
    assert "剔除 1 个 <img> 标签" in out
    # 存档已 in-place 修
    assert len(ma._doctor_image_parts(worker.llm.last_failed_messages[3])) == 0
    # 落盘成功 → 试修预算清零（下次事件新预算）
    assert "第 1 次" in fns["req_probe"]("worker_1", {"strip_images": []})

    # 修不了的 session 侧：历史档消息（非当前轮 tool 结果）→ 只修缓存并说明
    worker.llm.last_failed_messages = [
        {"role": "tool", "tool_call_id": "old-9", "content": [
            {"type": "image_url", "image_url": {"url": BAD_IMG}}]},
    ]
    out2 = patch_fn("worker_1", {"strip_images": [0]})
    assert "session 侧未修（只修了缓存）" in out2 and "历史档" in out2


# ── 4. req_resume：冷却清零 + 状态透传 ──

def test_resume_clears_cooldown_and_translates_states():
    fns, main = _doctor()
    resume = fns["req_resume"]
    llm = FakeLLM()
    worker = FakeAgent(aid="worker_1", llm=llm)
    main.registry = FakeRegistry({"worker_1": worker})

    out = resume("worker_1")
    assert llm._provider_cooldown == {}      # 冷却清零
    assert worker.resume_calls == 1
    assert "已恢复" in out and "冷却清 2 项" in out

    worker.resume_ret = "__queued__"
    llm._provider_cooldown = {"m3": 300.0}
    assert "入队其 inbox" in resume("worker_1")
    worker.resume_ret = "[错误] 没有可恢复的轮次"
    llm._provider_cooldown = {"m3": 300.0}
    out3 = resume("worker_1")
    assert "[恢复返回] [错误] 没有可恢复的轮次" in out3 and "冷却已清 1 项" in out3

    # resume_interrupted 抛异常 → 冷却仍清了 + 异常透传
    def boom():
        raise ValueError("session 炸了")
    worker.resume_interrupted = boom
    llm._provider_cooldown = {"m4": 400.0, "m5": 500.0}
    out4 = resume("worker_1")
    assert "[恢复异常] ValueError" in out4 and "冷却已清 2 项" in out4


# ── 5. _doctor_apply_patch：drop+truncate 修 messages ──

def test_apply_patch_drop_truncate_strip_replace():
    msgs = [
        {"role": "system", "content": "s" * 10},
        {"role": "user", "content": "u" * 100},
        {"role": "assistant", "content": None,
         "tool_calls": [{"id": "t", "type": "function",
                         "function": {"name": "f", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "t",
         "content": [{"type": "text", "text": "a" * 80},
                     {"type": "image_url", "image_url": {"url": BAD_IMG}}]},
        {"role": "user", "content": "final"},
    ]
    notes = ma._doctor_apply_patch(
        msgs, {"drop": [2], "truncate": {1: 20, 3: 30}, "strip_images": [3]})
    assert len(msgs) == 4
    assert msgs[1]["content"] == "u" * 20
    # drop/strip/truncate 索引一律按 patch 前编号：原 [3] 变 [2]
    assert msgs[2]["content"] == [{"type": "text", "text": "a" * 30}]
    assert any("drop 共删 1 条" in n for n in notes)
    assert any("truncate[3] 80→30" in n for n in notes)
    assert any("strip_images[3] 剔除 1 个" in n for n in notes)

    notes2 = ma._doctor_apply_patch(msgs, {"replace_text": {0: "占位"}})
    assert msgs[0]["content"] == "占位"
    assert "replace_text[0] 10→2" in notes2[0]

    # patch 入参归一化：JSON 字符串 / 非法值
    p, err = ma._doctor_normalize_patch('{"drop":[0]}')
    assert err == "" and p == {"drop": [0]}
    p2, err2 = ma._doctor_normalize_patch("not json")
    assert p2 is None and "解析失败" in err2
    p3, err3 = ma._doctor_normalize_patch(None)
    assert p3 == {} and err3 == ""


# ── 6. 存档点：回退链耗尽前冻结 last_failed_messages（真实 LLMClient） ──

def test_fallback_exhaustion_freezes_archive():
    from unittest.mock import patch as mock_patch
    import httpx
    from openai import BadRequestError
    from llm_client import LLMClient

    def boom(self, messages, **kw):
        req = httpx.Request("POST", "https://router.flatkey.ai/v1/chat/completions")
        body = {"error": {"message": "totally broken endpoint 400"}}
        resp = httpx.Response(400, request=req, json=body)
        raise BadRequestError(message="totally broken endpoint 400",
                              response=resp, body=body)

    msgs = [{"role": "user", "content": "现场冻结测试"}]
    c = LLMClient(model_name="fk-ds-flash", fallback_chain=["proxy"], fallback_policy="reset")
    with mock_patch.object(LLMClient, "_chat_inner", boom):
        with pytest.raises(RuntimeError, match="回退链不可用"):
            c.chat(msgs, tools=None)
    # 存档 = 失败请求全文（深拷贝——原列表后续变化不影响）
    assert getattr(c, "last_failed_messages", None) == [{"role": "user", "content": "现场冻结测试"}]
    msgs.append({"role": "user", "content": "after"})
    assert len(c.last_failed_messages) == 1
