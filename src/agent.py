"""agent.py —— 自主 Agent（事件化输出，CLI 与 Web 各取所需）。

输出抽象成结构化事件流：`_emit(event)`。若设了 `on_event`（如 Web 后端）则回调它；
同时若 `verbose=True` 则 `_print_event` 复刻原控制台格式。故 `chat.py`（不设 on_event、
verbose=True）输出与之前完全一致；`web.py` 设 on_event 把事件推给浏览器。

能力：ReAct 主循环、长程自主、单步并行工具、软 token 预算、Ctrl+C 优雅打断、
多模型热切换、多 Agent（self.sub_agents）、定时纯自主模式。
"""
from __future__ import annotations

import base64
import collections
import difflib
import json
import logging
import os
import re
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
from plan_tools import restore_active_plan, clear_active_plan, _format_plan_block
from spec_tools import (restore_active_spec, clear_active_spec, _format_spec_block,
                        _clear_active_spec, _set_active_spec, _emit_spec)
from session import Session, Step, ToolCall, repo_images_dir
from tools import Toolbox
from mdrender import render_cli   # LLM 回答的 CLI 渲染（表格→框线表，代码块→带边框）

_LOG = logging.getLogger("agt.agent")

# 以 path 为入参、对文件做 read-modify-write 的工具：同文件的多个调用必须串行，
# 否则并行线程会读同一快照→各自改写→后写覆盖先写（静默丢更新）。
_FILE_TOOLS = frozenset({"read_file", "write_file", "edit", "insert", "delete", "move",
                         "grep", "find_function"})

# Recent-file 快照跟屁虫：这些工具的参数里有明确的文件 path/file/script，step 完成后收集其全文速照
_FILE_SNAP_TOOLS = frozenset({"edit", "insert", "delete", "move", "write_file",
                              "read_file", "grep", "find_function",
                              "run_python", "run_script"})
_FILE_SNAP_MAX = 3         # 每步最多快照几个不同文件（防膨胀；同文件后面覆盖前面）
_SERVICE_EXIT_LOG_LINES = 50   # 后台服务退出时，tool 结果里附带的尾部日志行数

# 工具结果里的 data-URL 图片段（MCP ImageContent 经 mcp_client._extract_text 转来 / 普通工具返回）：
# _materialize_tool_result 把它落盘到 repo images/ 并替换成 <img>name</img> 标签（base64 不进存档）。
_DATA_URL_RE = re.compile(r"data:image/(png|jpe?g|gif|webp);base64,([A-Za-z0-9+/=]+)", re.I)

# —— 工作流生命周期钩子 —— 工具调用前后 workspace mtime 快照（真实副作用检测）——
# 排除两层：① 框架硬排除（_SNAP_EXCLUDE_DIRS：.git/.agent 等运行时目录，gitignore 没写也要排）
#          ② workspace/.gitignore 里的全部模式（目录剪枝 + 通配匹配；跳过注释/空行/! 否定模式）
_SNAP_EXCLUDE_DIRS = frozenset({
    ".git", ".agent", ".agt",
})


def _load_gitignore_patterns(workspace) -> list:
    """读 workspace/.gitignore → fnmatch 模式列表（去注释/空行；! 否定模式跳过——重新包含语义简化不支持）。
    返回 (patterns, anchored)：anchored 为根锚定（以 / 开头）的子集，单独匹配。"""
    gi = workspace / ".gitignore"
    pats, anchored = [], []
    try:
        for ln in gi.read_text(encoding="utf-8").splitlines():
            ln = ln.strip()
            if not ln or ln.startswith("#") or ln.startswith("!"):
                continue
            if ln.startswith("/"):
                anchored.append(ln.lstrip("/").rstrip("/"))
            else:
                pats.append(ln.rstrip("/"))
    except Exception:
        pass
    return pats, anchored


def _make_gitignore_filter(workspace):
    """构造 (keep_dir, keep_file) 两个谓词：gitignore 模式未命中才保留。
    - 锚定模式（/dist）：只匹配根层路径
    - 目录模式（dist/ 或与某目录名全等的模式）：路径中任一段命中即剪枝/排除
    - 通配模式（*.pyc / *.egg-info）：fnmatch 匹配任意层级 basename 或全路径"""
    import fnmatch as _fm
    pats, anchored = _load_gitignore_patterns(workspace)
    # egg-info 这类 dist 产物即使没写 gitignore 也按惯例排除
    pats = pats + ["*.egg-info"]

    def _seg_or_fnmatch(name: str, pat: str) -> bool:
        return name == pat or _fm.fnmatch(name, pat)

    def keep_dir(rel: str) -> bool:
        """rel 为目录相对路径（无尾斜杠）。目录被剪枝 → 整棵子树不进快照。"""
        name = rel.rsplit("/", 1)[-1]
        if any(_seg_or_fnmatch(name, p) for p in pats):
            return False
        if any(rel == p or _fm.fnmatch(rel, p) for p in anchored):
            return False
        return True

    def keep_file(rel: str) -> bool:
        name = rel.rsplit("/", 1)[-1]
        # 任意目录段命中（目录模式作用到文件路径）或文件名/全路径通配
        segs = rel.split("/")
        if any(_seg_or_fnmatch(seg, p) for seg in segs for p in pats):
            return False
        if any(_fm.fnmatch(rel, p) or _fm.fnmatch(name, p) for p in pats):
            return False
        if any(rel == p or rel.startswith(p + "/") or _fm.fnmatch(rel, p) for p in anchored):
            return False
        return True

    return keep_dir, keep_file


def _workspace_snapshot() -> dict:
    """WORKSPACE 下所有文件的 mtime 快照 {相对路径(posix): mtime_ns}。
    排除 = 框架硬排除目录 + 嵌套 git 仓库整棵剪枝（git 同样不追踪其内部文件）
         + .gitignore 全部模式（目录级剪枝，不深入）。
    os.walk 全量扫描（排除后几百文件的仓库毫秒级）；OSError 单文件静默跳过。"""
    import os
    from real_tools import WORKSPACE
    keep_dir, keep_file = _make_gitignore_filter(WORKSPACE)
    snap = {}
    for root, dirs, files in os.walk(WORKSPACE):
        pruned = []
        for d in dirs:
            if d in _SNAP_EXCLUDE_DIRS:
                continue
            full = os.path.join(root, d)
            if os.path.exists(os.path.join(full, ".git")):
                continue   # 嵌套 git 仓库（clone 进来的完整仓库/.git 目录或子模块 .git 文件）→ 整棵剪枝
            rel = os.path.relpath(full, WORKSPACE).replace("\\", "/")
            if keep_dir(rel):
                pruned.append(d)
        dirs[:] = pruned
        for f in files:
            if f.endswith((".pyc", ".pyo", ".log", ".tmp")):
                continue
            p = os.path.join(root, f)
            try:
                rel = os.path.relpath(p, WORKSPACE).replace("\\", "/")
                if not keep_file(rel):
                    continue
                snap[rel] = os.stat(p).st_mtime_ns
            except OSError:
                continue
    return snap


def _diff_snapshots(before: dict, after: dict) -> list:
    """两份 mtime 快照对比 → 变化文件清单 [{"file","change"}]。
    change ∈ new（after 有 before 无）/ deleted（before 有 after 无）/ modified（mtime_ns 变化）。
    按文件名排序保证稳定。注：并行子 Agent 同刻写盘会一并计入（多报不漏报，方向安全）。"""
    changed = []
    for f, m in after.items():
        if f not in before:
            changed.append({"file": f, "change": "new"})
        elif before[f] != m:
            changed.append({"file": f, "change": "modified"})
    for f in before:
        if f not in after:
            changed.append({"file": f, "change": "deleted"})
    changed.sort(key=lambda x: x["file"])
    return changed

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


def _render_write_cli(path, content, max_lines: int = 25) -> str:
    """write_file 的新建预览渲染（全绿 +，超长截断首 max_lines 行）。"""
    all_lines = (content or "").splitlines()
    show = all_lines[:max_lines]
    lines = [f"📝 write_file {path}（新建 · {len(all_lines)} 行）"]
    for l in show:
        lines.append(f"  {GREEN}+{l}{RESET}")
    if len(all_lines) > max_lines:
        lines.append(f"  {GRAY}…（省略 {len(all_lines) - max_lines} 行）{RESET}")
    return "\n".join(lines)


def _render_insert_cli(path: str, entries: list) -> str:
    """insert 的行级预览：→行号\n[文本块]，紧凑不刷屏。每块最多 20 行预览。"""
    lines = [f"✏️ insert {path}"]
    for ent in entries or []:
        ln = ent.get("line", "?")
        ct = (ent.get("content") or "").splitlines()
        preview = ct[:20]
        lines.append(f"  →{ln}")
        for c in preview:
            lines.append(f"     {c}")
        if len(ct) > 20:
            lines.append("     ...")
    return "\n".join(lines)


def _render_run_python_cli(code: str) -> str:
    """run_python 代码块预览（前 30 行），避免一行巨长。"""
    code = code or ""
    lines = code.splitlines()
    preview = lines[:30]
    more = f"\n   ... ({len(lines)} 行)" if len(lines) > 30 else ""
    return "🔧 run_python:\n   " + "\n   ".join(preview) + more


