"""spec_tools.py —— 复杂任务的施工方案（spec）流程。

当前 create_plan 是「标题 + 一维 todoList + design 字段」，对简单任务够用，但对「多处修改 / 跨文件 /
需要先探索再施工」的复杂任务 hold 不住。本模块在 plan_tools 之上叠加一套 spec 流程：

  1. 探索（可选）：explore_subagent 派若干一次性子 Agent 并行去读不同模块/文件，各自返回发现报告，
     汇总后喂给主 Agent 生成更准的施工方案。
  2. 制定：create_spec(title, steps, design) 把【结构化施工步骤】落盘成 spec 文件（draft 态）。
     每个 step 是 {file, action, anchor, content, rationale}，机器可读、可自动落地。
  3. 批阅：commit_spec(spec_id) 触发 UI 事件「请批阅施工方案」，等用户裁定。
     - 用户「通过」→ build_plan_from_spec(spec_id) 自动生成对应 plan、设为活动计划、开始施工。
     - 用户「返工」+ 反馈 → regenerate_spec(spec_id, feedback) 标记旧 spec rejected，
       Agent 据反馈重新生成新 spec（新 id），再次 commit_spec 供批阅。

存储：~/.agt/repos/<hash>/specs/<spec_id>.json，每个 spec 一个文件、带稳定 id，跨 session 共享。
内存模型：agent.active_spec（完整 dict，单一事实源）+ agent.active_spec_id。
批阅态机：draft → committed → approved | rejected（rejected 后由 regenerate 产生新 spec）。
"""
from __future__ import annotations

import json
import os
import time
import uuid
from datetime import datetime
from pathlib import Path

from session import repo_plans_dir
from tools import Tool

_SPEC_ICON = {"draft": "📝", "committed": "🔍", "approved": "✅", "rejected": "❌"}
_SPEC_LABEL = {"draft": "草稿", "committed": "待批阅", "approved": "已通过", "rejected": "已返工"}
_VALID_REVIEW = ("approved", "rejected")
_VALID_ACTION = ("create", "insert", "edit", "replace", "delete", "move", "review")


# ========== 目录 ==========

def _specs_dir(workspace) -> Path:
    """该工作区的【施工方案】目录：~/.agt/repos/<hash>/specs/。与 plans/ 同根、互相隔离。
    每个 spec 一个 <spec_id>.json 文件，跨 session 共享。"""
    d = Path.home() / ".agt" / "repos" / _repo_hash(workspace) / "specs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _repo_hash(workspace) -> str:
    """与 session.repo_plans_dir / repo_memories_dir 同算法：sha1(绝对路径)[:12]。"""
    import hashlib
    return hashlib.sha1(str(Path(workspace).resolve()).encode("utf-8")).hexdigest()[:12]


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _gen_spec_id() -> str:
    """本 repo 内足够唯一的 spec id：s_ + 8 位 hex。文件名即 <spec_id>.json。"""
    return "s_" + uuid.uuid4().hex[:8]


def _spec_path(workspace, spec_id: str) -> Path:
    return _specs_dir(workspace) / f"{spec_id}.json"


# ========== 文件 I/O ==========

def _load_spec(workspace, spec_id: str):
    """按 id 读单个 spec；不存在 / 损坏返回 None。"""
    p = _spec_path(workspace, spec_id)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def _save_spec(workspace, spec: dict) -> None:
    """原子落盘：写 .tmp 再 os.replace；刷新 updated_at。"""
    spec["updated_at"] = _now_iso()
    p = _spec_path(workspace, spec["id"])
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(spec, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, p)


def _list_specs(workspace) -> list:
    """列出本 repo 全部 spec，按 updated_at 倒序。"""
    out = []
    for f in _specs_dir(workspace).glob("s_*.json"):
        try:
            out.append(json.loads(f.read_text(encoding="utf-8")))
        except Exception:
            continue
    out.sort(key=lambda x: x.get("updated_at", ""), reverse=True)
    return out


# ========== step 规整 ==========

