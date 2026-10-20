"""multiagent.py —— 多 Agent 协作（主 Agent 派任务给一次性子 Agent）。

子 Agent 是【声明式 + 按需实例化 + 一次性】：声明存 .agent/agents/<name>.md
（frontmatter: name/description/tools/model + body: systemPrompt）。harness 每轮把
可用子 agent 清单投影进主 Agent SYSTEM（见 agent_config.agents_summary）。agent_prompt
按需读 md 建临时实例跑完即弃——多次 agent_prompt 同名 = 多个独立实例（无共享状态）。

工具：
  create_agent(name, description, system, tools, model)  写 .agent/agents/<name>.md（不建实例）
  agent_prompt(name, prompt)               读 md 建临时实例，跑完返回报告后销毁
  kill_agent(name)                         删 .agent/agents/<name>.md
  list_agents()                            扫 .agent/agents/ 列出
"""
from __future__ import annotations

import copy
import logging
import re
import shutil
import threading
import time
from pathlib import Path

import yaml

_LOG = logging.getLogger("agt.multiagent")

import config
from agent import Agent
from agent_config import _AGENT_DIR, _NAME_RE, _split_frontmatter, load_agents_index
from real_tools import WORKSPACE
from tools import Tool, Toolbox

# 子 Agent 绝不能继承的工具（防止递归生子 Agent、互相操控；计划工具绑定主 Agent）
# 通信工具排除——绑定到主 Agent 闭包，子 Agent 需要重新注册绑定到自身的版本
# 会话工具（get_session_history/semantic_search_history/rename_session）同理：闭包绑定
# 主 session——钩子工作流（before_turn_retrieval 等）在子 Agent 里跑时必须查子自己的历史
_AGENT_TOOL_NAMES = {"create_agent", "agent_prompt", "kill_agent", "list_agents",
                     "create_plan",
                     # update_plan 例外保留（2026-10-15 施工委托实锤）：施工 worker 靠它标记计划
                     # 进度——与主 Agent 共享同一 plan 文件（worker 标、双方面板同步见）。
                     # create_plan 仍排除（worker 只推进度不建计划）。
                     "list_team", "agent_ask", "agent_notify",
                     "agent_query_events", "agent_query_tool_detail", "wait_subagents",
                     "get_session_history", "semantic_search_history", "rename_session",
                     # 钩子副作用工具：闭包绑定 agent（写 _recap/registry/Turn.recap）——子 Agent 需重绑自身版本
                     "hook_write",
                     # spec 工具：闭包绑定创建时的 agent（主 Agent）——被子 Agent 继承会
                     # 跨 agent 串写状态（子 Agent 建 spec 会挂到主 Agent、气泡弹到主会话）。
                     # recall_spec 例外保留（2026-10-15 施工委托实锤）：纯只读查 spec 全文，
                     # 施工 worker 的兜底查询通道（主路径=施工牌 prompt 全文，2026-10-18 裁定）。
                     "create_spec", "commit_spec", "regenerate_spec", "list_specs"}


def _resolve_agent_id(existing: dict, name: str, agent_id: str) -> str:
    """子 agent 的唯一 id：显式 agent_id 优先（原样用）；否则 name / name_2… 避让已有键。
    因为多次 agent_prompt 同名 = 多个独立实例，id 必须唯一以区分各自嵌套的 session 目录。"""
    aid = (agent_id or "").strip()
    if aid:
        return aid
    aid = name
    i = 2
    while aid in existing:
        aid = f"{name}_{i}"
        i += 1
    return aid


def deliver_notice(target_agent, message: str, seed=None, on_event=None) -> bool:
    """把一条系统提示投给【指定 Agent 实例】并保证被看到（用户裁定 2026-10-15 从 agent_notify 抽出
    复用）：bg_task 完成回执按 owner 路由时调它——谁起的后台任务，回执投给谁。
    忙（run 中）→ 有 seed 走 push_message（保留 check_bg_task 合成工具记录，与主线同口径），
    否则 queue_user_message；空闲 → 起裸线程跑一轮直接消费（与 agent_notify 的空闲分支同款）。
    返回 True=已投递 / False=实例不可用（调用方兜底自收）。不抛异常。"""
    if target_agent is None or getattr(target_agent, "session", None) is None:
        return False
    aid = getattr(target_agent, "agent_id", "") or ""
    reg = getattr(target_agent, "registry", None)
    try:
        if getattr(target_agent, "busy", False):
            try:
                if isinstance(seed, dict):
                    target_agent.push_message(message, source="bg_task", seed=seed, wake=True)
                else:
                    target_agent.queue_user_message(message)
            except Exception:
                target_agent.queue_user_message(message)   # 兜底：至少把文本排进插话队列
            return True
        # 空闲唤醒：这条路没有 seed 通道 → 把种子里的结果正文并进消息（否则只有一句标题）
        _msg = message
        if isinstance(seed, dict):
            _res = str(seed.get("result", "") or "")
            if _res:
                _msg = f"{message}\n\n{_res}"
        if reg is not None and aid:
            try:
                reg.update_status(aid, "running")
            except Exception:
                pass

        def _run_sub(_a=target_agent, _t=_msg):
            _old = getattr(_a, "on_event", None)
            if on_event is not None:
                _a.on_event = on_event
            _dead = None
            try:
                _a.run(_t)
            except Exception as e:
                # 静默失败黑洞修复（2026-10-20 · 20049 worker-p_1b7df7e7 实锤）：线程异常死
                # 原被 except:pass 吞掉 + finally 标 done——主 Agent/用户完全无从得知"worker 消失"
                # 的真因。三路留痕：日志 + 主 Agent 事件流（WebUI 弹 error）+ registry 标 failed。
                _dead = e
                import traceback as _tb
                _err = (f"⚠️ 子 Agent 线程异常死亡（{getattr(_a, 'agent_id', '?')}）: "
                        f"{type(e).__name__}: {e}\n" + "\n".join(_tb.format_exc().splitlines()[-6:]))
                try:
                    _LOG.error(_err)
                except Exception:
                    pass
                try:
                    _a._emit({"type": "error", "text": _err})
                except Exception:
                    pass
            finally:
                _a.on_event = _old
                if reg is not None and aid:
                    try:
                        reg.update_status(aid, "failed" if _dead is not None else "done")
                    except Exception:
                        pass

        import threading as _th
        _th.Thread(target=_run_sub, daemon=True).start()
        return True
    except Exception:
        return False


class SubAgent:
    """一次性子 Agent：建实例 → prompt 跑一次 → 丢弃（不存任何 dict）。
    on_event 接主 Agent 的事件流，输出经主线程 _render_loop 渲染（CLI）/ broadcast（Web）。"""

    def __init__(self, name: str, model_name: str, system: str, tools: Toolbox,
                 on_event=None, max_steps: int = 50, token_budget: int = 0, session_dir=None,
                 registry=None, agent_id=None, caller_id=None, current_turn_only: bool = False,
                 assembly: dict = None):
        self.name = name
        self.model_name = model_name
        self.agent = Agent(system, tools, model_name=model_name,
                           enable_thinking=True, max_steps=max_steps,
                           token_budget=token_budget, verbose=False, on_event=on_event,
                           session_dir=session_dir, registry=registry,
                           agent_id=agent_id or (name or "_main_"))   # 构造即带正确 id —— Agent.__init__ 按 id 分流注册
        if agent_id:
            self.agent.session._asm_agent_id = agent_id   # assembly workflow 项的入参 agent_id
        # 复用模式：session 投影只含当前轮（历史轮不投影但完整归档）——多次派活不膨胀上下文
        if current_turn_only:
            self.agent.session.current_turn_only = True
        # assembly DSL v2：上下文装配清单（None=默认清单；来自 md 声明 + agent_prompt 参数覆盖）
        if assembly:
            self.agent.session.set_assembly_plan(assembly)
        # 子 Agent 默认不跑 before_turn 钩子（除非 .md assembly 显式列了 hooks）——
        # 纯函数型工人每轮跑历史/wiki 检索是重复浪费；主 Agent 保持默认开。
        self.agent.session.hooks_default_on = False
        # 注册绑定到子 Agent 自身的通信工具（替换从主 Agent 继承的、绑定到主 Agent 闭包的版本）
        comm_tools = make_communication_tools(self.agent)
        for t in comm_tools:
            self.agent.tools.register(t)
        # 注册表变化时刷新自己 comm 工具的 target_id enum（订阅与 registry entry 同生命周期，
        # 不额外增加内存——agent 实例本就被 entry.agent 引用常驻）
        if registry:
            registry.add_on_change(str(agent_id or name),
                                   lambda: _inject_agent_enums(self.agent, list(self.agent.tools)))
        # 会话工具同样重绑：钩子工作流（before_turn_retrieval 等）在子 Agent 里跑时，
        # get_session_history/semantic_search 必须查【子 Agent 自己的 session】，
        # 而不是继承自主 Agent 的闭包（那会查到主 Agent 的历史）
        from session_tools import make_session_tools
        for t in make_session_tools(self.agent):
            self.agent.tools.register(t)
        # 钩子副作用工具同样重绑：turn_end 钩子（recap_gen）里的 hook_write 必须
        # 写【子 Agent 自己】的 _recap/registry/Turn——不能继承主 Agent 闭包版本
        for t in make_hook_side_effects(self.agent):
            self.agent.tools.register(t)
        self.caller_id = caller_id or ""

    def prompt(self, text: str) -> str:
        """派一个任务，子 Agent 自主用工具完成，返回最终回复。过程事件经 on_event 回流。"""
        if self.agent.on_event:
            self.agent.on_event({"type": "system",
                                 "text": f"▸ [子 Agent '{self.name}' ({self.model_name}) 开始工作]"})
        self.agent.cumulative_tokens = 0   # 一次性实例，本就是 0；保留防御
        result = self.agent.run(text)
        if self.agent.on_event:
            self.agent.on_event({"type": "system", "text": f"▸ [子 Agent '{self.name}' 完成]"})
        return result or "(空回复)"


def _resolve_tools(agent, tools_str: str):
    """把 tools 字符串解析成 Toolbox：留空/all/* = 继承主 Agent 全部（排除子 Agent 管理工具）；
    否则逗号分隔只注册这些（仍排除管理工具）。返回 (toolbox, 说明)。"""
    all_main_tools = list(agent.tools)
    if not tools_str or tools_str.strip().lower() in ("all", "*", "default", "继承", "全部"):
        chosen = [t for t in all_main_tools if t.name not in _AGENT_TOOL_NAMES]
        return Toolbox(*chosen), f"继承全部({len(chosen)}个)"
    wanted = [w.strip() for w in tools_str.split(",") if w.strip()]
    chosen = [t for t in all_main_tools if t.name in wanted and t.name not in _AGENT_TOOL_NAMES]
    found = {t.name for t in chosen}
    missing = [w for w in wanted if w not in found and w not in _AGENT_TOOL_NAMES]
    note = f"仅{len(chosen)}个" + (f"，未找到:{missing}" if missing else "")
    return Toolbox(*chosen), note


def _agent_md_path(name: str):
    """[兼容别名] .agent/agents/<name> 定义文件路径（.yml 优先，无则 .md）。"""
    return _agent_def_path(name)


def _agent_def_path(name: str):
    """子 Agent 定义文件路径（三种形态，优先级从高到低）：
    · .agent/agents/<name>/<name>.yml —— 自包含目录形态（可带 <name>.md 人设与 tools/ 专属工具，
      用户提案 2026-09-27：一个 agent 一个目录，人设/装配/专属工具整体拷贝与分发）
    · .agent/agents/<name>.yml —— 平铺（v2 主形态）
    · .agent/agents/<name>.md —— 平铺（旧格式）
    名字非法返回 None。返回值不保证存在（调用方自行判 exists）。"""
    if not _NAME_RE.match(name or ""):
        return None
    d = WORKSPACE / _AGENT_DIR / "agents"
    yml_dir = d / name / f"{name}.yml"
    if yml_dir.exists():
        return yml_dir
    md_dir = d / name / f"{name}.md"
    if md_dir.exists():
        return md_dir
    yml = d / f"{name}.yml"
    if yml.exists():
        return yml
    return d / f"{name}.md"


def _svc_spec(v) -> tuple[str, str]:
    """services 声明值规范化：'命令'（旧形态）或 '命令 | reuse=<探测URL>'（2026-10-16 跨实例复用）。
    返回 (cmd, reuse_url)。行式单值保持 WebUI 编辑器/收藏全链路兼容（textarea 直读直写）。"""
    s = str(v).strip()
    for sep in ("| reuse=", "|reuse="):
        if sep in s:
            cmd, _, rest = s.partition("|")
            return cmd.strip(), rest.strip()[len("reuse="):].strip()
    return s, ""


def _probe_alive(url: str, timeout: float = 2.0) -> tuple[bool, str]:
    """服务存活探测（GET，2s 超时；参照 WebIDE 三态探测经验）：
    - 2xx/3xx = 活；
    - 4xx/5xx = 端口有人但非 2xx——判特征防复用"别人的服务"：响应 Content-Type 为 image/*
      （desktop-frame 帧端点特征）才算自己人；否则视为他人服务不复用；
    - 连接拒绝/超时/其它异常 = 死（拉起）。"""
    import urllib.error
    import urllib.request
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return True, f"HTTP {r.status}"
    except urllib.error.HTTPError as e:
        ct = ""
        try:
            ct = (e.headers or {}).get("Content-Type", "") or ""
        except Exception:
            pass
        if "image" in ct.lower():
            return True, f"HTTP {e.code} + image 特征"
        return False, f"HTTP {e.code} 非本服务特征（Content-Type={ct or '未知'}）"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def _ensure_agent_services(meta: dict, agent) -> dict:
    """yml 声明的依赖服务（2026-10-06 用户提案）：子 Agent 实例化时幂等拉起。
    形态：services: [{服务名: 命令}, ...]（列表 of 单键映射；dict 形态也认）。
    已存在同名服务（在跑）→ 跳过；命令 cwd=workspace（相对路径可用）。
    跨实例复用（2026-10-16 用户补充）：命令可带 "| reuse=<探测URL>" 后缀——拉起前先
    GET 探测（2s 超时）：已活 → 跳过拉起只复用（desktop-frame 帧服务全局单例：多 agt
    实例不重复拉；复用方不注册服务池条目——不拥有不 stop，只使用）；死才真拉起。
    返回 {"started": [...], "reused": [...]}（2026-10-16 技能装卸闭环：started=本次真拉起的
    ——skill_equip 记入装备登记，unload 只停自己拉起的；reused=探测复用的不拥有不停。
    子 Agent 调用点忽略返回值，零回归）。"""
    svcs = meta.get("services") or []
    if isinstance(svcs, dict):
        svcs = [svcs]
    started, reused = [], []
    for it in svcs:
        if not isinstance(it, dict):
            continue
        for sname, val in it.items():
            cmd, reuse_url = _svc_spec(val)
            try:
                if reuse_url:
                    alive, detail = _probe_alive(reuse_url)
                    if alive:
                        _LOG.info("子 Agent 依赖服务 %s: 复用已活服务 %s（%s）——不重复拉起", sname, reuse_url, detail)
                        reused.append(str(sname))
                        continue
                    _LOG.info("子 Agent 依赖服务 %s: 探测不可复用（%s）→ 拉起", sname, detail)
                r = agent.services.start(str(sname), cmd, str(agent.session.workspace)
                                         if getattr(agent.session, "workspace", "") else "",
                                         quiet_secs=120)   # 启动蜜月：秒退不吓 agent
                if str(r).startswith("[已存在同名服务]"):
                    pass   # 服务池里同名条目在跑（别人拉起的）——不拥有，不记 started
                else:
                    started.append(str(sname))
                _LOG.info("子 Agent 依赖服务 %s: %s", sname, str(r)[:80])
            except Exception as e:
                _LOG.warning("子 Agent 依赖服务 '%s' 拉起失败: %s", sname, e)
    return {"started": started, "reused": reused}


