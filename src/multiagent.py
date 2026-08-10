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

import threading
import time
from pathlib import Path

import yaml

import config
from agent import Agent
from agent_config import _AGENT_DIR, _NAME_RE, _split_frontmatter, load_agents_index
from real_tools import WORKSPACE
from tools import Tool, Toolbox

# 子 Agent 绝不能继承的工具（防止递归生子 Agent、互相操控；计划工具绑定主 Agent）
_AGENT_TOOL_NAMES = {"create_agent", "agent_prompt", "kill_agent", "list_agents",
                     "create_plan", "update_plan", "update_wiki"}


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
                 on_event=None, max_steps: int = 50, token_budget: int = 0, session_dir=None):
        self.name = name
        self.model_name = model_name
        self.agent = Agent(system, tools, model_name=model_name,
                           enable_thinking=True, max_steps=max_steps,
                           token_budget=token_budget, verbose=False, on_event=on_event,
                           session_dir=session_dir)

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

    def agent_prompt(name: str, prompt: str, tools: str = "", agent_id: str = "",
                     background: bool = False) -> str:
        """向子 Agent <name> 派任务：读它的声明 md 建临时实例，自主用工具完成后回复，实例即弃。
        多次派同名 = 多个独立实例（无共享状态）。
        tools: 临时指定本次子 Agent 可用的工具（留空=用 .md 里配置的；all/*=继承主 Agent 全部除
               管理工具；逗号分隔=只注册这些，如 'read_file,edit,write_file'）——主 agent 可借此
               临时出借部分工具给子 agent 操作，覆盖其默认工具集。
        agent_id: 本次子 agent 的唯一标识（留空=自动 name / name_2…）。子 agent 的 session 存到
               主 session 文件夹下 agents/<agent_id>/，并登记进主 agent 的后台任务表（投影可见、供将来 wait）。
        background: True=后台异步跑（不阻塞你，看板出现⏳进行中，完成后转✅，用 wait_subagents 取结果）；
               False=同步阻塞等结果（默认）。异步适合并行【读/探索/搜索】——并发【写文件】无跨 agent 锁、
               有覆盖风险，改代码类任务请用 sync 或主 agent 自己做。"""
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
        agent.background_tasks[aid] = {
            "id": aid, "kind": "subagent", "name": name, "task": prompt,
            "status": "running", "session_dir": str(sub_dir) if sub_dir else None,
            "result": None, "started_at": time.time(), "finished_at": None,
        }
        try:
            sub = SubAgent(name, model_name, system, toolbox,
                           on_event=(None if background else agent.on_event), session_dir=sub_dir)
        except Exception as e:
            agent.background_tasks[aid].update(status="failed", result=f"构造失败: {type(e).__name__}: {e}",
                                               finished_at=time.time())
            return f"[子 Agent 构造出错] {type(e).__name__}: {e}"
        if background:
            def _bg(_sub=sub, _aid=aid, _prompt=prompt):
                try:
                    res = _sub.prompt(_prompt)
                    agent.background_tasks[_aid].update(status="done", result=res, finished_at=time.time())
                except Exception as ex:
                    agent.background_tasks[_aid].update(status="failed", result=f"{type(ex).__name__}: {ex}",
                                                        finished_at=time.time())
            th = threading.Thread(target=_bg, daemon=True)
            agent._bg_threads[aid] = th
            th.start()
            return (f"🚀 已在后台启动子 Agent '{name}' [agent_id={aid}]（不阻塞你）。完成后【后台子 Agent 任务】"
                    f"看板自动更新；调 wait_subagents(agent_ids=\"{aid}\") 等结果。")
        try:
            result = sub.prompt(prompt)
            agent.background_tasks[aid].update(status="done", result=result, finished_at=time.time())
            return result
        except Exception as e:
            agent.background_tasks[aid].update(status="failed", result=f"{type(e).__name__}: {e}",
                                               finished_at=time.time())
            return f"[子 Agent 调用出错] {type(e).__name__}: {e}"

    def wait_subagents(agent_ids: str = "", timeout: int = 120) -> str:
        """等待后台（agent_prompt background=True 启动的）子 Agent 完成，返回它们的结果。
        agent_ids: 逗号分隔的 agent_id（留空=等所有还在跑的）；timeout: 秒（超时返回仍 running 的项，不杀线程）。
        本工具会【阻塞】直到指定任务结束或超时——适合"并行起 N 个探索后等齐再综合"。
        sync 派的或已完成的任务会立即返回其结果摘要。"""
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
            res = (res[:300] + "…") if len(res) > 300 else res
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

    return [Tool(create_agent), Tool(kill_agent), Tool(agent_prompt), Tool(list_agents), Tool(wait_subagents)]
