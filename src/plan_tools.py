"""plan_tools.py —— 跨 session 的 repo 级计划/清单工具（类 Claude Code 的 TodoWrite，但持久化为文件）。

计划存到 ~/.agt/repos/<hash>/plans/<plan_id>.json，每个计划一个文件、带稳定 id，
可跨 session 共享：session 的 extra_state 只记一个 plan_id（当前活动计划引用），
另一个 session 用 join_plan(id) 即可加入同一个计划继续推进。

加入计划后，计划的完整信息（标题 / 制定时的设计 / TodoList 及各步状态）每轮被动注入 SYSTEM
（见 agent._plan_system_block / session.messages_for_llm 的 _plan_provider 槽），让 Agent 始终
清楚在干哪一步；exit_plan() 退出后停止注入（文件仍保留）。

内存模型：agent.active_plan（完整 dict，单一事实源）+ agent.plan（steps 的镜像，兼容旧读者）
+ agent.active_plan_id。所有变更工具改 active_plan → _flush（同步镜像 + 原子落盘）→ emit。
"""
from __future__ import annotations

import json
import os
import uuid
from datetime import datetime
from pathlib import Path

from session import repo_plans_dir
from tools import Tool

_PLAN_ICON = {"pending": "☐", "in_progress": "▶", "completed": "✅"}
_PLAN_LABEL = {"pending": "待办", "in_progress": "进行中", "completed": "已完成"}
_VALID_STATUS = ("pending", "in_progress", "completed")


# ========== 文件 I/O ==========

def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _gen_plan_id() -> str:
    """本 repo 内足够唯一的计划 id：p_ + 8 位 hex。文件名即 <plan_id>.json。"""
    return "p_" + uuid.uuid4().hex[:8]


def _plan_path(workspace, plan_id: str) -> Path:
    return repo_plans_dir(workspace) / f"{plan_id}.json"


def _load_plan(workspace, plan_id: str):
    """按 id 读单个计划；不存在 / 损坏返回 None。"""
    p = _plan_path(workspace, plan_id)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def _save_plan(workspace, plan: dict) -> None:
    """原子落盘：写 .tmp 再 os.replace；刷新 updated_at。"""
    plan["updated_at"] = _now_iso()
    p = _plan_path(workspace, plan["id"])
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, p)


def _list_plans(workspace) -> list:
    """列出本 repo 全部计划，按 updated_at 倒序。"""
    out = []
    for f in repo_plans_dir(workspace).glob("p_*.json"):
        try:
            out.append(json.loads(f.read_text(encoding="utf-8")))
        except Exception:
            continue
    out.sort(key=lambda x: x.get("updated_at", ""), reverse=True)
    return out


# ========== 内存 ↔ 落盘 同步 ==========

import threading

# 进程内锁（p_c0eebb19 步 4，2026-11-27）：main 记账与 worker 标进度并发改同一计划文件——
# 此前 update_plan 直接改内存副本再写盘，两边互相覆盖（worker 标的步被 main 的旧内存副本
# 写回 pending）。读文件→改→写全程持锁互斥。
_PLAN_LOCK = threading.Lock()

# ========== 进程级计划仓库 PlanStore（p_c6cde42a，2026-11-28）==========

import copy
import logging
import weakref

_LOG = logging.getLogger("agt")


