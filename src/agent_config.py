"""agent_config.py —— .agent/ 工作区配置：rules + skills（渐进式披露 + 自动沉淀）。

读取启动目录(cwd=WORKSPACE)下的 .agent/：
  .agent/rules/*              → 始终生效的规则，启动时读进 SYSTEM。
  .agent/skills/<名>/SKILL.md → 技能(YAML frontmatter name/description/when_to_use + markdown SOP)。
                                只把 frontmatter 摘要放进 SYSTEM；LLM 用 read_skill(name) 按需读完整 SOP。
save_skill 让 Agent 自主把可复用任务的 SOP 沉淀成新技能(或更新)，积累经验。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import yaml

from real_tools import WORKSPACE
from tools import Tool, Toolbox

_AGENT_DIR = ".agent"
_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def _split_frontmatter(text: str) -> tuple[dict, str]:
    """按首尾 --- 切分 YAML frontmatter 与 markdown 正文。无 frontmatter 返回 ({}, 全文)。"""
    if not text.startswith("---"):
        return {}, text
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}, text
    try:
        meta = yaml.safe_load(parts[1]) or {}
    except yaml.YAMLError:
        meta = {}
    return meta, parts[2].lstrip("\n")


def load_agent_yml(path: Path) -> tuple[dict, str]:
    """加载一个 agent 定义（新 .yml 格式优先，兼容旧 .md frontmatter）。
    返回 (meta, system_text)。.yml：yaml.safe_load 直读整个文件，无正文（正文进 assembly）；
    .md：旧 frontmatter + markdown 正文。失败返回 ({}, "")。"""
    try:
        raw = path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return {}, ""
    if path.suffix.lower() == ".yml":
        try:
            meta = yaml.safe_load(raw) or {}
        except yaml.YAMLError:
            return {}, ""
        return meta, ""
    return _split_frontmatter(raw)


# ===== assembly DSL：func 模板函数注册表（{func: name()} 动作项）=====
# 白名单受控取值函数——把框架内部动态值（模型清单/工作流清单等）注入装配项，
# 不做任意 eval（防触达文件系统/命令执行）。

def _func_load_models() -> str:
    """可用模型清单（"名（描述）"分号分隔）。"""
    import config as _c
    return "；".join(f"{n}（{m.get('desc', '').strip()}）" for n, m in _c.MODELS.items())


def _func_load_workflows() -> str:
    """当前 .agent/workflows/ 下可调用的工作流清单（名 — 描述）。"""
    try:
        from workflow import scan_workflows
        from real_tools import WORKSPACE as _ws
        parts = []
        for it in scan_workflows(_ws):
            if it.get("error"):
                continue
            desc = (it.get("meta") or {}).get("description", "")
            parts.append(f"- {it['name']}: {desc}".rstrip())
        return "\n".join(parts) or "(无工作流)"
    except Exception:
        return ""


def _func_load_skills() -> str:
    """.agent/skills/ 技能摘要清单（名: 描述（使用时机））。"""
    try:
        s = skills_summary(WORKSPACE)
        return s or "(无技能)"
    except Exception:
        return ""


def _func_load_agents() -> str:
    """.agent/agents/ 子 Agent 摘要清单（名: 描述）。"""
    try:
        s = agents_summary(WORKSPACE)
        return s or "(无子 Agent)"
    except Exception:
        return ""


def _runtime_form() -> str:
    """运行形态自我认知（用户提案 2026-09-19）：让 Agent 知道「自己在什么形态下跑、外界怎么和它说话」——
    CLI / WebUI（服务地址、端口）/ 容器与隧道公网入口 / 消息桥接 / 数据目录。
    直接影响行为：网页气泡场景要渲染友好（markdown/文件引用控件）、终端场景要纯文本紧凑；
    知道被 daemon 桥接才知道回复要走回执协议。内容启动后恒定（缓存前缀友好）。"""
    import os as _os
    bits: list[str] = []
    # ① 交互形态 + 服务地址
    ui = ""
    try:
        import server as _srv
        if getattr(_srv, "_server", None) is not None:
            url = ""
            try:
                import remote_tools as _rt
                url = str(getattr(_rt, "MY_URL", "") or "")
            except Exception:
                pass
            if not url:
                url = f"http://127.0.0.1:{getattr(_srv, '_port', '') or 8000}"
            ui = (f"WebUI 模式（`agt-web`）：本地服务 {url}，外界通过该地址的网页界面 / HTTP API 与你交互；"
                  f"你的回答渲染为网页气泡（markdown、代码块、文件引用控件均可用）")
    except Exception:
        pass
    if not ui:
        ui = ("CLI 模式：用户在终端与你对话（无 WebUI 服务在跑；需要网页/手机交互时用户可 `/web start`）")
    bits.append(ui)
    # ② 工作目录 / 数据目录
    try:
        from paths import AGT_DIR
        bits.append(f"workspace={WORKSPACE}；数据目录(AGT_HOME)={AGT_DIR}（session 存档/配置/记忆）")
    except Exception:
        bits.append(f"workspace={WORKSPACE}")
    # ③ 容器 / 隧道公网入口（CNB 容器场景：/etc/profile 注入模板）
    try:
        _tpl = _os.environ.get("CNB_VSCODE_PROXY_URI", "")
        if not _tpl:
            try:
                import re as _re
                _m = _re.search(r"CNB_VSCODE_PROXY_URI='([^']+)'", open("/etc/profile", encoding="utf-8").read())
                _tpl = _m.group(1) if _m else ""
            except Exception:
                _tpl = ""
        if _tpl:
            bits.append(f"运行在云容器（CNB）中：公网入口 {_tpl.replace('{{port}}', '8000')}"
                        f"（由容器 /etc/profile 的 CNB_VSCODE_PROXY_URI 推导，重建后自动变化）；"
                        f"容器有约 18 小时生命周期上限，重建后由启动脚本恢复身份/配置/凭证")
    except Exception:
        pass
    # ④ 消息桥接（外部平台 daemon → a2a_bridge → 本实例）
    try:
        if _os.environ.get("OKX_A2A_AI_CLAUDE_COMMAND"):
            bits.append("消息桥接：外部平台（如 OKX.A2A）的 daemon 经 a2a_bridge 把消息注入本实例，"
                        "你的回答按回执协议写入指定文件交付")
    except Exception:
        pass
    return "【运行形态】" + "；".join(bits) + "。"


def _func_runtime_env() -> str:
    """{func:runtime_env()} —— 运行时自我认知：包名/版本/升级方式 + 运行形态（main.yml 装配引用）。
    版本动态读 src/__init__.py 的 __version__（发版自动跟随，不烘焙）。
    其它 repo 的 session 由此知道自己跑在 agt-agent 里、怎么升级（用户提案）。"""
    try:   # 优先运行源码的 __version__（开发机 importlib.metadata 可能是旧安装的 0.9.0）
        import __init__ as _pkg
        v = getattr(_pkg, "__version__", "") or "?"
    except Exception:
        v = ""
    if not v or v == "?":
        try:
            from importlib.metadata import version as _v
            v = _v("agt-agent")
        except Exception:
            v = "?"
    try:
        _form = "\n" + _runtime_form()
    except Exception:
        _form = ""
    return (f"agt-agent v{v}（pip 包；CLI `agt` / WebUI `agt-web`）。"
            f"升级：`pip install -U agt-agent` 后 /restart 生效；"
            f"随包播种资产刷新：/update-assets apply。GitHub: vgp7758/Agt。"
            f"{_form}"
            f"\n【外部事件注入】需要让脚本/服务/其它机器（或你自己的后台任务）通过 HTTP 向本实例或队友"
            f"推送消息/文件/事件时，见 docs/external-injection.md"
            f"（GitHub: vgp7758/Agt/blob/main/docs/external-injection.md）——核心一句话："
            f"POST <实例地址>/api/callback + header X-Cb-Token（token=该实例 settings.json 的 callback_token）"
            f"+ JSON {{\"text\":…, \"source\":…}} → 进对方 inbox 并唤醒一轮；推文件加 X-Cb-Type: file。")


def _func_remote_instances() -> str:
    """{func:load_remote_instances()} —— 已连接远程 agt 实例清单 + remote_instance_id 路由使用规则。
    无连接返回空串（不注入——避免无远程场景的 SYSTEM 噪声）。"""
    try:
        import remote_tools as _rt
        with _rt._LOCK:
            items = list(_rt.REMOTE_SERVERS.items())
        if not items:
            return ""
        lines = ["【远程 agt 实例（工具调用 arguments 里加 remote_instance_id=\"<id>\" 即路由到该实例执行，结果前缀 [remote:id]；id 值即下列 server_id）】"]
        for sid, it in items:
            tag = "" if it.get("status") == "online" else f" [⚠ {it.get('status')}]"
            lines.append(f"- {sid}: {it['url']}{tag} · {it.get('tools_count', '?')} 工具 · session={it.get('session_name', '?')}")
        lines.append("规则：远程文件操作（read/edit/write…）对同一文件须持续带同一 remote_instance_id（远程 file_version 乐观锁跨实例生效）；"
                     "远程实例的会话上下文不参与——纯工具直执行（要对方带上下文干活用消息驱动而非工具路由）。")
        return "\n".join(lines)
    except Exception:
        return ""


def _func_print_time() -> str:
    """{func:print_time()} —— 实时时段块（替代 tail.time 拆段——用户简化 2026-09-02：动态内容用 func 项放清单尾部，
    配合三区重构的「steps 后全进区3 merge」语义，无需专门段名）。"""
    try:
        import agent_config as _self
        from datetime import datetime
        return f"[当前时间] {datetime.now().strftime('%Y-%m-%d %H:%M:%S %A')}"
    except Exception:
        return ""


def _func_team_profiles(viewer_id: str = "") -> str:
    """{func:get_team_profiles()} —— 团队成员看板（agent_id/模型/忙闲/recap——registry 为准，不含看者自己）。
    viewer_id：看板所属的 agent（session._asm_agent_id 经 resolve_assembly_func 转发）——子 Agent 装配时
    exclude 自己而非主 Agent；缺省（text 内插等无所属场景）= 挂点主 Agent。
    修复注记（2026-09-02）：此前 from multiagent import format_team 是恒 ImportError 的坏路径
    （multiagent 无模块级 format_team——它是 AgentRegistry 方法）→ 恒空串；改走运行时挂点。"""
    a = _RUNTIME_AGENT
    exclude = str(viewer_id or "").strip() or (a.agent_id if a is not None else "")
    reg = getattr(a, "registry", None) if a is not None else None
    if reg is None:
        return ""
    try:
        return reg.format_team(exclude_id=exclude)
    except Exception:
        return ""


# 运行时主 Agent 挂点（用户提案 2026-09-02）：FUNC_REGISTRY 补实例状态函数——spec/plan/bg_services/
# 团队看板的数据在 Agent 实例上（active_spec/active_plan/services/registry），模块级函数经此引用取数。
# 主 Agent 构造时自挂（agent.py，agent_id == '_main_'）；子 Agent/无引擎场景（外置脚本独立跑）无挂点
# → 相关函数返回空（内插空判语义：整段不注入，不炸装配）。
_RUNTIME_AGENT = None


def set_runtime_agent(agent) -> None:
    """主 Agent 构造时挂引用（幂等——重建/复用时覆盖为最新主实例）。"""
    global _RUNTIME_AGENT
    _RUNTIME_AGENT = agent


def _active_spec_dict():
    """活动 spec dict 或 None（approved 态返回 None——已生成 plan，由 plan_* 接管注入，防双重）。"""
    a = _RUNTIME_AGENT
    s = getattr(a, "active_spec", None) if a is not None else None
    if not s or not s.get("steps") or s.get("review_state") == "approved":
        return None
    return s


def _active_plan_dict():
    a = _RUNTIME_AGENT
    p = getattr(a, "active_plan", None) if a is not None else None
    return p if (p and p.get("steps")) else None


def _func_spec_content() -> str:
    """{func:spec_content()} —— 当前活动 spec 的设计概述（id/标题/design/批阅态）。
    draft/committed/rejected 态注入；approved 由 plan 接管返回空。与 spec_steps() 配套拆开装配。"""
    s = _active_spec_dict()
    if not s:
        return ""
    try:
        from spec_tools import _SPEC_LABEL
    except Exception:
        _SPEC_LABEL = {}
    title = s.get("title", "")
    rs = s.get("review_state", "draft")
    head = (f"【施工方案 spec】{s.get('id', '')}" + (f" · {title}" if title else "")
            + f"（{_SPEC_LABEL.get(rs, rs)}）")
    design = (s.get("design") or "").strip()
    return head + (f"\n设计：{design}" if design else "")


def _func_spec_steps() -> str:
    """{func:spec_steps()} —— 当前活动 spec 的施工步骤清单（[action] file @ anchor — rationale，
    尾随批阅态提示行——草稿未提交/待批阅/已返工+反馈）。"""
    s = _active_spec_dict()
    if not s:
        return ""
    rs = s.get("review_state", "draft")
    lines = [f"施工步骤（共 {len(s['steps'])} 步）："]
    for i, st in enumerate(s["steps"]):
        anchor_s = f" @ {st['anchor']}" if st.get("anchor") else ""
        rat_s = f" — {st.get('rationale', '')}" if st.get("rationale") else ""
        lines.append(f"  {i + 1}. [{st.get('action', 'review')}] {st.get('file', '') or '(无文件)'}{anchor_s}{rat_s}")
    if rs == "draft":
        lines.append("这是草稿，尚未提交批阅。用 commit_spec 提交，或 regenerate_spec 改进。")
    elif rs == "committed":
        lines.append("已提交批阅，等待用户裁定（通过 → 自动建 plan 开始施工；返工 → 据反馈重新生成）。")
    elif rs == "rejected":
        fb = s.get("feedback", "")
        lines.append("已被返工。" + (f"用户反馈：{fb}" if fb else ""))
    return "\n".join(lines)


def _func_plan_content() -> str:
    """{func:plan_content()} —— 当前活动计划的设计概述（id/标题/design）。
    无活动计划返回空；**全部步骤完成后只留一行标题**（2026-09-13·用户裁定——
    design 全文每步注入是纯浪费，完成态仅需可见性：知道有 plan 挂着、可 exit_plan 收尾）。
    **施工模式返回空**（spec s_e1804804）：存在未完成步时 design 全文已前移到头部施工牌
    （第二条 system·byte-stable 前缀）——此处再注入就是双份。"""
    p = _active_plan_dict()
    if not p:
        return ""
    steps = p.get("steps") or []
    done = sum(1 for x in steps if x.get("status") == "completed")
    if steps and done < len(steps):
        return ""   # 施工模式：内容已前移施工牌（防双份）
    title = p.get("title", "")
    head = f"【当前计划】{p.get('id', '')}" + (f" · {title}" if title else "")
    if steps and done == len(steps):
        return f"{head}（已完成 {done}/{len(steps)} 步——细节已消化；确认收尾可 exit_plan 退出）"
    design = (p.get("design") or "").strip()
    return head + f"\n设计：{design or '（无）'}"


def _func_plan_steps() -> str:
    """{func:plan_steps()} —— 当前活动计划的步骤进度清单（☐/▶/✅ + 描述 + 状态 + 推进提示行）。
    全部完成后返回空（plan_content 的一行摘要已含进度——步骤全文不再注入）。"""
    p = _active_plan_dict()
    if not p:
        return ""
    from plan_tools import _PLAN_ICON, _PLAN_LABEL   # 延迟 import：防顶层循环
    steps = p["steps"]
    done = sum(1 for x in steps if x.get("status") == "completed")
    if steps and done == len(steps):
        return ""
    lines = [f"进度（共 {len(steps)} 步，已完成 {done}）："]
    for i, x in enumerate(steps):
        st = x.get("status")
        lines.append(f"  {_PLAN_ICON.get(st, '?')} {i + 1}. {x.get('description', '')} ({_PLAN_LABEL.get(st, '')})")
    lines.append("推进时用 update_plan 更新状态、add_step 追加步骤、edit_plan 改标题/设计、exit_plan 退出。")
    return "\n".join(lines)


def _func_bg_services() -> str:
    """{func:bg_services()} —— 后台服务清单（名称/pid/运行时长/退出——_runtime_system_extra 的服务部分）。
    无运行中服务返回空（整段不注入）。"""
    a = _RUNTIME_AGENT
    if a is None:
        return ""
    try:
        svc = a.services.status_lines()
    except Exception:
        return ""
    return ("【后台服务状态】当前服务：\n" + "\n".join(svc)) if svc else ""


FUNC_REGISTRY = {
    "load_models": _func_load_models,
    "load_workflows": _func_load_workflows,
    "load_skills": _func_load_skills,
    "load_agents": _func_load_agents,
    "runtime_env": _func_runtime_env,
    "load_remote_instances": _func_remote_instances,
    "print_time": _func_print_time,
    "get_team_profiles": _func_team_profiles,
    "spec_content": _func_spec_content,
    "spec_steps": _func_spec_steps,
    "plan_content": _func_plan_content,
    "plan_steps": _func_plan_steps,
    "bg_services": _func_bg_services,
}



def resolve_assembly_func(name: str, viewer_id: str = "") -> str:
    """执行 assembly func: 项里的模板函数（白名单）。未知名返回空。
    viewer_id：func 项所属的 agent（session._asm_agent_id）——签名带 viewer_id 的函数
    （get_team_profiles 等「谁在看」敏感的）据此区分视角；其余函数忽略。"""
    import inspect
    fn = FUNC_REGISTRY.get(name)
    if fn is None:
        return ""
    try:
        if "viewer_id" in inspect.signature(fn).parameters:
            return str(fn(viewer_id=viewer_id) or "").strip()
        return str(fn() or "").strip()
    except Exception:
        return ""


def load_rules(workspace: Path) -> str:
    """拼接 .agent/rules/ 下所有文件内容（按文件名排序）。无则空串。"""
    d = workspace / _AGENT_DIR / "rules"
    if not d.exists():
        return ""
    chunks = [f.read_text(encoding="utf-8", errors="ignore").strip()
              for f in sorted(d.iterdir()) if f.is_file()]
    return "\n\n".join(chunks)


def _global_skills_root() -> Path:
    """全局技能目录 ~/.agt/skills/（跨 repo 共享：技能包一次安装，各 repo 按 global-skills.json 按需激活）。"""
    from paths import AGT_DIR
    return AGT_DIR / "skills"


def _enabled_global_skills(workspace: Path) -> list[str]:
    """读 repo 的 .agent/skills/global-skills.json 激活清单（纯数组，元素=全局技能名）。
    不存在/损坏 = 零全局激活（现行为不变）。"""
    p = workspace / _AGENT_DIR / "skills" / "global-skills.json"
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return []
    return [x.strip() for x in data if isinstance(x, str) and x.strip()] if isinstance(data, list) else []


def _resolve_skill(name: str, workspace: Path | None = None):
    """统一寻址（本地/全局透明）：先 repo .agent/skills/<name>/（本地优先 shadow），
    再全局 ~/.agt/skills/<name>/（须在激活清单）。返回 (技能目录|None, scope: ''|'local'|'global')。"""
    if not _NAME_RE.match(name or ""):
        return None, ""
    ws = workspace or WORKSPACE
    local = ws / _AGENT_DIR / "skills" / name
    if (local / "SKILL.md").exists():
        return local, "local"
    if name in _enabled_global_skills(ws):
        g = _global_skills_root() / name
        if (g / "SKILL.md").exists():
            return g, "global"
    return None, ""


def load_skills_index(workspace: Path) -> list[dict]:
    """扫 repo .agent/skills/*/SKILL.md + 全局 ~/.agt/skills/ 中已激活技能。
    返回 [{name, description, when_to_use, path, scope}, ...]（scope=local/global；本地同名 shadow 全局）。"""
    out = []
    local_dir = workspace / _AGENT_DIR / "skills"
    local_names = set()
    if local_dir.exists():
        for skill_md in sorted(local_dir.glob("*/SKILL.md")):
            try:
                meta, _ = _split_frontmatter(skill_md.read_text(encoding="utf-8", errors="ignore"))
            except Exception:
                continue
            local_names.add(skill_md.parent.name)
            out.append({
                "name": meta.get("name", skill_md.parent.name),
                "description": meta.get("description", ""),
                "when_to_use": meta.get("when_to_use", ""),
                "path": str(skill_md.relative_to(workspace)).replace("\\", "/"),
                "scope": "local",
            })
    enabled = set(_enabled_global_skills(workspace))
    if enabled:
        g_root = _global_skills_root()
        for name in sorted(enabled):
            if name in local_names:  # 本地同名技能优先（shadow 全局）
                continue
            skill_md = g_root / name / "SKILL.md"
            if not skill_md.exists():
                continue
            try:
                meta, _ = _split_frontmatter(skill_md.read_text(encoding="utf-8", errors="ignore"))
            except Exception:
                continue
            out.append({
                "name": meta.get("name", name),
                "description": meta.get("description", ""),
                "when_to_use": meta.get("when_to_use", ""),
                "path": str(skill_md).replace("\\", "/"),
                "scope": "global",
            })
    return out


