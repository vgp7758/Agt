"""explore_tools.py —— 外置探索工具（spec s_54a1eb86：工作流式 react 探索 + Step 嫁接）。

用户提案 2026-09-09：纯探索过程（多轮 grep/read_file 定位代码）对历史会话依赖低——
由本工具在小上下文 react 循环里完成，工具调用过程嫁接回主 agent steps（复用
agent._seed_steps：toollog.record + Step + add_step——events.jsonl/读档重放/步距
衰减全走既有管线，零新落盘代码），主 agent 只拿结构化摘要继续干活。

只读白名单：探索 agent 不能改文件——改动决策留给主 agent。
recent-file 语义澄清：_FILE_SNAP_TOOLS 只挂写工具（edit/write…），探索全只读——
嫁接步不进 rf_map 是与主循环一致的正确行为（rf 管"本轮变更文件速览"）。

需要 ctx["agent"]（chat.py → attach_script_tools → scan_script_tools 注入）；
无 agent 引用（纯工具箱构建/测试）时本组工具不注册（降级，不炸主程序）。
改完本文件用 /reload tools 热加载。
"""
from __future__ import annotations

import json
import time
from pathlib import Path

# workspace：默认 import 时捕获（兼容直接 import），agt_register(ctx) 时用引擎传入的覆盖
# （ctx["cwd"] 是引擎视角的真实 workspace——比 Path.cwd() 稳，os.chdir 后不漂移）
_WORKSPACE = Path.cwd()

# 只读工具白名单：探索 agent 可用的全部工具（改文件类一律拒绝）
_READ_ONLY = frozenset({"grep", "read_file", "glob_files", "find_function", "list_dir"})

_SYSTEM = (
    "你是代码探索员，在一个代码仓库里定位与目标相关的代码。规则：\n"
    "0. 下方已附 workspace 文件树（已排除 .gitignore/构建产物）——先扫树：看到与目标相关的可疑文件"
    "【直接 read_file 查看】，无需先 list_dir 摸结构；树被截断时用 glob_files 补充。\n"
    "1. 只能调用给定工具（全部只读）；优先 grep 定位 → read_file/find_function 看实现。\n"
    "2. 每步少读：read_file 用 start_line/end_line 分段，不要整读大文件。\n"
    "3. 信息足够后【立即停止调用工具】，用要点输出总结：相关位置（文件:行号）、关键函数/类、"
    "与目标的关系、值得注意的细节。总结要具体（带行号与函数名），这是调用方唯一确定保留的内容。"
)

_RESULT_CAP = 6000   # 单次工具结果进探索上下文/嫁接记录的字符上限（防投影膨胀）

# ---------- 文件树预注入（用户提案 2026-09-09：省掉探索 agent 的 list_dir 开局步） ----------
# 排除 = 硬清单（.git/__pycache__ 等）+ .gitignore（用户中途补充 2026-09-09：文件树须排除 gitignore 项，
# 与 glob_files 同语义）。谓词实现是 fs_tools._make_gitignore_filter 的轻量复制（外置件自洽——动态模块名
# 不可跨文件 import，两文件同款语义各自持有）。
import fnmatch as _fnmatch
_EXCLUDE_DIRS = {".git", "__pycache__", "node_modules", ".venv", "venv", "dist", "build", ".idea", ".vscode", ".agt",
                 ".pytest_cache", ".claude"}
_TREE_MAX_LINES = 400   # 树行数硬上限（超出截断标注——探索 agent 用 glob_files 补）
_TREE_MAX_DEPTH = 5
_TREE_DIR_W, _TREE_FILE_W = 6, 12   # 每层目录/文件条目上限（防巨型子树吞噬全局预算）
_TREE_TTL = 60.0        # 树缓存秒（同轮并行 explore 共享，免重复遍历）
_TREE_CACHE = {"key": None, "ts": 0.0, "text": ""}
# 顶层目录优先序（教训 2026-09-09：固定预算下字母序后段的核心目录（src/）会被 .agent/probes
# 这类次重要目录挤出局——第一层按"源码目录优先"排，未列出的按字母序跟在后面）
_TOP_DIR_PRIORITY = ("src", "tools", "test", "tests", "docs", "examples", "scripts", ".agent")