def _agent_own_tools(name: str, agent):
    """目录形态子 Agent 的专属工具：.agent/agents/<name>/tools/*.py
    （agt_register 约定同 tools/builtin——一次性任务脚本防御自动继承）。
    返回 Tool 列表（无目录/扫描失败 → []）。同名工具追加在全局工具之后（Toolbox 后注册胜出）。"""
    d = WORKSPACE / _AGENT_DIR / "agents" / name / "tools"
    if not d.is_dir():
        return []
    try:
        from script_tools import scan_script_tools
        return list(scan_script_tools(dirs=[d], agent=agent))
    except Exception as e:
        _LOG.warning("子 Agent '%s' 专属工具扫描失败: %s", name, e)
        return []
def _agent_skill_gear(name: str, meta: dict, agent):
    """yml `skills:` 字段装备（2026-10-16 用户裁定）：一个字段三效果——
    ① 技能包 tools/*.py 外置工具注册进子 Agent 工具箱（复用 agent 目录 tools/ 的发现管线，
       来源改为技能目录，工具协议不变）；
    ② SKILL.md 的 SOP 正文注入子 Agent system 尾（子 Agent 带着作业规程上岗，
       skill_* 工具族的手动 read_skill 流程免掉）；
    ③ SKILL.md frontmatter `assemblies:`/`services:` 声明聚合（plan p_76f6623b）——
       extras=[{position, action, anchor, items}]（装备时插段/挂钩子的静态口声明），
       services=[{名: 命令}]（技能自带依赖服务，调用方并入 _ensure_agent_services）。
    返回 dict(tools, sop, extras, services, error)；error 非空 = 声明了不存在的技能名
    或装配声明坏（清晰报错，调用方中断派活，不静默丢弃）。
    技能寻址走 _resolve_skill（本地 .agent/skills/ 优先，全局 ~/.agt/skills/ 已激活的也认）。"""
    names = meta.get("skills") or []
    if isinstance(names, str):   # 宽松：skills: "a,b" 字符串形态也认
        names = [x.strip() for x in names.split(",") if x.strip()]
    names = [str(x).strip() for x in names if str(x).strip()]
    empty = {"tools": [], "sop": "", "extras": [], "services": [], "error": ""}
    if not names:
        return empty
    from agent_config import (_resolve_skill, _split_frontmatter, load_skills_index,
                              load_skill_assemblies, fill_default_hook_items)
    tools, chunks, missing, extras, services = [], [], [], [], []
    for sn in names:
        d, _scope = _resolve_skill(sn, WORKSPACE)
        if d is None:
            missing.append(sn)
            continue
        try:
            fm, sop = _split_frontmatter((d / "SKILL.md").read_text(encoding="utf-8", errors="ignore"))
            if (sop or "").strip():
                chunks.append(f"## 装备技能：{sn}\n\n{sop.strip()}")
        except Exception as e:
            _LOG.warning("子 Agent '%s' 装备技能 '%s' SKILL.md 读取失败: %s", name, sn, e)
            fm = {}
        td = d / "tools"
        if td.is_dir():
            try:
                from script_tools import scan_script_tools
                tools.extend(scan_script_tools(dirs=[td], agent=agent))
            except Exception as e:
                _LOG.warning("子 Agent '%s' 装备技能 '%s' tools/ 扫描失败: %s", name, sn, e)
        # ③ 装配声明（静态口）+ 钩子缺省 ass = 技能包 workflows/（fill_default_hook_items 共用补全）
        _, d_ex, d_err = load_skill_assemblies(sn, WORKSPACE)
        if d_err:
            return {**empty, "error": f"技能 '{sn}' 装配声明错误：" + "；".join(d_err)}
        fill_default_hook_items(d_ex, d)
        for _ex in d_ex:
            _ex["_skill_src"] = sn   # 来源标记（2026-10-16 装卸闭环）——插段/钩子项据此撤销
        extras.extend(d_ex)
        svcs = fm.get("services")
        if svcs:
            services.extend(svcs if isinstance(svcs, list) else [svcs])
    if missing:
        known = ", ".join(s["name"] for s in load_skills_index(WORKSPACE)) or "(无已安装技能)"
        return {**empty, "error": f"未知技能: {', '.join(missing)}（已安装: {known}）——检查 .agent/skills/<名>/SKILL.md 是否存在"}
    return {"tools": tools, "sop": ("\n\n".join(chunks) if chunks else ""),
            "extras": extras, "services": services, "error": ""}


def _apply_extra_assemblies(base_plan, base_hooks, extras):
    """extra_assemblies 统一应用（plan p_76f6623b 铁律②：子 Agent 与主 Agent 走同一插入逻辑）。
    base_plan/base_hooks：宿主装配基线（plan None = 默认清单物化副本——动作项原位插入需要实体清单）。
    返回 (plan, hooks, errors)：errors 非空 = 锚段不在宿主清单（清晰报错给装备方，不静默丢弃）。
    插段语义：锚段前/后插入 ass 项；多技能同锚按装备顺序稳定追加（先装备的先插）。
    钩子语义：hook:<位> 的项并入宿主 hook_specs[位]（workflow 项带 dir=技能包 workflows/，
    装备时冻结路径），并确保 hooks 段开关打开（hook_specs 才会被 _run_hooks 消费）。
    来源标记（2026-10-16 装卸闭环）：extra 声明可带 _skill_src=技能名（聚合方/装备方打标），
    落到每个插入项上——skill_unequip / replace 幂等按标记撤销（_revoke_skill_items）。
    冻结铁律：本函数只在装备时（派活链/ skill_equip）调用一次，产物作为静态清单快照
    set_assembly_plan 进宿主 session，逐轮不再重算。"""
    from session import Session   # 延迟 import（session 不依赖 multiagent，安全）
    plan = list(base_plan) if base_plan else [dict(it) for it in Session._DEFAULT_ASSEMBLY_PLAN]
    hooks = {k: list(v) for k, v in (base_hooks or {}).items()}
    errors = []
    for ex in extras or []:
        action, anchor, items = ex["action"], ex["anchor"], ex["items"]
        pos = ex.get("position") or f"{action}:{anchor}"
        src = ex.get("_skill_src") or ""
        tag = {"_xsrc": pos, **({"_skill_src": src} if src else {})}
        if action in ("before", "after"):
            idx = next((i for i, it in enumerate(plan)
                        if it.get("kind") == "seg" and it.get("name") == anchor), None)
            if idx is None:
                errors.append(f"锚段 '{anchor}' 不在宿主装配清单（{pos}）——锚必须是宿主已装配的段名")
                continue
            if action == "after":
                at = idx + 1
                while at < len(plan) and plan[at].get("_xsrc") == pos:
                    at += 1   # 跳过同锚簇（先装备的先插——稳定追加，不插到已装备技能前面）
            else:
                at = idx   # before 紧贴锚插入：锚位置随每次插入后移，后装备的自然排在前装备之后（稳定）
            plan[at:at] = [dict(dict(it), **tag) for it in items]
        elif action == "hook":
            if items:
                htag = {"_skill_src": src} if src else {}
                hooks.setdefault(anchor, []).extend(dict(dict(it), **htag) for it in items)
            if not any(it.get("kind") == "seg" and it.get("name") == "hooks" for it in plan):
                plan.append({"kind": "seg", "name": "hooks"})   # hooks 段开关打开（与 yml hooks 声明同款派生）
    return plan, hooks, errors


def _revoke_skill_items(plan, hooks, skill_name: str):
    """按 _skill_src 标记从装配快照撤销某技能的插段/钩子（skill_unequip 与 replace 幂等共用）。
    plan/hooks 传 None（宿主未物化快照）合法——对应侧原样返回、计数 0。
    只删标记匹配项：宿主原有项与其它技能/来源的插段不碰（'正在服务的其它来源不碰'）。
    钩子位撤空则删位（空位无消费语义）。返回 (plan2, hooks2, 撤段数, 撤钩子数)。
    注意：返回的 plan 需经 set_assembly_plan 重新冻结（清 once 缓存）——不重跑整体装配，
    未装备技能的其余项原样保留（快照仍冻结有效）。"""
    n_seg = n_hook = 0
    if plan is not None:
        keep = [it for it in plan
                if not (isinstance(it, dict) and it.get("_skill_src") == skill_name)]
        n_seg = len(plan) - len(keep)
        plan = keep
    if hooks:
        hooks2 = {}
        for pos, items in hooks.items():
            items = items or []
            keep = [it for it in items
                    if not (isinstance(it, dict) and it.get("_skill_src") == skill_name)]
            n_hook += len(items) - len(keep)
            if keep:
                hooks2[pos] = keep
        hooks = hooks2
    return plan, hooks, n_seg, n_hook


# assembly DSL v2：有序装配清单。合法段名（8 段）：必装段 system/user_message/steps 恒装
# （未列出时按默认顺序相对位置自动补插）；可关段 rules/history/ltm/tail 未列出即不装；
# hooks 不占投影位置（产出绑在当前轮内），仅作开关——子 Agent 未列出时默认 off。
# 动作项：file/dir/cmd/text 默认每轮求值（mtime 热改生效），workflow 默认 once 实例固化。
_ASSEMBLY_SEGS = {"system", "rules", "history", "ltm", "user_message", "hooks", "steps", "recent_file", "tail"}
_ASSEMBLY_TOGGLES = {"rules", "history", "ltm", "hooks", "tail"}
_ASSEMBLY_ACTIONS = ("file", "dir", "cmd", "workflow", "text", "func", "tool", "image_feed")
_ASSEMBLY_MUST = ("system", "user_message", "steps")
# 段的默认相对顺序（必装段自动补插的位置基准；= session._DEFAULT_ASSEMBLY_PLAN 的段序）
_ASSEMBLY_SEG_ORDER = ("system", "rules", "history", "ltm", "user_message", "steps", "tail")
_ASSEMBLY_HISTORY_MODES = ("tiered", "window", "full")
# steps 段的尾段注入模式（用户提案 2026-09-02，演进自单点全局）：reminder=求值合并→
# <system-reminder> 包裹并入末条 content（默认，三区重构语义）；reasoning=作为思考链一部分注入
# ——user 之后最后一条 assistant 的 reasoning_content 前缀（"当前状态：{result}\n"+原文；
# s0 无 assistant 回退 reminder）。
# 【粒度演进 2026-09-03】从 step 一处全局 → steps 后每个动作项各自带 pose（mode）：
# every 动作项（text/func/tool/file/dir/cmd/workflow）可声明 mode: reasoning（思考链）或
# reminder（并入正文，默认）；引擎按 pose 分两批合入（reasoning 合并一次注入思考链，
# reminder 合并一次并入末条 content）——兼顾"每段独立选姿势"与"同姿势合批缓存友好"。
# steps=reasoning（seg 级）保留为"未标 pose 项整体默认 reasoning"的向后兼容写法。
_ASSEMBLY_STEPS_MODES = ("reminder", "reasoning")
_ACTION_MODES = ("reminder", "reasoning")   # 动作项 pose 合法值（与 steps 段共用语义）
_HOOK_POSITIONS = ("before_turn", "before_tool", "after_tool", "before_answer", "turn_end")


def _hook_item_from_str(s: str) -> dict:
    """'workflow: name | async' / 'cmd: ...' / 'emit: ...' → 项 dict。
    尾随 '| async' / '| optional' 等标志解析成 flag 键。"""
    flags = {}
    segs = [p.strip() for p in str(s).split("|")]
    main = segs[0]
    for f in segs[1:]:
        fk = f.strip().lower()
        if fk:
            flags[fk] = True
    if ":" in main:
        kind, _, val = main.partition(":")
        kind, val = kind.strip().lower(), val.strip()
        return {"kind": kind, "value": val, **flags}
    # 无冒号：裸工作流名
    return {"kind": "workflow", "value": main.strip(), **flags}


def _parse_agent_fallback(meta: dict):
    """frontmatter/yml 的 fallback 字段 → (chain: list[str], policy: str|None) 或 None（未声明）。
    形态：'a, b'（串=只有链）/ [a, b]（list）/ {chain: ..., policy: sticky|reset}（完整声明）。
    显式空串/空 list = 关回退（与全局 settings 隔离）；未声明（None）= 继承全局 settings
    （/model、WebUI 配的——语义上是主 Agent 的用户配置）。"""
    raw = meta.get("fallback")
    if raw is None:
        return None
    policy = None
    if isinstance(raw, dict):
        chain = raw.get("chain", [])
        policy = str(raw.get("policy") or "").strip().lower() or None
    else:
        chain = raw
    if isinstance(chain, str):
        chain = [m.strip() for m in chain.split(",") if m.strip()]
    else:
        chain = [str(m).strip() for m in (chain or []) if str(m).strip()]
    if policy not in ("sticky", "reset", None):
        _LOG.warning("fallback policy '%s' 未知（sticky|reset），按默认", policy)
        policy = None
    return chain, policy


def _apply_declared_fallback(sub_agent, fb):
    """子 Agent 声明式回退链统一应用（2026-09-16 修）：实例链（非 react 调用）+ react 链一并设。
    此前三处调用点（新建/复用/复活）只调了 llm.set_fallback —— agent._declared_fallback
    恒 None → react 链退化为 [当前模型] 单元素，主模型 429 时子 Agent 直接"回退链耗尽"炸轮
    （用户实测 vision_17：tried=['glm-official-flash']），yml 里声明的 fallback 形同虚设。"""
    if not fb:
        return
    try:
        sub_agent.llm.set_fallback(fb[0], fb[1])
    except Exception:
        pass
    try:
        sub_agent._declared_fallback = list(fb[0])
    except Exception:
        pass


def _parse_hooks(meta: dict) -> dict:
    """frontmatter/yml 的 hooks 字段 → {hook位置: [{kind, value, async...}]}。
    项格式：'workflow: x' / 'x'（裸名=workflow）/ 'cmd: ...' / 'emit: ...'，
    尾随 '| async' 等标志。未知位置忽略+日志。无声明返回 {}。"""
    raw = meta.get("hooks")
    if not raw or not isinstance(raw, dict):
        return {}
    out = {}
    for hook, items in raw.items():
        if hook not in _HOOK_POSITIONS:
            _LOG.warning("hooks 未知位置 '%s'（合法：%s），已忽略", hook, list(_HOOK_POSITIONS))
            continue
        lst = out.setdefault(hook, [])
        if items is None:
            continue
        src = items if isinstance(items, list) else [items]
        for it in src:
            if isinstance(it, dict):
                # dict 项两类来源：管理页（/agents#edit）保存的 {workflow: x, async: true}；
                # agent_prompt 注入默认的 {kind: workflow, value: x, async: true}。
                # kind 键取值走字符串解析；其余真值兄弟键（async 等）→ 标志位——
                # 曾静默丢弃 async 键：turn_end 钩子按同步跑，阻塞轮结束+注入结果
                # （用户实测抓到，2026-09-12）。
                item = None
                for k, v in it.items():
                    if k in ("workflow", "cmd", "emit"):
                        item = _hook_item_from_str(f"{k}: {v}")
                        break
                if item is None:
                    item = _hook_item_from_str(str(it.get("value") or
                                                   (next(iter(it.values())) if it else "")))
                for k, v in it.items():
                    if k not in ("workflow", "cmd", "emit", "kind", "value") and v:
                        item[str(k).strip().lower()] = True   # async: true → {"async": True}
            else:
                item = _hook_item_from_str(it)
            if item.get("value"):
                lst.append(item)
    return out