def skills_summary(workspace: Path) -> str:
    """拼成 SYSTEM 里一行一技能的摘要。无技能返回空串。"""
    idx = load_skills_index(workspace)
    if not idx:
        return ""
    lines = []
    for s in idx:
        when = f"（使用时机: {s['when_to_use']}）" if s["when_to_use"] else ""
        g = "🌐 " if s.get("scope") == "global" else ""
        lines.append(f"- {g}{s['name']}: {s['description']}{when}")
    return "\n".join(lines)


# ===== 子 Agent 声明（.agent/agents/*.yml，声明式 + 按需实例化 + 一次性；兼容旧 .md）=====

def _agents_glob(d: Path):
    """扫 .agent/agents/ 下的 *.yml 与 *.md（兼容旧格式），返回 file 列表。"""
    if not d.exists():
        return []
    return sorted([p for p in d.glob("*.yml")] + [p for p in d.glob("*.md")])


def load_agents_index(workspace: Path) -> list[dict]:
    """扫 .agent/agents/*.yml（兼容 .md），返回 [{name, description, tools, model, path}, ...]。"""
    d = workspace / _AGENT_DIR / "agents"
    out = []
    for f in _agents_glob(d):
        # 同名 .yml 与 .md 同时存在时，.yml 优先（迁移期两格式并存）
        if f.suffix.lower() == ".md" and (f.with_suffix(".yml").exists()):
            continue
        try:
            meta, _ = load_agent_yml(f)
        except Exception:
            continue
        out.append({
            "name": meta.get("name", f.stem),
            "description": meta.get("description", ""),
            "tools": meta.get("tools", ""),
            "model": meta.get("model", ""),
            "path": str(f.relative_to(workspace)).replace("\\", "/"),
        })
    return out


