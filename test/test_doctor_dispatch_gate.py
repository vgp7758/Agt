"""doctor 自动派发的通道类门控（2026-11-25）。

背景：子 Agent 因回退链耗尽失败时，multiagent 无条件派 doctor——但 429 限流 / Connection error
这类通道问题没有请求数据可修，doctor 自己还会撞同一通道再失败一次（连锁白耗）。
门控：last_failures 的真实失败（cls≠cooldown）全为 rate_limit / network / quota → 跳过派发。
"""
import ast
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from multiagent import _all_channel_failures, _CHANNEL_ONLY_CLS  # noqa: E402


def _f(cls):
    return {"model": "m", "err": "X", "cls": cls, "msg": "..."}


def test_all_channel_skip():
    assert _all_channel_failures([_f("rate_limit"), _f("network")]) is True
    assert _all_channel_failures([_f("quota")]) is True
    assert _all_channel_failures([_f("cooldown"), _f("cooldown"), _f("rate_limit")]) is True


def test_data_problem_still_dispatch():
    # other = 400 BadRequest 等数据问题 → 必须照旧派 doctor
    assert _all_channel_failures([_f("rate_limit"), _f("other")]) is False
    assert _all_channel_failures([_f("other")]) is False
    assert _all_channel_failures([_f("network"), _f("not_found")]) is False
    assert _all_channel_failures([_f("auth")]) is False


def test_insufficient_info_still_dispatch():
    # 宁多勿漏：明细为空 / None / 只剩 cooldown 记录 → 照旧派
    assert _all_channel_failures([]) is False
    assert _all_channel_failures(None) is False
    assert _all_channel_failures([_f("cooldown")]) is False
    assert _all_channel_failures([{"model": "m"}]) is False      # 无 cls 字段


def test_channel_cls_set_is_exact():
    assert _CHANNEL_ONLY_CLS == {"rate_limit", "network", "quota"}


def test_doctor_call_sits_in_gate_else_branch():
    """结构回归：派 doctor 的 agent_prompt 调用必须位于 `if _all_channel_failures(...)` 的 else 分支内，
    防止后续重构把门控挪坏（调用跑到 if 外 = 门控失效）。"""
    src = Path(__file__).resolve().parent.parent / "src" / "multiagent.py"
    tree = ast.parse(src.read_text(encoding="utf-8"))
    parent = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parent[child] = node

    def _is_doctor_call(n):
        return (isinstance(n, ast.Call) and getattr(n.func, "id", "") == "agent_prompt"
                and n.args and isinstance(n.args[0], ast.Constant) and n.args[0].value == "doctor")

    calls = [n for n in ast.walk(tree) if _is_doctor_call(n)]
    assert len(calls) == 1, f"应恰有一处派 doctor 的调用，实得 {len(calls)}"
    cur, guarded = calls[0], False
    while cur in parent:
        p = parent[cur]
        if (isinstance(p, ast.If) and isinstance(p.test, ast.Call)
                and getattr(p.test.func, "id", "") == "_all_channel_failures"
                and cur in p.orelse):
            guarded = True
            break
        cur = p
    assert guarded, "派 doctor 的调用不在 _all_channel_failures 门控的 else 分支内"


def test_main_agent_dispatch_doctor_also_gated():
    """结构回归（主 Agent 路径）：agent.py 的 dispatch_doctor(...) 调用也必须位于门控的 else 分支内。"""
    src = Path(__file__).resolve().parent.parent / "src" / "agent.py"
    tree = ast.parse(src.read_text(encoding="utf-8"))
    parent = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parent[child] = node
    calls = [n for n in ast.walk(tree)
             if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "dispatch_doctor"]
    assert len(calls) == 1, f"应恰有一处 dispatch_doctor 调用，实得 {len(calls)}"
    cur, guarded = calls[0], False
    while cur in parent:
        p = parent[cur]
        if (isinstance(p, ast.If) and isinstance(p.test, ast.Call)
                and getattr(p.test.func, "id", "") == "_all_channel_failures"
                and cur in p.orelse):
            guarded = True
            break
        cur = p
    assert guarded, "dispatch_doctor 调用不在 _all_channel_failures 门控的 else 分支内"


# ---------- 5xx 计费错误按文案归 quota（moss 实锤 2026-11-26）----------
def test_billing_5xx_classified_as_quota():
    """代理把上游 402 计费错误包成 502 proxy_error——SDK 归 InternalServerError。
    旧逻辑落 other → 不属通道类 → doctor 照派（而它治不了欠费）。按文案识别为 quota：
    doctor 派发门控按通道类跳过；UI 充值入口（按 cls=quota/auth）同批生效。"""
    import httpx
    import openai
    from llm_client import _classify_err

    def _err(status, msg):
        resp = httpx.Response(status, request=httpx.Request("POST", "http://x/v1/chat/completions"))
        cls = openai.InternalServerError if status >= 500 else openai.BadRequestError
        return cls(msg, response=resp, body=None)

    billing = ("Error code: 502 - {'error': {'message': 'RuntimeError: backend HTTP 402: "
               '{\"error\":{\"message\":\"billing error\",\"code\":\"insufficient_credits\"}}',
               "'type': 'proxy_error', 'code': 502}}")
    e = _err(502, billing)
    assert isinstance(e, openai.InternalServerError)
    cls = _classify_err(e)
    assert cls == "quota", f"5xx 计费错误应归 quota，实得 {cls}"
    # 端到端语义：这条失败明细进门控 → 判定为通道类 → 不派 doctor
    assert _all_channel_failures([{"model": "moss", "err": "InternalServerError",
                                   "cls": cls, "msg": billing}]) is True
    assert _classify_err(_err(502, "Error code: 502 - bad gateway")) == "other"        # 无计费词的 5xx 仍归 other
    assert _classify_err(_err(400, "insufficient permissions for this field")) == "other"  # 400 保守不转
    assert _classify_err(_err(500, "Error code: 500 - insufficient_quota")) == "quota"   # 上游配额词同样命中
