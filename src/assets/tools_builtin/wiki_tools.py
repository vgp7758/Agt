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

# workspace：默认 import 时捕获（兼容直接 import），agt_register(ctx) 时用引擎传入的覆盖
# （ctx["cwd"] 是引擎视角的真实 workspace——比 Path.cwd() 稳，测试 os.chdir 等场景不受影响）
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


# ========== 章节（chapter）级维护 ==========
# 章节 = 标题 + 其全部子树（到下一个层级不深于它的标题行或 EOF 为止）——结构化 md 的自然边界。
# 与 edit/insert/delete/move 行级文件工具同哲学：外科手术式小改，避免整页重写。

def _chapter_spans(lines):
    """章节切分（fence 感知——代码围栏里的 # 不是标题）。
    返回 [(层级, 标题, 起始行, 结束行)]，行号 0-based、区间 [start, end)。
    end = 子树末：跳过所有更深层级的连续章节，停在第一个层级 ≤ 本章的标题行或 EOF。"""
    in_fence, fence = False, ""
    heads = []
    for idx, raw in enumerate(lines):
        s = raw.strip()
        if s[:3] in ("```", "~~~"):
            if not in_fence:
                in_fence, fence = True, s[:3]
            elif s == fence:
                in_fence = False
            continue
        if in_fence:
            continue
        m = re.match(r"^(#{1,6})\s+(.*?)\s*#*\s*$", raw)
        if m:
            heads.append((idx, len(m.group(1)), m.group(2).strip()))
    out = []
    for i, (ln, lv, title) in enumerate(heads):
        end_ln = len(lines)
        for j in range(i + 1, len(heads)):
            if heads[j][1] <= lv:   # 下一个不深于本章的标题 → 子树结束
                end_ln = heads[j][0]
                break
        out.append((lv, title, ln, end_ln))
    return out


def _find_chapter(spans, title):
    t = str(title).strip()
    for (lv, ti, ln, end) in spans:
        if ti == t:
            return (lv, ti, ln, end)
    return None


def _no_such(title, spans, what="章节"):
    names = "; ".join(f"{'#' * lv} {ti}" for (lv, ti, _, _) in spans[:20])
    return f"[未找到{what}] {title}（现有章节: {names or '（无）'}）"


def wiki_add_chapter(path: str, title: str, content: str, level: int = 2, after: str = "") -> str:
    """向 wiki 页面【新增一个章节】（按标题边界插入，不动页面其余部分）。
    level=标题层级 1~6（章节通常 2）；after=空 → 追加页面末尾；after=锚点章节标题 → 插到该章节【子树之后】
    （锚点的子章节不会被拆散）。增量维护优先用本工具，避免 wiki_write 整页重写（整页重写中断会留残页）。"""
    p = _wiki_resolve(path)
    if not p.exists():
        return f"[页面不存在] {path}（新页面请先用 wiki_write 创建）"
    lv = int(level)
    if not (1 <= lv <= 6):
        return "[level 应为 1~6]"
    text = p.read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)
    spans = _chapter_spans(lines)
    if _find_chapter(spans, title):
        return f"[章节已存在] {title}（更新请用 wiki_update_chapter）"
    block = ["#" * lv + " " + str(title).strip() + "\n", "\n"]
    body = str(content or "").rstrip("\n")
    if body:
        block += [l + "\n" for l in body.split("\n")]
        block.append("\n")
    if after:
        hit = _find_chapter(spans, after)
        if not hit:
            return _no_such(after, spans, "锚点章节")
        pos = hit[3]
        where = f"『{str(after).strip()}』之后"
    else:
        pos = len(lines)
        if lines:
            if not lines[-1].endswith("\n"):
                lines[-1] += "\n"
            if lines[-1].strip() != "":
                lines.append("\n")
        where = "页面末尾"
    lines[pos:pos] = block
    p.write_text("".join(lines), encoding="utf-8")
    return f"✅ 已在 {path} {where}新增章节『{str(title).strip()}』（level {lv}，{len(block)} 行）"


