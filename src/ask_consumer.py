"""ask 消费端统一实现（用户提案 2026-10-22）——agent_ask / remote_ask 被问方的同一套语义。

被 ask 的 Agent 不再"裸 LLM 纯对话"（旧 agent_ask 的 tools=[]）也不"跑一整轮 agent.run"
（旧 remote_ask 的污染路径），而是：

1. 【拷贝上下文】把被问者当前投影 messages 拷一份（只 append，不改原 session 任何状态）；
2. 【临时 agent】内部 new 一个轻量 AskAgent（messages + 被问者的 llm + 被问者的工具箱）：
   - 只读/召回类工具 → 正常执行并回填结果（问"你那边某文件写了什么"它能自己查）；
   - run_shell / run_python / 写类工具 → 拒绝执行，返回引导文案（"这是询问场景，请直接回答"）；
   - 未知工具（新 MCP/新外置工具）→ fail-closed 拒绝（只有明确只读/召回类才放行）；
3. 【60s 软预算】带工具轮迟迟不收敛（到点仍无最终 answer）→ 下一轮请求【清空工具】
   （tools=[]）+ 一句"预算已到、立即直接回答"再发一次，逼它收敛出回答。

纯旁路：不写 session / events.jsonl / toollog，不注册 registry，不跑钩子，不触发审批。
"""
from __future__ import annotations

import json
import logging
import time

_LOG = logging.getLogger("ask_consumer")

ASK_BUDGET_SECONDS = 60.0      # 软预算：超时仍未给出 answer → 清空工具重问
ASK_MAX_STEPS = 8              # 带工具最多轮数（防无限工具链）
ASK_TOOL_RESULT_CAP = 8000     # 单个只读工具结果回填上限（字符，防上下文爆炸）


# ===================== 工具三分类（黑名单优先 → 白名单 → 未知 fail-closed） =====================
# 明确写/执行/有副作用类（优先判黑，避免 "list_"-类前缀把 wiki_write 等误放行）
_WRITE_TOOLS = frozenset({
    # 命令 / 代码执行
    "run_python", "run_shell",
    # 文件写（含按行编辑族）
    "write_file", "edit", "replace_lines", "insert", "delete", "move",
    "create_file", "append_file", "copy_file", "rename_file", "mkdir", "remove_file",
    # 版本控制 / 发布
    "git_commit", "git_push", "git_checkout", "release_publish", "sync_workflow_sources",
    # 计划 / 施工方案
    "create_plan", "update_plan", "add_step", "edit_plan", "join_plan", "exit_plan",
    "create_spec", "commit_spec", "regenerate_spec",
    # 长期记忆 / 技能 / 子 Agent 管理
    "add_memory", "update_memory", "delete_memory", "save_skill",
    "create_agent", "kill_agent", "agent_prompt", "agent_notify", "wait_subagents",
    "agent_ask", "remote_connect", "remote_disconnect", "remote_message", "remote_ask",
    "remote_call_tool", "agentid_get_token",
    # 调度 / 服务 / 进程
    "add_schedule", "cancel_schedule",
    "start_service", "stop_service", "service_stdin", "service_watch", "service_unwatch",
    "restart_agent", "reload_hot", "reload_mcp_server", "set_tool_timeout", "sleep",
    # 技能动作
    "skill_equip", "skill_unequip", "skill_use", "skill_command",
    "skill_run_code", "skill_evaluate",
    # 工作流执行/改写
    "exec_workflow", "debug_workflow", "hotswap_workflow_node", "eval_node_output",
    # 人在环 / 交互
    "ask_user", "human_step",
    # wiki 写（读侧 wiki_read/wiki_list/wiki_tree/wiki_search 在白名单）
    "wiki_write", "wiki_delete", "wiki_add_chapter", "wiki_update_chapter",
    "wiki_remove_chapter", "wiki_move_chapter",
    # 团队 / 远程实例
    "team_up",
})

_WRITE_PREFIXES = (
    "write_", "append_", "edit_", "create_", "commit_", "update_", "delete_", "remove_",
    "set_", "save_", "kill_", "start_", "stop_", "restart_", "reload_",
    "skill_", "remote_",
)