def agents_summary(workspace: Path) -> str:
    """拼成 SYSTEM 里一行一个子 agent 的摘要（name + description/何时调用）。
    声明了 optional 装配段的附一行提示——主 Agent 派活时才知道可用 assembly 参数按需打开。
    行内描述 'history|optional // 说明' 优先于内置默认文案。"""
    idx = load_agents_index(workspace)
    if not idx:
        return ""
    _HINT = {"history": "可带本 agent 历轮对话记忆",
             "ltm": "可带跨会话长期记忆",
             "rules": "可带项目规则",
             "tail": "可带动态尾块",
             "hooks": "可跑钩子工作流"}

    def _opt_of(it):
        """assembly 项 → (段名, 自定义描述) 或 None。字符串/单键 dict 两形态。
        dict 形态是 YAML 把 'history|optional: 描述' 解析成 {"history|optional": "描述"} 的兜底。"""
        if isinstance(it, str):
            body, _, desc = it.partition("//")
            desc = desc.strip()
        elif isinstance(it, dict) and len(it) == 1:
            k, v = next(iter(it.items()))
            if isinstance(v, str) and "|" in str(k):
                body, desc = str(k), v.strip()
            else:
                return None
        else:
            return None
        base = body.split("|", 1)[0].split("=")[0].strip()
        return (base, desc) if base in _HINT and "|" in body else None

    lines = []
    for a in idx:
        opt: dict[str, str] = {}
        try:
            meta, _ = load_agent_yml(workspace / a["path"])
            for it in (meta.get("assembly") or []):
                r = _opt_of(it)
                if r and r[0] not in opt:
                    opt[r[0]] = r[1]
        except Exception:
            pass
        tail = ""
        if opt:
            tips = [f"{s}=on {(desc or _HINT[s])}" for s, desc in opt.items()]
            tail = " [可选装配: " + "；".join(tips) + "（默认关）]"
        lines.append(f"- {a['name']}: {a['description']}{tail}")
    return "\n".join(lines)