class PlanStore:
    """进程级 plan_id → 不可变快照的单例仓库（p_c6cde42a，2026-11-28）。

    - 条目 key = 计划文件绝对路径（str(_plan_path(ws, pid).resolve())，多 workspace 共存不串）；
      条目 = {"snapshot": plan dict, "stamp": (st_mtime_ns, st_size)}。
    - 快照视为不可变：改动一律 mutate（深拷贝→改→原子落盘→原子换快照），读方持有的旧快照
      引用不受影响；同一进程内所有读方拿到的是同一份快照对象（get 不拷贝，保身份不变量）。
    - 文件是持久化真源（重启恢复靠它）；仓库是进程内真源；文件戳兜底外部手改/别的进程写。
    - watch/unwatch 登记「谁把它设为活动计划」（weakref——Agent 死了自动掉，不延长生命周期）；
      mutate/put 成功后锁外通知全部订阅 Agent（main/worker、含空闲），各自面板在写盘瞬间刷新。
    """

    NO_PLAN_ERR = "[错误] 还没有活动计划，先用 create_plan 新建或 join_plan 加入"

    def __init__(self):
        self._lock = threading.Lock()
        self._entries = {}      # {key: {"snapshot": dict, "stamp": (mtime_ns, size)}}
        self._watchers = {}     # {key: weakref.WeakSet[agent]}

    # —— 内部 ——

    @staticmethod
    def _key(workspace, pid) -> str:
        return str(_plan_path(workspace, pid).resolve())

    @staticmethod
    def _stamp_of(key: str):
        """文件当前戳（mtime_ns, size）；读不到返回 None。恰好一次 os.stat。"""
        try:
            st = os.stat(key)
            return (st.st_mtime_ns, st.st_size)
        except OSError:
            return None

    def _refresh_locked(self, key: str):
        """（持锁前提）按文件戳校验条目：没动→现有快照；变了/无条目→重读换快照；
        文件没了/损坏→保留现有快照或 None。读文件也在锁内：与 mutate 的读→改→写互斥，
        不会读到半改状态。"""
        e = self._entries.get(key)
        stamp = self._stamp_of(key)
        if e is not None and stamp is not None and e["stamp"] == stamp:
            return e["snapshot"]                                # 文件没动：零读盘
        try:
            fresh = json.loads(Path(key).read_text(encoding="utf-8"))
        except Exception:
            fresh = None
        if fresh is None:
            return e["snapshot"] if e is not None else None     # 文件没了/损坏：保留现有快照
        self._entries[key] = {"snapshot": fresh, "stamp": stamp}
        return fresh

    # —— 读 ——

    def get(self, workspace, pid):
        """按 id 取计划快照（懒加载 + 文件戳校验）。无 id / 文件不存在且无缓存 → None。
        返回的 dict 视为不可变；同进程内读到的是同一份快照对象（不做拷贝，保快照身份）。"""
        if not pid:
            return None
        with self._lock:
            return self._refresh_locked(self._key(workspace, pid))

    # —— 写 ——

    def put(self, workspace, plan: dict):
        """落盘 + 入库 + 通知订阅者（create_plan / 旧存档迁移的写入口）。"""
        key = self._key(workspace, plan["id"])
        _save_plan(workspace, plan)                             # 原子替换（刷新 updated_at）
        with self._lock:
            self._entries[key] = {"snapshot": plan, "stamp": self._stamp_of(key)}
        self._notify(key)                                       # 锁外通知
        return plan

    def mutate(self, workspace, pid, mutator):
        """持锁取最新快照→深拷贝→mutator(copy)（返回 None=成功 / 文案=中止不写盘）→
        原子落盘→换快照（stamp 取写后 stat）→锁外通知订阅者。返回 None=成功 / 错误文案。"""
        if not pid:
            return self.NO_PLAN_ERR
        key = self._key(workspace, pid)
        with self._lock:
            snap = self._refresh_locked(key)
            if snap is None:
                return self.NO_PLAN_ERR
            plan = copy.deepcopy(snap)
            err = mutator(plan)
            if err:
                return err
            _save_plan(workspace, plan)                         # 原子替换（刷新 updated_at）
            self._entries[key] = {"snapshot": plan, "stamp": self._stamp_of(key)}
        self._notify(key)                                       # 锁外通知
        return None

    # —— 订阅与通知 ——

    def watch(self, workspace, pid, agent):
        """登记「agent 把该计划设为活动计划」：写盘变更通知它。weakref 不延长 Agent 生命周期；
        对象不可弱引用（SimpleNamespace 等替身）时 TypeError 兜底跳过（debug 日志），不炸。"""
        if not pid or agent is None:
            return
        key = self._key(workspace, pid)
        with self._lock:
            subs = self._watchers.setdefault(key, weakref.WeakSet())
            try:
                subs.add(agent)
            except TypeError:
                _LOG.debug("plan watch 跳过（对象不可弱引用）：%r", agent)

    def unwatch(self, agent):
        """从所有计划条目移除该 Agent 的订阅（exit_plan / _clear_active / 换计划前调用）。"""
        if agent is None:
            return
        with self._lock:
            for key in list(self._watchers.keys()):
                subs = self._watchers[key]
                try:
                    subs.discard(agent)
                except TypeError:
                    pass
                if len(subs) == 0:
                    self._watchers.pop(key, None)

    def _notify(self, key):
        """写盘成功后通知该计划全部订阅 Agent：逐个 _emit_plan（各自面板在写盘瞬间刷新）。
        单个异常吞并留 debug 日志——面板刷新失败不能影响写盘结果。"""
        with self._lock:
            subs = self._watchers.get(key)
            targets = list(subs) if subs else []
        for ag in targets:
            if ag is None:
                continue
            try:
                _emit_plan(ag)
            except Exception as e:
                _LOG.debug("plan 变更通知 emit 失败（忽略）：%s", e)

    def _reset(self):
        """清空条目与订阅（测试用）。"""
        with self._lock:
            self._entries.clear()
            self._watchers.clear()


