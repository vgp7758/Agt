"""explore_tools.py —— 外置探索工具（spec s_54a1eb86 react 探索；s_7edebd56 投影层内插 + 断点续跑）。

探索过程（多轮 grep/read_file 定位代码）由本工具在小上下文 react 循环里完成。过程落盘【旁车】
sessions/<sid>/explore/<run_id>.jsonl（meta + 每步 assistant/tool 消息行 + done 终行），
不再 _seed_steps 嫁接（不写 toollog/events.jsonl）——seeds 进 agent.session._current._explore_seeds
（运行时属性），由 session 投影组装时【内插】到当前轮 steps 之后（下轮/读档自然消失：
历史里只剩"调用 explore + 结果摘要"两条）。进程中断（旁车无 done 终行）后可用
explore_resume(run_id) 从断点续跑；explore_list() 查看全部 run。

只读白名单：探索 agent 不能改文件——改动决策留给主 agent。
recent-file 语义澄清：_FILE_SNAP_TOOLS 只挂写工具（edit/write…），探索全只读——
内插消息组不进 rf_map 是与主循环一致的正确行为（rf 管"本轮变更文件速览"）。

需要 ctx["agent"]（chat.py → attach_script_tools → scan_script_tools 注入）；
无 agent 引用（纯工具箱构建/测试）时本组工具不注册（降级，不炸主程序）。
改完本文件用 /reload tools 热加载；改完同步种源 src/assets/tools_builtin/explore_tools.py。
"""
from __future__ import annotations

import json
import time
import uuid
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

# 旁车锁（用户裁定 2026-09-09：常规用法是同一步并行多个 explore 各查不同目标）——
# 2026-10-16 起（spec s_7edebd56）explore 过程不再 _seed_steps 嫁接（不写 toollog/events——
# 历史轮里只剩"调用 explore + 结果摘要"两条，读档回放干净），改【投影层内插】：seeds 进
# agent.session._current._explore_seeds（运行时属性，渲染在 session._explore_seeds_to_msgs）。
# 此锁串行化两段共享可变面：①seeds append（并行 explore 顺序拼接，与原嫁接顺序一致）；
# ②旁车 jsonl append（events 无锁 append 的教训——同文件并发写防行交错）。
# _seed_steps 函数本身不动（bg_task/service_exit 通知通道共用，agent.py）。
import threading
_SEED_LOCK = threading.Lock()


def _sidecar_dir(agent):
    """旁车目录：<session_dir>/explore/；session_dir 未绑定（新会话首轮 explore）→
    _ensure_session_dir() 现建（与 events/toollog 同目录约定）；建目录失败 → None（纯内存降级）。"""
    ses = getattr(agent, "session", None)
    sdir = getattr(ses, "session_dir", None)
    if not sdir and ses is not None:
        try:
            sdir = ses._ensure_session_dir()
        except Exception:
            return None
    if not sdir:
        return None
    d = Path(sdir) / "explore"
    try:
        d.mkdir(parents=True, exist_ok=True)
        return d
    except Exception:
        return None


class _Sidecar:
    """旁车 jsonl append 封装（spec s_7edebd56）：explore react 状态落盘
    sessions/<sid>/explore/<run_id>.jsonl——首行 meta、每步 assistant/tool 消息行、done 终行。
    文件写失败静默降级（ok=False 后不再尝试）——行为不劣于纯内存（无旁车即无续跑能力）。
    seq：tool 消息序号（seed 的 tool_call_id = exp-<run>-<seq> 用，跨 resume 单调不撞）。"""

    def __init__(self, agent, run_id: str, append: bool = False, seq0: int = 0):
        self.run_id, self.seq, self.ok = run_id, seq0, False
        d = _sidecar_dir(agent)
        self.path = (d / f"{run_id}.jsonl") if d else None
        if self.path is None:
            return
        try:
            if append:                 # 续写模式：文件已存在（存在性/完整性由调用方校验过）
                self.ok = True
            else:
                self.path.write_text("", encoding="utf-8")   # 新建清空
                self.ok = True
        except Exception:
            self.path = None

    def append(self, obj: dict):
        if not self.ok:
            return
        try:
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(obj, ensure_ascii=False) + "\n")
        except Exception:
            self.ok = False   # 降级纯内存（后续 append 跳过）