def _normalize_step(s, idx: int) -> dict:
    """把模型传入的一个 step 规整成统一结构 {file, action, anchor, content, rationale}。
    模型可能传：
      - 纯字符串 → 当作 description（action=review, content=该字符串）
      - dict 含 file/action/anchor/content/rationale → 直接用（补缺省）
      - dict 含 description → 旧式 plan step，降级成 review（content=description）
    无论哪种，都抽成干净的、UI 可渲染、可自动落地的结构。"""
    if isinstance(s, str):
        st = s.strip()
        return {"file": "", "action": "review", "anchor": "", "content": st, "rationale": ""}
    if not isinstance(s, dict):
        st = str(s).strip()
        return {"file": "", "action": "review", "anchor": "", "content": st, "rationale": ""}
    # 优先用结构化字段
    action = (str(s.get("action", "") or "").strip().lower() or "review")
    if action not in _VALID_ACTION:
        action = "review"
    file = str(s.get("file", s.get("path", "")) or "").strip()
    anchor = str(s.get("anchor", s.get("at", "")) or "").strip()
    content = str(s.get("content", s.get("new", "")) or "")
    rationale = str(s.get("rationale", s.get("why", s.get("reason", ""))) or "").strip()
    # 旧式 plan step 降级
    if not content and not file:
        desc = str(s.get("description", s.get("desc", s.get("text", ""))) or "").strip()
        if desc:
            content, action = desc, "review"
    return {"file": file, "action": action, "anchor": anchor, "content": content, "rationale": rationale}


# ========== 内存 ↔ 落盘 同步 ==========

def _set_active_spec(agent, spec: dict):
    agent.active_spec = spec
    agent.active_spec_id = spec.get("id") if spec else None


def _clear_active_spec(agent):
    agent.active_spec = None
    agent.active_spec_id = None


def _spec_event_payload(agent, event_type: str = "spec") -> dict:
    """构造 spec 的 UI 事件 payload。_emit_spec（广播）与 WS 重连/切会话补发共用，
    保证字段一致；committed/rejected 态由调用方传 event_type='spec_review'。"""
    spec = getattr(agent, "active_spec", None)
    return {
        "type": event_type,
        "spec": _spec_view(spec) if spec else None,
        "spec_id": getattr(agent, "active_spec_id", None),
        "review_state": (spec or {}).get("review_state", ""),
        "spec_title": (spec or {}).get("title", ""),
        "spec_design": (spec or {}).get("design", ""),
    }


def _emit_spec(agent, event_type: str = "spec"):
    """把当前 spec 推给 UI（spec 面板同步 + 批阅态）。"""
    if getattr(agent, "on_event", None):
        try:
            agent.on_event(_spec_event_payload(agent, event_type))
        except Exception:
            pass


def _spec_view(spec: dict) -> list:
    """spec 的 UI 投影视：steps 只取可展示字段（不含 rationale 的大块，UI 按需展开）。"""
    if not spec:
        return []
    return [{"file": s.get("file", ""), "action": s.get("action", "review"),
             "anchor": s.get("anchor", ""), "content": s.get("content", "")}
            for s in spec.get("steps", [])]


def restore_active_spec(agent, state: dict) -> None:
    """从 session 存档恢复活动 spec：按 spec_id 从文件读回。供 agent.restore_runtime_state 调用。"""
    if not state:
        _clear_active_spec(agent)
        return
    spec_id = state.get("spec_id")
    if spec_id:
        spec = _load_spec(agent.session.workspace, spec_id)
        if spec:
            _set_active_spec(agent, spec)
        else:
            _clear_active_spec(agent)
        return
    _clear_active_spec(agent)


def clear_active_spec(agent) -> None:
    """清空活动 spec（/reset、新 session 调用）。文件保留，可再次 load。"""
    _clear_active_spec(agent)


# ========== SYSTEM 注入块 ==========

