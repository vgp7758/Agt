"""agent.py —— 自主 Agent（事件化输出，CLI 与 Web 各取所需）。

输出抽象成结构化事件流：`_emit(event)`。若设了 `on_event`（如 Web 后端）则回调它；
同时若 `verbose=True` 则 `_print_event` 复刻原控制台格式。故 `chat.py`（不设 on_event、
verbose=True）输出与之前完全一致；`web.py` 设 on_event 把事件推给浏览器。

能力：ReAct 主循环、长程自主、单步并行工具、软 token 预算、Ctrl+C 优雅打断、
多模型热切换、多 Agent（self.sub_agents）、定时纯自主模式。
"""
from __future__ import annotations

import config
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Callable, Optional, List

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
        max_steps_per_turn: int = 80,
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
        self.session = Session(system, llm=self.llm, recent_window_turns=recent_window_turns,
                               max_steps_per_turn=max_steps_per_turn)
        self.cumulative_tokens = 0
        self.sub_agents: dict = {}  # 多 Agent 协作：name -> SubAgent
        self.plan: list = []        # 计划清单（create_plan/update_plan 维护）
        # 纯自主模式状态
        self.autonomous_mode: bool = False
        self.autonomous_end_time: Optional[datetime] = None
        self.autonomous_prompt: str = "当前为纯自主模式，请继续按照要求完成更多工作"
        self.pending_messages: List[str] = []  # 用户插入的消息队列
        self.goal_check_script: str = ""       # 目标达成验证脚本(Python，输出 PASS=达成)

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
            print(f"\n🧑 用户：{e['text']}")
        elif t == "step":
            print(f"\n{GRAY}━━━ 第 {e['n']} 步 (累计 {e['tokens']} token) ━━━{RESET}")
        elif t == "warn":
            print(f"{GRAY}{e['text']}{RESET}")
        elif t == "budget_hit":
            print(f"\n⚠️ token 预算 ({self.token_budget}) 已用尽，强制收尾。")
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
            print(f"\n🤖 最终回答：{e['text'].strip()}")
            print(f"{GRAY}[本次累计 token: {e.get('tokens', self.cumulative_tokens)}]{RESET}")
        elif t == "wrap_up":
            print(f"\n⚠️ 达到最大步数 {self.max_steps}，强制收尾。")
        elif t == "wrap_answer":
            print(f"\n🤖 收尾回答：{e['text'].strip()}")
        elif t == "interrupted":
            print("\n\n⏹ 已中断（已完成的轮次保留在会话中，可用 /save 保存）。")
        elif t == "autonomous_status":
            if e.get("active"):
                print(f"\n🔁 纯自主模式已开启，持续到 {e['end_time']}")
            else:
                print("\n🔁 纯自主模式已关闭")
        elif t == "autonomous_continue":
            print(f"\n{GRAY}🔁 自主继续：{e['text']}{RESET}")
        elif t == "autonomous_next":
            print(f"{GRAY}🔁 准备自主继续：{e['text']}{RESET}")
        elif t == "message_queued":
            print(f"{GRAY}📨 消息已入队（队列大小：{e['queue_size']}）{RESET}")

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

    def set_autonomous_mode(self, end_time: datetime, prompt: str = None):
        """设置纯自主模式：到 end_time 之前，任务完成后自动继续。
        prompt: 自动继续时使用的提示词（默认使用预设提示）。"""
        self.autonomous_mode = True
        self.autonomous_end_time = end_time
        if prompt:
            self.autonomous_prompt = prompt
        self._emit({"type": "autonomous_status", "active": True, "end_time": end_time.isoformat(),
                    "prompt": self.autonomous_prompt})

    def exit_autonomous_mode(self):
        """退出纯自主模式。"""
        self.autonomous_mode = False
        self.autonomous_end_time = None
        self._emit({"type": "autonomous_status", "active": False})

    def is_autonomous_active(self) -> bool:
        """检查纯自主模式是否仍有效（未超时且未被手动关闭）。"""
        if not self.autonomous_mode:
            return False
        if self.autonomous_end_time and datetime.now() > self.autonomous_end_time:
            self.exit_autonomous_mode()
            return False
        return True

    def queue_user_message(self, text: str):
        """在自主模式下，将用户消息加入队列（等当前任务完成后注入）。"""
        if self.autonomous_mode:
            self.pending_messages.append(text)
            self._emit({"type": "message_queued", "text": text, "queue_size": len(self.pending_messages)})
            return True
        return False

    def get_next_message(self) -> Optional[str]:
        """获取下一条要处理的消息（优先队列中的用户消息，否则用自主提示）。"""
        if self.pending_messages:
            return self.pending_messages.pop(0)
        if self.is_autonomous_active():
            return self.autonomous_prompt
        return None

    def run_goal_check(self) -> str:
        """运行目标验证脚本（独立子进程），返回输出。'PASS' 表示目标达成。"""
        if not self.goal_check_script:
            return ""
        import subprocess, sys, tempfile, os
        with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False, encoding="utf-8") as f:
            f.write(self.goal_check_script)
            tmp = f.name
        try:
            proc = subprocess.run([sys.executable, tmp], capture_output=True,
                                  text=True, timeout=30, cwd=os.getcwd())
            return (proc.stdout or "").strip()
        except subprocess.TimeoutExpired:
            return "[目标检查超时]"
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass

    # ========== ReAct 主循环 ==========
    def run(self, user_message: str, images: Optional[list] = None, _autonomous_continue: bool = False) -> str:
        """
        :param user_message: 用户消息（自主继续时为自动生成的提示）
        :param images: 图片列表
        :param _autonomous_continue: 内部标记，表示这是自主继续的一轮（用于事件区分）
        """
        # 用循环替代递归：自主继续时走下一轮迭代而不是 self.run() 递归
        msg, auto_flag, imgs = user_message, _autonomous_continue, images
        while True:
            self._stop_flag = False
            self.session.start_turn(msg, imgs)
            if not auto_flag:
                self._emit({"type": "user", "text": msg, "image_count": len(imgs or [])})
            else:
                self._emit({"type": "autonomous_continue", "text": msg})
            if self.snapshot_manager is not None:
                try:
                    sha = self.snapshot_manager.snapshot()
                    self.session._current.snapshot_sha = sha
                    self._emit({"type": "checkpoint", "sha": sha})
                except Exception as e:
                    self._emit({"type": "warn", "text": f"快照失败：{type(e).__name__}: {e}"})
            tool_schemas = self.tools.schemas()
            continue_loop = False
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
                            # 目标检查：跑验证脚本，PASS 则结束自主模式
                            if self.goal_check_script:
                                result = self.run_goal_check()
                                if result and result.startswith("PASS"):
                                    self._emit({"type": "system", "text": f"🎯 目标达成：{result}"})
                                    self.exit_autonomous_mode()
                            # 纯自主模式：完成后检查是否继续
                            if self.is_autonomous_active():
                                next_msg = self.get_next_message()
                                if next_msg:
                                    self._emit({"type": "autonomous_next", "text": next_msg})
                                    msg, auto_flag, imgs, continue_loop = next_msg, True, None, True
                                    break
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

                    if continue_loop:
                        continue
                    self._emit({"type": "wrap_up"})
                    if self.is_autonomous_active():
                        next_msg = self.get_next_message()
                        if next_msg:
                            self._emit({"type": "autonomous_next", "text": next_msg})
                            msg, auto_flag, imgs, continue_loop = next_msg, True, None, True
                            continue
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