def seed_default_agents(workspace: Path) -> int:
    """首次启动把随包默认子 agent 模板（src/agents/ 的 yml+md——v2.1 声明的 persona md
    必须随行，缺了装配清单的 file: 项取不到人设）播种到 .agent/agents/。
    目标已存在则跳过（不覆盖用户修改）。返回播种数量。照搬 workflow.seed_default_workflows。
    播种时写 seed_state 基线（/update-assets 三方 hash 判定用）。"""
    bundled = Path(__file__).resolve().parent / "agents"
    dst = workspace / _AGENT_DIR / "agents"
    if not bundled.exists():
        return 0
    dst.mkdir(parents=True, exist_ok=True)
    from asset_sync import _sha, _load_state, _save_state
    st = _load_state(workspace)
    n = 0
    for src in sorted(list(bundled.glob("*.yml")) + list(bundled.glob("*.md"))):
        target = dst / src.name
        if target.exists():
            continue
        try:
            target.write_bytes(src.read_bytes())   # 字节级：write_text 行尾转换会让 /update-assets 的 hash 对不上
            st[f"agent/{src.name}"] = _sha(src)
            n += 1
        except Exception:
            pass
    if n:
        _save_state(workspace, st)
    return n


def seed_main_agent(workspace: Path = None) -> Path:
    """首次启动把随包默认主 agent 元信息（src/assets/main.yml）播种到 ~/.agt/main.yml。
    目标已存在则跳过（不覆盖用户修改）。返回 main.yml 路径——repo 级覆盖（用户裁定
    2026-08-31·多实例组网）：<cwd>/.agent/main.yml 存在则优先返回它（本地实例的
    独立主声明——角色实例的认知/配置双隔离），播种本身仍写全局。"""
    from config import _AGT_DIR, config_file
    bundled = Path(__file__).resolve().parent / "assets" / "main.yml"
    dst = _AGT_DIR / "main.yml"
    if bundled.exists():
        _AGT_DIR.mkdir(parents=True, exist_ok=True)
        if not dst.exists():
            dst.write_text(bundled.read_text(encoding="utf-8"), encoding="utf-8")
    return config_file("main.yml")   # repo 级覆盖：本地存在读本地（播种不动全局既有份）


