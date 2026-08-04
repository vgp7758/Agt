"""mdrender.py —— LLM 回答文本的 CLI 渲染（表格→ASCII 框线表，代码块→带边框灰条）。

零依赖（仅标准库 unicodedata / re）。被 agent.py 的 _print_event 与 session.py 的
_format_turn_full（/recall）共用，避免两者互相 import 造成循环依赖。

渲染范围（与 WebUI renderAnswer 对齐，只做"表格 + 代码块"，不做完整 markdown）：
- fenced code block（``` 围栏）→ 每行加灰色左边框前缀 ` │ `；
- markdown 表格（连续 |...| 行，且第 2 行为分隔行）→ box-drawing 框线表，按 CJK 显示宽度对齐；
- 其余文本原样保留；行内 `code` 标灰去反引号。

逐行状态机的判定顺序固定为「先 ``` 围栏，再 |...| 表格行」——保证代码块内的 `|` 不被表格吞掉。
"""
from __future__ import annotations

import re
import unicodedata

GRAY, RESET, GREEN = "\033[90m", "\033[0m", "\033[32m"


# ===================== 显示宽度（CJK 双宽） =====================

def disp_width(s: str) -> int:
    """字符串在等宽终端里的显示列数：CJK/全角/Ambiguous 算 2，其余 1；
    跳过 Variation Selectors / ZWJ / 组合符（宽 0）。零依赖近似，够用。"""
    w = 0
    for ch in s:
        o = ord(ch)
        if 0xFE00 <= o <= 0xFE0F:      # Variation Selectors（VS1-VS16），宽 0
            continue
        if o == 0x200D:                # ZWJ（emoji 组合序列），宽 0
            continue
        if unicodedata.combining(ch):  # 通用组合符（combining 类），宽 0
            continue
        w += 2 if unicodedata.east_asian_width(ch) in "WFA" else 1
    return w


def pad_right(s: str, width: int) -> str:
    """按显示宽度右侧补空格到 width（不足不截）。"""
    pad = width - disp_width(s)
    return s + (" " * pad if pad > 0 else "")


# ===================== cell 拆分（\| 转义 + 反引号保护） =====================

def split_cells(row: str) -> list:
    """把一行表格内容（首尾的 | 已剥掉）拆成单元格列表。

    1) 先把成对反引号段（`…`）整段替换成 NUL 占位符保护起来，code span 里的 | 不当分隔；
    2) 按非转义 | 切分（`(?<!\\)\\|`，不切 \\|）；
    3) 每 cell 还原 \\| → |，再还原反引号占位符；
    4) strip。
    """
    placeholders = []

    def _hide(m):
        placeholders.append(m.group(0))
        return f"\x00{len(placeholders) - 1}\x00"

    row = re.sub(r"`[^`]*`", _hide, row)
    parts = re.split(r"(?<!\\)\|", row)
    cells = []
    for p in parts:
        p = p.replace("\\|", "|")
        p = re.sub(r"\x00(\d+)\x00", lambda m: placeholders[int(m.group(1))], p)
        cells.append(p.strip())
    return cells


_TABLE_ROW_RE = re.compile(r"^\s*\|.*\|\s*$")
_SEP_CELL_RE = re.compile(r"^:?-+:?$")


def is_table_row(line: str) -> bool:
    return bool(_TABLE_ROW_RE.match(line))


def _is_separator(cells: list) -> bool:
    """GFM 分隔行：每个 cell 形如 :-- / --: / :--: / ---（至少 1 个 -）。"""
    return bool(cells) and all(_SEP_CELL_RE.match(c.replace(" ", "")) for c in cells)


def _inline_gray(s: str) -> str:
    """行内 `code` → 灰色（去反引号）。仅作用于显示，不影响 disp_width 计算。"""
    return re.sub(r"`([^`]*)`", lambda m: f"{GRAY}{m.group(1)}{RESET}", s)


# ===================== ASCII 框线表 =====================