def _gitignore_predicates(base: Path):
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
        return name == pat or _fnmatch.fnmatch(name, pat)

    def keep_dir(rel: str) -> bool:
        name = rel.rsplit("/", 1)[-1]
        if any(_seg_or_fnmatch(name, p) for p in pats):
            return False
        if any(_fnmatch.fnmatch(rel, p) for p in pats):
            return False   # 路径模式（如 .agent/wiki_queue）——name 级匹配漏、全路径补（fs_tools 谓词同款缺口，树场景实测 2026-09-09）
        if any(rel == p or _fnmatch.fnmatch(rel, p) for p in anchored):
            return False
        return True

    def keep_file(rel: str) -> bool:
        name = rel.rsplit("/", 1)[-1]
        segs = rel.split("/")
        if any(_seg_or_fnmatch(seg, p) for seg in segs for p in pats):
            return False
        if any(_fnmatch.fnmatch(rel, p) or _fnmatch.fnmatch(name, p) for p in pats):
            return False
        if any(rel == p or rel.startswith(p + "/") or _fnmatch.fnmatch(rel, p) for p in anchored):
            return False
        return True

    return keep_dir, keep_file


def workspace_tree(root: Path = None) -> str:
    """缩进树文本（目录/文件混合，硬清单+gitignore 过滤，行数/深度截断）。带 TTL 缓存。
    每层限宽（目录 ≤10、文件 ≤20，超出折行标注）——防 assets/ 这类巨型子树吞掉全局
    行数预算、把浅层主代码（如 src/agent.py）挤出局（首版实测教训 2026-09-09）。"""
    root = Path(root) if root else _WORKSPACE
    key = str(root)
    now = time.time()
    if _TREE_CACHE["key"] == key and now - _TREE_CACHE["ts"] < _TREE_TTL:
        return _TREE_CACHE["text"]
    keep_dir, keep_file = _gitignore_predicates(root)
    lines: list = []
    _DIR_W, _FILE_W = 10, 20   # 每层目录/文件条目上限

    def walk(d: Path, rel: str, depth: int):
        if depth > _TREE_MAX_DEPTH:
            return
        try:
            entries = [e for e in d.iterdir()]
        except OSError:
            return
        dirs = sorted((e for e in entries if e.is_dir() and e.name not in _EXCLUDE_DIRS),
                      key=lambda e: ((_TOP_DIR_PRIORITY.index(e.name) if e.name in _TOP_DIR_PRIORITY else 99), e.name.lower())
                      if depth == 0 else e.name.lower())
        files = sorted((e for e in entries if not e.is_dir()), key=lambda e: e.name.lower())
        pad = "  " * depth
        # 深度自适应限宽（教训 2026-09-09：浅层是主代码所在——src/ 直下 38 个 py，固定
        # 12 宽按字母序切会把 session/server 这类后段核心挤出局）；深层收紧防子树膨胀
        dw = 12 if depth == 0 else (8 if depth == 1 else _TREE_DIR_W)   # 根层目录全可见（导航价值最高）
        fw = 48 if depth <= 1 else _TREE_FILE_W   # src/ 直下 46 文件全列（46≤48；折行兜底）

        def visible(ent_list, keep, width):
            kept, skipped = [], 0
            for e in ent_list:
                r = f"{rel}/{e.name}" if rel else e.name
                if not keep(r):
                    continue
                if len(kept) >= width:
                    skipped += 1
                    continue
                kept.append((e, r))
            return kept, skipped

        vdirs, dskip = visible(dirs, keep_dir, dw)
        vfiles, fskip = visible(files, keep_file, fw)
        # 本层文件先输出、子目录后展开——浅层主代码（如 src/agent.py）不被巨型子树
        # （assets/ 等深度展开）吞掉全局行数预算（首版教训 2026-09-09：深度优先时
        # src/assets 先吃满 400 行，src 直下的 py 文件全部出局）
        for e, r in vfiles:
            if len(lines) >= _TREE_MAX_LINES:
                lines.append(pad + "…（全局截断：用 glob_files 查看更多）")
                return
            lines.append(pad + e.name)
        if fskip:
            lines.append(pad + f"…还有 {fskip} 个文件（glob_files 查看）")
        for e, r in vdirs:
            if len(lines) >= _TREE_MAX_LINES:
                lines.append(pad + "…（全局截断：用 glob_files 查看更多）")
                return
            # 独立 git 仓库短路（submodule 的 .git 是文件 / 嵌套完整 clone 的 .git 是目录——都短路）：
            # 只列名不展开——外部完整项目展开必吞预算且非本仓库代码（首版教训 2026-09-09：
            # coze-studio 吃满 400 行，src/ 整体出局）
            try:
                is_sub = (e / ".git").exists()
            except OSError:
                is_sub = False
            if is_sub:
                lines.append(pad + e.name + "/（子模块，未展开）")
                continue
            lines.append(pad + e.name + "/")
            walk(e, r, depth + 1)
        if dskip:
            lines.append(pad + f"…还有 {dskip} 个目录（list_dir 查看）")

    walk(root, "", 0)
    text = "\n".join(lines)
    _TREE_CACHE.update(key=key, ts=now, text=text)
    return text

