"""session.py —— 分层上下文引擎（完整原文不丢版）。

结构 Turn > Step > ToolCall（"每轮请求中的多轮工具调用分层管理"）：
  - 一轮用户请求 = 一个 Turn，内含若干 Step，每个 Step 是一次 LLM 调用，可带多个 ToolCall。
  - 喂给 LLM 的上下文 = system + 【窗口外各轮 summary 拼接】+ 近期若干轮原文(recent window)。
  - 完整原文永不丢：self.turns 不再被截断，超出近期窗口的旧 Turn 只把它的 summary
    拼进 global_summary 喂给模型，原文仍完整留在内存 + 存档里，可按需召回。
  - 每轮 finish 时生成该轮 summary（贴在该轮最后，作语义索引 + 窗口外摘要源）。
  - recall(query)：用关键词在全部历史里搜，召回匹配轮的完整上下文（不含 reasoning）。
  - 首轮自动命名（一句话总结）；每轮异步自动落盘；save/load 结构化持久化。

设计要点（延续前面的教训）：
  - reasoning 不进历史，只存 content/工具调用；召回时也丢弃 reasoning。
  - 摘要源是该轮自带的 summary 字段，窗口外拼接便宜（纯字符串 join），超长才压缩并缓存。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import threading
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional, Callable

from llm_client import LLMClient

# 会话存档放用户主目录：~/.agt/repos/<repo-hash>/sessions/。每个 repo 一棵目录树
# （sessions/ + 未来可加其它子目录），互相隔离。放包目录会在 pip 安装后写进
# site-packages（不可写/难找），故统一到 ~/.agt，与 models.json/settings.json 同惯例。
REPOS_DIR = Path.home() / ".agt" / "repos"
# 旧位置（用于一次性自动迁移；SESSIONS_DIR 同时保留作 legacy 别名供 commands.py 等 import）：
SESSIONS_DIR = Path.home() / ".agt" / "sessions"                              # 上一版 ~/.agt/sessions/<hash>/
_LEGACY_SESSIONS_DIR = Path(__file__).resolve().parent.parent / "sessions"   # 开发期项目根（pip 装后不存在）


def _repo_hash(workspace) -> str:
    """把工作区路径稳定地哈希成 12 位十六进制（固定位、文件系统安全、跨运行稳定）。"""
    return hashlib.sha1(str(Path(workspace).resolve()).encode("utf-8")).hexdigest()[:12]


def _repo_sessions_dir(workspace) -> Path:
    """该工作区的会话子目录：~/.agt/repos/<hash>/sessions/。每个 repo 互相隔离。
    首次访问时把两处旧位置的存档一次性整体迁移过来。"""
    h = _repo_hash(workspace)
    d = REPOS_DIR / h / "sessions"
    d.mkdir(parents=True, exist_ok=True)
    _migrate_all_legacy()
    return d


_ALL_MIGRATED = False   # 进程级标志：全量迁移只跑一次


def _migrate_all_legacy() -> None:
    """一次性把旧位置的存档搬到 ~/.agt/repos/<hash>/sessions/。
    两处旧源：项目根 sessions/<hash>/（开发期）、~/.agt/sessions/<hash>/（上一版结构）。
    每个 hash 目标为空才迁（copy 不删源），避免覆盖新存档；旧目录可手动清理。"""
    global _ALL_MIGRATED
    if _ALL_MIGRATED:
        return
    _ALL_MIGRATED = True
    try:
        for legacy_root in (_LEGACY_SESSIONS_DIR, SESSIONS_DIR):
            if not legacy_root.exists():
                continue
            for legacy_hash_dir in legacy_root.iterdir():
                if not legacy_hash_dir.is_dir():
                    continue
                target = REPOS_DIR / legacy_hash_dir.name / "sessions"
                _migrate_one(legacy_hash_dir, target)
    except Exception:
        pass  # 迁移失败绝不影响正常读写


def _migrate_one(legacy_dir: Path, target: Path) -> None:
    """把 legacy_dir 的 *.json + _origin.txt 搬到 target（目标为空才迁）。"""
    try:
        if any(target.glob("*.json")):
            return  # 目标已有存档，不动
        old_files = list(legacy_dir.glob("*.json"))
        if not old_files:
            return
        target.mkdir(parents=True, exist_ok=True)
        for f in old_files:
            shutil.copy2(f, target / f.name)
        origin = legacy_dir / "_origin.txt"
        if origin.exists():
            shutil.copy2(origin, target / "_origin.txt")
    except Exception:
        pass

GLOBAL_SUMMARY_CAP = 2000  # 窗口外 summary 拼接超过这么多字就再压缩一次

# 文件名安全字符：保留字母数字下划线 + 中文，其余替成 _
_NAME_SAFE_RE = re.compile(r"[^\w一-鿿]")


@dataclass
class ToolCall:
    id: str = ""
    name: str = ""
    arguments: dict = field(default_factory=dict)
    result: str = ""


@dataclass
class Step:
    reasoning: str = ""
    tool_calls: list = field(default_factory=list)  # list[ToolCall]


@dataclass
class Turn:
    user_message: str
    images: list = field(default_factory=list)       # list[str] 用户附带的图片(data URL)，多模态用
    snapshot_sha: str = ""                           # 该轮发送前的工作区快照(检查点回溯用)
    steps: list = field(default_factory=list)        # list[Step]
    answer: str = ""
    answer_reasoning: str = ""                       # 最终回答那步的 reasoning_content（GLM 等要求回传）
    summary: str = ""                                # 该轮的一句话摘要（finish 时生成，贴在该轮最后）


class Session:
    def __init__(self, system: str, llm: Optional[LLMClient] = None,
                 recent_window_turns: int = 4, max_steps_per_turn: int = 80,
                 workspace=None):
        self.system = system
        self.llm = llm or LLMClient(enable_thinking=False, temperature=0.3)
        self.recent_window_turns = recent_window_turns
        self.max_steps_per_turn = max_steps_per_turn  # 0/None = 不限
        self.workspace = Path(workspace) if workspace else Path.cwd()
        self.turns: list[Turn] = []
        self.global_summary = ""
        self.name: str = ""                           # session 自动命名（首轮一句话总结）
        self._current: Optional[Turn] = None          # 进行中的轮（run 期间）
        self._save_lock = threading.Lock()            # 异步落盘的并发保护
        self._summary_sig: tuple = ()                 # 窗口外 summary 缓存的失效签名
        self.extra_state: dict = {}                   # 附加运行时状态（Agent 经 _state_provider 收集：plan/自主模式等）
        self._state_provider: Optional[Callable[[], dict]] = None  # Agent 注册的附加状态收集回调
        self._system_extra_provider: Optional[Callable[[], str]] = None  # Agent 注册：返回动态 system 段（后台服务状态等）

    # ========== 构建 ==========
    def start_turn(self, user_message: str, images: Optional[list] = None):
        self._current = Turn(user_message=user_message, images=images or [])

    def add_step(self, step: Step):
        if self._current is None:
            raise RuntimeError("没有进行中的 Turn，请先 start_turn()")
        self._current.steps.append(step)

    def finish_turn(self, answer: str, answer_reasoning: str = ""):
        if self._current is None:
            return
        self._current.answer = answer
        self._current.answer_reasoning = answer_reasoning
        # 生成该轮 summary（贴在该轮最后：作语义索引 + 窗口外摘要源 + 召回匹配文本）
        try:
            self._current.summary = self._summarize_turn(self._current)
        except Exception:
            self._current.summary = ""
        self.turns.append(self._current)
        self._current = None
        self._ensure_name()            # 首轮自动命名（落盘前确保 name 就绪）
        self._refresh_summary_cache()  # 维护窗口外 summary 拼接（不截断 turns）
        self._autosave()               # 异步落盘

    def abort_current_turn(self, note: str = "（被中断）"):
        """中断时把进行中的轮收尾，避免丢失已完成的步骤。"""
        if self._current is None:
            return
        self._current.answer = note
        try:
            self._current.summary = self._summarize_turn(self._current)
        except Exception:
            self._current.summary = ""
        self.turns.append(self._current)
        self._current = None
        self._ensure_name()
        self._refresh_summary_cache()
        self._autosave()

    def restore_to_snapshot(self, sha: str) -> Optional[str]:
        """检查点回溯：找到 snapshot_sha==sha 的那轮，截断它及之后的轮，回到它【之前】。
        返回那轮的用户消息（供 UI 提示）；找不到返回 None。"""
        for i, t in enumerate(self.turns):
            if t.snapshot_sha == sha:
                target_msg = t.user_message
                self.turns = self.turns[:i]
                self._current = None
                self._refresh_summary_cache()
                self._autosave()  # 回溯后也落盘
                return target_msg
        return None

    # ========== 融合上下文（关键）==========
    def messages_for_llm(self) -> list[dict]:
        """返回 system + 【窗口外各轮 summary 拼接】+ 近期窗口(逐 step 还原) + 当前进行中的轮。

        self.turns 现在是全量（永不截断）：recent 窗口外的旧 Turn 不进消息体，而是通过
        self.global_summary（窗口外各轮 summary 的拼接/压缩）以一条 system 摘要喂给模型。
        需要早期轮的细节时，模型可用 recall_turn 工具按需召回完整原文。
        """
        msgs = [{"role": "system", "content": self.system}]
        if self._system_extra_provider:
            try:
                extra = self._system_extra_provider()
                if extra:
                    msgs.append({"role": "system", "content": extra})
            except Exception:
                pass
        if self.global_summary:
            msgs.append({"role": "system", "content": "【历史会话摘要】\n" + self.global_summary})

        recent = self.turns[-self.recent_window_turns:]
        for t in recent:
            msgs.append({"role": "user", "content": self._user_content(t)})
            msgs.extend(self._steps_to_messages(t.steps, self.max_steps_per_turn))
            if t.answer:
                a_msg = {"role": "assistant", "content": t.answer}
                if t.answer_reasoning:
                    a_msg["reasoning_content"] = t.answer_reasoning
                msgs.append(a_msg)

        # 当前进行中的轮：带上它的 user_message 和已完成的步骤（保证工具对话连续）
        if self._current is not None:
            msgs.append({"role": "user", "content": self._user_content(self._current)})
            msgs.extend(self._steps_to_messages(self._current.steps, self.max_steps_per_turn))
            # 自主模式下用户插入的消息：在工具结果后以 system 消息注入，Agent 下一步就能看到
            hint = getattr(self._current, "_user_hint", None)
            if hint:
                msgs.append({"role": "system", "content": f"📨 用户在自主模式运行期间发来消息：\n{hint}"})
        return msgs

    @staticmethod
    def _user_content(turn: "Turn"):
        """构造 user 消息内容：纯文本→str；带图片→多模态 [text + image_url] 块。"""
        if not turn.images:
            return turn.user_message
        blocks = [{"type": "text", "text": turn.user_message}]
        blocks.extend({"type": "image_url", "image_url": {"url": img}} for img in turn.images)
        return blocks

    @staticmethod
    def _steps_to_messages(steps: list[Step], max_steps: int = 0) -> list[dict]:
        """把一组 Step 还原成 role 消息：assistant(tool_calls + reasoning_content) + 各 tool 结果。
        max_steps>0 时只保留最近 max_steps 步，超过的在开头加省略提示。"""
        msgs = []
        if max_steps and len(steps) > max_steps:
            skipped = len(steps) - max_steps
            steps = steps[-max_steps:]
            msgs.append({"role": "system", "content": f"（本轮的 {skipped} 个早期步骤已省略，仅保留最近 {max_steps} 步）"})
        for step in steps:
            if not step.tool_calls:
                continue
            a_msg = {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {"id": tc.id or str(i), "type": "function",
                     "function": {"name": tc.name,
                                  "arguments": json.dumps(tc.arguments, ensure_ascii=False)}}
                    for i, tc in enumerate(step.tool_calls)
                ],
            }
            if step.reasoning:
                a_msg["reasoning_content"] = step.reasoning
            msgs.append(a_msg)
            for i, tc in enumerate(step.tool_calls):
                msgs.append({"role": "tool", "tool_call_id": tc.id or str(i), "content": tc.result})
        return msgs

    # ========== 窗口外摘要缓存（不再截断 turns）==========
    def _refresh_summary_cache(self):
        """维护 global_summary = 窗口外各轮 summary 的拼接（超长则压缩，按签名缓存）。
        关键：不再截断 self.turns——完整原文永久保留，这里只决定「窗口外的轮喂给模型时的摘要形态」。"""
        if len(self.turns) <= self.recent_window_turns:
            self.global_summary = ""
            self._summary_sig = ()
            return
        outside = self.turns[:-self.recent_window_turns]
        sig = (len(outside), len(self.turns))  # 窗口外集合变了才重算
        if sig == self._summary_sig and self.global_summary:
            return
        parts = [f"[第{i + 1}轮] {(t.summary or t.user_message[:40]).strip()}"
                 for i, t in enumerate(outside)]
        self.global_summary = "\n".join(parts)
        if len(self.global_summary) > GLOBAL_SUMMARY_CAP:
            self.global_summary = self._compress_summary()
        self._summary_sig = sig

    def _summarize_turn(self, turn: Turn) -> str:
        """用一次短 LLM 调用把一轮压成 2-3 句中文摘要。"""
        tools = "; ".join(
            f"{tc.name}({tc.arguments})→{tc.result[:80]}"
            for step in turn.steps for tc in step.tool_calls
        )[:600]
        prompt = (
            "把下面这一轮对话压成 2-3 句中文摘要，保留：用户意图、用了什么工具/做了什么、关键结果。\n"
            f"用户: {turn.user_message}\n"
            f"工具调用: {tools or '无'}\n"
            f"最终回答: {turn.answer[:300]}"
        )
        try:
            return self.llm.chat([{"role": "user", "content": prompt}]).content.strip()
        except Exception as e:
            return f"[摘要失败 {e}] 用户: {turn.user_message[:60]}；回答: {turn.answer[:60]}"

    def _compress_summary(self) -> str:
        prompt = ("把下面这段多轮会话摘要进一步压缩成一个更短的整体摘要"
                  "（保留关键决策、当前状态、重要结论），不超过 800 字:\n\n" + self.global_summary)
        try:
            return self.llm.chat([{"role": "user", "content": prompt}]).content.strip()
        except Exception:
            return self.global_summary[-GLOBAL_SUMMARY_CAP:]  # 兜底：截断保留最近部分

    # ========== 自动命名 ==========
    def _ensure_name(self):
        """首轮完成后自动给 session 命名（一句话总结首轮）。name 一旦设定不再变。
        在落盘前调用，确保 _autosave 有稳定文件名。"""
        if self.name or not self.turns:
            return
        first = self.turns[0]
        prompt = ("用一句话（≤20个中文字）总结下面这轮对话的主题，作为会话标题。"
                  "只输出标题文字本身，不要引号、不要任何解释、不要句末标点：\n"
                  f"用户: {first.user_message[:200]}\n回答: {first.answer[:200]}")
        title = ""
        try:
            title = self.llm.chat([{"role": "user", "content": prompt}]).content.strip()
            title = title.split("\n")[0].strip().strip("。.！!？?\"'“”‘’")
        except Exception:
            title = ""
        safe = _NAME_SAFE_RE.sub("_", title)[:30].strip("_") if title else ""
        if safe:
            self.name = safe
            return
        # fallback：用首轮 user_message 片段，再不行用时间戳
        seed = _NAME_SAFE_RE.sub("", first.user_message[:12]).strip()
        self.name = ("session_" + seed) if seed else f"session_{int(time.time())}"

    # ========== 异步自动落盘 ==========
    def _capture_state(self):
        """落盘前从 Agent 收集附加运行时状态（plan/自主模式等）进 extra_state。
        Agent 通过 self._state_provider 回调注册收集器；未注册则跳过。"""
        if self._state_provider is not None:
            try:
                self.extra_state = self._state_provider() or {}
            except Exception:
                pass

    def _autosave(self):
        """每轮结束后异步落盘（daemon 线程，不阻塞主循环）。失败静默，绝不影响主循环。
        注意：不在本层持锁——save() 内部用同一把锁保护「快照+序列化+写文件」整段，
        本层再持锁会和 save() 二次获取同一把不可重入 Lock 导致死锁。"""
        name = self.name
        if not name:
            return  # name 未就绪本轮跳过（_ensure_name 已尽量保证非空）
        # _capture_state 由 save() 内部完成，此处只负责异步触发 save
        def _write():
            try:
                self.save(name)
            except Exception:
                pass
        threading.Thread(target=_write, daemon=True).start()

    # ========== 召回（Agent / 用户按需查完整原文）==========
    def recall(self, query: str) -> str:
        """按关键词在【全部】历史轮次里搜索，返回匹配轮的完整上下文（不含 reasoning）。
        匹配域：summary + user_message + answer（大小写不敏感子串，中文直接子串）。"""
        if not self.turns:
            return "（当前会话还没有历史轮次）"
        q = (query or "").strip().lower()
        if not q:
            return "（请提供要搜索的关键词）"
        hits = [(i, t) for i, t in enumerate(self.turns)
                if q in (t.summary + "\n" + t.user_message + "\n" + t.answer).lower()]
        if not hits:
            return f"未找到包含「{query}」的历史轮次。可用 /recall 换个关键词，或 /show 看概览。"
        out, total, CAP = [f"找到 {len(hits)} 轮匹配「{query}」的历史："], 0, 4000
        for i, t in hits:
            block = self._format_turn_full(i + 1, t)
            if total + len(block) > CAP:
                out.append(f"\n…（还有 {len(hits) - len(out) + 1} 轮命中已省略）")
                break
            out.append(block)
            total += len(block)
        return "\n".join(out)

    def _format_turn_full(self, n: int, t: Turn) -> str:
        """把一轮格式化成可读文本（召回展示用）。不含 reasoning。"""
        lines = [f"━━━ 【第{n}轮】{t.summary or '(无摘要)'}", f"用户: {t.user_message}"]
        for step in t.steps:
            for tc in step.tool_calls:
                args_s = json.dumps(tc.arguments, ensure_ascii=False)
                lines.append(f"  🔧 {tc.name}({args_s}) → {(tc.result or '')[:300]}")
        lines.append(f"回答: {t.answer}")
        return "\n".join(lines)

    def to_history(self) -> list:
        """导出全量历史（结构化），供 webui resume 后原样渲染。不含 reasoning。"""
        out = []
        for i, t in enumerate(self.turns):
            steps = []
            for s in t.steps:
                tcs = [{"name": tc.name, "arguments": tc.arguments, "result": (tc.result or "")[:500]}
                       for tc in s.tool_calls]
                if tcs:
                    steps.append({"tool_calls": tcs})
            out.append({"turn": i + 1, "user": t.user_message, "answer": t.answer,
                        "summary": t.summary, "steps": steps})
        return out

    # ========== 持久化 ==========
    def save(self, name: Optional[str] = None) -> Path:
        name = name or self.name or f"session_{int(time.time())}"
        if not name.endswith(".json"):
            name += ".json"
        self._capture_state()  # 落盘前收集 Agent 附加状态（plan/自主模式等），无论谁触发 save
        d = _repo_sessions_dir(self.workspace)
        (d / "_origin.txt").write_text(str(self.workspace.resolve()), encoding="utf-8")  # 便于人眼追溯
        path = d / name
        # 锁保护「快照 turns + 序列化 + 写文件」整段：与 _autosave 的 daemon 线程、
        # 以及 /save 命令的并发写互斥；list(self.turns) 快照后，主线程 append 新 turn 不影响本次落盘。
        with self._save_lock:
            snapshot = list(self.turns)
            data = {
                "name": self.name or Path(name).stem,
                "system": self.system,
                "global_summary": self.global_summary,
                "recent_window_turns": self.recent_window_turns,
                "max_steps_per_turn": self.max_steps_per_turn,
                "turns": [asdict(t) for t in snapshot],   # 全量，不再截断；每个 turn 含 summary 字段
                "extra_state": self.extra_state,          # 附加运行时状态（plan/自主模式等）
                "saved_at": int(time.time()),
            }
            # 原子写：先写 .tmp 再 os.replace，避免 autosave(daemon 线程) 与 load 并发时读到半个文件
            tmp_path = path.with_suffix(path.suffix + ".tmp")
            tmp_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp_path, path)
        return path

    @classmethod
    def load(cls, path_or_name: str, llm: Optional[LLMClient] = None, workspace=None) -> "Session":
        ws = workspace or Path.cwd()
        path = _resolve_session_path(path_or_name, ws)
        data = json.loads(path.read_text(encoding="utf-8"))
        s = cls(system=data["system"], llm=llm,
                recent_window_turns=data.get("recent_window_turns", 4),
                max_steps_per_turn=data.get("max_steps_per_turn", 80), workspace=ws)
        s.name = data.get("name") or path.stem   # 旧存档无 name → 用文件名，保证继续对话时覆盖同一文件
        s.global_summary = data.get("global_summary", "")
        s.turns = [_turn_from_dict(d) for d in data.get("turns", [])]
        s.extra_state = data.get("extra_state", {})  # 附加运行时状态（plan/自主模式等）
        s._summary_sig = ()  # 让首次 _refresh_summary_cache 重算
        return s

    # ========== 展示 ==========
    def summary_str(self) -> str:
        lines = []
        if self.name:
            lines.append(f"会话名称: {self.name}")
        lines.append(f"已完成轮数: {len(self.turns)}")
        lines.append(f"近期窗口: 最近 {self.recent_window_turns} 轮（原文），更早的以摘要喂给模型、原文仍可召回")
        if self.global_summary:
            lines.append(f"窗口外摘要({len(self.global_summary)}字): {self.global_summary[:200]}...")
        lines.append("近期轮次:")
        for i, t in enumerate(self.turns[-5:], 1):
            n_tools = sum(len(s.tool_calls) for s in t.steps)
            lines.append(f"  {i}. 「{t.user_message[:30]}」→ {n_tools}次工具调用 →「{t.answer[:30]}」")
        return "\n".join(lines)

    def __repr__(self):
        return f"Session(name={self.name!r}, turns={len(self.turns)}, summary={'yes' if self.global_summary else 'no'})"


def _turn_from_dict(d: dict) -> Turn:
    t = Turn(user_message=d.get("user_message", ""),
             images=d.get("images", []),
             snapshot_sha=d.get("snapshot_sha", ""),
             answer=d.get("answer", ""), answer_reasoning=d.get("answer_reasoning", ""),
             summary=d.get("summary", ""))
    for s in d.get("steps", []):
        step = Step(reasoning=s.get("reasoning", ""))
        for tc in s.get("tool_calls", []):
            step.tool_calls.append(ToolCall(
                id=tc.get("id", ""), name=tc.get("name", ""),
                arguments=tc.get("arguments", {}), result=tc.get("result", "")))
        t.steps.append(step)
    return t


def _resolve_session_path(path_or_name: str, workspace=None) -> Path:
    """查找会话文件：优先新目录 ~/.agt/repos/<hash>/sessions/，
    再回退旧 ~/.agt/sessions/<hash>/（迁移前的兼容读取）。"""
    ws = workspace or Path.cwd()
    repo_dir = _repo_sessions_dir(ws)
    legacy_dir = SESSIONS_DIR / _repo_hash(ws)
    for base in (repo_dir, legacy_dir):
        for cand in (Path(path_or_name), base / path_or_name, base / (path_or_name + ".json")):
            if cand.exists():
                return cand
    raise FileNotFoundError(f"找不到会话文件: {path_or_name}（可在 /list 查看）")


def list_sessions(workspace=None) -> list[Path]:
    """列出该工作区 hash 子目录下的会话（按修改时间倒序）。"""
    return sorted(_repo_sessions_dir(workspace or Path.cwd()).glob("*.json"),
                  key=lambda p: p.stat().st_mtime, reverse=True)


def session_meta(p: Path) -> dict:
    """轻量读一个会话文件的展示元信息：{id, name, turns, first}。读取出错返回兜底。"""
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        turns = data.get("turns", [])
        first = (turns[0].get("user_message", "") if turns else "")[:30]
        return {"id": p.stem, "name": data.get("name") or p.stem,
                "turns": len(turns), "first": first}
    except Exception:
        return {"id": p.stem, "name": p.stem, "turns": 0, "first": "(读取失败)"}
