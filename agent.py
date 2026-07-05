"""agent.py —— 自主 Agent（Step 8 强化版）。

升级点（相对早期版本）：
  - 记忆改用 Session（分层 Turn>Step>ToolCall + 摘要/窗口融合），替代朴素滑动窗口。
  - 长程自主：max_steps 默认 50（可 /config 调）；ReAct 循环"工具失败→观察→再改"天然支持闭环。
  - 软 token 预算：累计到预算上限强制收尾（一次无工具的总结调用），防止失控烧钱。
  - Ctrl+C 优雅打断：保留已完成的轮次到会话，不丢上下文。
"""
from __future__ import annotations

import config
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional

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
    ):
        self.base_system = system
        self.tools = tools
        self.max_steps = max_steps
        self.token_budget = token_budget
        self.verbose = verbose
        self.model_name = model_name or config.DEFAULT_MODEL

        self.llm = LLMClient(model_name=self.model_name,
                             temperature=temperature, enable_thinking=enable_thinking)
        self.session = Session(system, llm=self.llm, recent_window_turns=recent_window_turns)
        self.cumulative_tokens = 0
        self.sub_agents: dict = {}  # 多 Agent 协作：name -> SubAgent

    def _log(self, *args):
        if self.verbose:
            print(*args)

    def switch_model(self, name: str):
        """热切换模型。Session 共用 self.llm，故摘要调用也跟着切。"""
        self.llm.switch_model(name)
        self.model_name = name

    @staticmethod
    def _truncate(s, n=500):
        s = str(s)
        return s if len(s) <= n else s[:n] + f"...(+{len(s) - n}字)"

    def _run_tools_parallel(self, calls: list) -> list:
        """并行执行一组工具调用，按原顺序返回结果。子 Agent 各有独立 LLMClient/Session，线程安全。"""
        results = [None] * len(calls)
        with ThreadPoolExecutor(max_workers=len(calls)) as ex:
            fut_to_idx = {
                ex.submit(self.tools.call, tc["name"], tc["arguments"]): i
                for i, tc in enumerate(calls)
            }
            for fut in as_completed(fut_to_idx):
                i = fut_to_idx[fut]
                try:
                    results[i] = fut.result()
                except Exception as e:
                    results[i] = f"[执行出错] {type(e).__name__}: {e}"
        return results

    def run(self, user_message: str) -> str:
        """跑 ReAct 主循环。每步记录到当前 Turn；超步数/预算则优雅收尾。"""
        self.session.start_turn(user_message)
        self._log(f"\n🧑 用户: {user_message}")
        tool_schemas = self.tools.schemas()

        try:
            for step_num in range(1, self.max_steps + 1):
                # —— 软预算硬墙：到顶就收尾 ——
                if self.cumulative_tokens >= self.token_budget:
                    self._log(f"\n⚠️ token 预算({self.token_budget})已用尽，强制收尾。")
                    return self._wrap_up()

                self._log(f"\n{GRAY}━━━ 第 {step_num} 步 (累计 {self.cumulative_tokens} token) ━━━{RESET}")
                resp = self.llm.chat(self.session.messages_for_llm(), tools=tool_schemas)
                if resp.usage:
                    self.cumulative_tokens += resp.usage.get("total_tokens", 0)

                # 预算 80% 提醒（仅打印；不往消息里插，避免打乱工具流）
                if self.cumulative_tokens >= self.token_budget * 0.8:
                    self._log(f"{GRAY}⚠️ 预算已用 80%+，即将触顶收尾{RESET}")

                if resp.reasoning:
                    snippet = resp.reasoning[:200].replace("\n", " ")
                    if len(resp.reasoning) > 200:
                        snippet += "..."
                    self._log(f"{GRAY}[思考] {snippet}{RESET}")

                # 不再调用工具 → 最终答案
                if not resp.tool_calls:
                    self.session.finish_turn(resp.content)
                    self._log(f"\n🤖 最终回答: {resp.content.strip()}")
                    self._log(f"{GRAY}[本次累计 token: {self.cumulative_tokens}]{RESET}")
                    return resp.content

                # 执行工具，记录到当前 Turn 的一个 Step
                step = Step(reasoning=resp.reasoning)
                calls = resp.tool_calls
                if len(calls) == 1:
                    tc = calls[0]
                    self._log(f"🔧 调用 {tc['name']}({tc['arguments']})")
                    result = self.tools.call(tc["name"], tc["arguments"])
                    self._log(f"   → {self._truncate(result)}")
                    step.tool_calls.append(ToolCall(
                        id=tc.get("id", ""), name=tc["name"],
                        arguments=tc["arguments"], result=result))
                else:
                    # 单步多工具调用：并行执行（如多个 agent_prompt 派给不同子 Agent）
                    self._log(f"{GRAY}⚡ 并行执行 {len(calls)} 个工具调用{RESET}")
                    for tc in calls:
                        self._log(f"🔧 调用 {tc['name']}({tc['arguments']})")
                    results = self._run_tools_parallel(calls)
                    for tc, result in zip(calls, results):
                        self._log(f"   → [{tc['name']}] {self._truncate(result)}")
                        step.tool_calls.append(ToolCall(
                            id=tc.get("id", ""), name=tc["name"],
                            arguments=tc["arguments"], result=result))
                self.session.add_step(step)

            self._log(f"\n⚠️ 达到最大步数 {self.max_steps}，强制收尾。")
            return self._wrap_up()

        except KeyboardInterrupt:
            self._log("\n\n⏹ 已中断（已完成的轮次保留在会话中，可用 /save 保存）。")
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
        self._log(f"\n🤖 收尾回答: {answer.strip()}")
        return answer
