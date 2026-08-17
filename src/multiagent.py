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
                     "get_session_history", "semantic_search_history", "rename_session"}


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
        # 复用模式：session 投影只含当前轮（历史轮不投影但完整归档）——多次派活不膨胀上下文
        if current_turn_only:
            self.agent.session.current_turn_only = True
        # assembly DSL：上下文装配开关（{}=全装；来自 md 声明 + agent_prompt 参数覆盖）
        if assembly:
            self.agent.session.assembly.update(assembly)
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
    """.agent/agents/<name>.md 路径；名字非法返回 None。"""
    if not _NAME_RE.match(name or ""):
        return None
    return WORKSPACE / _AGENT_DIR / "agents" / f"{name}.md"


# assembly DSL：合法段名（7 段）。必装段 system/user_message/steps 恒装（session 层不查开关）；
# 可关段 rules/history/hooks/tail 由 session.assembly / agent._run_hooks 查开关。
_ASSEMBLY_SEGS = {"system", "rules", "history", "user_message", "hooks", "steps", "tail"}
_ASSEMBLY_TOGGLES = {"rules", "history", "hooks", "tail"}


def _parse_assembly(meta: dict) -> dict:
    """frontmatter 的 assembly 字段 → {可关段: bool}。
    语义：【只装列出的段】——列出的可关段 True，没列的可关段 False（必装段无所谓，忽略）。
    元素格式 'name' 或 'name|optional'（optional 是文档性标记，不参与逻辑——4 个可关段都允许
    agent_prompt 参数关闭）。支持 YAML list 或逗号分隔串；未知段名忽略+日志；无声明返回 {}（全装）。"""
    raw = meta.get("assembly")
    if not raw:
        return {}
    items = raw if isinstance(raw, list) else [s.strip() for s in str(raw).split(",") if s.strip()]
    segs = set()
    for it in items:
        seg = str(it).split("|", 1)[0].strip()
        if seg in _ASSEMBLY_SEGS:
            segs.add(seg)
        elif seg:
            _LOG.warning("assembly 含未知段名 '%s'（合法：%s），已忽略", seg, sorted(_ASSEMBLY_SEGS))
    return {t: (t in segs) for t in _ASSEMBLY_TOGGLES}


def _apply_assembly_overrides(base_asm: dict, overrides_str: str) -> tuple:
    """agent_prompt 的 assembly 参数（'rules=off,history=off'）覆盖 base_asm 的可关段。
    返回 (合并后的 asm, 提示语)。off/false/0/关 = 关；on/true/1/开 = 开。"""
    asm = dict(base_asm)
    notes = []
    for part in (overrides_str or "").split(","):
        part = part.strip()
        if not part or "=" not in part:
            continue
        seg, _, val = part.partition("=")
        seg = seg.strip()
        val = val.strip().lower()
        if seg not in _ASSEMBLY_TOGGLES:
            if seg in ("system", "user_message", "steps"):
                notes.append(f"'{seg}' 必装不可关")
            elif seg:
                notes.append(f"'{seg}' 未知段名")
            continue
        asm[seg] = val in ("on", "true", "1", "开")
    return asm, ("；assembly 参数：" + "，".join(notes) if notes else "")


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