class Agent:
    def __init__(
        self,
        system: str,
        tools: Toolbox,
        *,
        enable_thinking: bool = True,
        max_steps: int = 50,
        token_budget: int = 0,
        temperature: float = 0.7,
        verbose: bool = True,
        recent_window_turns: int = 4,
        max_steps_per_turn: int = 80,
        model_name: Optional[str] = None,
        on_event: Optional[Callable[[dict], None]] = None,
        snapshot_manager=None,
        session_dir=None,
        registry=None,
    ):
        self.base_system = system
        self.tools = tools
        self.tool_groups: dict = {}   # 工具名 -> 来源模块（build_agent 注册时标注，供 /api/tools 分组）
        self.max_steps = max_steps
        self.token_budget = token_budget
        self.verbose = verbose
        self.on_event = on_event
        self.snapshot_manager = snapshot_manager
        self.registry = registry
        self.background_tasks: dict = {}   # 后台异步任务登记表 {id:{id,kind,name,task,status,session_dir,result,...}}；子 agent 异步化后供投影/wait
        self._bg_threads: dict = {}        # 异步子 agent 的后台线程 {agent_id: Thread}（仅内存，不持久化；wait_subagents 用它 join）
        self.model_name = model_name or config.DEFAULT_MODEL

        self.llm = LLMClient(model_name=self.model_name,
                             temperature=temperature, enable_thinking=enable_thinking)
        self.session = Session(system, llm=self.llm, recent_window_turns=recent_window_turns,
                               max_steps_per_turn=max_steps_per_turn, session_dir=session_dir)
        # assembly DSL v2：workflow 装配项求值用的工具引用（session._asm_evaluate 消费）
        self.session._asm_workflow_tools = self.tools
        self.session._state_provider = self.capture_runtime_state  # session 落盘时收集 plan/自主模式状态
        self.session._system_extra_provider = self._runtime_system_extra  # system prompt 实时注入后台服务状态
        self.session._time_provider = self._runtime_time_block  # tail 每步注入实时时间（感知时段）
        # 长期记忆（per-repo，跨 session）：建库 + 挂两个注入 provider 到 session
        #   - 静态层（semantic 事实 + procedural 标题）每轮始终注入
        #   - 情境层（episodic）按当前 user_message 每轮召回注入
        self.ltm = LongTermMemory(self.session.workspace)
        self.session._ltm_static_provider = self._ltm_static_block
        self.session._ltm_episodic_provider = self._ltm_episodic_block
        # 计划注入：加入计划后每轮把【当前计划】块注入 SYSTEM（id/title/design/进度），退出后为空
        self.session._plan_provider = self._plan_system_block
        # 施工方案（spec）注入：draft/committed/rejected 态 spec 每轮注入 SYSTEM（让 Agent 清楚在等批阅）；
        # approved 态由生成的 plan 接管注入（避免双重注入）。无活动 spec 返回 ''。
        self.session._spec_provider = self._spec_system_block
        self.session.utility_llm = self.utility_client()   # session 层短调用（摘要/命名）统一辅助模型
        self._task_guidance_provider_fn: Optional[Callable[[], str]] = None  # 由 chat.build_agent 注入（读 AGENTS.md/rules/skills）；set_session 转挂到新 session
        self.llm.call_recorder = self.session.llm_calls.record   # LLM 调用流水落 llm_calls.jsonl（可观测性）
        # 日志：配置根 agt logger（文件跟 session 走 + 控制台默认 WARNING+），handler 接到 session
        self._log_handler = configure_logging()
        self._log_handler.set_session(self.session.workspace, self.session.name)
        self.session._log_handler = self._log_handler
        # 后台服务 + 定时调度（producer）→ inbox → run()内循环 / chat/web 消费者 串行触发
        self.inbox: collections.deque = collections.deque()
        self._inbox_lock = threading.Lock()
        self.services = ServiceManager(on_exit=self._on_service_exit)
        self.scheduler = Scheduler(self)
        self.cumulative_tokens = 0
        self.plan: list = []        # 计划 steps 镜像（兼容旧读者；真相在 active_plan）
        self.active_plan_id: Optional[str] = None  # 当前活动计划 id（= 文件名 stem）；None=无活动计划
        self.active_plan: Optional[dict] = None     # 当前活动计划完整 dict（单一事实源：id/title/design/steps/...）
        # —— 施工方案（spec）状态 ——
        self.active_spec_id: Optional[str] = None    # 当前活动 spec id（= 文件名 stem）；None=无活动 spec
        self.active_spec: Optional[dict] = None      # 当前活动 spec 完整 dict（单一事实源）
        # 纯自主模式状态
        self.autonomous_mode: bool = False
        self.autonomous_end_time: Optional[datetime] = None
        self.autonomous_prompt: str = "当前为纯自主模式，请继续按照要求完成更多工作"
        self.pending_messages: List[str] = []  # 用户插入的消息队列
        self.goal_check_script: str = ""       # 目标达成验证脚本(Python，输出 PASS=达成)
        # —— 工作流生命周期钩子（每轮 run 开头重置）——
        self._hook_notes: list[dict] = []       # 待注入的 system 旁注（before_tool/after_tool/before_answer），每项 {hook, name, result}
        self._answer_redo_draft: Optional[str] = None   # before_answer 重跑时上一次草稿（临时 assistant 续接）
        self._last_answer_draft: Optional[str] = None   # 收敛判据：上次注入所针对的草稿
        self._answer_inject_count: int = 0      # 本轮 before_answer 注入次数（封顶 5 防死循环）
        self._turn_end_inject_count: int = 0    # 本轮 turn_end 注入次数（封顶 3 防死循环）
        # —— Agent 注册表（多 Agent 协作通信）——
        self.agent_id: str = "_main_"   # 本 Agent 在 registry 中的 id（子 Agent 创建时覆盖）
        self._active_target: str = "_main_"  # 当前用户直接交互的目标 agent_id（/agent 切换）
        self._recap: str = ""           # 最近一轮的 recap（队友可见，不进入自己的上下文）
        self.dump_projections: bool = False  # 投影转储开关（运行时设置）
        self._fs_snap: Optional[dict] = None  # 最近一次 workspace mtime 快照（after_tool 钩子的 changed_files 基准；链式复用省一半扫描）
        self._turn_changed_calls: list = []   # 本轮产生文件变更的工具调用原文 [{call_id,name,arguments,result_preview,changed_files}]——before_answer 钩子（wiki 维护等）直接消费，免子 Agent read_file
        # 统一辅助模型（settings.json utility_model；空=跟随主模型）：所有 LLM 短调用共用——
        # recap 总结 / RAG 检索 / reasoning 补全默认 / 工作流 LLM/意图节点默认。
        # 兼容旧字段 retrieval_model / recap_model（config.get_utility_model 内部处理）。
        try:
            import config as _cfg
            self.utility_model: str = _cfg.get_utility_model()
        except Exception:
            self.utility_model = ""
        self._utility_llm = None   # 惰性创建的辅助 client（None=未建；=self.llm 表示回退主模型）
        if self.registry is not None:
            self.registry.register(self.agent_id, "main", "main", self.model_name,
                                   agent=self, task="", status="running")
        self.session._teammates_provider = self._teammates_block

    # ========== 事件输出 ==========
    def _print_only_emit(self, event: dict):
        """CLI 模式的流式回调：tool_stream/tool_progress 直接打印。"""
        t = event.get("type")
        if t == "tool_stream":
            print(f"{GRAY}{event.get('text', '')}{RESET}", end="", flush=True)
        elif t == "tool_progress":
            print(f"{GRAY}⏳ {event['name']} 已运行 {event['elapsed']}s，{event.get('lines', 0)} 行输出{RESET}")

    def _emit(self, event: dict):
        """发一个事件：回调 on_event（Web）；verbose 时打印（CLI）。
        事件统一打 agent_id 标（主=_main_，子 Agent=各自 id）——前端据此分流渲染
        （子 Agent 的 answer 分页显示，thinking/step 加 [id] 前缀），不再与主输出串台。"""
        event.setdefault("agent_id", self.agent_id)
        if self.on_event:
            try:
                self.on_event(event)
            except Exception:
                pass
        if self.verbose:
            self._print_event(event)

    def _materialize_tool_result(self, result, tool_name, args, cid) -> str:
        """工具结果里的 data-URL 图片段落盘到 repo images/，用 <img>name</img> 占位替换。
        base64 不进 toollog/事件流/存档。线程安全：文件名含 cid（并行调用唯一）+ 序号。"""
        result = result or ""
        matches = list(_DATA_URL_RE.finditer(result))
        if not matches:
            return result
        out_dir = repo_images_dir(self.session.workspace)
        out, last = [], 0
        for idx, m in enumerate(matches):
            out.append(result[last:m.start()])
            try:
                ext = m.group(1).replace("jpg", "jpeg")
                fn = f"{cid}_{idx}.{ext}"
                (out_dir / fn).write_bytes(base64.b64decode(m.group(2)))
                out.append(f"<img>{fn}</img>")
            except Exception:
                out.append(m.group(0))   # 落盘失败保留原 data URL（不丢信息）
            last = m.end()
        out.append(result[last:])
        return "".join(out)

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
            print(f"\n⚠️ token 预算 ({self.token_budget}) 已用尽，强制收尾。" if self.token_budget else "")
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
            elif _n == "insert":
                print(_render_insert_cli(_a.get("path", ""), _a.get("entries", [])))
            elif _n == "run_python" and _a.get("code"):
                print(_render_run_python_cli(_a.get("code")))
            else:
                print(f"🔧 调用 {_n}({_a})")
        elif t == "tool_result":
            prefix = f"   → [{e['name']}]" if e.get("parallel") else "   →"
            print(f"{prefix} {e['result']}")
        elif t == "answer":
            print("\n🤖 最终回答：")
            print(render_cli(e['text'].strip()))
            print(f"{GRAY}[本次累计 token: {e.get('tokens', self.cumulative_tokens)}]{RESET}")
        elif t == "wrap_up":
            print(f"\n⚠️ 达到最大步数 {self.max_steps}，强制收尾。")
        elif t == "wrap_answer":
            print("\n🤖 收尾回答：")
            print(render_cli(e['text'].strip()))
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

    def _file_key(self, tc):
        """返回该调用锁定的文件 key：同 key 的调用必须串行（防 read-modify-write 竞态丢更新）。
        非文件工具 / 无 path → None（可任意并行）。"""
        if tc.get("name") not in _FILE_TOOLS:
            return None
        p = (tc.get("arguments") or {}).get("path")
        if not p:
            return None
        try:
            from real_tools import WORKSPACE
            return str((WORKSPACE / p).resolve())   # 归一化绝对路径作 key
        except Exception:
            return None

    def _run_tools_parallel(self, calls: list) -> list:
        """并行执行一组工具调用，按原顺序返回结果。
        以【目标文件】为锁：同文件的多个调用按原顺序【串行】（避免 read-modify-write 竞态丢更新），
        不同文件 / 无文件工具【并行】。等价于"跨文件并行、文件内串行"。"""
        results = [None] * len(calls)
        groups: dict = {}   # file_key -> [orig_idx,...]（保持原顺序）
        free: list = []     # 不锁定文件的调用（各自独立并行）
        for i, tc in enumerate(calls):
            k = self._file_key(tc)
            (free.append(i) if k is None else groups.setdefault(k, []).append(i))
        # 每个 task = 一组同文件下标(组内串行) 或 一个 free 下标；tasks 之间并行
        tasks = [idxs for idxs in groups.values()] + [[i] for i in free]

        def _run_seq(idxs):
            out = []
            for i in idxs:
                tc = calls[i]
                try:
                    out.append((i, self.tools.call(tc["name"], tc["arguments"])))
                except Exception as e:
                    out.append((i, f"[执行出错] {type(e).__name__}: {e}"))
            return out

        with ThreadPoolExecutor(max_workers=max(1, len(tasks))) as ex:
            futs = {ex.submit(_run_seq, idxs): idxs for idxs in tasks}
            for fut in as_completed(futs):
                for i, r in fut.result():
                    results[i] = r
        return results

    def switch_model(self, name: str, _user_initiated: bool = False):
        """热切换模型。Session 共用 self.llm，故摘要调用也跟着切。
        _user_initiated=True 时（用户 /model 或 WebUI 下拉框），llm 会重建有效回退链
        （把新模型提前到链首）；回退路径切换不传此参数，不动链。"""
        self.llm.switch_model(name, _user_initiated=_user_initiated)
        self.model_name = name

    # ========== 运行时状态的存取（随 session 落盘/恢复）==========
    def capture_runtime_state(self) -> dict:
        """收集要随 session 存档保留的运行时状态（resume 时恢复）。"""
        return {
            "plan_id": self.active_plan_id,   # 只存活动计划文件名；计划本体在 plans/<plan_id>.json
            "spec_id": self.active_spec_id,   # 只存活动 spec 文件名；spec 本体在 specs/<spec_id>.json
            "autonomous_mode": self.autonomous_mode,
            "autonomous_end_time": self.autonomous_end_time.isoformat() if self.autonomous_end_time else None,
            "autonomous_prompt": self.autonomous_prompt,
            "goal_check_script": self.goal_check_script,
            "background_tasks": self.background_tasks,
        }

    def restore_runtime_state(self, state: dict):
        """从存档恢复运行时状态（resume / 切换 session 后调用）。"""
        restore_active_plan(self, state or {})   # 活动计划：按 plan_id 从文件读回；空存档→清空；旧格式自动迁移
        restore_active_spec(self, state or {})   # 活动 spec：按 spec_id 从文件读回；空存档→清空
        self.background_tasks = (state or {}).get("background_tasks") or {}  # 后台任务登记表
        if state:
            self.autonomous_mode = bool(state.get("autonomous_mode", False))
            end = state.get("autonomous_end_time")
            self.autonomous_end_time = datetime.fromisoformat(end) if end else None
            if "autonomous_prompt" in state:
                self.autonomous_prompt = state["autonomous_prompt"]
            if "goal_check_script" in state:
                self.goal_check_script = state["goal_check_script"]
        self._emit_plan_if_any()
        self._emit_spec_if_any()

    def set_session(self, session):
        """切换到指定 session：换引用 + 重新挂状态收集回调 + 恢复附加状态 + 同步 UI。
        所有 resume / reset / new_session 都应走这里，保证 provider 与附加状态一致。"""
        _prev = self.session
        self.session = session
        # assembly DSL v2 转挂：清单/钩子声明/hooks 默认开关跟到新 session
        # （persona 在 assembly text: 项的场景，不转挂则切档后 persona 丢失）
        if getattr(_prev, "assembly_plan", None) is not None:
            session.set_assembly_plan(_prev.assembly_plan)
        session.hook_specs = getattr(_prev, "hook_specs", None)
        session.hooks_default_on = getattr(_prev, "hooks_default_on", True)
        session._asm_workflow_tools = self.tools
        session._asm_agent_id = self.agent_id
        session._state_provider = self.capture_runtime_state
        session._system_extra_provider = self._runtime_system_extra
        session._time_provider = self._runtime_time_block
        session._ltm_static_provider = self._ltm_static_block      # 长期记忆·静态层
        session._ltm_episodic_provider = self._ltm_episodic_block  # 长期记忆·情境层
        session._plan_provider = self._plan_system_block            # 当前活动计划·每轮注入
        session._spec_provider = self._spec_system_block            # 当前活动 spec·每轮注入
        session.utility_llm = self.utility_client()                  # session 层短调用（摘要/命名）统一辅助模型
        session._teammates_provider = self._teammates_block         # 团队感知·每轮注入
        session._task_guidance_provider = getattr(self, "_task_guidance_provider_fn", None)  # 任务指引·每轮重读
        session.system = self.base_system   # 读档用当前框架 system，丢弃存档里烤死的旧 task-guidance（防与新 provider 双重注入）
        self.llm.call_recorder = session.llm_calls.record           # LLM 调用流水跟到新 session
        # 辅助 client 的流水记录也跟到新 session（若有独立实例）
        if getattr(self, "_utility_llm", None) is not None and self._utility_llm is not self.llm:
            self._utility_llm.call_recorder = session.llm_calls.record
        session._log_handler = self._log_handler                   # 日志 handler 跟到新 session
        self._log_handler.set_session(session.workspace, session.name)
        self.restore_runtime_state(session.extra_state)
        self._restore_subagents()   # 扫描 session_dir/agents/ 恢复子 Agent 列表到 registry
        self._inbox_restore()       # 从 inbox.jsonl 恢复未消费的后台消息（/restart 或崩溃后不丢）

    def _emit_plan_if_any(self):
        """把当前 plan 推给 UI（resume 后让前端 plan 面板同步）。"""
        if getattr(self, "on_event", None):
            try:
                self.on_event({"type": "plan", "plan": [dict(s) for s in self.plan],
                               "plan_id": self.active_plan_id,
                               "plan_title": (self.active_plan or {}).get("title", "")})
            except Exception:
                pass

    def _plan_system_block(self) -> str:
        """当前活动计划的 SYSTEM 注入块（加入计划后每轮注入；退出/无计划返回 ''，session 不注入）。
        格式化逻辑在 plan_tools._format_plan_block，这里只做转发 + 异常兜底。"""
        try:
            return _format_plan_block(self)
        except Exception:
            return ""

    def _spec_system_block(self) -> str:
        """当前活动 spec 的 SYSTEM 注入块（draft/committed/rejected 态每轮注入；approved/无 spec 返回 ''）。
        格式化逻辑在 spec_tools._format_spec_block，这里只做转发 + 异常兜底。"""
        try:
            return _format_spec_block(self)
        except Exception:
            return ""

    def _emit_spec_if_any(self):
        """把当前 spec 推给 UI（resume 后让前端 spec 面板同步）。"""
        _emit_spec(self)

    # ========== 后台消息 inbox（producer → inbox → 串行消费者 → run） ==========
    # inbox 持久化：session_dir/inbox.jsonl（append-only + 消费时重写剩余）
    # push_message 时 append 一行 JSON {ts, source, msg, seed}；pop_inbox 时 popleft + 重写文件头。
    # 启动恢复（set_session 后）：若 inbox.jsonl 存在 → 逐行 load 回 deque。
    # 这样 /restart 或崩溃后 inbox 里的消息不丢——新进程的 inbox_thread 会捡到并触发 run。
    def _inbox_path(self):
        """inbox 持久化文件路径（跟 session_dir 走）。"""
        sdir = getattr(self.session, "session_dir", None)
        if not sdir:
            return None
        from pathlib import Path
        return Path(sdir) / "inbox.jsonl"

    def _inbox_persist_append(self, source, msg, seed):
        """append 一条到 inbox.jsonl（失败静默——内存 deque 已有，持久化是 best-effort）。"""
        try:
            p = self._inbox_path()
            if not p:
                return
            p.parent.mkdir(parents=True, exist_ok=True)
            import json as _j
            rec = {"ts": time.time(), "source": source, "msg": msg, "seed": seed}
            with open(p, "a", encoding="utf-8") as f:
                f.write(_j.dumps(rec, ensure_ascii=False) + "\n")
        except Exception:
            pass   # 持久化失败不影响内存操作

    def _inbox_persist_rewrite(self):
        """消费后重写 inbox.jsonl（剩余条目）。失败静默。
        inbox 通常很短（几条），直接全量重写即可——不用 append+truncate 的复杂方案。"""
        try:
            p = self._inbox_path()
            if not p:
                return
            import json as _j
            with self._inbox_lock:
                items = list(self.inbox)
            if not items:
                # 空了：删文件（干净）
                if p.exists():
                    p.unlink()
                return
            tmp = p.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                for (src, msg, seed) in items:
                    f.write(_j.dumps({"ts": time.time(), "source": src, "msg": msg, "seed": seed},
                                     ensure_ascii=False) + "\n")
            os.replace(tmp, p)   # 原子替换
        except Exception:
            pass

    def _inbox_restore(self):
        """启动/set_session 时从 inbox.jsonl 恢复未消费的消息到 deque。"""
        try:
            p = self._inbox_path()
            if not p or not p.exists():
                return
            import json as _j
            count = 0
            with open(p, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    rec = _j.loads(line)
                    with self._inbox_lock:
                        self.inbox.append((rec.get("source", ""), rec.get("msg", ""), rec.get("seed")))
                    count += 1
            if count:
                _LOG.info("inbox 恢复了 %d 条未消费消息（从 %s）", count, p)
        except Exception as e:
            _LOG.warning("inbox 恢复失败: %s", e)

    def push_message(self, msg: str, source: str = "background", seed: Optional[dict] = None):
        """后台/调度器推一条消息进 inbox，等 Agent 空闲时触发一轮 run。线程安全。
        被 background.Scheduler / ServiceManager / _bg 路由等后台线程调用。
        持久化：append 到 inbox.jsonl——/restart 或崩溃后新进程可恢复。

        seed（可选）= 一条合成工具记录 {tool, args, result, reasoning}，消费侧开新 turn 时
        会预置成一个 Step（渲染为 assistant(tool_use)→tool(结果)）。用于后台服务退出等异步事件：
        把退出结果+启动参数以 stop_service 工具结果的形式回传，Agent 醒来即在上下文里看到。"""
        with self._inbox_lock:
            self.inbox.append((source, msg, seed))
        self._inbox_persist_append(source, msg, seed)
        self._emit({"type": "background_trigger", "source": source,
                    "text": (msg or "")[:80], "queue_size": len(self.inbox),
                    "seed": bool(seed)})

    def pop_inbox(self):
        """取一条 inbox 消息 (source, msg, seed)，空则 None。线程安全。
        两处消费点（run() 内循环 / chat/web 主循环 drain）都调它，锁保证不重复消费。
        消费后重写 inbox.jsonl（移除已消费的条目）。"""
        with self._inbox_lock:
            if not self.inbox:
                return None
            item = self.inbox.popleft()
        self._inbox_persist_rewrite()
        return item

    def _seed_steps(self, seeds: list):
        """把一批合成工具记录预置成本轮 Step：每条 {tool, args, result, reasoning} 落 toollog +
        建一个 Step（单 ToolCall）add_step。使本轮首轮 _chat_msgs 就把它们渲染成
        assistant(tool_use)→tool(结果)——用于后台服务退出等异步事件，让模型在上下文里直接看到
        事件结果（含启动参数）而非一段纯文本通知。"""
        for sd in seeds:
            cid = self.session.toollog.next_id()
            self.session.toollog.record(cid, sd.get("tool", ""), sd.get("args", {}),
                                        sd.get("result", ""))
            step = Step(reasoning=sd.get("reasoning", ""))
            step.tool_calls.append(ToolCall(call_id=cid))
            self.session.add_step(step)

    def _on_service_exit(self, name: str, entry: dict, rc: int):
        """ServiceManager 进程自行退出回调（读线程在 stdout 关闭后调）：
        把退出事件包成一条 stop_service 的合成工具记录推 inbox——args 带启动参数，
        result 带退出码+尾部日志+复盘。Agent 醒来时上下文里即是一条完整的工具调用记录，
        模型可直接据其决定是否重启/善后。手动 stop_service 不走这里（stop 时已置 manual_stop）。"""
        try:
            logs = list(entry.get("logs", []))[-_SERVICE_EXIT_LOG_LINES:]
        except Exception:
            logs = []
        startup = {
            "name": name,
            "command": entry.get("command", ""),
            "cwd": entry.get("cwd", ""),
            "started_at": (datetime.fromtimestamp(entry["started_at"]).strftime("%Y-%m-%d %H:%M:%S")
                           if entry.get("started_at") else ""),
            "pid": entry.get("pid"),
        }
        brief = f"rc={rc}" + ("（正常退出）" if rc == 0 else "（异常退出/崩溃）")
        seed = {
            "tool": "stop_service",
            "args": startup,
            "result": self._format_service_exit(name, startup, rc, logs),
            "reasoning": (f"(后台服务「{name}」已自行退出（{brief}），系统将其退出结果作为 "
                          "stop_service 工具结果注入供你查阅；注意这是服务自行退出，并非你主动 stop)"),
        }
        header = f"📨〔后台服务退出〕「{name}」已自行退出（{brief}）"
        self.push_message(header, source=f"service_exit:{name}", seed=seed)

    @staticmethod
    def _format_service_exit(name: str, startup: dict, rc: int, logs: list) -> str:
        """组装后台服务退出时塞进 tool 结果 content 的文本：退出判读 + 启动参数复盘 + 尾部日志。"""
        ok = "✅ 正常退出" if rc == 0 else f"⚠️ 异常退出/崩溃（rc={rc}）"
        lines = [
            f"【后台服务「{name}」自行退出】{ok}",
            "⚠️ 本服务系【自行退出】，并非你主动 stop_service。如需继续可重新 start_service。",
            "—— 启动参数（复盘）——",
            f"  command: {startup.get('command', '')}",
            f"  cwd: {startup.get('cwd', '') or '(默认)'}",
            f"  pid: {startup.get('pid')}  启动于: {startup.get('started_at', '')}",
        ]
        if logs:
            lines.append(f"—— 退出前最近 {len(logs)} 行输出 ——")
            lines.extend(logs)
        else:
            lines.append("—— 退出前无输出 ——")
        return "\n".join(lines)

    def _runtime_system_extra(self) -> str:
        """动态注入 system prompt 的运行时段：后台服务清单 + 子 Agent 任务看板。
        两者皆空返回空串（不注入）；有则 Agent 每步都能看到哪些在跑/已完成，不必自己查。"""
        parts = []
        svc = self.services.status_lines()
        if svc:
            parts.append("【后台服务状态】当前服务：\n" + "\n".join(svc))
        board = self._format_subagent_board()
        if board:
            parts.append(board)
        return "\n\n".join(parts)

    def _format_subagent_board(self) -> str:
        """后台子 Agent / 异步任务看板：每项「名称 [agent_id] 任务 — 状态」。
        以 registry 为准（子 Agent 完成/失败时 registry 与 _agent_meta 已同步），
        不再用 background_tasks（主 Agent 不维护它）。"""
        if not self.registry:
            return ""
        try:
            team = self.registry.format_team(exclude_id=self.agent_id)
            if not team:
                return ""
            return team
        except Exception:
            return ""

    def _runtime_time_block(self) -> str:
        """tail 每步注入实时时间（秒级）+ 当前会话名，让 Agent 感知真实时段（深夜/工作日）
        并知道自己所在的会话（从而判断自动命名是否合适、是否该 rename_session）。
        persona 不再含日期（保前缀缓存稳定），现实时间与会话名统一由这里每步刷新进 tail。"""
        try:
            from datetime import datetime
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S %A")
            name = getattr(self.session, "name", "") or "(未命名)"
            return f"当前时间：{now}\n当前会话：{name}"
        except Exception:
            return ""

    def _ltm_static_block(self) -> str:
        """长期记忆·静态层注入：semantic 事实 + procedural 标题（每轮始终注入）。失败静默不炸主循环。"""
        try:
            return self.ltm.static_block()
        except Exception:
            return ""

    def _ltm_episodic_block(self, query: str) -> str:
        """长期记忆·情境层注入：按当前 user_message 召回 episodic（每轮按需）。失败静默。
        检索工作流接管：当 before_turn_retrieval 工作流（hook=before_turn）存在时，
        episodic 已由工作流统一召回（search_memory → collect → LLM 精排，kind=epi 候选），
        provider 不再注入，避免双重。工作流被删/重命名则自动回退到本 provider。"""
        try:
            from real_tools import WORKSPACE as _ws
            from workflow import get_hook_workflows
            if any(hw.get("name") == "before_turn_retrieval"
                   for hw in get_hook_workflows(_ws, "before_turn")):
                return ""
        except Exception:
            pass
        try:
            return self.ltm.episodic_block(query)
        except Exception:
            return ""

    def _teammates_block(self) -> str:
        """团队感知注入：列出 registry 中其他活跃 Agent，让本 Agent 知道队友的存在。
        无 registry 或无其他 Agent 时返回 ''（不注入）。"""
        if not self.registry:
            return ""
        try:
            return self.registry.format_team(exclude_id=self.agent_id)
        except Exception:
            return ""

    def _restore_subagents(self):
        """读档后恢复子 Agent 列表：扫描 session_dir/agents/，注册到 registry（status=done）。
        子 Agent 的 meta.json 里存了 _agent_meta（agent_id/name/model/task/caller_id/recap）；
        老数据没有则用目录名兜底。agent=None（历史子 Agent，不创建实例——/agent 切换时按需 lazy load）。"""
        if not self.registry:
            return
        # 先清除 registry 中非 _main_ 的旧条目（上个 session 的子 Agent）
        with self.registry._lock:
            old_ids = [k for k in self.registry._agents if k != self.agent_id]
            for k in old_ids:
                del self.registry._agents[k]
        sdir = getattr(self.session, "session_dir", None)
        if not sdir:
            _LOG.warning("_restore_subagents: session_dir 为空，跳过")
            return
        from pathlib import Path
        import json
        agents_dir = Path(sdir) / "agents"
        if not agents_dir.exists():
            _LOG.debug("_restore_subagents: agents/ 目录不存在: %s", agents_dir)
            return
        count = 0
        for ad in sorted(agents_dir.iterdir()):
            if not ad.is_dir():
                continue
            aid = ad.name
            if self.registry.lookup(aid):
                continue  # 已注册（当前运行中的）
            # 读 meta.json
            meta_path = ad / "meta.json"
            am = {}
            if meta_path.exists():
                try:
                    meta = json.loads(meta_path.read_text(encoding="utf-8"))
                    am = (meta.get("extra_state") or {}).get("_agent_meta") or {}
                except Exception:
                    pass
            self.registry.register(
                am.get("agent_id", aid),
                am.get("name", aid),
                "subagent",
                am.get("model", "?"),
                agent=None,
                task=am.get("task", "(历史任务)"),
                status=am.get("status", "done"),
                caller_id=am.get("caller_id", ""),
                recap=am.get("recap", ""),
            )
            count += 1
        if count:
            _LOG.info("_restore_subagents: 从 %s 恢复了 %d 个子 Agent", agents_dir, count)

    def utility_client(self):
        """统一辅助模型 client：所有 LLM 短调用共用（recap / RAG 检索 / 工作流 LLM/意图节点默认）。
        utility_model 配置了且≠主模型 → 独立实例（enable_thinking=False + 挂 llm_calls 流水）；
        空/无效/等于主模型 → 主 llm（跟随主模型）。惰性创建、进程内复用。"""
        um = getattr(self, "utility_model", "")
        if not um or um == self.llm.model_name:
            return self.llm
        if getattr(self, "_utility_llm", None) is None:
            try:
                cli = LLMClient(model_name=um, enable_thinking=False, max_retries=2)
                cli.call_recorder = self.session.llm_calls.record
                self._utility_llm = cli
            except Exception:
                self._utility_llm = self.llm   # 配置无效退回主模型
        return self._utility_llm

    def _generate_recap(self, user_msg: str, answer: str, tool_names: list = None):
        """异步用 LLM 生成一句话 recap（最近在干嘛），不阻塞主循环。
        recap 不进入自己的上下文，但队友在 teammates_block 中能看到。
        用统一辅助模型（utility_model 未配=主模型）。失败静默（recap 保持上一轮的值或空）。"""
        def _bg():
            try:
                prompt = (
                    f"用一句话（不超过30字）总结你刚才这轮做了什么。\n"
                    f"用户请求：{(user_msg or '')[:100]}\n"
                    f"你的回答：{(answer or '')[:200]}\n"
                    f"工具调用：{', '.join(tool_names) if tool_names else '无'}\n"
                    f"只输出这一句话，不要任何额外内容。"
                )
                resp = self.utility_client().chat([{"role": "user", "content": prompt}], scene="recap")
                recap = (resp.content or "").strip().split("\n")[0].strip()[:60]
                if recap:
                    self._recap = recap
                    if self.registry:
                        self.registry.update_recap(self.agent_id, recap)
            except Exception:
                pass   # 静默失败，recap 保持上一轮的值
        threading.Thread(target=_bg, daemon=True).start()

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
        """将用户消息加入队列（等下一步边界注入当前任务上下文；任何模式均可用）。
        供 web/CLI 忙时插话：消息会在当前轮的下一步作为 user_hint 注入，模型当步可见、可改向。"""
        self.pending_messages.append(text)
        self._emit({"type": "message_queued", "text": text, "queue_size": len(self.pending_messages)})
        return True

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
                                  text=True, timeout=30, cwd=os.getcwd(),
                                  creationflags=(subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0))
            return (proc.stdout or "").strip()
        except subprocess.TimeoutExpired:
            return "[目标检查超时]"
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass

    # ========== 工作流生命周期钩子 ==========
    def _hook_tasks(self, hook: str) -> list[dict]:
        """解析本 agent 某 hook 位置的任务清单：yml hook_specs 优先（v2），
        未声明时回退旧 workflow meta.hook 扫描（兼容期）。返回 [{kind, name, canvas?, async, recap, meta}]。
        kind: workflow / cmd / emit。workflow 项经 _wf_canvas_index（每轮 run 开始时刷新）取画布。"""
        from real_tools import WORKSPACE as _ws
        from workflow import get_hook_workflows
        specs = getattr(self.session, "hook_specs", None)
        if specs is None:
            hws = get_hook_workflows(_ws, hook)
            return [{"kind": "workflow", "name": hw["name"], "canvas": hw["canvas"],
                     "async": bool((hw.get("meta") or {}).get("async")),
                     "recap": bool((hw.get("meta") or {}).get("recap")),
                     "meta": hw.get("meta") or {}} for hw in hws]
        tasks = []
        for item in specs.get(hook, []):
            kind = item.get("kind")
            name = item.get("value") or ""
            if not name:
                continue
            entry = {"kind": kind, "name": name, "value": name, "async": bool(item.get("async")),
                     "recap": bool(item.get("recap")), "meta": {}}
            if kind == "workflow":
                entry["canvas"] = (self._wf_canvas_index() or {}).get(name)
                if entry["canvas"] is None:
                    _LOG.warning("hooks 声明的工作流 '%s' 未找到（.agent/workflows/），跳过", name)
                    continue
            tasks.append(entry)
        return tasks

    def _wf_canvas_index(self) -> dict:
        """工作流名 → canvas 索引（钩子声明解析用）。带 mtime 缓存：目录没变不重扫。"""
        import os
        from real_tools import WORKSPACE as _ws
        d = _ws / ".agent" / "workflows"
        try:
            stamp = max((p.stat().st_mtime_ns for p in d.iterdir()
                         if not p.name.endswith(".meta")), default=0)
        except OSError:
            stamp = 0
        cache = getattr(self, "_wf_idx_cache", None)
        if cache is not None and cache[0] == stamp:
            return cache[1]
        from workflow import scan_workflows
        idx = {}
        for it in scan_workflows(_ws):
            if it.get("canvas") is not None and not it.get("error"):
                idx[it["name"]] = it["canvas"]
        self._wf_idx_cache = (stamp, idx)
        return idx

    def _run_hooks(self, hook: str, context: dict) -> list[dict]:
        """运行所有声明在 hook 位置触发的任务（工作流 / 命令 / 事件），返回需注入的旁注列表。
        context: 该钩子位置的上下文（key 对应工作流开始节点 <out> 声明）。
        工作流约定返回 {inject, result, message}：
          - inject=True 且 result 非空 → 加入返回列表 {hook, name, result}（作 system 旁注喂主 LLM）；
          - message 非空 → 发 workflow_message 事件到 UI（不进主 LLM，用于静默通知类钩子）。
        cmd 项：执行命令，stdout 非空则注入。emit 项：发 emit 事件（不进主 LLM）。
        失败仅发 auto_wf_error 事件，绝不炸主循环。
        assembly DSL：hooks=off（子 Agent 声明/agent_prompt 参数）时本 Agent 不跑任何钩子工作流。
        子 Agent 未显式声明装配时 hooks_default_on=False（Session 构造代码置位）——默认不跑钩子。"""
        if not getattr(self.session, "assembly", {}).get("hooks",
                                                         getattr(self.session, "hooks_default_on", True)):
            return []
        from real_tools import WORKSPACE as _ws
        from workflow import run_hook
        tasks = self._hook_tasks(hook)
        # 拆分：emit 即时执行（同步发事件）；cmd 走同步执行器；workflow 走原 sync/async 并发
        emit_tasks = [t for t in tasks if t["kind"] == "emit"]
        cmd_tasks = [t for t in tasks if t["kind"] == "cmd"]
        wf_tasks = [t for t in tasks if t["kind"] == "workflow"]
        notes = []
        # —— emit 项：同步发事件（如 confirm_tool_use，UI 消费）——
        for t in emit_tasks:
            try:
                self._emit({"type": t["name"], "hook": hook, "context": dict(context)})
            except Exception as e:
                _LOG.warning("钩子 emit %s 失败：%s", t["name"], e)
        # —— cmd 项：同步执行命令，stdout 非空注入（超时 10s，失败跳过不炸主循环）——
        for t in cmd_tasks:
            try:
                import subprocess as _sp
                r = _sp.run(t["value"], shell=True, capture_output=True, timeout=10,
                            cwd=str(self.session.workspace))
                out = (r.stdout or b"").decode("utf-8", errors="replace").strip()[:4000]
                self._emit({"type": "auto_wf", "name": f"cmd:{t['name'][:60]}", "hook": hook,
                            "run_id": "", "text": out[:300]})
                if out:
                    notes.append({"hook": hook, "name": f"cmd:{t['name']}", "result": out})
            except Exception as e:
                self._emit({"type": "auto_wf_error", "name": f"cmd:{t['name'][:60]}", "hook": hook,
                            "text": str(e)[:200]})
        try:
            # 同步/异步工作流划分
            sync_hws = [t for t in wf_tasks if not t.get("async")]
            async_hws = [t for t in wf_tasks if t.get("async")]
            # —— async 钩子：后台线程执行（wiki_auto_maintenance 等推理长但无需等）——
            for hw in async_hws:
                from workflow import new_wf_run
                _rid = new_wf_run(hw["name"], hook)   # 观测注册（run registry）
                self._emit({"type": "auto_wf_start", "name": hw["name"], "hook": hook,
                            "run_id": _rid, "text": str(context)[:80]})
                _agent_ref = self
                _hw = hw
                _hook = hook
                _rid_c = _rid
                _ctx = dict(context)
                def _async_hook():
                    try:
                        _llm = _agent_ref.utility_client()
                        _ov = getattr(_llm, "_scene_override", None)
                        _llm._scene_override = f"hook:{_hook}"
                        try:
                            inject, result, message = run_hook(_hw["canvas"], _ctx,
                                                      tools=_agent_ref.tools, llm=_llm, workspace=_ws,
                                                      run_id=_rid_c)
                        finally:
                            _llm._scene_override = _ov
                        _agent_ref._emit({"type": "auto_wf", "name": _hw["name"], "hook": _hook,
                                    "run_id": _rid_c, "text": result[:300] or message[:300]})
                        # recap 工作流回写：meta.recap=true 的异步钩子，结果写 agent._recap（队友可见，
                        # 不进自己上下文）——recap_gen 等本地小模型总结工作流的引擎侧落点。
                        # 错误过滤：LLM 端点挂掉/回退链耗尽时 run_hook 会把错误文本当 result 返回
                        # （如 "APIStatusError: Error code: 402..."）——特征识别，不污染 recap（保持旧值）
                        _RECAP_ERR_MARKS = ("APIStatusError", "APIConnectionError", "APITimeoutError",
                                            "RateLimitError", "Error code:", "执行失败", "Traceback",
                                            "[工作流", "出错]", "BadRequestError")
                        if (_hw.get("recap") or (_hw.get("meta") or {}).get("recap")) and (result or "").strip():
                            _rc = (result or "").strip().split("\n")[0].strip()[:60]
                            if _rc and not any(mk in _rc for mk in _RECAP_ERR_MARKS):
                                _agent_ref._recap = _rc
                                if _agent_ref.registry:
                                    _agent_ref.registry.update_recap(_agent_ref.agent_id, _rc)
                        if message.strip():
                            _agent_ref._emit({"type": "workflow_message", "name": _hw["name"], "hook": _hook,
                                        "text": message, "auto": True})
                    except Exception as e2:
                        _agent_ref._emit({"type": "auto_wf_error", "name": _hw["name"], "hook": _hook,
                                    "run_id": _rid_c, "text": str(e2)[:200]})
                threading.Thread(target=_async_hook, daemon=True).start()
            # —— 同步钩子：并发执行 + 按声明序收集注入 ——
            if sync_hws:
                from concurrent.futures import ThreadPoolExecutor, as_completed
                from workflow import new_wf_run
                def _run_one(hw):
                    rid = new_wf_run(hw["name"], hook)   # 观测注册
                    self._emit({"type": "auto_wf_start", "name": hw["name"], "hook": hook,
                                "run_id": rid, "text": str(context)[:80]})
                    hook_llm = self.utility_client()
                    _ov = getattr(hook_llm, "_scene_override", None)
                    hook_llm._scene_override = f"hook:{hook}"   # 钩子内 LLM 调用标注 scene（chat 自动读取）
                    try:
                        try:
                            inject, result, message = run_hook(hw["canvas"], context,
                                                      tools=self.tools, llm=hook_llm, workspace=_ws,
                                                      run_id=rid)
                        finally:
                            hook_llm._scene_override = _ov
                        return hw["name"], inject, result, message, rid
                    except Exception as e2:
                        # 单个钩子失败不拖垮并行组：发错误事件 + 返回空结果（不注入）
                        self._emit({"type": "auto_wf_error", "name": hw["name"], "hook": hook,
                                    "run_id": rid, "text": f"{type(e2).__name__}: {str(e2)[:200]}"})
                        return hw["name"], False, "", "", rid
                results = {}
                # 同步钩子整组超时（settings.json hook_timeout，默认 300s；0=不限）：
                # 超时的钩子发 auto_wf_error + 结果丢弃（Python 线程不可强杀——后台自然跑完但不再等它），
                # 已完成/后续完成的其它钩子结果照常合并注入。异步钩子（async=true）不受此限制。
                _timeout_s = 300
                try:
                    import config as _cfg
                    _timeout_s = max(0, int(_cfg.load_hook_timeout()))
                except Exception:
                    pass
                ex = ThreadPoolExecutor(max_workers=max(1, len(sync_hws)))
                try:
                    futs = {hw["name"]: ex.submit(_run_one, hw) for hw in sync_hws}
                    import concurrent.futures as _cf
                    _deadline = time.time() + (_timeout_s if _timeout_s else 1e18)
                    for hw in sync_hws:   # 按声明序等结果（注入顺序稳定；单 fut 按剩余 deadline 等待）
                        nm = hw["name"]
                        try:
                            _r = futs[nm].result(timeout=max(0.05, _deadline - time.time()))
                            # _run_one 返回五元组 (name, inject, result, message, rid)——剥掉 name 存四元组，
                            # 与下方合并段解包对齐（此前整存五元组 → 解包错位：inject=name/result=bool）
                            results[nm] = (_r[1], _r[2], _r[3], _r[4])
                        except _cf.TimeoutError:
                            futs[nm].cancel()
                            _LOG.warning("钩子工作流 %s 超时（>%ss）结果丢弃", nm, _timeout_s)
                            self._emit({"type": "auto_wf_error", "name": nm, "hook": hook,
                                        "run_id": "", "text": f"⏱ 超时（>{_timeout_s}s）结果已丢弃，主循环继续"})
                        except Exception as e3:
                            _LOG.warning("钩子 %s 收集异常: %s", nm, e3)
                finally:
                    ex.shutdown(wait=False, cancel_futures=True)   # 不等超时线程（结果已丢弃）
                # 按声明序合并（注入顺序稳定）
                try:
                    for hw in sync_hws:
                        nm = hw["name"]
                        inject, result, message, rid = results.get(nm, (False, "", "", ""))
                        self._emit({"type": "auto_wf", "name": nm, "hook": hook,
                                "run_id": rid, "text": result[:300] or message[:300], "auto": True})
                        if message.strip():
                            self._emit({"type": "workflow_message", "name": nm, "hook": hook,
                                    "text": message, "auto": True})
                        if inject and result.strip():
                            notes.append({"hook": hook, "name": nm, "result": result.strip()})
                except Exception as e2:
                    self._emit({"type": "auto_wf_error", "name": hw["name"], "hook": hook,
                                "text": str(e2)[:200]})
        except Exception as e:
            _LOG.error("钩子机制异常 (%s): %s", hook, e)
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
        # 钩子旁注【按位置分组】：同一位置（before_tool/after_tool/before_answer）多个触发合并成
        # 一组 <system-reminder pos="...">，用 <hook> 子标签区分各工作流；不同位置各自独立一条，
        # 保持各钩子结果在消息队列里的位置归属（而非混并成一坨）。
        if notes:
            from collections import OrderedDict
            groups = OrderedDict()
            for n in notes:
                groups.setdefault(n["hook"], []).append(n)
            for hook_pos, items in groups.items():
                parts = [f'<hook name="{n["name"]}">\n{n["result"]}\n</hook>' for n in items]
                inner = "\n".join(parts)
                msgs.append({"role": "system", "content": f'<system-reminder pos="{hook_pos}">\n{inner}\n</system-reminder>'})
        # 投影转储（调试用）：把完整 messages 以纯文本写到 projections/ 目录
        if getattr(self, "dump_projections", False):
            self._dump_projection(msgs)
        return msgs

    def _dump_projection(self, msgs: list):
        """把完整投影（messages）以纯文本写到 session_dir/projections/ 目录。"""
        try:
            sdir = getattr(self.session, "session_dir", None)
            if not sdir:
                return
            from pathlib import Path
            from datetime import datetime
            proj_dir = Path(sdir) / "projections"
            proj_dir.mkdir(parents=True, exist_ok=True)
            ts = datetime.now().strftime("%H%M%S")
            turn_num = len(self.session.turns)
            step_num = len(self.session._current.steps) if self.session._current else 0
            fname = f"t{turn_num}_s{step_num}_{ts}.txt"
            lines = [f"=== 投影转储 turn={turn_num} step={step_num} model={self.llm.model_name} time={datetime.now().isoformat()} ===\n"]
            for i, m in enumerate(msgs):
                role = m.get("role", "?")
                content = m.get("content", "") or ""   # 可能是 None（带 tool_calls 的 assistant 消息）
                # 截断超长 tool_call 相关内容（保持可读性）
                if len(content) > 8000:
                    content = content[:8000] + f"\n... (+{len(content) - 8000} chars)"
                lines.append(f"--- [{i}] role={role} ({len(content)} chars) ---")
                lines.append(content)
                lines.append("")
            (proj_dir / fname).write_text("\n".join(lines), encoding="utf-8")
        except Exception as e:
            _LOG.warning("投影转储失败: %s", e)

    # ========== Recent-file 快照采集 ==========
    def _collect_file_snapshots(self, step) -> dict:
        """扫本步所有 tool_calls，收集涉及的文件路径（最多 3 个；同路径后面覆盖前面）。
        对每个文件读当前快照（>4000 行首尾截断），返回 {call_id: {path,version,text}}。
        run_python/run_script 的处理：run_python 有 file= 时捕获该路径（但 code= 内打开文件是黑盒，漏过可接受）"""
        from real_tools import _resolve, _file_version, WORKSPACE, _number_lines, _md_snapshot
        seen = {}      # resolved_key -> call_id
        order = []     # ordered resolved_key list (most recent first)
        for tc in reversed(step.tool_calls):
            name, args, _result = self.session.toollog.view(tc.call_id)
            if name not in _FILE_SNAP_TOOLS:
                continue
            path = args.get("path") or args.get("file") or args.get("script")
            if not path:
                continue
            try:
                target = _resolve(path)
            except Exception:
                continue
            if not target.is_file():
                continue   # 目录 / 不存在 → 不拍
            key = str(target.resolve())
            if key not in seen:
                if len(order) >= _FILE_SNAP_MAX:
                    break    # 最多 3 个文件
                order.append(key)
                seen[key] = tc.call_id   # 逆序第一个（原序最后）锁定，忽略同文件旧者
        # 倒回成正序（先发生的 tool 在前），逐文件拍快照
        snapshots = {}
        for key in reversed(order):
            cid = seen[key]
            real = __import__("pathlib").Path(key)
            ver = _file_version(real)
            raw = real.read_text(encoding="utf-8")
            text = _md_snapshot(raw) if real.suffix.lower() in {".md", ".markdown"} else _number_lines(raw)
            rel = real.relative_to(WORKSPACE).as_posix()
            snapshots[cid] = {"path": rel, "version": ver, "text": text}
        return snapshots

    def resume_interrupted(self) -> str:
        """恢复中断轮。两种中断形态都支持：
        ① 归档形态（用户停止）：abort_current_turn 已归档 → _current=None，turns 尾部是中断轮 → pop 回 _current；
        ② 挂起形态（异常逃出，如 LLM 502 抛穿）：run() 异常返回但 _current 未归档——worker 已空闲，
           它就是待恢复的轮（answer 空/中断标注即中断态），直接作为恢复目标（不必 pop）。
        两条路径汇合后：清中断标注、发 turn_resume 事件、_autosave。返回 "" 成功，否则错误消息。
        已归档 steps 完整保留——若上一步 tool_call 曾发起但未完成，该 step 未归档；
        恢复后投影里看到的是已完成链，模型从断点自然续跑。"""
        from session import _INTERRUPT_MARKS
        s = self.session
        if s._current is not None:
            # ② 挂起形态：直接以挂着的 _current 续跑（此前这里无条件拒绝——异常逃出后点继续
            #    恒报"当前已有进行中的轮次"，恰是最该恢复的形态没有恢复路径）
            t = s._current
            a = (t.answer or "").strip()
            if a and a not in _INTERRUPT_MARKS:
                return "[错误] 当前轮已有回答（非中断态），无法恢复"
            t.answer = ""
            t.answer_reasoning = ""
            s._emit_event({"event": "turn_resume"})
            s._refresh_summary_cache()
            s._autosave()
            _LOG.info("resume_interrupted: 续跑挂起轮（异常逃出形态，%d 步已保留）", len(t.steps))
            return ""
        # ① 归档形态：从 turns 尾部 pop 回 _current
        if not s.turns:
            return "[错误] 没有可恢复的轮次"
        t = s.turns[-1]
        a = (t.answer or "").strip()
        if a and a not in _INTERRUPT_MARKS:
            return f"[错误] 最后一轮已正常完成（answer 非中断标注），无需恢复"
        s.turns.pop()
        t.answer = ""
        t.answer_reasoning = ""
        s._current = t
        # 分档缓存一致性：pop 掉末轮 → 清越界边界与冻结渲染（索引 >= 新长度的丢弃）
        if s._tier_boundaries and s._tier_boundaries[-1] >= len(s.turns):
            s._tier_boundaries = [b for b in s._tier_boundaries if b < len(s.turns)]
        if s._frozen_renders:
            s._frozen_renders = {k: v for k, v in s._frozen_renders.items() if k < len(s.turns)}
        s._emit_event({"event": "turn_resume"})
        s._refresh_summary_cache()
        s._autosave()
        _LOG.info("resume_interrupted: 恢复第 %d 轮（%d 步已保留，投影续接）",
                  len(s.turns) + 1, len(t.steps))
        return ""

    # ========== ReAct 主循环 ==========
    def run(self, user_message: str, images: Optional[list] = None,
            _autonomous_continue: bool = False, _seeds: Optional[list] = None,
            _resume_current: bool = False) -> str:
        """
        :param user_message: 用户消息（自主继续时为自动生成的提示）
        :param images: 图片列表
        :param _autonomous_continue: 内部标记，表示这是自主继续的一轮（用于事件区分）
        :param _seeds: 内部用——预置的合成工具记录列表（后台服务退出等异步事件）；开轮后各自落成
                        一个 Step（assistant(tool_use)→tool(结果)），让首轮上下文就带上这些事件。
        :param _resume_current: 中断轮续跑——不 start_turn 新 user_message，直接以已恢复的
                        session._current（resume_interrupted 弹回的轮）续 ReAct 循环：
                        user/steps 原样在投影里，跳过 before_turn 钩子与 user 事件（首轮已发过），
                        注入 resume 旁注引导模型从断点续做、不重复已完成步骤。
        """
        # 用循环替代递归：自主继续时走下一轮迭代而不是 self.run() 递归
        msg, auto_flag, imgs = user_message, _autonomous_continue, images
        seeds = _seeds or []   # 本轮迭代要预置的合成 Step（后台事件）；用完即清空
        while True:
            self._stop_flag = False
            resumed = bool(_resume_current and self.session._current is not None)
            if resumed:
                # 中断轮续跑：_current 已由 resume_interrupted() 恢复就位，不新开 turn
                msg = self.session._current.user_message
                _resume_current = False
            else:
                self.session.start_turn(msg, imgs)
            if seeds:
                self._seed_steps(seeds)   # 预置合成 Step → 首轮 _chat_msgs 即渲染 tool_use→tool
                seeds = []
            # —— 重置本轮钩子状态 ——
            self._hook_notes = []
            self._turn_changed_calls = []   # 本轮变更调用收集（before_answer 钩子消费）
            self._answer_redo_draft = None
            self._last_answer_draft = None
            self._answer_inject_count = 0
            self._turn_end_inject_count = 0
            self._active_hooks = set()
            if resumed:
                # resume 提示旁注：引导模型从断点续做（投影里 user+steps 完整，模型能看到断在哪；
                # 若断前某工具调用已发起未完成，该 step 未归档——模型据上下文自然重新发起）
                self._hook_notes.append({
                    "hook": "resume", "name": "interrupted_resume",
                    "result": ("本轮此前因进程退出/异常而中断，现在已从断点恢复继续。"
                               "已完成的工具调用及其结果都完整保留在上下文中；"
                               "请从中断处继续完成原任务，不要重复已完成的步骤。")})
            # —— before_turn 钩子（旧 auto:true ≡ before_turn）：用当前消息作输入预执行 ——
            # 注入方式：挂到 _current._before_turn_hint（session 投影时在 user 后渲染）。
            # 传 session_id 供工作流当上下文/日志标识；真正检索靠工具节点直接访问 session。
            bt_notes = []   # resume 时跳过（该轮首轮已检索过，重跑浪费）——首跑才走 _run_hooks
            if not resumed:
                bt_ctx = {
                    "user_message": msg,
                    "session_id": self.session.name or (self.session.session_dir.name if self.session.session_dir else ""),
                }
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
                # before_turn 对 user_message 做意图识别/预检索等预处理，结果作为【user 之后的补充】注入
                # （不拼进 user 文本）：多个钩子合并成一组挂到当前 turn，session 投影时在 user 消息后渲染
                parts = [f'<hook name="{n["name"]}">\n{n["result"]}\n</hook>' for n in bt_notes]
                self.session._current._before_turn_hint = (
                    '<system-reminder pos="before_turn">\n' + "\n".join(parts) + '\n</system-reminder>')
            if not resumed:   # 中断轮续跑：首轮已发过 user/autonomous_continue 事件，不重发（否则前端误判为新轮）
                if not auto_flag:
                    self._emit({"type": "user", "text": msg, "image_count": len(imgs or [])})
                else:
                    self._emit({"type": "autonomous_continue", "text": msg})
            _LOG.info("run 开始 session=%s: %s", self.session.name or "(未命名)", (msg or "")[:60])
            if self.snapshot_manager is not None:
                try:
                    sha = self.snapshot_manager.snapshot()
                    self.session.record_snapshot(sha)   # 设 _current.snapshot_sha + 记 snapshot 事件
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
            # 缓存本轮启用的钩子位置集合（避免每步重复扫描工作流目录）。
            # yml hook_specs 声明了钩子的 agent 取其键；未声明回退扫 workflow meta.hook（兼容期）
            try:
                specs = getattr(self.session, "hook_specs", None)
                if specs is not None:
                    self._active_hooks = {h for h, lst in specs.items() if lst}
                else:
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
                        # 中途插话注入（任何模式）：忙时排队的用户消息挂到"本步将生成"的 pending 位，
                        # 渲染为 user 消息(带标签)跟在上一组 tool 结果后；该步一生成就锚到其 preceding_hint、
                        # 后续步它滚入历史中部、不再每步尾部复读
                        if self.pending_messages:
                            inject = "；".join(self.pending_messages)
                            self.pending_messages.clear()
                            self._emit({"type": "message_injected", "text": inject})
                            self.session._current._pending_step_hint = inject
                        else:
                            self.session._current._pending_step_hint = None
                        if self.token_budget and self.cumulative_tokens >= self.token_budget:
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
                        _t_num = len(self.session.turns)   # 与 _dump_projection 同源（对上 projections/t{N}_s{M} 文件名）
                        _s_num = len(self.session._current.steps) if self.session._current else 0
                        resp = self.llm.chat(msgs, tools=tool_schemas, scene="react", turn=_t_num, step=_s_num)
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
                            }], tools=tool_schemas, scene="react")
                        # 空回答保险丝：无工具调用且 content 为空（ModelScope 等偶发空响应）→ 提示重试一次
                        if not resp.tool_calls and not (resp.content or "").strip():
                            self._emit({"type": "warn", "text": "⚠️ 模型返回空回答，已提示重试"})
                            try:
                                r2 = self.llm.chat(
                                    msgs + [{
                                        "role": "system",
                                        "content": "你上一轮返回了空内容。请给出明确的最终回答，或调用工具继续完成任务，不要返回空内容。"
                                    }], tools=tool_schemas, scene="react", turn=_t_num, step=_s_num)
                                if r2.tool_calls or (r2.content or "").strip():
                                    resp = r2
                            except Exception:
                                pass
                        if resp.usage:
                            self.cumulative_tokens += resp.usage.get("total_tokens", 0)

                        if self.token_budget and self.cumulative_tokens >= self.token_budget * 0.8:
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
                                                        "turn_context": turn_context,
                                                        "changed_calls": self._turn_changed_calls})   # 变更调用原文数组（工作流 ref 原样透传）
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
                            # turn_end 钩子：answer 确定后、finish 前——工作检查
                            # inject=true 则打回重做：注入检查结果让模型修正
                            te_notes = self._run_hooks("turn_end", {
                                "user_message": (self.session._current.user_message
                                                  if self.session._current else msg),
                                "draft_answer": resp.content,
                                "turn_context": self._turn_context_str(),
                            })
                            if te_notes and self._turn_end_inject_count < 3:
                                self._turn_end_inject_count += 1
                                self._hook_notes.extend(te_notes)
                                self._emit({"type": "warn",
                                            "text": "⚠️ turn_end 钩子检查未通过，要求模型修正"})
                                continue   # 回 step 循环：模型带检查结果继续
                            if te_notes and self._turn_end_inject_count >= 3:
                                self._emit({"type": "warn",
                                            "text": "⚠️ turn_end 钩子注入达上限(3)，强制结束本轮"})
                            self.session.finish_turn(resp.content, resp.reasoning)
                            self._emit({"type": "answer", "text": resp.content,
                                        "tokens": self.cumulative_tokens})
                            _LOG.info("回答完成 累计token=%d %d步", self.cumulative_tokens, step_num)
                            # 异步生成一句话 recap（队友可见，不进入自己上下文）：
                            # 有 recap_gen 类工作流（meta.recap=true）时由 turn_end 钩子负责（本地小模型），
                            # 无则回退内置 _generate_recap（utility client）
                            _has_recap_wf = False
                            try:
                                from real_tools import WORKSPACE as _ws3
                                from workflow import get_hook_workflows
                                _has_recap_wf = any((hw.get("meta") or {}).get("recap")
                                                    for hw in get_hook_workflows(_ws3))
                            except Exception:
                                pass
                            if not _has_recap_wf:
                                self._generate_recap(
                                    (self.session._current.user_message if self.session._current else msg),
                                    resp.content,
                                    [tc["name"] for tc in resp.tool_calls] if resp.tool_calls else None)
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
                                src, next_msg, seed = item
                                self._emit({"type": "background_trigger", "source": src,
                                            "text": next_msg[:100], "seed": bool(seed)})
                                msg, auto_flag, imgs, continue_loop = next_msg, False, None, True
                                seeds = [seed] if seed else []   # 下一轮迭代预置该合成 Step
                                break
                            # 用户插话兜底：answer 前到达但没赶上步边界注入（如 answer_reasoning
                            # 期间插话）→ pending_messages 残留。此前无消费点：消息滞留到用户
                            # 手动发下一条消息才在新一轮第 1 步边界被注入（插话"迟到"的根因）。
                            if self.pending_messages:
                                next_msg = "\n".join(f"〔用户中途补充〕{x}" for x in self.pending_messages)
                                self.pending_messages.clear()
                                self._emit({"type": "background_trigger", "source": "user_insert",
                                            "text": next_msg[:100], "seed": False})
                                msg, auto_flag, imgs, continue_loop = next_msg, False, None, True
                                break
                            return resp.content

                        # 执行工具
                        calls = resp.tool_calls
                        # 首轮首次工具调用前：提前为 session 命名并异步落盘（不阻塞工具执行）
                        if not self.session.name:
                            tool_names = [tc["name"] for tc in calls]
                            self.session._ensure_name_early(
                                user_message=(self.session._current.user_message
                                             if self.session._current else msg),
                                reasoning=resp.reasoning or "",
                                tool_names=tool_names
                            )
                        step = Step(reasoning=resp.reasoning)
                        # 锚定本步 pending 的中途插话 → 渲染在 assistant 前(随本步滚入历史)；清 pending 防 wrap_up 重复
                        step.preceding_hint = getattr(self.session._current, "_pending_step_hint", "") or ""
                        self.session._current._pending_step_hint = None
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
                                # 工具执行前 workspace 快照（真实副作用检测；链式复用上次 after 快照省一半扫描）
                                # before_answer 钩子存在时也做快照（_turn_changed_calls 收集用——wiki 维护等钩子
                                # 直接消费变更调用原文，免子 Agent read_file 重读源文件）
                                _snap_before = None
                                _need_snap = ("after_tool" in self._active_hooks) or ("before_answer" in self._active_hooks)
                                if _need_snap:
                                    _snap_before = self._fs_snap if self._fs_snap is not None else _workspace_snapshot()
                                _t0 = time.time()
                                result = self.tools.call(tc["name"], tc["arguments"])
                                _LOG.info("工具 %s 耗时%.1fs 结果%d字", tc["name"],
                                          time.time() - _t0, len(result or ""))
                                cid = self.session.toollog.next_id()
                                result = self._materialize_tool_result(result, tc["name"], tc["arguments"], cid)
                                if _need_snap:
                                    _snap_after = _workspace_snapshot()
                                    self._fs_snap = _snap_after   # 下一个 call 的 before 基准
                                    changed = _diff_snapshots(_snap_before, _snap_after)
                                    if changed:
                                        # 变更调用收集：原文（args+result 预览）随 changed_files 存内存，
                                        # before_answer 钩子（wiki 维护）直接消费——子 Agent 不必 read_file
                                        self._turn_changed_calls.append({
                                            "call_id": cid, "name": tc["name"],
                                            "arguments": tc["arguments"],
                                            "result_preview": (result or "")[:800],
                                            "changed_files": changed,
                                        })
                                    if "after_tool" in self._active_hooks:
                                        self._hook_notes += self._run_hooks("after_tool", {
                                            "user_message": cur_user_msg, "tool_name": tc["name"],
                                            "tool_args": tc_args_json, "tool_result": result,
                                            "changed_files": changed})   # 直接传数组（工作流 ref 原样透传，无需序列化往返）
                                self._emit({"type": "tool_result", "name": tc["name"],
                                            "result": self._truncate(result), "parallel": len(calls) > 1})
                                self.session.toollog.record(cid, tc["name"], tc["arguments"], result)
                                step.tool_calls.append(ToolCall(call_id=cid))
                        elif len(calls) == 1:
                            tc = calls[0]
                            self._emit({"type": "tool_call", "name": tc["name"], "arguments": tc["arguments"]})
                            _snap_b1 = None
                            _need1 = "before_answer" in self._active_hooks
                            if _need1:
                                _snap_b1 = self._fs_snap if self._fs_snap is not None else _workspace_snapshot()
                            _t0 = time.time()
                            result = self.tools.call(tc["name"], tc["arguments"])
                            _LOG.info("工具 %s 耗时%.1fs 结果%d字", tc["name"], time.time() - _t0, len(result or ""))
                            cid = self.session.toollog.next_id()
                            result = self._materialize_tool_result(result, tc["name"], tc["arguments"], cid)
                            if _need1:
                                _snap_a1 = _workspace_snapshot()
                                self._fs_snap = _snap_a1
                                _ch1 = _diff_snapshots(_snap_b1, _snap_a1)
                                if _ch1:
                                    self._turn_changed_calls.append({
                                        "call_id": cid, "name": tc["name"], "arguments": tc["arguments"],
                                        "result_preview": (result or "")[:800], "changed_files": _ch1})
                            self._emit({"type": "tool_result", "name": tc["name"],
                                        "result": self._truncate(result), "parallel": False})
                            self.session.toollog.record(cid, tc["name"], tc["arguments"], result)
                            step.tool_calls.append(ToolCall(call_id=cid))
                        else:
                            self._emit({"type": "parallel", "count": len(calls)})
                            for tc in calls:
                                self._emit({"type": "tool_call", "name": tc["name"], "arguments": tc["arguments"]})
                            _snap_b2 = None
                            _need2 = "before_answer" in self._active_hooks
                            if _need2:
                                _snap_b2 = self._fs_snap if self._fs_snap is not None else _workspace_snapshot()
                            _t0 = time.time()
                            results = self._run_tools_parallel(calls)
                            _LOG.info("并行 %d 工具 耗时%.1fs", len(calls), time.time() - _t0)
                            _ch2 = _diff_snapshots(_snap_b2, _workspace_snapshot()) if _need2 and _snap_b2 is not None else []
                            if _need2:
                                self._fs_snap = _workspace_snapshot()
                            for tc, result in zip(calls, results):
                                _LOG.debug("  └ %s 结果%d字", tc["name"], len(result or ""))
                                cid = self.session.toollog.next_id()
                                result = self._materialize_tool_result(result, tc["name"], tc["arguments"], cid)
                                if _ch2:   # 整批 diff 归属到批内每个调用（多报不漏报，方向安全）
                                    self._turn_changed_calls.append({
                                        "call_id": cid, "name": tc["name"], "arguments": tc["arguments"],
                                        "result_preview": (result or "")[:800], "changed_files": _ch2})
                                self._emit({"type": "tool_result", "name": tc["name"],
                                            "result": self._truncate(result), "parallel": True})
                                self.session.toollog.record(cid, tc["name"], tc["arguments"], result)
                                step.tool_calls.append(ToolCall(call_id=cid))
                        _rt._tool_emit = None  # 清理
                        step.file_snapshots = self._collect_file_snapshots(step)   # recent-file 快照
                        self.session.add_step(step)
                        # 动态注册的工具（新写的工作流、ensure_lsp 装的 LSP 等）当轮即可见：
                        # 仍扫描新写的工作流/工具脚本（注册进 toolbox）+ 每步无条件重算 schemas
                        # （schemas 无缓存，只是 dict 遍历，成本低）
                        self._refresh_tools_if_written(step)
                        tool_schemas = self.tools.schemas()

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
                self.session.abort_current_turn("（被用户停止）")   # 与 stop_flag 路径统一文案（旧"（被用户中断）"不在 _INTERRUPT_MARKS，resume 会拒绝）
                return ""

    def _wrap_up(self) -> str:
        """预算/步数到顶时，做一次无工具的总结性收尾。"""
        msgs = self.session.messages_for_llm() + [{
            "role": "system",
            "content": "token 预算或步数已达上限。请基于目前已有的工具结果，直接给出最终总结性回答，不要再调用工具。"
        }]
        try:
            resp = self.utility_client().chat(msgs, scene="wrap_up")   # 收尾总结走辅助模型（预算耗尽时主模型可能正是问题源）
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