def ascii_table(header: list, rows: list) -> str:
    """把表头 + 数据行画成 box-drawing 框线表。列数按 header 归一（短补空、多出拼末格）；
    列宽 = 该列最大显示宽度；表头 GREEN，框线 GRAY，数据行 cell 里的 inline code 标灰。"""
    ncol = max(1, len(header))

    def norm(r):
        r = ["" if c is None else str(c) for c in r]
        if len(r) < ncol:
            r += [""] * (ncol - len(r))
        elif len(r) > ncol:
            r = r[:ncol - 1] + [" ".join(r[ncol - 1:])]   # 多出的格子拼进末格
        return r

    header = norm(header)
    rows = [norm(r) for r in rows]

    widths = []
    for i in range(ncol):
        w = disp_width(header[i])
        for r in rows:
            w = max(w, disp_width(r[i]))
        widths.append(w)

    def border(left, mid, right):
        return GRAY + left + mid.join("─" * (w + 2) for w in widths) + right + RESET

    def render_row(cells, is_header=False):
        parts = []
        for i in range(ncol):
            c = cells[i]
            vis = c if is_header else _inline_gray(c)   # 表头不单独标灰（避免与 GREEN 冲突）
            pad = widths[i] - disp_width(c)             # pad 按原始文本算，ANSI 不计入
            parts.append(" " + vis + " " * (pad if pad > 0 else 0) + " ")
        line = "│" + "│".join(parts) + "│"
        return GREEN + line + RESET if is_header else line

    out = [border("┌", "┬", "┐"), render_row(header, is_header=True)]
    if rows:
        out.append(border("├", "┼", "┤"))
        out.extend(render_row(r) for r in rows)
    out.append(border("└", "┴", "┘"))
    return "\n".join(out)


# ===================== 代码块 =====================

def render_code_block(code_lines: list, lang: str = "") -> str:
    """fenced code block：每行灰色 ` │ ` 左边框（首行可挂 [lang] 小标签）。靠左边框 + 颜色
    标识，不画 ``` 头尾——避免反引号计数歧义，也更贴近终端里代码块的常见呈现。"""
    lines = []
    if lang:
        lines.append(f"{GRAY}  [{lang}]{RESET}")
    lines.extend(f"{GRAY} │ {ln}{RESET}" for ln in code_lines)
    return "\n".join(lines)


# ===================== 顶层：回答整段渲染 =====================

def _try_render_table(tbl_lines: list):
    """连续的表格行 → 合法则返回 ascii_table 字符串，否则返回 None（交回普通文本）。"""
    if len(tbl_lines) < 2:
        return None

    def cells_of(row):
        inner = row.strip()
        if inner.startswith("|") and inner.endswith("|"):
            inner = inner[1:-1]
        return split_cells(inner)

    header = cells_of(tbl_lines[0])
    sep = cells_of(tbl_lines[1])
    if not _is_separator(sep):
        return None
    data = [cells_of(r) for r in tbl_lines[2:]]
    return ascii_table(header, data)


def render_cli(text: str) -> str:
    """LLM 回答整段 → CLI 可读字符串。逐行状态机：先 ``` 围栏，再 |表格|，其余原样。"""
    lines = (text or "").split("\n")
    out, buf, i, n = [], [], 0, len(lines)

    def flush_plain():
        if buf:
            out.append("\n".join(buf))
            buf.clear()

    while i < n:
        line = lines[i]
        stripped = line.strip()
        # 1) fenced code（围栏优先级最高）
        if stripped.startswith("```"):
            flush_plain()
            lang = stripped[3:].strip()
            i += 1
            code_lines = []
            while i < n and not lines[i].strip().startswith("```"):
                code_lines.append(lines[i])
                i += 1
            i += 1  # 跳过闭合围栏（若存在）
            out.append(render_code_block(code_lines, lang))
            continue
        # 2) table（连续表格行）
        if is_table_row(line):
            tbl = []
            while i < n and is_table_row(lines[i]):
                tbl.append(lines[i])
                i += 1
            rendered = _try_render_table(tbl)
            if rendered is not None:
                flush_plain()
                out.append(rendered)
            else:
                buf.extend(_inline_gray(x) for x in tbl)   # 非法表→当普通文本
            continue
        # 3) 普通行
        buf.append(_inline_gray(line))
        i += 1

    flush_plain()
    return "\n".join(out)
