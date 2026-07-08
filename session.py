"""session.py —— 分层上下文引擎（Step 8 强化的核心）。

结构 Turn > Step > ToolCall（"每轮请求中的多轮工具调用分层管理"）：
  - 一轮用户请求 = 一个 Turn，内含若干 Step，每个 Step 是一次 LLM 调用，可带多个 ToolCall。
  - 喂给 LLM 的上下文 = system + 全局摘要(global_summary) + 近期若干轮原文(recent window)。
  - 超出近期窗口的旧 Turn 被压成摘要并入 global_summary —— 这就是"摘要 + 窗口融合"。
  - save/load 把整个 Session 结构化持久化，配合 /save /resume 斜杠命令。

设计要点（延续前面的教训）：
  - reasoning 不进历史，只存 content/工具调用。
  - 旧轮"惰性摘要"：只在被挤出近期窗口时才花一次 LLM 调用总结，窗口内的轮不额外花钱。
"""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional

from llm_client import LLMClient

SESSIONS_DIR = Path(__file__).parent / "sessions"
SESSIONS_DIR.mkdir(exist_ok=True)


def _repo_hash(workspace) -> str:
    """把工作区路径稳定地哈希成 12 位十六进制（固定位、文件系统安全、跨运行稳定）。"""
    return hashlib.sha1(str(Path(workspace).resolve()).encode("utf-8")).hexdigest()[:12]


def _repo_sessions_dir(workspace) -> Path:
    """该工作区的会话子目录：sessions/<hash>/。每个 repo 的存档互相隔离。"""
    d = SESSIONS_DIR / _repo_hash(workspace)
    d.mkdir(parents=True, exist_ok=True)
    return d

GLOBAL_SUMMARY_CAP = 2000  # global_summary 超过这么多字就再压缩一次


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
    summary: str = ""


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
        self._current: Optional[Turn] = None  # 进行中的轮（run 期间）

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
        self.turns.append(self._current)
        self._current = None
        self._compact()

    def abort_current_turn(self, note: str = "（被中断）"):
        """中断时把进行中的轮收尾，避免丢失已完成的步骤。"""
        if self._current is None:
            return
        self._current.answer = note
        self.turns.append(self._current)
        self._current = None
        self._compact()

    def restore_to_snapshot(self, sha: str) -> Optional[str]:
        """检查点回溯：找到 snapshot_sha==sha 的那轮，截断它及之后的轮，回到它【之前】。
        返回那轮的用户消息（供 UI 提示）；找不到返回 None。"""
        for i, t in enumerate(self.turns):
            if t.snapshot_sha == sha:
                target_msg = t.user_message
                self.turns = self.turns[:i]
                self._current = None
                return target_msg
        return None

    # ========== 融合上下文（关键）==========
    def messages_for_llm(self) -> list[dict]:
        """返回 system + 全局摘要 + 近期窗口(逐 step 还原) + 当前进行中的轮。"""
        msgs = [{"role": "system", "content": self.system}]
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

    # ========== 压缩 ==========
    def _compact(self):
        """把超出近期窗口的旧 Turn 压成摘要并入 global_summary。"""
        if len(self.turns) <= self.recent_window_turns:
            return
        evict = self.turns[:-self.recent_window_turns]
        self.turns = self.turns[-self.recent_window_turns:]
        for t in evict:
            self.global_summary = (self.global_summary + "\n" + self._summarize_turn(t)).strip()
        if len(self.global_summary) > GLOBAL_SUMMARY_CAP:
            self.global_summary = self._compress_summary()

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

    # ========== 持久化 ==========
    def save(self, name: Optional[str] = None) -> Path:
        name = name or f"session_{int(time.time())}"
        if not name.endswith(".json"):
            name += ".json"
        d = _repo_sessions_dir(self.workspace)
        (d / "_origin.txt").write_text(str(self.workspace.resolve()), encoding="utf-8")  # 便于人眼追溯
        path = d / name
        data = {
            "system": self.system,
            "global_summary": self.global_summary,
            "recent_window_turns": self.recent_window_turns,
            "max_steps_per_turn": self.max_steps_per_turn,
            "turns": [asdict(t) for t in self.turns],
            "saved_at": int(time.time()),
        }
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    @classmethod
    def load(cls, path_or_name: str, llm: Optional[LLMClient] = None, workspace=None) -> "Session":
        ws = workspace or Path.cwd()
        path = _resolve_session_path(path_or_name, ws)
        data = json.loads(path.read_text(encoding="utf-8"))
        s = cls(system=data["system"], llm=llm,
                recent_window_turns=data.get("recent_window_turns", 4),
                max_steps_per_turn=data.get("max_steps_per_turn", 80), workspace=ws)
        s.global_summary = data.get("global_summary", "")
        s.turns = [_turn_from_dict(d) for d in data.get("turns", [])]
        return s

    # ========== 展示 ==========
    def summary_str(self) -> str:
        lines = [f"已完成轮数: {len(self.turns)}",
                 f"近期窗口: 最近 {self.recent_window_turns} 轮（原文），更早的已压成摘要"]
        if self.global_summary:
            lines.append(f"全局摘要({len(self.global_summary)}字): {self.global_summary[:200]}...")
        lines.append("近期轮次:")
        for i, t in enumerate(self.turns[-5:], 1):
            n_tools = sum(len(s.tool_calls) for s in t.steps)
            lines.append(f"  {i}. 「{t.user_message[:30]}」→ {n_tools}次工具调用 →「{t.answer[:30]}」")
        return "\n".join(lines)

    def __repr__(self):
        return f"Session(turns={len(self.turns)}, summary={'yes' if self.global_summary else 'no'})"


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
    """查找会话文件：优先该工作区的 hash 子目录，再回退到扁平根目录(兼容旧存档)。"""
    repo_dir = _repo_sessions_dir(workspace or Path.cwd())
    for base in (repo_dir, SESSIONS_DIR):
        for cand in (Path(path_or_name), base / path_or_name, base / (path_or_name + ".json")):
            if cand.exists():
                return cand
    raise FileNotFoundError(f"找不到会话文件: {path_or_name}（可在 /list 查看）")


def list_sessions(workspace=None) -> list[Path]:
    """列出该工作区 hash 子目录下的会话（按修改时间倒序）。"""
    return sorted(_repo_sessions_dir(workspace or Path.cwd()).glob("*.json"),
                  key=lambda p: p.stat().st_mtime, reverse=True)
