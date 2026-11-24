"""toollog.py —— 工具调用详情库（append-only JSONL 落盘）。

设计：ToolCall 只存 call_id，完整 name/arguments/result 存本库。组装上下文（session.
_steps_to_messages）时按 call_id 召回 + 按步距衰减摘要。落盘用 JSONL 流式 append——
record 一行，O(1)，不再随 session 全量重写。

文件：<sessions_root>/toollog.jsonl（repo 共享一份）。record 在 session name 就绪前 buffer
在内存；set_path 后若文件不存在则 flush 内存全量建立，之后纯 append（buffer→flush→append
状态机，和 log.py 的 SessionLogHandler 同思路）。

进程级共享（内存治理 2026-11-24）：同进程同 sessions_root 的所有 ToolLog 实例共享一份
_data（_ToolLogStore 单例，按 sessions_root 键控）——旧实现每个 Session 各自整表 load
共享 jsonl（数十 MB、数万条）成 N 份全量副本，现在 N 份→1 份。call_id 自带会话前缀
（<sid>-N）全局唯一，counter 按前缀分区存 store.counters；record/append 持 store 锁
（多 session 线程并发 append 同一 jsonl 不再交错）。公开接口签名全部不变。

距离衰减：limit(d)=max(FLOOR, BASE-d*STEP)，d=步距（最近步 d=0）；入参/结果各自按 limit 摘。
"""
from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Optional

from tools import Tool

# —— 距离衰减参数（模块级变量，运行时可 set_detail_params 改）——
DETAIL_BASE = 1500   # 当前步（d=0）的最大摘要字数
DETAIL_STEP = 15     # 每远一步减少的字数
DETAIL_FLOOR = 20    # 最远也至少保留的字数


def detail_limit(distance: int, base: int = None,
                 step: int = None, floor: int = DETAIL_FLOOR) -> int:
    """按步距算本次工具调用的摘要上限字数。base/step 留空时动态读模块变量（运行时可改）。"""
    if base is None:
        base = DETAIL_BASE
    if step is None:
        step = DETAIL_STEP
    if distance <= 0:
        return base
    return max(floor, base - distance * step)


def set_detail_params(base: int = None, step: int = None):
    """运行时设置步距衰减参数（base=当前步最大摘要字数, step=每远一步减少字数）。"""
    global DETAIL_BASE, DETAIL_STEP
    if base is not None:
        DETAIL_BASE = base
    if step is not None:
        DETAIL_STEP = step


# ========== 进程级共享 store（按 sessions_root 键控） ==========

class _ToolLogStore:
    """同进程同 sessions_root 的 ToolLog 共享一份数据：_data + 落盘路径 + 各前缀 counter。

    lock 保护 data/counters/path 读写与 jsonl append——多 session 线程并发 record 时
    行序不交错（修复旧实现多实例各自 open append 同一文件的潜在交错）。"""
    __slots__ = ("data", "path", "counters", "lock", "loaded")

    def __init__(self):
        self.data: dict[str, dict] = {}       # call_id -> entry（全 repo 共享）
        self.path: Optional[Path] = None      # 绑定的共享 jsonl 路径
        self.counters: dict[str, int] = {}    # prefix（可为 ""）-> 已用最大序号 N
        self.lock = threading.RLock()
        self.loaded = False                   # jsonl 是否已整表读入（同 store 只读一次）


_STORES: dict[str, "_ToolLogStore"] = {}
_STORES_GUARD = threading.Lock()


def _store_key(sessions_root) -> str:
    try:
        return str(Path(sessions_root).resolve())
    except Exception:
        return str(sessions_root)


def store_for(sessions_root) -> _ToolLogStore:
    """取（或建）sessions_root 对应的进程级共享 store。"""
    k = _store_key(sessions_root)
    with _STORES_GUARD:
        st = _STORES.get(k)
        if st is None:
            st = _ToolLogStore()
            _STORES[k] = st
        return st