def _parse_tool_expr(val: str):
    """tool 项的值 'read_file(AGENTS.md)' / 'concat_files(.agent/rules/*.md)' → (工具名, 参数字符串)。
    无括号（裸工具名单参）：参数为空。解析失败返回 None。"""
    val = str(val).strip()
    m = re.match(r"^([A-Za-z_]\w*)\s*\((.*)\)$", val)
    if m:
        return m.group(1), m.group(2).strip()
    # 裸工具名（无参）
    if re.match(r"^[A-Za-z_]\w*$", val):
        return val, ""
    return None


def _asm_item_from_str(s: str):
    """'seg' / 'seg|optional' / 'history=window' / 'tool: read_file(x)' → 项 dict 或 None。
    行内描述 '// 说明文字'：不参与装配语义，随 item 携带（agents_summary 优先展示，无则回退内置文案）。"""
    raw = str(s).strip()
    desc = ""
    if "//" in raw:
        raw, _, d = raw.partition("//")
        raw, desc = raw.strip(), d.strip()
    # tool: 形式（动作项）
    if raw.startswith("tool:"):
        val = raw[len("tool:"):].strip()
        parsed = _parse_tool_expr(val)
        if parsed:
            tname, targs = parsed
            item = {"kind": "tool", "tool": f"{tname}({targs})", "tool_name": tname, "tool_args": targs,
                    "timing": "turn"}
            if desc:
                item["desc"] = desc
            return item
        return None
    seg_raw, _, tail = raw.partition("|")          # 尾随 '|optional' 等标志（optional=真语义：默认不装，=on 打开）
    opt = tail.strip().lower() == "optional"
    seg = seg_raw.strip()
    mode = None
    if "=" in seg:
        seg, _, mode = seg.partition("=")
        seg, mode = seg.strip(), mode.strip().lower()
        if seg == "history" and mode in ("on", "true", "1", "开"):
            mode = None
    if seg in _ASSEMBLY_SEGS:
        if seg == "history" and mode and mode not in _ASSEMBLY_HISTORY_MODES:
            _LOG.warning("assembly history 模式 '%s' 未知（合法：%s），按默认处理", mode, list(_ASSEMBLY_HISTORY_MODES))
            mode = None
        if seg == "steps" and mode and mode not in _ASSEMBLY_STEPS_MODES:
            _LOG.warning("assembly steps 模式 '%s' 未知（合法：%s），按默认 reminder 处理", mode, list(_ASSEMBLY_STEPS_MODES))
            mode = None
        item = {"kind": "seg", "name": seg}
        if opt:
            item["opt"] = True   # 默认不装配：messages_for_llm 跳过；agent_prompt assembly="seg=on" 清此标记打开
        if mode:
            item["mode"] = mode
        if desc:
            item["desc"] = desc
        return item
    if seg:
        _LOG.warning("assembly 含未知段名 '%s'（合法：%s + 动作 file/dir/cmd/workflow/text），已忽略",
                     seg, sorted(_ASSEMBLY_SEGS))
    return None


def _asm_timing(kind: str, d: dict) -> str:
    """动作项求值时机：every: turn|once / once: true 显式覆盖；默认 file/dir/cmd/text=turn、workflow=once。"""
    ev = str(d.get("every") or "").strip().lower()
    if ev in ("turn", "once"):
        return ev
    if str(d.get("once", "")).strip().lower() in ("true", "1", "yes"):
        return "once"
    return "once" if kind == "workflow" else "turn"


def _asm_item_from_dict(d: dict):
    """{file: path} / {dir: path} / {cmd: str} / {workflow: name} / {text: str} / {func: name()}
    / {tool: read_file(x)} [+ every/once] 或 {history: window} 简写 → 项 dict 或 None。
    {seg: "段名[|optional][=mode]"}（2026-09-02 兼容——声明书写的友好形态：段名做值而非做键，
    值复用字符串解析全语义——用户复盘 seg: 形态此前被静默丢弃）。
    动作项 pose（2026-09-03 粒度演进）：dict 支持 `mode: reminder|reasoning`（并入正文/注入
    思考链，默认 reminder）——steps 后每个动作项各自选择注入姿势，引擎按 pose 分两批合入。"""
    # {seg: "steps=reasoning"}：段名做值——委托字符串解析（全语义：|optional/=mode//描述）
    if d.get("seg"):
        return _asm_item_from_str(str(d["seg"]))
    for k in _ASSEMBLY_ACTIONS:
        if k in d and d[k]:
            raw_val = d[k]
            val = raw_val.strip() if isinstance(raw_val, str) else raw_val
            # 非 str 值原样保留（2026-10-16 image_feed: {tool: expr} dict 形态——帧提供者抽象，
            # plan p_76f6623b）；str 值 strip 行为与历史一致，零回归。
            if k == "func":
                # {func: load_models()} → 剥掉尾随 ()
                val = val.rstrip("()").strip()
            item = {"kind": k, k: val, "timing": _asm_timing(k, d)}
            if k == "tool":
                parsed = _parse_tool_expr(val)
                if parsed:
                    item["tool_name"], item["tool_args"] = parsed
                    item["tool"] = val
            _d = str(d.get("desc") or "").strip()
            if _d:
                item["desc"] = _d   # 管理页/手写声明的动作项行内描述（与段名项 '// 说明' 同语义）
            _m = str(d.get("mode") or "").strip().lower()
            if _m in _ACTION_MODES:
                item["mode"] = _m   # 动作项 pose（并入正文/注入思考链）
            return item
    for seg in _ASSEMBLY_SEGS:
        if seg in d and d[seg]:
            val = str(d[seg]).strip().lower()
            item = {"kind": "seg", "name": seg}
            if seg == "history" and val in _ASSEMBLY_HISTORY_MODES:
                item["mode"] = val
            if seg == "steps" and val in _ASSEMBLY_STEPS_MODES:
                item["mode"] = val   # {steps: reasoning}——尾段注入模式（reminder=默认）
            return item
    return None


def _normalize_assembly_plan(items: list) -> list:
    """归一化：必装段（system/user_message/steps）未列出时按 _ASSEMBLY_SEG_ORDER 的相对位置
    自动补插（保持与默认投影顺序一致的插入点）。hooks 移除（不占位置，仅开关派生）。"""
    plan = [it for it in items if not (it.get("kind") == "seg" and it.get("name") == "hooks")]
    present = {it["name"] for it in plan if it.get("kind") == "seg"}
    for seg in _ASSEMBLY_MUST:
        if seg in present:
            continue
        # 插入点：清单里最后一段的默认序 < 本段的默认序 → 插其后；无 → 插最前
        pos = 0
        for i, it in enumerate(plan):
            if it.get("kind") == "seg" and it.get("name") in _ASSEMBLY_SEG_ORDER \
                    and _ASSEMBLY_SEG_ORDER.index(it["name"]) < _ASSEMBLY_SEG_ORDER.index(seg):
                pos = i + 1
        plan.insert(pos, {"kind": "seg", "name": seg})
        present.add(seg)
    return plan

def _parse_assembly(meta: dict) -> list:
    """frontmatter 的 assembly 字段 → 有序装配清单（v2）。
    元素：段名 'name' / 'name|optional' / 'history=window|tiered|full'；
    动作 {file: path} / {dir: path} / {cmd: 命令} / {workflow: 名} / {text: 文本}
    [+ every: turn|once / once: true]；YAML list 或逗号分隔串（仅段名场景）。
    语义：【清单即装配顺序，只装列出的段】；必装段自动补插。无声明返回 None（默认清单全装）。"""
    raw = meta.get("assembly")
    if not raw:
        return None
    items = []
    if isinstance(raw, list):
        for it in raw:
            if isinstance(it, dict):
                x = _asm_item_from_dict(it)
                if x:
                    items.append(x)
            else:
                x = _asm_item_from_str(it)
                if x:
                    items.append(x)
    else:
        for s in str(raw).split(","):
            x = _asm_item_from_str(s)
            if x:
                items.append(x)
    if not items:
        return None
    return _normalize_assembly_plan(items)


def _apply_assembly_overrides(base_plan: list, overrides_str: str) -> tuple:
    """agent_prompt 的 assembly 参数覆盖 base_plan（.md 声明的清单）。返回 (plan, 提示语)。
    语义（白名单）：显式列段名/动作名 = 打开（补插到清单，保持默认序）；seg=off = 关闭（移除）。
    支持 'history=window' 改历史投影方式。段开关优先级：列段名 on > 已有 > off。"""
    plan = list(base_plan) if base_plan else []
    notes, add, offs = [], {}, set()
    for part in (overrides_str or "").split(","):
        part = part.strip()
        if not part or "=" not in part:
            continue
        seg, _, val = part.partition("=")
        seg, val = seg.strip(), val.strip().lower()
        cleaned = seg.split("|", 1)[0]
        if cleaned == "steps" and val in _ASSEMBLY_STEPS_MODES:
            add["steps"] = val   # steps 模式覆盖（steps=reasoning：尾段以思考链注入）——须在必装检查前拦截
            continue
        if cleaned in ("system", "user_message", "steps"):
            notes.append(f"'{cleaned}' 必装不可关")
            continue
        if cleaned in ("file", "dir", "cmd", "workflow", "text") and cleaned in _ASSEMBLY_ACTIONS:
            notes.append(f"动作项不能在参数里新增（仅段开关/模式可覆盖），'{cleaned}' 忽略")
            continue
        if cleaned == "history" and val in _ASSEMBLY_HISTORY_MODES:
            add["history"] = val   # 模式覆盖（同时视为打开）
            continue
        if seg not in _ASSEMBLY_TOGGLES:
            notes.append(f"'{seg}' 未知段名" if seg else "")
            continue
        if val in ("off", "false", "0", "关"):
            offs.add(seg)
        elif val in ("on", "true", "1", "开"):
            add[seg] = None
    # off 剔除（参数显式 off 覆盖 md 声明/已有同名段）
    plan = [it for it in plan if not (it.get("kind") == "seg" and it.get("name") in offs)]
    # add 打开：history 带模式 → 已有段改模式；其余可关段补插（保持默认序）；hooks 只派生开关不进清单
    for seg, mode in add.items():
        if seg == "hooks":
            continue
        existing = next((it for it in plan if it.get("kind") == "seg" and it.get("name") == seg), None)
        if existing is not None:
            existing.pop("opt", None)   # optional 段（默认关）被 =on 显式打开：清标记
            if mode:
                existing["mode"] = mode
            continue
        if seg in _ASSEMBLY_MUST and not mode:
            continue   # 必装段无需"打开"；但带 mode 覆盖时（steps=reasoning）须插入清单——否则投影走默认清单丢 mode
        pos = 0
        for i, it in enumerate(plan):
            if it.get("kind") == "seg" and it.get("name") in _ASSEMBLY_SEG_ORDER \
                    and _ASSEMBLY_SEG_ORDER.index(it["name"]) < _ASSEMBLY_SEG_ORDER.index(seg):
                pos = i + 1
        plan.insert(pos, {"kind": "seg", "name": seg, **({"mode": mode} if mode else {})})
    return plan, ("；assembly 参数：" + "，".join(notes) if notes else "")