PLAN_STORE = PlanStore()   # 进程级单例


def _mutate_active_plan(agent, mutator):
    """活动计划的读文件→改→写（p_c0eebb19 步 4）：持 _PLAN_LOCK，按 active_plan_id 重新读盘
    （读不到再退回内存副本），跑 mutator(plan)（返回 None=成功 / 错误文案=中止不写盘），
    写盘并把内存 active_plan 换成刚读到的 dict。内存副本落后于文件时不会用旧状态覆盖文件里
    其它步骤的最新状态（main 记账与 worker 标进度互不踩）。"""
    pid = getattr(agent, "active_plan_id", None)
    ws = getattr(getattr(agent, "session", None), "workspace", None)
    with _PLAN_LOCK:
        fresh = _load_plan(ws, pid) if pid else None
        plan = fresh if fresh is not None else getattr(agent, "active_plan", None)
        if not plan:
            return "[错误] 还没有活动计划，先用 create_plan 新建或 join_plan 加入"
        err = mutator(plan)
        if err:
            return err
        _save_plan(ws, plan)
        _set_active(agent, plan)   # 内存换成刚读到的 dict（镜像 agent.plan 一并刷新）
        _emit_plan(agent)
        return None


def _plan_sig(p):
    """计划内容签名（面板可见部分：id/标题/各步状态+描述）——视图刷新时判断「内容真变了没」。"""
    if not p:
        return None
    return (p.get("id"), p.get("title"),
            tuple((s.get("status"), s.get("description")) for s in (p.get("steps") or [])))


def view_active_plan(agent):
    """读侧视图校验（2026-11-28·用户问诊「main 看到的还是内存里自己那份」）：计划文件是唯一
    事实源，agent.active_plan 只是缓存——另一个 Agent（如施工 worker）update_plan 写盘后，
    本 Agent 的 SYSTEM 进度块 / 计划面板若仍读内存副本就永远是旧进度。文件比上次看到的新
    （mtime/size 变了）就重读并替换内存视图（含 agent.plan 镜像），内容真变了再 emit 给 UI。
    每次调用一次 os.stat（微秒级），没变直接返回——放在每步装配路径上无感。"""
    cur = getattr(agent, "active_plan", None)
    pid = getattr(agent, "active_plan_id", None)
    if not pid:
        return cur
    ws = getattr(getattr(agent, "session", None), "workspace", None)
    try:
        _st = _plan_path(ws, pid).stat()
        stamp = (_st.st_mtime_ns, _st.st_size)
    except Exception:
        return cur                      # 文件没了/路径不可用：保留现有视图
    if cur is not None and getattr(agent, "_plan_view_stamp", None) == stamp:
        return cur                      # 文件没动：零开销
    fresh = _load_plan(ws, pid)
    if fresh is None:
        return cur                      # 读到损坏内容（写端是原子替换，正常不会出现）：保留现有视图
    try:
        agent._plan_view_stamp = stamp
    except Exception:
        pass
    if cur is None:
        _set_active(agent, fresh)
        return fresh
    if _plan_sig(cur) == _plan_sig(fresh):
        agent.active_plan = fresh       # 内容没变（如仅 updated_at）：只换引用，不打扰 UI
        return fresh
    _set_active(agent, fresh)           # 内容变了（多半是别的 Agent 标了进度）：换视图 + 刷镜像
    _emit_plan(agent)                   # + 推 UI（面板实时跟上）
    return fresh