def _split_call_id(call_id: str) -> tuple:
    """把 call_id 拆成 (prefix, N)；不匹配返回 ("", -1)。
    识别形态：<sid>-N（a-1024 / a-coder-3，尾段纯数字）与旧无前缀 cN；分支锚点时代
    m1000-c1 尾段非数字 → ("", -1)（不顶 counter，记录本身仍可 get 召回）。"""
    cid = call_id or ""
    if "-" in cid:
        head, tail = cid.rsplit("-", 1)
        if head and tail.isdigit():
            return head, int(tail)
        return "", -1
    if cid.startswith("c") and cid[1:].isdigit():
        return "", int(cid[1:])
    return "", -1


class ToolLog:
    """工具调用详情库：call_id -> {name, arguments, result, step, turn}。

    进程级共享 store + JSONL append 落盘。实例状态机：
      未绑定（buffer）：record 只进本实例 _buffer（session name/sessions_root 未就绪）
      已绑定 store：record 进共享 _data + append 一行；set_path 时文件不存在则 flush 全量建立
    """

    def __init__(self, prefix: str = ""):
        # session 前缀（用户提案 2026-10-09 二轮定稿）：call_id = "<session_id>-N"（如 a-1024 /
        # b-35）——session_id 就是会话目录名（字母 id），归属从 id 直读、跨 session 物理不撞。
        # 一个 repo 共用一份 toollog.jsonl（sessions 根下），各 session 只 append + 只数自己的 N。
        self._prefix = (prefix or "").strip()
        self._store: Optional[_ToolLogStore] = None   # 绑定后指向进程级共享 store
        self._buffer: dict[str, dict] = {}            # 未绑定时的内存缓冲（bind/set_path 时并入）
        self._local_counter = 0                        # 未绑定态的私有 counter（绑定后弃用）

    # ---- 进程级共享绑定 ----

    @classmethod
    def shared(cls, sessions_root, prefix: str = "") -> "ToolLog":
        """取绑定进程级共享 store 的 ToolLog：同 sessions_root 的所有实例共享一份数据/
        落盘/counter 分区（N 份全量副本 → 1 份）。"""
        tl = cls(prefix=prefix)
        tl.bind(sessions_root)
        return tl

    def bind(self, sessions_root, prefix: str = "") -> list:
        """绑定 sessions_root 的共享 store（幂等）；buffer 态积累的记录并入 store
        （counter 一并按记录自身前缀恢复）。prefix 非空时校准本实例前缀。
        返回并入的 buffer entries（供 set_path 对已存在文件补 append）。"""
        st = store_for(sessions_root)
        with st.lock:
            merged = list(self._buffer.values())
            for cid, e in self._buffer.items():
                st.data[cid] = e
                pfx, n = _split_call_id(cid)
                if n > 0:
                    st.counters[pfx] = max(st.counters.get(pfx, 0), n)
            self._buffer = {}
            self._store = st
            if prefix:
                self._prefix = (prefix or "").strip()
            return merged

    @property
    def _data(self) -> dict:
        """兼容旧直接访问：绑定后 = 共享 store 数据；未绑定 = 本实例 buffer。"""
        return self._store.data if self._store is not None else self._buffer

    @property
    def _path(self) -> Optional[Path]:
        """兼容旧直接访问（server.py dashboard 回填读它）：当前绑定 store 的落盘路径。"""
        return self._store.path if self._store is not None else None

    def next_id(self) -> str:
        """生成调用 id：<session_id>-N（如 a-1024）；无前缀（旧路径/未绑定）退 cN 兼容。
        counter 按前缀分区存共享 store——同前缀多实例连续编号，跨前缀互不顶号。"""
        if self._store is not None:
            with self._store.lock:
                n = self._store.counters.get(self._prefix, 0) + 1
                self._store.counters[self._prefix] = n
        else:
            self._local_counter += 1
            n = self._local_counter
        return f"{self._prefix}-{n}" if self._prefix else f"c{n}"

    def _own_counter_from(self, call_id: str) -> int:
        """提取本前缀的序号（不匹配返回 -1）：共享 toollog 里别的 session 的记录不顶本 counter。
        兼容旧存档 id 形态：c1/c2…（2026-10-09 前的主线）、m1000-c1…（分支锚点前缀时代）。"""
        cid = call_id or ""
        if self._prefix:
            head = self._prefix + "-"
            tail = cid[len(head):]
            if cid.startswith(head) and tail.isdigit():
                return int(tail)
            return -1
        return int(cid[1:]) if cid.startswith("c") and cid[1:].isdigit() else -1

    def record(self, call_id: str, name: str, arguments: dict,
               result: str, step: Optional[int] = None,
               turn: Optional[int] = None) -> dict:
        """记录一次工具调用的完整详情；已绑定且路径就绪时同步 append 一行到 jsonl。"""
        entry = {"call_id": call_id, "name": name,
                 "arguments": arguments or {}, "result": result or "",
                 "step": step, "turn": turn}
        st = self._store
        if st is not None:
            with st.lock:
                st.data[call_id] = entry
                if st.path is not None:
                    self._append_line(entry)
        else:
            self._buffer[call_id] = entry
        return entry

    def get(self, call_id: str) -> Optional[dict]:
        return self._data.get(call_id)

    def view(self, call_id: str) -> tuple:
        """召回 (name, arguments, result)；缺失返回占位（兜底防崩）。"""
        e = self._data.get(call_id)
        if not e:
            return ("(详情已失效)", {}, "")
        return (e["name"], e.get("arguments", {}), e.get("result", ""))

    def search(self, keywords, max_hits=20) -> list:
        """按关键字在 name/arguments/result 里子串匹配初筛（Agentic RAG 第一阶段，无 LLM）。
        返回 [(entry, [命中的关键字])]，最多 max_hits 条。（共享 store 含其它 session 记录，
        与旧「整表 load 进各实例」的可见范围一致；按会话过滤由调用方按 call_id 前缀做。）"""
        kws = [k for k in (keywords or []) if k]
        if not kws:
            return []
        hits = []
        for e in self._data.values():
            text = (e["name"] + " " + json.dumps(e.get("arguments", {}), ensure_ascii=False)
                    + " " + (e.get("result", "") or ""))
            matched = [k for k in kws if k in text]
            if matched:
                hits.append((e, matched))
        return hits[:max_hits]

    def __len__(self):
        return len(self._data)

    def to_list(self) -> list:
        return list(self._data.values())

    # ========== JSONL 落盘 ==========
    def set_path(self, path: Path):
        """绑定 jsonl 路径（= <sessions_root>/toollog.jsonl，parent 即 sessions 根 → 定共享 store）。
        文件不存在 → flush 建立；存在 → 假定已 load，不重写（buffer 态积累的记录补 append）。
        （2026-10-09 二轮定稿：一个 repo 共用一份 toollog.jsonl（sessions 根下），
        各 session 的 record 都 append 进去——不再有分支物化/合载/过滤那套。）"""
        p = Path(path)
        merged = self.bind(p.parent)
        st = self._store
        with st.lock:
            st.path = p
            if not p.exists():
                try:
                    p.parent.mkdir(parents=True, exist_ok=True)
                except Exception:
                    pass
                self._flush_all()
            elif merged:
                for e in merged:      # 文件已由同 store 其它实例建立：buffer 记录补 append
                    self._append_line(e)

    def _append_line(self, entry: dict):
        """append 一行（须持 store.lock，调用方保证）。落盘失败不影响主循环（内存里仍有）。"""
        st = self._store
        path = st.path if st is not None else None
        if path is None:
            return
        try:
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception:
            pass

    def _flush_all(self):
        """把共享 store 的 entry 全量写入文件（建立或重建）。须持 store.lock。
        文件不存在（新 repo 首次）时 store 里只有本 session 已并入的记录，直接写即可。"""
        st = self._store
        path = st.path if st is not None else None
        if path is None:
            return
        try:
            with open(path, "w", encoding="utf-8") as f:
                for e in st.data.values():
                    f.write(json.dumps(e, ensure_ascii=False) + "\n")
        except Exception:
            pass

    def load_from_jsonl(self, path: Path):
        """流式读 jsonl 全部 entry 进共享 store（恢复各前缀 counter）。同 store 只整表读
        一次——第二个及以后的 session 实例直接复用（内存治理：不再每实例整表读一遍）。
        假定调用方随后会 set_path 同一路径。"""
        p = Path(path)
        self.bind(p.parent)           # buffer 先并入（旧实现 load 不清 _data，旧记录保留）
        st = self._store
        with st.lock:
            st.path = p
            if st.loaded:
                return
            if not p.exists():
                st.loaded = True
                return
            try:
                with open(p, encoding="utf-8") as f:
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
                        st.data[cid] = e
                        pfx, n = _split_call_id(cid)
                        if n > 0:
                            st.counters[pfx] = max(st.counters.get(pfx, 0), n)
                st.loaded = True
            except Exception:
                pass

    def load_list(self, items: list):
        """从 0.7.4 的嵌入字段恢复（兼容旧存档一次性迁移用）。
        未绑定 store 时进 buffer（set_path 时并入）；counter 按本实例前缀恢复（与旧实现一致）。"""
        for e in (items or []):
            cid = e.get("call_id")
            if not cid:
                continue
            self._buffer[cid] = e
            n = self._own_counter_from(cid)
            if n > 0:
                self._local_counter = max(self._local_counter, n)