# 嫁接锁（用户裁定 2026-09-09：常规用法是同一步并行多个 explore 各查不同目标）——
# 探索循环本身并行，只有 _seed_steps 嫁接段串行化：session.add_step→_emit_event
# 追加写 events.jsonl 无锁，并发嫁接可能行交错；锁内做 seed 逐条落盘，开销可忽略。
import threading
_SEED_LOCK = threading.Lock()


def _make_explore(agent):
    def explore(goal: str, max_steps: int = 8, budget_seconds: int = 120, model: str = "") -> str:
        """外置探索：把"找相关代码"的多轮 grep/read 过程外包给小上下文 react 循环（默认 utility 模型，
        便宜且不占你的步数），探索的原始工具调用记录自动嫁接进本轮上下文（可追溯），你直接拿摘要继续工作。
        何时用：需要 3 步以上搜索/阅读才能定位的探索（如"找到 X 功能的实现和调用链"）；
        单次 grep 能命中时直接自己调更省。返回=结构化摘要（不衰减）；嫁接步允许轮内衰减。
        并行用法：在【同一步】发起多个 explore（各查不同目标）即多路并行探索——比逐个串行调用快得多。

        goal: 探索目标——尽量具体（要找什么、在哪个模块、关注哪些方面）
        max_steps: 最多工具调用轮数（默认 8，上限 20）
        budget_seconds: 墙钟预算秒（默认 120；超限返回已完成部分）
        model: 探索用模型名（空=utility_model）"""
        max_steps = max(1, min(int(max_steps or 8), 20))
        budget_seconds = max(10, int(budget_seconds or 120))
        try:
            schemas = [s for s in agent._llm_tool_schemas()
                       if (s.get("function") or {}).get("name") in _READ_ONLY]
        except Exception as e:
            return f"[explore] 工具 schema 获取失败：{type(e).__name__}: {e}"
        if not schemas:
            return "[explore] 无可用只读工具（白名单 " + ", ".join(sorted(_READ_ONLY)) + " 均未注册）"

        if model:
            try:
                from llm_client import LLMClient
                llm = LLMClient(model_name=model, enable_thinking=False, max_retries=2)
                llm.call_recorder = agent.session.llm_calls.record
            except Exception as e:
                return f"[explore] 模型 {model} 初始化失败：{e}"
        else:
            llm = agent.utility_client()

        # 文件树预注入（用户提案 2026-09-09）：system 尾部附树（已排除 gitignore/构建产物）——
        # 探索 agent 免 list_dir 开局，扫树直读可疑文件。树生成失败不阻塞探索（降级无树）。
        try:
            tree = workspace_tree()
            system = _SYSTEM + ("\n\n## workspace 文件树\n```\n" + tree + "\n```" if tree else "")
        except Exception:
            system = _SYSTEM
        messages = [{"role": "system", "content": system},
                    {"role": "user", "content": f"探索目标：{goal}"}]
        seeds, summary, stop_reason = [], "", "步数上限"
        deadline = time.time() + budget_seconds
        try:
            for _ in range(max_steps):
                if time.time() >= deadline:
                    stop_reason = "时限"
                    break
                resp = llm.chat(messages, tools=schemas, scene="explore")
                tcs = resp.tool_calls or []
                if not tcs:
                    summary = (resp.content or "").strip()
                    stop_reason = "完成"
                    break
                messages.append({
                    "role": "assistant", "content": resp.content or None,
                    "tool_calls": [{"id": tc["id"], "type": "function",
                                    "function": {"name": tc["name"],
                                                 "arguments": json.dumps(tc["arguments"], ensure_ascii=False)}}
                                   for tc in tcs]})
                for tc in tcs:
                    name, args = tc["name"], dict(tc["arguments"] or {})
                    if name not in _READ_ONLY:
                        result = f"[explore 白名单拒绝] {name} 不可用（探索只读）"
                    else:
                        try:
                            result = str(agent._exec_tool(name, args))
                        except Exception as e:
                            result = f"[执行出错] {type(e).__name__}: {e}"
                    if len(result) > _RESULT_CAP:
                        result = result[:_RESULT_CAP] + "\n…[explore 截断]"
                    # reasoning 置空 + 标注来源：DeepSeek requires_reasoning_in_history 占位规则按空值自然处理
                    seeds.append({"tool": name, "args": args, "result": result,
                                  "reasoning": "[外置探索] " + (resp.reasoning or "")[:150]})
                    messages.append({"role": "tool", "tool_call_id": tc["id"], "content": result})

            if not summary:   # 预算耗尽没出总结——无工具快速收口一次（部分结果优于断链）
                try:
                    messages.append({"role": "user",
                                     "content": "预算已尽。立即停止调用工具，用要点总结已获得的信息（可含'未查完'部分）。"})
                    r2 = llm.chat(messages, tools=None, scene="explore")
                    summary = (r2.content or "").strip()
                except Exception:
                    pass
        except Exception as e:
            stop_reason = f"异常:{type(e).__name__}"

        grafted = 0
        if seeds:
            try:
                with _SEED_LOCK:   # 并行 explore 时串行化嫁接段（events.jsonl 追加写无锁）
                    agent._seed_steps(seeds)   # toollog + Step + add_step（自动落 events.jsonl）；此时 explore 调用步尚未归档 → 嫁接步自然在前
                grafted = len(seeds)
            except Exception as e:
                grafted = f"失败:{type(e).__name__}"

        head = (f"[外置探索·{stop_reason}] 嫁接 {grafted} 步工具调用进本轮上下文"
                f"（原始记录可追溯，随轮内逐步衰减）。\n要点摘要：\n")
        return head + (summary or "（无摘要——预算内未产出结论，建议自行降级手工探索）")
    return explore


def agt_register(ctx=None):
    """ctx: {"cwd", "agent", ...}——agent 由 chat.py 注册链注入；缺失时不注册（纯工具箱场景降级）。"""
    global _WORKSPACE
    if ctx and ctx.get("cwd"):
        _WORKSPACE = Path(ctx["cwd"])   # 文件树预注入的根（引擎视角真实 workspace）
    agent = (ctx or {}).get("agent")
    if agent is None:
        return []
    return [{
        "name": "explore", "func": _make_explore(agent),
        "group": "搜索定位", "version": 1,
        "params": {
            "goal": "探索目标（要找什么代码/信息——尽量具体：模块/关键词/关注点）",
            "max_steps": "最多工具调用轮数（默认 8）",
            "budget_seconds": "墙钟预算秒（默认 120）",
            "model": "探索用模型名（空=utility_model）",
        },
    }]
