"""csharp_lsp.py —— C# 代码语义导航 MCP server（基于 multilspy，C# 后端为 OmniSharp）。

工具（路径相对启动 cwd）：
  cs_def(file, line, col)   → 跳转到定义
  cs_ref(file, line, col)   → 查找所有引用（重构/重命名前必查）
  cs_syms(file)             → 文件符号
  cs_wsym(query)            → 工作区符号搜索
  cs_hover(file, line, col) → 类型/签名悬停
  cs_diag(file)             → 编译诊断（编辑器红线/黄线，改完 .cs 调用验证）

multilspy C# 后端是 OmniSharp（Unity 友好）。首次 _get_ls() 启动 OmniSharp + 索引
（大工程几分钟，自动下二进制），常驻复用。multilspy 的 request_* 返回 dict（LSP JSON），
故用 _get 兼容访问。publishDiagnostics 默认被 multilspy 丢弃，这里覆盖 on_notification 捕获。
所有 request 必须在 with ls.open_file(file): 内。需先 ensure_lsp('csharp')。
依赖：multilspy（ensure_lsp 装配时自动 pip install）。
WORKSPACE = 启动此进程时的 cwd（= agent 的 cwd）。
"""
import asyncio
import logging
from pathlib import Path
from urllib.parse import urlparse, unquote

from mcp.server.fastmcp import FastMCP

logging.getLogger("mcp").setLevel(logging.WARNING)
mcp = FastMCP("csharp-lsp")

WORKSPACE = Path.cwd()
_ls = None
_ls_ctx = None
_ls_ready = False
_diag_store = {}   # relative_path -> diagnostics list


def _get(obj, key, default=None):
    """兼容 dict（LSP JSON）和对象的属性访问。"""
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _on_publish_diag(payload):
    """捕获 OmniSharp 推来的诊断（覆盖 multilspy 的 do_nothing）。"""
    try:
        uri = payload.get("uri", "") or ""
        fp = unquote(urlparse(uri).path)
        if fp.startswith("/") and len(fp) > 2 and fp[2] == ":":
            fp = fp[1:]   # Windows: /C:/Users → C:/Users
        try:
            rel = str(Path(fp).relative_to(WORKSPACE)).replace("\\", "/")
        except Exception:
            rel = fp
        _diag_store[rel] = payload.get("diagnostics", []) or []
    except Exception:
        pass


async def _get_ls():
    """懒启动 multilspy C# LanguageServer（OmniSharp，首次下二进制+索引，常驻）。"""
    global _ls, _ls_ctx, _ls_ready
    if _ls_ready:
        return _ls
    # patch multilspy 的 dotnet 版本检测 bug：它取第一个 Microsoft.NETCore.App（老版本 5.x
    # 常排在前），误判 Unknown。改成取最高兼容版本（8/9/10→V8, 7→V7, 6→V6）。
    import subprocess as _sp
    import multilspy.multilspy_utils as _mu
    _DV = _mu.DotnetVersion

    def _best_dotnet():
        try:
            r = _sp.run(["dotnet", "--list-runtimes"], capture_output=True, check=True, text=True)
            majors = set()
            for line in r.stdout.split("\n"):
                if line.startswith("Microsoft.NETCore.App"):
                    p = line.split()
                    if len(p) >= 2:
                        try:
                            majors.add(int(p[1].split(".")[0]))
                        except Exception:
                            pass
            if majors & {8, 9, 10}: return _DV.V8
            if 7 in majors: return _DV.V7
            if 6 in majors: return _DV.V6
            if 4 in majors: return _DV.V4
        except Exception:
            pass
        return _DV.V8
    _mu.PlatformUtils.get_dotnet_version = staticmethod(_best_dotnet)

    from multilspy import LanguageServer
    from multilspy.multilspy_config import MultilspyConfig, Language
    from multilspy.multilspy_logger import MultilspyLogger
    config = MultilspyConfig(code_language=Language.CSHARP)
    _ls = LanguageServer.create(config, MultilspyLogger(), str(WORKSPACE))
    _ls_ctx = _ls.start_server()
    await _ls_ctx.__aenter__()
    try:
        _ls.server.on_notification("textDocument/publishDiagnostics", _on_publish_diag)
    except Exception:
        pass
    _ls_ready = True
    return _ls


