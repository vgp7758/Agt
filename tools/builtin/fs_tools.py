"""fs_tools.py —— 文件系统类脚本工具（外置件）。

agt_register() 返回描述符列表（script_tools.py 扫描注册约定）。
改完本文件用 /reload tools 热加载（不需要重启）。
"""
import fnmatch
from pathlib import Path

_EXCLUDE_DIRS = {".git", "__pycache__", "node_modules", ".venv", "venv", "dist", "build", ".idea", ".vscode"}
_MAX_RESULTS = 500


def _make_gitignore_filter(base: Path):
    """读 base/.gitignore → (keep_dir, keep_file) 谓词（用户提案 2026-09-02：glob 跳过 gitignore 排除项）。
    与引擎 agent.py 同款语义的轻量复制（外置件自洽——不 import src，subprocess 模式下 sys.path 无 src）：
    锚定模式（/dist）只匹配根层；目录模式匹配任意路径段；通配模式 fnmatch 文件名/全路径；
    ! 否定模式不支持（简化）。base 无 .gitignore → 恒 True 谓词。"""
    pats, anchored = [], []
    try:
        for ln in (base / ".gitignore").read_text(encoding="utf-8").splitlines():
            ln = ln.strip()
            if not ln or ln.startswith("#") or ln.startswith("!"):
                continue
            if ln.startswith("/"):
                anchored.append(ln.lstrip("/").rstrip("/"))
            else:
                pats.append(ln.rstrip("/"))
    except Exception:
        pass
    pats.append("*.egg-info")

    def _seg_or_fnmatch(name: str, pat: str) -> bool:
        return name == pat or fnmatch.fnmatch(name, pat)

    def keep_dir(rel: str) -> bool:
        name = rel.rsplit("/", 1)[-1]
        if any(_seg_or_fnmatch(name, p) for p in pats):
            return False
        if any(rel == p or fnmatch.fnmatch(rel, p) for p in anchored):
            return False
        return True

    def keep_file(rel: str) -> bool:
        name = rel.rsplit("/", 1)[-1]
        segs = rel.split("/")
        if any(_seg_or_fnmatch(seg, p) for seg in segs for p in pats):
            return False
        if any(fnmatch.fnmatch(rel, p) or fnmatch.fnmatch(name, p) for p in pats):
            return False
        if any(rel == p or rel.startswith(p + "/") or fnmatch.fnmatch(rel, p) for p in anchored):
            return False
        return True

    return keep_dir, keep_file


def glob_files(pattern: str, path: str = "") -> str:
    """按通配模式查找文件名（不搜内容——搜内容用 grep）。
    pattern 支持 ** 递归 / * 单层通配 / ? 单字符 / [abc] 字符集，如：
      '**/*.py'（全仓 Python，含子目录）   'src/*.xml'（src 一层）   'docs/**/*.md'
      'test_*.py'   'agents/*.yml'
    path: 起始目录（留空=workspace 根；相对路径）。
    返回匹配列表（相对 path 的正斜杠路径，按名称排序）+ 统计行。
    跳过 .gitignore 排除项与 .git/__pycache__ 等硬清单（与 git 工作区一致）；
    pattern 静态前缀（首个通配符前的目录，如 'blog/**'）或 path 显式命中排除区 → 尊重意图照列。"""
    if not pattern or not str(pattern).strip():
        return "[错误] pattern 不能为空（如 '**/*.py'）"
    pat = str(pattern).strip()
    base = Path(path) if str(path or "").strip() else Path(".")
    if not base.exists():
        return f"[错误] 目录不存在：{path}"
    # 豁免判定：path 或 pattern 静态前缀目录被 gitignore 命中 → 该次搜索不应用 gitignore
    # （用户显式要去排除区找东西；硬清单仍生效——.git/__pycache__ 恒无意义）
    keep_dir, keep_file = _make_gitignore_filter(base if base.is_absolute() else Path(".").resolve())
    exempt = False
    try:
        _rb = base.resolve()
        _rel = _rb.relative_to(Path(".").resolve()).as_posix()
        if _rel not in ("", ".") and not keep_dir(_rel):
            exempt = True
    except ValueError:
        exempt = True   # 根外：无 gitignore 语义，不过滤
    _static = pat
    for ch in "*?['":
        _static = _static.split(ch)[0]
    _static_dir = _static.rstrip("/").rsplit("/", 1)[0] if "/" in _static else ""
    if _static_dir and not keep_dir(_static_dir):
        exempt = True
    try:
        matches = []
        for p in base.glob(pat):
            if not p.is_file():
                continue
            parts = {seg.lower() for seg in p.parts[:-1]}
            if parts & _EXCLUDE_DIRS:
                continue
            if not exempt:
                rel = p.relative_to(base).as_posix()
                _full = (Path(path or ".") / rel).as_posix() if path else rel
                if not keep_file(_full):
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
        {"name": "glob_files", "func": glob_files, "group": "fs", "version": 2,
         "outputs": [{"name": "raw", "type": "string",
                      "description": "匹配文件列表（每行一个相对路径）+ 头部统计行"}]},
    ]
