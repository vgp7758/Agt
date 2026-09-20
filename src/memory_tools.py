"""memory_tools.py —— 长期记忆召回工具（绑定到 Agent 的 session）。

完整原文永不丢后，近期窗口外的早期对话不直接喂给模型，而是以摘要形式注入。
当模型需要早期某轮的具体细节（用户说过什么、当时工具返回了什么、结论是什么）时，
用 recall_turn 按关键词召回那一轮的完整上下文（不含思考过程）。

工厂 make_recall_tools(agent) 仿 plan_tools.py / wiki.py 惯例，返回绑定到
agent.session 的工具列表。
"""
from __future__ import annotations

from tools import Tool


def make_recall_tools(agent) -> list:
    """生成绑定到指定 Agent 的记忆召回工具（recall_turn）。"""

    def recall_turn(query: str, contains_reasoning: bool = False, tools: str = "brief") -> str:
        """召回历史轮次的完整内容。当近期上下文窗口之外的早期对话、或忘了之前某轮的具体细节时，
        用关键词/短句在【全部历史】中搜索，返回匹配轮次（含被折叠为结构摘要的轮——其 user/answer
        原文始终可召回）。query 支持：多关键词 AND 不用管——用 | / 空格 / 中英文逗号 / 顿号 / 分号分隔
        即 OR 语义（任一命中即命中）；支持通配符 * ? []（如 'replace_*' 命中 replace_lines 轮）。
        contains_reasoning 默认 False 不含思考过程，设 True 时一并带上每步与回答的 reasoning。
        tools="brief"（默认）工具调用折叠成一行，只给整段 user+answer 原文（工具细节用
        agent_query_tool_detail 按 call_id 查）；"full" 展开每个工具调用的入参与结果。
        匹配域：每轮的摘要+用户消息+回答。"""
        return agent.session.recall(query, contains_reasoning, tools)

    return [Tool(recall_turn)]
