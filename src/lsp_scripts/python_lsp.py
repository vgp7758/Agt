"""python_lsp.py —— Python 代码语义导航 MCP server（基于 jedi）。

工具（路径相对启动 cwd）：
  py_def(file, line, col)   → 跳转到符号定义（含文档摘要）
  py_ref(file, line, col)   → 查找所有引用
  py_syms(file)             → 文件符号/结构概览

比 grep 更"懂代码"——语义查找定义/引用/结构。
作为 ensure_lsp 装配的语言脚本之一，由 agt-agent 的 lsp_manager 分发到 ~/.agt/lsp/。
WORKSPACE = 启动此进程时的 cwd（连接时 cfg.cwd 传入 = agent 的 cwd）。
"""
import ast
import logging
from pathlib import Path

import jedi
from mcp.server.fastmcp import FastMCP

logging.getLogger("mcp").setLevel(logging.WARNING)
mcp = FastMCP("python-lsp")

WORKSPACE = Path.cwd()


def _script(file: str, line: int, col_1: int):
    p = Path(file)
    if not p.is_absolute():
        p = WORKSPACE / file
    if not p.exists():
        return None, f"[文件不存在] {file}"
    code = p.read_text(encoding="utf-8", errors="ignore")
    try:
        s = jedi.Script(code, path=str(p))
    except Exception:
        s = jedi.Script(code)
    return s, ""


def _rel_path(abs_path: str) -> str:
    try:
        return str(Path(abs_path).relative_to(WORKSPACE)).replace("\\", "/")
    except Exception:
        return abs_path


def _parse_ref_line(ref) -> str:
    f = _rel_path(ref.module_path)
    col = ref.column + 1 if ref.column is not None else 1
    ln = ref.line if isinstance(ref.line, int) else 0
    desc = getattr(ref, "description", "") or ""
    full = getattr(ref, "full_name", "") or ref.name or ""
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
    """查找符号的所有引用位置。返回 file:line:col + 描述。"""
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
    names = [n for n in s.get_names(all_scopes=True) if n.type != "keyword"]
    if not names:
        return "(无符号)"
    return "\n".join(f"  {n.type:<14} {n.name:<32} line {n.line}" for n in names)


@mcp.tool()
def py_diag(file: str) -> str:
    """获取 Python 文件的编译诊断（语法错误/告警，类似 IDE 里的红线）。无问题返回 ✅ 开头。
    供 Agent 形成「改 → 查错 → 再改」闭环（改完 .py 调它立刻知道对不对）。"""
    p = Path(file)
    if not p.is_absolute():
        p = WORKSPACE / file
    if not p.exists():
        return f"[文件不存在] {file}"
    code = p.read_text(encoding="utf-8", errors="ignore")
    lines = []
    # 1) 语法层（ast）：语法错误是最常见、最关键的"红线"
    try:
        ast.parse(code)
    except SyntaxError as e:
        lines.append(f"[ERROR] {e.lineno or 1}:{e.offset or 1} {e.msg}")
    except (ValueError, UnicodeDecodeError) as e:
        lines.append(f"[ERROR] 1:1 {type(e).__name__}: {e}")
    # 2) 语义层（jedi 诊断，若有则附加）
    try:
        s = jedi.Script(code, path=str(p))
        for d in s.get_diagnostics()[:30]:
            sev = getattr(d, "severity", None)
            tag = "[ERROR]" if sev in (1, 2) else "[WARN]"
            ln = getattr(d, "line", None) or 1
            col = (getattr(d, "column", None) or 0) + 1
            msg = (getattr(d, "message", "") or "").strip()
            if msg:
                lines.append(f"{tag} {ln}:{col} {msg}")
    except Exception:
        pass
    if not lines:
        return "✅ 无诊断问题"
    return "\n".join(dict.fromkeys(lines))   # 去重保序


if __name__ == "__main__":
    mcp.run()
