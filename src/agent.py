"""agent.py —— 自主 Agent（事件化输出，CLI 与 Web 各取所需）。

输出抽象成结构化事件流：`_emit(event)`。若设了 `on_event`（如 Web 后端）则回调它；
同时若 `verbose=True` 则 `_print_event` 复刻原控制台格式。故 `chat.py`（不设 on_event、
verbose=True）输出与之前完全一致；`web.py` 设 on_event 把事件推给浏览器。

能力：ReAct 主循环、长程自主、单步并行工具、软 token 预算、Ctrl+C 优雅打断、
多模型热切换、多 Agent（self.sub_agents）、定时纯自主模式。
"""
from __future__ import annotations

import collections
import difflib
import json
import logging
import threading
import time

import config
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Callable, Optional, List

from background import ServiceManager, Scheduler
from llm_client import LLMClient
from log import configure_logging
from longterm_memory import LongTermMemory
from session import Session, Step, ToolCall
from tools import Toolbox

_LOG = logging.getLogger("agt.agent")

GRAY, RESET = "\033[90m", "\033[0m"
GREEN, RED = "\033[32m", "\033[31m"


def _render_edit_cli(path, old, new) -> str:
    """edit 的行级 diff 渲染（红删 / 绿增），只显示变化行，紧凑不刷屏。"""
    lines = [f"✏️ edit {path}"]
    for dl in difflib.ndiff((old or "").splitlines(), (new or "").splitlines()):
        if dl.startswith("- "):
            lines.append(f"  {RED}-{dl[2:]}{RESET}")
        elif dl.startswith("+ "):
            lines.append(f"  {GREEN}+{dl[2:]}{RESET}")
        # 跳过 "  "（不变行）与 "? "（ndiff 提示行）
    return "\n".join(lines)


