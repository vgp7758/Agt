# -*- coding: utf-8 -*-
"""sleep 不睡过头（2026-11-27 用户提案）：等待中收到插话/后台消息提前醒来；没收/停止立即中断。

mock 最小 owner（线程局部 set_bg_owner），不依赖 LLM。覆盖：
① 插话到达 → 提前醒来并附摘要/剩余秒数；② 后台 inbox 到达 → 带来源摘要，起点前的旧消息不算；
③ 后台泵瞬态（入队后立刻弹出旧条，长度不变）→ 靠末条对象判据仍识别并报出；
④ wake_on_message=False 睡满；⑤ 无 owner（脱离 Agent 上下文执行）睡满；
⑥ 用户停止 / ⑦ 被没收 → 立即中断；⑧ 起点前已在队的消息不触发；⑨ schema 暴露 wake 参数。"""
import collections
import threading
import time

import pytest

from real_tools import set_bg_owner
from assets.tools_builtin import misc_tools as mt


class _Owner:
    def __init__(self):
        self.pending_messages = []
        self.inbox = collections.deque()
        self._revoked = False
        self._revoked_reason = ""
        self._stop_flag = False


@pytest.fixture(autouse=True)
def _reset_owner():
    yield
    set_bg_owner(None)


def _after(delay, fn):
    threading.Timer(delay, fn).start()


def test_wakes_on_pending_message_with_preview():
    ow = _Owner()
    set_bg_owner(ow)
    _after(0.6, lambda: ow.pending_messages.append("[来自队友 '_main_' 的提示] 服务已就绪"))
    t0 = time.time()
    out = mt.sleep(30)
    assert time.time() - t0 < 3, time.time() - t0
    assert "提前结束" in out and "插话 1 / 后台 0" in out
    assert "服务已就绪" in out and "下一步即注入" in out
    assert "sleep(约" in out


def test_wakes_on_inbox_message_with_source():
    ow = _Owner()
    set_bg_owner(ow)
    ow.inbox.append(("bg_task", "旧任务回执", None))     # 起点前就在的：不算新消息
    _after(0.6, lambda: ow.inbox.append(("service_exit", "svc-x 已退出 rc=0", None)))
    t0 = time.time()
    out = mt.sleep(30)
    assert time.time() - t0 < 3
    assert "插话 0 / 后台 1" in out
    assert "后台·service_exit" in out and "svc-x 已退出" in out
    assert "旧任务回执" not in out


def test_transient_push_then_pop_still_wakes():
    """后台泵瞬态：新消息入队后旧条立刻被弹出（长度不变）——末条对象判据仍能识别并报出。"""
    ow = _Owner()
    set_bg_owner(ow)
    ow.inbox.append(("bg_task", "a", None))
    ow.inbox.append(("bg_task", "b", None))

    def churn():
        ow.inbox.popleft()
        ow.inbox.append(("bg_task", "新来的 c", None))

    _after(0.6, churn)
    t0 = time.time()
    out = mt.sleep(30)
    assert time.time() - t0 < 3
    assert "新来的 c" in out


def test_wake_on_message_false_sleeps_full():
    ow = _Owner()
    set_bg_owner(ow)
    _after(0.3, lambda: ow.pending_messages.append("不该吵醒"))
    t0 = time.time()
    out = mt.sleep(1.3, wake_on_message=False)
    assert out.startswith("已等待") and time.time() - t0 >= 1.2


def test_no_owner_sleeps_full():
    set_bg_owner(None)
    t0 = time.time()
    out = mt.sleep(0.7)
    assert out == "已等待 0.7 秒" and time.time() - t0 >= 0.6


def test_stop_flag_interrupts():
    ow = _Owner()
    set_bg_owner(ow)
    _after(0.5, lambda: setattr(ow, "_stop_flag", True))
    t0 = time.time()
    out = mt.sleep(30)
    assert time.time() - t0 < 3 and out.startswith("[已停止]")


def test_revoked_interrupts():
    ow = _Owner()
    set_bg_owner(ow)
    _after(0.5, lambda: setattr(ow, "_revoked", True))
    t0 = time.time()
    out = mt.sleep(30)
    assert time.time() - t0 < 3 and out.startswith("[已中断]")


def test_messages_present_before_start_do_not_wake():
    ow = _Owner()
    set_bg_owner(ow)
    ow.pending_messages.append("早就有的插话")
    ow.inbox.append(("bg_task", "早就有的回执", None))
    t0 = time.time()
    out = mt.sleep(1.2)
    assert out == "已等待 1.2 秒" and time.time() - t0 >= 1.1


def test_schema_exposes_wake_param():
    from tools import Tool
    t = Tool(mt.sleep, param_descriptions={"seconds": "秒数", "wake_on_message": "是否被消息叫醒"})
    params = t.schema["function"]["parameters"]
    assert params["properties"]["wake_on_message"]["type"] == "boolean"
    assert "wake_on_message" not in params["required"]
    assert params["properties"]["seconds"]["description"] == "秒数"
    assert "提前醒来" in t.description          # 首行（=LLM 看到的工具描述）必须点明不会睡过头
