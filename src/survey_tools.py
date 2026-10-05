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


def _normalize_survey(questions):
    """问卷宽容归一（2026-10-05·20048 实例实锤：对象选项渲染成 [object Object] / 整句逐词拆成百个选项）。
    - options 对象项：提取 text/label/value/name/option/content/title 键 → 字符串
    - options 整段字符串：按常见分隔符（、,，;；/|换行）拆；拆不动按单选项并警告
    - 疑似逐词拆分（>10 项且平均长度<6）：警告（LLM 把整句拆成单词数组的常见失误）
    返回 (归一后的 questions, warnings)；warnings 会附在题面顶部给用户看。"""
    import json as _j, re as _re
    warns = []

    def _norm_opts(opts, qid):
        if isinstance(opts, str):
            parts = [x.strip() for x in _re.split(r"[、,，;；/|\n]", opts) if x.strip()]
            if len(parts) > 1:
                warns.append(f"第{qid}题 options 传了整段字符串，已按分隔符拆成 {len(parts)} 项")
                return parts
            warns.append(f"第{qid}题 options 只有一个字符串选项（多选项请传字符串数组）")
            return [opts]
        if isinstance(opts, list):
            out = []
            for o in opts:
                if isinstance(o, str):
                    out.append(o)
                elif isinstance(o, dict):
                    for k in ("text", "label", "value", "name", "option", "content", "title"):
                        if isinstance(o.get(k), str) and o[k].strip():
                            out.append(o[k]); break
                    else:
                        out.append(_j.dumps(o, ensure_ascii=False))
                else:
                    out.append(str(o))
            if len(out) > 10 and out and sum(len(x) for x in out) / len(out) < 6:
                warns.append(f"第{qid}题 options 有 {len(out)} 项且平均长度过短——疑似整句被逐词拆开，请检查")
            return out
        return []

    out = []
    for i, q in enumerate(questions, 1):
        if not isinstance(q, dict):
            continue
        nq = dict(q)
        nq["options"] = _norm_opts(q.get("options") or [], i)
        t_ = q.get("title") or q.get("question") or q.get("prompt") or q.get("label") or ""
        if not t_:
            t_ = f"（第{i}题未提供题面）"
        nq["title"] = str(t_)
        out.append(nq)
    return out, warns



def check_pending_survey(agent) -> bool:
    """检测是否有 pending survey（committed 态）。有则 re-emit survey_pending 事件。
    用于启动/重连/读档后恢复等待状态。返回是否有 pending survey。"""
    sid = agent.session.extra_state.get("_pending_survey", None)
    if not sid:
        return False
    # 恢复路径同归一（2026-10-05·20048 实锤：旧存档的坏问卷重启后原样渲染）——
    # 对象选项提文本/整段字符串拆分，存回后再 emit
    try:
        sid, _w = _normalize_survey(sid)
        agent.session.extra_state["_pending_survey"] = sid
    except Exception:
        pass
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
        questions, _warns = _normalize_survey(questions)
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
            return "[错误] 用户未提供任何回答" + ('\n⚠️ 归一警告：' + '；'.join(_warns) if _warns else '')
        # 返回 answers 的 JSON 字符串（Agent 可 parse 后使用）；附归一警告供 agent 复盘自愈
        return _j.dumps(result, ensure_ascii=False, indent=2) + ('\n⚠️ 归一警告：' + '；'.join(_warns) if _warns else '')
    
    ask_user.__doc__ += "\n\n示例：\n" + """```json
[
  {"id": "lang", "title": "用什么语言？", "options": ["Python", "Go", "Rust"], "multi_select": false, "allow_custom": true},
  {"id": "features", "title": "需要哪些功能？", "options": ["日志", "监控", "告警"], "multi_select": true, "allow_custom": true}
]
```"""
    
    return [Tool(ask_user, param_schemas={
        "questions": {
            "type": "array",
            "description": "问卷题目数组",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string", "description": "题目唯一标识（用于返回 answers 的 key）"},
                    "title": {"type": "string", "description": "题面/问题描述"},
                    "options": {"type": "array", "items": {"type": "string"}, "description": "选项数组"},
                    "multi_select": {"type": "boolean", "description": "是否多选（false=单选 radio, true=多选 checkbox）"},
                    "allow_custom": {"type": "boolean", "description": "是否允许用户自定义输入"},
                },
                "required": ["id", "title", "options"],
            },
        }
    })]


# ========== human_step：人在环——指挥人类完成 GUI/物理操作步骤并收取反馈 ==========

def _emit_human_step(agent, event_type: str = "human_step_pending"):
    """广播 human_step 事件（WebUI 渲染引导卡片）。"""
    step = agent.session.extra_state.get("_pending_human_step", {})
    if agent.on_event:
        agent.on_event({"type": event_type, "id": step.get("id", ""),
                        "instruction": step.get("instruction", ""),
                        "expect": step.get("expect", "")})


def resolve_human_step(agent, sid: str, text: str):
    """用户提交操作结果 → 解除 human_step 的阻塞。由 server.py（WS action）调用。"""
    entry = (getattr(agent, "_human_step_events", None) or {}).get(sid)
    if entry:
        entry["result"] = text
        entry["event"].set()
        return
    # 无阻塞线程（重启后恢复场景）→ 以系统消息注入
    agent.session.extra_state.pop("_pending_human_step", None)


def get_human_step_tools(agent) -> list[Tool]:
    """返回 [Tool(human_step)]——人在环：Agent 指挥人类完成 GUI/物理操作并收取反馈（用户提案 2026-09-29）。"""

    def human_step(instruction: str, expect: str = "") -> str:
        """指挥人类完成一个 GUI/物理操作步骤，阻塞等待其完成后收取反馈文本。
        instruction: 给人类的操作指引——具体、有步骤感（如"扫码完成登录xxx"、"勾选最下方的同意协议，然后点击安装按钮"）。
        expect: 期望人类回报什么（如"登录后的页面布局"、"安装进度"）——帮助人类知道该反馈什么信息。
        返回: 人类完成操作后的反馈文本（页面描述/结果状态/截图描述等）。30 分钟无反馈自动超时。"""
        import uuid
        sid = uuid.uuid4().hex[:8]
        ev = threading.Event()
        if not hasattr(agent, "_human_step_events"):
            agent._human_step_events = {}
        entry = {"event": ev, "result": None}
        agent._human_step_events[sid] = entry
        agent.session.extra_state["_pending_human_step"] = {
            "id": sid, "instruction": instruction, "expect": expect}
        _emit_human_step(agent)
        got = ev.wait(timeout=1800)
        agent._human_step_events.pop(sid, None)
        agent.session.extra_state.pop("_pending_human_step", None)
        if not got or entry["result"] is None:
            return "[超时] 30 分钟内未收到人类反馈——操作可能未完成，请决定重试或改变策略"
        return f"人类反馈：\n{entry['result']}"

    return [Tool(human_step, param_schemas={
        "instruction": {"type": "string", "description": "给人类的操作指引——具体、有步骤感（如\"扫码完成登录xxx\"、\"勾选同意协议后点击安装按钮\"）"},
        "expect": {"type": "string", "description": "期望人类回报什么（如\"登录后的页面布局\"、\"安装进度\"）——帮人类知道该反馈什么"},
    })]