def make_communication_tools(agent) -> list:
    """生成绑定到指定 Agent 的通信工具（agent_ask / agent_notify / agent_query / list_team）。
    主 Agent 和子 Agent 都用这个——各自绑定到自身的 agent 实例，确保 agent_id 正确。"""
    reg = getattr(agent, "registry", None)

    def _resolve_target(target_id: str):
        """查找目标 Agent。若 agent=None（从磁盘恢复的历史 Agent），尝试 lazy load session。
        返回 (session, llm) 或 None。"""
        entry = reg.lookup(target_id) if reg else None
        if entry is None:
            return None
        if entry.agent is not None:
            return entry.agent.session, entry.agent.llm
        # agent=None：从磁盘 lazy load
        sdir = getattr(agent.session, "session_dir", None)
        if not sdir:
            return None
        meta_path = Path(sdir) / "agents" / target_id / "meta.json"
        if not meta_path.exists():
            return None
        try:
            from session import Session
            sub_session = Session.load(str(meta_path), llm=agent.llm,
                                       workspace=agent.session.workspace)
            return sub_session, agent.llm
        except Exception as e:
            _LOG.warning("lazy load 子 Agent %s session 失败: %s", target_id, e)
            return None

    def list_team() -> str:
        """列出当前所有活跃的 Agent（主 Agent + 运行中的子 Agent），含它们的 agent_id、名称、模型、任务和状态。
        用于了解当前团队构成，与队友通信时需要知道对方的 agent_id。"""
        if not reg:
            return "(多 Agent 通信未启用：无 registry)"
        return reg.format_team(exclude_id=agent.agent_id, active_window=False)   # 工具查询=全量（含隐藏的历史实例）

    def agent_ask(target_id: str, question: str) -> str:
        """向另一个活跃 Agent 发起无状态询问：用对方的上下文 + 你的问题调用其 LLM，返回回答。
        被询问的 Agent 不会记录这次询问（其 session 不变），适合快速获取信息而不打扰对方。
        target_id: 目标 Agent 的 agent_id（用 list_team 查看）；question: 要问的问题。"""
        if not reg:
            return "(多 Agent 通信未启用：无 registry)"
        entry = reg.lookup(target_id)
        if entry is None:
            return f"[未找到] agent_id='{target_id}' 不在注册表中（可能已退出）。用 list_team 查看当前活跃 Agent。"
        reg.touch(target_id)   # 通信即交互（团队投影活跃窗口）
        try:
            agent.recap_interact("a:" + target_id)   # 互动清脏（用户提案 2026-09-30）
        except Exception:
            pass
        target_agent = entry.agent
        if target_agent is None:
            return f"[错误] '{target_id}' 的 Agent 实例不可用"
        try:
            msgs = list(target_agent.session.messages_for_llm())
            msgs.append({"role": "user", "content": f"[来自队友 '{agent.agent_id}' 的询问] {question}"})
            # 不装配工具（用户提案 2026-10-08）：agent_ask 是纯询问立刻要进展——克隆对方
            # 历史发 LLM 请求时带工具会诱导它想调工具（本调用无执行环境，调了也是空转）。
            # tools=[] 透传 _build_kwargs 覆盖实例工具表，纯对话问答。
            resp = target_agent.llm.chat(msgs, tools=[])
            answer = resp.content or "(对方返回空回答)"
            return f"[{target_id} 回答] {answer}"
        except Exception as e:
            return f"[询问失败] {type(e).__name__}: {e}"

    def agent_notify(target_id: str, message: str) -> str:
        """向另一个活跃 Agent 发送有状态提示，并保证被看到：
        忙（running/busy）→ 插话入队，对方下一步边界看到（与用户插话同机制，落盘）；
        空闲 → 直接触发一轮 run 消费该提示（用户裁定 2026-09-24：notify 的本意是
                "让对方看到"——空闲时没人会消费队列，唤醒一轮才是完整语义；与 WS
                直连路径（_handle_user_input 空闲分支）同构：消息作为本轮 user_message）。
        target_id: 目标 agent_id；message: 提示内容。"""
        if not reg:
            return "(多 Agent 通信未启用：无 registry)"
        entry = reg.lookup(target_id)
        if entry is None:
            return f"[未找到] agent_id='{target_id}' 不在注册表中。用 list_team 查看当前活跃 Agent。"
        reg.touch(target_id)   # 通信即交互（团队投影活跃窗口）
        target_agent = entry.agent
        if target_agent is None:
            return f"[错误] '{target_id}' 的 Agent 实例不可用"
        _msg = f"[来自队友 '{agent.agent_id}' 的提示] {message}"
        if entry.status == "running" or getattr(target_agent, "busy", False):
            try:
                target_agent.queue_user_message(_msg)
                return f"✅ 已向 '{target_id}' 插话排队，对方下一步边界会看到。"
            except Exception as e:
                return f"[发送失败] {type(e).__name__}: {e}"
        # 空闲 → 唤醒一轮直接消费（裸线程与 agent_prompt 的 _bg 同款：子 Agent 独立实例，
        # 与主 Agent run 不共享；事件流临时接通 broadcast（带 agent_id 分发），跑完恢复
        # 并回落 registry 状态）。
        reg.update_status(target_id, "running")
        def _run_sub(_a=target_agent, _t=_msg):
            _old = getattr(_a, "on_event", None)
            _a.on_event = agent.on_event
            _dead = None
            try:
                _a.run(_t)
            except Exception as e:
                # 静默失败黑洞修复（2026-10-20 · 20049 worker-p_1b7df7e7 实锤）：与 deliver_notice
                # 同款——吞异常 + 标 done 让"worker 消失"无从追因。三路留痕 + failed 状态。
                _dead = e
                import traceback as _tb
                _err = (f"⚠️ 子 Agent 线程异常死亡（{getattr(_a, 'agent_id', target_id)}）: "
                        f"{type(e).__name__}: {e}\n" + "\n".join(_tb.format_exc().splitlines()[-6:]))
                try:
                    _LOG.error(_err)
                except Exception:
                    pass
                try:
                    _a._emit({"type": "error", "text": _err})
                except Exception:
                    pass
            finally:
                _a.on_event = _old
                try:
                    reg.update_status(target_id, "failed" if _dead is not None else "done")
                except Exception:
                    pass
        import threading as _th
        _th.Thread(target=_run_sub, daemon=True).start()
        return f"🔔 '{target_id}' 空闲——已唤醒一轮直接处理该提示（不再排队悬挂）。"

    def agent_query_events(target_id: str, count: int = 5) -> str:
        """查询另一个活跃 Agent 的最近 N 条对话事件（只读）：每轮的用户消息摘要 + 工具调用名 + 回答摘要。
        用于了解队友的进展。target_id: 目标 agent_id；count: 查最近几轮（默认 5）。"""
        if not reg:
            return "(多 Agent 通信未启用：无 registry)"
        resolved = _resolve_target(target_id)
        if resolved is None:
            return f"[未找到或无法加载] agent_id='{target_id}'。用 list_team 查看可用 Agent。"
        if reg:
            reg.touch(target_id)   # 通信即交互（团队投影活跃窗口）
            try:
                agent.recap_interact("a:" + target_id)   # 互动清脏（用户提案 2026-09-30）
            except Exception:
                pass
        target_session, _ = resolved
        try:
            turns = target_session.turns
            if not turns:
                return f"[{target_id}] 暂无对话历史"
            recent = turns[-max(1, min(count, 20)):]
            lines = [f"[{target_id}] 最近 {len(recent)} 轮："]
            # count=1 视为"查完整回复"模式：answer 放宽到 4000 字（正常浏览模式仍 100 字摘要）
            full_mode = (count == 1)
            for t in recent:
                user = (t.user_message or "")[:60].replace("\n", " ")
                answer = (t.answer or "")[(0 if full_mode else slice(0, 100))].replace("\n", " ") if full_mode else (t.answer or "")[:100].replace("\n", " ")
                if full_mode and len(t.answer or "") > 4000:
                    answer = (t.answer or "")[:4000] + f"…(+{len(t.answer) - 4000}字)"
                tools = []
                for s in t.steps:
                    for tc in s.tool_calls:
                        name, _, _ = target_session.toollog.view(tc.call_id)
                        tools.append(f"{tc.call_id}: {name}")
                tool_str = ", ".join(tools[:8]) if tools else "(无工具)"
                lines.append(f"  用户: {user}")
                lines.append(f"  工具: {tool_str}")
                lines.append(f"  回答: {answer}")
            return "\n".join(lines)
        except Exception as e:
            return f"[查询失败] {type(e).__name__}: {e}"

    def agent_query_tool_detail(target_id: str, call_id: str) -> str:
        """查询另一个活跃 Agent 的某次工具调用完整详情（只读）：工具名、入参、完整结果。
        target_id: 目标 agent_id；call_id: 工具调用 id（如 'c7'，从 agent_query_events 的工具列表或对方上下文中获取）。"""
        if not reg:
            return "(多 Agent 通信未启用：无 registry)"
        resolved = _resolve_target(target_id)
        if resolved is None:
            return f"[未找到或无法加载] agent_id='{target_id}'。用 list_team 查看可用 Agent。"
        if reg:
            reg.touch(target_id)   # 通信即交互（团队投影活跃窗口）
            try:
                agent.recap_interact("a:" + target_id)   # 互动清脏（用户提案 2026-09-30）
            except Exception:
                pass
        target_session, _ = resolved
        try:
            name, args, result = target_session.toollog.view(call_id)
            import json as _j
            args_s = _j.dumps(args, ensure_ascii=False, indent=2)
            result = result or "(空)"
            if len(result) > 2000:
                result = result[:2000] + f"...(+{len(result) - 2000}字)"
            return f"[{target_id} · {call_id}] 工具: {name}\n入参:\n{args_s}\n结果:\n{result}"
        except Exception as e:
            return f"[查询失败] {type(e).__name__}: {e}"

    if not reg:
        return []
    return [Tool(list_team), Tool(agent_ask), Tool(agent_notify),
            Tool(agent_query_events), Tool(agent_query_tool_detail)]


# recap 错误特征：LLM 端点挂掉/回退链耗尽时 run_hook 会把错误文本当 result 返回——
# 不污染 recap（保持旧值）。从 agent.py 的 _async_hook 引擎侧回写迁移而来（见 make_hook_side_effects）
_RECAP_ERR_MARKS = ("APIStatusError", "APIConnectionError", "APITimeoutError",
                    "RateLimitError", "Error code:", "执行失败", "Traceback",
                    "[工作流", "出错]", "BadRequestError")


def make_hook_side_effects(agent) -> list:
    """钩子工作流的副作用写入工具（hidden，仅 plugin 节点调用）。
    用户提案：回写从引擎特判（_async_hook 的 recap 分支 + meta.recap 契约标志）移到工作流数据流——
    多个 turn_end 钩子共存时"以谁为准"由工作流显式决定（谁调 hook_write 谁负责，后写覆盖），
    而不是引擎靠 name 兜底猜。工作流经 hook_ctx（start 的 object 输入，引擎注入整袋上下文
    含 turn_idx）拿到轮号等元信息，组装 payload 后调本工具。
    payload 键值分派：{"action":"set_recap","value":"一句话","turn":轮号} → 三落点回写。"""
    import json as _json

    def hook_write(payload: dict) -> str:
        """钩子副作用写入。payload={"action":"set_recap","value":"≤60字总结","turn":轮号}：
        set_recap 写 agent._recap（队友看板）+ registry 条目 + Turn.recap（recaps.jsonl 持久化，
        fc 折叠摘要行优先用）。值疑似 LLM 错误文本（APIStatusError 等）时跳过保持旧值。"""
        try:
            d = payload if isinstance(payload, dict) else _json.loads(payload)
        except Exception:
            return "[hook_write] payload 需为对象（含 action 键）"
        if not isinstance(d, dict):
            return "[hook_write] payload 需为对象"
        act = str(d.get("action") or "").strip()
        if act == "set_recap":
            rc = str(d.get("value") or "").strip().split("\n")[0].strip()[:60]
            if not rc or any(mk in rc for mk in _RECAP_ERR_MARKS):
                return "[hook_write] set_recap 值为空或疑似错误文本，跳过（保持旧值）"
            agent._recap = rc
            reg = getattr(agent, "registry", None)
            if reg:
                try:
                    reg.update_recap(agent.agent_id, rc)
                except Exception:
                    pass
            t = d.get("turn")
            try:
                t = int(t)
            except (TypeError, ValueError):
                t = -1
            if t >= 0:
                try:
                    agent.session.set_turn_recap(t, rc)
                except Exception as e:
                    _LOG.warning("hook_write set_recap 落 Turn 失败(turn=%s): %s", t, e)
            return f"✅ recap 已写入（turn={t}）：{rc[:40]}"
        return f"[hook_write] 未知 action：{act or '(空)'}（支持：set_recap）"

    t = Tool(hook_write)
    t.hidden = True   # 仅钩子工作流 plugin 节点调用，不投影主 LLM
    return [t]


def _revive_subagent(agent, reg, entry, caller_id: str, prompt: str = ""):
    """复活一个历史子 Agent（registry 中 agent=None、磁盘上有 session）：
    读声明重建实例 + Session.load 恢复完整历史 + 回填 registry。
    返回 (Agent, model_name, sub_dir) 或 None（声明/磁盘 session 丢失时，调用方落回新建路径）。
    复活后投影 current_turn_only=True（reuse 语义）：历史轮完整归档可查，但不进上下文。
    声明加载与新建路径【同源】（_agent_def_path + load_agent_yml：v2.1 yml / 旧 md 双格式）——
    旧实现用 _agent_md_path+_split_frontmatter+已删除的 _build_subagent_system，
    对 v2.1 纯 YAML 解析出 meta={}（工具白名单/模型全丢），且 NameError 导致每次重启后
    复活失败 → 静默新建实例（wiki-updater_2/_3 多实例的根因）。"""
    try:
        p = _agent_def_path(entry.name)
        if p is None or not p.exists():
            return None
        from agent_config import load_agent_yml
        meta, system = load_agent_yml(p)
        # react 链也按声明注入（用户裁定 2026-09-15）：复活路径此前只设了 current_turn_only，
        # 漏了 set_fallback/_declared_fallback —— 复活实例带着构造时从 settings 继承的链跑，
        # "重启后子 Agent 回退链不对" 的根因。
        from multiagent import _parse_agent_fallback as _paf   # self-module：同文件函数，防御式导入
        try:
            _fb = _paf(meta)
        except Exception:
            _fb = None
        # .yml：正文为空、persona 在 assembly file: 项里 → system 传空；.md 旧格式无正文才用兜底文案
        if not (system or "").strip() and not (meta.get("assembly") or []) and p.suffix.lower() == ".md":
            system = "你是一个自主子 Agent，用工具完成任务。"
        from multiagent import _agent_own_tools
        _sk_bundle = _agent_skill_gear(entry.name, meta, agent)
        if _sk_bundle["error"]:   # 复活遇未知技能名/装配声明坏 → 落回新建路径（同样会报错给派发方，不静默吞）
            _LOG.warning("子 Agent '%s' 复活：skills 装备失败（%s）→ 落回新建", entry.name, _sk_bundle["error"])
            return None
        sk_tools, sk_sop = _sk_bundle["tools"], _sk_bundle["sop"]
        # extras 不在此应用：调用方派活链已在基线处合并（base_asm/base_hooks 一处合并三路生效）
        toolbox, _ = _resolve_tools(agent, meta.get("tools", ""))
        toolbox = Toolbox(*list(toolbox), *_agent_own_tools(entry.name, agent), *sk_tools)   # 专属+技能工具（同名覆盖全局）
        if sk_sop:   # 技能 SOP 注入 system 尾（与新建路径同源）
            system = ((system or "").rstrip() + "\n\n" + sk_sop).strip()
        model_name = meta.get("model") or entry.model or agent.model_name
        if model_name not in config.MODELS:
            model_name = agent.model_name
        # 磁盘 session lazy load（历史轮完整恢复，后续落盘仍写原目录）
        from session import Session
        sdir = getattr(agent.session, "session_dir", None)
        if not sdir:
            return None
        sub_dir = Path(sdir) / "agents" / entry.agent_id
        sub_meta = sub_dir / "meta.json"
        if not sub_meta.exists():
            return None
        loaded = Session.load(str(sub_meta), llm=agent.llm,
                              workspace=agent.session.workspace)
        # 2cec328 重构删了这行构造、只留下面 set_session —— 复活必炸 NameError（name 'sub'
        # is not defined），被 except 静默吞掉后落回新建路径 → 每次重启后子 Agent 重建实例
        # （wiki-updater_2/_3 多实例的根因回归，2026-09-29 用户实测日志坐实）。
        sub = SubAgent(entry.name, model_name, system, toolbox,
                       session_dir=loaded.session_dir or sub_dir,
                       registry=reg, agent_id=entry.agent_id,
                       caller_id=caller_id, current_turn_only=True)
        sub.agent.set_session(loaded)   # 换上磁盘 session：重挂 provider + 流水记录指到原目录
        # set_session 会换掉 __init__ 里设过开关的那个 session，这里在 loaded session 上重设
        sub.agent.session.current_turn_only = True
        if _fb is not None:   # 声明的回退链：实例链（非 react）+ react 链都补上
            try:
                sub.agent.llm.set_fallback(_fb[0], _fb[1])
                sub.agent._declared_fallback = list(_fb[0])
            except Exception:
                pass
        return sub.agent, model_name, (loaded.session_dir or sub_dir)
    except Exception as e:
        _LOG.warning("复活子 Agent %s(%s) 失败: %s", entry.name, entry.agent_id, e)
        return None


def _inject_agent_enums(agent, tools_list):
    """给多 Agent 工具的参数注入动态 enum（合法值提示 + LLM schema 约束 + 编辑器下拉框）：
    - agent_prompt / kill_agent 的 name：已声明子 Agent 名（.agent/agents/ 扫描）
    - agent_prompt 的 caller：['', 'user']（空=自动捕获 / user=fire-and-forget；显式 agent_id 仍可手填）
    - 通信工具的 target_id：registry 当前全部 agent_id（动态性强，作为提示性候选）
    enum 是提示性的（不在列表内的值仍可传），过期无害——create/kill 声明变化后重注入刷新。"""
    try:
        names = sorted(a["name"] for a in load_agents_index(WORKSPACE))
    except Exception:
        names = []
    reg_ids = []
    reg = getattr(agent, "registry", None)
    if reg:
        try:
            with reg._lock:
                reg_ids = sorted(e.agent_id for e in reg._agents.values())
        except Exception:
            reg_ids = []
    for t in tools_list:
        props = (t.schema.get("function", {}).get("parameters", {}) or {}).get("properties", {})
        if t.name in ("agent_prompt", "kill_agent") and "name" in props and names:
            props["name"]["enum"] = names
        if t.name in ("agent_ask", "agent_notify", "agent_query_events",
                      "agent_query_tool_detail") and "target_id" in props and reg_ids:
            props["target_id"]["enum"] = reg_ids
        if t.name == "agent_prompt" and "caller" in props:
            props["caller"]["enum"] = ["", "user"]   # 空=自动捕获 / user=fire-and-forget（显式 agent_id 也可手填）


def _make_service_watch_router(agent):
    """服务退出通知按 watcher 路由（用户提案 2026-10-16）：registry 查 watcher 实例 →
    deliver_notice 推送；查不到/异常返回 False——调用方（agent._on_service_exit）兜底自收，
    通知不丢。"""
    def _route(agent_id, text, seed, wake, source):
        try:
            reg = getattr(agent, "registry", None)
            entry = reg.lookup(agent_id) if reg else None
            target = getattr(entry, "agent", None) if entry else None
            if target is None:
                _LOG.warning("service_exit 多播：registry 无 %s 活条目——该 watcher 由持有者兜底", agent_id)
                return False
            return bool(deliver_notice(target, text, seed=seed))
        except Exception:
            return False
    return _route


