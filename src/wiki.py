"""wiki.py —— repo-wiki 知识库工具（.agent/wiki/，按业务/技术逻辑自由组织）。

让 Agent 给仓库积累"项目记忆"：开始不熟悉的任务前先查 wiki；完成重要功能/修改后
调用 update_wiki(summary)——同步薄封装，实际派活给 wiki-updater 子 Agent
（装配完全由 .agent/agents/wiki-updater.md 声明驱动：system 正文 / tools 白名单 /
assembly 清单含 wiki_tree 动态注入），工具本身零特殊处理。

wiki 结构不强制镜像仓库目录——按业务/技术逻辑自由组织。每篇 wiki 页可以：
  - 引用相关代码的相对路径（如 \"详见 src/auth/login.py\"）
  - 关联多个代码文件（不限于 1:1）
  - 通过 Markdown 相对链接跳转到其他 wiki 页（如 \"[认证流程](auth/flow.md)\"）

工具：
  wiki_read / wiki_list / wiki_search / wiki_tree   查（限定 .agent/wiki/）
  wiki_write / wiki_delete                          改（同上）
  update_wiki(summary)                              同步派活 wiki-updater 子 Agent 维护
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
    """主 Agent 的 wiki 工具集 = CRUD + update_wiki（同步薄封装：声明驱动的子 Agent 派活）。"""
    tools = wiki_crud_tools()

    def update_wiki(summary: str = "") -> str:
        """完成重要功能或修改后调用（同步阻塞，返回维护报告）。
        子 Agent 的装配（system/tools 白名单/assembly 清单）全部来自 .agent/agents/wiki-updater.md
        声明——本工具只负责拼 prompt + 同步跑，不再有代码级特殊处理。
        summary 留空 → 自动把当前 Turn 的完整上下文(任务+工具调用+结果+计划)交给子 Agent 理解；
        自己填 summary → 用它（更聚焦）。"""
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
        try:
            from multiagent import SubAgent, _agent_def_path, _resolve_tools, _parse_assembly, _parse_hooks
            from agent_config import load_agent_yml
            p = _agent_def_path("wiki-updater")
            if p is None or not p.exists():
                return "[声明缺失] .agent/agents/wiki-updater.md 不存在，无法维护 wiki（create_agent 重建）"
            meta, system = load_agent_yml(p)
            toolbox, _ = _resolve_tools(agent, meta.get("tools", ""))
            model_name = meta.get("model") or agent.model_name
            if model_name not in config.MODELS:
                model_name = agent.model_name
            sub = SubAgent("wiki-updater", model_name, system or "", toolbox,
                           registry=getattr(agent, "registry", None),
                           agent_id="wiki-updater", caller_id=agent.agent_id,
                           assembly=_parse_assembly(meta))
            hooks = _parse_hooks(meta)
            if hooks:
                sub.agent.session.hook_specs = hooks
            report = sub.prompt(
                f"请据此更新 repo-wiki（.agent/wiki/）：\n\n{prompt}\n\n"
                f"上下文已注入最新 wiki 树——按它定位相关页面（需要细节再 wiki_read），"
                f"wiki_write 按业务/技术逻辑更新/新建页面，聚焦改动涉及的模块。"
            )
            return report or "(wiki 维护子 Agent 未产出报告)"
        except Exception as e:
            return f"[update_wiki 失败] {type(e).__name__}: {e}"

    tools.append(Tool(update_wiki))
    return tools
