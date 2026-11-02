"""project_tools.py —— 项目组工具（施工牌 p_2d4f784f：看板创建项目 + coordinator 自动巡检推进）。

项目 = 一组成员实例 + 一份计划文档 + 一个自动巡检定时任务：
  - 数据真源：.agent/projects.json（repo 级，随 git 走）——主 Agent / coordinator / 前端看板三方共用；
  - 计划文档：.agent/projects/<id>/okr.md——coordinator 用 read/write/edit OKR 文档（Objective + Key Results）——coordinator 与用户商议维护；
  - 巡检任务：创建项目时自动 add_schedule（每 5 分钟 code 型）——全员 idle 且距上次派活
    >15 分钟 → 产出唤醒消息（注入主 Agent → 转派 coordinator）；非全 idle / 探测失败 → 静默。

成员双源解析（kind: remote|local）：
  remote = remote_tools.REMOTE_SERVERS 里的 agt 实例（忙闲 POST /api/status）；
  local  = 主实例 registry 里的子 Agent（忙闲看 entry.status=="running"）。

改完本文件用 /reload tools 热加载。schedule 生命周期绑项目：delete_project 自动 cancel。
"""
from pathlib import Path
import json as _json
import threading as _threading

_WORKSPACE = Path.cwd()
_LOCK = _threading.RLock()          # projects.json 读改写互斥（前端/工具/巡检并发）

PROJECTS_FILE = ".agent/projects.json"
PROJECTS_DIR = ".agent/projects"
PATROL_INTERVAL = 300               # 巡检周期：每 5 分钟
PATROL_COOLDOWN = 15 * 60           # 距上次派活 <15 分钟内不再唤醒（防重复派活风暴）


# ---------------------------------------------------------------- 基础读写

def _projects_path() -> Path:
    p = Path(PROJECTS_FILE)
    return p if p.is_absolute() else _WORKSPACE / p


def _load_projects() -> dict:
    p = _projects_path()
    if not p.exists():
        return {"projects": []}
    try:
        data = _json.loads(p.read_text(encoding="utf-8")) or {}
    except Exception:
        return {"projects": []}
    if not isinstance(data.get("projects"), list):
        data["projects"] = []
    return data


