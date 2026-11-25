"""remote_message 工具调用指纹去重（用户方案 2026-11-25：工具调用参数 + 当前轮号）。

指纹 = sha1({tool, to, message, expect_reply, turn, from})[:16]，在 expect_reply 协议头
注入前计算（只含原始工具参数：发送内容 + 发送对象 + 其它参数 + 当前轮号 + 发送方）。
接收端 _fp_seen_and_register：任一键在 _FP_WINDOW（600s，盖过发送端 ~5 分钟重发窗口）
内出现过 → 重复，丢弃；窗口外同指纹 = 新意图，放行。_rmid（旧发送端）同表兼容。
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import remote_tools as rt  # noqa: E402
from remote_tools import _msg_fingerprint  # noqa: E402
from server import _FP_WINDOW, _RMSG_SEEN, _fp_seen_and_register  # noqa: E402


def test_fingerprint_covers_content_target_params_turn_sender():
    a = _msg_fingerprint("8000", "同一句话", False, 5, "9000")
    assert a == _msg_fingerprint("8000", "同一句话", False, 5, "9000")     # 确定性：同参数同指纹
    assert a.startswith("fp_") and len(a) == 19
    assert _msg_fingerprint("8000", "另一句话", False, 5, "9000") != a      # 发送内容变 → 指纹变
    assert _msg_fingerprint("20048", "同一句话", False, 5, "9000") != a     # 发送对象变 → 指纹变
    assert _msg_fingerprint("8000", "同一句话", True, 5, "9000") != a       # 其它参数变 → 指纹变
    assert _msg_fingerprint("8000", "同一句话", False, 6, "9000") != a      # 轮号变（跨轮=新意图）→ 放行
    assert _msg_fingerprint("8000", "同一句话", False, 5, "20048") != a     # 发送方变（多实例互发不误吞）


def test_fingerprint_uses_preprotocol_header_message(monkeypatch):
    """expect_reply=True 时 message 会被注入 ⟨expect_reply:url⟩ 协议头——指纹必须在注入前
    用原始 message 计算（否则本机回发 URL 一变，同一次发送的指纹就变了）。"""
    captured = {}

    def fake_send(url, **kw):
        captured.update(kw.get("payload") or {})
        return ("✅ 已送达（测试桩）", [])

    fake = type("FA", (), {"session": type("S", (), {"turns": [1, 2, 3]})()})()
    monkeypatch.setattr(rt, "_ws_send_collect", fake_send)
    monkeypatch.setattr(rt, "REMOTE_SERVERS", {"8000": {"url": "ws://x"}})
    monkeypatch.setattr(rt, "MY_URL", "http://x:9000")
    rt.send_message("8000", "同一句话", expect_reply=True, _agent=fake)
    assert captured.get("text", "").startswith("⟨expect_reply:http://x:9000⟩")   # 协议头确实注入了
    # 指纹用注入前的原文 + 当前轮号（len(turns)=3）+ 发送方 tag
    assert captured.get("_fp") == _msg_fingerprint("8000", "同一句话", True, 3, rt._self_tag())
    # 轮号推进 → 同内容下一轮指纹不同（新意图放行）
    fake.session.turns = [1, 2, 3, 4]
    rt.send_message("8000", "同一句话", expect_reply=True, _agent=fake)
    assert captured.get("_fp") != _msg_fingerprint("8000", "同一句话", True, 3, rt._self_tag())


def test_seen_and_register_window():
    _RMSG_SEEN.clear()
    now = time.time()
    assert _fp_seen_and_register(["fp_a"], now) is False                     # 首见 → 放行
    assert _fp_seen_and_register(["fp_a"], now + 1) is True                  # 窗口内重复 → 丢弃
    assert _fp_seen_and_register(["fp_b"], now + 2) is False                 # 不同指纹互不影响
    assert _fp_seen_and_register(["fp_a"], now + _FP_WINDOW + 1) is False    # 窗口外 = 新意图 → 放行
    assert _fp_seen_and_register([None, ""], now) is False                   # 空键不登记不判定
    assert _fp_seen_and_register(["x" * 100], now) is False                  # 超长键按 64 位截断登记
    assert _RMSG_SEEN.get("x" * 64), "超长键应截断后登记"


def test_rmid_legacy_key_same_table():
    """旧发送端只带 _rmid——同表同窗口兼容。"""
    _RMSG_SEEN.clear()
    now = time.time()
    assert _fp_seen_and_register(["rm_abc"], now) is False
    assert _fp_seen_and_register(["rm_abc"], now + 5) is True
    assert _fp_seen_and_register(["fp_x", "rm_abc"], now + 6) is True        # 任一键命中即重复
    assert _fp_seen_and_register(["fp_x"], now + 6 + _FP_WINDOW + 2) is False    # fp_x 登记后超窗 → 放行