# 明确只读 / 召回类（真执行）
_READ_TOOLS = frozenset({
    # 文件系统（读）
    "read_file", "list_dir", "grep", "glob_files",
    "diff_files", "diff_paths",
    # repo 知识库 / RAG
    "wiki_read", "wiki_list", "wiki_tree", "wiki_search", "rag_query",
    # 长期记忆 / 历史召回
    "search_memory", "read_procedure", "recall_turn", "recall_spec",
    "get_session_history", "semantic_search_history",
    "list_plans", "list_specs",
    # 工具调用观测（只读）
    "get_tool_detail", "list_tool_logs", "get_tool_schemas", "cache_breakpoint",
    # 团队 / 队友观测（只读）
    "list_team", "list_agents", "agent_query_events", "agent_query_tool_detail",
    "agentid_status",
    # 服务 / 调度 / 工作流观测（只读）
    "list_services", "check_bg_task", "list_schedules",
    "list_workflows", "list_workflow_nodes", "list_workflow_outputs",
    "read_workflow_spec", "read_workflow_demo", "query_workflow_node",
    "list_downloadable",
    # 外部信息获取（只读）
    "web_search", "open_url", "file_parser", "web_reader",
    "readme",
})

_READ_PREFIXES = (
    "read_", "list_", "get_", "search_", "recall_", "diff_", "query_",
    "__mcp__python-lsp__py_",   # 纯静态分析（定义/引用/符号/诊断）
)


def classify_tool(name: str) -> str:
    """工具三分类：'read'（只读/召回，可执行）/ 'write'（写/执行，拒绝）/ 'unknown'（未知，拒绝）。
    顺序=黑名单优先（前缀白名单不会误放行写类）→ 白名单 → 未知。"""
    n = (name or "").strip()
    if not n:
        return "unknown"
    if n in _WRITE_TOOLS or n.startswith(_WRITE_PREFIXES):
        return "write"
    if n in _READ_TOOLS or n.startswith(_READ_PREFIXES):
        return "read"
    return "unknown"


_REFUSE_WRITE = (
    "[询问场景·已拒绝] '{name}' 是写/执行类工具，本次询问不允许执行（因为是无状态旁路询问，"
    "被问方环境不应被改动）。请直接基于你已有的上下文回答；需要的事实可用只读工具自查"
    "（read_file / list_dir / grep / wiki_read / search_memory / recall_turn 等）。"
    "如果确实需要执行操作，请让提问方改用 agent_prompt / remote_message 派活。"
)

_REFUSE_UNKNOWN = (
    "[询问场景·受限] 工具 '{name}' 不在询问场景的只读白名单内（无法判定为只读），已跳过执行。"
    "请改用明确只读的工具（read_file / list_dir / grep / wiki_read / search_memory 等），"
    "或直接基于已有信息回答。"
)

_ASK_PROMPT = """[来自{asker}的询问] {question}

（这是**无状态询问**场景：你的上下文已拷贝一份用于回答，本次不会改动你的会话历史。
你可以调用只读/召回类工具核对事实（read_file / list_dir / grep / wiki_* / search_memory /
recall_turn / get_* / list_* 等）；写文件、run_shell、run_python 等写/执行类工具本次会被拒绝。
请专注于回答问题本身，尽快给出直接、可用的回答。）"""

_DEGRADE_PROMPT = (
    "[系统·询问预算] 询问时间预算已到，本轮立即停止调用工具。"
    "请仅基于以上已有信息（含已拿到的只读结果）直接给出最终回答；不确定的部分明确说"
    "“不确定/需要对方提供”，不要再尝试任何工具调用。"
)