def _format_spec_block(agent) -> str:
    """活动 spec 的 SYSTEM 注入块。无活动 spec / 非 draft/committed 态返回 ''（session 不注入）。
    approved 态 → 由生成的 plan 接管注入（避免双重注入）；draft/committed 才注入让 Agent 一直清楚在等批阅。"""
    s = getattr(agent, "active_spec", None)
    if not s or not s.get("steps"):
        return ""
    rs = s.get("review_state", "draft")
    if rs == "approved":
        return ""   # 已通过 → 已生成 plan，由 _plan_provider 注入
    title = s.get("title", "")
    design = (s.get("design") or "").strip()
    lines = [f"【施工方案 spec】{s.get('id', '')}" + (f" · {title}" if title else "") +
             f"（{_SPEC_LABEL.get(rs, rs)}）"]
    if design:
        lines.append("设计：" + design)
    lines.append(f"施工步骤（共 {len(s['steps'])} 步）：")
    for i, st in enumerate(s["steps"]):
        act = st.get("action", "review")
        file = st.get("file", "") or "(无文件)"
        anchor = st.get("anchor", "")
        anchor_s = f" @ {anchor}" if anchor else ""
        rat = st.get("rationale", "")
        rat_s = f" — {rat}" if rat else ""
        lines.append(f"  {i + 1}. [{act}] {file}{anchor_s}{rat_s}")
    if rs == "draft":
        lines.append("这是草稿，尚未提交批阅。用 commit_spec 提交，或 regenerate_spec 改进。")
    elif rs == "committed":
        lines.append("已提交批阅，等待用户裁定（通过 → 自动建 plan 开始施工；返工 → 据反馈重新生成）。")
    elif rs == "rejected":
        fb = s.get("feedback", "")
        lines.append("已被返工。" + (f"用户反馈：{fb}" if fb else ""))
        lines.append("请据反馈用 regenerate_spec(spec_id, feedback) 重新生成一版 spec。")
    return "\n".join(lines)


# ========== 渲染（工具返回值用）==========

def _spec_text(spec: dict) -> str:
    """spec 的纯文本视图（工具返回值用）。"""
    if not spec:
        return "(空 spec)"
    rs = spec.get("review_state", "draft")
    lines = [f"{_SPEC_ICON.get(rs, '?')} {spec.get('title', '未命名')} ({_SPEC_LABEL.get(rs, rs)})"]
    if spec.get("design"):
        lines.append("设计：" + spec["design"])
    for i, s in enumerate(spec.get("steps", [])):
        act = s.get("action", "review")
        file = s.get("file", "") or "(无文件)"
        anchor = s.get("anchor", "")
        anchor_s = f" @ {anchor}" if anchor else ""
        lines.append(f"  {i + 1}. [{act}] {file}{anchor_s}")
        c = s.get("content", "")
        if c:
            preview = c if len(c) <= 200 else c[:200] + f"…(共{len(c)}字)"
            lines.append(f"     内容: {preview}")
        rat = s.get("rationale", "")
        if rat:
            lines.append(f"     理由: {rat[:200]}")
    return "\n".join(lines)


# ========== 从 spec 生成 plan ==========

def _step_to_plan_desc(step: dict, idx: int) -> str:
    """把 spec 的结构化 step 压成 plan step 的 description 字符串（给 create_plan 用）。
    plan step 是一维 todoList，但描述里带上 file/action/anchor 让模型施工时知道往哪改。"""
    act = step.get("action", "review")
    file = step.get("file", "") or ""
    anchor = step.get("anchor", "")
    content = step.get("content", "")
    rat = step.get("rationale", "")
    parts = [f"[{act}]"]
    if file:
        parts.append(file)
    if anchor:
        parts.append(f"@ {anchor}")
    head = " ".join(parts)
    # 内容只摘前 150 字进 description（完整内容 spec 文件里有，且可通过 recall_spec 查）
    if content:
        c_preview = content if len(content) <= 150 else content[:150] + "…"
        head += f"：{c_preview}"
    if rat:
        head += f"（{rat[:100]}）"
    return head


def _build_plan_from_spec(workspace, spec: dict) -> dict:
    """把一个 approved spec 转成 plan_tools 的 plan dict（不落盘，由 plan_tools._save_plan 写）。
    复用 plan_tools 的数据结构：id/title/design/steps。"""
    from plan_tools import _gen_plan_id
    return {
        "id": _gen_plan_id(),
        "title": spec.get("title", "未命名计划"),
        "design": (spec.get("design", "") or "") + f"\n\n[由 spec {spec.get('id')} 生成]",
        "steps": [{"description": _step_to_plan_desc(s, i), "status": "pending"}
                   for i, s in enumerate(spec.get("steps", []))],
        "created_at": _now_iso(),
        "updated_at": _now_iso(),
        "created_session": spec.get("created_session"),
        "source_spec": spec.get("id"),
    }


# ========== 工具 ==========