def _sync_legacy(agent):
    """把 active_plan['steps'] 镜像到 agent.plan（供 wiki/commands/web 等旧读者）。"""
    if getattr(agent, "active_plan", None):
        agent.plan = [dict(s) for s in agent.active_plan.get("steps", [])]
    else:
        agent.plan = []


def _set_active(agent, plan: dict):
    """把一个计划 dict 设为当前活动计划（同步 id / 镜像）。"""
    agent.active_plan = plan
    agent.active_plan_id = plan.get("id")
    _sync_legacy(agent)


def _clear_active(agent):
    """清空活动计划（/reset、新 session、exit_plan 调用）。文件不动。"""
    agent.active_plan = None
    agent.active_plan_id = None
    agent.plan = []


def _flush(agent):
    """把内存里的活动计划同步镜像并原子落盘。"""
    if getattr(agent, "active_plan", None):
        _sync_legacy(agent)  # active_plan['steps'] 可能被工具就地改过 → 刷到 agent.plan
        _save_plan(agent.session.workspace, agent.active_plan)


def restore_active_plan(agent, state: dict) -> None:
    """从 session 存档恢复活动计划：优先按 plan_id 从文件读回；
    旧存档只有 plan 列表（无 plan_id）则迁移成计划文件。供 agent.restore_runtime_state 调用。"""
    if not state:
        _clear_active(agent)
        return
    plan_id = state.get("plan_id")
    if plan_id:
        plan = _load_plan(agent.session.workspace, plan_id)
        if plan:
            _set_active(agent, plan)
        else:
            _clear_active(agent)  # 文件已丢失：视为无活动计划
        return
    legacy = state.get("plan")  # 旧格式：内联的 steps 列表
    if legacy:
        plan = {
            "id": _gen_plan_id(),
            "title": "迁移的计划",
            "design": "",
            "steps": [dict(s) for s in legacy],
            "created_at": _now_iso(),
            "updated_at": _now_iso(),
            "created_session": getattr(agent.session, "name", None),
        }
        _save_plan(agent.session.workspace, plan)
        _set_active(agent, plan)
    else:
        _clear_active(agent)


def clear_active_plan(agent) -> None:
    """清空活动计划（/reset、新 session 调用）。文件保留，可再次 join。"""
    _clear_active(agent)


# ========== 渲染 ==========

def _parse_maybe_json(s: str):
    """尝试把字符串解析成对象：先标准 JSON，再 Python 字面量（单引号 dict，模型常误输出这种）。
    成功返回解析后的对象，失败返回 None。"""
    try:
        return json.loads(s)
    except Exception:
        pass
    try:
        import ast
        return ast.literal_eval(s)
    except Exception:
        return None


def _step_desc(s) -> str:
    """从 create_plan/add_step 的一个 step 项里取出描述纯文本。
    模型可能传：纯字符串 / dict(含 description|text|title|content 等) / JSON 或 Python-literal 字符串
    （后者如 "{'step':1,'description':'...','status':'pending'}"——模型把整坨结构塞进了 description）。"""
    if isinstance(s, str):
        st = s.strip()
        if st[:1] in ("{", "["):
            parsed = _parse_maybe_json(st)
            if parsed is not None:
                d = _step_desc(parsed)
                if d and d != st:
                    return d
        return st
    if isinstance(s, dict):
        for k in ("description", "desc", "text", "title", "content", "task", "name"):
            v = s.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()
        for v in s.values():            # 兜底：第一个非空字符串值
            if isinstance(v, str) and v.strip():
                return v.strip()
        return json.dumps(s, ensure_ascii=False)
    return str(s).strip()


def _plan_text(agent) -> str:
    """计划 steps 的纯文本视图（工具返回值用）。"""
    if not agent.plan:
        return "(空计划)"
    return "\n".join(f"{_PLAN_ICON.get(s.get('status'), '?')} {i + 1}. {s.get('description', '')}"
                     for i, s in enumerate(agent.plan))


