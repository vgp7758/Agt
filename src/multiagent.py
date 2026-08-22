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

import logging
import re
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
                     "create_plan", "update_plan", "update_wiki",
                     "list_team", "agent_ask", "agent_notify",
                     "agent_query_events", "agent_query_tool_detail", "wait_subagents",
                     "get_session_history", "semantic_search_history", "rename_session",
                     # spec 五件套：闭包绑定创建时的 agent（主 Agent）——被子 Agent 继承会
                     # 跨 agent 串写状态（子 Agent 建 spec 会挂到主 Agent、气泡弹到主会话）
                     "create_spec", "commit_spec", "regenerate_spec", "list_specs", "recall_spec"}


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
                           session_dir=session_dir, registry=registry)
        if agent_id:
            self.agent.agent_id = agent_id
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
        # 会话工具同样重绑：钩子工作流（before_turn_retrieval 等）在子 Agent 里跑时，
        # get_session_history/semantic_search 必须查【子 Agent 自己的 session】，
        # 而不是继承自主 Agent 的闭包（那会查到主 Agent 的历史）
        from session_tools import make_session_tools
        for t in make_session_tools(self.agent):
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
    """子 Agent 定义文件路径：.agent/agents/<name>.yml（v2）优先，不存在回退 <name>.md（旧格式）。
    名字非法返回 None。返回值不保证存在（调用方自行判 exists）。"""
    if not _NAME_RE.match(name or ""):
        return None
    d = WORKSPACE / _AGENT_DIR / "agents"
    yml = d / f"{name}.yml"
    if yml.exists():
        return yml
    return d / f"{name}.md"