def make_spec_tools(agent) -> list:
    """生成绑定到指定 Agent 的施工方案工具（探索/制定/批阅/施工 共 8 个）。"""

    def create_spec(title: str, steps: list, design: str = "") -> str:
        """新建一个施工方案 spec（draft 态，新 id + 新文件）。
        title: 方案名称；steps: 结构化步骤数组，每项 {file, action, anchor, content, rationale}：
               file=要改的文件路径；action∈create/insert/edit/replace/delete/move/review；
               anchor=定位（行号/函数名/锚串，如 'after _run_hooks(约605行)'）；content=要插入/替换的内容；
               rationale=为什么这么改。也接受纯字符串 step（降级为 review）。design: 可选，整体设计/思路。
        建成后用 commit_spec 提交批阅；用户通过则自动建 plan 开始施工。"""
        if not isinstance(steps, list) or not steps:
            return "[错误] steps 必须是非空数组"
        norm_steps = [_normalize_step(s, i) for i, s in enumerate(steps)]
        spec = {
            "id": _gen_spec_id(),
            "title": (title or "未命名方案").strip() or "未命名方案",
            "design": design or "",
            "steps": norm_steps,
            "review_state": "draft",
            "created_at": _now_iso(),
            "updated_at": _now_iso(),
            "created_session": getattr(agent.session, "name", None),
            "feedback": "",
        }
        _save_spec(agent.session.workspace, spec)
        _set_active_spec(agent, spec)
        _emit_spec(agent)
        return (f"已创建施工方案 {spec['id']}（draft 态，{len(spec['steps'])} 步）：\n"
                + _spec_text(spec)
                + "\n\n用 commit_spec 提交批阅，或 regenerate_spec 据反馈重新生成。")

    def commit_spec(spec_id: str = "") -> str:
        """提交一个 spec 供用户批阅（draft → committed）。spec_id 留空=提交当前活动 spec。
        提交后【阻塞等待用户裁定】——你在 WebUI/CLI 看到 spec 详情后点「通过」或「返工」。
        通过 → 自动建 plan 开始施工；返工 → 返回反馈，你用 regenerate_spec 重新生成。"""
        import threading
        sid = (spec_id or agent.active_spec_id or "").strip()
        if not sid:
            return "[错误] 没有活动 spec 可提交；先 create_spec 或 commit_spec(<spec_id>)"
        spec = _load_spec(agent.session.workspace, sid)
        if not spec:
            return f"[错误] 找不到 spec {sid}"
        if spec.get("review_state") == "approved":
            return f"spec {sid} 已通过，无需再提交"
        spec["review_state"] = "committed"
        _save_spec(agent.session.workspace, spec)
        _set_active_spec(agent, spec)
        # 记录 pending spec 到 extra_state（持久化：程序关了读档后能恢复等待状态）
        agent.session.extra_state["_pending_spec"] = sid
        # 阻塞等待用户裁定（无超时——一直等到用户回应或程序关闭）
        agent._spec_decision_event = threading.Event()
        agent._spec_decision_result = None
        _emit_spec(agent, event_type="spec_pending")
        agent._spec_decision_event.wait()   # 无限等待
        result = agent._spec_decision_result
        agent._spec_decision_event = None
        # 清除 pending 标记
        agent.session.extra_state.pop("_pending_spec", None)
        decision = result.get("decision", "")
        feedback = result.get("feedback", "")
        if decision == "approve":
            spec["review_state"] = "approved"
            _save_spec(agent.session.workspace, spec)
            from plan_tools import _set_active as _plan_set_active, _save_plan, _emit_plan
            plan = _build_plan_from_spec(agent.session.workspace, spec)
            _save_plan(agent.session.workspace, plan)
            _plan_set_active(agent, plan)
            _emit_plan(agent)
            _set_active_spec(agent, spec)
            _emit_spec(agent)
            return (f"✅ 用户已批准 spec {sid}！已生成 plan {plan['id']}（{len(plan['steps'])} 步）并设为活动计划，开始施工。\n"
                    + f"用 update_plan(step, status) 推进进度。")
        else:
            spec["review_state"] = "rejected"
            spec["feedback"] = feedback
            _save_spec(agent.session.workspace, spec)
            _set_active_spec(agent, spec)
            _emit_spec(agent)
            return (f"❌ 用户返工了 spec {sid}." + (f" 反馈：{feedback}" if feedback else "")
                    + "\n请据反馈用 regenerate_spec(spec_id, feedback) 重新生成一版 spec。")

    def regenerate_spec(spec_id: str, feedback: str, title: str = "", steps: list = None,
                        design: str = "") -> str:
        """据用户反馈重新生成一版 spec（新 id；旧 spec 保留为 rejected 供对照）。
        spec_id: 要返工的旧 spec id；feedback: 用户返工反馈（必填，指导新方案）；
        title/steps/design: 留空则从旧 spec 继承（你只改有意见的部分）；steps 传了则整个替换。
        生成的新 spec 是 draft 态，需再 commit_spec 提交批阅。"""
        sid = (spec_id or "").strip()
        if not sid:
            return "[错误] 必须提供要返工的 spec_id"
        if not (feedback or "").strip():
            return "[错误] 必须提供 feedback（指导新方案）"
        old = _load_spec(agent.session.workspace, sid)
        if not old:
            return f"[错误] 找不到旧 spec {sid}"
        if old.get("review_state") != "rejected":
            old["review_state"] = "rejected"
            old["feedback"] = feedback.strip()
            _save_spec(agent.session.workspace, old)
        new_spec = {
            "id": _gen_spec_id(),
            "title": (title or old.get("title", "未命名方案")).strip() or "未命名方案",
            "design": design if design else (old.get("design", "") or ""),
            "steps": ([_normalize_step(s, i) for i, s in enumerate(steps)] if steps is not None
                      else [_normalize_step(s, i) for i, s in enumerate(old.get("steps", []))]),
            "review_state": "draft",
            "created_at": _now_iso(),
            "updated_at": _now_iso(),
            "created_session": getattr(agent.session, "name", None),
            "feedback": "",
            "replaces": sid,
        }
        _save_spec(agent.session.workspace, new_spec)
        _set_active_spec(agent, new_spec)
        _emit_spec(agent)
        return (f"已据反馈重新生成 spec {new_spec['id']}（draft 态，替代被返工的 {sid}）：\n"
                + _spec_text(new_spec)
                + f"\n\n反馈摘要：{feedback.strip()[:200]}"
                + "\n用 commit_spec 提交批阅。")

    def list_specs() -> str:
        """列出本仓库的全部施工方案：spec_id / 标题 / 步数 / 批阅态，标注当前活动 spec。"""
        specs = _list_specs(agent.session.workspace)
        if not specs:
            return "本仓库还没有施工方案。用 create_spec 新建一个。"
        active = agent.active_spec_id
        rows = []
        for s in specs:
            rs = s.get("review_state", "draft")
            steps = s.get("steps", [])
            mark = "   （当前活动）" if s.get("id") == active else ""
            fb = f"  反馈:{s['feedback'][:40]}" if rs == "rejected" and s.get("feedback") else ""
            rows.append(f"{s.get('id', '?')}   {s.get('title', '未命名')}   "
                        f"{_SPEC_ICON.get(rs, '?')}{_SPEC_LABEL.get(rs, rs)}   "
                        f"{len(steps)}步{mark}{fb}")
        return "\n".join(rows) + "\n\n用 recall_spec(spec_id) 查看详情。"

    def recall_spec(spec_id: str) -> str:
        """查看一个 spec 的完整内容（含每步的 file/action/anchor/content/rationale + design + 反馈）。"""
        sid = (spec_id or "").strip()
        if not sid:
            return "[错误] 必须提供 spec_id"
        spec = _load_spec(agent.session.workspace, sid)
        if not spec:
            return f"[错误] 找不到 spec {sid}"
        out = _spec_text(spec)
        if spec.get("feedback"):
            out += f"\n\n返工反馈：{spec['feedback']}"
        if spec.get("replaces"):
            out += f"\n（替代了被返工的 spec {spec['replaces']}）"
        return out

    def explore_subagent(name: str, goal: str, model: str = "") -> str:
        """派一个一次性【探索子 Agent】去读/摸清某个模块或文件，返回发现报告。
        name: 子 Agent 角色（如 'reader'/'arch'）；goal: 探索目标（如 '摸清 src/session.py 的上下文注入点'）。
        在【同一步】里发起多个 explore_subagent 即可并行探索不同模块，各自返回报告，
        汇总后喂给你生成更准的施工方案（create_spec）。
        model: 指定模型，留空=用当前模型。本质是 agent_prompt 的语义化包装（探索专用）。"""
        from multiagent import SubAgent
        import config
        system = (f"你是探索子 Agent「{name}」，专注【只读探索】。目标：{goal}\n"
                  "用 read_file/grep/list_dir/find_function 等只读工具摸清代码结构，"
                  "返回结构化发现报告：关键文件、关键函数/类、注入点/集成点、潜在坑。不要改任何文件。")
        model_name = model or agent.model_name
        if model_name not in config.MODELS:
            model_name = agent.model_name
        _READONLY = {"read_file", "grep", "list_dir", "find_function", "get_tool_detail",
                     "list_tool_logs", "recall", "web_search", "open_url"}
        from tools import Toolbox
        chosen = [t for t in agent.tools if t.name in _READONLY]
        try:
            sub = SubAgent(name, model_name, system, Toolbox(*chosen), on_event=agent.on_event,
                           max_steps=12, token_budget=20000)
            return sub.prompt(f"探索目标：{goal}\n返回结构化发现报告。")
        except Exception as e:
            return f"[探索子 Agent 调用出错] {type(e).__name__}: {e}"

    return [Tool(create_spec), Tool(commit_spec),
            Tool(regenerate_spec), Tool(list_specs), Tool(recall_spec), Tool(explore_subagent)]