def _pos_tuple(p):
    ln = _get(p, "line", None)
    ln = (ln + 1) if isinstance(ln, int) else 0
    ch = _get(p, "character", None)
    if ch is None:
        ch = _get(p, "column", None)
    ch = (ch + 1) if isinstance(ch, int) else 1
    return ln, ch


def _loc_str(loc) -> str:
    fp = _get(loc, "relativePath", None) or _get(loc, "relative_file_path", None)
    if fp is None:
        fp = (_get(loc, "absolutePath", None) or _get(loc, "absolute_path", None)
              or _get(loc, "uri", "") or "")
    if fp and isinstance(fp, str) and Path(fp).is_absolute():
        try:
            fp = str(Path(fp).relative_to(WORKSPACE)).replace("\\", "/")
        except Exception:
            pass
    start = _get(loc, "start", None)
    if start is None:
        start = _get(loc, "range", _get(loc, "location", loc))
        start = _get(start, "start", start)
    ln, ch = _pos_tuple(start)
    return f"{fp}:{ln}:{ch}"


@mcp.tool()
async def cs_def(file: str, line: int, col: int = 1) -> str:
    """跳转到 C# 符号定义。file: 相对路径；line: 1-based 行号；col: 1-based 列号。"""
    ls = await _get_ls()
    with ls.open_file(file):
        defs = await ls.request_definition(file, line - 1, col - 1)
    if not defs:
        return "(未找到定义)"
    return "\n".join(_loc_str(d) for d in defs[:15])


@mcp.tool()
async def cs_ref(file: str, line: int, col: int = 1) -> str:
    """查找 C# 符号的所有引用（重构/重命名前必查，比 grep 准）。"""
    ls = await _get_ls()
    with ls.open_file(file):
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
        name = _get(s, "name", "?")
        kind = _get(s, "kind", "")
        loc = _loc_str(_get(s, "location", s))
        out.append(f"{kind} {name}  @ {loc}")
    return "\n".join(out)


@mcp.tool()
async def cs_syms(file: str) -> str:
    """列出 C# 文件的所有符号（类/方法/属性），快速了解结构。"""
    ls = await _get_ls()
    with ls.open_file(file):
        result = await ls.request_document_symbols(file)
    # request_document_symbols 返回 (symbols, tree) 元组
    syms = result[0] if isinstance(result, tuple) else result
    if not syms:
        return "(无符号)"
    out = []
    for s in syms[:60]:
        name = _get(s, "name", "?")
        kind = _get(s, "kind", "")
        rng = _get(s, "range", None) or _get(s, "location", None)
        loc = _loc_str(rng) if rng else ""
        out.append(f"{kind} {name}  {loc}")
    return "\n".join(out)


@mcp.tool()
async def cs_hover(file: str, line: int, col: int = 1) -> str:
    """获取光标位置的悬停信息（类型/签名/文档）。"""
    ls = await _get_ls()
    with ls.open_file(file):
        h = await ls.request_hover(file, line - 1, col - 1)
    return h if h else "(无悬停信息)"


@mcp.tool()
async def cs_diag(file: str) -> str:
    """获取 C# 文件编译诊断（编辑器里的红线/黄线）。改完 .cs 后调用——OmniSharp
    实时分析并返回错误/警告，形成"改→查错→再改"闭环，不必等 dotnet build 或肉眼翻编辑器。
    file: 相对路径。首次查询前需先 ensure_lsp('csharp') 且 OmniSharp 完成索引。"""
    ls = await _get_ls()
    _diag_store.pop(file, None)
    with ls.open_file(file):
        await asyncio.sleep(2.5)   # 等 OmniSharp 分析并推送 publishDiagnostics
    diags = _diag_store.get(file, [])
    if not diags:
        return "(无诊断：编译通过，或诊断尚未到达——大工程首次索引需更久，可稍后再查)"
    sev_map = {1: "ERROR", 2: "WARN", 3: "INFO", 4: "HINT"}
    out = []
    for d in diags[:60]:
        sev = sev_map.get(d.get("severity", 0), "?")
        msg = (d.get("message", "") or "").strip().split("\n")[0]
        st = (d.get("range", {}) or {}).get("start") or {}
        ln = st.get("line", 0) + 1
        ch = st.get("character", 0) + 1
        out.append(f"{file}:{ln}:{ch} [{sev}] {msg}")
    return "\n".join(out) + (f"\n…(共 {len(diags)} 条，显示前 60)" if len(diags) > 60 else "")


if __name__ == "__main__":
    mcp.run()