def migrate_agents_md_to_yml(workspace: Path) -> int:
    """一次性迁移（幂等）：.agent/agents/*.md（frontmatter+正文）→ 同名 .yml。
    正文（persona）变成 assembly 的首个 text: 项；frontmatter 的 assembly 白名单段名保留在后。
    已有同名 .yml 跳过（不覆盖）。.md 原文件保留不删（兼容读取，yml 优先）。返回迁移数。"""
    d = workspace / _AGENT_DIR / "agents"
    if not d.exists():
        return 0
    n = 0
    for md in sorted(d.glob("*.md")):
        yml = md.with_suffix(".yml")
        if yml.exists():
            continue
        try:
            meta, body = _split_frontmatter(md.read_text(encoding="utf-8", errors="ignore"))
        except Exception:
            continue
        if not isinstance(meta, dict) or not meta.get("name"):
            continue
        asm = meta.get("assembly") or []
        if not isinstance(asm, list):
            asm = []
        new_asm = ([{"text": body.strip()}] if body.strip() else []) + asm
        data = dict(meta)
        if new_asm:
            data["assembly"] = new_asm
        yml.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
        n += 1
    return n


# ===== 技能工具（注册进 Agent，子 Agent 继承）=====

def read_skill(name: str) -> str:
    """读取某个技能的完整 SKILL.md（含详细 SOP）。任务匹配某技能时，先调它取执行步骤。
    name: 技能名（repo .agent/skills/ 与已激活全局技能统一寻址，本地优先；大技能建议先 skill_navigate 浏览结构）。"""
    if not _NAME_RE.match(name or ""):
        return f"[非法名称] '{name}'，技能名只能含字母数字、下划线、连字符"
    d, _scope = _resolve_skill(name)
    if d is None:
        return _skill_not_found(name)
    return (d / "SKILL.md").read_text(encoding="utf-8", errors="ignore")