def _format_plan_block(agent) -> str:
    """活动计划的 SYSTEM 注入块。无活动计划 / 空步骤返回 ''（session 不注入）。
    先走 view_active_plan 读侧校验（2026-11-28）：别的 Agent 标的进度实时可见。"""
    p = view_active_plan(agent)
    if not p or not p.get("steps"):
        return ""
    steps = p["steps"]
    total = len(steps)
    done = sum(1 for s in steps if s.get("status") == "completed")
    title = p.get("title", "")
    design = (p.get("design") or "").strip()
    lines = [f"【当前计划】{p.get('id', '')}" + (f" · {title}" if title else "")]
    # 施工委托提示（用户提案 2026-10-15；2026-10-22 标记真源迁至文件级 .agent/construction_dispatch.json
    # ——引擎硬编码退役，由 after_tool 钩子工作流 construction_dispatch 写入）：已委托 → 主 Agent 只需等汇报+记账
    try:
        _ws = getattr(getattr(agent, "session", None), "workspace", None)
        _mf = (Path(_ws) / ".agent" / "construction_dispatch.json") if _ws else Path(".agent/construction_dispatch.json")
        _d = json.loads(_mf.read_text(encoding="utf-8")) if _mf.exists() else {}
        if _d.get("agent_id") and str(_d.get("plan_id") or "") == str(p.get("id") or ""):
            lines.append(f"施工：已委托 {_d.get('agent_id')}（施工细节在其 session；完成会自动汇报。"
                         f"进度随其标步实时同步——收到汇报后验收即可，不必重复记账）")
    except Exception:
        pass
    lines.append("设计：" + (design or "（无）"))
    lines.append(f"进度（共 {total} 步，已完成 {done}）：")
    for i, s in enumerate(steps):
        st = s.get("status")
        lines.append(f"  {_PLAN_ICON.get(st, '?')} {i + 1}. {s.get('description', '')} ({_PLAN_LABEL.get(st, '')})")
    lines.append("推进时用 update_plan 更新状态、add_step 追加步骤、edit_plan 改标题/设计、exit_plan 退出。")
    return "\n".join(lines)


def _emit_plan(agent):
    if getattr(agent, "on_event", None):
        try:
            agent.on_event({"type": "plan", "plan": [dict(s) for s in agent.plan],
                            "plan_id": agent.active_plan_id,
                            "plan_title": (agent.active_plan or {}).get("title", "")})
        except Exception:
            pass


# ========== 工具 ==========

