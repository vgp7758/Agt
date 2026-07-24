"""llm_call_log.py —— LLM 调用流水（append-only JSONL，可观测性地基）。

每次 LLM 调用追加一条记录（成功 / 空响应 / 截断 / 异常 / 回退每跳 / completer 补全），
供 /stats 聚合 per-model 可靠性：calls / success / empty / truncated / errors(按类型) /
completer 次数 / tokens / 平均耗时。这是「LLM agent 可观测性」的第一手数据——debug 空响应、
回退、reasoning 回传、截断等问题时，能直接看到每次调用的 model/finish_reason/usage/耗时/报错。

文件：<name>.llm_calls.jsonl（和 events/toollog 并排）。name 就绪前 buffer 在内存，
set_path 后 flush 全量建立 + 之后纯 append（状态机同 ToolLog）。

记录字段（record）：
  ts(时间戳,time.time) / model / attempt / max_tokens / finish_reason / usage / elapsed /
  outcome(success|empty|truncated|error) / content_len / reasoning_len / tool_calls /
  msgs_count / msgs_chars / error(异常时) / completer(bool)
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional


class LLMCallLog:
    """LLM 调用流水：顺序 append-only JSONL。name 就绪前 buffer，set_path 后 flush+append。"""

    def __init__(self):
        self._records: list[dict] = []
        self._path: Optional[Path] = None   # 绑定的 jsonl 路径；None 时只 buffer

    def record(self, rec: dict) -> None:
        """追加一条调用记录；path 已绑定时同步 append 一行。"""
        self._records.append(rec)
        if self._path is not None:
            try:
                with open(self._path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            except Exception:
                pass   # 落盘失败不影响主循环（内存里仍有）

    def set_path(self, path: Path) -> None:
        """绑定 jsonl 路径。文件不存在 → flush 当前内存全量建立；存在 → 假定已 load，不重写。"""
        self._path = Path(path)
        if not self._path.exists():
            self._path.parent.mkdir(parents=True, exist_ok=True)
            try:
                with open(self._path, "w", encoding="utf-8") as f:
                    for r in self._records:
                        f.write(json.dumps(r, ensure_ascii=False) + "\n")
            except Exception:
                pass

    def load_from_jsonl(self, path: Path) -> None:
        """流式读 jsonl 全部记录进内存（resume 用）。随后调用方应 set_path 同一路径。"""
        self._path = Path(path)
        self._records = []
        if not self._path.exists():
            return
        try:
            with open(self._path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            self._records.append(json.loads(line))
                        except Exception:
                            pass
        except Exception:
            pass

    def all_records(self) -> list:
        return list(self._records)


def load_all_calls(sessions_dir) -> list:
    """聚合某 repo 下所有 session 的 llm_calls（供 /stats all 跨 session 看整体可靠性）。"""
    out = []
    for p in Path(sessions_dir).glob("*.llm_calls.jsonl"):
        try:
            with open(p, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            out.append(json.loads(line))
                        except Exception:
                            pass
        except Exception:
            pass
    return out


def aggregate_calls(records: list) -> dict:
    """聚合 per-model 可靠性统计。
    返回 {model: {calls, success, empty, truncated, errors{type:cnt}, completer,
                  tokens, avg_latency}}（success_rate/empty_rate 等比率由展示层算）。"""
    stats: dict = {}
    for r in records or []:
        m = r.get("model") or "?"
        s = stats.setdefault(m, {
            "calls": 0, "success": 0, "empty": 0, "truncated": 0, "errors": {},
            "completer": 0, "tokens": 0, "latency_sum": 0.0, "latency_n": 0,
        })
        s["calls"] += 1
        outcome = r.get("outcome")
        if outcome == "success":
            s["success"] += 1
        elif outcome == "empty":
            s["empty"] += 1
        elif outcome == "truncated":
            s["truncated"] += 1
        elif outcome == "error":
            etype = ((r.get("error") or "").split(":")[0]).strip() or "error"
            s["errors"][etype] = s["errors"].get(etype, 0) + 1
        if r.get("completer"):
            s["completer"] += 1
        u = r.get("usage") or {}
        s["tokens"] += (u.get("total_tokens") or 0)
        el = r.get("elapsed")
        if isinstance(el, (int, float)) and el >= 0:
            s["latency_sum"] += el
            s["latency_n"] += 1
    for s in stats.values():
        s["avg_latency"] = round(s["latency_sum"] / s["latency_n"], 2) if s["latency_n"] else 0.0
        del s["latency_sum"], s["latency_n"]
    return stats


def format_stats(records: list, title: str = "LLM 调用统计") -> str:
    """把聚合结果格式化成可读文本（CLI /stats 与 webui 共用）。"""
    stats = aggregate_calls(records)
    if not stats:
        return f"{title}：（暂无 LLM 调用记录）"
    lines = [f"{title}（{sum(s['calls'] for s in stats.values())} 次调用，{len(records)} 条记录）："]
    for m, s in stats.items():
        rate = (f"{s['success']*100//s['calls']}%" if s["calls"] else "—")
        err = ("；错误 " + ", ".join(f"{k}:{v}" for k, v in s["errors"].items())) if s["errors"] else ""
        comp = f"；completer 补全 {s['completer']}" if s["completer"] else ""
        lines.append(
            f"  ▣ {m}: {s['calls']} 次 | 成功 {s['success']}({rate})"
            f" | 空 {s['empty']} | 截断 {s['truncated']}{err}{comp}"
            f" | {s['tokens']} tokens | 均{s['avg_latency']}s"
        )
    return "\n".join(lines)