def _render_write_cli(path, content, max_lines: int = 15) -> str:
    """write_file 的新建预览渲染（全绿 +，超长截断首 max_lines 行）。"""
    all_lines = (content or "").splitlines()
    show = all_lines[:max_lines]
    lines = [f"📝 write_file {path}（新建 · {len(all_lines)} 行）"]
    for l in show:
        lines.append(f"  {GREEN}+{l}{RESET}")
    if len(all_lines) > max_lines:
        lines.append(f"  {GRAY}…（省略 {len(all_lines) - max_lines} 行）{RESET}")
    return "\n".join(lines)


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
        self.session._state_provider = self.capture_runtime_state  # session 落盘时收集 plan/自主模式状态
        self.session._system_extra_provider = self._runtime_system_extra  # system prompt 实时注入后台服务状态
        # 长期记忆（per-repo，跨 session）：建库 + 挂两个注入 provider 到 session
        #   - 静态层（semantic 事实 + procedural 标题）每轮始终注入
        #   - 情境层（episodic）按当前 user_message 每轮召回注入
        self.ltm = LongTermMemory(self.session.workspace)
        self.session._ltm_static_provider = self._ltm_static_block
        self.session._ltm_episodic_provider = self._ltm_episodic_block
        # 日志：配置根 agt logger（文件跟 session 走 + 控制台默认 WARNING+），handler 接到 session
        self._log_handler = configure_logging()
        self._log_handler.set_session(self.session.workspace, self.session.name)
        self.session._log_handler = self._log_handler
        # 后台服务 + 定时调度（producer）→ inbox → run()内循环 / chat/web 消费者 串行触发
        self.inbox: collections.deque = collections.deque()
        self._inbox_lock = threading.Lock()
        self.services = ServiceManager()
        self.scheduler = Scheduler(self)
        self.cumulative_tokens = 0
        self.sub_agents: dict = {}  # 多 Agent 协作：name -> SubAgent
        self.plan: list = []        # 计划清单（create_plan/update_plan 维护）
        # 纯自主模式状态
        self.autonomous_mode: bool = False
        self.autonomous_end_time: Optional[datetime] = None
        self.autonomous_prompt: str = "当前为纯自主模式，请继续按照要求完成更多工作"
        self.pending_messages: List[str] = []  # 用户插入的消息队列
        self.goal_check_script: str = ""       # 目标达成验证脚本(Python，输出 PASS=达成)
        # —— 工作流生命周期钩子（每轮 run 开头重置）——
        self._hook_notes: list[str] = []        # 待注入的 system 旁注（before_tool/after_tool/before_answer）
        self._answer_redo_draft: Optional[str] = None   # before_answer 重跑时上一次草稿（临时 assistant 续接）
        self._last_answer_draft: Optional[str] = None   # 收敛判据：上次注入所针对的草稿
        self._answer_inject_count: int = 0      # 本轮 before_answer 注入次数（封顶 5 防死循环）

    # ========== 事件输出 ==========
    def _print_only_emit(self, event: dict):
        """CLI 模式的流式回调：tool_stream/tool_progress 直接打印。"""
        t = event.get("type")
        if t == "tool_stream":
            print(f"{GRAY}{event.get('text', '')}{RESET}", end="", flush=True)
        elif t == "tool_progress":
            print(f"{GRAY}⏳ {event['name']} 已运行 {event['elapsed']}s，{event.get('lines', 0)} 行输出{RESET}")

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
            _n, _a = e["name"], e["arguments"]
            if _n == "edit":
                print(_render_edit_cli(_a.get("path", ""), _a.get("old_string", ""), _a.get("new_string", "")))
            elif _n == "write_file":
                print(_render_write_cli(_a.get("path", ""), _a.get("content", "")))
            else:
                print(f"🔧 调用 {_n}({_a})")
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
        elif t == "tool_stream":
            # CLI 模式：流式输出直接 print，不加换行（全靠子进程自己控制）
            pass  # _print_only_emit 已在流式回调中处理
        elif t == "tool_progress":
            print(f"{GRAY}⏳ {e['name']} 运行中 {e['elapsed']}s，{e.get('lines',0)} 行输出{RESET}")
        elif t == "auto_wf_start":
            print(f"{GRAY}🔍 自动工作流[{e['name']}] 执行中…（参数 {e.get('param','?')}={e.get('input','')[:60]}）{RESET}")
        elif t == "auto_wf":
            print(f"{GRAY}🔍 自动工作流[{e['name']}] 完成: {e['text'][:120]}{RESET}")
        elif t == "auto_wf_error":
            print(f"{GRAY}❌ 自动工作流[{e['name']}] 失败: {e['text'][:120]}{RESET}")
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

    # ========== 运行时状态的存取（随 session 落盘/恢复）==========
    def capture_runtime_state(self) -> dict:
        """收集要随 session 存档保留的运行时状态（resume 时恢复）。"""
        return {
            "plan": [dict(s) for s in self.plan],
            "autonomous_mode": self.autonomous_mode,
            "autonomous_end_time": self.autonomous_end_time.isoformat() if self.autonomous_end_time else None,
            "autonomous_prompt": self.autonomous_prompt,
            "goal_check_script": self.goal_check_script,
        }

    def restore_runtime_state(self, state: dict):
        """从存档恢复运行时状态（resume / 切换 session 后调用）。"""
        if state:
            self.plan = [dict(s) for s in state.get("plan", [])]
            self.autonomous_mode = bool(state.get("autonomous_mode", False))
            end = state.get("autonomous_end_time")
            self.autonomous_end_time = datetime.fromisoformat(end) if end else None
            if "autonomous_prompt" in state:
                self.autonomous_prompt = state["autonomous_prompt"]
            if "goal_check_script" in state:
                self.goal_check_script = state["goal_check_script"]
        self._emit_plan_if_any()

    def set_session(self, session):
        """切换到指定 session：换引用 + 重新挂状态收集回调 + 恢复附加状态 + 同步 UI。
        所有 resume / reset / new_session 都应走这里，保证 provider 与附加状态一致。"""
        self.session = session
        session._state_provider = self.capture_runtime_state
        session._system_extra_provider = self._runtime_system_extra
        session._ltm_static_provider = self._ltm_static_block      # 长期记忆·静态层
        session._ltm_episodic_provider = self._ltm_episodic_block  # 长期记忆·情境层
        session._log_handler = self._log_handler                   # 日志 handler 跟到新 session
        self._log_handler.set_session(session.workspace, session.name)
        self.restore_runtime_state(session.extra_state)

    def _emit_plan_if_any(self):
        """把当前 plan 推给 UI（resume 后让前端 plan 面板同步）。"""
        if getattr(self, "on_event", None):
            try:
                self.on_event({"type": "plan", "plan": [dict(s) for s in self.plan]})
            except Exception:
                pass

    # ========== 后台消息 inbox（producer → inbox → 串行消费者 → run） ==========
    def push_message(self, msg: str, source: str = "background"):
        """后台/调度器推一条消息进 inbox，等 Agent 空闲时触发一轮 run。线程安全。
        被 background.Scheduler / ServiceManager 等后台线程调用。"""
        with self._inbox_lock:
            self.inbox.append((source, msg))
        self._emit({"type": "background_trigger", "source": source,
                    "text": (msg or "")[:80], "queue_size": len(self.inbox)})

    def pop_inbox(self):
        """取一条 inbox 消息 (source, msg)，空则 None。线程安全。
        两处消费点（run() 内循环 / chat/web 主循环 drain）都调它，锁保证不重复消费。"""
        with self._inbox_lock:
            return self.inbox.popleft() if self.inbox else None

    def _runtime_system_extra(self) -> str:
        """动态注入 system prompt 的运行时段：当前后台服务清单 + 状态。
        无服务返回空串（不注入），有则 Agent 每步都能看到哪些在跑/已断，不必自己查。"""
        lines = self.services.status_lines()
        if not lines:
            return ""
        return "【后台服务状态】当前服务：\n" + "\n".join(lines)

    def _ltm_static_block(self) -> str:
        """长期记忆·静态层注入：semantic 事实 + procedural 标题（每轮始终注入）。失败静默不炸主循环。"""
        try:
            return self.ltm.static_block()
        except Exception:
            return ""

    def _ltm_episodic_block(self, query: str) -> str:
        """长期记忆·情境层注入：按当前 user_message 召回 episodic（每轮按需）。失败静默。"""
        try:
            return self.ltm.episodic_block(query)
        except Exception:
            return ""

    def shutdown(self):
        """退出时清理：停所有后台服务（防孤儿进程）+ 停调度器。供 chat/web 退出时调。"""
        try:
            self.services.stop_all()
        except Exception:
            pass
        try:
            self.scheduler.stop()
        except Exception:
            pass

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

    # ========== 工作流生命周期钩子 ==========
    def _run_hooks(self, hook: str, context: dict) -> list[str]:
        """运行所有声明在 hook 位置触发的工作流，返回需注入的 result 列表（已带【...】前缀）。
        context: 该钩子位置的上下文（key 对应工作流开始节点 <out> 声明）。
        工作流约定返回 {inject, result, message}：
          - inject=True 且 result 非空 → 加入返回列表（作 system 旁注喂主 LLM）；
          - message 非空 → 发 workflow_message 事件到 UI（不进主 LLM，用于静默通知类钩子）。
        失败仅发 auto_wf_error 事件，绝不炸主循环。"""
        notes = []
        try:
            from real_tools import WORKSPACE as _ws
            from workflow import get_hook_workflows, run_hook
            for hw in get_hook_workflows(_ws, hook):
                try:
                    self._emit({"type": "auto_wf_start", "name": hw["name"], "hook": hook,
                                "text": str(context)[:80]})
                    inject, result, message = run_hook(hw["canvas"], context,
                                              tools=self.tools, llm=self.llm, workspace=_ws)
                    self._emit({"type": "auto_wf", "name": hw["name"], "hook": hook,
                                "text": result[:300] or message[:300]})
                    if message.strip():
                        # 系统消息：仅 UI 可见，不进主 LLM（如 wiki 自动维护报告）
                        self._emit({"type": "workflow_message", "name": hw["name"], "hook": hook,
                                    "text": message})
                    if inject and result.strip():
                        notes.append(f"【{hook} 钩子「{hw['name']}」补充】{result}")
                except Exception as e2:
                    self._emit({"type": "auto_wf_error", "name": hw["name"], "hook": hook,
                                "text": str(e2)[:200]})
        except Exception as e:
            _LOG.error("钩子机制异常(%s): %s", hook, e)  # 钩子机制本身绝不影响主循环
        return notes

    def _turn_context_str(self) -> str:
        """本轮（进行中）的工具调用摘要，供 before_answer 钩子（如 wiki 自动维护）判断
        '本轮做了什么值得记录'。格式：每步每调用一行 `name(args)→result[:150]`。"""
        cur = self.session._current
        if not cur or not cur.steps:
            return ""
        lines = []
        for step in cur.steps:
            for tc in step.tool_calls:
                name, args, result = self.session.toollog.view(tc.call_id)
                args_s = json.dumps(args, ensure_ascii=False)
                if len(args_s) > 120:
                    args_s = args_s[:117] + "..."
                res = result or ""
                if len(res) > 150:
                    res = res[:147] + "..."
                lines.append(f"- {name}({args_s}) → {res}")
        return "\n".join(lines)

    def _chat_msgs(self) -> list:
        """构造喂给 LLM 的消息：session 上下文 + 排空 hook 旁注 + before_answer 重做草稿（临时 assistant 续接）。
        排空（清空 _hook_notes）保证一次注入只喂一次，重试 chat 二次调用时已空，天然不重复注入。"""
        msgs = list(self.session.messages_for_llm())
        notes = self._hook_notes
        self._hook_notes = []
        if self._answer_redo_draft is not None:
            # before_answer 重跑：让模型看到它上一版草稿，再据旁注修正
            msgs.append({"role": "assistant", "content": self._answer_redo_draft})
            self._answer_redo_draft = None
        for n in notes:
            msgs.append({"role": "system", "content": n})
        return msgs

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
            # —— 重置本轮钩子状态 ——
            self._hook_notes = []
            self._answer_redo_draft = None
            self._last_answer_draft = None
            self._answer_inject_count = 0
            self._active_hooks = set()
            # —— before_turn 钩子（旧 auto:true ≡ before_turn）：用当前消息作输入预执行 ——
            # 注入方式：拼进用户消息（RAG 风格强化输入）。兼容旧 auto_param 参数名。
            bt_ctx = {"user_message": msg}
            try:
                from real_tools import WORKSPACE as _ws2
                from workflow import get_hook_workflows
                for aw in get_hook_workflows(_ws2, "before_turn"):
                    if aw.get("auto_param"):
                        bt_ctx[aw["auto_param"]] = msg
            except Exception:
                pass
            bt_notes = self._run_hooks("before_turn", bt_ctx)
            if bt_notes and not auto_flag:
                msg = "\n\n".join(bt_notes) + "\n\n---\n用户消息：" + msg
            if not auto_flag:
                self._emit({"type": "user", "text": msg, "image_count": len(imgs or [])})
            else:
                self._emit({"type": "autonomous_continue", "text": msg})
            _LOG.info("run 开始 session=%s: %s", self.session.name or "(未命名)", (msg or "")[:60])
            if self.snapshot_manager is not None:
                try:
                    sha = self.snapshot_manager.snapshot()
                    self.session._current.snapshot_sha = sha
                    self._emit({"type": "checkpoint", "sha": sha})
                except Exception as e:
                    self._emit({"type": "warn", "text": f"快照失败：{type(e).__name__}: {e}"})
            # 每轮扫描 .agent/workflows/，把工作流刷新成工具（新增/改动的工作流即时生效）
            try:
                from real_tools import WORKSPACE as _ws
                from workflow import refresh_workflow_tools
                refresh_workflow_tools(self.tools, _ws, self)
            except Exception:
                pass  # 工作流刷新绝不影响主循环
            # 缓存本轮启用的钩子位置集合（避免每步重复扫描工作流目录）
            try:
                from real_tools import WORKSPACE as _ws
                from workflow import get_hook_workflows
                self._active_hooks = {hw["hook"] for hw in get_hook_workflows(_ws)}
            except Exception:
                self._active_hooks = set()
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

                        self._emit({"type": "step", "n": step_num, "tokens": self.cumulative_tokens,
                                    "model": self.llm.model_name})
                        _LOG.debug("step %d 累计token=%d model=%s", step_num, self.cumulative_tokens,
                                   self.llm.model_name)
                        # 本步消息基底：session 上下文 + 排空 hook 旁注 + before_answer 重做草稿
                        # （_chat_msgs 内部清空 _hook_notes，故本步工具钩子产生的新旁注留给下一步；
                        #  重试复用同一 msgs 快照，旁注不丢失也不重复注入）
                        msgs = self._chat_msgs()
                        resp = self.llm.chat(msgs, tools=tool_schemas)
                        # DSML 泄漏保险丝：llm_client 已尝试兜底解析；若 content 仍残留 DSML
                        # 工具调用标记且无 tool_calls，说明这次没解析出来 → 提示模型用标准
                        # function calling 重试一次（重试结果不再二次检查，避免无限循环）。
                        if (not resp.tool_calls and resp.content and "DSML" in resp.content
                                and "invoke" in resp.content):
                            self._emit({"type": "warn",
                                        "text": "⚠️ 工具调用格式泄漏(DSML)，已提示模型改用标准 function calling 重试"})
                            resp = self.llm.chat(msgs + [{
                                "role": "system",
                                "content": "你上一轮的工具调用以文本(DSML 标记)泄漏进了回复正文，系统没能解析执行。"
                                          "请重新发起这些工具调用，务必使用标准的 function calling（tool_calls 字段），"
                                          "不要在回复正文里输出任何 <｜｜DSML｜｜> 标记。"
                            }], tools=tool_schemas)
                        # 空回答保险丝：无工具调用且 content 为空（ModelScope 等偶发空响应）→ 提示重试一次
                        if not resp.tool_calls and not (resp.content or "").strip():
                            self._emit({"type": "warn", "text": "⚠️ 模型返回空回答，已提示重试"})
                            try:
                                r2 = self.llm.chat(
                                    msgs + [{
                                        "role": "system",
                                        "content": "你上一轮返回了空内容。请给出明确的最终回答，或调用工具继续完成任务，不要返回空内容。"
                                    }], tools=tool_schemas)
                                if r2.tool_calls or (r2.content or "").strip():
                                    resp = r2
                            except Exception:
                                pass
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
                            # —— before_answer 钩子：提交前给一次补充上下文的机会 ——
                            # 返回 inject=True 则把 result 作为 system 旁注 + 草稿续接，重跑一轮 LLM
                            # （回答可能被修正/补充）；草稿不变即收敛，另设上限 5 防死循环。
                            draft = resp.content or ""
                            cur_user_msg = self.session._current.user_message if self.session._current else ""
                            turn_context = self._turn_context_str()
                            ba_notes = self._run_hooks("before_answer",
                                                       {"user_message": cur_user_msg, "draft_answer": draft,
                                                        "turn_context": turn_context})
                            if ba_notes and draft != self._last_answer_draft \
                                    and self._answer_inject_count < 5:
                                self._last_answer_draft = draft
                                self._answer_inject_count += 1
                                self._hook_notes.extend(ba_notes)
                                self._answer_redo_draft = draft   # 下一步 _chat_msgs 带上草稿续接
                                continue   # 回 for step_num 顶部：带草稿+旁注再 chat()
                            if ba_notes and self._answer_inject_count >= 5 \
                                    and draft != self._last_answer_draft:
                                self._emit({"type": "warn",
                                            "text": "⚠️ before_answer 钩子注入达上限(5)，结束本轮"})
                            self.session.finish_turn(resp.content, resp.reasoning)
                            self._emit({"type": "answer", "text": resp.content,
                                        "tokens": self.cumulative_tokens})
                            _LOG.info("回答完成 累计token=%d %d步", self.cumulative_tokens, step_num)
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
                            # 后台推送（调度器/服务）：消费 inbox 触发下一轮
                            item = self.pop_inbox()
                            if item:
                                src, next_msg = item
                                self._emit({"type": "background_trigger", "source": src,
                                            "text": next_msg[:100]})
                                msg, auto_flag, imgs, continue_loop = next_msg, False, None, True
                                break
                            return resp.content

                        # 执行工具
                        calls = resp.tool_calls
                        step = Step(reasoning=resp.reasoning)
                        # 设置流式回调（run_python/run_shell 通过它推 tool_stream/tool_progress）
                        import real_tools as _rt
                        _rt._tool_emit = self.on_event if self.on_event else (self._print_only_emit if self.verbose else None)
                        has_tool_hooks = bool(self._active_hooks & {"before_tool", "after_tool"})
                        cur_user_msg = self.session._current.user_message if self.session._current else ""
                        if has_tool_hooks:
                            # 有 before_tool/after_tool 钩子 → 逐 call 顺序执行，前后跑钩子（保证时序）
                            if len(calls) > 1:
                                self._emit({"type": "parallel", "count": len(calls)})
                            for tc in calls:
                                self._emit({"type": "tool_call", "name": tc["name"], "arguments": tc["arguments"]})
                                tc_args_json = json.dumps(tc["arguments"], ensure_ascii=False)
                                if "before_tool" in self._active_hooks:
                                    self._hook_notes += self._run_hooks("before_tool", {
                                        "user_message": cur_user_msg, "tool_name": tc["name"], "tool_args": tc_args_json})
                                _t0 = time.time()
                                result = self.tools.call(tc["name"], tc["arguments"])
                                _LOG.info("工具 %s 耗时%.1fs 结果%d字", tc["name"],
                                          time.time() - _t0, len(result or ""))
                                if "after_tool" in self._active_hooks:
                                    self._hook_notes += self._run_hooks("after_tool", {
                                        "user_message": cur_user_msg, "tool_name": tc["name"],
                                        "tool_args": tc_args_json, "tool_result": result})
                                self._emit({"type": "tool_result", "name": tc["name"],
                                            "result": self._truncate(result), "parallel": len(calls) > 1})
                                cid = self.session.toollog.next_id()
                                self.session.toollog.record(cid, tc["name"], tc["arguments"], result)
                                step.tool_calls.append(ToolCall(call_id=cid))
                        elif len(calls) == 1:
                            tc = calls[0]
                            self._emit({"type": "tool_call", "name": tc["name"], "arguments": tc["arguments"]})
                            _t0 = time.time()
                            result = self.tools.call(tc["name"], tc["arguments"])
                            _LOG.info("工具 %s 耗时%.1fs 结果%d字", tc["name"], time.time() - _t0, len(result or ""))
                            self._emit({"type": "tool_result", "name": tc["name"],
                                        "result": self._truncate(result), "parallel": False})
                            cid = self.session.toollog.next_id()
                            self.session.toollog.record(cid, tc["name"], tc["arguments"], result)
                            step.tool_calls.append(ToolCall(call_id=cid))
                        else:
                            self._emit({"type": "parallel", "count": len(calls)})
                            for tc in calls:
                                self._emit({"type": "tool_call", "name": tc["name"], "arguments": tc["arguments"]})
                            _t0 = time.time()
                            results = self._run_tools_parallel(calls)
                            _LOG.info("并行 %d 工具 耗时%.1fs", len(calls), time.time() - _t0)
                            for tc, result in zip(calls, results):
                                _LOG.debug("  └ %s 结果%d字", tc["name"], len(result or ""))
                                self._emit({"type": "tool_result", "name": tc["name"],
                                            "result": self._truncate(result), "parallel": True})
                                cid = self.session.toollog.next_id()
                                self.session.toollog.record(cid, tc["name"], tc["arguments"], result)
                                step.tool_calls.append(ToolCall(call_id=cid))
                        _rt._tool_emit = None  # 清理
                        self.session.add_step(step)
                        # 动态注册的工具（新写的工作流、ensure_lsp 装的 LSP 等）当轮即可见：
                        # 仍扫描新写的工作流/工具脚本（注册进 toolbox）+ 每步无条件重算 schemas
                        # （schemas 无缓存，只是 dict 遍历，成本低）
                        self._refresh_tools_if_written(step)
                        tool_schemas = self.tools.schemas()
                        # 自主模式下：工具执行完后检查是否有用户插入消息，附加到结果里让 Agent 立刻看到
                        if self.autonomous_mode and self.pending_messages:
                            inject = "；".join(self.pending_messages)
                            self.pending_messages.clear()
                            self._emit({"type": "message_injected", "text": inject})
                            # 在下一步发给 LLM 的上下文里，通过 system 消息注入用户提示
                            self.session._current._user_hint = inject

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

    def _refresh_tools_if_written(self, step) -> bool:
        """若本步用 write_file/edit 写了 .agent/workflows/ 下的文件（工具脚本 *.py 或工作流 *.json），
        立即重新扫描注册，让本轮后续步骤即可调用新工具/工作流——不必等到下一轮 run() 开头。
        返回是否执行了刷新（调用方据此重算 tool_schemas）。"""
        for tc in step.tool_calls:
            name, args, _r = self.session.toollog.view(tc.call_id)
            if name in ("write_file", "edit"):
                p = str(args.get("path", "")).replace("\\", "/")
                if "/workflows/" in p or "/workflows" in p:
                    try:
                        from real_tools import WORKSPACE
                        from workflow import refresh_workflow_tools
                        refresh_workflow_tools(self.tools, WORKSPACE, self)
                        return True
                    except Exception:
                        return False
        return False

    def _diag_if_cs_written(self, step):
        """[已迁移] 写 .cs 后自动诊断改由 .agent/workflows/cs_auto_diag.xml
        (after_tool 钩子工作流) 承担。空壳保留仅为兼容旧引用/子类覆写。"""
        return