def _read_sidecar(path: Path):
    """读旁车 jsonl → (meta, msgs, done, truncated, tool_n)。
    损坏行（JSON 解析失败）→ truncated=True，从最后完整行截断（恢复拼用）。
    done：有无完成终行（无 = 半截，可续跑）。"""
    meta, msgs, done, truncated, tool_n = None, [], False, False, 0
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return None, [], False, True, 0
    for ln in text.splitlines():
        if not ln.strip():
            continue
        try:
            obj = json.loads(ln)
        except Exception:
            truncated = True
            break
        ev = obj.get("ev")
        if ev == "meta":
            meta = obj
        elif ev in ("assistant", "tool") and isinstance(obj.get("msg"), dict):
            msgs.append(dict(obj["msg"]))
            if ev == "tool":
                tool_n += 1
        elif ev == "done":
            done = True
    return meta, msgs, done, truncated, tool_n


def _new_llm(agent, model: str):
    """每次调用 new 独立 LLMClient（用户提案 2026-10-12：并行 explore 共用单例 client
    → 同 api_key 的并发请求在 provider 侧被限流排队——半串行；独立 client 各自带
    连接池，多 token 池起始位随机分散——并行 explore 各走各的 key，不叠在同一把锁上）。
    返回 (llm, 模型名)；失败返回 (None, 模型名)。"""
    from llm_client import LLMClient
    _m = model or getattr(agent, "utility_model", "") or agent.model_name
    try:
        llm = LLMClient(model_name=_m, enable_thinking=False, max_retries=2)
        llm.call_recorder = agent.session.llm_calls.record
        if len(llm.api_tokens) > 1:
            import random as _rnd
            llm._token_idx = _rnd.randrange(len(llm.api_tokens))
            llm.api_key = llm.api_tokens[llm._token_idx]
            llm._client = llm._openai_client()
        return llm, _m
    except Exception:
        return None, _m


def _build_system() -> str:
    """system（文件树预注入，用户提案 2026-09-09）：树生成失败不阻塞探索（降级无树）。"""
    try:
        tree = workspace_tree()
        return _SYSTEM + ("\n\n## workspace 文件树\n```\n" + tree + "\n```" if tree else "")
    except Exception:
        return _SYSTEM


def _react(agent, llm, schemas, messages, max_steps: int, deadline: float, sc: _Sidecar, seeds: list):
    """explore react 主循环（explore 与 explore_resume 共用，spec s_7edebd56）：
    messages/seeds 原地扩展；每步旁车 append（assistant/tool 消息行，恢复拼回用）；
    返回 (summary, stop_reason, steps_run)。旁车 assistant 行不带 reasoning_content
    （DeepSeek requires_reasoning_in_history 占位规则按原样；reasoning 只进 seed 供投影）。"""
    summary, stop_reason, steps_run = "", "步数上限", 0
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
            a_msg = {"role": "assistant", "content": resp.content or None,
                     "tool_calls": [{"id": tc["id"], "type": "function",
                                     "function": {"name": tc["name"],
                                                  "arguments": json.dumps(tc["arguments"], ensure_ascii=False)}}
                                    for tc in tcs]}
            messages.append(a_msg)
            sc.append({"ev": "assistant", "msg": a_msg})   # 旁车（断点恢复拼回 messages 用）
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
                # reasoning 置空 + 标注来源（[外置探索] 前缀在投影时挂 reasoning_content）
                seeds.append({"tool": name, "args": args, "result": result,
                              "reasoning": "[外置探索] " + (resp.reasoning or "")[:150],
                              "run": sc.run_id, "seq": sc.seq})
                t_msg = {"role": "tool", "tool_call_id": tc["id"], "content": result}
                messages.append(t_msg)
                sc.append({"ev": "tool", "msg": t_msg})
                sc.seq += 1
            steps_run += 1
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
    return summary, stop_reason, steps_run


def _attach_seeds(agent, seeds: list) -> bool:
    """seeds 进当前轮旁车（投影层内插数据源，spec s_7edebd56）：
    with _SEED_LOCK: agent.session._current._explore_seeds.extend(seeds)。
    仅运行时属性（Turn dataclass 字段；Turn 持久化走 events 重放，字段天然不落盘）
    → 下轮/读档自然消失。无 _current/异常 → 丢弃 seeds（返回 False，调用方标注[过程未投影]）。"""
    if not seeds:
        return True
    try:
        with _SEED_LOCK:
            cur = agent.session._current
            if cur is None:
                return False
            if getattr(cur, "_explore_seeds", None) is None:
                cur._explore_seeds = []
            cur._explore_seeds.extend(seeds)
        return True
    except Exception:
        return False