def wiki_update_chapter(path: str, title: str, content: str = None, new_title: str = "") -> str:
    """【更新已有章节】：content 传值 → 替换章节正文（标题行保留；章节含其全部子章节，子章节一并被替换）；new_title 传值 → 重命名标题（层级不变）；可同时用。
    content 不传（保持原正文）即可做纯重命名；按标题精确定位，正文中的代码围栏安全。"""
    p = _wiki_resolve(path)
    if not p.exists():
        return f"[页面不存在] {path}"
    lines = p.read_text(encoding="utf-8").splitlines(keepends=True)
    spans = _chapter_spans(lines)
    hit = _find_chapter(spans, title)
    if not hit:
        return _no_such(title, spans)
    lv, ti, ln, end = hit
    changed = []
    nt = str(new_title or "").strip()
    if nt:
        lines[ln] = "#" * lv + " " + nt + "\n"
        changed.append(f"标题『{ti}』→『{nt}』")
    if content is not None:
        body = str(content).rstrip("\n")
        new_body = ["\n"]
        if body:
            new_body += [l + "\n" for l in body.split("\n")]
            new_body.append("\n")
        lines[ln + 1:end] = new_body
        changed.append(f"正文 {len(new_body)} 行")
    if not changed:
        return "[未做修改] content / new_title 至少传一项"
    p.write_text("".join(lines), encoding="utf-8")
    return f"✅ 已更新 {path} 章节（{'; '.join(changed)}）"


def wiki_remove_chapter(path: str, title: str) -> str:
    """【删除一个章节】（标题 + 正文 + 其全部子章节整棵移除，页面其余部分不动）。按标题精确定位。"""
    p = _wiki_resolve(path)
    if not p.exists():
        return f"[页面不存在] {path}"
    lines = p.read_text(encoding="utf-8").splitlines(keepends=True)
    spans = _chapter_spans(lines)
    hit = _find_chapter(spans, title)
    if not hit:
        return _no_such(title, spans)
    lv, ti, ln, end = hit
    n = end - ln
    del lines[ln:end]
    p.write_text("".join(lines), encoding="utf-8")
    return f"✅ 已删除 {path} 章节『{ti}』（{n} 行）"


def wiki_move_chapter(path: str, title: str, after: str = "") -> str:
    """【移动章节】调整页面结构：整棵子树移到锚点章节【子树之后】（after=空 → 移到页面末尾）。
    章节顺序重排（页面结构维护）用它，多次调用可完成任意重排。"""
    p = _wiki_resolve(path)
    if not p.exists():
        return f"[页面不存在] {path}"
    lines = p.read_text(encoding="utf-8").splitlines(keepends=True)
    spans = _chapter_spans(lines)
    hit = _find_chapter(spans, title)
    if not hit:
        return _no_such(title, spans)
    if str(after).strip():
        if not _find_chapter(spans, after):
            return _no_such(after, spans, "锚点章节")
    lv, ti, ln, end = hit
    block = lines[ln:end]
    if block and block[-1].strip() != "":
        block.append("\n")   # 移动块尾确保空行分隔（原 span 若贴下一标题则无尾空行）
    del lines[ln:end]
    where = "页面末尾"
    pos = len(lines)
    if lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n"
    a = str(after).strip()
    if a:
        hit2 = _find_chapter(_chapter_spans(lines), a)
        if hit2:
            pos = hit2[3]
            where = f"『{a}』之后"
    lines[pos:pos] = block
    p.write_text("".join(lines), encoding="utf-8")
    return f"✅ 已移动 {path} 章节『{ti}』到 {where}"


def agt_register(ctx=None):
    """ctx: {"cwd": "<workspace 绝对路径>", ...}——引擎扫描时按位置传入（签名兼容：无 ctx 也工作，
    回退 import 时的 Path.cwd()）。模块级 _WORKSPACE 用 ctx 覆盖（global）。"""
    global _WORKSPACE
    if ctx and ctx.get("cwd"):
        _WORKSPACE = Path(ctx["cwd"])
    return [
        {"name": n, "func": f, "hidden": False, "group": "wiki", "version": 1}
        for n, f in [("wiki_read", wiki_read), ("wiki_list", wiki_list),
                     ("wiki_tree", wiki_tree), ("wiki_search", wiki_search),
                     ("wiki_write", wiki_write), ("wiki_delete", wiki_delete),
                     ("wiki_add_chapter", wiki_add_chapter), ("wiki_update_chapter", wiki_update_chapter),
                     ("wiki_remove_chapter", wiki_remove_chapter), ("wiki_move_chapter", wiki_move_chapter)]
    ]
