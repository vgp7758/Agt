"""session_tools.py —— 会话管理工具（绑定到 Agent 的 session）。

工厂 make_session_tools(agent) 仿 memory_tools / plan_tools 惯例，返回绑定到
agent.session 的工具列表：
  - rename_session：Agent 可调，发现自动命名不准时改成更贴切的名字
  - get_session_history：hidden，供工作流节点编排检索/重排/投影用，返回全量未截断结构化历史
  - semantic_search_history：hidden，供工作流（before_turn_retrieval 等）语义召回历史轮次
"""
from __future__ import annotations

import json

from tools import Tool


def make_session_tools(agent) -> list:
    """生成绑定到指定 Agent 的会话管理工具。"""

    def rename_session(new_name: str) -> str:
        """重命名当前会话。适用：首轮自动命名的名字不准确，或对话主题已转变，想换个更贴切的名字。
        new_name 可含空格（如「Unity 转 AI 求职」）。改名即时同步到 WebUI 标题与会话列表。
        无需主动用——只在当前会话名明显不合适时调用；改名不影响历史与上下文，可继续对话。"""
        new_name = (new_name or "").strip()
        if not new_name:
            return "[错误] new_name 不能为空"
        try:
            old = agent.session.name or "(未命名)"
            agent.session.rename(new_name)
        except ValueError as e:
            return f"[错误] {e}"
        # 通知 WebUI 同步标题（CLI 无 on_event 时跳过）
        if getattr(agent, "on_event", None):
            try:
                agent.on_event({"type": "session_renamed", "name": agent.session.name})
            except Exception:
                pass
        return f"✅ 会话已重命名：{old} → {agent.session.name}"

    def get_session_history(max_turns: int = None) -> list:
        """返回当前 session 的全量结构化历史（turns + 工具调用，result 未截断）。
        max_turns 非空时只返回最近 N 轮（None=全部）。
        仅供工作流节点编排检索/重排/投影用——不投影给 Agent LLM（hidden=True）。"""
        return agent.session.to_history_full(max_turns)

    def semantic_search_history(query: str, top_k: int = 10) -> list:
        """语义检索历史轮次。用 embedding 在全部已索引的会话轮次里搜，换说法也能命中
        （不像 keyword 搜法那样只能精确子串匹配）。返回候选列表，每项含 kind/turn_idx/score/text，
        供 before_turn_retrieval 工作流的 collect_candidates 与 keyword 结果合并。

        query: 用户原始消息句（不要填关键词列表——语义检索用原文效果最好）
        top_k: 最多返回条数（默认 10）
        """
        store = getattr(agent.session, "vec_store", None)
        if store is None:
            return []   # 没配 embed → 空结果（工作流走 keyword 单路）
        try:
            results = store.search(query, top_k=top_k)
        except Exception:
            return []
        # 把 vec_store 的搜索结果转成工作流 collect_candidates 兼容格式
        # kind="semantic" 让 LLM 精排节点知道这是语义候选（可能有噪声但覆盖广）
        sid = agent.session.name or (agent.session.session_dir.name if agent.session.session_dir else "")
        out = []
        for r in results:
            tno = r["turn_no"]
            # 当前 session 的 turn:直接按索引取内容拼 text
            if r.get("session_id") == sid:
                turns = agent.session.turns
                idx = tno - 1
                if 0 <= idx < len(turns):
                    t = turns[idx]
                    text = f"用户: {t.user_message[:120]} | 回答: {t.answer[:200]}"
                    if t.summary:
                        text = f"[{t.summary[:80]}] {text}"
                    out.append({"kind": "semantic", "turn_idx": tno,
                                "score": round(r.get("score", 0), 3), "text": text})
            else:
                # 跨 session（异 session_id）暂不深度打捞（需 load 异地 meta+events）；
                # 只标来源，由工作流 LLM 节点决定要不要自行查
                out.append({"kind": "semantic_x", "turn_idx": tno,
                            "session_id": r["session_id"],
                            "score": round(r.get("score", 0), 3),
                            "text": f"(跨会话 {r['session_id']}#{tno})"})
        return out

    return [
        Tool(rename_session),                        # Agent LLM 可调
        Tool(get_session_history, hidden=True),       # 只工作流 plugin 节点能调
        Tool(semantic_search_history, hidden=True),   # 只工作流 plugin 节点能调
    ]
