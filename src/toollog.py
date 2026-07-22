"""toollog.py —— 工具调用详情库（瘦 session 的详情侧）。

设计动机：工具结果（run_python/open_url/run_shell）动辄几千字，全量喂 LLM 会吃掉
大量上下文。所以把【完整详情】从 session 里剥离出来——session 的 ToolCall 只存 call_id，
完整 name/arguments/result 存在 ToolLog。组装上下文（session._steps_to_messages）时按
call_id 召回，并按"距当前步的距离"差异化摘要：越近越完整、越远越简略，省 token 又不丢
存在性；模型需要完整内容时用 get_tool_detail(call_id) 拉取。

距离衰减算式：limit(d) = max(FLOOR, BASE - d*STEP)，d=步距（最近一步 d=0）。
默认 BASE=1500 / STEP=15 / FLOOR=20 —— 当前步最多 1500 字、每远一步 -15、最远也保 20 字。
入参(arguments)和结果(result)各自独立按 limit 摘要。
"""
from __future__ import annotations

import json
from typing import Optional

from tools import Tool

# —— 距离衰减参数（可在 Session 里按需覆盖）——
DETAIL_BASE = 1500   # 当前步（d=0）的最大摘要字数
DETAIL_STEP = 15     # 每远一步减少的字数
DETAIL_FLOOR = 20    # 最远也至少保留的字数（保证存在性 + 可拉详情）


def detail_limit(distance: int, base: int = DETAIL_BASE,
                 step: int = DETAIL_STEP, floor: int = DETAIL_FLOOR) -> int:
    """按步距算本次工具调用的摘要上限字数。"""
    if distance <= 0:
        return base
    return max(floor, base - distance * step)


class ToolLog:
    """工具调用详情库：call_id -> {name, arguments, result, step, turn}。

    内存 dict + 随 session 存档落盘（嵌入 session.json 的 "toollog" 字段）。
    单 session 量可控（max_steps × 窗口内轮数），无需 LRU。
    """

    def __init__(self):
        self._data: dict[str, dict] = {}
        self._counter = 0

    def next_id(self) -> str:
        """生成会话内自增 id：c1 / c2 / …（load 后继续自增不撞旧 id）。"""
        self._counter += 1
        return f"c{self._counter}"

    def record(self, call_id: str, name: str, arguments: dict,
               result: str, step: Optional[int] = None,
               turn: Optional[int] = None) -> dict:
        """记录一次工具调用的完整详情。"""
        entry = {"call_id": call_id, "name": name,
                 "arguments": arguments or {}, "result": result or "",
                 "step": step, "turn": turn}
        self._data[call_id] = entry
        return entry

    def get(self, call_id: str) -> Optional[dict]:
        return self._data.get(call_id)

    def view(self, call_id: str) -> tuple:
        """召回 (name, arguments, result)；缺失返回占位（兜底防崩，正常不该发生）。"""
        e = self._data.get(call_id)
        if not e:
            return ("(详情已失效)", {}, "")
        return (e["name"], e.get("arguments", {}), e.get("result", ""))

    def __len__(self):
        return len(self._data)

    def to_list(self) -> list:
        return list(self._data.values())

    def load_list(self, items: list):
        """从存档恢复详情（先 load 已有详情，再迁移旧 ToolCall，保证 next_id 不撞）。"""
        for e in (items or []):
            cid = e.get("call_id")
            if not cid:
                continue
            self._data[cid] = e
            # 恢复计数器到已用最大值，避免继续自增时撞旧 id
            if cid.startswith("c") and cid[1:].isdigit():
                self._counter = max(self._counter, int(cid[1:]))


def make_tool_log_tools(agent) -> list:
    """工具详情拉取工具（闭包绑定 agent，经 agent.session.toollog 召回）。"""

    def _toollog():
        return getattr(getattr(agent, "session", None), "toollog", None)

    def get_tool_detail(call_id: str) -> str:
        """拉取某次工具调用的【完整】详情（工具名 / 完整入参 / 完整结果）。
        call_id 见工具结果摘要末尾的标注（如 c7）；仅在被距离衰减截断、需要完整内容时调用。"""
        tl = _toollog()
        if tl is None:
            return "[无详情库] 当前会话未启用工具详情记录。"
        e = tl.get(call_id)
        if not e:
            return (f"[无此 id] {call_id}（可能笔误，或属更早不在窗口的会话）。"
                    f"可用 list_tool_logs 看当前可拉的 id。")
        args_s = json.dumps(e.get("arguments", {}), ensure_ascii=False)
        return (f"[完整详情·{call_id}] 工具: {e['name']}\n"
                f"入参: {args_s}\n"
                f"结果({len(e.get('result', ''))}字):\n{e.get('result', '')}")

    def list_tool_logs() -> str:
        """列出当前会话所有工具调用详情的 id 清单（id/工具名/结果字数/步号），
        便于从中选 id 再用 get_tool_detail(id) 取完整内容。"""
        tl = _toollog()
        if tl is None:
            return "[无详情库]"
        items = tl.to_list()
        if not items:
            return "(尚无工具调用记录)"
        lines = [f"共 {len(items)} 条工具调用详情："]
        for e in items:
            lines.append(f"  {e['call_id']} · {e['name']} · 结果{len(e.get('result', ''))}字 · step={e.get('step')}")
        return "\n".join(lines)

    return [Tool(get_tool_detail), Tool(list_tool_logs)]
