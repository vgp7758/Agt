"""toollog.py —— 工具调用详情库（append-only JSONL 落盘）。

设计：ToolCall 只存 call_id，完整 name/arguments/result 存本库。组装上下文（session.
_steps_to_messages）时按 call_id 召回 + 按步距衰减摘要。落盘用 JSONL 流式 append——
record 一行，O(1)，不再随 session 全量重写。

文件：<name>.toollog.jsonl（和 <name>.json 并排）。record 在 session name 就绪前 buffer
在内存；set_path 后若文件不存在则 flush 内存全量建立，之后纯 append（buffer→flush→append
状态机，和 log.py 的 SessionLogHandler 同思路）。

距离衰减：limit(d)=max(FLOOR, BASE-d*STEP)，d=步距（最近步 d=0）；入参/结果各自按 limit 摘。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from tools import Tool

# —— 距离衰减参数 ——
DETAIL_BASE = 1500   # 当前步（d=0）的最大摘要字数
DETAIL_STEP = 15     # 每远一步减少的字数
DETAIL_FLOOR = 20    # 最远也至少保留的字数


def detail_limit(distance: int, base: int = DETAIL_BASE,
                 step: int = DETAIL_STEP, floor: int = DETAIL_FLOOR) -> int:
    """按步距算本次工具调用的摘要上限字数。"""
    if distance <= 0:
        return base
    return max(floor, base - distance * step)


class ToolLog:
    """工具调用详情库：call_id -> {name, arguments, result, step, turn}。

    内存 dict + JSONL append 落盘。状态机：
      _path=None（buffer）：record 只进内存（session name 未就绪）
      _path 已绑定：record 进内存 + append 一行；set_path 时文件不存在则先 flush 全量建立
    """

    def __init__(self):
        self._data: dict[str, dict] = {}
        self._counter = 0
        self._path: Optional[Path] = None   # 绑定的 jsonl 路径；None 时只 buffer

    def next_id(self) -> str:
        """生成会话内自增 id：c1 / c2 / …（load 后继续自增不撞旧 id）。"""
        self._counter += 1
        return f"c{self._counter}"

    def record(self, call_id: str, name: str, arguments: dict,
               result: str, step: Optional[int] = None,
               turn: Optional[int] = None) -> dict:
        """记录一次工具调用的完整详情；path 已绑定时同步 append 一行到 jsonl。"""
        entry = {"call_id": call_id, "name": name,
                 "arguments": arguments or {}, "result": result or "",
                 "step": step, "turn": turn}
        self._data[call_id] = entry
        if self._path is not None:
            self._append_line(entry)
        return entry

    def get(self, call_id: str) -> Optional[dict]:
        return self._data.get(call_id)

    def view(self, call_id: str) -> tuple:
        """召回 (name, arguments, result)；缺失返回占位（兜底防崩）。"""
        e = self._data.get(call_id)
        if not e:
            return ("(详情已失效)", {}, "")
        return (e["name"], e.get("arguments", {}), e.get("result", ""))

    def __len__(self):
        return len(self._data)

    def to_list(self) -> list:
        return list(self._data.values())

    # ========== JSONL 落盘 ==========
    def set_path(self, path: Path):
        """绑定 jsonl 路径。文件不存在 → flush 当前内存全量建立；存在 → 假定已 load，不重写。"""
        self._path = Path(path)
        if not self._path.exists():
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._flush_all()

    def _append_line(self, entry: dict):
        try:
            with open(self._path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception:
            pass   # 落盘失败不影响主循环（内存里仍有）

    def _flush_all(self):
        """把内存全部 entry 写入文件（建立或重建）。"""
        try:
            with open(self._path, "w", encoding="utf-8") as f:
                for e in self._data.values():
                    f.write(json.dumps(e, ensure_ascii=False) + "\n")
        except Exception:
            pass

    def load_from_jsonl(self, path: Path):
        """流式读 jsonl 全部 entry 进内存（恢复 counter）。假定调用方随后会 set_path 同一路径。"""
        self._path = Path(path)
        if not self._path.exists():
            return
        try:
            with open(self._path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        e = json.loads(line)
                    except Exception:
                        continue
                    cid = e.get("call_id")
                    if not cid:
                        continue
                    self._data[cid] = e
                    if cid.startswith("c") and cid[1:].isdigit():
                        self._counter = max(self._counter, int(cid[1:]))
        except Exception:
            pass

    def load_list(self, items: list):
        """从 0.7.4 的嵌入字段恢复（兼容旧存档一次性迁移用）。"""
        for e in (items or []):
            cid = e.get("call_id")
            if not cid:
                continue
            self._data[cid] = e
            if cid.startswith("c") and cid[1:].isdigit():
                self._counter = max(self._counter, int(cid[1:]))


def make_tool_log_tools(agent) -> list:
    """工具详情拉取工具（闭包绑定 agent，经 agent.session.toollog 召回）。"""

    def _toollog():
        return getattr(getattr(agent, "session", None), "toollog", None)

    def get_tool_detail(call_id: str) -> str:
        """拉取工具调用的【完整】详情（工具名 / 完整入参 / 完整结果）。
        call_id 可传单个(如 c7)或多个(逗号/空格分隔，如 c7,c8,c9)，一次返回多条；
        id 见历史工具结果摘要末尾的标注，不确定有哪些时先 list_tool_logs 看清单。"""
        tl = _toollog()
        if tl is None:
            return "[无详情库] 当前会话未启用工具详情记录。"
        ids = [x.strip() for x in str(call_id).replace("，", ",").replace(" ", ",").split(",") if x.strip()]
        if not ids:
            return "[请提供 call_id] 见历史工具结果摘要末尾标注，或先 list_tool_logs 看清单。"
        blocks = []
        for cid in ids:
            e = tl.get(cid)
            if not e:
                blocks.append(f"[无此 id] {cid}（可能笔误，或属更早不在窗口的会话）")
                continue
            args_s = json.dumps(e.get("arguments", {}), ensure_ascii=False)
            blocks.append(f"[完整详情·{cid}] 工具: {e['name']}\n"
                          f"入参: {args_s}\n"
                          f"结果({len(e.get('result', ''))}字):\n{e.get('result', '')}")
        return "\n\n---\n".join(blocks)

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