def resolve_spec_decision(agent, decision: str, feedback: str = ""):
    """用户对 commit_spec 的阻塞等待做出裁定。由 server.py（WS action）或 chat.py（CLI 命令）调用。
    
    两种场景：
    1. 正常阻塞中：commit_spec 在 worker 线程里 Event.wait() 阻塞 → set event 解除阻塞，
       commit_spec 自己处理 approve（建 plan）/ reject（返回反馈给 Agent）。
    2. 读档恢复：程序重启后从 meta.json 发现 _pending_spec → 直接执行 approve/reject + 喂 agent 新消息
       （因为原始的 agent.run() 已随程序退出而消失）。"""
    ev = getattr(agent, "_spec_decision_event", None)
    if ev:
        # 场景1：正常阻塞中——set event 解除 commit_spec 的阻塞
        agent._spec_decision_result = {"decision": decision, "feedback": (feedback or "").strip()}
        ev.set()
        return
    # 场景2：读档恢复——没有阻塞线程，直接处理
    sid = agent.session.extra_state.pop("_pending_spec", "")
    if not sid:
        sid = getattr(agent, "active_spec_id", "") or ""
    if not sid:
        return
    spec = _load_spec(agent.session.workspace, sid)
    if not spec:
        return
    if decision == "approve":
        spec["review_state"] = "approved"
        _save_spec(agent.session.workspace, spec)
        _set_active_spec(agent, spec)
        from plan_tools import _set_active as _plan_set_active, _save_plan, _emit_plan
        plan = _build_plan_from_spec(agent.session.workspace, spec)
        _save_plan(agent.session.workspace, plan)
        _plan_set_active(agent, plan)
        _emit_plan(agent)
        _emit_spec(agent)
        # 喂 agent 一条消息让它知道 spec 已通过、开始施工
        try:
            from plan_tools import _emit_plan
            msg = (f"[系统] 用户已批准 spec {sid}（读档恢复）。已生成 plan {plan['id']}（{len(plan['steps'])} 步）。"
                   f"请用 update_plan 推进施工。")
            if getattr(agent, "on_event", None):
                agent.on_event({"type": "system", "text": msg})
        except Exception:
            pass
    else:
        spec["review_state"] = "rejected"
        spec["feedback"] = (feedback or "").strip()
        _save_spec(agent.session.workspace, spec)
        _set_active_spec(agent, spec)
        _emit_spec(agent)
        try:
            msg = (f"[系统] 用户返工了 spec {sid}（读档恢复）。" +
                   (f"反馈：{feedback}" if feedback else "") +
                   " 请用 regenerate_spec 重新生成。")
            if getattr(agent, "on_event", None):
                agent.on_event({"type": "system", "text": msg})
        except Exception:
            pass


def check_pending_spec(agent):
    """检查是否有未决的 pending spec（程序重启/读档后恢复等待状态）。
    如果有，重新 emit spec_pending 事件让用户看到待批阅的 spec。
    由 server.py（WS 连接）和 chat.py（session 恢复）调用。"""
    sid = agent.session.extra_state.get("_pending_spec", "")
    if not sid:
        # 也检查活动 spec 是否处于 committed 态（兼容旧数据）
        spec = getattr(agent, "active_spec", None)
        if spec and spec.get("review_state") == "committed":
            sid = spec.get("id", "")
    if sid:
        spec = _load_spec(agent.session.workspace, sid)
        if spec and spec.get("review_state") == "committed":
            _set_active_spec(agent, spec)
            _emit_spec(agent, event_type="spec_pending")
            return True
    return False