# ═══ doctor 诊断工具族（2026-10-16 用户裁定） ═══
# BadRequest 数据诊断：子 Agent 的 LLM 请求因坏数据（坏图/超长/畸形消息）连锁 400 打穿
# 回退链时，_bg 失败分支自动派 doctor（.agent/agents/doctor/）按其人设工作循环诊断修复。
# 四件套：req_inspect 体检 / req_probe 试修 / req_patch 落盘 / req_resume 恢复。
# 数据源：llm_client._chat_with_fallback 的存档点（回退链耗尽 raise 前冻结
# self.last_failed_messages）。挂主 Agent 工具箱（chat.py），tools:"" 的子 Agent 自动继承。

_DOCTOR_PROBE_CAP = 5        # 每 agent 试修上限：穷尽仍败 → 判定非数据问题（人设安全阀同款）
_DOCTOR_LONG_LIMIT = 50000   # 单条消息正文超长可疑阈值（字）


def _doctor_normalize_patch(patch):
    """patch 入参归一化：LLM 可能传 JSON 字符串 → dict。返回 (patch|None, 错误文本)。"""
    if patch is None:
        return {}, ""
    if isinstance(patch, str):
        import json as _json
        try:
            patch = _json.loads(patch)
        except Exception as e:
            return None, f"[patch 解析失败] {type(e).__name__}: {e}——应为 JSON 对象，如 {{\"strip_images\":[3]}}"
    if not isinstance(patch, dict):
        return None, f"[patch 类型错] 应为 dict/JSON 对象，收到 {type(patch).__name__}"
    return patch, ""


def _doctor_msg_len(content) -> int:
    """消息正文总长（str 直取；list 累加 text 部件；None=0）。"""
    if isinstance(content, str):
        return len(content)
    if isinstance(content, list):
        return sum(len(p.get("text", "")) for p in content if isinstance(p, dict))
    return 0


def _doctor_image_parts(msg) -> list:
    """消息里的 image_url 部件清单（list content 才可能有）。"""
    c = msg.get("content")
    if not isinstance(c, list):
        return []
    return [p for p in c if isinstance(p, dict) and p.get("type") == "image_url"]


def _doctor_apply_patch(msgs: list, patch: dict) -> list:
    """修Data patch 应用到一份 messages（就地修改——probe 传深拷贝、patch 落盘传存档本体）。
    patch 键（全部可选）：strip_images=[i,...] 剔 image_url 部件；truncate={i: 保留长度}
    （list 内容按 text 部件顺序累计截）；drop=[i,...] 整条删；replace_text={i: "新文本"} 整条换文。
    drop 最后统一执行（索引一律按 patch 前编号）。返回应用摘要 list[str]。"""
    notes = []
    if not patch:
        return notes
    for k in patch:
        if k not in ("strip_images", "truncate", "drop", "replace_text"):
            notes.append(f"⚠️ 未知 patch 键 '{k}' 已忽略")

    def _idx_ok(i) -> bool:
        return isinstance(i, int) and 0 <= i < len(msgs)

    for i in (patch.get("strip_images") or []):
        if not _idx_ok(i):
            continue
        parts = _doctor_image_parts(msgs[i])
        if parts:
            msgs[i]["content"] = [p for p in msgs[i]["content"]
                                  if not (isinstance(p, dict) and p.get("type") == "image_url")]
            notes.append(f"strip_images[{i}] 剔除 {len(parts)} 个 image_url 部件")
        else:
            notes.append(f"strip_images[{i}] 无 image_url 部件（未动）")
    for k, n in (patch.get("truncate") or {}).items():
        try:
            i, n = int(k), int(n)
        except Exception:
            continue
        if not (0 <= i < len(msgs)) or n < 0:
            continue
        c = msgs[i].get("content")
        before = _doctor_msg_len(c)
        if isinstance(c, str):
            msgs[i]["content"] = c[:n]
        elif isinstance(c, list):
            left = n
            for p in c:
                if isinstance(p, dict) and "text" in p:
                    t = p.get("text") or ""
                    if len(t) > left:
                        p["text"] = t[:max(left, 0)]
                    left -= len(t)
            msgs[i]["content"] = [p for p in c
                                  if not (isinstance(p, dict) and p.get("type") == "text"
                                          and not (p.get("text") or ""))]
        notes.append(f"truncate[{i}] {before:,}→{_doctor_msg_len(msgs[i].get('content')):,} 字")
    for k, txt in (patch.get("replace_text") or {}).items():
        try:
            i = int(k)
        except Exception:
            continue
        if not _idx_ok(i):
            continue
        before = _doctor_msg_len(msgs[i].get("content"))
        msgs[i]["content"] = str(txt)
        notes.append(f"replace_text[{i}] {before:,}→{len(str(txt)):,} 字")
    drops = [i for i in (patch.get("drop") or []) if _idx_ok(i)]
    if drops:
        ds = set(drops)
        msgs[:] = [m for i, m in enumerate(msgs) if i not in ds]
        notes.append(f"drop 共删 {len(ds)} 条（索引按 patch 前编号）")
    return notes


def _doctor_fix_session_source(target, patched_msgs: list, patch: dict) -> tuple:
    """把 patch 同步修到 session 源头（2026-10-16）。范围=_current.steps 引用的 tool 结果
    （toollog 条目——投影经 _cap_full_result/_project_imgs 由它定型进存档）。
    特征匹配：strip_images → 剔 toollog result 里的 <img> 标签（image_url 部件的源头）；
    truncate → 截断 toollog result；经 record() 重记（内存替换 + jsonl 追加修正行，
    重放按 call_id 后写胜出）。drop/replace_text/user 消息无安全对应 → 只修缓存。
    返回 (已修清单, 修不了清单)。"""
    fixed, skip = [], []

    def _to_ints(v):
        out = []
        for x in (v or []):
            try:
                out.append(int(x))
            except Exception:
                pass
        return out
    strip = set(_to_ints(patch.get("strip_images")))
    trunc = {}
    for k, n in (patch.get("truncate") or {}).items():
        try:
            trunc[int(k)] = int(n)
        except Exception:
            pass
    if not strip and not trunc:
        return fixed, skip
    sess = getattr(target, "session", None)
    cur = getattr(sess, "_current", None)
    toollog = getattr(sess, "toollog", None)
    if cur is None or toollog is None:
        return fixed, ["目标无进行中的轮（可能已归档）——历史档消息不回改，只修了缓存"]
    # 存档内 tool 消息索引 → tool_call_id 映射（patch 索引一律按存档编号）
    idx_call = {i: m.get("tool_call_id") for i, m in enumerate(patched_msgs)
                if m.get("role") == "tool" and m.get("tool_call_id")}
    cur_callids = {tc.call_id for s in (cur.steps or []) for tc in (s.tool_calls or [])}
    for i in sorted(strip | set(trunc)):
        cid = idx_call.get(i)
        if not cid or cid not in cur_callids:
            skip.append(f"[{i}] 非当前轮 tool 结果（历史档/user 消息）——不回改")
            continue
        e = toollog.get(cid)
        if not e:
            skip.append(f"[{i}] toollog 无 {cid} 记录")
            continue
        r = e.get("result") or ""
        if i in strip and "<img" in r:
            n_img = len(re.findall(r"<img\b", r))
            r = re.sub(r"<img\b[^>]*>", "", r)
            fixed.append(f"[{i}] {cid} 剔除 {n_img} 个 <img> 标签")
        if i in trunc and len(r) > trunc[i]:
            r = r[:trunc[i]]
            fixed.append(f"[{i}] {cid} 截断到 {trunc[i]:,} 字")
        if r != (e.get("result") or ""):
            toollog.record(cid, e.get("name") or "", e.get("arguments") or {},
                           r, step=e.get("step"), turn=e.get("turn"))
    return fixed, skip


def make_doctor_tools(agent) -> list:
    """生成绑定到主 Agent 的诊断工具族（BadRequest 诊断修复 Agent 'doctor' 专用，
    2026-10-16 用户裁定）。目标定位：agent.registry.lookup(agent_id).agent；
    agent_id 留空 = doctor 自己（主 Agent）。probe 计数：每 agent 内存 dict，
    上限 5 次（穷尽 → 判定非数据问题）；req_patch 落盘成功即清零（下次事件新预算）。"""

    probe_counts: dict = {}   # {agent_id: 已 probe 次数}

    def _find_target(agent_id: str):
        """按 id 找目标 Agent 实例。返回 (实例|None, 解析后的 aid, 错误文本)。"""
        aid = (agent_id or "").strip()
        if not aid or aid == getattr(agent, "agent_id", ""):
            return agent, aid or getattr(agent, "agent_id", ""), ""
        reg = getattr(agent, "registry", None)
        entry = reg.lookup(aid) if reg is not None else None
        if entry is None or getattr(entry, "agent", None) is None:
            return None, aid, f"[目标不存在] agent_id='{aid}' 不在注册表或实例已销毁（list_team 查看）"
        return entry.agent, aid, ""

    def _load_archive(target):
        """目标 llm 的失败请求存档。返回 (llm, msgs|None)。"""
        llm = getattr(target, "llm", None)
        msgs = getattr(llm, "last_failed_messages", None)
        return llm, (msgs if isinstance(msgs, list) and msgs else None)

    def req_inspect(agent_id: str = "") -> str:
        """体检目标 agent 最近一次失败请求，逐条报告：role/content形态/长度/image_url(base64头60字)/tool_calls形态/可疑标记（空content数组、超长、图但模型非视觉）。
        agent_id: 目标实例 id（留空=自己）。数据源 llm.last_failed_messages——回退链耗尽抛错前由引擎冻结。"""
        target, aid, err = _find_target(agent_id)
        if err:
            return err
        llm, msgs = _load_archive(target)
        if msgs is None:
            return (f"[无存档] '{aid}' 的 llm 没有失败请求现场（last_failed_messages 空）——"
                    f"只有回退链耗尽抛错前才冻结；或该 agent 从未失败过")
        vision = getattr(llm, "vision_supported", None)
        lines = [f"📋 '{aid}' 失败请求体检：{len(msgs)} 条消息；模型={getattr(llm, 'model_name', '?')}；"
                 f"vision_supported={vision}"]
        suspicious = []
        for i, m in enumerate(msgs):
            role = m.get("role", "?")
            c = m.get("content")
            shape = "str" if isinstance(c, str) else (f"list({len(c)})" if isinstance(c, list) else "None")
            clen = _doctor_msg_len(c)
            imgs = _doctor_image_parts(m)
            flags = []
            if isinstance(c, list) and not c:
                flags.append("空content数组")
            if clen > _DOCTOR_LONG_LIMIT:
                flags.append(f"超长({clen:,}>5万)")
            if imgs and vision is False:
                flags.append(f"图但模型非视觉(×{len(imgs)})")
            tc = m.get("tool_calls")
            tc_desc = ""
            if tc:
                names, malformed = [], False
                for t in tc:
                    t = t or {}
                    fn = t.get("function") or {}
                    nm = fn.get("name", "")
                    names.append(nm or "?")
                    if not t.get("id") or not nm:
                        malformed = True
                tc_desc = f" | tool_calls×{len(tc)}[{', '.join(names)}]"
                if malformed:
                    flags.append("畸形tool_calls(缺id/name)")
            img_desc = ""
            if imgs:
                iu0 = imgs[0].get("image_url") or {}
                u0 = iu0.get("url", "") if isinstance(iu0, dict) else str(iu0)
                img_desc = f" | image_url×{len(imgs)}({u0[:60]})"
            line = f"[{i}] {role} | content={shape} 长度={clen:,}{img_desc}{tc_desc}"
            if flags:
                line += f" ⚠️{'/'.join(flags)}"
                suspicious.append(i)
            lines.append(line)
        if suspicious:
            lines.append(f"—— ⚠️ 可疑消息：{suspicious}。建议 req_probe(agent_id='{aid}', "
                         f"patch={{'strip_images':[...], 'truncate':{{i:2000}}, 'drop':[...], "
                         f"'replace_text':{{i:'占位'}}}}) 迭代试修")
        else:
            lines.append("—— 未发现明显坏数据特征；若仍 400 考虑 replace_text 首条历史浓缩块，"
                         "或穷尽后按安全阀判定非数据问题")
        return "\n".join(lines)

    def req_probe(agent_id: str = "", patch=None) -> str:
        """试修验证：深拷贝失败存档→应用 patch→同模型最小探测（max_tokens=16）。200=修法有效可落盘；400=看新错误调整 patch。每 agent 上限 5 次，穷尽判定非数据问题。存档本体不动。"""
        patch, perr = _doctor_normalize_patch(patch)
        if perr:
            return perr
        target, aid, err = _find_target(agent_id)
        if err:
            return err
        cnt = probe_counts.get(aid, 0)
        if cnt >= _DOCTOR_PROBE_CAP:
            return (f"[probe 上限] '{aid}' 已试修 {cnt} 次仍失败——判定非数据问题"
                    f"（模型端/账号/配额），按 doctor.md 安全阀如实上报，禁止继续 probe。")
        llm, msgs = _load_archive(target)
        if msgs is None:
            return f"[无存档] '{aid}' 没有失败请求现场可试修"
        fn = getattr(llm, "_chat_inner", None)
        if not callable(fn):
            return f"[不支持] '{aid}' 的 llm 无 _chat_inner（非标准 LLMClient），无法探测"
        probe_msgs = copy.deepcopy(msgs)
        notes = _doctor_apply_patch(probe_msgs, patch)
        probe_counts[aid] = cnt + 1
        try:
            fn(probe_msgs, max_tokens=16)
        except Exception as e:
            return (f"❌ probe 失败（第 {cnt + 1}/{_DOCTOR_PROBE_CAP} 次）：{type(e).__name__}: "
                    f"{str(e)[:400]}\n应用摘要：{'; '.join(notes) if notes else '(无操作)'}\n"
                    f"——调整 patch（加大力度/极简模式只留 system+末条）再 probe；存档未被改动")
        return (f"✅ probe 通过（第 {cnt + 1} 次，max_tokens=16 最小探测）——patch 有效，"
                f"可 req_patch 落盘。应用摘要：{'; '.join(notes) if notes else '(无操作)'}")

    def req_patch(agent_id: str = "", patch=None) -> str:
        """落盘修复：内部先自动 probe 验证同 patch（未通过拒绝落盘）；通过则 in-place 修 llm 失败存档 + 同步修 session 源头（当前轮 tool 结果）并 _autosave。历史档消息修不了只修缓存并说明。"""
        patch, perr = _doctor_normalize_patch(patch)
        if perr:
            return perr
        target, aid, err = _find_target(agent_id)
        if err:
            return err
        llm, msgs = _load_archive(target)
        if msgs is None:
            return f"[无存档] '{aid}' 没有失败请求现场可修"
        fn = getattr(llm, "_chat_inner", None)
        # 强制验证门：同 patch 先 probe 一遍（不计试修预算——门禁非探索）
        gate_msgs = copy.deepcopy(msgs)
        notes = _doctor_apply_patch(gate_msgs, patch)
        if callable(fn):
            try:
                fn(gate_msgs, max_tokens=16)
            except Exception as e:
                return (f"🚫 拒绝落盘：验证 probe 未通过（{type(e).__name__}: {str(e)[:300]}）——"
                        f"req_patch 前必须 probe 200，先 req_probe 迭代修法。"
                        f"应用摘要：{'; '.join(notes) if notes else '(无操作)'}")
        # ① 修存档本体（in-place——resume 重试若复用冻结投影，用的就是这份）
        _doctor_apply_patch(msgs, patch)
        # ② 修 session 源头：当前轮 steps 引用的 tool 结果（toollog 条目）+ 持久化
        sess_fixed, sess_skip = _doctor_fix_session_source(target, msgs, patch)
        try:
            target.session.invalidate_projection("doctor req_patch：session 源头已修，重投影")
        except Exception:
            pass
        saved = ""
        try:
            target.session._autosave()
            saved = "；session 已 _autosave"
        except Exception as e:
            saved = f"；⚠️ _autosave 失败: {type(e).__name__}: {e}"
        probe_counts[aid] = 0   # 落盘成功 → 预算清零（下次事件新预算）
        parts = [f"✅ 已落盘到 '{aid}'：{'; '.join(notes) if notes else '(无操作)'}{saved}",
                 f"session 源头修复：{'; '.join(sess_fixed) if sess_fixed else '(无)'}"]
        if sess_skip:
            parts.append("session 侧未修（只修了缓存）：" + "；".join(sess_skip))
        return "\n".join(parts)

    def req_resume(agent_id: str = "") -> str:
        """恢复目标 agent：清其 llm 回退链冷却（修复有效后冷却无意义，不清则白等 300s）+ 续跑其中断轮（resume_interrupted，返回状态透传）。"""
        target, aid, err = _find_target(agent_id)
        if err:
            return err
        llm = getattr(target, "llm", None)
        cleared = 0
        cd = getattr(llm, "_provider_cooldown", None)
        if isinstance(cd, dict):
            cleared = len(cd)
            cd.clear()
        try:
            r = target.resume_interrupted()
        except Exception as e:
            return f"[恢复异常] {type(e).__name__}: {e}（冷却已清 {cleared} 项）"
        if r == "":
            return f"✅ '{aid}' 已恢复：冷却清 {cleared} 项；中断轮已复位续跑"
        if r == "__queued__":
            return f"✅ '{aid}' 冷却已清 {cleared} 项；中断轮被更新轮顶替 → 续跑指令已入队其 inbox"
        return f"[恢复返回] {r}（冷却已清 {cleared} 项）"

    return [Tool(req_inspect), Tool(req_probe), Tool(req_patch), Tool(req_resume)]