class AskAgent:
    """临时 agent（ask 专用）：不实例化 Agent 类，只持有【拷贝的 messages + 被问者 llm +
    受控工具执行】——天然不落盘、不写 events、不注册 registry、不跑钩子。"""

    def __init__(self, target_agent, question: str, asker: str = "",
                 budget: float = ASK_BUDGET_SECONDS):
        self.target = target_agent
        self.question = (question or "").strip()
        self.asker = (asker or "队友").strip()
        self.budget = float(budget if budget and budget > 0 else ASK_BUDGET_SECONDS)
        self.started = time.monotonic()
        self.steps = 0
        self.degraded = False
        self.refused: list[str] = []
        # —— 上下文拷贝：投影副本，只 append，不改原 session ——
        self.messages: list[dict] = self._clone_messages()

    def _clone_messages(self) -> list[dict]:
        try:
            return list(self.target.session.messages_for_llm())
        except Exception as e:
            _LOG.warning("ask 克隆上下文失败（按空上下文处理）：%s: %s", type(e).__name__, e)
            return []

    def _tool_schemas(self) -> list[dict]:
        """全量工具 schema（模型看得见自己能要什么；执行侧再按名单分流）。"""
        try:
            return self.target._llm_tool_schemas()
        except Exception:
            try:
                return self.target.tools.schemas()
            except Exception:
                return []

    def _elapsed(self) -> float:
        return time.monotonic() - self.started

    # —— 工具执行分流 ——
    def _run_tool(self, name: str, args) -> str:
        kind = classify_tool(name)
        if kind != "read":
            self.refused.append(name)
            return (_REFUSE_WRITE if kind == "write" else _REFUSE_UNKNOWN).format(name=name)
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except Exception:
                args = {}
        if not isinstance(args, dict):
            args = {}
        t0 = time.monotonic()
        try:
            out = self.target.tools.call(name, args)
        except Exception as e:
            out = f"[工具执行出错] {type(e).__name__}: {e}"
        out = str(out or "")
        if len(out) > ASK_TOOL_RESULT_CAP:
            out = out[:ASK_TOOL_RESULT_CAP] + f"\n…(已截断，原 {len(out)} 字)"
        _LOG.info("ask 只读工具 %s 耗时 %.1fs 结果 %d 字", name, time.monotonic() - t0, len(out))
        return out

    def _append_assistant(self, resp) -> list[str]:
        """把带 tool_calls 的响应按标准协议回填（assistant(tool_calls) 后紧跟各 tool 结果）。"""
        calls, cids = [], []
        for i, tc in enumerate(resp.tool_calls):
            cid = tc.get("id") or f"ask_{self.steps}_{i}"
            cids.append(cid)
            _args = tc.get("arguments")
            calls.append({
                "id": cid, "type": "function",
                "function": {"name": tc.get("name") or "",
                             "arguments": json.dumps(_args if isinstance(_args, (dict, list)) else {},
                                                     ensure_ascii=False, default=str)},
            })
        msg = {"role": "assistant", "content": resp.content or None, "tool_calls": calls}
        if getattr(resp, "reasoning", ""):
            msg["reasoning_content"] = resp.reasoning   # DS 系要求带 tool_calls 的 assistant 回填思考
        self.messages.append(msg)
        return cids

    def run(self) -> str:
        if not self.question:
            return ""
        self.messages.append({"role": "user",
                              "content": _ASK_PROMPT.format(asker=self.asker, question=self.question)})
        schemas = self._tool_schemas()
        # —— 带工具轮：只读/召回可用，写类/未知拒绝；到 60s 或步数上限 → 降级 ——
        try:
            while self.steps < ASK_MAX_STEPS and self._elapsed() < self.budget:
                resp = self.target.llm.chat(self.messages, tools=schemas, scene="ask")
                if not resp.tool_calls:
                    return (resp.content or "").strip() or "(对方返回空回答)"
                cids = self._append_assistant(resp)
                for tc, cid in zip(resp.tool_calls, cids):
                    res = self._run_tool(tc.get("name") or "", tc.get("arguments") or {})
                    self.messages.append({"role": "tool", "tool_call_id": cid, "content": res})
                self.steps += 1
        except Exception as e:
            _LOG.warning("ask 带工具轮异常（转降级直接回答）：%s: %s", type(e).__name__, e)
        # —— 降级：清空工具（tools=[]）+ 要求立即直接回答（用户提案 2026-10-22）——
        self.degraded = True
        self.messages.append({"role": "user", "content": _DEGRADE_PROMPT})
        try:
            resp2 = self.target.llm.chat(self.messages, tools=[], scene="ask")
            return (resp2.content or "").strip() or "(对方返回空回答)"
        except Exception as e:
            return f"[询问失败] {type(e).__name__}: {e}"


def answer_ask(target_agent, question: str, asker: str = "",
               budget: float = ASK_BUDGET_SECONDS) -> str:
    """统一入口：agent_ask（同进程）与 remote_ask 对端（WS action=ask）都调它。
    返回回答文本；任何异常都转 [询问失败] 文案（不抛，调用方不用兜）。"""
    if target_agent is None or getattr(target_agent, "session", None) is None:
        return "[询问失败] 目标 Agent 实例不可用"
    a = AskAgent(target_agent, question, asker=asker, budget=budget)
    ans = a.run()
    _LOG.info("ask 完成：asker=%s 源=%s 步数=%d 降级=%s 拒工具=%s 耗时=%.1fs",
              a.asker, getattr(target_agent, "agent_id", "?"), a.steps, a.degraded,
              ",".join(a.refused) or "-", a._elapsed())
    return ans
