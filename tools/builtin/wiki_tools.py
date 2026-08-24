"""wiki_tools.py —— repo-wiki CRUD 六件套（外置件，真限界上下文）。

外置判别标准（wiki: architecture/tool-externalization-criteria.md）：
.agent/wiki/*.md 由本组工具【自写自读】——wiki_write 写、wiki_read 读，引擎从头到尾
不碰这些文件 → 数据主权在工具组自身，可完全外置（零 agent 注入，连 factory 都不需要）。

workspace 约定：inline 模式在主进程执行，cwd 即 workspace——`Path.cwd() / .agent / wiki`
是磁盘约定，不依赖 real_tools.WORKSPACE。改完本文件用 /reload tools 热加载。
"""
from __future__ import annotations

import re
from pathlib import Path

# import 时捕获（与 real_tools.WORKSPACE 同款语义；扫描器在主进程 import，cwd=workspace）
_WORKSPACE = Path.cwd()
WIKI_ROOT = lambda: _WORKSPACE / ".agent" / "wiki"


def _md_headings(text: str) -> list:
    """提取 Markdown 的 ATX 标题（#~######），返回 [(行号1based, 层级, 标题), ...]。
    跳过 ``` / ~~~ 代码围栏里的 #。（与 real_tools._md_headings 同实现——外置件自带，不 import 框架）"""
    in_fence, fence = False, ""
    out = []
    for idx, raw in enumerate(text.splitlines()):
        s = raw.strip()
        if s[:3] in ("```", "~~~"):
            if not in_fence:
                in_fence, fence = True, s[:3]
            elif s == fence:
                in_fence = False
            continue
        if in_fence:
            continue
        m = re.match(r"^(#{1,6})\s+(.*?)\s*#*$", raw)
        if m:
            out.append((idx + 1, len(m.group(1)), m.group(2).strip()))
    return out


def _md_outline_lines(fp: Path) -> list:
    """读 md 文件，返回其标题大纲行（按层级缩进 + ·L行号）；非 md / 读失败 → []。"""
    if fp.suffix.lower() not in {".md", ".markdown"}:
        return []
    try:
        text = fp.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return []
    return [f"{'  ' * lv}{'#' * lv} {title} ·L{ln}" for (ln, lv, title) in _md_headings(text)]


def _wiki_resolve(path: str) -> Path:
    """把路径解析到 .agent/wiki/ 内；越界拒绝。"""
    base = WIKI_ROOT().resolve()
    target = (base / path).resolve()
    try:
        target.relative_to(base)
    except ValueError:
        raise PermissionError(f"拒绝访问 wiki 外的路径: {path}")
    return target


# ========== 查 ==========

def wiki_read(path: str) -> str:
    """读取 .agent/wiki/ 下某个 wiki 页面的内容。path 相对 wiki 根（如 'src/auth/login.md'）。"""
    p = _wiki_resolve(path)
    if not p.exists():
        return f"[wiki 页面不存在] {path}（用 wiki_list/wiki_tree 查看已有页面）"
    return p.read_text(encoding="utf-8")


def wiki_list(path: str = ".") -> str:
    """列出 .agent/wiki/ 下某子目录的 wiki 页面；每个 .md 文件下附其标题大纲（各层级标题 + 行号）。"""
    p = _wiki_resolve(path)
    if not p.exists():
        return f"[目录不存在] {path}"
    children = sorted(p.iterdir(), key=lambda x: x.relative_to(WIKI_ROOT()).as_posix())
    if not children:
        return "(空)"
    out = []
    for x in children:
        out.append(x.relative_to(WIKI_ROOT()).as_posix() + ("/" if x.is_dir() else ""))
        out.extend(_md_outline_lines(x))
    return "\n".join(out)


def wiki_tree() -> str:
    """显示整个 .agent/wiki/ 的页面树（相对路径）；每个 .md 文件下附其标题大纲（层级 + 行号）。"""
    root = WIKI_ROOT()
    if not root.exists():
        return "(wiki 还没有任何页面)"
    files = sorted((p for p in root.rglob("*") if p.is_file()),
                   key=lambda x: x.relative_to(root).as_posix())
    if not files:
        return "(空)"
    out = []
    for fp in files:
        out.append(fp.relative_to(root).as_posix())
        out.extend(_md_outline_lines(fp))
    return "\n".join(out)


def wiki_search(query: str, regex: bool = False, max_results: int = 30) -> str:
    """在 .agent/wiki/ 全文搜索。返回 '相对路径:行号:匹配行'。regex=True 按正则。"""
    root = WIKI_ROOT()
    if not root.exists():
        return "(wiki 为空)"
    try:
        rx = re.compile(query if regex else re.escape(query))
    except re.error as e:
        return f"[正则错误] {e}"
    out = []
    for fp in sorted(root.rglob("*")):
        if not fp.is_file():
            continue
        try:
            text = fp.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        rel = fp.relative_to(root).as_posix()
        for i, line in enumerate(text.splitlines(), 1):
            if rx.search(line):
                out.append(f"{rel}:{i}: {line.strip()[:200]}")
                if len(out) >= max_results:
                    out.append(f"...（达 max_results={max_results}）")
                    return "\n".join(out)
    return "\n".join(out) if out else "(未找到)"


# ========== 改 ==========

def wiki_write(path: str, content: str) -> str:
    """写入/更新 .agent/wiki/ 下一个 wiki 页面（覆盖）。path 相对 wiki 根。"""
    p = _wiki_resolve(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return f"✅ 已写入 wiki 页面 {path}（{len(content)} 字符）"


def wiki_delete(path: str) -> str:
    """删除 .agent/wiki/ 下一个 wiki 页面。"""
    p = _wiki_resolve(path)
    if not p.exists():
        return f"[页面不存在] {path}"
    p.unlink()
    return f"✅ 已删除 wiki 页面 {path}"


def agt_register():
    return [
        {"name": n, "func": f, "hidden": False, "group": "wiki", "version": 1}
        for n, f in [("wiki_read", wiki_read), ("wiki_list", wiki_list),
                     ("wiki_tree", wiki_tree), ("wiki_search", wiki_search),
                     ("wiki_write", wiki_write), ("wiki_delete", wiki_delete)]
    ]
