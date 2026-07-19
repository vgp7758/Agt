"""csharp_lsp.py —— C# 代码语义导航 MCP server（基于 multilspy，C# 后端为 OmniSharp）。

工具（路径相对启动 cwd）：
  cs_def(file, line, col)   → 跳转到定义
  cs_ref(file, line, col)   → 查找所有引用（重构/重命名前必查）
  cs_syms(file)             → 文件符号
  cs_wsym(query)            → 工作区符号搜索（按名字找类/方法）
  cs_hover(file, line, col) → 类型/签名悬停信息

multilspy 的 C# 语言服务器是 OmniSharp（对 Unity 工程友好）。首次调用会自动下载
OmniSharp 二进制 + 索引工程（大工程首次几分钟）；常驻复用，之后秒级响应。
注：multilspy 0.0.15 不暴露编译诊断（publishDiagnostics 被忽略），诊断请用 dotnet build。
依赖：multilspy（ensure_lsp 装配时自动 pip install）。
WORKSPACE = 启动此进程时的 cwd（= agent 的 cwd）。
"""
import logging
from pathlib import Path

from mcp.server.fastmcp import FastMCP

logging.getLogger("mcp").setLevel(logging.WARNING)
mcp = FastMCP("csharp-lsp")

WORKSPACE = Path.cwd()
_ls = None          # multilspy LanguageServer（懒初始化）
_ls_ctx = None      # start_server() 的 async context manager（常驻保持）
_ls_ready = False


async def _get_ls():
    """懒启动 multilspy C# LanguageServer（OmniSharp，首次自动下二进制+索引，常驻复用）。"""
    global _ls, _ls_ctx, _ls_ready
    if _ls_ready:
        return _ls
    from multilspy import LanguageServer
    from multilspy.multilspy_config import LanguageServerConfig
    from multilspy.multilspy_logger import MultiLSPLogger
    config = LanguageServerConfig(code_language="csharp", code_language_root=str(WORKSPACE))
    # create 需 3 参：config、logger、repository_root_path（git 仓库根定位）
    _ls = LanguageServer.create(config, MultiLSPLogger(), str(WORKSPACE))
    # start_server 是 async context manager；常驻：进 context 不退出，LSP 进程保持运行
    _ls_ctx = _ls.start_server()
    await _ls_ctx.__aenter__()   # 启动 OmniSharp 子进程 + initialize（首次自动下载二进制）
    _ls_ready = True
    return _ls


def _pos_tuple(p):
    ln = getattr(p, "line", None)
    ln = (ln + 1) if isinstance(ln, int) else 0
    ch = getattr(p, "character", None) or getattr(p, "column", None)
    ch = (ch + 1) if isinstance(ch, int) else 1
    return ln, ch


def _loc_str(loc) -> str:
    fp = getattr(loc, "relative_file_path", None)
    if fp is None:
        fp = getattr(loc, "absolute_path", None) or getattr(loc, "uri", "") or ""
    if fp and isinstance(fp, str) and Path(fp).is_absolute():
        try:
            fp = str(Path(fp).relative_to(WORKSPACE)).replace("\\", "/")
        except Exception:
            pass
    start = getattr(loc, "start", None)
    if start is None:
        start = getattr(loc, "range", getattr(loc, "location", loc))
        start = getattr(start, "start", start)
    ln, ch = _pos_tuple(start)
    return f"{fp}:{ln}:{ch}"


@mcp.tool()
async def cs_def(file: str, line: int, col: int = 1) -> str:
    """跳转到 C# 符号定义。file: 相对路径；line: 行号(1-based)；col: 列号(1-based)。"""
    ls = await _get_ls()
    defs = await ls.request_definition(file, line - 1, col - 1)
    if not defs:
        return "(未找到定义)"
    return "\n".join(_loc_str(d) for d in defs[:15])


@mcp.tool()
async def cs_ref(file: str, line: int, col: int = 1) -> str:
    """查找 C# 符号的所有引用（重构/重命名前必查，比 grep 准）。"""
    ls = await _get_ls()
    refs = await ls.request_references(file, line - 1, col - 1)
    if not refs:
        return "(未找到引用)"
    return "\n".join(_loc_str(r) for r in refs[:40]) + (
        "\n…(仅显示前 40 条)" if len(refs) > 40 else "")


@mcp.tool()
async def cs_wsym(query: str) -> str:
    """按名字搜索整个工作区的 C# 符号（类/方法/字段）。query: 名字片段。"""
    ls = await _get_ls()
    syms = await ls.request_workspace_symbol(query)
    if not syms:
        return "(未找到符号)"
    out = []
    for s in syms[:40]:
        name = getattr(s, "name", "?")
        kind = getattr(s, "kind", "")
        loc = _loc_str(getattr(s, "location", s))
        out.append(f"{kind} {name}  @ {loc}")
    return "\n".join(out)


@mcp.tool()
async def cs_syms(file: str) -> str:
    """列出 C# 文件的所有符号（类/方法/属性），快速了解结构。"""
    ls = await _get_ls()
    syms = await ls.request_document_symbols(file)
    if not syms:
        return "(无符号)"
    out = []
    for s in syms[:60]:
        name = getattr(s, "name", "?")
        kind = getattr(s, "kind", "")
        rng = getattr(s, "range", None) or getattr(s, "location", None)
        loc = _loc_str(rng) if rng else ""
        out.append(f"{kind} {name}  {loc}")
    return "\n".join(out)


@mcp.tool()
async def cs_hover(file: str, line: int, col: int = 1) -> str:
    """获取光标位置的悬停信息（类型/签名/文档）。"""
    ls = await _get_ls()
    h = await ls.request_hover(file, line - 1, col - 1)
    return h if h else "(无悬停信息)"


if __name__ == "__main__":
    mcp.run()