def _skill_not_found(name: str) -> str:
    return (f"[未找到技能] {name}（可用技能见 SYSTEM 的【可用技能】清单；"
            f"若是全局技能，需在本 repo 的 .agent/skills/global-skills.json 激活清单里启用）")


def _skill_invalid_name(name: str) -> str:
    return f"[非法名称] '{name}'，技能名只能含字母数字、下划线、连字符"


def _md_sections(text: str) -> list[tuple[int, str, int]]:
    """markdown 标题清单：[(level, title, lineno), ...]（# ~ ###）。"""
    heads: list[tuple[int, str, int]] = []
    for i, ln in enumerate(text.splitlines(), 1):
        m = re.match(r"^(#{1,3})\s+(.+?)\s*$", ln)
        if m:
            heads.append((len(m.group(1)), m.group(2), i))
    return heads


def _md_section_text(text: str, want: str) -> str | None:
    """取 markdown 指定章节正文（标题含/不含#均可，大小写不敏感）；未找到返回 None。"""
    heads = _md_sections(text)
    body = text.splitlines()
    want = want.lstrip("#").strip()
    for idx, (lv, title, _ln) in enumerate(heads):
        if title == want or title.lower() == want.lower():
            end = next((h[2] - 1 for h in heads[idx + 1:] if h[0] <= lv), len(body))
            seg = "\n".join(body[_ln - 1:end]).strip()
            if len(seg) > 12000:
                seg = seg[:12000] + "\n…（截断，章节共 %d 字）" % len(seg)
            return seg
    return None


def _find_skill_file(d: Path, file: str):
    """技能包内文件宽松定位（用户提案 2026-09-20：file 传完整路径或文件名皆可）。
    匹配链：① 完整相对路径 → ② 包内 rglob 同名 basename → ③ basename 去扩展名/部分包含（stem 包含）。
    返回 Path（唯一定位）/ list[Path]（多候选，调用方列出让模型重选）/ None（找不到）。"""
    p = d / file
    if p.exists():
        return p
    name = Path(file).name
    cands = [q for q in d.rglob(name)]
    if len(cands) == 1:
        return cands[0]
    if len(cands) > 1:
        return cands
    stem = Path(name).stem
    cands = [q for q in d.rglob("*")
             if q.is_file() and q.suffix.lower() in (".md", ".txt") and stem and stem in q.stem]
    if len(cands) == 1:
        return cands[0]
    return cands or None


