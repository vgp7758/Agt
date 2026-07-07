"""agent.py —— 自主 Agent（事件化输出，CLI 与 Web 各取所需）。

输出抽象成结构化事件流：`_emit(event)`。若设了 `on_event`（如 Web 后端）则回调它；
同时若 `verbose=True` 则 `_print_event` 复刻原控制台格式。故 `chat.py`（不设 on_event、
verbose=True）输出与之前完全一致；`web.py` 设 on_event 把事件推给浏览器。

能力：ReAct 主循环、长程自主、单步并行工具、软 token 预算、Ctrl+C 优雅打断、
多模型热切换、多 Agent（self.sub_agents）。
"""
from __future__ import annotations

import config
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable, Optional

from llm_client import LLMClient
from session import Session, Step, ToolCall
from tools import Toolbox

GRAY, RESET = "\033[90m", "\033[0m"


class Agent:
    def __init__(
        self,
        system: str,
        tools: Toolbox,
        *,
        enable_thinking: bool = True,
        max_steps: int = 50,
        token_budget: int = 80000,
        temperature: float = 0.7,
        verbose: bool = True,
        recent_window_turns: int = 4,
        model_name: Optional[str] = None,
        on_event: Optional[Callable[[dict], None]] = None,
        snapshot_manager=None,
    ):
        self.base_system = system
        self.tools = tools
        self.max_steps = max_steps
        self.token_budget = token_budget
        self.verbose = verbose
        self.on_event = on_event
        self.snapshot_manager = snapshot_manager
        self.model_name = model_name or config.DEFAULT_MODEL

        self.llm = LLMClient(model_name=self.model_name,
                             temperature=temperature, enable_thinking=enable_thinking)
        self.session = Session(system, llm=self.llm, recent_window_turns=recent_window_turns)
        self.cumulative_tokens = 0
        self.sub_agents: dict = {}  # 多 Agent 协作：name -> SubAgent
        self.plan: list = []        # 计划清单（create_plan/update_plan 维护）

    # ========== 事件输出 ==========
    def _emit(self, event: dict):
        """发一个事件：回调 on_event（Web）；verbose 时打印（CLI）。"""
        if self.on_event:
            try:
                self.on_event(event)
            except Exception:
                pass
        if self.verbose:
            self._print_event(event)

    def _print_event(self, e: dict):
        """复刻原控制台输出格式（保证 CLI 行为不变）。"""
        t = e.get("type")
        if t == "user":
            print(f"\n🧑 用户: {e['text']}")
        elif t == "step":
            print(f"\n{GRAY}━━━ 第 {e['n']} 步 (累计 {e['tokens']} token) ━━━{RESET}")
        elif t == "warn":
            print(f"{GRAY}{e['text']}{RESET}")
        elif t == "budget_hit":
            print(f"\n⚠️ token 预算({self.token_budget})已用尽，强制收尾。")
        elif t == "thinking":
            print(f"{GRAY}[思考] {e['text']}{RESET}")
        elif t == "parallel":
            print(f"{GRAY}⚡ 并行执行 {e['count']} 个工具调用{RESET}")
        elif t == "tool_call":
            print(f"🔧 调用 {e['name']}({e['arguments']})")
        elif t == "tool_result":
            prefix = f"   → [{e['name']}]" if e.get("parallel") else "   →"
            print(f"{prefix} {e['result']}")
        elif t == "answer":
            print(f"\n🤖 最终回答: {e['text'].strip()}")
            print(f"{GRAY}[本次累计 token: {e.get('tokens', self.cumulative_tokens)}]{RESET}")
        elif t == "wrap_up":
            print(f"\n⚠️ 达到最大步数 {self.max_steps}，强制收尾。")
        elif t == "wrap_answer":
            print(f"\n🤖 收尾回答: {e['text'].strip()}")
        elif t == "interrupted":
            print("\n\n⏹ 已中断（已完成的轮次保留在会话中，可用 /save 保存）。")

    @staticmethod
    def _truncate(s, n=500):
        s = str(s)
        return s if len(s) <= n else s[:n] + f"...(+{len(s) - n}字)"

    def _run_tools_parallel(self, calls: list) -> list:
        """并行执行一组工具调用，按原顺序返回结果。"""
        results = [None] * len(calls)
        with ThreadPoolExecutor(max_workers=len(calls)) as ex:
            fut_to_idx = {ex.submit(self.tools.call, tc["name"], tc["arguments"]): i
                          for i, tc in enumerate(calls)}
            for fut in as_completed(fut_to_idx):
                i = fut_to_idx[fut]
                try:
                    results[i] = fut.result()
                except Exception as e:
                    results[i] = f"[执行出错] {type(e).__name__}: {e}"
        return results

    def switch_model(self, name: str):
        """热切换模型。Session 共用 self.llm，故摘要调用也跟着切。"""
        self.llm.switch_model(name)
        self.model_name = name

    # ========== ReAct 主循环 ==========
    def run(self, user_message: str, images: Optional[list] = None) -> str:
        self._stop_flag = False  # 每轮 run 开始时清掉停止标志
        self.session.start_turn(user_message, images)
        self._emit({"type": "user", "text": user_message, "image_count": len(images or [])})
        # 检查点：本轮工具改动前给工作区打快照（用于"回溯到这条指令之前"）
        if self.snapshot_manager is not None:
            try:
                sha = self.snapshot_manager.snapshot()
                self.session._current.snapshot_sha = sha
                self._emit({"type": "checkpoint", "sha": sha})
            except Exception as e:
                self._emit({"type": "warn", "text": f"快照失败: {type(e).__name__}: {e}"})
        tool_schemas = self.tools.schemas()

        try:
            for step_num in range(1, self.max_steps + 1):
                if self._stop_flag:
                    self._emit({"type": "interrupted"})
                    self.session.abort_current_turn("（被用户停止）")
                    return ""
                if self.cumulative_tokens >= self.token_budget:
                    self._emit({"type": "budget_hit"})
                    return self._wrap_up()

                self._emit({"type": "step", "n": step_num, "tokens": self.cumulative_tokens})
                resp = self.llm.chat(self.session.messages_for_llm(), tools=tool_schemas)
                if resp.usage:
                    self.cumulative_tokens += resp.usage.get("total_tokens", 0)

                if self.cumulative_tokens >= self.token_budget * 0.8:
                    self._emit({"type": "warn", "text": "⚠️ 预算已用 80%+，即将触顶收尾"})

                if resp.reasoning:
                    snippet = resp.reasoning[:200].replace("\n", " ")
                    if len(resp.reasoning) > 200:
                        snippet += "..."
                    self._emit({"type": "thinking", "text": snippet})

                # 不再调用工具 → 最终答案
                if not resp.tool_calls:
                    self.session.finish_turn(resp.content, resp.reasoning)
                    self._emit({"type": "answer", "text": resp.content,
                                "tokens": self.cumulative_tokens})
                    return resp.content

                # 执行工具
                calls = resp.tool_calls
                step = Step(reasoning=resp.reasoning)
                if len(calls) == 1:
                    tc = calls[0]
                    self._emit({"type": "tool_call", "name": tc["name"], "arguments": tc["arguments"]})
                    result = self.tools.call(tc["name"], tc["arguments"])
                    self._emit({"type": "tool_result", "name": tc["name"],
                                "result": self._truncate(result), "parallel": False})
                    step.tool_calls.append(ToolCall(id=tc.get("id", ""), name=tc["name"],
                                                     arguments=tc["arguments"], result=result))
                else:
                    self._emit({"type": "parallel", "count": len(calls)})
                    for tc in calls:
                        self._emit({"type": "tool_call", "name": tc["name"], "arguments": tc["arguments"]})
                    results = self._run_tools_parallel(calls)
                    for tc, result in zip(calls, results):
                        self._emit({"type": "tool_result", "name": tc["name"],
                                    "result": self._truncate(result), "parallel": True})
                        step.tool_calls.append(ToolCall(id=tc.get("id", ""), name=tc["name"],
                                                         arguments=tc["arguments"], result=result))
                self.session.add_step(step)

            self._emit({"type": "wrap_up"})
            return self._wrap_up()

        except KeyboardInterrupt:
            self._emit({"type": "interrupted"})
            self.session.abort_current_turn("（被用户中断）")
            return ""

    def _wrap_up(self) -> str:
        """预算/步数到顶时，做一次无工具的总结性收尾。"""
        msgs = self.session.messages_for_llm() + [{
            "role": "system",
            "content": "token 预算或步数已达上限。请基于目前已有的工具结果，直接给出最终总结性回答，不要再调用工具。"
        }]
        try:
            resp = self.llm.chat(msgs)
            answer = resp.content
            if resp.usage:
                self.cumulative_tokens += resp.usage.get("total_tokens", 0)
        except Exception as e:
            answer = f"（收尾调用失败：{e}）"
        self.session.finish_turn(answer)
        self._emit({"type": "wrap_answer", "text": answer})
        return answer
