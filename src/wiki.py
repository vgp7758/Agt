"""wiki.py —— update_wiki 工具（同步薄封装：声明驱动的 wiki-updater 子 Agent 派活）。

wiki CRUD 六件套（read/list/tree/search/write/delete）已外置到 tools/builtin/wiki_tools.py
（真限界上下文：.agent/wiki/ 由工具组自写自读，零 agent 注入；/reload tools 热加载）。
本模块只保留 update_wiki——D 类（SubAgent 派活需要 agent.session/model/registry，
永远内置）；wiki-updater 子 Agent 的装配（system/tools 白名单/assembly 清单）全部由
.agent/agents/wiki-updater.yml 声明驱动，工具本身零特殊处理。
"""
from __future__ import annotations

import config
from tools import Tool


def make_wiki_tools(agent) -> list:
    """主 Agent 的 wiki 工具集：update_wiki（CRUD 六件套由外置脚本工具提供）。"""
    def update_wiki(summary: str = "") -> str:
        """完成重要功能或修改后调用（同步阻塞，返回维护报告）。
        子 Agent 的装配（system/tools 白名单/assembly 清单）全部来自 .agent/agents/wiki-updater.md
        声明——本工具只负责拼 prompt + 同步跑，不再有代码级特殊处理。
        summary 留空 → 自动把当前 Turn 的完整上下文(任务+工具调用+结果+计划)交给子 Agent 理解；
        自己填 summary → 用它（更聚焦）。"""
        prompt = summary.strip()
        if not prompt:
            last = agent.session.turns[-1] if agent.session.turns else None
            blocks = []
            if last:
                blocks.append(f"用户任务：{last.user_message}")
                for step in last.steps:
                    for tc in step.tool_calls:
                        n, a, r = agent.session.toollog.view(tc.call_id)
                        blocks.append(f"- {n}({a}) → {r[:200]}")
                if last.answer:
                    blocks.append(f"最终结果：{last.answer[:300]}")
            if agent.plan:
                from plan_tools import _plan_text
                blocks.append(f"执行计划：\n{_plan_text(agent)}")
            prompt = "\n".join(blocks) if blocks else "(无上下文)"
        try:
            from multiagent import SubAgent, _agent_def_path, _resolve_tools, _parse_assembly, _parse_hooks
            from agent_config import load_agent_yml
            p = _agent_def_path("wiki-updater")
            if p is None or not p.exists():
                return "[声明缺失] .agent/agents/wiki-updater.md 不存在，无法维护 wiki（create_agent 重建）"
            meta, system = load_agent_yml(p)
            toolbox, _ = _resolve_tools(agent, meta.get("tools", ""))
            model_name = meta.get("model") or agent.model_name
            if model_name not in config.MODELS:
                model_name = agent.model_name
            sub = SubAgent("wiki-updater", model_name, system or "", toolbox,
                           registry=getattr(agent, "registry", None),
                           agent_id="wiki-updater", caller_id=agent.agent_id,
                           assembly=_parse_assembly(meta))
            hooks = _parse_hooks(meta)
            if hooks:
                sub.agent.session.hook_specs = hooks
            report = sub.prompt(
                f"请据此更新 repo-wiki（.agent/wiki/）：\n\n{prompt}\n\n"
                f"上下文已注入最新 wiki 树——按它定位相关页面（需要细节再 wiki_read），"
                f"wiki_write 按业务/技术逻辑更新/新建页面，聚焦改动涉及的模块。"
            )
            return report or "(wiki 维护子 Agent 未产出报告)"
        except Exception as e:
            return f"[update_wiki 失败] {type(e).__name__}: {e}"

    return [Tool(update_wiki)]