def skill_navigate(name: str, section: str = "", list_only: bool = False, file: str = "") -> str:
    """浏览技能包结构 / 读包内任意文件（大技能不必整读）。
    默认：目录树（.md 文件附带其 #/## 级标题清单——一眼看到每个文件有哪些 section）+ SKILL.md 章节清单；
    file=文件名或相对路径（如 '08-CINEDANCE视频提示词.md' 或 '专业模块/08-CINEDANCE视频提示词.md'）→ 读该文件
    （.md 可配合 section 分节读；同名多文件时列出候选；截断 12000 字）；
    section=章节标题 → 读 SKILL.md（或 file 指定文件）的该章节；list_only=True 只列结构。
    name: 技能名（本地/全局统一寻址）。"""
    d, scope = _resolve_skill(name)
    if d is None:
        return _skill_invalid_name(name) if not _NAME_RE.match(name or "") else _skill_not_found(name)
    src = "🌐 全局 ~/.agt/skills" if scope == "global" else "repo .agent/skills"
    lines = [f"📦 技能 '{name}'（{src}）", f"路径: {d}"]
    # 目录树（≤3 层，跳过隐藏/缓存目录；.md 附 #/## 级标题——每文件≤6 条 + …，总预算 160 行防刷屏）
    shown = 0
    for p in sorted(d.rglob("*")):
        rel = p.relative_to(d)
        if len(rel.parts) > 3 or any(pt.startswith(".") or pt == "__pycache__" for pt in rel.parts):
            continue
        lines.append("  " * (len(rel.parts) - 1) + ("📁 " if p.is_dir() else "  • ") + p.name)
        shown += 1
        if p.is_file() and p.suffix.lower() == ".md" and p.name != "SKILL.md":
            try:
                _t = p.read_text(encoding="utf-8", errors="ignore")
                _hs = [t for lv, t, _ in _md_sections(_t) if lv <= 2]
                _indent = "  " * len(rel.parts) + "  · "
                lines.extend(_indent + t for t in _hs[:6])
                if len(_hs) > 6:
                    lines.append(_indent + f"…（共 {len(_hs)} 个标题）")
            except Exception:
                pass
        if shown >= 120 or len(lines) >= 160:
            lines.append("  …（条目过多省略；用 file= 直接读指定文件）")
            break
    # ── file=：读包内任意文件（文件名或相对路径均可；防逃逸同 run_code）──
    if file:
        if ".." in Path(file).parts:
            return "[非法路径] file 须为技能目录内的相对路径/文件名（禁止 .. 逃逸）"
        hit = _find_skill_file(d, file)
        if hit is None:
            return "\n".join(lines) + f"\n[文件不存在] {file}。上方目录树可见包内文件；.md 附了标题清单"
        if isinstance(hit, list):
            cands = "\n".join(f"  - {str(q.relative_to(d)).replace(chr(92), '/')}'" for q in hit[:10])
            return "\n".join(lines) + f"\n[多个同名候选] '{file}' 匹配到 {len(hit)} 个文件：\n{cands}\n请用完整相对路径重试。"
        fp = hit
        if not (fp == d.resolve() or d.resolve() in fp.parents):
            return "[非法路径] 文件必须在技能目录内"
        disp = str(fp.relative_to(d)).replace(chr(92), "/")
        try:
            text = fp.read_text(encoding="utf-8", errors="ignore")
        except Exception as e:
            return f"[读取失败] {disp}: {e}"
        try:
            text = fp.read_text(encoding="utf-8", errors="ignore")
        except Exception as e:
            return f"[读取失败] {file}: {e}"
        if fp.suffix.lower() == ".md":
            if section:
                seg = _md_section_text(text, section)
                if seg is None:
                    avail = "\n".join(f"  {'#' * lv} {t}" for lv, t, _ in _md_sections(text)) or "（无章节标题）"
                    return "\n".join(lines) + f"\n[未找到章节] '{section}'。{disp} 可用章节：\n{avail}"
                return "\n".join(lines) + f"\n\n📖 {disp} · 章节 '{section.lstrip('#').strip()}'：\n\n{seg}"
            heads = _md_sections(text)
            hdr = ("\n📑 章节：\n" + "\n".join(f"  {'  ' * (lv-1)}- {t}  (L{n})" for lv, t, n in heads)) if heads else ""
            full = f"📄 {disp}（{len(text.splitlines())} 行）{hdr}\n\n（全文 12000 字截断；section=标题 分节读）\n\n{text[:12000]}"
            if len(text) > 12000:
                full += f"\n…（截断，共 {len(text)} 字）"
            return "\n".join(lines) + "\n" + full
        return "\n".join(lines) + f"\n\n📄 {disp}：\n\n{text[:12000]}" + (f"\n…（截断，共 {len(text)} 字）" if len(text) > 12000 else "")
    # ── 默认：SKILL.md 章节导航 ──
    md = d / "SKILL.md"
    if not md.exists():
        return "\n".join(lines) + "\n[注意] 该技能包没有 SKILL.md（纯资产包；用 file= 读包内文件）"
    text = md.read_text(encoding="utf-8", errors="ignore")
    body = text.splitlines()
    heads = _md_sections(text)
    if section:
        seg = _md_section_text(text, section)
        if seg is not None:
            return "\n".join(lines) + f"\n\n📖 章节 '{section.lstrip('#').strip()}'：\n\n{seg}"
        avail = "\n".join(f"  {'#' * lv} {t}" for lv, t, _ in heads) or "（无章节标题）"
        return "\n".join(lines) + f"\n[未找到章节] '{section}'。可用章节：\n{avail}"
    lines.append("\n📑 SKILL.md 章节：")
    lines.extend(f"  {'  ' * (lv - 1)}- {t}  (L{n})" for lv, t, n in heads)
    if not list_only:
        lines.append(f"（全文 {len(body)} 行：section=标题 读指定章节；file=路径 读包内其它文件；read_skill 读全文）")
    return "\n".join(lines)


