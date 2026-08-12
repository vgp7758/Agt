"""survey_tools.py —— Agent 向用户发起问卷（阻塞等待用户回答）。

复用 commit_spec 的阻塞模式：
  - ask_user(questions) → extra_state["_pending_survey"] 持久化 + threading.Event().wait()
  - 用户回答 → resolve_survey(agent, answers) → event.set()
  - 程序重启 → check_pending_survey → re-emit survey_pending → 用户回答 → resolve（场景2）

数据结构：
  questions: [{"id": "q1", "title": "题面", "options": ["A","B","C"], "multi_select": false, "allow_custom": true}]
  answers: {"q1": "Python", "q2": ["日志", "监控", "我想要链路追踪"]}
"""
from __future__ import annotations

import threading
from typing import Optional

from tools import Tool


def _emit_survey(agent, event_type: str = "survey_pending"):
    """广播 survey 事件（WebUI 渲染表单 / CLI 打印题目）。"""
    questions = agent.session.extra_state.get("_pending_survey", [])
    if agent.on_event:
        agent.on_event({
            "type": event_type,
            "questions": questions,
        })


def check_pending_survey(agent) -> bool:
    """检测是否有 pending survey（committed 态）。有则 re-emit survey_pending 事件。
    用于启动/重连/读档后恢复等待状态。返回是否有 pending survey。"""
    sid = agent.session.extra_state.get("_pending_survey", None)
    if not sid:
        return False
    _emit_survey(agent, "survey_pending")
    return True


def resolve_survey(agent, answers: dict):
    """用户对 ask_user 的阻塞等待做出回答。由 server.py（WS action）或 chat.py（CLI 命令）调用。
    
    两种场景：
    1. 正常阻塞中：ask_user 在 worker 线程里 Event.wait() 阻塞 → set event 解除阻塞，
       ask_user 自己返回 answers 给 Agent。
    2. 读档恢复：程序重启后从 extra_state 发现 _pending_survey → 直接执行（因为原始的 agent.run 已随程序退出而消失）。"""
    ev = getattr(agent, "_survey_decision_event", None)
    if ev:
        # 场景 1：正常阻塞中——set event 解除 ask_user 的阻塞
        agent._survey_decision_result = answers
        ev.set()
        return
    # 场景 2：读档恢复——没有阻塞线程，直接处理
    questions = agent.session.extra_state.pop("_pending_survey", None)
    if not questions:
        return
    # 直接返回 answers 给 Agent（通过系统消息）
    from io import StringIO
    buf = StringIO()
    buf.write("✅ 用户已完成问卷：\n")
    for q in questions:
        qid = q.get("id", "")
        ans = answers.get(qid, "(未答)")
        if isinstance(ans, list):
            ans = ", ".join(str(a) for a in ans)
        buf.write(f"  {qid}: {ans}\n")
    agent.on_event({"type": "system", "text": buf.getvalue()})


def make_survey_tools(agent):
    """返回 [Tool(ask_user)]。"""
    
    def ask_user(questions: list) -> str:
        """向用户发起问卷（阻塞等待用户完成全部题目后继续）。
        
        :param questions: 问卷题目数组，每项含：
            - id: 题目唯一标识（用于后续 answers 字典的 key）
            - title: 题面（问题描述）
            - options: 选项数组（如 ["Python", "Go", "Rust"]）
            - multi_select: 是否多选（false=单选 radio，true=多选 checkbox）
            - allow_custom: 是否允许用户自定义输入（是则在选项末尾加"其他，请输入..."）
        :return: 用户回答的 JSON 字符串（{"q1": "答案 1", "q2": ["答案 A", "答案 B"]}）
        
        阻塞等待用户裁定（无超时——一直等到用户回应或程序关闭）。
        程序重启后从 extra_state 恢复等待状态。"""
        import json as _j
        if not questions or not isinstance(questions, list):
            return "[错误] questions 需为非空数组"
        # 记录 pending survey 到 extra_state（持久化：程序关了读档后能恢复等待状态）
        agent.session.extra_state["_pending_survey"] = questions
        # 阻塞等待用户裁定（无超时——一直等到用户回应或程序关闭）
        agent._survey_decision_event = threading.Event()
        agent._survey_decision_result = None
        _emit_survey(agent, "survey_pending")
        agent._survey_decision_event.wait()   # 无限等待
        result = agent._survey_decision_result
        agent._survey_decision_event = None
        # 清除 pending 标记
        agent.session.extra_state.pop("_pending_survey", None)
        if result is None:
            return "[错误] 用户未提供任何回答"
        # 返回 answers 的 JSON 字符串（Agent 可 parse 后使用）
        return _j.dumps(result, ensure_ascii=False, indent=2)
    
    ask_user.__doc__ += "\n\n示例：\n" + """```json
[
  {"id": "lang", "title": "用什么语言？", "options": ["Python", "Go", "Rust"], "multi_select": false, "allow_custom": true},
  {"id": "features", "title": "需要哪些功能？", "options": ["日志", "监控", "告警"], "multi_select": true, "allow_custom": true}
]
```"""
    
    return [Tool(ask_user)]