def make_tool_log_tools(agent) -> list:
    """工具详情拉取工具（闭包绑定 agent，经 agent.session.toollog 召回）。"""

    def _toollog():
        return getattr(getattr(agent, "session", None), "toollog", None)

    def get_tool_detail(call_id: str) -> str:
        """拉取工具调用的【完整】详情（工具名 / 完整入参 / 完整结果）。
        call_id 可传单个（如 a-1024）或多个（逗号/空格分隔，如 a-1024,a-1031），一次返回多条；
        id 形态 = <session_id>-N（见历史工具结果摘要末尾标注）；不确定有哪些时先 list_tool_logs。"""
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
        # 共享 toollog（一个 repo 一份）里含其它 session 的记录——只列本 session 前缀的
        _pfx = getattr(tl, "_prefix", "") or ""
        items = [e for e in tl.to_list()
                 if not _pfx or str(e.get("call_id") or "").startswith(_pfx + "-")]
        if not items:
            return "(尚无工具调用记录)"
        lines = [f"共 {len(items)} 条工具调用详情："]
        for e in items:
            lines.append(f"  {e['call_id']} · {e['name']} · 结果{len(e.get('result', ''))}字 · step={e.get('step')}")
        return "\n".join(lines)

    def call_again(call_id: str) -> str:
        """重复执行某次历史工具调用：同一工具、同一组参数原样重跑（用户提案 2026-10-11）。
        适用：再跑一次刚才的检索/查询拿最新结果、重试时好时坏的外部调用、以相同输入验证修复效果。
        call_id 见历史工具结果旁的 id 标注（如 a-1024）或你自己刚发起的调用；不确定时先 list_tool_logs。
        本次结果作为 call_again 自己的返回与记录（get_tool_detail 可查）；走标准执行管线（审批/截断照常）。
        注：远程执行的旧调用重跑为本地（remote_instance_id 不在存档参数里，需要远程请直接调原工具）。"""
        tl = _toollog()
        e = tl.get(call_id) if tl else None
        if not e:
            return f"[无此 id] {call_id}（笔误或不在详情库；list_tool_logs 查清单）"
        name = str(e.get("name") or "")
        args = dict(e.get("arguments") or {})
        if not name:
            return f"[记录缺损] {call_id} 无工具名"
        if name == "call_again":
            return "[不支持] 重复 call_again 自身——请用原工具的 call_id"
        if not hasattr(agent, "_exec_tool"):
            return "[不可用] 执行入口未就绪"
        try:
            result = agent._exec_tool(name, args)
            return f"[重跑 {call_id} → {name}（参数与原次一致）]\n{result}"
        except Exception as ex:
            return f"[重跑失败] {call_id} → {name}：{type(ex).__name__}: {ex}"

    return [Tool(get_tool_detail), Tool(list_tool_logs),
            Tool(call_again, param_descriptions={
                "call_id": "要重跑的历史调用 id（如 a-1024）——见历史工具结果旁的 id 标注或你自己刚发起的调用；不确定时先 list_tool_logs",
            })]