def dispatch_doctor(agent, reason: str) -> str:
    """程序化派 doctor（引擎侧直调工具面，不经 LLM 决策——用户提案 2026-10-16 主 Agent 也触发）。
    复用 agent_prompt 完整管线（registry 登记/通知回流/_bg 线程）；doctor 不存在时返回提示
    （不炸调用方——agent.py 的异常路径里调用，失败只告警）。"""
    try:
        return agent.tools.run("agent_prompt", {
            "name": "doctor",
            "prompt": f"〔引擎自动派单〕{reason}\n\n目标 agent_id 留空（即 _main_ 主 Agent）。"
                      f"用 req_inspect 体检其最近失败请求，按 doctor.md 工作循环处理："
                      f"req_probe 验证修法 → req_patch 落盘 → req_resume（冷却清零+续跑）→ 汇报。",
        })
    except Exception as e:
        return f"[派 doctor 失败] {type(e).__name__}: {e}"


def make_subagent_tools(agent) -> list:
    """生成绑定到指定主 Agent 的子 Agent 管理工具（声明式 + 一次性）。"""
    # 服务退出多播路由注入（用户提案 2026-10-16）：主 Agent 的 services 池是全实例共享的，
    # _on_service_exit 在主 Agent 身上——watcher 路由经 registry 找到对应子 Agent 实例投递。
    try:
        agent._watch_router = _make_service_watch_router(agent)
    except Exception:
        pass

    def create_agent(name: str, description: str, system: str, tools: str = "", model: str = "",
                     assembly: str = "", hooks: str = "") -> str:
        """声明一个子 Agent（写 .agent/agents/<name>.yml，不建实例）。
        name: 唯一名；description: 一句话作用 + 何时调用（投影给主 Agent 决定何时派活）；
        system: 子 Agent 的角色/任务定义（超过 2000 字自动抽到 <name>.md 由 file: 装配——yml 保持精简，用户提案 2026-09-02）；
        tools: 留空/all=继承主 Agent 全部(除管理工具)，或逗号分隔工具名只注册这些；model: 指定模型，留空=主 Agent 当前模型。
        assembly: 装配清单（逗号分隔段名，支持 |optional 后缀——如 'user_message,steps,history|optional,tail.time'）。
                留空=默认（system + user_message + steps——白名单语义下没这两个段收不到任务，2026-09-02 教训）。
        hooks: 钩子挂载（逗号分隔 '位置=工作流'，支持 |async 后缀——如 'before_turn=wiki_auto_query,turn_end=recap_gen|async'）。
        声明后下一轮主 Agent SYSTEM 就会列出它，可用 agent_prompt 派活。"""
        d = WORKSPACE / _AGENT_DIR / "agents"
        if not _NAME_RE.match(name or ""):
            return f"[非法名称] '{name}'，只能含字母数字、下划线、连字符"
        if model and model not in config.MODELS:
            return f"[未知模型] '{model}'，可用：{list(config.MODELS)}"
        # 2026-10-06 用户裁定：create_agent 默认目录形态——<name>/<name>.yml + <name>.md + tools/
        d = d / name
        d.mkdir(parents=True, exist_ok=True)
        (d / "tools").mkdir(exist_ok=True)   # 专属工具占位目录（desktop-tools 式 agent 直接放脚本）
        p = d / f"{name}.yml"
        # 装配清单：system 大段抽 .md（file: 装配）；assembly 参数解析段名（|optional）；默认补必需段
        asm = []
        _sys = (system or "").strip()
        if len(_sys) > 2000:
            md = d / f"{name}.md"
            md.write_text(_sys, encoding="utf-8")
            asm.append({"file": f".agent/agents/{name}/{name}.md"})
        else:
            asm.append({"text": _sys})
        if assembly.strip():
            for part in assembly.split(","):
                part = part.strip()
                if not part:
                    continue
                if "|" in part:
                    seg, _, mods = part.partition("|")
                    if "off" in mods:
                        continue   # 显式 off：不进清单
                    item = f"{seg.strip()}|optional" if "optional" in mods else seg.strip()
                else:
                    item = part
                asm.append(item)
        else:
            asm.extend(["user_message", "steps"])   # 默认必需段（裸字符串——标准形态，收任务/看步骤）
        data = {
            "name": name,
            "description": description,
            "tools": tools,
            "model": model,
            "assembly": asm,
        }
        # 钩子挂载：'位置=工作流[|async]' → {hooks: {位置: [{workflow, async}]}}
        if hooks.strip():
            hk = {}
            for part in hooks.split(","):
                pos, _, wf = part.strip().partition("=")
                if not pos.strip() or not wf.strip():
                    return f"[hooks 格式错] '{part.strip()}'——应为 '位置=工作流'，如 'before_turn=wiki_auto_query'"
                _async = "|async" in wf
                wf = wf.replace("|async", "").strip()
                entry = {"workflow": wf}
                if _async:
                    entry["async"] = True
                hk.setdefault(pos.strip(), []).append(entry)
            data["hooks"] = hk
        p.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
        _inject_agent_enums(agent, tools_list)   # 声明变化：刷新 name 的 enum（编辑器下拉/LLM schema 同步）
        _extra = (f"；system {len(_sys):,} 字已抽 {name}.md（file: 装配）" if len(_sys) > 2000 else "")
        _extra += (f"；钩子：{hooks.strip()}" if hooks.strip() else "")
        return f"✅ 已声明子 Agent '{name}' -> {p.relative_to(WORKSPACE)}（下一轮 SYSTEM 可见）{_extra}"

    def kill_agent(name: str) -> str:
        """删除子 Agent 声明（.agent/agents/<name>.yml；同名 .md 一并清理）。"""
        if not _NAME_RE.match(name or ""):
            return f"[非法名称] '{name}'，只能含字母数字、下划线、连字符"
        d = WORKSPACE / _AGENT_DIR / "agents"
        gone = False
        # 停声明里拉起的依赖服务（2026-10-06）：kill 与服务生命周期同步
        try:
            from agent_config import load_agent_yml
            _p = _agent_def_path(name)
            if _p and _p.exists():
                _meta, _ = load_agent_yml(_p)
                _svcs = _meta.get("services") or []
                if isinstance(_svcs, dict):
                    _svcs = [_svcs]
                for _it in _svcs:
                    if isinstance(_it, dict):
                        for _sn in _it:
                            try:
                                agent.services.stop(str(_sn))
                            except Exception:
                                pass
        except Exception:
            pass
        # 目录形态优先（2026-10-06）：整目录移除（yml+md+tools）；平铺兜底（存量）
        if (d / name).is_dir():
            shutil.rmtree(d / name, ignore_errors=True)
            gone = True
        for ext in (".yml", ".md"):
            p = d / f"{name}{ext}"
            if p.exists():
                p.unlink()
                gone = True
        if not gone:
            return f"[不存在] 没有名为 '{name}' 的子 Agent"
        _inject_agent_enums(agent, tools_list)   # 声明变化：刷新 enum
        return f"✅ 已删除子 Agent '{name}'"

    def agent_prompt(name: str, prompt: str, tools: str = "", agent_id: str = "", reuse: str = "yes",
                     assembly: str = "", caller: str = "", context_messages: str = "",
                     current_turn_only: bool = True, max_steps: int = 0, images=None) -> str:
        """向子 Agent <name> 派任务（全异步）：后台自主跑，立即返回。
        完成后结果自动入队到调用者（你）的 inbox——你下一步边界就能看到（跟用户插话效果一样）。
        多次派同名：默认 reuse=yes 复用同名活实例（见下）；reuse=no 才每次新建独立实例。
        current_turn_only: 投影隔离（默认 True=复用模式只投影当前轮，历史轮归档不进上下文）。
        max_steps: 本任务的 react 步数上限（用户提案 2026-10-07：默认 0=用声明值/50）。
        images: 附图路径数组（workspace 相对或绝对；也兼容逗号分隔字符串）——框架把图复制进
                 repo 图片目录后以 <img> 标签追加到 prompt 尾部；子 Agent 是视觉模型时直接看像素，
                 非视觉模型降级文字占位（与 <img> 机制同源，免手写标签 + 路径自适应）（用户提案 2026-10-14）。
        长任务（多文件重构/大范围测试）建议显式给大（如 150）——50 步强制收尾对它们不够。
                长对话型实例（如按客户分线的客服）传 False：完整装配 history——该实例自己的
                会话历史就是它的记忆（跨轮连续），配合显式 agent_id 定向复用即"每客户一条线"。
        context_messages: 【一次性前置上下文】JSON 数组 [{"role","content"},...]——投影时展开在
                本轮 user_message 之前（结构化角色消息：还原的历史对话/工作记录），轮结束即焚
                （不落 turns、复用实例下一轮不带）——适合"把一批历史记录交给子 Agent 消费"的场景
                （如 wiki 维护：主 Agent 近几轮的事件序列还原成 messages 传入，避免它 read_file 重探索）。
        caller: 汇报对象（answer 完成后路由给谁）。留空=自动捕获调用者（你）；
                'user'/'system'=不路由给任何 Agent（fire-and-forget——工作流节点里派活配合
                wait_subagents 取结果的场景用它，避免每次完成都唤醒主 Agent 烧一轮 token）；
                也可显式传某个 agent_id（跨 Agent 委托）。
        tools: 临时指定本次子 Agent 可用的工具（留空=用 .md 里配置的；all/*=继承主 Agent 全部除
               管理工具；逗号分隔=只注册这些，如 'read_file,edit,write_file'）。复用模式下忽略（实例工具已定）。
        agent_id: 本次子 agent 的唯一标识（留空=自动 name / name_2…）。复用模式下忽略（沿用原 id）。
        reuse: 复用模式（枚举 yes/no，默认 yes=复用——不传即复用）：yes=registry 中有同名且空闲
                (done/failed)的活实例则直接派新任务给它（不新建实例；无活实例则复活同名历史实例，
                都没有才新建）；no=强制新建独立实例。复用实例的上下文投影【只含当前轮】
                （历史轮完整归档可 agent_query_events 查但不投影）——每次任务上下文干净、token 不随
                复用次数增长。同名实例全在跑时返回提示（wait_subagents 等它完成，或 reuse=no 并行开新实例）。
         assembly: 上下文装配覆盖（本次调用生效，不改 .md）：逗号分隔 '段=on/off'、
                'history=window|tiered|full' 或 'steps=reasoning'（steps 后的尾段以思考链姿势注入——
                末条 assistant 的 reasoning_content 前缀"当前状态：…"；默认 reminder=<system-reminder>
                包裹并入末条 content）。可关段 rules/history/ltm/hooks/tail。
               动作项（file/dir/cmd/workflow/text）不能在参数里新增，只走 .md 声明。
               .md 的 assembly 声明是基线，参数在其上覆盖。
               ⚠️ 子 Agent 未在 .md 声明 assembly 时默认不装 hooks（免每轮重跑 before_turn 检索）。
        如果需要结果才能继续，可调 wait_subagents(agent_ids) 显式阻塞等待。"""
        # reuse 字符串枚举解析（用户提案 2026-09-06：默认复用少建实例）。
        # 宽松兼容 true/false/1/0/bool（工作流 plugin 节点旧传参 type=boolean 直传 Python bool）
        _rv = str(reuse).strip().lower()
        if _rv in ("yes", "true", "1", "y", ""):
            reuse = True
        elif _rv in ("no", "false", "0", "n"):
            reuse = False
        else:
            return (f"[参数错误] reuse 只接受 yes/no（收到 '{reuse}'）。"
                    f"yes=复用同名活实例（默认，不传即是），no=新建独立实例。")
        caller_id = (caller or "").strip().lower() or agent.agent_id   # 汇报对象：显式指定 > 自动捕获
        # context_messages 直通：JSON 串 → list（plugin 节点传参恒为字符串）；非法输入静默忽略
        import json as _json
        _ctx_msgs = None
        if context_messages and isinstance(context_messages, str) and context_messages.strip().startswith("["):
            try:
                _cm = _json.loads(context_messages)
                if isinstance(_cm, list) and _cm and all(
                        isinstance(m, dict) and m.get("role") and "content" in m for m in _cm):
                    _ctx_msgs = _cm
            except Exception:
                _ctx_msgs = None
        elif isinstance(context_messages, list) and context_messages:
            _ctx_msgs = context_messages
        # images 附图（用户提案 2026-10-14）：复制进 repo images 目录 → <img>文件名</img> 追加到 prompt。
        # 子 Agent 侧投影按其模型 vision 门控展开（_project_imgs 只认 images 目录内的文件名——
        # 直接传 workspace 路径会「读取失败」，这里统一搬运后引用，两条根因一起堵住）。
        if images:
            from pathlib import Path as _P
            import shutil as _sh
            from session import repo_images_dir as _rid
            _paths = images if isinstance(images, (list, tuple)) else [
                x.strip() for x in str(images).replace("\n", ",").split(",") if x.strip()]
            _tags, _miss = [], []
            for _p in _paths:
                _fp = _P(_p) if _P(_p).is_absolute() else _P(WORKSPACE) / _p
                if not _fp.exists():
                    _miss.append(str(_p)); continue
                try:
                    _dst = _rid(WORKSPACE) / _fp.name
                    if not _dst.exists():
                        _sh.copy2(_fp, _dst)
                    _tags.append(f"<img>{_fp.name}</img>")
                except Exception:
                    _miss.append(str(_p))
            if _miss:
                return f"[图片不存在] images 里的这些路径未找到（相对路径基于 workspace）：{'、'.join(_miss)}"
            if _tags:
                prompt = (prompt or "") + "\n" + "\n".join(_tags)
        # 'user'/'system' = fire-and-forget：answer 不路由（现有 caller_id == "user" 判断天然处理），
        # 派发信息段同样跳过（子 Agent 无法反查 user 的上下文）；其它显式 id 校验存在性
        if caller_id in ("user", "system"):
            caller_id = "user"
        reg = getattr(agent, "registry", None)
        if caller_id not in ("user", agent.agent_id) and reg:
            if reg.lookup(caller_id) is None:
                return f"[未知 caller] agent_id='{caller_id}' 不在注册表（list_team 查看）"
        asm_note = ""
        # 读声明文件的 assembly 基线清单 + hooks 声明 + fallback 模型链（复用/复活/新建三条路径都要）
        p_md = _agent_def_path(name)
        base_asm = None
        base_hooks = None
        base_fb = None
        _meta = {}
        if p_md is not None and p_md.exists():
            try:
                from agent_config import load_agent_yml
                _meta, _ = load_agent_yml(p_md)
                base_asm = _parse_assembly(_meta)
                base_hooks = _parse_hooks(_meta) or None
                base_fb = _parse_agent_fallback(_meta)
            except Exception:
                pass
        # skills 装备（工具+SOP+装配声明）在基线读取处一次聚合（plan p_76f6623b）：
        # extras 在此并入 base_asm/base_hooks——复用/复活/新建三条路径共用同一合并产物（一处合并三路生效，
        # 复用路径 set_assembly_plan 才不会把装备的插段冲掉）；声明坏/锚缺失在此中断派活（装备时报错，不静默）。
        sk_bundle = _agent_skill_gear(name, _meta, agent)
        if sk_bundle["error"]:
            return f"[skills 装备失败] {sk_bundle['error']}"
        if sk_bundle["extras"]:
            base_asm, base_hooks, sk_aerr = _apply_extra_assemblies(base_asm, base_hooks, sk_bundle["extras"])
            if sk_aerr:
                return "[skills 装配失败] " + "；".join(sk_aerr)
        # 子 Agent 默认装 recap_gen（turn_end·async）：队友看板里的 recap 由本地小模型生成（零 token）。
        # yml 未声明 hooks → 注入默认；已声明 turn_end 但缺 recap_gen → 追加；显式关（assembly 参数 hooks=off）→ 不注入。
        if base_hooks is None:
            base_hooks = {"turn_end": [{"kind": "workflow", "value": "recap_gen", "async": True}]}
        else:
            te = base_hooks.setdefault("turn_end", [])
            if not any(it.get("value") == "recap_gen" for it in te):
                te.append({"kind": "workflow", "value": "recap_gen", "async": True})
        if base_asm and not any(it.get("kind") == "seg" and it.get("name") == "hooks" for it in base_asm):
            base_asm.append({"kind": "seg", "name": "hooks"})   # hooks 段开关打开（hook_specs 才会被 _run_hooks 消费）
        if assembly:
            base_asm, asm_note = _apply_assembly_overrides(base_asm, assembly)

        def _launch(_target, _aid, _name, _model, _sub_dir, _prompt, _reused):
            """通用启动：登记 background_tasks + 起 _bg 线程跑 _target.run()。新建/复用两条路径共用。"""
            if reg:
                reg.touch(_aid)   # 派活即交互（团队投影活跃窗口，2026-09-20）
            try:
                agent.recap_interact("a:" + _aid)   # 互动清脏（用户提案 2026-09-30）
            except Exception:
                pass
            # context_messages 直通：投影时展开在 user 前（一次性——finish_turn 即焚，复用实例下一轮不带）
            if _ctx_msgs:
                _ag = getattr(_target, "agent", _target)   # SubAgent 包装 or 裸 Agent（复用路径）
                _ag.session._pinned_ctx = list(_ctx_msgs)
            agent.background_tasks[_aid] = {
                "id": _aid, "kind": "subagent", "name": _name, "task": _prompt,
                "status": "running", "session_dir": str(_sub_dir) if _sub_dir else None,
                "result": None, "started_at": time.time(), "finished_at": None,
            }

            def _bg():
                # 增强 prompt：告知子 Agent 谁派的任务 + 如何反查派发者上下文
                enriched = _prompt
                if caller_id and caller_id != "user":
                    enriched = (
                        f"{_prompt}\n\n---\n〔任务派发信息〕\n"
                        f"本任务由 Agent '{caller_id}' 派发。"
                        f"如需了解派发者的更多上下文（它之前的工具调用结果、对话历史），"
                        f"可用 agent_query_events(\"{caller_id}\", 5) 查看其最近对话，"
                        f"或用 agent_query_tool_detail(\"{caller_id}\", \"call_id\") 查看某次工具调用的完整详情，"
                        f"或用 agent_ask(\"{caller_id}\", \"你的问题\") 直接向派发者提问。"
                    )

                def _interrupt_ctx(sa, task_text: str) -> str:
                    """中断/失败通知的近期上下文（用户提案 2026-10-16）：任务头 + 最后一步 +
                    倒数第二步 + recap——让主 Agent 不用 agent_query_events 就能判断怎么重派。"""
                    try:
                        cur = getattr(getattr(sa, "session", None), "_current", None)
                        steps = list(getattr(cur, "steps", []) or []) if cur else []
                        lines = [f"任务头: {' '.join((task_text or '').split())[:150]}"]
                        for idx, st in enumerate(steps[-2:]):
                            try:
                                s = str(st)
                                # Step 的 repr 兜底：取头 260 字（含 tool_use 名/参数或文本片段）
                                lines.append(f"倒数第{2 - idx}步: {' '.join(s.split())[:260]}")
                            except Exception:
                                pass
                        if not steps:
                            lines.append("(无已落步骤——可能起步即断)")
                        recap = (getattr(sa, "_recap", "") or "").strip()
                        if recap:
                            lines.append(f"recap: {recap[:120]}")
                        return "\n".join(lines)
                    except Exception as e:
                        return f"(上下文提取失败: {type(e).__name__})"

                def _route_answer(answer_text: str):
                    """路由 answer 到 caller 的 inbox——run() 返回后立即调用。
                    放在 recap/meta/background_tasks 之前：即使后续步骤被 /restart 杀进程，
                    answer 已在 inbox → inbox_thread → work_q → worker → run → 唤醒 caller。
                    闭包兜底（用户实锤 2026-10-15·worker-p_d721b263）：registry 条目缺失/条目
                    agent=None 时此前【静默丢通知】（answer 出来了但 caller 收不到）——caller 就是
                    派发者自身（施工委托 caller=self.agent_id）时 registry 只是加速索引，闭包里的
                    agent 才是真源，直接退回它推；跨 Agent 显式 caller 仍依赖 registry（查不到只告警）。"""
                    if not caller_id or caller_id == "user":
                        return
                    try:
                        routed = False
                        if reg:
                            caller_entry = reg.lookup(caller_id)
                            if caller_entry and caller_entry.agent:
                                target = caller_entry.agent
                            else:
                                target = None
                                _LOG.warning("子 Agent %s 完成时 registry 无 %s 的活条目"
                                             "（缺失或 agent=None）%s", _aid, caller_id,
                                             "——caller=派发者自身，走闭包兜底直推"
                                             if caller_id == agent.agent_id else "——跨 Agent 委托，无法兜底")
                        else:
                            target = agent if caller_id == agent.agent_id else None
                            if target is None:
                                _LOG.warning("子 Agent %s 完成但无 registry 且 caller %s 非派发者，通知丢弃", _aid, caller_id)
                        if target is not None:
                            body = answer_text if len(answer_text) <= 4000 else (
                                answer_text[:4000] + f"\n…（已截断 {len(answer_text) - 4000} 字，"
                                             f"完整回复用 agent_query_events(\"{_aid}\", 1) 查看）")
                            target.push_message(
                                f"📨〔子 Agent '{_name}' [{_aid}] 完成〕{body}",
                                source=f"subagent:{_aid}")
                            routed = True
                            _LOG.info("子Agent %s answer 已入队 caller %s 的 inbox（len=%d）",
                                      _aid, caller_id, len(target.inbox))
                        if not routed and caller_id == agent.agent_id:
                            # registry 路径全失败但 caller 就是派发者：闭包直推（终极兜底——
                            # 真源永远是派发时的 agent 实例，本工具组就是绑定它构建的）
                            agent.push_message(
                                f"📨〔子 Agent '{_name}' [{_aid}] 完成〕{answer_text[:4000]}",
                                source=f"subagent:{_aid}")
                            _LOG.info("子Agent %s answer 经闭包兜底直推派发者 %s", _aid, caller_id)
                    except Exception as route_err:
                        _LOG.error("子 Agent %s 完成后路由 answer 失败: %s", _aid, route_err)

                def _write_meta(status: str, recap: str = ""):
                    """写 _agent_meta 到子 Agent 的 meta.json（start/done/failed 三时机）。"""
                    if not _sub_dir:
                        return
                    try:
                        import json
                        meta_path = _sub_dir / "meta.json"
                        # 首次派发该子 Agent：agents/<id>/ 目录可能尚未建立（用户实测 2026-10-14 告警
                        # "写 _agent_meta 失败: No such file or directory ... agents/vision/meta.json"）
                        meta_path.parent.mkdir(parents=True, exist_ok=True)
                        meta = {}
                        if meta_path.exists():
                            meta = json.loads(meta_path.read_text(encoding="utf-8"))
                        meta.setdefault("extra_state", {})["_agent_meta"] = {
                            "agent_id": _aid, "name": _name, "model": _model,
                            "task": _prompt, "caller_id": caller_id,
                            "recap": recap, "status": status,
                        }
                        meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
                    except Exception as meta_err:
                        _LOG.warning("子 Agent %s 写 _agent_meta(status=%s) 失败: %s", _aid, status, meta_err)

                # 任务开始时写 running 态（用户提案 2026-09-02：/restart 杀 daemon 线程后
                # meta.json 停留旧 done——重启恢复无法辨别中断；start 时写 running 让
                # _restore_subagents 检测到"meta=running 但进程已重启"→ 标 failed）
                _write_meta("running")

                try:
                    _target.cumulative_tokens = 0
                    res = _target.run(enriched) or "(空回复)"

                    # 中断轮检测（用户提案 2026-09-02）：run 返回但 session._current 未清/
                    # 无 answer——被用户停止（stop_flag→return ""）或收尾 wrap_up 失败。
                    # 此前一律标 done（看板 ✅ 误导）。中断 → caller 收到中断通知 + registry 标 failed。
                    _sa = getattr(_target, "agent", _target)
                    _cur = getattr(getattr(_sa, "session", None), "_current", None)
                    if _cur is not None and not (_cur.answer or "").strip():
                        res = (f"[中断] 子 Agent 轮未正常完成（已完成 {len(_cur.steps)} 步、"
                               f"可能被停止或收尾失败）。\n部分结果可 agent_query_events(\"{_aid}\", 3) 查看\n\n"
                               f"── 近期上下文 ──\n{_interrupt_ctx(_sa, enriched)}")
                        # 主 Agent 兜底（用户提案 2026-10-16）：caller 非主 Agent（施工委托
                        # worker 的 caller 是系统）时 _route_answer 送达不了主 Agent——中断
                        # 就静默了，主 Agent 无从重派。中断/失败必须让 _main_ 知道。
                        if caller_id != agent.agent_id:
                            agent.push_message(
                                f"📨〔子 Agent '{_name}' [{_aid}] 中断〕{res[:2000]}",
                                source=f"subagent:interrupted:{_aid}")

                    # 立即路由 answer（在 recap/meta 之前——即使后续被杀，answer 已在 inbox）
                    _route_answer(res)
                    # 等待 recap 生成（异步 daemon 线程，最多等 3 秒）
                    for _ in range(30):
                        if getattr(_target, '_recap', ''):
                            break
                        time.sleep(0.1)
                    recap = getattr(_target, '_recap', '')
                    _is_fail = res.startswith("[失败]") or res.startswith("[中断]")
                    _write_meta("failed" if _is_fail else "done", recap)
                    agent.background_tasks[_aid].update(status="failed" if _is_fail else "done",
                                                        result=res, finished_at=time.time())
                    if reg:
                        reg.update_status(_aid, "failed" if _is_fail else "done")
                        reg.touch(_aid)   # answer 回流即交互（活跃窗口）
                except Exception as ex:
                    _sa_ex = getattr(_target, "agent", _target)
                    res = (f"[失败] {type(ex).__name__}: {ex}\n\n"
                           f"── 近期上下文 ──\n{_interrupt_ctx(_sa_ex, enriched)}")
                    # doctor 自动派发（2026-10-16 用户裁定）：失败原因是"回退链不可用"
                    # （多模型连锁失败，典型为请求数据问题 400 打穿全链）且失败者非 doctor
                    # 自身 → 派 doctor 按其人设工作循环诊断修复+恢复；doctor 自己失败不
                    # 递归（_name 判断即熔断）。主 Agent 自身的回退链耗尽不在此接
                    # （不能自己派自己——doctor.md 人设已有说明）。
                    if (_name != "doctor" and isinstance(ex, RuntimeError)
                            and "回退链不可用" in str(ex)):
                        try:
                            _doc_prompt = (
                                f"[自动派发·BadRequest 诊断] 子 Agent '{_name}' [agent_id={_aid}] "
                                f"的 LLM 请求刚因回退链耗尽失败（多模型连锁失败，疑似请求数据问题）。\n\n"
                                f"错误全文：\n{type(ex).__name__}: {ex}\n\n"
                                f"── 目标近期上下文 ──\n{_interrupt_ctx(_sa_ex, enriched)}\n\n"
                                f"请按 doctor.md 工作循环处理：req_inspect(agent_id='{_aid}') 体检 → "
                                f"req_probe 试修 → req_patch 落盘 → req_resume(agent_id='{_aid}') 恢复，"
                                f"完成后向派发者汇报坏在哪/怎么修的/根因建议。")
                            _doc_r = agent_prompt("doctor", _doc_prompt)
                            _LOG.warning("子 Agent %s 回退链耗尽 → 已自动派 doctor 诊断: %s",
                                         _aid, str(_doc_r)[:120])
                        except Exception as doc_err:
                            _LOG.warning("子 Agent %s 派 doctor 失败: %s", _aid, doc_err)
                    # 失败也立即路由（caller 需要知道子 Agent 出错了）+ 主 Agent 兜底
                    # （同中断分支 2026-10-16：施工委托 worker 的 caller 非主 Agent 时
                    #  失败也会静默——主 Agent 必须知道才能重派）
                    _route_answer(res)
                    if caller_id != agent.agent_id:
                        agent.push_message(
                            f"📨〔子 Agent '{_name}' [{_aid}] 失败〕{res[:2000]}",
                            source=f"subagent:interrupted:{_aid}")
                    _write_meta("failed")
                    agent.background_tasks[_aid].update(status="failed", result=res, finished_at=time.time())
                    if reg:
                        reg.update_status(_aid, "failed")

            th = threading.Thread(target=_bg, daemon=True)
            agent._bg_threads[_aid] = th
            th.start()
            tag = "♻️ 已复用" if _reused else "🚀 已启动"
            return (f"{tag}子 Agent '{_name}' [agent_id={_aid}]（异步，不阻塞）。"
                    f"完成后结果自动入队通知你。需要立即要结果可 wait_subagents(agent_ids=\"{_aid}\")。")

        # —— reuse 复用模式：registry 中找同名实例直接派任务（不新建） ——
        if reuse and reg:
            entry = None
            # 原子复用临界区（用户实锤 2026-09-22·8100 并行 agent_prompt 竞态）：查空闲→选实例→
            # 占位→标 running 必须同一临界区。此前分步加锁，单步并行工具（ThreadPoolExecutor）
            # 下的多个 agent_prompt 都在对方标 running 前查到同一"空闲"实例 → 全部复用同一
            # agent_id → 任务书串台（street/linwan 混进同一 vision 实例）。锁为 RLock，
            # 锁内调 update_status 重入安全。
            with reg._lock:
                same = [e for e in reg._agents.values()
                        if e.name == name and e.role == "subagent"]
                # 定向复用（用户提案 2026-09-24·淘宝客服按客户分线）：显式 agent_id = 定向到该实例——
                # 匹配集从「同 name 全体」收窄为「该 agent_id」，杜绝同 name 多实例（多条客户线）时
                # 按「最近注册的空闲」复用到别的线（任务书串台）。agent_id 不在 registry 时 same 为空
                # → 落到新建路径（_resolve_agent_id 用显式 id → 每客户一条实例线，重启后按同 id 复活）。
                if (agent_id or "").strip():
                    _want = agent_id.strip()
                    same = [e for e in reg._agents.values()
                            if e.agent_id == _want and e.role == "subagent"]
                    # 人设锁定（50052 淘宝店演练实锤 2026-09-24）：同一 agent_id 二次派单若换了 name
                    # （人设），会往同一条会话线里塞进另一个人设的历史（人设串台污染）。一经创建即锁定，
                    # 异名派单直接拒绍——SOP 之外的兜底（kefu_<cid> 必须始终用 name="kefu"）。
                    if same and same[0].name != name:
                        return (f"[拒绍] agent_id='{_want}' 已绑定人设 '{same[0].name}'，本次传入"
                                f" name='{name}' 不一致——同一实例线的 name 必须恒定（防人设串台）。"
                                f"请改用 name='{same[0].name}'，或换一个 agent_id。")
                live = [e for e in same if e.agent is not None and e.status != "reviving"]
                hist = [e for e in same if e.agent is None and e.status != "reviving"]
                if live:
                    idle = [e for e in live if e.status != "running"]
                    if not idle:
                        busy = ", ".join(e.agent_id for e in live)
                        return (f"[忙] '{name}' 的实例都在跑（{busy}）。"
                                f"先 wait_subagents 等它完成，或传 reuse=no 新建独立实例。")
                    entry = max(idle, key=lambda e: e.registered_at)
                    entry.task = prompt
                    entry.caller_id = caller_id
                    reg.update_status(entry.agent_id, "running")   # 占位即标（锁内）
                elif hist:
                    # 复活占位（同款竞态防护）：锁内先标 reviving，并行第二个同名调用看不到它 →
                    # 落到新建路径，不会双复活（revive 成功后 status 转正常态）
                    _hist_entry = max(hist, key=lambda e: e.registered_at)
                    reg.update_status(_hist_entry.agent_id, "reviving")
            if entry is not None:
                entry.agent.session.current_turn_only = bool(current_turn_only)   # 复用路径同样按参数（默认 True 保持原语义；False=完整装配 history——长对话型实例如客服）
                entry.agent.session.set_assembly_plan(base_asm)  # assembly：声明基线清单 + 参数覆盖（本次生效）
                if base_hooks is not None:
                    entry.agent.session.hook_specs = base_hooks
                if base_fb is not None:   # fallback：yml 声明（改链后复用实例下一任务即生效）
                    _apply_declared_fallback(entry.agent, base_fb)
                return _launch(entry.agent, entry.agent_id, name, entry.model,
                               getattr(entry.agent.session, "session_dir", None), prompt, _reused=True) + asm_note
            if hist:
                # 复活：同名历史实例（磁盘上有 session）→ lazy load 后继续派活
                # （历史轮完整归档接续，投影仍 current_turn_only 隔离；复活失败落回新建路径）
                entry = max(hist, key=lambda e: e.registered_at)
                revived = _revive_subagent(agent, reg, entry, caller_id, prompt)
                if revived is not None:
                    sub_agent, model_name, sub_dir = revived
                    sub_agent.session.current_turn_only = bool(current_turn_only)   # _revive 内默认 True；按参数覆盖（客服线复活也要完整记忆）
                    sub_agent.session.set_assembly_plan(base_asm)   # assembly：复活路径同样应用（声明基线 + 参数覆盖）
                    if base_hooks is not None:
                        sub_agent.session.hook_specs = base_hooks
                    if base_fb is not None:
                        _apply_declared_fallback(sub_agent, base_fb)
                    reg.register(entry.agent_id, name, "subagent", model_name,
                                 agent=sub_agent, task=prompt, status="running",
                                 caller_id=caller_id)
                    return _launch(sub_agent, entry.agent_id, name, model_name,
                                   sub_dir, prompt, _reused=True) + asm_note
            # 无同名实例（活/历史都没有或复活失败）→ 落到新建路径（current_turn_only=reuse）

        # —— 新建路径：读声明文件（.yml v2 / .md 旧格式）建临时实例 ——
        p = _agent_def_path(name)
        if p is None or not p.exists():
            return f"[不存在] 没有名为 '{name}' 的子 Agent，先 create_agent"
        try:
            from agent_config import load_agent_yml
            meta, system = load_agent_yml(p)
        except Exception as e:
            return f"[读取失败] {type(e).__name__}: {e}"
        # .yml：正文为空、persona 在 assembly text: 项里 → system 传空；.md 旧格式无正文才用兜底文案
        if not (system or "").strip() and not (meta.get("assembly") or []) and p.suffix.lower() == ".md":
            system = "你是一个自主子 Agent，用工具完成任务。"
        # 技能自带 services（plan p_76f6623b）：SKILL.md frontmatter services: 声明并入拉起清单
        # （yml services 同构形态：list of {名: 命令}；dict 形态包一层）。一次调用合并清单（_ensure 幂等）。
        _decl_svcs = meta.get("services") or []
        _all_svcs = ([_decl_svcs] if isinstance(_decl_svcs, dict) else list(_decl_svcs)) + sk_bundle["services"]
        if _all_svcs:
            _ensure_agent_services({**meta, "services": _all_svcs}, agent)
        sk_tools, sk_sop = sk_bundle["tools"], sk_bundle["sop"]   # 装备已在基线处聚合（extras 已并入 base_asm/base_hooks）
        toolbox, _ = _resolve_tools(agent, tools or meta.get("tools", ""))
        toolbox = Toolbox(*list(toolbox), *_agent_own_tools(name, agent), *sk_tools)   # 专属+技能工具（同名覆盖全局）
        if name == "doctor":
            # doctor 专属四件套（用户裁定 2026-10-16）：req_inspect/probe/patch/resume 不进
            # 主 Agent 工具箱（schema 膨胀），只在 doctor 实例化时注入——它也只需要这几个工具。
            toolbox = Toolbox(*make_doctor_tools(agent))   # 覆盖：诊断 Agent 面板=纯四件套
        if sk_sop:   # 技能 SOP 注入 system 尾（yml v2 场景 persona 在 assembly file 项，system 段只装 SOP）
            system = ((system or "").rstrip() + "\n\n" + sk_sop).strip()
        model_name = meta.get("model") or agent.model_name
        if model_name not in config.MODELS:
            model_name = agent.model_name
        # agent_id（显式优先，否则 name/name_2…）+ 子 session 嵌套到 主 session/agents/<id>/
        aid = _resolve_agent_id(agent.background_tasks, name, agent_id)
        sub_dir = None
        try:
            main_dir = agent.session._ensure_session_dir()   # 同步确保主 session 目录就绪
            sub_dir = Path(main_dir) / "agents" / aid
        except Exception:
            pass   # 主目录暂不可用 → 子 agent 退回默认时间戳目录，不阻断派活
        try:
            sub = SubAgent(name, model_name, system, toolbox,
                           on_event=None, session_dir=sub_dir,
                           registry=reg, agent_id=aid,
                           caller_id=caller_id, current_turn_only=(reuse and bool(current_turn_only)),
                           max_steps=int(max_steps or meta.get("max_steps") or 50),
                           assembly=(base_asm or None))
            if base_hooks is not None:
                sub.agent.session.hook_specs = base_hooks
            if base_fb is not None:
                _apply_declared_fallback(sub.agent, base_fb)
            if reg:
                reg.register(aid, name, "subagent", model_name,
                             agent=sub.agent, task=prompt, status="running",
                             caller_id=caller_id)
                sub.agent.session.extra_state["_agent_meta"] = {
                    "agent_id": aid, "name": name, "model": model_name,
                    "task": prompt, "caller_id": caller_id,
                }
        except Exception as e:
            agent.background_tasks[aid].update(status="failed", result=f"构造失败: {type(e).__name__}: {e}",
                                               finished_at=time.time())
            if reg:
                reg.update_status(aid, "failed")
            return f"[子 Agent 构造出错] {type(e).__name__}: {e}"
        return _launch(sub.agent, aid, name, model_name, sub_dir, prompt, _reused=False) + asm_note

    def wait_subagents(agent_ids: str = "", timeout: int = 120) -> str:
        """等待异步子 Agent 完成，返回它们的结果。
        agent_ids: 逗号分隔的 agent_id（留空=等所有还在跑的）；timeout: 秒（超时返回仍 running 的项，不杀线程）。
        本工具会【阻塞】直到指定任务结束或超时——适合"并行起 N 个探索后等齐再综合"。
        已完成的任务会立即返回其结果摘要。"""
        ids = [s.strip() for s in (agent_ids or "").split(",") if s.strip()]
        if not ids:
            ids = [aid for aid, th in agent._bg_threads.items() if th.is_alive()]
        if not ids:
            return "(没有正在跑的后台子 Agent；已完成的见【后台子 Agent 任务】看板)"
        deadline = time.time() + max(0, int(timeout))
        # 状态快照日志（卡等诊断：用户实测 wait 长时间不返回时，看这里每个 id 的线程/任务态）
        _LOG.info("wait_subagents: ids=%s timeout=%ds | 线程态=%s | 任务态=%s", ids, int(timeout),
                  {aid: ("alive" if (agent._bg_threads.get(aid) or threading.Thread()).is_alive() else "dead")
                   for aid in ids},
                  {aid: agent.background_tasks.get(aid, {}).get("status", "?") for aid in ids})
        out = []
        for aid in ids:
            th = agent._bg_threads.get(aid)
            if th is not None and th.is_alive():
                th.join(timeout=max(0.0, deadline - time.time()))
            entry = agent.background_tasks.get(aid, {})
            st = entry.get("status", "?")
            # 2026-09-04·用户裁定：工具返回值不截断、换行不压平——投影层按步距衰减统一压缩。
            # 此前 800 字截断 + \n→空格把 markdown 表格压烂（导演 session c115：验收报告
            # 被拦腰截断，为看全文连跑 agent_query_events/_main_ 误查 + get_tool_detail 三轮）。
            res = (entry.get("result") or "").strip()
            still = th is not None and th.is_alive()
            if still:
                _LOG.info("wait_subagents: %s join 超时仍 running（timeout=%ds）——任务未完成而非卡死判定",
                          aid, int(timeout))
            out.append(f"[{aid}] {'⏳仍在跑（超时，稍后再 wait）' if still else f'{st}：{res}'}")
        # 【去重】（用户实锤 2026-09-28）：上面的循环已把这些子 Agent 的结果作为本工具返回
        # 交给本轮上下文——把 inbox 里同源（subagent:<aid>）的副本移除并同步 inbox.jsonl，
        # 防轮结束后 inbox 出队再消费一次同一回复（wait 返回 + inbox 唤醒轮 = 双消费）。
        # 安全性：inbox 无时间戳，但"wait 之前完成"的条目早已在之前的轮被 pop 消费（不在 inbox），
        # 现存的 subagent:<aid> 必为本 wait 等待期间入队；aid 完成即终结，不会二次 push。
        try:
            import collections as _co
            srcs = {f"subagent:{a_}" for a_ in ids}
            with agent._inbox_lock:
                before = len(agent.inbox)
                if before:
                    agent.inbox = _co.deque(it for it in agent.inbox if it[0] not in srcs)
                    if len(agent.inbox) != before:
                        agent._inbox_persist_rewrite()
                        _LOG.info("wait_subagents 去重：移除 %d 条已由本工具返回的 inbox 副本",
                                  before - len(agent.inbox))
        except Exception:
            pass
        return "\n\n".join(out)

    def list_agents() -> str:
        """列出所有已声明的子 Agent（扫 .agent/agents/）。"""
        idx = load_agents_index(WORKSPACE)
        if not idx:
            return "(暂无子 Agent)"
        return "\n".join(
            f"- {a['name']} (模型={a['model'] or '默认'}, 工具={a['tools'] or '全部'}): {a['description']}"
            for a in idx
        )

    # 通信工具由 make_communication_tools 统一生成（绑定到正确的 agent 实例）
    reg = getattr(agent, "registry", None)
    # create_agent 退役（2026-10-06 用户裁定：DSL 字段越来越多，参数面板跟不上——
    # 官方路径=直接 write_file .agent/agents/<name>/<name>.yml（README 有最小模板）；
    # hidden 保留：工具箱可调（plugin/旧会话），不再投影给 LLM
    tools_list = [Tool(create_agent, hidden=True), Tool(kill_agent),
                  Tool(agent_prompt, param_schemas={
                      "reuse": {"type": "string", "enum": ["yes", "no"],
                                "description": "复用同名活实例：yes=复用（默认，不传即是）；no=强制新建独立实例"},
                      "current_turn_only": {"type": "boolean",
                                "description": "投影隔离：True(默认)=复用模式只投影当前轮；False=完整装配 history（长对话型实例如按客户分线的客服——传 False 且带固定 agent_id 即每客户一条记忆线）"},
                      "images": {"type": "array", "items": {"type": "string"},
                                "description": "附图路径（workspace 相对或绝对）——框架复制进 repo 图片目录后以 <img> 标签追加到 prompt；子 Agent 为视觉模型（如 qwen）时直接看像素，非视觉模型降级文字占位，无需手写 <img> 标签"}}),
                  Tool(list_agents), Tool(wait_subagents)]
    if reg:
        tools_list += make_communication_tools(agent)
        # 注册表变化时自动刷新 target_id enum（2026-09-04·导演 session 教训：创建时快照的
        # enum 只有 _main_，模型看枚举就传了 _main_ 误查自己、连跑三轮才绕出来——
        # 子 Agent 创建/读档恢复/kill 后 enum 需自动跟上；同 subscriber_id 覆盖不叠列表）
        _self_id = str(getattr(agent, "agent_id", "") or "_main_")
        reg.add_on_change(_self_id, lambda: _inject_agent_enums(agent, list(agent.tools)))
    _inject_agent_enums(agent, tools_list)   # name/caller/target_id 动态 enum（编辑器下拉 + LLM schema）
    # 声明变化回调注入（2026-10-06）：write_file 落 .agent/agents/ 后刷新 name enum
    try:
        import real_tools as _rt
        _rt._agents_changed_cb = lambda: _inject_agent_enums(agent, tools_list)
    except Exception:
        pass
    # 引擎程序化派发入口（用户提案 2026-10-15·施工委托）：把 prompt 实现挂到 agent 实例上，
    # 引擎（Agent._maybe_dispatch_construction）不经 LLM 工具调用即可起子 Agent——
    # 施工牌由 create_plan/commit_spec 触发自动派给 worker-<plan_id>。
    agent._prompt_subagent = agent_prompt
    return tools_list