# assembly DSL v2：有序装配清单。合法段名（8 段）：必装段 system/user_message/steps 恒装
# （未列出时按默认顺序相对位置自动补插）；可关段 rules/history/ltm/tail 未列出即不装；
# hooks 不占投影位置（产出绑在当前轮内），仅作开关——子 Agent 未列出时默认 off。
# 动作项：file/dir/cmd/text 默认每轮求值（mtime 热改生效），workflow 默认 once 实例固化。
_ASSEMBLY_SEGS = {"system", "rules", "history", "ltm", "user_message", "hooks", "steps", "tail"}
_ASSEMBLY_TOGGLES = {"rules", "history", "ltm", "hooks", "tail"}
_ASSEMBLY_ACTIONS = ("file", "dir", "cmd", "workflow", "text", "func", "tool")
_ASSEMBLY_MUST = ("system", "user_message", "steps")
# 段的默认相对顺序（必装段自动补插的位置基准；= session._DEFAULT_ASSEMBLY_PLAN 的段序）
_ASSEMBLY_SEG_ORDER = ("system", "rules", "history", "ltm", "user_message", "steps", "tail")
_ASSEMBLY_HISTORY_MODES = ("tiered", "window", "full")
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
                # {workflow: x | async} 或 {cmd: ...} 或 {emit: ...}
                for k, v in it.items():
                    if k in ("workflow", "cmd", "emit"):
                        item = _hook_item_from_str(f"{k}: {v}")
                        break
                else:
                    item = _hook_item_from_str(next(iter(it.values())) if it else "")
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
    """'seg' / 'seg|optional' / 'history=window' / 'tool: read_file(x)' → 项 dict 或 None。"""
    raw = str(s).strip()
    # tool: 形式（动作项）
    if raw.startswith("tool:"):
        val = raw[len("tool:"):].strip()
        parsed = _parse_tool_expr(val)
        if parsed:
            tname, targs = parsed
            return {"kind": "tool", "tool": f"{tname}({targs})", "tool_name": tname, "tool_args": targs,
                    "timing": "turn"}
        return None
    seg = raw.split("|", 1)[0].strip()
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
        item = {"kind": "seg", "name": seg}
        if mode:
            item["mode"] = mode
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
    / {tool: read_file(x)} [+ every/once] 或 {history: window} 简写 → 项 dict 或 None。"""
    for k in _ASSEMBLY_ACTIONS:
        if k in d and d[k]:
            val = str(d[k]).strip()
            if k == "func":
                # {func: load_models()} → 剥掉尾随 ()
                val = val.rstrip("()").strip()
            item = {"kind": k, k: val, "timing": _asm_timing(k, d)}
            if k == "tool":
                parsed = _parse_tool_expr(val)
                if parsed:
                    item["tool_name"], item["tool_args"] = parsed
                    item["tool"] = val
            return item
    for seg in _ASSEMBLY_SEGS:
        if seg in d and d[seg]:
            val = str(d[seg]).strip().lower()
            item = {"kind": "seg", "name": seg}
            if seg == "history" and val in _ASSEMBLY_HISTORY_MODES:
                item["mode"] = val
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
            if mode:
                existing["mode"] = mode
            continue
        if seg in _ASSEMBLY_MUST:
            continue
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
        return reg.format_team(exclude_id=agent.agent_id)

    def agent_ask(target_id: str, question: str) -> str:
        """向另一个活跃 Agent 发起无状态询问：用对方的上下文 + 你的问题调用其 LLM，返回回答。
        被询问的 Agent 不会记录这次询问（其 session 不变），适合快速获取信息而不打扰对方。
        target_id: 目标 Agent 的 agent_id（用 list_team 查看）；question: 要问的问题。"""
        if not reg:
            return "(多 Agent 通信未启用：无 registry)"
        entry = reg.lookup(target_id)
        if entry is None:
            return f"[未找到] agent_id='{target_id}' 不在注册表中（可能已退出）。用 list_team 查看当前活跃 Agent。"
        target_agent = entry.agent
        if target_agent is None:
            return f"[错误] '{target_id}' 的 Agent 实例不可用"
        try:
            msgs = list(target_agent.session.messages_for_llm())
            msgs.append({"role": "user", "content": f"[来自队友 '{agent.agent_id}' 的询问] {question}"})
            resp = target_agent.llm.chat(msgs)
            answer = resp.content or "(对方返回空回答)"
            return f"[{target_id} 回答] {answer}"
        except Exception as e:
            return f"[询问失败] {type(e).__name__}: {e}"

    def agent_notify(target_id: str, message: str) -> str:
        """向另一个活跃 Agent 发送有状态提示：等效于用户插话，消息插入对方的待处理队列。
        对方会在下一步边界看到这条提示（与用户插话机制完全相同），且会被记录到其 session 中并落盘。
        适合需要对方记住的信息（如"我改了 xxx 文件"）。target_id: 目标 agent_id；message: 提示内容。"""
        if not reg:
            return "(多 Agent 通信未启用：无 registry)"
        entry = reg.lookup(target_id)
        if entry is None:
            return f"[未找到] agent_id='{target_id}' 不在注册表中。用 list_team 查看当前活跃 Agent。"
        target_agent = entry.agent
        if target_agent is None:
            return f"[错误] '{target_id}' 的 Agent 实例不可用"
        try:
            target_agent.queue_user_message(f"[来自队友 '{agent.agent_id}' 的提示] {message}")
            return f"✅ 已向 '{target_id}' 发送提示，对方下一步边界会看到。"
        except Exception as e:
            return f"[发送失败] {type(e).__name__}: {e}"

    def agent_query_events(target_id: str, count: int = 5) -> str:
        """查询另一个活跃 Agent 的最近 N 条对话事件（只读）：每轮的用户消息摘要 + 工具调用名 + 回答摘要。
        用于了解队友的进展。target_id: 目标 agent_id；count: 查最近几轮（默认 5）。"""
        if not reg:
            return "(多 Agent 通信未启用：无 registry)"
        resolved = _resolve_target(target_id)
        if resolved is None:
            return f"[未找到或无法加载] agent_id='{target_id}'。用 list_team 查看可用 Agent。"
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


def _revive_subagent(agent, reg, entry, caller_id: str, prompt: str = ""):
    """复活一个历史子 Agent（registry 中 agent=None、磁盘上有 session）：
    读声明 md 重建实例 + Session.load 恢复完整历史 + 回填 registry。
    返回 (Agent, model_name, sub_dir) 或 None（声明/磁盘 session 丢失时，调用方落回新建路径）。
    复活后投影 current_turn_only=True（reuse 语义）：历史轮完整归档可查，但不进上下文。
    system_append DSL 同样生效（用本次新任务 prompt 展开动态段）。"""
    try:
        p = _agent_md_path(entry.name)
        if p is None or not p.exists():
            return None
        meta, system = _split_frontmatter(p.read_text(encoding="utf-8"))
        system = (system or "").strip() or "你是一个自主子 Agent，用工具完成任务。"
        system = _build_subagent_system(agent, system, _parse_system_append(meta), prompt, entry.agent_id)
        toolbox, _ = _resolve_tools(agent, meta.get("tools", ""))
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
        sub = SubAgent(entry.name, model_name, system, toolbox,
                       session_dir=loaded.session_dir or sub_dir,
                       registry=reg, agent_id=entry.agent_id,
                       caller_id=caller_id, current_turn_only=True)
        sub.agent.set_session(loaded)   # 换上磁盘 session：重挂 provider + 流水记录指到原目录
        # set_session 会换掉 __init__ 里设过开关的那个 session，这里在 loaded session 上重设
        sub.agent.session.current_turn_only = True
        return sub.agent, model_name, (loaded.session_dir or sub_dir)
    except Exception as e:
        _LOG.warning("复活子 Agent %s(%s) 失败: %s", entry.name, entry.agent_id, e)
        return None


def make_subagent_tools(agent) -> list:
    """生成绑定到指定主 Agent 的子 Agent 管理工具（声明式 + 一次性）。"""

    def create_agent(name: str, description: str, system: str, tools: str = "", model: str = "") -> str:
        """声明一个子 Agent（写 .agent/agents/<name>.yml，不建实例）。
        name: 唯一名；description: 一句话作用 + 何时调用（投影给主 Agent 决定何时派活）；
        system: 子 Agent 的角色/任务定义（存为 assembly 的首个 text: 项，即 persona）；tools: 留空/all=继承主 Agent 全部
               (除管理工具)，或逗号分隔工具名只注册这些；model: 指定模型，留空=主 Agent 当前模型。
        声明后下一轮主 Agent SYSTEM 就会列出它，可用 agent_prompt 派活。"""
        d = WORKSPACE / _AGENT_DIR / "agents"
        if not _NAME_RE.match(name or ""):
            return f"[非法名称] '{name}'，只能含字母数字、下划线、连字符"
        if model and model not in config.MODELS:
            return f"[未知模型] '{model}'，可用：{list(config.MODELS)}"
        d.mkdir(parents=True, exist_ok=True)
        p = d / f"{name}.yml"
        data = {
            "name": name,
            "description": description,
            "tools": tools,
            "model": model,
            "assembly": [{"text": system.strip()}],
        }
        p.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
        return f"✅ 已声明子 Agent '{name}' -> {p.relative_to(WORKSPACE)}（下一轮 SYSTEM 可见）"

    def kill_agent(name: str) -> str:
        """删除子 Agent 声明（.agent/agents/<name>.yml；同名 .md 一并清理）。"""
        if not _NAME_RE.match(name or ""):
            return f"[非法名称] '{name}'，只能含字母数字、下划线、连字符"
        d = WORKSPACE / _AGENT_DIR / "agents"
        gone = False
        for ext in (".yml", ".md"):
            p = d / f"{name}{ext}"
            if p.exists():
                p.unlink()
                gone = True
        if not gone:
            return f"[不存在] 没有名为 '{name}' 的子 Agent"
        return f"✅ 已删除子 Agent '{name}'"

    def agent_prompt(name: str, prompt: str, tools: str = "", agent_id: str = "", reuse: bool = False,
                     assembly: str = "") -> str:
        """向子 Agent <name> 派任务（全异步）：后台自主跑，立即返回。
        完成后结果自动入队到调用者（你）的 inbox——你下一步边界就能看到（跟用户插话效果一样）。
        多次派同名 = 多个独立实例（无共享状态）；reuse=True 则复用同名活实例（见下）。
        tools: 临时指定本次子 Agent 可用的工具（留空=用 .md 里配置的；all/*=继承主 Agent 全部除
               管理工具；逗号分隔=只注册这些，如 'read_file,edit,write_file'）。复用模式下忽略（实例工具已定）。
        agent_id: 本次子 agent 的唯一标识（留空=自动 name / name_2…）。复用模式下忽略（沿用原 id）。
        reuse: 复用模式——registry 中有同名且空闲(done/failed)的活实例则直接派新任务给它（不新建实例）；
               没有则新建（之后的同名 reuse 调用会复用它）。复用实例的上下文投影【只含当前轮】
               （历史轮完整归档可 agent_query_events 查但不投影）——每次任务上下文干净、token 不随
               复用次数增长，适合高频派活避免实例越建越多。同名实例全在跑时返回提示。
        assembly: 上下文装配覆盖（本次调用生效，不改 .md）：逗号分隔 '段=on/off' 或
               'history=window|tiered|full'。可关段 rules/history/ltm/hooks/tail。
               动作项（file/dir/cmd/workflow/text）不能在参数里新增，只走 .md 声明。
               .md 的 assembly 声明是基线，参数在其上覆盖。
               ⚠️ 子 Agent 未在 .md 声明 assembly 时默认不装 hooks（免每轮重跑 before_turn 检索）。
        如果需要结果才能继续，可调 wait_subagents(agent_ids) 显式阻塞等待。"""
        caller_id = agent.agent_id   # 自动捕获调用者 id，完成后按此路由 answer
        reg = getattr(agent, "registry", None)
        asm_note = ""
        # 读声明文件的 assembly 基线清单 + hooks 声明 + fallback 模型链（复用/复活/新建三条路径都要）
        p_md = _agent_def_path(name)
        base_asm = None
        base_hooks = None
        base_fb = None
        if p_md is not None and p_md.exists():
            try:
                from agent_config import load_agent_yml
                _meta, _ = load_agent_yml(p_md)
                base_asm = _parse_assembly(_meta)
                base_hooks = _parse_hooks(_meta) or None
                base_fb = _parse_agent_fallback(_meta)
            except Exception:
                pass
        if assembly:
            base_asm, asm_note = _apply_assembly_overrides(base_asm, assembly)

        def _launch(_target, _aid, _name, _model, _sub_dir, _prompt, _reused):
            """通用启动：登记 background_tasks + 起 _bg 线程跑 _target.run()。新建/复用两条路径共用。"""
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

                def _route_answer(answer_text: str):
                    """路由 answer 到 caller 的 inbox——run() 返回后立即调用。
                    放在 recap/meta/background_tasks 之前：即使后续步骤被 /restart 杀进程，
                    answer 已在 inbox → inbox_thread → work_q → worker → run → 唤醒 caller。"""
                    if not caller_id or caller_id == "user":
                        return
                    try:
                        if reg:
                            caller_entry = reg.lookup(caller_id)
                            if caller_entry and caller_entry.agent:
                                body = answer_text if len(answer_text) <= 4000 else (
                                    answer_text[:4000] + f"\n…（已截断 {len(answer_text) - 4000} 字，"
                                                 f"完整回复用 agent_query_events(\"{_aid}\", 1) 查看）")
                                caller_entry.agent.push_message(
                                    f"📨〔子 Agent '{_name}' [{_aid}] 完成〕{body}",
                                    source=f"subagent:{_aid}")
                                _LOG.info("子Agent %s answer 已入队 caller %s 的 inbox（len=%d）",
                                          _aid, caller_id, len(caller_entry.agent.inbox))
                            else:
                                _LOG.warning("子 Agent %s 完成但找不到 caller %s 的 registry 条目", _aid, caller_id)
                    except Exception as route_err:
                        _LOG.error("子 Agent %s 完成后路由 answer 失败: %s", _aid, route_err)

                try:
                    _target.cumulative_tokens = 0
                    res = _target.run(enriched) or "(空回复)"
                    # 立即路由 answer（在 recap/meta 之前——即使后续被杀，answer 已在 inbox）
                    _route_answer(res)
                    # 等待 recap 生成（异步 daemon 线程，最多等 3 秒）
                    for _ in range(30):
                        if getattr(_target, '_recap', ''):
                            break
                        time.sleep(0.1)
                    recap = getattr(_target, '_recap', '')
                    # 无条件写完整 _agent_meta 到子 Agent 的 meta.json
                    if _sub_dir:
                        meta_path = _sub_dir / "meta.json"
                        try:
                            import json
                            meta = {}
                            if meta_path.exists():
                                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                            meta.setdefault("extra_state", {})["_agent_meta"] = {
                                "agent_id": _aid, "name": _name, "model": _model,
                                "task": _prompt, "caller_id": caller_id,
                                "recap": recap, "status": "done",
                            }
                            meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
                        except Exception as meta_err:
                            _LOG.warning("子 Agent %s 写 _agent_meta 到 meta.json 失败: %s", _aid, meta_err)
                    agent.background_tasks[_aid].update(status="done", result=res, finished_at=time.time())
                    if reg:
                        reg.update_status(_aid, "done")
                except Exception as ex:
                    res = f"[失败] {type(ex).__name__}: {ex}"
                    # 失败也立即路由（caller 需要知道子 Agent 出错了）
                    _route_answer(res)
                    # 失败也要写 _agent_meta
                    if _sub_dir:
                        meta_path = _sub_dir / "meta.json"
                        try:
                            import json
                            meta = {}
                            if meta_path.exists():
                                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                            meta.setdefault("extra_state", {})["_agent_meta"] = {
                                "agent_id": _aid, "name": _name, "model": _model,
                                "task": _prompt, "caller_id": caller_id,
                                "recap": "", "status": "failed",
                            }
                            meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
                        except Exception:
                            pass
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
            with reg._lock:
                same = [e for e in reg._agents.values()
                        if e.name == name and e.role == "subagent"]
            live = [e for e in same if e.agent is not None]
            hist = [e for e in same if e.agent is None]
            if live:
                idle = [e for e in live if e.status != "running"]
                if not idle:
                    busy = ", ".join(e.agent_id for e in live)
                    return (f"[忙] '{name}' 的实例都在跑（{busy}）。"
                            f"先 wait_subagents 等它完成再 reuse，或去掉 reuse 新建独立实例。")
                entry = max(idle, key=lambda e: e.registered_at)
                entry.agent.session.current_turn_only = True   # 保证投影隔离（旧实例可能未设）
                entry.agent.session.set_assembly_plan(base_asm)  # assembly：声明基线清单 + 参数覆盖（本次生效）
                if base_hooks is not None:
                    entry.agent.session.hook_specs = base_hooks
                if base_fb is not None:   # fallback：yml 声明（改链后复用实例下一任务即生效）
                    entry.agent.llm.set_fallback(base_fb[0], base_fb[1])
                with reg._lock:
                    entry.task = prompt
                    entry.caller_id = caller_id
                reg.update_status(entry.agent_id, "running")
                return _launch(entry.agent, entry.agent_id, name, entry.model,
                               getattr(entry.agent.session, "session_dir", None), prompt, _reused=True) + asm_note
            if hist:
                # 复活：同名历史实例（磁盘上有 session）→ lazy load 后继续派活
                # （历史轮完整归档接续，投影仍 current_turn_only 隔离；复活失败落回新建路径）
                entry = max(hist, key=lambda e: e.registered_at)
                revived = _revive_subagent(agent, reg, entry, caller_id, prompt)
                if revived is not None:
                    sub_agent, model_name, sub_dir = revived
                    sub_agent.session.set_assembly_plan(base_asm)   # assembly：复活路径同样应用（声明基线 + 参数覆盖）
                    if base_hooks is not None:
                        sub_agent.session.hook_specs = base_hooks
                    if base_fb is not None:
                        sub_agent.llm.set_fallback(base_fb[0], base_fb[1])
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
        toolbox, _ = _resolve_tools(agent, tools or meta.get("tools", ""))
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
                           caller_id=caller_id, current_turn_only=reuse,
                           assembly=(base_asm or None))
            if base_hooks is not None:
                sub.agent.session.hook_specs = base_hooks
            if base_fb is not None:
                sub.agent.llm.set_fallback(base_fb[0], base_fb[1])
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
        out = []
        for aid in ids:
            th = agent._bg_threads.get(aid)
            if th is not None and th.is_alive():
                th.join(timeout=max(0.0, deadline - time.time()))
            entry = agent.background_tasks.get(aid, {})
            st = entry.get("status", "?")
            res = (entry.get("result") or "").strip().replace("\n", " ")
            res = (res[:800] + f"…（截断，完整回复 agent_query_events(\"{aid}\", 1)）") if len(res) > 800 else res
            still = th is not None and th.is_alive()
            out.append(f"[{aid}] {'⏳仍在跑（超时，稍后再 wait）' if still else f'{st}：{res}'}")
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
    tools_list = [Tool(create_agent), Tool(kill_agent), Tool(agent_prompt), Tool(list_agents), Tool(wait_subagents)]
    if reg:
        tools_list += make_communication_tools(agent)
    return tools_list