def _save_projects(data: dict) -> None:
    p = _projects_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(_json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _get_project(data: dict, pid: str) -> dict:
    for prj in data["projects"]:
        if prj.get("id") == pid:
            return prj
    raise ValueError(f"项目「{pid}」不存在（list_projects 看现有项目）")


# ---------------------------------------------------------------- 成员解析 / 忙闲探测

def _runtime_agent():
    """主 Agent 挂点（agent_config._RUNTIME_AGENT）——registry 双源解析用；无则本地源不可用。"""
    try:
        import agent_config
        return agent_config._RUNTIME_AGENT
    except Exception:
        return None


def _local_candidates() -> list:
    a = _runtime_agent()
    reg = getattr(a, "registry", None)
    if not reg:
        return []
    with reg._lock:
        return [{"id": e.agent_id, "kind": "local", "url": "",
                 "label": f"{e.name}（本地子 Agent · {e.role or 'sub'}）"}
                for e in reg._agents.values() if e.agent_id not in ("_main_", "main")]


def _remote_candidates() -> list:
    try:
        import remote_tools as rt
    except Exception:
        return []
    out = []
    with rt._LOCK:
        for sid, it in list(rt.REMOTE_SERVERS.items()):
            out.append({"id": sid, "kind": "remote", "url": it.get("url", ""),
                        "label": f"{sid}（remote · {it.get('status', '?')} · {it.get('url', '')}）"})
    return out


def resolve_member(member_id: str) -> dict:
    """member_id → {id, kind, url}：先查 remote 表（精确），再查 registry。都查不到抛 ValueError 带候选。"""
    mid = str(member_id or "").strip()
    for c in _remote_candidates():
        if c["id"] == mid:
            return {"id": c["id"], "kind": "remote", "url": c["url"]}
    for c in _local_candidates():
        if c["id"] == mid:
            return {"id": c["id"], "kind": "local", "url": ""}
    cand = [c["id"] for c in (_remote_candidates() + _local_candidates())]
    raise ValueError(f"成员「{member_id}」不在 remotes/registry 双源里。可选：{', '.join(cand) or '（无）'}"
                     "——remote 实例先 remote_connect；本地子 Agent 用其 agent_id")


def probe_member(member: dict, timeout: float = 4.0) -> dict:
    """忙闲探测：remote → POST /api/status（busy/session_turns）；local → registry entry.status。
    返回 {busy: bool|None, turns: int|None, online: bool}——探测失败 busy=None（不假设）。"""
    if member.get("kind") == "remote":
        import urllib.request
        try:
            url = str(member.get("url", "")).rstrip("/") + "/api/status"
            req = urllib.request.Request(url, data=_json.dumps({}).encode(), method="POST",
                                         headers={"Content-Type": "application/json"})
            d = _json.loads(urllib.request.urlopen(req, timeout=timeout).read().decode("utf-8"))
            if not d.get("ready"):
                return {"busy": None, "turns": None, "online": False}
            return {"busy": bool(d.get("busy")), "turns": d.get("session_turns"), "online": True}
        except Exception:
            return {"busy": None, "turns": None, "online": False}
    # local：registry
    a = _runtime_agent()
    reg = getattr(a, "registry", None)
    e = reg.lookup(member.get("id")) if reg else None
    if e is None:
        return {"busy": None, "turns": None, "online": False}
    return {"busy": (getattr(e, "status", "") == "running"), "turns": None, "online": True}


# ---------------------------------------------------------------- 巡检（步骤 4：schedule code 体）

def _parse_last_dispatch(plan_text: str) -> float:
    """从 okr.md 提取 last_dispatch 时间戳（行格式：last_dispatch: 2026-11-19T10:00:00）。
    无该行/解析失败 → 0.0（=从未派活，立即可唤醒）。"""
    from datetime import datetime
    for line in plan_text.splitlines():
        s = line.strip()
        if s.startswith("last_dispatch:"):
            raw = s.split(":", 1)[1].strip()
            try:
                return datetime.fromisoformat(raw).timestamp()
            except Exception:
                return 0.0
    return 0.0


def patrol_once() -> str:
    """跑一轮全员 idle 巡检（工具形态，供手动触发/调试；schedule code 体走同款逻辑）。
    返回唤醒消息（无项目可唤醒时返回说明文本）。"""
    now = __import__("time").time()
    wake = []
    for prj in _load_projects().get("projects", []):
        members = prj.get("members") or []
        if not members:
            continue
        all_idle = True
        for m in members:
            st = probe_member(m)
            if st["busy"] is not None and not st["busy"]:
                continue          # 空闲，继续看下一个
            all_idle = False      # 忙/探测失败（None）→ 不唤醒
            break
        if not all_idle:
            continue
        plan_file = _WORKSPACE / str(prj.get("okr_file") or "")
        last = 0.0
        try:
            if plan_file.exists():
                last = _parse_last_dispatch(plan_file.read_text(encoding="utf-8"))
        except Exception:
            pass
        if last and (now - last) < PATROL_COOLDOWN:
            continue              # 15 分钟内刚派过活，静默
        wake.append((f"🔔 项目《{prj.get('name')}》（{prj.get('id')}）全员空闲"
                     + ("" if not last else "，距上次派活已超 15 分钟")
                     + "——请巡检推进：read " + str(prj.get("okr_file"))
                     + " → agent_query_events / remote_ask 收各成员进展 → 对照 OKR 派活或推进下一步"
                     + "；派活后记得在 okr.md 更新 last_dispatch 与「进行中」段。"))
    return "\n\n".join(wake)


def _patrol_code_src() -> str:
    """生成 add_schedule 的 code 体（与 patrol_once 同逻辑的独立形态——schedule exec 环境
    只注入 agent/result 两个全局量，不 import 本模块，避免热加载后旧 code 悬空）。"""
    return '''import json, time, urllib.request
from pathlib import Path
from datetime import datetime
result = ""
try:
    root = Path.cwd()
    pf = root / ".agent" / "projects.json"
    if pf.exists():
        data = json.loads(pf.read_text(encoding="utf-8")) or {}
        reg = getattr(agent, "registry", None)
        now = time.time()
        wake = []
        for prj in (data.get("projects") or []):
            members = prj.get("members") or []
            if not members:
                continue
            all_idle = True
            for m in members:
                try:
                    if m.get("kind") == "remote":
                        req = urllib.request.Request(str(m.get("url", "")).rstrip("/") + "/api/status",
                                                     data=b"{}", method="POST",
                                                     headers={"Content-Type": "application/json"})
                        st = json.loads(urllib.request.urlopen(req, timeout=4).read().decode("utf-8"))
                        if st.get("ready") and not st.get("busy"):
                            continue
                    else:
                        e = reg.lookup(m.get("id")) if reg else None
                        if e is not None and getattr(e, "status", "") != "running":
                            continue
                except Exception:
                    pass
                all_idle = False
                break
            if not all_idle:
                continue
            plan = root / str(prj.get("okr_file") or "")
            last = 0.0
            try:
                if plan.exists():
                    for line in plan.read_text(encoding="utf-8").splitlines():
                        s = line.strip()
                        if s.startswith("last_dispatch:"):
                            try:
                                last = datetime.fromisoformat(s.split(":", 1)[1].strip()).timestamp()
                            except Exception:
                                last = 0.0
                            break
            except Exception:
                pass
            if last and (now - last) < 900:
                continue
            wake.append("🔔 项目《%s》（%s）全员空闲%s——请巡检推进：read %s → agent_query_events / remote_ask "
                        "收各成员进展 → 对照 OKR 派活或推进下一步；派活后记得在 okr.md 更新 last_dispatch 与「进行中」段。"
                        % (prj.get("name"), prj.get("id"),
                           "" if not last else "，距上次派活已超 15 分钟", prj.get("okr_file")))
        result = "\\n\\n".join(wake)
except Exception:
    result = ""
'''


def _ensure_patrol_schedule(prj: dict, enabled: bool = True) -> str:
    """为项目创建/对齐巡检 schedule（每 5 分钟 code 型；同名覆盖幂等）。
    返回 schedule 名；enabled=False 或拿不到主 Agent scheduler 时返回 ''。"""
    name = f"project-{prj['id']}-patrol"
    a = _runtime_agent()
    sch = getattr(a, "scheduler", None) if a is not None else None
    if sch is None:
        return ""
    if not enabled:
        return name
    try:
        sch.add_interval(name, PATROL_INTERVAL, message="", code=_patrol_code_src(),
                         repeat=True, mode="idle")
    except Exception:
        return ""
    return name


def _cancel_patrol_schedule(pid: str) -> None:
    a = _runtime_agent()
    sch = getattr(a, "scheduler", None) if a is not None else None
    if sch is not None:
        try:
            sch.cancel(f"project-{pid}-patrol")
        except Exception:
            pass


# ---------------------------------------------------------------- OKR 文档骨架

_OKR_TEMPLATE = """# {name} — OKR

- id：{pid}
- 创建：{ts}
- 成员：{members}
- 巡检：每 {interval_min} 分钟（schedule：project-{pid}-patrol；全员空闲且距上次派活 >15 分钟时唤醒 coordinator）

## Objective（目标）

（一句话说清这个项目最终要做成什么——与用户商定后填写；O 不轻易改，改前先和用户确认）

## Key Results（关键结果）

（2~5 条可验收的关键结果；完成勾选 [x]——coordinator 派活的依据就是这些 KR）

- [ ] KR1（示例：可验收的具体结果）
- [ ] KR2

## 进行中

（coordinator 维护：哪个 KR 正在推进、谁在干什么 + 何时派的。派活前若此段非空且成员 idle——先问询再重派，防重复派活）

（空）

## 进展记录

（各成员回报的进展按时间追加；KR 达成时在此记一笔并勾选上方复选框）

last_dispatch: 1970-01-01T00:00:00
"""


def _write_okr_skeleton(prj: dict) -> Path:
    d = _WORKSPACE / PROJECTS_DIR / prj["id"]
    d.mkdir(parents=True, exist_ok=True)
    f = d / "okr.md"
    if not f.exists():
        members = ", ".join(f"{m['id']}({m['kind']})" for m in prj.get("members") or [])
        f.write_text(_OKR_TEMPLATE.format(name=prj["name"], pid=prj["id"], ts=prj["created_at"],
                                          members=members or "（待添加）",
                                          interval_min=PATROL_INTERVAL // 60), encoding="utf-8")
    return f


# ---------------------------------------------------------------- 工具

def create_project(name: str, member_ids: str, desc: str = "", patrol: bool = True) -> str:
    """创建项目组：写 projects.json + 建 okr.md 骨架 + 自动挂每 5 分钟全员 idle 巡检 schedule。
    member_ids：逗号分隔的成员 id（remote 实例的 server_id ∪ 本地子 Agent 的 agent_id）。
    返回项目卡摘要（id/成员/巡检任务名）。"""
    name = str(name or "").strip()
    if not name:
        raise ValueError("项目名不能为空")
    # 宽容归一（2026-11-01 e2e 实锤）：前端/LLM 可能传 JSON 数组形态——str() 化数组
    # 会得到 "['coder']" 脏串；list/tuple 直接逐项取，字符串仍走逗号分隔解析
    if isinstance(member_ids, (list, tuple)):
        ids = [str(x).strip() for x in member_ids if str(x).strip()]
    else:
        ids = [x.strip() for x in str(member_ids or "").replace("，", ",").split(",") if x.strip()]
    if not ids:
        raise ValueError("member_ids 不能为空（逗号分隔；list_projects/remote_list/team 看可选成员）")
    from datetime import datetime
    with _LOCK:
        data = _load_projects()
        if any(p.get("name") == name for p in data["projects"]):
            raise ValueError(f"项目名「{name}」已存在（项目名唯一；换名或复用现有项目）")
        import uuid
        pid = f"{uuid.uuid4().hex[:6]}-{len(data['projects']) + 1}"
        members = [resolve_member(mid) for mid in ids]
        prj = {"id": pid, "name": name, "desc": str(desc or ""),
               "members": members,
               "okr_file": f"{PROJECTS_DIR}/{pid}/okr.md",
               "created_at": datetime.now().isoformat(timespec="seconds"),
               "patrol": bool(patrol),
               "schedule": ""}
        data["projects"].append(prj)
        _save_projects(data)
    prj["schedule"] = _ensure_patrol_schedule(prj, enabled=bool(patrol))
    okr_file = _write_okr_skeleton(prj)
    lines = [f"✅ 项目「{name}」已创建（id: {pid}）",
             f"  成员：{', '.join(m['id'] + '·' + m['kind'] for m in members)}",
             f"  OKR：{okr_file}（Objective/KR 骨架已就位——在 /agents/coordinator 页面与协调者商议填写）",
             f"  巡检：{'✅ ' + prj['schedule'] + '（每 5 分钟；全员空闲且距上次派活>15分钟时唤醒 coordinator）' if prj['schedule'] else '⚠️ 未挂（patrol=false 或 scheduler 不可用——可手动 add_schedule）'}"]
    if desc:
        lines.append(f"  说明：{desc}")
    return "\n".join(lines)


def list_projects(project_id: str = "", probe: bool = True) -> str:
    """列出项目组（含成员忙闲实时探测：remote POST /api/status，local 查 registry）。
    project_id 传单个项目的 id 看详情；probe=false 跳过探测（快）。"""
    with _LOCK:
        data = _load_projects()
    projects = data["projects"]
    if project_id:
        projects = [p for p in projects if p.get("id") == project_id]
        if not projects:
            raise ValueError(f"项目「{project_id}」不存在")
    if not projects:
        return "（暂无项目组——看板「项目组」区创建，或 create_project(name, member_ids)）"
    out = [f"📁 项目组（{len(projects)}）："]
    for prj in projects:
        out.append(f"  {prj.get('name')}（id: {prj.get('id')}）"
                   + (f" · {prj['desc']}" if prj.get("desc") else ""))
        out.append(f"    OKR：{prj.get('okr_file')} · 创建：{prj.get('created_at')}"
                   + f" · 巡检：{'✅ ' + prj['schedule'] if prj.get('schedule') else '❌ 未挂'}")
        for m in prj.get("members") or []:
            mark = "  ·"
            if probe:
                st = probe_member(m)
                if st["busy"] is True:
                    mark = "  ⏳ 忙"
                elif st["busy"] is False:
                    mark = "  💤 空闲"
                else:
                    mark = "  ❔ 不可达/未知"
            out.append(f"    {mark} {m['id']}（{m.get('kind')}）")
    return "\n".join(out)


def add_project_member(project_id: str, member_id: str) -> str:
    """往项目里加成员（双源解析 remotes/registry；已在则幂等返回）。"""
    member = resolve_member(member_id)
    with _LOCK:
        data = _load_projects()
        prj = _get_project(data, project_id)
        if any(m["id"] == member["id"] for m in prj["members"]):
            return f"成员「{member_id}」已在项目「{prj['name']}」里（幂等，未变动）"
        prj["members"].append(member)
        _save_projects(data)
    _write_okr_skeleton(prj)
    return (f"✅ 成员「{member['id']}」（{member['kind']}）已加入项目「{prj['name']}」"
            f"（现 {len(prj['members'])} 人）")


def remove_project_member(project_id: str, member_id: str) -> str:
    """从项目里移除成员（不动实例本身，只摘出项目组）。"""
    with _LOCK:
        data = _load_projects()
        prj = _get_project(data, project_id)
        before = len(prj["members"])
        prj["members"] = [m for m in prj["members"] if m["id"] != member_id]
        if len(prj["members"]) == before:
            return f"成员「{member_id}」不在项目「{prj['name']}」里（未变动）"
        _save_projects(data)
    return f"✅ 成员「{member_id}」已移出项目「{prj['name']}」（现 {len(prj['members'])} 人）"


def delete_project(project_id: str) -> str:
    """删除项目组：cancel 巡检 schedule + 移除 projects.json 条目 + 计划目录挪入
    .agent/projects/.trash/<id>-<ts>/（档案保留，防误删不可恢复）。"""
    with _LOCK:
        data = _load_projects()
        prj = _get_project(data, project_id)
        data["projects"] = [p for p in data["projects"] if p.get("id") != project_id]
        _save_projects(data)
    _cancel_patrol_schedule(project_id)
    src = _WORKSPACE / PROJECTS_DIR / project_id
    trashed = ""
    if src.exists():
        from datetime import datetime
        trash = _WORKSPACE / PROJECTS_DIR / ".trash"
        trash.mkdir(parents=True, exist_ok=True)
        dst = trash / f"{project_id}-{datetime.now().strftime('%Y%m%d%H%M%S')}"
        try:
            src.rename(dst)
            trashed = f"计划目录已挪 {dst}"
        except Exception as e:
            trashed = f"⚠️ 计划目录挪移失败（{type(e).__name__}: {e}），留在原地 {src}"
    return f"🗑️ 项目「{prj.get('name')}」（{project_id}）已删除；巡检 schedule 已取消。" + (f" {trashed}。" if trashed else "")


def patrol_projects() -> str:
    """手动跑一轮全员 idle 巡检（与每 5 分钟的 schedule code 同逻辑）：返回唤醒消息或「无事可做」。"""
    msg = patrol_once()
    return msg or "（巡检：无全员空闲且过冷却期的项目——各项目仍在忙 / 刚派过活 / 探测不可达，静默不唤醒）"


# ---------------------------------------------------------------- 注册

def agt_register(ctx=None):
    """ctx: {"cwd": ...}——引擎扫描按位置传入（无 ctx 也工作）。"""
    global _WORKSPACE
    if ctx and ctx.get("cwd"):
        _WORKSPACE = Path(ctx["cwd"])
    return [
        {"name": "create_project", "func": create_project, "group": "项目", "version": 1},
        {"name": "list_projects", "func": list_projects, "group": "项目", "version": 1},
        {"name": "add_project_member", "func": add_project_member, "group": "项目", "version": 1},
        {"name": "remove_project_member", "func": remove_project_member, "group": "项目", "version": 1},
        {"name": "delete_project", "func": delete_project, "group": "项目", "version": 1},
        {"name": "patrol_projects", "func": patrol_projects, "group": "项目", "version": 1},
    ]
