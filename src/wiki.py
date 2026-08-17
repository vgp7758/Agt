"""wiki.py —— repo-wiki 知识库工具（.agent/wiki/，按业务/技术逻辑自由组织）。

让 Agent 给仓库积累"项目记忆"：开始不熟悉的任务前先查 wiki；完成重要功能/修改后
调用 update_wiki(summary)，由一个【wiki 维护子 Agent】按摘要更新对应页面。

wiki 结构不强制镜像仓库目录——按业务/技术逻辑自由组织。每篇 wiki 页可以：
  - 引用相关代码的相对路径（如 \"详见 src/auth/login.py\"）
  - 关联多个代码文件（不限于 1:1）
  - 通过 Markdown 相对链接跳转到其他 wiki 页（如 \"[认证流程](auth/flow.md)\"）

工具：
  wiki_read / wiki_list / wiki_search / wiki_tree   查（限定 .agent/wiki/）
  wiki_write / wiki_delete                          改（同上）
  update_wiki(summary)                              绑定主 Agent：起子 Agent 自动维护
"""
from __future__ import annotations

from pathlib import Path

import config

from real_tools import WORKSPACE, _md_headings
from tools import Tool, Toolbox

WIKI_ROOT = lambda: WORKSPACE / ".agent" / "wiki"


def _md_outline_lines(fp: Path) -> list:
    """读 md 文件，返回其标题大纲行（按层级缩进 + ·L行号）；非 md / 读失败 → []。
    模型看到 'auth/flow.md' 下的 '# 认证流程 ·L10'，即可 read_file(...,start_line=10) 跳到该节。"""
    if fp.suffix.lower() not in {".md", ".markdown"}:
        return []
    try:
        text = fp.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return []
    return [f"{'  ' * lv}{'#' * lv} {title} ·L{ln}" for (ln, lv, title) in _md_headings(text)]

WIKI_UPDATER_SYSTEM = (
    "你是 repo-wiki 维护助手。根据主 Agent 提供的改动摘要，维护 `.agent/wiki/` 下的知识库页面。\n"
    "wiki 按【业务 / 技术逻辑】自由组织（不必镜像仓库文件目录），如 features/auth.md、architecture/data-flow.md。\n"
    "原则：\n"
    "- 先用 wiki_tree/wiki_read 了解现有 wiki 结构与内容\n"
    "- 用 wiki_write 更新/新建受影响模块的页面（聚焦改动，简洁）\n"
    "- 每页可引用相关代码的相对路径（如 src/auth/login.py），可关联多个文件\n"
    "- 文档间通过 Markdown 相对链接互相跳转（如 [认证流程](auth/flow.md)），形成知识网\n"
    "- 每页核心内容：模块职责、关键函数/类、与其它模块的关系、依赖、注意事项"
)   # 内置兜底；正式声明在 .agent/agents/wiki-updater.md（外置优先，见 _load_wiki_updater_decl）


def _load_wiki_updater_decl():
    """读 .agent/agents/wiki-updater.md 声明（与其他子 Agent 同款管理方式）。
    返回 (meta, system)；文件不存在/解析失败 → ({}, "")，调用方回退内置 WIKI_UPDATER_SYSTEM。
    声明里的 tools 字段被有意忽略——wiki 子 Agent 工具集固定为 wiki CRUD 六件套（防越权）。"""
    try:
        from multiagent import _agent_md_path, _split_frontmatter
        p = _agent_md_path("wiki-updater")
        if p is None or not p.exists():
            return {}, ""
        meta, system = _split_frontmatter(p.read_text(encoding="utf-8"))
        return (meta or {}), (system or "").strip()
    except Exception:
        return {}, ""


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
    import re
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


def wiki_crud_tools() -> list:
    """wiki 增删改查工具（不依赖具体 Agent，可被任意 Agent 使用）。"""
    return [Tool(wiki_read), Tool(wiki_list), Tool(wiki_tree), Tool(wiki_search),
            Tool(wiki_write), Tool(wiki_delete)]


def make_wiki_tools(agent) -> list:
    """主 Agent 的 wiki 工具集 = CRUD + update_wiki（后者绑定主 Agent，起子 Agent 维护）。"""
    tools = wiki_crud_tools()

    def update_wiki(summary: str = "") -> str:
        """完成重要功能或修改后调用。
        summary 留空 → 自动把当前 Turn 的完整上下文(任务+工具调用+结果+计划)交给子 Agent 理解；
        自己填 summary → 用它（更聚焦）。"""
        # 自动摘要：无 summary 时从最近一轮 Turn 提取上下文
        prompt = summary.strip()
        if not prompt:
            last = agent.session.turns[-1] if agent.session.turns else None
            blocks = []
            if last:
                blocks.append(f"用户任务：{last.user_message}")
                for step in last.steps:
                    for tc in step.tool_calls:
                        n, a, r = agent.session.toollog.view(tc.call_id)
                        blocks.append(f"- {n}({a}) → {r[:200]}")
                if last.answer:
                    blocks.append(f"最终结果：{last.answer[:300]}")
            if agent.plan:
                from plan_tools import _plan_text
                blocks.append(f"执行计划：\n{_plan_text(agent)}")
            prompt = "\n".join(blocks) if blocks else "(无上下文)"
        from agent import Agent
        # 声明外置（.agent/agents/wiki-updater.md，与其他子 Agent 同款管理）；内置兜底
        meta, system_ext = _load_wiki_updater_decl()
        model_name = (meta.get("model") or "").strip() or agent.model_name
        if model_name not in config.MODELS:
            model_name = agent.model_name
        sub = Agent(
            system=system_ext or WIKI_UPDATER_SYSTEM,
            tools=Toolbox(*wiki_crud_tools()),
            model_name=model_name,
            enable_thinking=False,
            verbose=False,
            on_event=None,           # 静默执行；结果以工具返回值回到主 Agent
        )
        report = sub.run(
            f"请据此更新 repo-wiki（.agent/wiki/）：\n\n{prompt}\n\n"
            f"先 wiki_tree/wiki_read 了解现有 wiki 结构，再 wiki_write 按业务/技术逻辑更新/新建页面"
            f"（引用相关代码相对路径、文档间可 Markdown 相对链接互相跳转）。聚焦改动涉及的模块。"
        )
        return report or "(wiki 维护子 Agent 未产出报告)"

    tools.append(Tool(update_wiki))
    return tools