def skill_run_code(name: str, script: str, args: str = "") -> str:
    """执行技能包内的 python 脚本（脚本须在技能目录内，如 scripts/xxx.py、tools/xxx.py）。
    name: 技能名；script: 相对技能目录的脚本路径；args: 命令行参数（空格分隔，支持引号）。
    cwd=技能目录；超时 120s；stdout/stderr 截断 8000 字。"""
    import shlex
    import subprocess
    import sys
    d, _scope = _resolve_skill(name)
    if d is None:
        return _skill_invalid_name(name) if not _NAME_RE.match(name or "") else _skill_not_found(name)
    if not script or not str(script).endswith(".py") or ".." in Path(str(script)).parts:
        return "[非法脚本] script 须为技能目录内的 .py 相对路径（禁止 .. 逃逸）"
    sp = (d / script).resolve()
    if d.resolve() not in sp.parents:
        return "[非法脚本路径] 脚本必须在技能目录内"
    if not sp.exists():
        pys = [str(p.relative_to(d)).replace("\\", "/") for p in d.rglob("*.py")]
        listing = "\n".join(pys[:40]) if pys else "（无）"
        return f"[脚本不存在] {script}。技能包内的 .py：\n{listing}"
    cmd = [sys.executable, str(sp)] + (shlex.split(args) if args else [])
    try:
        r = subprocess.run(cmd, cwd=str(d), capture_output=True, text=True,
                           timeout=120, encoding="utf-8", errors="replace")
    except subprocess.TimeoutExpired:
        return f"[超时] python {script} 超过 120s 被终止"
    except Exception as e:
        return f"[执行失败] {e}"
    out = (r.stdout or "").strip()
    err = (r.stderr or "").strip()
    body = (out + ("\n[stderr]\n" + err if err else "")) or "（无输出）"
    if len(body) > 8000:
        body = body[:8000] + f"\n…（截断，共 {len(body)} 字）"
    return f"▶ python {script} {args}".strip() + f"  [exit={r.returncode}]\n{body}"


def skill_evaluate(name: str, input: str = "") -> str:
    """技能约定评测入口。优先级：① 技能包 scripts/evaluate.py 存在 → 以 input 作 stdin 执行它
    （cwd=技能目录，超时 120s）；② 包内 EVAL.md → 返回评测说明；③ 都没有 → 明确提示未提供。
    name: 技能名；input: 传给评测脚本的 stdin 内容（如待检产物/提示词）。"""
    import subprocess
    import sys
    d, _scope = _resolve_skill(name)
    if d is None:
        return _skill_invalid_name(name) if not _NAME_RE.match(name or "") else _skill_not_found(name)
    ev = d / "scripts" / "evaluate.py"
    if ev.exists():
        try:
            r = subprocess.run([sys.executable, str(ev)], cwd=str(d), input=input or "",
                               capture_output=True, text=True, timeout=120,
                               encoding="utf-8", errors="replace")
        except subprocess.TimeoutExpired:
            return "[超时] scripts/evaluate.py 超过 120s 被终止"
        except Exception as e:
            return f"[执行失败] {e}"
        out = (r.stdout or "").strip()
        err = (r.stderr or "").strip()
        body = (out + ("\n[stderr]\n" + err if err else "")) or "（无输出）"
        if len(body) > 8000:
            body = body[:8000] + f"\n…（截断，共 {len(body)} 字）"
        return f"🧪 evaluate '{name}'  [exit={r.returncode}]\n{body}"
    em = d / "EVAL.md"
    if em.exists():
        txt = em.read_text(encoding="utf-8", errors="ignore")
        return f"📋 技能 '{name}' 评测说明（EVAL.md）：\n\n{txt[:6000]}" + ("\n…（截断）" if len(txt) > 6000 else "")
    return f"[无评测入口] 技能 '{name}' 未提供 scripts/evaluate.py 或 EVAL.md（技能作者未约定评测方式）"


def save_skill(name: str, description: str, when_to_use: str, sop: str) -> str:
    """把一个可复用任务的 SOP 沉淀为技能（写/更新 .agent/skills/<name>/SKILL.md）。
    name: 技能名；description: 一句话作用；when_to_use: 使用时机；sop: 详细步骤(markdown)。"""
    if not _NAME_RE.match(name or ""):
        return f"[非法名称] '{name}'，技能名只能含字母数字、下划线、连字符"
    d = WORKSPACE / _AGENT_DIR / "skills" / name
    d.mkdir(parents=True, exist_ok=True)
    meta = yaml.safe_dump(
        {"name": name, "description": description, "when_to_use": when_to_use},
        allow_unicode=True, sort_keys=False,
    ).strip()
    (d / "SKILL.md").write_text(f"---\n{meta}\n---\n\n{sop.strip()}\n", encoding="utf-8")
    return f"✅ 已保存技能 '{name}' -> {(d / 'SKILL.md').relative_to(WORKSPACE)}"


SKILL_TOOLS = Toolbox(Tool(read_skill), Tool(save_skill), Tool(skill_navigate),
                     Tool(skill_run_code), Tool(skill_evaluate))
