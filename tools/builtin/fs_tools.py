"""fs_tools.py —— 文件系统类脚本工具（外置件）。

agt_register() 返回描述符列表（script_tools.py 扫描注册约定）。
改完本文件用 /reload tools 热加载（不需要重启）。
"""
from pathlib import Path

_EXCLUDE_DIRS = {".git", "__pycache__", "node_modules", ".venv", "venv", "dist", "build", ".idea", ".vscode"}
_MAX_RESULTS = 500


def glob_files(pattern: str, path: str = "") -> str:
    """按通配模式查找文件名（不搜内容——搜内容用 grep）。
    pattern 支持 ** 递归 / * 单层通配 / ? 单字符 / [abc] 字符集，如：
      '**/*.py'（全仓 Python，含子目录）   'src/*.xml'（src 一层）   'docs/**/*.md'
      'test_*.py'   'agents/*.yml'
    path: 起始目录（留空=workspace 根；相对路径）。
    返回匹配列表（相对 path 的正斜杠路径，按名称排序）+ 统计行。"""
    if not pattern or not str(pattern).strip():
        return "[错误] pattern 不能为空（如 '**/*.py'）"
    base = Path(path) if str(path or "").strip() else Path(".")
    if not base.exists():
        return f"[错误] 目录不存在：{path}"
    try:
        matches = []
        for p in base.glob(str(pattern).strip()):
            if not p.is_file():
                continue
            # 排除目录树：路径任一段命中排除集则跳过（** 扫描常见噪声）
            parts = {seg.lower() for seg in p.parts[:-1]}
            if parts & _EXCLUDE_DIRS:
                continue
            matches.append(p.as_posix())
        matches.sort()
    except (OSError, ValueError) as e:
        return f"[错误] glob 失败：{type(e).__name__}: {e}"
    if not matches:
        return f"[glob {pattern!r} @ {base.as_posix()}] 无匹配文件"
    total = len(matches)
    shown = matches[:_MAX_RESULTS]
    head = (f"[glob {pattern!r} @ {base.as_posix()} 命中 {total} 个文件"
            + (f"，仅列前 {_MAX_RESULTS} 个" if total > _MAX_RESULTS else "") + "]")
    return head + "\n" + "\n".join(shown)


def agt_register():
    return [
        {"name": "glob_files", "func": glob_files, "group": "fs", "version": 1,
         "outputs": [{"name": "raw", "type": "string",
                      "description": "匹配文件列表（每行一个相对路径）+ 头部统计行"}]},
    ]