def _revive_subagent(agent, reg, entry, caller_id: str):
    """复活一个历史子 Agent（registry 中 agent=None、磁盘上有 session）：
    读声明 md 重建实例 + Session.load 恢复完整历史 + 回填 registry。
    返回 (Agent, model_name, sub_dir) 或 None（声明/磁盘 session 丢失时，调用方落回新建路径）。
    复活后投影 current_turn_only=True（reuse 语义）：历史轮完整归档可查，但不进上下文。"""
    try:
        p = _agent_md_path(entry.name)
        if p is None or not p.exists():
            return None
        meta, system = _split_frontmatter(p.read_text(encoding="utf-8"))
        system = (system or "").strip() or "你是一个自主子 Agent，用工具完成任务。"
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
        """声明一个子 Agent（写 .agent/agents/<name>.md，不建实例）。
        name: 唯一名；description: 一句话作用 + 何时调用（投影给主 Agent 决定何时派活）；
        system: 子 Agent 的角色/任务定义（systemPrompt）；tools: 留空/all=继承主 Agent 全部
               (除管理工具)，或逗号分隔工具名只注册这些；model: 指定模型，留空=主 Agent 当前模型。
        声明后下一轮主 Agent SYSTEM 就会列出它，可用 agent_prompt 派活。"""
        p = _agent_md_path(name)
        if p is None:
            return f"[非法名称] '{name}'，只能含字母数字、下划线、连字符"
        if model and model not in config.MODELS:
            return f"[未知模型] '{model}'，可用：{list(config.MODELS)}"
        p.parent.mkdir(parents=True, exist_ok=True)
        meta = yaml.safe_dump(
            {"name": name, "description": description, "tools": tools, "model": model},
            allow_unicode=True, sort_keys=False,
        ).strip()
        p.write_text(f"---\n{meta}\n---\n\n{system.strip()}\n", encoding="utf-8")
        return f"✅ 已声明子 Agent '{name}' -> {p.relative_to(WORKSPACE)}（下一轮 SYSTEM 可见）"

    def kill_agent(name: str) -> str:
        """删除子 Agent 声明（.agent/agents/<name>.md）。"""
        p = _agent_md_path(name)
        if p is None or not p.exists():
            return f"[不存在] 没有名为 '{name}' 的子 Agent"
        p.unlink()
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
        assembly: 上下文装配覆盖（本次调用生效，不改 .md）：逗号分隔 '段=on/off'，可关段
               rules/history/hooks/tail（如 'rules=off,history=off' 给纯任务型工人瘦身）。
               system/user_message/steps 必装不可关。.md 的 assembly 声明是基线，参数在其上覆盖。
        如果需要结果才能继续，可调 wait_subagents(agent_ids) 显式阻塞等待。"""
        caller_id = agent.agent_id   # 自动捕获调用者 id，完成后按此路由 answer
        reg = getattr(agent, "registry", None)
        asm_note = ""
        # 读声明 md 的 assembly 基线（复用/复活/新建三条路径都要；读不到为 {} 全装）
        p_md = _agent_md_path(name)
        base_asm = {}
        if p_md is not None and p_md.exists():
            try:
                _meta, _ = _split_frontmatter(p_md.read_text(encoding="utf-8"))
                base_asm = _parse_assembly(_meta)
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
                try:
                    _target.cumulative_tokens = 0
                    res = _target.run(enriched) or "(空回复)"
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
                    agent.background_tasks[_aid].update(status="failed", result=res, finished_at=time.time())
                    if reg:
                        reg.update_status(_aid, "failed")
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
                # 按 caller_id 路由 answer：入 caller 的 inbox → caller 下一步边界自动看到
                # 子 Agent 的最终回复就是交付物，不应粗暴截断；仅超长（>4000字）时截断并
                # 指引 caller 用 agent_query_events 查完整版（子 Agent session 已落盘，answer 可查）。
                if caller_id and caller_id != "user":
                    try:
                        if reg:
                            caller_entry = reg.lookup(caller_id)
                            if caller_entry and caller_entry.agent:
                                body = res if len(res) <= 4000 else (
                                    res[:4000] + f"\n…（已截断 {len(res) - 4000} 字，"
                                                 f"完整回复用 agent_query_events(\"{_aid}\", 1) 查看）")
                                caller_entry.agent.push_message(
                                    f"📨〔子 Agent '{_name}' [{_aid}] 完成〕{body}",
                                    source=f"subagent:{_aid}")
                            else:
                                _LOG.warning("子 Agent %s 完成但找不到 caller %s 的 registry 条目", _aid, caller_id)
                    except Exception as route_err:
                        _LOG.error("子 Agent %s 完成后路由 answer 失败: %s", _aid, route_err)

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
                entry.agent.session.assembly.update(base_asm)  # assembly：md 基线 + 参数覆盖（本次生效）
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
                revived = _revive_subagent(agent, reg, entry, caller_id)
                if revived is not None:
                    sub_agent, model_name, sub_dir = revived
                    sub_agent.session.assembly.update(base_asm)   # assembly：复活路径同样应用（md 基线 + 参数覆盖）
                    reg.register(entry.agent_id, name, "subagent", model_name,
                                 agent=sub_agent, task=prompt, status="running",
                                 caller_id=caller_id)
                    return _launch(sub_agent, entry.agent_id, name, model_name,
                                   sub_dir, prompt, _reused=True) + asm_note
            # 无同名实例（活/历史都没有或复活失败）→ 落到新建路径（current_turn_only=reuse）

        # —— 新建路径：读声明 md 建临时实例 ——
        p = _agent_md_path(name)
        if p is None or not p.exists():
            return f"[不存在] 没有名为 '{name}' 的子 Agent，先 create_agent"
        try:
            meta, system = _split_frontmatter(p.read_text(encoding="utf-8"))
        except Exception as e:
            return f"[读取失败] {type(e).__name__}: {e}"
        system = (system or "").strip() or "你是一个自主子 Agent，用工具完成任务。"
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