def make_plan_tools(agent) -> list:
    """生成绑定到指定 Agent 的计划工具（创建/导航/查看/修改 共 7 个）。"""

    def create_plan(title: str, steps: list, design: str = "", worker_id: str = "") -> str:
        """新建一个计划（新 id + 新文件，旧计划保留可被 list/join）并设为当前活动计划。
        title: 计划名称；steps: 步骤描述字符串数组；design: 可选，制定时的设计/目标/背景。
        worker_id: 可选，指派特定 sub-agent 实例执行本计划（如 worker-p_1b7df7e7——上一阶段的
        施工 worker，可从团队看板/list_team 查到）。指派后引擎派发复用该实例（带它上一阶段的
        session 记忆续接施工线）；留空=默认新开 worker-<plan_id>。"""
        if not isinstance(steps, list) or not steps:
            return "[错误] steps 必须是非空字符串数组"
        wid = (worker_id or "").strip()
        plan = {
            "id": _gen_plan_id(),
            "title": (title or "未命名计划").strip() or "未命名计划",
            "design": design or "",
            "steps": [{"description": _step_desc(s), "status": "pending"} for s in steps],
            "created_at": _now_iso(),
            "updated_at": _now_iso(),
            "created_session": getattr(agent.session, "name", None),
        }
        if wid:
            plan["worker_id"] = wid
        _save_plan(agent.session.workspace, plan)
        _set_active(agent, plan)
        _emit_plan(agent)
        return (f"已创建计划 {plan['id']}（{len(plan['steps'])} 步）：\n" + _plan_text(agent)
                + (f"\n\n👷 已指派 {wid} 执行（复用该实例续接施工线）。" if wid else ""))

    def join_plan(plan_id: str) -> str:
        """加入一个已存在的计划（按 plan_id 从本仓库加载），设为当前活动计划，每轮注入 SYSTEM。
        plan_id 可先用 list_plans() 查到；加入后即可继续推进同一计划（跨 session 共享）。"""
        pid = (plan_id or "").strip()
        if not pid:
            return "[错误] plan_id 不能为空；先用 list_plans() 查看可用计划"
        plan = _load_plan(agent.session.workspace, pid)
        if not plan:
            return f"[错误] 找不到计划 {pid}；用 list_plans() 查看可用计划"
        _set_active(agent, plan)
        _emit_plan(agent)
        return f"已加入计划 {pid}（当前 {len(plan.get('steps', []))} 步）：\n" + _plan_text(agent)

    def exit_plan() -> str:
        """退出当前活动计划：停止每轮 SYSTEM 注入、清空计划面板。计划文件仍保留，可再次 join。"""
        if not agent.active_plan_id:
            return "当前没有活动计划"
        pid = agent.active_plan_id
        _clear_active(agent)
        _emit_plan(agent)
        aid = str(getattr(agent, "agent_id", "") or "").strip()
        by = f"（由 {aid}）" if aid else ""
        return f"已退出计划 {pid}{by}（文件保留，可用 join_plan 重新加入）"

    def list_plans() -> str:
        """列出本仓库的全部计划：plan_id / 标题 / 完成进度，并标注当前活动计划。"""
        plans = _list_plans(agent.session.workspace)
        if not plans:
            return "本仓库还没有计划。用 create_plan 新建一个。"
        active = agent.active_plan_id
        rows = []
        for p in plans:
            steps = p.get("steps", [])
            done = sum(1 for s in steps if s.get("status") == "completed")
            mark = "   （当前活动）" if p.get("id") == active else ""
            rows.append(f"{p.get('id', '?')}   {p.get('title', '未命名')}   progress:{done}/{len(steps)}{mark}")
        return "\n".join(rows) + "\n\n用 join_plan(plan_id) 加入某个计划。"

    def update_plan(step: int, status: str = "", description: str = "") -> str:
        """更新当前计划某一步：status∈pending/in_progress/completed 标记进度；description 非空则改写该步描述。至少给一个。"""
        if not getattr(agent, "active_plan", None) and not getattr(agent, "active_plan_id", None):
            return "[错误] 还没有活动计划，先用 create_plan 新建或 join_plan 加入"
        try:
            step_i = int(step)
        except (TypeError, ValueError):
            return f"[错误] step 需为整数序号，收到 {step!r}"

        def mut(plan):
            steps = plan.get("steps") or []
            if not (1 <= step_i <= len(steps)):
                return f"[错误] step 越界（共 {len(steps)} 步）"
            if not status and not description:
                return "[错误] 至少提供 status 或 description 之一"
            if status and status not in _VALID_STATUS:
                return "[错误] status 必须是 pending / in_progress / completed"
            if status:
                steps[step_i - 1]["status"] = status
            if description:
                steps[step_i - 1]["description"] = _step_desc(description) or str(description)
            return None

        # 读文件→改→写（进程内锁，p_c0eebb19 步 4）：内存副本落后于文件时不覆盖其它步骤状态
        err = _mutate_active_plan(agent, mut)
        if err:
            return err
        return _plan_text(agent)

    def add_step(description: str) -> str:
        """给当前活动计划追加一步（状态 pending）。"""
        desc = _step_desc(description)

        def mut(plan):
            if not desc:
                return "[错误] description 不能为空"
            plan.setdefault("steps", []).append({"description": desc, "status": "pending"})
            return None

        err = _mutate_active_plan(agent, mut)   # 同一把锁（p_c0eebb19 步 4）
        if err:
            return err
        return f"已追加第 {len(agent.active_plan['steps'])} 步：\n" + _plan_text(agent)

    def edit_plan(title: str = "", design: str = "") -> str:
        """改写当前活动计划的标题（title）和/或设计（design）；至少给一个。"""
        def mut(plan):
            if not title and not design:
                return "[错误] 至少提供 title 或 design 之一"
            if title:
                t = str(title).strip()
                if t:
                    plan["title"] = t
            if design:
                plan["design"] = str(design)
            return None

        err = _mutate_active_plan(agent, mut)   # 同一把锁（p_c0eebb19 步 4）
        if err:
            return err
        return (f"计划 {agent.active_plan_id} 已更新："
                f"标题={agent.active_plan.get('title', '')}；设计长度={len(agent.active_plan.get('design', ''))}")

    return [Tool(create_plan), Tool(update_plan), Tool(add_step), Tool(edit_plan),
            Tool(join_plan), Tool(list_plans), Tool(exit_plan)]