def _make_explore(agent):
    def explore(goal: str, max_steps: int = 8, budget_seconds: int = 120, model: str = "") -> str:
        """外置探索：把"找相关代码"的多轮 grep/read 过程外包给小上下文 react 循环（默认 utility 模型，
        便宜且不占你的步数），探索的原始工具调用过程【内插】进本轮上下文（仅当前轮可追溯；下轮
        自动只剩本调用与摘要两条），你直接拿摘要继续工作。过程同时落盘旁车（run 号见返回头）——
        进程中断后可 explore_resume(run_id) 从断点续跑。
        何时用：需要 3 步以上搜索/阅读才能定位的探索（如"找到 X 功能的实现和调用链"）；
        单次 grep 能命中时直接自己调更省。返回=结构化摘要（不衰减）；内插过程允许轮内衰减。
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
        llm, _m = _new_llm(agent, model)
        if llm is None:
            return f"[explore] 模型 {_m} 初始化失败"

        run_id = uuid.uuid4().hex[:8]
        sc = _Sidecar(agent, run_id)
        sc.append({"ev": "meta", "goal": goal, "model": _m, "max_steps": max_steps,
                   "budget": budget_seconds, "ts": time.time()})
        messages = [{"role": "system", "content": _build_system()},
                    {"role": "user", "content": f"探索目标：{goal}"}]
        seeds = []
        summary, stop_reason, _ran = _react(agent, llm, schemas, messages,
                                            max_steps, time.time() + budget_seconds, sc, seeds)
        sc.append({"ev": "done", "summary": summary, "stop_reason": stop_reason})

        grafted = _attach_seeds(agent, seeds)
        g_txt = (f"过程已内插本轮上下文（{len(seeds)} 次工具调用，随轮内逐步衰减；下轮只剩本调用与摘要）"
                 if grafted else "[过程未投影]")
        r_txt = "" if not sc.ok else f"；旁车 run:{run_id}（中断可 explore_resume 续跑）"
        head = f"[外置探索·run:{run_id}·{stop_reason}] {g_txt}{r_txt}。\n要点摘要：\n"
        return head + (summary or "（无摘要——预算内未产出结论，建议自行降级手工探索）")
    return explore


def _make_explore_resume(agent):
    def explore_resume(run_id: str) -> str:
        """续跑一个中断的外置探索（spec s_7edebd56）：读旁车 sessions/<sid>/explore/<run_id>.jsonl，
        把已完成步骤拼回 messages 从断点继续 react（剩余步数 = 原上限 - 已跑；预算按原值重给），
        每步继续 append 旁车，完成后写 done 终行 + 新增过程内插本轮上下文（与正常 explore 同路径）。
        旁车损坏（JSON 行解析失败）自动从最后完整行截断续跑；已完成（有 done 终行）的 run 拒绝续跑。
        不想继续的 run 忽略即可（旁车不投影进上下文，无副作用）。

        run_id: 要续跑的探索 run 号（explore/explore_list 返回头里的 run:xxxx，8 位短号）"""
        run_id = str(run_id or "").strip().lower()
        if run_id.startswith("run:"):
            run_id = run_id[4:].strip()
        sdir = getattr(getattr(agent, "session", None), "session_dir", None)
        path = (Path(sdir) / "explore" / f"{run_id}.jsonl") if sdir else None
        if not run_id or not path or not path.exists():
            return f"[explore_resume] 旁车文件不存在：run:{run_id or '（空）'}（explore_list 查看全部 run）"
        meta, msgs, done, truncated, tool_n = _read_sidecar(path)
        if meta is None:
            return f"[explore_resume] 旁车损坏（无 meta 行）：run:{run_id}（不可恢复，可手动删除该文件）"
        if done:
            return (f"[explore_resume] run:{run_id} 已完成（有 done 终行），无需续跑——"
                    f"goal：{str(meta.get('goal', ''))[:60]}")
        done_n = sum(1 for m in msgs if m.get("role") == "assistant")
        try:
            schemas = [s for s in agent._llm_tool_schemas()
                       if (s.get("function") or {}).get("name") in _READ_ONLY]
        except Exception as e:
            return f"[explore_resume] 工具 schema 获取失败：{type(e).__name__}: {e}"
        if not schemas:
            return "[explore_resume] 无可用只读工具（白名单均未注册）"
        llm, _m = _new_llm(agent, str(meta.get("model", "")))
        if llm is None:
            return f"[explore_resume] 模型 {_m} 初始化失败"
        # 拼 messages：meta→system+user 重建；各行→assistant/tool 消息（与断点前探索所见逐字一致）
        goal = str(meta.get("goal", ""))
        messages = [{"role": "system", "content": _build_system()},
                    {"role": "user", "content": f"探索目标：{goal}"}] + msgs
        max_steps = max(1, int(meta.get("max_steps", 8)) - done_n)
        budget_seconds = max(10, int(meta.get("budget", 120)))
        sc = _Sidecar(agent, run_id, append=True, seq0=tool_n)   # 续写旁车，seq 接着编号
        seeds = []
        summary, stop_reason, ran = _react(agent, llm, schemas, messages,
                                           max_steps, time.time() + budget_seconds, sc, seeds)
        sc.append({"ev": "done", "summary": summary, "stop_reason": stop_reason})
        grafted = _attach_seeds(agent, seeds)
        g_txt = f"新增过程已内插本轮上下文（{len(seeds)} 次工具调用）" if grafted else "[过程未投影]"
        t_txt = "（旁车有损坏行，已从最后完整行截断续跑）" if truncated else ""
        head = (f"[外置探索·续跑·run:{run_id}·{stop_reason}] 已在 {done_n} 步基础上续跑 {ran} 步{t_txt}；"
                f"{g_txt}。\n要点摘要：\n")
        return head + (summary or "（无摘要——预算内未产出结论，可再次 explore_resume 或自行降级手工探索）")
    return explore_resume


def _make_explore_list(agent):
    def explore_list() -> str:
        """列出本会话全部外置探索 run（旁车 sessions/<sid>/explore/*.jsonl，新→旧）：
        run_id / 状态（完成 | 半截(可续跑) | 半截(损坏行已截断) | 损坏）/ 已完成步数 / goal。
        用于中断恢复决策（explore_resume(run_id) 续跑）与历史追溯。
        无参数。"""
        sdir = getattr(getattr(agent, "session", None), "session_dir", None)
        d = (Path(sdir) / "explore") if sdir else None
        if not d or not d.is_dir():
            return "[explore_list] 本会话无外置探索旁车记录"
        try:
            files = sorted(d.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
        except Exception:
            files = []
        rows = []
        for f in files:
            meta, _msgs, done, truncated, tool_n = _read_sidecar(f)
            if meta is None:
                state, goal = "损坏", "?"
            elif done:
                state, goal = "完成", str(meta.get("goal", ""))
            elif truncated:
                state, goal = "半截(损坏行已截断，可续跑)", str(meta.get("goal", ""))
            else:
                state, goal = "半截(可续跑)", str(meta.get("goal", ""))
            rows.append(f"- run:{f.stem} [{state}] {tool_n}步 goal={goal[:60]}")
        if not rows:
            return "[explore_list] 本会话无外置探索旁车记录"
        return f"[explore_list] 共 {len(rows)} 个 run（新→旧）：\n" + "\n".join(rows)
    return explore_list


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
        "group": "搜索定位", "version": 2,
        "params": {
            "goal": "探索目标（要找什么代码/信息——尽量具体：模块/关键词/关注点）",
            "max_steps": "最多工具调用轮数（默认 8）",
            "budget_seconds": "墙钟预算秒（默认 120）",
            "model": "探索用模型名（空=utility_model）",
        },
    }, {
        "name": "explore_resume", "func": _make_explore_resume(agent),
        "group": "搜索定位", "version": 1,
        "params": {
            "run_id": "要续跑的探索 run 号（explore/explore_list 返回头里的 run:xxxx）",
        },
    }, {
        "name": "explore_list", "func": _make_explore_list(agent),
        "group": "搜索定位", "version": 1,
        "params": {},
    }]
