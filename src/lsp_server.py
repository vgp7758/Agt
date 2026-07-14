"""lsp_server.py —— Python 代码语义导航 MCP server（基于 jedi，v1）。

工具（全部相对 WORKSPACE 路径）：
  py_def(file, line, col)   → 跳转到符号定义（含文档摘要）
  py_ref(file, line, col)   → 查找所有引用
  py_syms(file)             → 文件符号/结构概览

比 grep 更"懂代码"——grep 搜字符串，这几个搜语义（谁定义的、谁在用、文件有什么）。
Agent 改 Python 代码时用它替代盲 grep。
"""
import logging
import re

import jedi
from mcp.server.fastmcp import FastMCP

from real_tools import WORKSPACE

logging.getLogger("mcp").setLevel(logging.WARNING)
mcp = FastMCP("python-lsp")


# jedi 用 0-based 列号，我们暴露给 Agent 用 1-based（与编辑器一致，更直观）。
# 返回的文件路径和行号也是 1-based。

def _script(file: str, line: int, col_1: int):
    """构建 jedi Script 对象。line/col_1 均为 1-based。
    file 可以是相对路径（相对于 WORKSPACE）或绝对路径。"""
    from pathlib import Path as _P
    p = _P(file)
    if not p.is_absolute():
        p = WORKSPACE / file
    if not p.exists():
        return None, f"[文件不存在] {file}"
    code = p.read_text(encoding="utf-8", errors="ignore")
    try:
        s = jedi.Script(code, path=str(p))
    except Exception:
        # 回退：不带 path（有时文件名解析问题）
        s = jedi.Script(code)
    return s, ""


def _rel_path(abs_path: str) -> str:
    """把绝对路径转成相对 WORKSPACE 的路径；转不成就用绝对路径。"""
    try:
        return str((__import__('pathlib').Path(abs_path)).relative_to(WORKSPACE)).replace("\\", "/")
    except Exception:
        return abs_path


def _parse_ref_line(ref) -> str:
    """单个引用/定义为一行文本。"""
    f = _rel_path(ref.module_path)
    # jedi 的行是 1-based（int），列是 0-based——转为 1-based
    col = ref.column + 1 if ref.column is not None else 1
    ln = ref.line if isinstance(ref.line, int) else 0
    desc = getattr(ref, 'description', '') or ''
    full = getattr(ref, 'full_name', '') or ref.name or ''
    return f"{f}:{ln}:{col} | {desc} {full}"


@mcp.tool()
def py_def(file: str, line: int, col: int = 1) -> str:
    """跳转到符号定义（对光标位置的标识符）。返回 file:line:col + 文档摘要。
    file: 相对路径；line: 行号(1-based)；col: 列号(1-based，默认行首)。"""
    s, err = _script(file, line, col - 1)
    if err:
        return err
    defs = s.goto(line, col - 1)
    if not defs:
        return "(未找到定义)"
    out = []
    for d in defs[:10]:
        out.append(_parse_ref_line(d))
        doc = d.docstring()
        if doc:
            out.append(f"  📄 {doc.strip()[:120]}")
    return "\n".join(out)


@mcp.tool()
def py_ref(file: str, line: int, col: int = 1) -> str:
    """查找符号的所有引用位置。返回 file:line:col + 所在行代码片段。"""
    s, err = _script(file, line, col - 1)
    if err:
        return err
    refs = s.get_references(line, col - 1)
    if not refs:
        return "(未找到引用)"
    return "\n".join(_parse_ref_line(r) for r in refs[:30]) + (
        "\n…(仅显示前 30 条)" if len(refs) > 30 else "")


@mcp.tool()
def py_syms(file: str) -> str:
    """列出 Python 文件的所有符号(函数/类/变量/import)，快速了解文件结构。"""
    s, err = _script(file, 1, 0)
    if err:
        return err
    names = [n for n in s.get_names(all_scopes=True) if n.type != 'keyword']
    if not names:
        return "(无符号)"
    out = []
    for n in names:
        out.append(f"  {n.type:<14} {n.name:<32} line {n.line}")
    return "\n".join(out)


if __name__ == "__main__":
    mcp.run()
