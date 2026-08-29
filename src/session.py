"""session.py —— 分层上下文引擎（完整原文不丢版）。

结构 Turn > Step > ToolCall（"每轮请求中的多轮工具调用分层管理"）：
  - 一轮用户请求 = 一个 Turn，内含若干 Step，每个 Step 是一次 LLM 调用，可带多个 ToolCall。
  - 喂给 LLM 的上下文 = system + 【窗口外各轮 summary 拼接】+ 近期若干轮原文(recent window)。
  - 完整原文永不丢：self.turns 不再被截断，超出近期窗口的旧 Turn 只把它的 summary
    拼进 global_summary 喂给模型，原文仍完整留在内存 + 存档里，可按需召回。
  - 每轮 finish 时生成该轮 summary（贴在该轮最后，作语义索引 + 窗口外摘要源）。
  - recall(query)：用关键词在全部历史里搜，召回匹配轮的完整上下文（默认不含 reasoning，contains_reasoning=True 时带上）。
  - 首轮自动命名（一句话总结）；每轮异步自动落盘；save/load 结构化持久化。

设计要点（延续前面的教训）：
  - reasoning 随每步存入 Step 并在近期窗口/当前轮回传（维持推理链连贯）；窗口外摘要、recall(默认)、单轮超 max_steps 截断时不带 reasoning。
  - 摘要源是该轮自带的 summary 字段，窗口外拼接便宜（纯字符串 join），超长才压缩并缓存。
  - 压缩阈值判定的 token 估算用【实测校准比率】而非写死的 chars/4：react 每次成功回包
    observe_llm_usage 喂入 usage，chars÷prompt_tokens 持续校准 _chars_per_token（跨 session
    落盘 ~/.agt/token_usage.jsonl）；实测 total 超 panic 立即紧急压缩、超 win 标记下轮重规划。
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import mimetypes
import os
import re
import shutil
import threading
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional, Callable

import config
from llm_client import LLMClient
from toollog import ToolLog, DETAIL_BASE, DETAIL_FLOOR
from llm_call_log import LLMCallLog
from mdrender import render_cli   # /recall 回显 answer 时渲染表格/代码块（独立模块，避免循环依赖）

_LOG = logging.getLogger("agt.session")  # 直接用标准 logging（不 import log.py，避免循环）；handler 由 agent 配置时挂到 agt root

# —— 当前轮 step 级投影策略（保思维链连贯；模型上下文窗口普遍够大，近若干步值得全量）——
GROUP_STEPS = 10        # 步分组大小：每 GROUP_STEPS 步一组，组内 limit 一致（byte-stable 利于前缀缓存）
FOLD_TARGET_RATIO = 0.75  # 折叠目标比例：轮边界计划与轮内保命阀共用（panic 触发即一次压回计划水位）
GRADUATE_BATCH_TURNS = 30  # 大档分批毕业：当前档超过此轮数时一次只升【前 N 轮】，近期轮保持 level1（保真）
GRADUATE_FORCE_TURNS = 60  # 卫生性强档阈值：当前档超过此轮数时，无窗口压力也分批升前 30 轮（防档1 无限膨胀——
                           # 8000 实例实测 64 轮档1 占 58.6%：窗口宽绰时压力循环永不触发，档1 失去"近期窗口"语义）
RECENT_FULL_STEPS = GROUP_STEPS   # 兼容旧引用（组号差≤1 = 当前组+上一组 ≈ 最近 1~2 组全量）
FULL_STEP_CAP_CHARS = 32000   # 全量步的单步上限（≈8000 token；超过则截断标注 call_id，可 get_tool_detail 取完整）
# <img>name</img> 标签：工具图片落盘后的占位（投影时按模型 vision 能力转 image_url 或文字占位）
_IMG_TAG_RE = re.compile(r"<img>([^<]+)</img>")
# recent-file 跟屁虫块（tool result content 尾部附加的文件快照）：_rf_in_msgs 量体积 /
# 毕业判定估算时剥离用（rf 是轮内易变项，不该推动升档/折叠等不可逆历史压缩——用户裁定 2026-08-29）
_RE_RF_BLOCK = re.compile(r"\n<recent-file[\s\S]*?</recent-file>")

# 会话存档放用户主目录：~/.agt/repos/<repo-hash>/sessions/。每个 repo 一棵目录树
# （sessions/ + 未来可加其它子目录），互相隔离。放包目录会在 pip 安装后写进
# site-packages（不可写/难找），故统一到 ~/.agt，与 models.json/settings.json 同惯例。
REPOS_DIR = Path.home() / ".agt" / "repos"
# 实测 token 用量流水（react 每次成功回包 observe_llm_usage 追加一条）：既是超窗观察日志，
# 也是 chars/token 校准比率的持久化源——新 session init 回读末尾同模型记录作比率初值。
TOKEN_USAGE_FILE = Path.home() / ".agt" / "token_usage.jsonl"
# 旧位置（用于一次性自动迁移；SESSIONS_DIR 同时保留作 legacy 别名供 commands.py 等 import）：
SESSIONS_DIR = Path.home() / ".agt" / "sessions"                              # 上一版 ~/.agt/sessions/<hash>/
_LEGACY_SESSIONS_DIR = Path(__file__).resolve().parent.parent / "sessions"   # 开发期项目根（pip 装后不存在）


def _repo_key(workspace) -> str:
    """把工作区路径转成可读目录名：斜杠 / 和 \\ 替换为 '-'。
    例：C:\\Users\\vgp77\\Projects\\Agt → C:-Users-vgp77-Projects-Agt
    可读性好（一眼看出是哪个 repo），且文件系统安全（无斜杠/冒号）。"""
    p = str(Path(workspace).resolve())
    return p.replace("\\", "-").replace("/", "-").replace(":", "-")


def _repo_hash(workspace) -> str:
    """兼容旧引用：仍返回 hash（_repo_key 迁移后不再使用，保留给旧代码 import）。"""
    return hashlib.sha1(str(Path(workspace).resolve()).encode("utf-8")).hexdigest()[:12]


def _write_origin(workspace) -> None:
    """在 repo 目录写 _origin.txt（记录原始 cwd），供后续 hash→fixed-cwd 迁移用。"""
    try:
        d = REPOS_DIR / _repo_key(workspace)
        d.mkdir(parents=True, exist_ok=True)
        origin = d / "_origin.txt"
        if not origin.exists():
            origin.write_text(str(Path(workspace).resolve()), encoding="utf-8")
    except Exception:
        pass


_MIGRATED_HASH = False   # 进程级标志：hash→fixed-cwd 批量迁移只跑一次


def _migrate_all_hash_dirs() -> None:
    """启动时扫描 ~/.agt/repos/ 下所有文件夹：
    文件夹名不含 '-' 的（hash 名）→ 读 _origin.txt 获取 cwd → 改名为 <fixed-cwd>。
    目标已存在则合并内容（把旧目录的子目录移过去）。只跑一次（进程级标志）。"""
    global _MIGRATED_HASH
    if _MIGRATED_HASH:
        return
    _MIGRATED_HASH = True
    try:
        if not REPOS_DIR.exists():
            return
        for d in REPOS_DIR.iterdir():
            if not d.is_dir():
                continue
            name = d.name
            # hash 名是 12 位十六进制，不含 '-'
            if "-" in name:
                continue   # 已经是 fixed-cwd 命名，跳过
            # 读 _origin.txt 获取原始 cwd
            origin = d / "_origin.txt"
            if not origin.exists():
                continue   # 没有 _origin.txt，无法迁移
            cwd = origin.read_text(encoding="utf-8").strip()
            if not cwd:
                continue
            new_name = _repo_key(cwd)
            if new_name == name:
                continue   # 名字没变（不应该，但兜底）
            target = REPOS_DIR / new_name
            if not target.exists():
                d.rename(target)
                _LOG.info("repo 迁移：%s → %s", name, new_name)
            else:
                # 目标已存在：合并内容
                import shutil
                for item in d.iterdir():
                    dst = target / item.name
                    if dst.exists():
                        if item.is_dir():
                            shutil.copytree(item, dst, dirs_exist_ok=True)
                        else:
                            shutil.copy2(item, dst)
                    else:
                        item.rename(dst)
                shutil.rmtree(d, ignore_errors=True)
                _LOG.info("repo 合并迁移：%s → %s（内容已合并）", name, new_name)
    except Exception as e:
        _LOG.warning("repo 目录批量迁移失败：%s", e)


def _repo_sessions_dir(workspace) -> Path:
    """该工作区的会话根目录：~/.agt/repos/<fixed-cwd>/sessions/。每个 repo 互相隔离。
    首次访问时把旧位置的扁平存档一次性整体迁移成新文件夹结构。"""
    _migrate_all_hash_dirs()   # 扫描所有 hash 目录→fixed-cwd（一次性，进程级标志）
    _write_origin(workspace)   # 写 _origin.txt（供未来迁移用）
    k = _repo_key(workspace)
    d = REPOS_DIR / k / "sessions"
    d.mkdir(parents=True, exist_ok=True)
    _migrate_all_legacy()
    _migrate_flat_to_folder(d)  # 扁平→文件夹迁移
    return d


def _timestamp_dir_name(ts: float) -> str:
    """把创建时间戳格式化成文件夹名 YYYYMMDD_HHMMSS（文件系统安全、可排序、可读）。"""
    return time.strftime("%Y%m%d_%H%M%S", time.localtime(ts))


def _ts_from_dirname(name: str) -> Optional[float]:
    """从文件夹名 YYYYMMDD_HHMMSS 或 YYYYMMDD_HHMMSS_N 解析回时间戳。失败返回 None。"""
    try:
        # 先尝试完整格式（带冲突后缀 _N）
        m = re.match(r"^(\d{8}_\d{6})(?:_\d+)?$", name)
        if not m:
            return None
        t = time.strptime(m.group(1), "%Y%m%d_%H%M%S")
        return time.mktime(t)
    except Exception:
        return None


def _new_session_dir(workspace, created_ts: float) -> Path:
    """为一个新 session 创建以时间戳命名的专属文件夹并返回路径。
    同秒并发冲突时尾部追加 _2/__3（极少见，进程内串行 + 秒级粒度足够）。"""
    base = _repo_sessions_dir(workspace) / _timestamp_dir_name(created_ts)
    if not base.exists():
        base.mkdir(parents=True, exist_ok=True)
        return base
    # 同秒冲突：追加 _2/_3… 直到不撞
    for i in range(2, 999):
        cand = base.with_name(f"{base.name}_{i}")
        if not cand.exists():
            cand.mkdir(parents=True, exist_ok=True)
            return cand
    return base  # 兜底（999 个同名几乎不可能）

def repo_memories_dir(workspace) -> Path:
    """该工作区的【长期记忆】目录：~/.agt/repos/<fixed-cwd>/memories/。与 sessions/ 同根，互相隔离。
    供 longterm_memory.LongTermMemory 使用；不触发 sessions 的 legacy 迁移。"""
    _migrate_all_hash_dirs()
    _write_origin(workspace)
    d = REPOS_DIR / _repo_key(workspace) / "memories"
    d.mkdir(parents=True, exist_ok=True)
    return d


def repo_plans_dir(workspace) -> Path:
    """该工作区的【计划】目录：~/.agt/repos/<fixed-cwd>/plans/。与 sessions/memories 同根、互相隔离。
    每个计划一个 <plan_id>.json 文件，跨 session 共享（plan_id 存在 session 的 extra_state 里）。
    供 plan_tools 使用；不触发 sessions 的 legacy 迁移。"""
    _migrate_all_hash_dirs()
    _write_origin(workspace)
    d = REPOS_DIR / _repo_key(workspace) / "plans"
    d.mkdir(parents=True, exist_ok=True)
    return d


def repo_images_dir(workspace) -> Path:
    """该工作区的【工具图片】目录：~/.agt/repos/<fixed-cwd>/images/。工具返回的图片落盘于此，
    消息里用 <img>name</img> 标签引用（base64 不进存档）。repo 级（不绑 session），
    供视觉子 agent 跨 session 引用同一张图。"""
    _migrate_all_hash_dirs()
    _write_origin(workspace)
    d = REPOS_DIR / _repo_key(workspace) / "images"
    d.mkdir(parents=True, exist_ok=True)
    return d


_ALL_MIGRATED = False   # 进程级标志：全量迁移只跑一次
_MIGRATED_FLAT_TO_FOLDER = False  # 进程级标志：扁平→文件夹迁移只跑一次


def _migrate_flat_to_folder(sessions_dir: Path) -> None:
    """把扁平结构的 sessions（<name>.json + <name>.events.jsonl + ...）迁移到文件夹结构（<timestamp>/meta.json + ...）。
    每个 session 的 timestamp 从 meta.json 的 created_at 或 saved_at 字段来；无则用文件 mtime。
    增量迁移：只处理还没有对应文件夹的扁平文件，已迁移的跳过。"""
    global _MIGRATED_FLAT_TO_FOLDER
    if _MIGRATED_FLAT_TO_FOLDER:
        return
    _MIGRATED_FLAT_TO_FOLDER = True
    
    if not sessions_dir.exists():
        return
    
    # 扫描所有 *.json 文件（session 元信息）——增量迁移，不因已有文件夹就跳过
    json_files = [f for f in sessions_dir.glob("*.json") if f.stem != "_origin"]
    if not json_files:
        return
    
    for jf in json_files:
        try:
            name = jf.stem
            if name == "_origin":
                continue
            
            # 收集相关文件
            events_old = sessions_dir / f"{name}.events.jsonl"
            toollog_old = sessions_dir / f"{name}.toollog.jsonl"
            llm_calls_old = sessions_dir / f"{name}.llm_calls.jsonl"
            log_old = sessions_dir / f"{name}.log"
            
            # 读 meta.json 获取 created_at 或 saved_at
            data = json.loads(jf.read_text(encoding="utf-8"))
            ts = data.get("created_at") or data.get("saved_at")
            if not ts:
                # 用文件 mtime 兜底
                ts = jf.stat().st_mtime
            
            # 创建新文件夹
            new_dir = sessions_dir / _timestamp_dir_name(ts)
            if new_dir.exists():
                # 同秒冲突：追加 _2/_3...
                for i in range(2, 99):
                    cand = sessions_dir / f"{_timestamp_dir_name(ts)}_{i}"
                    if not cand.exists():
                        new_dir = cand
                        break
            
            new_dir.mkdir(parents=True, exist_ok=True)
            
            # 移动文件
            shutil.copy2(jf, new_dir / "meta.json")
            if events_old.exists():
                shutil.copy2(events_old, new_dir / "events.jsonl")
            if toollog_old.exists():
                shutil.copy2(toollog_old, new_dir / "toollog.jsonl")
            if llm_calls_old.exists():
                shutil.copy2(llm_calls_old, new_dir / "llm_calls.jsonl")
            if log_old.exists():
                shutil.copy2(log_old, new_dir / "log.log")
            
            # 补全 meta.json 的 created_at 字段（旧格式缺失）
            meta_path = new_dir / "meta.json"
            if "created_at" not in data:
                data["created_at"] = ts
            if "name" not in data:
                data["name"] = name
            data["saved_at"] = int(time.time())
            meta_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            
        except Exception as e:
            pass  # 单 session 迁移失败不影响其他
    
    # 清理旧扁平文件（迁移成功后）
    for ext in [".json", ".events.jsonl", ".toollog.jsonl", ".llm_calls.jsonl", ".log"]:
        for f in sessions_dir.glob(f"*{ext}"):
            try:
                f.unlink()
            except Exception:
                pass


def _migrate_all_legacy() -> None:
    """一次性把旧位置的存档搬到 ~/.agt/repos/<hash>/sessions/。
    两处旧源：项目根 sessions/<hash>/（开发期）、~/.agt/sessions/<hash>/（上一版结构）。
    每个 hash 目标为空才迁（copy 不删源），避免覆盖新存档；旧目录可手动清理。"""
    global _ALL_MIGRATED
    if _ALL_MIGRATED:
        return
    _ALL_MIGRATED = True
    try:
        for legacy_root in (_LEGACY_SESSIONS_DIR, SESSIONS_DIR):
            if not legacy_root.exists():
                continue
            for legacy_hash_dir in legacy_root.iterdir():
                if not legacy_hash_dir.is_dir():
                    continue
                target = REPOS_DIR / legacy_hash_dir.name / "sessions"
                _migrate_one(legacy_hash_dir, target)
    except Exception:
        pass  # 迁移失败绝不影响正常读写


def _migrate_one(legacy_dir: Path, target: Path) -> None:
    """把 legacy_dir 的 *.json + _origin.txt 搬到 target（目标为空才迁）。"""
    try:
        if any(target.glob("*.json")):
            return  # 目标已有存档，不动
        old_files = list(legacy_dir.glob("*.json"))
        if not old_files:
            return
        target.mkdir(parents=True, exist_ok=True)
        for f in old_files:
            shutil.copy2(f, target / f.name)
        origin = legacy_dir / "_origin.txt"
        if origin.exists():
            shutil.copy2(origin, target / "_origin.txt")
    except Exception:
        pass

GLOBAL_SUMMARY_CAP = 2000  # 窗口外 summary 拼接超过这么多字就再压缩一次

# 文件名安全字符：保留字母数字下划线 + 中文，其余替成 _
_NAME_SAFE_RE = re.compile(r"[^\w一-鿿]")


@dataclass
class ToolCall:
    call_id: str = ""   # 在 session.toollog 的 id（c1/c2/…）；完整 name/arguments/result 存 toollog，组装上下文时按 id 召回


@dataclass
class Step:
    reasoning: str = ""
    tool_calls: list = field(default_factory=list)  # list[ToolCall]
    preceding_hint: str = ""     # 该步之前插入的"用户中途补充"(user 消息，带标签)，随本步滚入历史、不每步复读
    file_snapshots: dict = field(default_factory=dict)  # {call_id: {path,version,text}} 运行时填充、不持久化


# 中途插话的标签：明确标注"非新一轮"，避免被模型/未来逻辑当成新 turn 的 user 输入
_MIDTURN_TAG = "📨〔用户中途补充，非新一轮〕\n"

# 中断轮的 answer 标注集合（abort/start_turn 防御写入；resume_interrupted/前端渲染据此识别）
# 注："（被用户中断）" 是旧 KeyboardInterrupt 路径的文案（已统一为"（被用户停止）"），保留兼容历史存档
_INTERRUPT_MARKS = ("（中断，本轮未完成）", "（被中断）", "（被用户停止）", "（被用户中断）")


def _is_interrupt_mark(answer: str) -> bool:
    """answer 是否为中断标注——前缀匹配（start_turn 归档的新文案带原因后缀
    "（中断，本轮未完成——XXX: ...）"，精确 in 集合会漏判 → resume 拒绝恢复）。"""
    a = (answer or "").strip()
    return any(a.startswith(m) for m in _INTERRUPT_MARKS)


@dataclass
class Turn:
    user_message: str
    images: list = field(default_factory=list)       # list[str] 用户附带的图片(data URL)，多模态用
    snapshot_sha: str = ""                           # 该轮发送前的工作区快照(检查点回溯用)
    steps: list = field(default_factory=list)        # list[Step]
    answer: str = ""
    answer_reasoning: str = ""                       # 最终回答那步的 reasoning_content（GLM 等要求回传）
    summary: str = ""                                # 该轮的一句话摘要（finish 时生成，贴在该轮最后）
    recap: str = ""                                  # 一句话 recap（turn_end 异步生成：队友看板 + fc 折叠摘要行）


def _eval_assembly_workflow(name: str, session) -> str:
    """assembly 清单 workflow 项求值（原 multiagent._build_subagent_system 的执行核心）：
    .agent/workflows/ 找同名工作流执行（入参 {prompt: 当前 user_message, agent_id}），
    返回 result 文本。找不到/执行失败返回空串（调用方跳过该段，不炸投影）。"""
    try:
        from workflow import scan_workflows, execute
    except Exception:
        return ""
    for it in scan_workflows(session.workspace):
        if it.get("name") == name and it.get("canvas") is not None and not it.get("error"):
            try:
                prompt = ""
                if session._current is not None:
                    prompt = session._current.user_message or ""
                result = execute(it["canvas"], {"prompt": prompt,
                                                "agent_id": getattr(session, "_asm_agent_id", "")},
                                 tools=session._asm_workflow_tools,
                                 llm=getattr(session, "utility_llm", None) or session.llm,
                                 workspace=session.workspace)
                return (result or "").strip()
            except Exception as e:
                _LOG.warning("assembly workflow 项 '%s' 执行失败，跳过：%s", name, e)
                return ""
    _LOG.warning("assembly workflow 项 '%s' 未找到（.agent/workflows/），跳过", name)
    return ""


def _interp_funcs(text: str) -> str:
    """把文本里的 {func:name()} 占位替换成模板函数结果（白名单 FUNC_REGISTRY）。
    未注册名 → 保留原占位（不炸装配）；已注册但返回空串 → 替换为空（load_remote_instances
    无连接时静默不注入的设计依赖此语义——空结果≠失败）。声明投影（load_agents 等）每次
    build 重读——create_agent 后立即派活（高频场景）当轮生效；轮内编辑声明破缓存属低频可接受代价。"""
    import re as _re
    def _rep(m):
        from agent_config import FUNC_REGISTRY
        name = m.group(1).strip().rstrip("()").strip()
        fn = FUNC_REGISTRY.get(name)
        if fn is None:
            return m.group(0)          # 未注册：保留占位（提示声明写错了）
        try:
            return str(fn() or "")     # 已注册：空串也替换（无连接=不注入）
        except Exception:
            return m.group(0)          # 执行异常：保留占位（不炸装配）
    return _re.sub(r"\{func:([^}]+)\}", _rep, text)


def _eval_assembly_tool(item: dict, session) -> str:
    """assembly 清单 tool: 项求值（通用）：调工具箱里已注册的工具，结果注入。
    项格式（multiagent._parse_tool_expr 解析）：tool_name + tool_args（单个字符串实参）。
    工具查找顺序：agent 工具箱 → LIGHT_TOOLS；参数按工具签名智能分派——单参工具直接传，
    read_file/concat_files/grep 等常用工具按名映射到其主参数。失败/空返回空串（跳过）。"""
    tname = str(item.get("tool_name") or "").strip()
    targs_raw = str(item.get("tool_args") or "").strip().strip('"').strip("'")
    if not tname:
        return ""
    tools = session._asm_workflow_tools
    from real_tools import LIGHT_TOOLS as _LT
    box = tools if (tools is not None and tname in tools) else _LT
    if tname not in box:
        _LOG.warning("assembly tool: 项的工具 '%s' 未在工具箱中找到，跳过", tname)
        return ""
    # 主参数名映射：常用装配工具的接收参数（工具箱里查 schema 的第一个 required 参数最通用，
    # 但手写映射更稳——read_file(path)/concat_files(pattern)/dir_outline(path) 等主参一目了然）
    _PRIMARY = {"read_file": "path", "concat_files": "pattern", "dir_outline": "path",
                "list_dir": "path", "grep": "pattern", "read_skill": "name",
                "wiki_read": "title", "wiki_search": "query"}
    kwargs = {}
    if targs_raw:
        pname = _PRIMARY.get(tname)
        if pname:
            kwargs[pname] = targs_raw
        else:
            # 未知工具：按 schema 的第一个必填参数名传（尽力而为）
            try:
                t = box._tools.get(tname)
                req = (t.schema.get("function", {}).get("parameters", {}).get("required") or [])
                if req:
                    kwargs[req[0]] = targs_raw
            except Exception:
                pass
    if not kwargs:
        kwargs = {}
    try:
        out = box.call(tname, kwargs)
        out = str(out or "").strip()
        return out[:64_000]
    except Exception as e:
        _LOG.warning("assembly tool:%s(%s) 执行失败，跳过：%s", tname, targs_raw, e)
        return ""


class Session:
    def __init__(self, system: str, llm: Optional[LLMClient] = None,
                 recent_window_turns: int = 4, max_steps_per_turn: int = 80,
                 workspace=None, session_dir=None, current_turn_only: bool = False):
        self.system = system
        # 复用模式投影开关（子 Agent agent_prompt(reuse=True)）：True 时历史轮一律不投影，
        # 只投影 system + 任务指引 + 当前进行中的轮 + tail ambient。历史轮仍完整归档在
        # turns/落盘（可 agent_query_events / recall 查）——session 积累、投影隔离。
        self.current_turn_only = current_turn_only
        # 上下文装配开关（assembly DSL）：{段名: bool}，缺省=True（全装）。
        # 段名：rules / history / hooks / tail / ltm（system/user_message/steps 恒装不可关）。
        # 子 Agent 的 .agent/agents/<name>.md frontmatter 声明 + agent_prompt 参数覆盖，
        # current_turn_only 时 history 强制关（交集语义）。
        self.assembly: dict = {}
        # assembly DSL v2：有序装配清单（[{kind: seg|file|dir|cmd|workflow|text, ...}]）。
        # None = 默认清单（=历史版硬编码投影顺序，见 _DEFAULT_ASSEMBLY_PLAN）；
        # 段顺序即装配顺序，未列出的可关段不装；动作项由 _asm_action_msgs 求值。
        self.assembly_plan: Optional[list] = None
        self._assembly_once_cache: dict = {}   # once 时机动作项的求值缓存 {key: str}
        # assembly workflow 项求值用的工具引用（Agent 构造后注入；None=该工作流内 plugin 节点不可用）
        self._asm_workflow_tools = None
        self._asm_agent_id: str = ""
        # hooks 默认开关：主 Agent 默认开（before_turn 检索等）；子 Agent 未显式声明装配时
        # 子 Agent 构造代码置 False（避免每次派活重跑 before_turn 检索）。_run_hooks 读它。
        self.hooks_default_on: bool = True
        # 本 agent 声明的钩子清单（assembly DSL v2 的 hooks: 段）{hook位置: [{kind,value,async...}]}
        # None = 未声明（回退旧 workflow meta.hook 扫描路径，兼容期）。Agent 构造后由装配代码 set。
        self.hook_specs: Optional[dict] = None
        self.llm = llm or LLMClient(enable_thinking=False, temperature=0.3)
        # 辅助模型引用（Agent 注入 utility_client()；None=跟随主 llm）：轮摘要/摘要压缩/会话命名等
        # session 内的 LLM 短调用统一走它——除 react 外场景默认 utility_model 的约定覆盖到 session 层。
        self.utility_llm: Optional[LLMClient] = None
        self.recent_window_turns = recent_window_turns
        self.max_steps_per_turn = max_steps_per_turn  # 0/None = 不限
        self.workspace = Path(workspace) if workspace else Path.cwd()
        self.turns: list[Turn] = []
        self.global_summary = ""
        self.name: str = ""                           # session 自动命名（首轮一句话总结）
        self.created_at: float = time.time()          # session 创建时间戳（文件夹名 + meta.json 记录）
        # 预设 session_dir（子 agent 用：主 session/agents/<agent_id>/）；None=按时间戳现算
        self.session_dir: Optional[Path] = (Path(session_dir) if session_dir else None)
        self._current: Optional[Turn] = None          # 进行中的轮（run 期间）
        self._save_lock = threading.Lock()            # 异步落盘的并发保护
        self._name_lock = threading.Lock()            # _ensure_name / _ensure_name_early 并发保护
        self._summary_sig: tuple = ()                 # 窗口外 summary 缓存的失效签名
        self.extra_state: dict = {}                   # 附加运行时状态（Agent 经 _state_provider 收集：plan/自主模式等）
        self._state_provider: Optional[Callable[[], dict]] = None  # Agent 注册的附加状态收集回调
        self._system_extra_provider: Optional[Callable[[], str]] = None  # Agent 注册：返回动态 system 段（后台服务状态等）
        self._time_provider: Optional[Callable[[], str]] = None  # Agent 注册：返回实时时间串（tail 每步注入，感知时段）
        # —— 长期记忆注入 provider（Agent 注册；两类机制不同，见 longterm_memory.py）——
        self._ltm_static_provider: Optional[Callable[[], str]] = None    # 静态层：semantic 事实 + procedural 标题（每轮始终注入）
        self._ltm_episodic_provider: Optional[Callable[[str], str]] = None  # 情境层：按当前问题召回 episodic（每轮按需注入）
        self._plan_provider: Optional[Callable[[], str]] = None  # 当前活动计划块（Agent 注册；加入计划后每轮注入 SYSTEM，退出后返回空）
        self._spec_provider: Optional[Callable[[], str]] = None  # 当前活动 spec 块（Agent 注册；draft/committed/rejected 态注入，approved/无返回空）
        self._task_guidance_provider: Optional[Callable[[], str]] = None  # 任务指引(AGENTS.md/rules/skills/子Agent)：每轮重读，紧跟 system 之后
        self._log_handler = None  # agent 注册的日志 handler（duck typing）；_ensure_name 时通知它 flush 缓冲并切到 <name>.log
        self.toollog = ToolLog()  # 工具调用完整详情库：ToolCall 只存 call_id，组装上下文时按 id 召回 + 按步距衰减摘要
        self.llm_calls = LLMCallLog()  # LLM 调用流水（可观测性）：每次调用追加一条，供 /stats 聚合
        self._event_path = None   # 事件日志路径 <name>.events.jsonl；None 时事件 buffer 在内存（name 未就绪）
        self._event_buffer: list[dict] = []  # name 就绪前缓冲的事件（turn_start/step/snapshot/...）
        # —— 分档上下文投影（provider 设 max_effective_context_window 才启用，否则走原 recent_window+summary）——
        self.max_effective_context_window = getattr(self.llm, "max_effective_context_window", None)
        self._detail_base = None   # 惰性缓存（detail_base property；/config / switch_model / /context 时失效重读）
        self.max_level = config.load_max_level()
        self._tier_boundaries: list[int] = []                    # 已毕业的 turn 索引边界，如 [5,10]
        self._frozen_renders: dict[int, tuple[int, list]] = {}   # turn_idx -> (level, msgs) 冻结渲染缓存
        self._last_fold_count: int = 0   # 最近一次分档 build 的折叠轮数（to_history 用它折叠前端历史）
        self._planned_fold: int = 0      # 轮边界折叠计划（start_turn 时算好折到 75%；轮内 _build 以它为起点，不再轮内折叠）
        self._planned_graduates: int = 0 # 轮边界毕业计划（start_turn 时算好升几档；轮内 _build 以它为起点，不再轮内升档）
        # —— 实测 token 校准（react 每次成功回包 observe_llm_usage 喂入）——
        # _estimate_tokens 的除数由此取代写死的 chars/4（中文 ≈1.5 字/token，chars/4 可低估 2~3 倍）
        self._chars_per_token: float = 4.0    # 实测字符/token 比率（EMA 平滑；初值 4=旧行为）
        self._over_window_mark: bool = False  # 实测 total 超 win（未超 panic）标记，下轮 start_turn 消费记日志
        self._tools_schema_chars: int = 0     # 当前请求的 tools schema 字符数（agent.run 每步更新）
                                              # ——校准分子含它（observe_llm_usage 的 extra_chars），
                                              #   估算分子必须同口径（否则系统性少算 schema token，
                                              #   折叠计划"以为达标"实超窗；本次排查实证：目标
                                              #   400K×0.75 正确，但估算漏 schema 压到 297K 就停、
                                              #   实际发出去 412K）
        self._load_calibration()              # 回读 ~/.agt/token_usage.jsonl 末尾同模型记录作初值（跨 session 校准）
        # —— 投影分段统计（真实装配时顺手记录，/context 直接读——见 messages_for_llm 尾部）——
        # None=本进程还没跑过投影（projection_breakdown 回退现算）；否则 {"sections":[...], ts, turn, step, ...}
        self._proj_stats: Optional[dict] = None
        self._hist_marks: Optional[list] = None   # 装配进行中的 history 子段标记 [(name, 段内偏移, meta)]（临时态）
        # 语义召回层（build_agent 注入；None=未配 embed → recall 退回子串）
        self.vec_store = None

    # ========== 步距衰减基数（显式配置 > 窗口推导 > 1500） ==========
    @property
    def detail_base(self) -> int:
        """步距衰减的档 1 基数（字/步）：档 N 上限 = base >> (N-1)。
        优先级：settings.json 显式 detail_base > 按 max_effective_context_window 推导 > 1500。
        推导公式 base = clamp(win × 0.00375, 600, 6000)——0.00375 恰使 400K 窗口=1500
        （主流配置行为不变）；600K→2250、900K→3375、60K→600。
        缓存 _detail_base：/config、switch_model（窗口变）、/context（直改 settings.json 后）
        各自失效重读。此前的坑：消费点混用 toollog from-import 绑定值（set_detail_params
        改模块变量不更新副本）与运行时属性访问，显式配置在部分路径永不生效。"""
        if self._detail_base is None:
            explicit = config.load_detail_base_opt()
            if explicit:
                self._detail_base = explicit
            elif self.max_effective_context_window:
                self._detail_base = max(600, min(6000, int(self.max_effective_context_window * 0.00375)))
            else:
                self._detail_base = 1500
        return self._detail_base

    def invalidate_detail_base(self):
        """失效 base 缓存（配置变化时调用：/config detail_base、switch_model 窗口变、
        /context 入口——兜底直改 settings.json 的场景）。冻结渲染随 key 自动失效。"""
        self._detail_base = None

    # ========== 投影分段估算（/context 诊断用，只读） ==========
    def projection_breakdown(self) -> dict:
        """分段统计：优先返回【真实装配时顺手记录】的 _proj_stats（live——真实发给模型的口径，
        含 ts/turn/step 元信息）；无缓存（本进程还没跑过投影）才现算兜底（重算一遍段函数）。
        返回 {sections: [{name, msgs, chars, tokens, meta}], total_tokens, total_chars, source?}。"""
        if self._proj_stats and self._proj_stats.get("sections"):
            return dict(self._proj_stats)   # 浅拷贝：调用方改动不污染缓存
        out = {"sections": [], "total_tokens": 0, "total_chars": 0}

        def _add(name: str, msgs: list, meta: str = ""):
            chars = sum(len(m.get("content") or "") if isinstance(m.get("content"), str)
                        else sum(len(b.get("text", "")) for b in m["content"] if isinstance(b, dict))
                        for m in msgs if m.get("content"))
            t = self._estimate_tokens(msgs)
            out["sections"].append({"name": name, "msgs": len(msgs), "chars": chars,
                                    "tokens": t, "meta": meta})
            out["total_tokens"] += t
            out["total_chars"] += chars

        plan = self.assembly_plan or self._DEFAULT_ASSEMBLY_PLAN
        for item in plan:
            kind = item.get("kind")
            if kind == "seg":
                name = item.get("name")
                if self.current_turn_only and name in ("history", "ltm"):
                    continue
                if name == "system":
                    _add("system(人设+环境)", self._seg_msgs_system())
                elif name == "rules":
                    _add("rules(AGENTS+规则+技能)", self._seg_msgs_rules())
                elif name == "history":
                    mode = item.get("mode")
                    if mode == "tiered" and not self.max_effective_context_window:
                        mode = "full"
                    if (mode or ("tiered" if self.max_effective_context_window else "window")) == "tiered":
                        fc = self._last_fold_count
                        if fc > 0:
                            _add(f"折叠摘要({fc}轮)", [{"role": "system", "content": self._ambient(self._folded_summary(fc))}],
                                 meta=f"最早{fc}轮折叠为结构摘要，原文可recall")
                        # 分组按投影顺序（老→新）：已折叠超深档（raw>max_level，最老）→ 档4→…→档1。
                        # 段序与 messages 里的实际出现顺序一致（最老的最靠前），读表即读投影。
                        fold_on = config.load_fold_deep_tools()
                        fold_msgs, fold_turns = [], 0
                        lv_msgs: dict[int, list] = {}
                        lv_turns: dict[int, int] = {}
                        for i in range(fc, len(self.turns)):
                            if fold_on and self._raw_tier_level(i) > self.max_level:
                                fold_msgs.extend(self._render_turn_frozen(i))
                                fold_turns += 1
                            else:
                                lv = self._tier_level(i)
                                lv_msgs.setdefault(lv, []).extend(self._render_turn_frozen(i))
                                lv_turns[lv] = lv_turns.get(lv, 0) + 1
                        if fold_turns:
                            _add(f"已折叠超深档({fold_turns}轮)", fold_msgs,
                                 meta="工具调用折叠成一行标注，保留回复+reasoning原文")
                        for lv in sorted(lv_msgs, reverse=True):   # 档4(老)→档1(新)，与投影顺序一致
                            _add(f"档{lv}历史({lv_turns[lv]}轮)", lv_msgs[lv],
                                 meta=f"工具结果上限{max(self.detail_base >> (lv-1), DETAIL_FLOOR)}字/步")
                    else:
                        if self.global_summary:
                            _add("历史摘要(窗口外)", [{"role": "system", "content": self._ambient("【历史会话摘要】\n" + self.global_summary)}])
                        _add(f"近窗口历史({min(len(self.turns), self.recent_window_turns)}轮)",
                             self._history_window_msgs()[1 if self.global_summary else 0:])
                elif name == "ltm":
                    _add("长期记忆·静态", self._seg_msgs_ltm())
                elif name == "user_message":
                    if self._current is not None:
                        _add(f"当前轮user(第{len(self.turns)+1}轮)", self._seg_msgs_user_message())
                elif name == "steps":
                    if self._current is not None:
                        _add(f"当前轮steps({len(self._current.steps)}步)", self._seg_msgs_steps())
                elif name == "tail":
                    _add("tail(时间/计划/团队/召回)", self._seg_msgs_tail())
            else:
                nm = f"asm:{item.get('kind')} {item.get('path') or item.get('name') or item.get('cmd') or ''}".strip()
                _add(nm, self._asm_action_msgs(item),
                     meta="once" if item.get("timing") == "once" or
                     (item.get("kind") == "workflow" and not item.get("timing")) else "turn")
        return out

    # ========== 构建 ==========
    def _emit_event(self, event: dict):
        """append 一个事件到 events.jsonl；name 未就绪(_event_path=None)时 buffer 在内存。
        落盘失败不阻塞主循环（内存里 turns 仍是真相，事件只是持久化投影）。"""
        if self._event_path is None:
            self._event_buffer.append(event)
        else:
            try:
                with open(self._event_path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(event, ensure_ascii=False) + "\n")
            except Exception:
                pass

    def _bind_event_path(self, path):
        """name 就绪后绑定 events.jsonl，把缓冲的事件 flush 进文件（append 模式，不覆盖已有）。"""
        self._event_path = Path(path)
        self._event_path.parent.mkdir(parents=True, exist_ok=True)
        if self._event_buffer:
            try:
                with open(self._event_path, "a", encoding="utf-8") as f:
                    for e in self._event_buffer:
                        f.write(json.dumps(e, ensure_ascii=False) + "\n")
                self._event_buffer = []
            except Exception:
                pass

    def record_snapshot(self, sha: str):
        """记录工作区快照 sha 到当前 turn（检查点回溯用）。agent 打快照后调用。"""
        if self._current is not None:
            self._current.snapshot_sha = sha
            self._emit_event({"event": "snapshot", "sha": sha})

    def start_turn(self, user_message: str, images: Optional[list] = None):
        # 防御：上一轮未正常 finish/abort（run 中途异常逃出，如 LLM 502 抛穿循环）→
        # 先收尾归档，否则 _current 被下面直接覆盖，中断轮（user+steps）从内存丢失、
        # 本进程内投影（recent window/分档都从 self.turns 渲染）完全看不到。
        # 不调 LLM 生成 summary（省一次短调用；折叠摘要对空 answer 有"中断(未回答)"兜底）。
        if self._current is not None:
            prev = self._current
            prev.answer = prev.answer or "（中断，本轮未完成）"
            self.turns.append(prev)
            _reason = getattr(self, "_interrupt_reason", "") or ""
            if _reason:
                # 标记完整闭合（_is_interrupt_mark 前缀匹配），原因另起一行——
                # 带原因内嵌的变体会让前缀匹配失效（"——原因）"插在标记的"）"之前）
                prev.answer = f"（中断，本轮未完成）\n原因：{_reason}"
                self._interrupt_reason = ""
            import logging as _lg
            _lg.getLogger("agt.session").warning(
                "归档异常中断轮（user=%r…，%d 步）原因：%s",
                (prev.user_message or "")[:40], len(prev.steps), _reason or "未知（无 _interrupt_reason）")
            self._emit_event({"event": "turn_end", "answer": prev.answer,
                              "answer_reasoning": prev.answer_reasoning or "",
                              "summary": prev.summary or "",
                              "interrupt_reason": _reason})
            self._refresh_summary_cache()
            self._autosave()
        self._current = Turn(user_message=user_message, images=images or [])
        self._emit_event({"event": "turn_start", "user": user_message, "images": images or []})
        if self._over_window_mark:   # 上轮实测 total 超 win（observe_llm_usage 置位）：本轮重规划并留痕
            _LOG.info("上轮实测 token 超窗（win=%d）：以校准比率 %.2f 字/token 重规划折叠",
                      self.max_effective_context_window, self._chars_per_token)
            self._over_window_mark = False
        self._plan_fold()   # 轮边界折叠计划：新轮开始瞬间算好折到 75%，轮内不再折叠（byte-stable）

    def add_step(self, step: Step):
        if self._current is None:
            raise RuntimeError("没有进行中的 Turn，请先 start_turn()")
        self._current.steps.append(step)
        self._emit_event({"event": "step", "reasoning": step.reasoning or "",
                          "call_ids": [tc.call_id for tc in step.tool_calls]})

    def finish_turn(self, answer: str, answer_reasoning: str = ""):
        if self._current is None:
            return
        self._pinned_ctx = None   # context_messages 用完即焚（本轮投影期间已展开；复用实例下一轮不带）
        self._current.answer = answer
        self._current.answer_reasoning = answer_reasoning
        # 生成该轮 summary（贴在该轮最后：作语义索引 + 窗口外摘要源 + 召回匹配文本）
        try:
            self._current.summary = self._summarize_turn(self._current)
        except Exception:
            self._current.summary = ""
        self.turns.append(self._current)
        finished = self._current
        self._current = None
        self._ensure_name()            # name 就绪 → 绑定 events/toollog 路径并 flush 缓冲
        self._emit_event({"event": "turn_end", "answer": finished.answer,
                          "answer_reasoning": finished.answer_reasoning,
                          "summary": finished.summary})
        self._refresh_summary_cache()  # 维护窗口外 summary 拼接（不截断 turns）
        self._autosave()               # 异步落盘
        self._index_turn(finished)     # 向量库增量索引（vec_store 为 None 时 no-op）

    def _index_turn(self, turn: "Turn"):
        """每轮完成后增量索引进向量库。空 store 或 summary 未生成时跳过。"""
        store = getattr(self, "vec_store", None)
        if store is None:
            return
        # 至少需要 user_message 或 answer 才能生成检索文本
        if not turn.user_message and not turn.answer:
            return
        sid = self.name or (self.session_dir.name if self.session_dir else "")
        rsn = "\n".join(s.reasoning for s in turn.steps if s.reasoning)
        cids = [tc.call_id for s in turn.steps for tc in s.tool_calls]
        try:
            store.build_one(sid, len(self.turns),   # turn_no = 1-based (len after append)
                            user=turn.user_message, answer=turn.answer,
                            summary=turn.summary, reasoning=rsn,
                            call_ids=cids)
        except Exception:
            pass   # 向量索引失败不影响主流程

    def abort_current_turn(self, note: str = "（被中断）"):
        """中断时把进行中的轮收尾，避免丢失已完成的步骤。"""
        if self._current is None:
            return
        self._current.answer = note
        try:
            self._current.summary = self._summarize_turn(self._current)
        except Exception:
            self._current.summary = ""
        self.turns.append(self._current)
        finished = self._current
        self._current = None
        self._ensure_name()            # name 就绪 → 绑定 events/toollog 路径并 flush 缓冲
        self._emit_event({"event": "turn_end", "answer": finished.answer,
                          "answer_reasoning": finished.answer_reasoning,
                          "summary": finished.summary})
        self._refresh_summary_cache()
        self._autosave()

    def restore_to_snapshot(self, sha: str) -> Optional[str]:
        """检查点回溯：找到 snapshot_sha==sha 的那轮，截断它及之后的轮，回到它【之前】。
        重写 events/toollog 落盘文件（仅留前 i 轮 + restore 标记），避免 reload 时旧事件复活。
        返回那轮的用户消息（供 UI 提示）；找不到返回 None。"""
        for i, t in enumerate(self.turns):
            if t.snapshot_sha == sha:
                target_msg = t.user_message
                self.turns = self.turns[:i]
                self._tier_boundaries = [b for b in self._tier_boundaries if b < i]
                self._frozen_renders.clear()
                self._current = None
                self._rewrite_persistence(i)   # 重写 events/toollog 文件（含 restore 标记）
                self._plan_fold()              # turns 变短：重算折叠计划（可能回退——折多了浪费）
                self._refresh_summary_cache()
                self._autosave()  # 回溯后也落盘（写 metadata json）
                return target_msg
        return None

    def _rewrite_persistence(self, keep: int):
        """rewind 后以 self.turns[:keep] 为真相重写 events/toollog 落盘文件（原子写）。
        解决 events.jsonl append-only 导致 reload 时旧事件复活的问题。
        name 未就绪（事件还在内存 buffer）时只重置 buffer + 裁剪 toollog 内存。"""
        # 重新生成前 keep 轮的标准事件序列 + restore 审计标记
        events = []
        for t in self.turns[:keep]:
            events.append({"event": "turn_start", "user": t.user_message, "images": t.images or []})
            if t.snapshot_sha:
                events.append({"event": "snapshot", "sha": t.snapshot_sha})
            for s in t.steps:
                events.append({"event": "step", "reasoning": s.reasoning or "",
                               "call_ids": [tc.call_id for tc in s.tool_calls]})
            events.append({"event": "turn_end", "answer": t.answer or "",
                           "answer_reasoning": t.answer_reasoning or "", "summary": t.summary or ""})
        events.append({"event": "restore", "keep": keep})

        if self._event_path is None:
            self._event_buffer = events
            kept = {tc.call_id for t in self.turns[:keep] for s in t.steps for tc in s.tool_calls}
            self.toollog._data = {k: v for k, v in self.toollog._data.items() if k in kept}
            return

        sd = self._event_path.parent
        name = self.name
        self._atomic_write_lines(self._event_path, events)
        # toollog.jsonl：仅留 kept turns 用到的 call_id，重写后重载（恢复 counter，新 id 不撞旧）
        tl_path = sd / f"{name}.toollog.jsonl"
        kept_ids, seen = [], set()
        for t in self.turns[:keep]:
            for s in t.steps:
                for tc in s.tool_calls:
                    if tc.call_id and tc.call_id not in seen:
                        seen.add(tc.call_id); kept_ids.append(tc.call_id)
        kept_entries = [e for e in (self.toollog.get(c) for c in kept_ids) if e]
        self._atomic_write_lines(tl_path, kept_entries)
        # recaps.jsonl 同步裁剪：idx >= keep 的 recap 若不删，rewind 后新轮会长到这些 idx
        # 而被旧 recap 张冠李戴（load 侧按 idx 盲配）。内存 Turn.recap 顺带清（pop 的轮已不在）。
        rp_path = sd / "recaps.jsonl"
        if rp_path.exists():
            kept_recs = []
            for t in self.turns[:keep]:
                if (t.recap or "").strip():
                    kept_recs.append({"idx": self.turns.index(t), "recap": t.recap, "ts": int(time.time())})
            self._atomic_write_lines(rp_path, kept_recs)
        self.toollog = ToolLog()
        if tl_path.exists():
            self.toollog.load_from_jsonl(tl_path)   # 加载 clean 数据 + 绑 path + 恢复 counter
        # 注：llm_calls.jsonl 是可观测流水（无 turn 索引），保留不动——不影响 replay/render

    @staticmethod
    def _atomic_write_lines(path: Path, rows: list):
        """原子写 jsonl：先 .tmp 再 os.replace，防并发/崩溃读到半个文件。"""
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        os.replace(tmp, path)

    # ========== 融合上下文（关键）==========
    # ========== assembly DSL v2：清单驱动投影 ==========
    # 默认装配清单（无声明 = 主 Agent / 未写 assembly 的路径）——与历史版硬编码投影顺序一致：
    # system(人设) → rules(AGENTS/规则/技能，每轮重读) → history(滑动窗口+摘要 或 分档毕业)
    # → ltm(长期记忆静态层) → user_message(当前轮) → steps(当前轮步骤) → tail(易变环境块)。
    # ltm 统一放在 history 之后：它偶发变化（agent 写记忆），越靠后变化时的缓存爆炸半径越小。
    _DEFAULT_ASSEMBLY_PLAN = [
        {"kind": "seg", "name": "system"},
        {"kind": "seg", "name": "rules"},
        {"kind": "seg", "name": "history"},
        {"kind": "seg", "name": "ltm"},
        {"kind": "seg", "name": "user_message"},
        {"kind": "seg", "name": "steps"},
        {"kind": "seg", "name": "tail"},
    ]

    def set_assembly_plan(self, plan: Optional[list]):
        """设置 assembly 清单（multiagent 解析 .md frontmatter / agent_prompt 参数覆盖后调用）。
        None = 恢复默认清单。同时派生旧版开关 dict（assembly.get("hooks") 等消费点不变）：
        可关段在清单中出现 → True，未出现 → False（白名单语义）；hooks 不占投影位置，仅开关。"""
        self.assembly_plan = plan
        self._assembly_once_cache = {}
        if plan is None:
            self.assembly = {}
            return
        segs = {it.get("name") for it in plan if it.get("kind") == "seg"}
        self.assembly = {t: (t in segs) for t in ("rules", "history", "ltm", "tail")}
        hist = next((it for it in plan if it.get("kind") == "seg" and it.get("name") == "history"), None)
        if hist and hist.get("mode"):
            self.assembly["history_mode"] = hist["mode"]

    def _seg_msgs_system(self) -> list[dict]:
        """核心 system（人设+今日+用户名）——真正的指令，不包裹。
        空则返回空列表（persona 已移到 assembly text: 项的场景）。"""
        if not (self.system or "").strip():
            return []
        return [{"role": "system", "content": self.system}]

    def _seg_msgs_rules(self) -> list[dict]:
        """任务指引（AGENTS.md/rules/skills/子Agent）：每次 build 从磁盘重读——
        用户/Agent 改了规则或声明文件（含 create_agent 新建子 Agent 后立即派活）当轮生效。"""
        if self._task_guidance_provider:
            try:
                _tg = self._task_guidance_provider() or ""
            except Exception:
                _tg = ""
            if _tg:
                return [{"role": "system", "content": _tg}]
        return []

    def _seg_msgs_history(self, mode: str = None, prefix_msgs: list = None) -> list[dict]:
        """历史段：mode 分派 window（滑动窗口+全局摘要）/ tiered（分档毕业）/ full（不压缩）。
        None = 原自动行为：配了 max_effective_context_window 走 tiered，否则 window。
        prefix_msgs：tiered 保命阀估算用的已累积消息（量级近似即可）。"""
        if mode == "tiered" and not self.max_effective_context_window:
            mode = "full"   # 无预算不毕业：tiered 退化为全量
        if mode == "full":
            return self._history_full_msgs()
        if mode == "window":
            return self._history_window_msgs()
        if self.max_effective_context_window:
            return self._history_tiered_msgs(prefix_msgs or [])
        return self._history_window_msgs()

    def _history_window_msgs(self) -> list[dict]:
        """滑动窗口+摘要：窗口外各轮 summary 拼成一条 system 摘要 + 近窗口逐 step 还原。
        需要早期轮细节时模型可用 recall_turn 按需召回完整原文。
        装配进行中（self._hist_marks 非 None）时记录子段标记供 /context 统计。"""
        out = []
        marks = self._hist_marks
        if self.global_summary:
            if marks is not None:
                marks.append(("历史摘要(窗口外)", 0, ""))
            out.append({"role": "system", "content": self._ambient("【历史会话摘要】\n" + self.global_summary)})
        recent = self.turns[-self.recent_window_turns:]
        _rw_start = len(out)   # 近窗口段起点（有摘要=1，无=0）
        for t in recent:
            out.append({"role": "user", "content": self._user_content(t)})
            out.extend(self._steps_to_messages(t.steps, self.max_steps_per_turn))
            if t.answer:
                a_msg = {"role": "assistant", "content": t.answer}
                if t.answer_reasoning:
                    a_msg["reasoning_content"] = t.answer_reasoning
                out.append(a_msg)
        if marks is not None and recent:
            marks.append((f"近窗口历史({len(recent)}轮)", _rw_start, ""))
        return out

    def _history_full_msgs(self) -> list[dict]:
        """不压缩：全部已完成轮逐条投影（子 agent 短会话/需要完整上下文精度的场景）。"""
        out = []
        for t in self.turns:
            out.append({"role": "user", "content": self._user_content(t)})
            out.extend(self._steps_to_messages(t.steps, self.max_steps_per_turn))
            if t.answer:
                a_msg = {"role": "assistant", "content": t.answer}
                if t.answer_reasoning:
                    a_msg["reasoning_content"] = t.answer_reasoning
                out.append(a_msg)
        return out

    def _history_tiered_msgs(self, prefix_msgs: list) -> list[dict]:
        """分档历史段（_build_tiered_messages 的保命阀逻辑迁移）：
        轮内以 _planned_fold/_planned_graduates 为起点零调整（75%~panic 纯追加，前缀缓存最优），
        超 panic 才应急：先升档（无损压缩）再折叠。"""
        win = self.max_effective_context_window
        panic_win = config.load_panic_window() or win
        settle = int(win * FOLD_TARGET_RATIO)
        fold_count = self._planned_fold
        # 估算辅助：history 之后的段（ltm/当前轮/tail），循环外算一次（tail 含 episodic 召回，避免循环内反复 embed）。
        # 剥除 recent-file 块（_rf_stripped）：应急判定针对历史段，rf 是轮内易变项不该推动升档/折叠（用户裁定 2026-08-29）
        rest = self._rf_stripped(self._seg_msgs_ltm() + self._seg_msgs_user_message()
                                 + self._seg_msgs_steps() + self._seg_msgs_tail())
        panic_mode = False
        for _ in range(len(self.turns) + self.max_level + 4):   # 安全上限，不会死循环
            body = self._render_tiered_history(fold_count)
            est = self._estimate_tokens(prefix_msgs + body + rest)
            if not panic_mode:
                if est <= panic_win:
                    break                                   # 保命线内：零调整（75%~panic 纯追加）
                panic_mode = True                           # 首次超线 → 应急模式（此后回落目标 settle）
                _LOG.info("保命阀触发：投影 est=%d 超 panic=%d（win=%d），回落至 ≤%d",
                          est, panic_win, win, settle)
            if est <= settle:
                break                                       # 已回落到位
            if self._graduate_once():                       # 先升档（无损压缩）止血
                continue
            # 应急首刀同款大刀（超深一半）；起点之后的微调碎刀
            nxt = (self._fold_leap_target(fold_count) if fold_count == self._planned_fold
                   else self._next_fold_target(fold_count))
            if nxt is not None:
                fold_count = nxt
                continue
            break
        self._last_fold_count = fold_count   # 记录本次折叠轮数（to_history 用它折叠前端历史）
        if self._hist_marks is not None:
            self._hist_marks.clear()   # 保命阀循环里调过多次 _render_tiered_history（各自塞了标记）——
                                       # 清空让下面最终渲染的标记成为唯一真相
        return self._render_tiered_history(fold_count)

    def _seg_msgs_ltm(self) -> list[dict]:
        """长期记忆·静态层（semantic 事实 + procedural 标题清单）：常驻背景知识块。"""
        if self._ltm_static_provider:
            try:
                block = self._ltm_static_provider()
                if block:
                    return [{"role": "system", "content": self._ambient(block)}]
            except Exception:
                pass
        return []

    def _seg_msgs_user_message(self) -> list[dict]:
        """当前进行中轮的 user 消息 + before_turn 钩子提示（保证工具对话连续）。"""
        if self._current is None:
            return []
        out = [{"role": "user", "content": self._user_content(self._current)}]
        _bt = getattr(self._current, "_before_turn_hint", None)
        if _bt:
            out.append({"role": "system", "content": _bt})
        return out

    def _seg_msgs_steps(self) -> list[dict]:
        """当前轮已完成的步骤 + 本步 pending 的用户中途补充（带标签，发出后滚入历史中部）。
        recent-file（跟屁虫快照，用户设计 2026-08-29）：_rf_latest_map 维护 filename→最新改它的
        call_id 映射，_steps_to_messages 渲染 role:tool 时按 call_id 命中——快照附加在【那次
        工具调用的 result content】尾部（因果位置自然，紧跟改它的调用）；同文件多次 edit 只有
        最新一次的 call 命中。归档轮/历史段不传映射（call_id 不在映射里，天然"前面的轮不管"）。"""
        if self._current is None:
            return []
        out = list(self._steps_to_messages(self._current.steps, self.max_steps_per_turn,
                                           full_window=RECENT_FULL_STEPS,
                                           rf_map=self._rf_latest_map()))
        _psh = getattr(self._current, "_pending_step_hint", None)
        if _psh:
            out.append({"role": "user", "content": _MIDTURN_TAG + _psh})
        return out

    def _seg_msgs_tail(self) -> list[dict]:
        """tail ambient（易变块合并成一组 <system-reminder>：时间+后台+计划+spec+情境记忆，
        放 user 后保前缀缓存）。"""
        tail_blocks = []
        self._collect_ambient(tail_blocks, self._time_provider)
        self._collect_ambient(tail_blocks, self._system_extra_provider)
        self._collect_ambient(tail_blocks, self._plan_provider)
        self._collect_ambient(tail_blocks, self._spec_provider)
        # 情境层（episodic）按问题召回，放 tail 最后
        if self._ltm_episodic_provider and self._current is not None and self._current.user_message:
            try:
                block = self._ltm_episodic_provider(self._current.user_message)
                if block and block.strip():
                    tail_blocks.append(block.strip())
            except Exception:
                pass
        grouped_tail = self._ambient_group(tail_blocks)
        return [{"role": "system", "content": grouped_tail}] if grouped_tail else []

    def _asm_action_msgs(self, item: dict) -> list[dict]:
        """assembly 清单里的动作项（file/dir/cmd/workflow/text）→ 一条 system 消息。
        timing=once 的项求值后缓存（key 按项内容，与清单位置无关）；turn 每次重求。
        求值失败/空结果 → 跳过该段 + 日志（不炸投影，保底可用）。"""
        timing = item.get("timing") or ("once" if item.get("kind") == "workflow" else "turn")
        key = None
        if timing == "once":
            key = ":".join(str(item.get(k) or "") for k in ("kind", "path", "file", "dir", "cmd", "name", "text"))
            if key in self._assembly_once_cache:
                txt = self._assembly_once_cache[key]
            else:
                txt = self._asm_evaluate(item)
                self._assembly_once_cache[key] = txt
        else:
            txt = self._asm_evaluate(item)
        if not txt:
            return []
        label = (item.get("path") or item.get("file") or item.get("dir")
                 or item.get("cmd") or item.get("name") or item.get("func") or "")
        return [{"role": "system", "content": self._ambient(f"[assembly:{item['kind']}{' ' + label if label else ''}]\n{txt}")}]

    def _asm_evaluate(self, item: dict) -> str:
        """动作项求值（workspace 沙箱 / 超时 / 失败跳过）。"""
        kind = item.get("kind")
        try:
            if kind == "text":
                return _interp_funcs(str(item.get("text") or ""))
            if kind == "func":
                from agent_config import resolve_assembly_func
                return resolve_assembly_func(str(item.get("func") or ""))
            if kind in ("file", "dir"):
                # 路径基准 = session.workspace（子 Agent 复活/临时目录场景与 real_tools.WORKSPACE 可能不同）；
                # 解析产物把值存在同名键下（{file: path} → item["file"]）；path 键兼容手写清单
                _val = str(item.get("path") or item.get("file") or item.get("dir") or "")
                cand = Path(_val)
                if not cand.is_absolute():
                    cand = Path(self.workspace) / _val
                try:   # 沙箱：解析后不许逃出 workspace
                    cand = cand.resolve()
                    cand.relative_to(Path(self.workspace).resolve())
                except ValueError:
                    _LOG.warning("assembly %s 项越界（workspace 外）：%s，跳过", kind, _val)
                    return ""
                target = cand
                if not target.exists():
                    _LOG.warning("assembly %s 项不存在：%s，跳过", kind, _val)
                    return ""
                if kind == "file":
                    if target.stat().st_size > 64_000:
                        _LOG.warning("assembly file 项超 64KB：%s，跳过（过大会挤爆上下文）", _val)
                        return ""
                    return target.read_text(encoding="utf-8", errors="ignore")
                import real_tools as _rt
                return _rt.dir_outline(str(target))
            if kind == "cmd":
                import subprocess as _sp
                r = _sp.run(str(item.get("cmd") or ""), shell=True, capture_output=True,
                            timeout=10, cwd=str(self.workspace))
                out = (r.stdout or b"").decode("utf-8", errors="replace").strip()
                if not out:
                    return ""
                return out[:32_000]
            if kind == "workflow":
                return _eval_assembly_workflow(str(item.get("name") or ""), self)
            if kind == "tool":
                return _eval_assembly_tool(item, self)
            return ""
        except Exception as e:
            _LOG.warning("assembly %s 项求值失败（%s），跳过：%s", kind, item.get("name") or item.get("path") or item.get("cmd"), e)
            return ""

    def messages_for_llm(self) -> list[dict]:
        """投影 = assembly 清单驱动：按 self.assembly_plan 的顺序装配段/动作项；
        无声明走默认清单（与历史版硬编码顺序一致）。
        current_turn_only（子 Agent reuse）：history/ltm 段强制跳过——每次任务上下文干净、
        token 不随复用次数增长；历史轮仍完整归档（agent_query_events / recall 可查）。
        装配时顺手记录分段统计到 _proj_stats（/context 直接读——真实投影口径，
        比事后重算的 projection_breakdown 更可信；后者退化为无缓存时的兜底）。"""
        plan = self.assembly_plan or self._DEFAULT_ASSEMBLY_PLAN
        msgs: list[dict] = []
        marks: list[tuple[str, int]] = []   # (段名, 全局起始 idx)；history 段用占位名，装完后用子标记展开
        self._hist_marks = []               # history 子段标记（_render_tiered_history/_history_window_msgs 填充）
        try:
            for item in plan:
                kind = item.get("kind")
                if kind == "seg":
                    if item.get("opt"):
                        continue   # optional 段默认不装配（agent_prompt assembly="seg=on" 清标记后才会到这里）
                    name = item.get("name")
                    if self.current_turn_only and name in ("history", "ltm"):
                        continue   # reuse 投影隔离：历史系段一律不投影
                    if name == "system":
                        marks.append(("system(人设+环境)", len(msgs)))
                        msgs.extend(self._seg_msgs_system())
                    elif name == "rules":
                        marks.append(("rules(AGENTS+规则+技能)", len(msgs)))
                        msgs.extend(self._seg_msgs_rules())
                    elif name == "history":
                        marks.append(("\x00HIST\x00", len(msgs)))   # 占位：下方展开为 hist 子标记
                        msgs.extend(self._seg_msgs_history(item.get("mode"), msgs))
                    elif name == "ltm":
                        marks.append(("长期记忆·静态", len(msgs)))
                        msgs.extend(self._seg_msgs_ltm())
                    elif name == "user_message":
                        marks.append((f"当前轮user(第{len(self.turns)+1}轮)", len(msgs)))
                        # context_messages 直通（agent_prompt 注入的一次性前置上下文）：
                        # 展开在 user 之前——还原的历史对话/工作记录；finish_turn 即焚（不落 turns）
                        _pinned = getattr(self, "_pinned_ctx", None)
                        if _pinned:
                            marks.append((f"前置上下文({len(_pinned)}条)", len(msgs)))
                            msgs.extend({"role": str(m.get("role")), "content": m.get("content")}
                                        for m in _pinned if isinstance(m, dict))
                        msgs.extend(self._seg_msgs_user_message())
                    elif name == "steps":
                        marks.append((f"当前轮steps({len(self._current.steps) if self._current else 0}步)", len(msgs)))
                        msgs.extend(self._seg_msgs_steps())
                    elif name == "tail":
                        marks.append(("tail(时间/计划/团队/召回)", len(msgs)))
                        msgs.extend(self._seg_msgs_tail())
                else:
                    nm = f"asm:{item.get('kind')} {item.get('path') or item.get('name') or item.get('cmd') or ''}".strip()
                    marks.append((nm, len(msgs)))
                    msgs.extend(self._asm_action_msgs(item))
            # —— 分段统计（真实装配口径）：history 占位展开为子标记，再统一切段计算 ——
            final: list[tuple[str, int, str]] = []
            for mn, st in marks:
                if mn == "\x00HIST\x00":
                    if self._hist_marks:
                        for (sub, off, meta) in self._hist_marks:
                            final.append((sub, st + off, meta))
                    else:
                        final.append(("history段", st, ""))
                else:
                    final.append((mn, st, ""))
            sections = []
            for i, (mn, st, meta) in enumerate(final):
                end = final[i + 1][1] if i + 1 < len(final) else len(msgs)
                if end <= st:
                    continue   # 空段
                seg = msgs[st:end]
                # 段 tokens 用纯内容口径（include_schema=False）——schema 是请求级，
                # 逐段含 schema 会重复计入（每段带一份底噪、段间之和≠合计）；schema 单列一段
                sections.append({"name": mn, "msgs": end - st, "chars": self._count_chars(seg),
                                 "tokens": self._estimate_tokens(seg, include_schema=False), "meta": meta,
                                 "sample": (str(seg[0].get("content") or "")[:120].replace("\n", " ")
                                            if seg else "")})   # 首条消息样本：段统计异常时（如 msgs=1
                                 # 却巨大）直接看切片里是什么——live 异常无法跨进程静态复现，埋此诊断
            if self._tools_schema_chars:
                sections.append({"name": "tools schema(请求级·计一次)", "msgs": 0,
                                 "chars": self._tools_schema_chars,
                                 "tokens": int(self._tools_schema_chars / self._chars_per_token),
                                 "meta": "随请求计费的函数 schema——各内容段之外单独占的部分"})
            self._proj_stats = {"sections": sections,
                                "total_msgs": len(msgs),
                                "total_chars": self._count_chars(msgs),
                                "total_tokens": self._estimate_tokens(msgs),
                                "ts": time.time(),
                                "turn": len(self.turns),
                                "step": len(self._current.steps) if self._current else 0,
                                "source": "live"}
        except Exception as e:
            _LOG.warning("投影分段统计失败（不影响投影）：%s", e)
        finally:
            self._hist_marks = None
        return msgs

    # ========== 分档上下文投影（max_effective_context_window 启用）==========
    def _append_ambient(self, msgs: list, provider, *args):
        """把一个背景 provider 的返回包成 <system-reminder> 追加（无返回/异常则跳过）。"""
        if not provider:
            return
        try:
            block = provider(*args)
            if block:
                msgs.append({"role": "system", "content": self._ambient(block)})
        except Exception:
            pass

    def _collect_ambient(self, blocks: list, provider, *args):
        """收集一个背景 provider 的返回（不包标签），追加到 blocks 列表。用于 tail 合并。"""
        if not provider:
            return
        try:
            block = provider(*args)
            if block and block.strip():
                blocks.append(block.strip())
        except Exception:
            pass

    @staticmethod
    def _ambient_group(blocks: list[str]) -> str:
        """把多个背景块合并进一组 <system-reminder>（子块之间空行分隔）。全空返回空串。"""
        parts = [b for b in blocks if b and b.strip()]
        if not parts:
            return ""
        return "<system-reminder>\n" + "\n\n".join(parts) + "\n</system-reminder>"

    def _tier_level(self, turn_idx: int) -> int:
        """turn 所在档位级别（封顶 max_level，渲染 base 用）。
        算式 level = 1 + count(boundaries 中 >= turn_idx)；验过 [5,10]→turn5=3/turn10=2/turn11=1，
        加 15 后→turn5=4/turn10=3/turn15=2/turn16=1（全档顺移）。"""
        return min(self._raw_tier_level(turn_idx), self.max_level)

    def _raw_tier_level(self, turn_idx: int) -> int:
        """未封顶的真实档位（滚动毕业后早期轮可持续 >max_level）。
        超深档折叠（fold_deep_tools 开）以它判定：raw > max_level 的轮走工具调用整体折叠。"""
        return 1 + sum(1 for b in self._tier_boundaries if b >= turn_idx)

    def _render_turn_frozen(self, turn_idx: int) -> list[dict]:
        """渲染一个【已完成】turn，按其档位级别冻结：同 (level, fold, base) 直接复用缓存 → byte-stable。
        档位 base = self.detail_base >> (level-1)（显式配置/窗口推导——见 detail_base property）。
        level 变了（毕业顺移）才重算；fold 开关/base 变化也失效重算（key 含 fold 位与 base）。
        超深档折叠（fold_deep_tools 开 且 raw level > max_level）：工具调用整体折叠成
        一行标注 + 保留最终回复原文与 reasoning 原文（用户设计——超深档残缺摘要信息密度低，
        不如结论原文 + 可 recall 的完整存档）。"""
        level = self._tier_level(turn_idx)
        fold = config.load_fold_deep_tools() and self._raw_tier_level(turn_idx) > self.max_level
        key = (level, fold, self.detail_base)
        cached = self._frozen_renders.get(turn_idx)
        if cached and cached[0] == key:
            return cached[1]
        turn = self.turns[turn_idx]
        msgs = [{"role": "user", "content": self._user_content(turn)}]
        if fold:
            n_calls = sum(len(s.tool_calls) for s in turn.steps)
            if n_calls:
                # 有工具调用才加标注行——纯回答轮（架构讨论等 0 工具轮）加
                # "已折叠共0次"是纯噪声（用户实测指出），此时 content 直接是 answer 原文
                content = f"---- 已折叠共{n_calls}次工具调用 ----\n\n{turn.answer}"
                a_msg = {"role": "assistant", "content": content}
            else:
                a_msg = {"role": "assistant", "content": turn.answer}
            if turn.answer_reasoning:
                a_msg["reasoning_content"] = turn.answer_reasoning
            msgs.append(a_msg)
        else:
            base = max(self.detail_base >> (level - 1), DETAIL_FLOOR)
            msgs.extend(self._steps_to_messages(turn.steps, self.max_steps_per_turn,
                                                 base=base, full_window=(1 if level == 1 else 0)))
            if turn.answer:
                a_msg = {"role": "assistant", "content": turn.answer}
                if turn.answer_reasoning:
                    a_msg["reasoning_content"] = turn.answer_reasoning
                msgs.append(a_msg)
        self._frozen_renders[turn_idx] = (key, msgs)
        return msgs

    def _render_tiered_history(self, fold_count: int = 0) -> list[dict]:
        """渲染分档历史段：[已折叠早期轮次摘要] + 未折叠的已完成 turn（按档冻结）。
        fold_count 个最早的 turn 折叠成摘要不逐条渲染（细节靠 recall 召回）。
        当前轮/tail 不在此（v2 段循环按清单位置各自装配）。
        装配进行中（self._hist_marks 非 None）时按档分组标记（/context 统计用）——
        分组拼接顺序与逐轮顺序完全一致（档位随 turn 索引单调不增），byte-stable 不变。"""
        marks = self._hist_marks
        body = []
        if fold_count > 0:
            if marks is not None:   # 装配外调用（_plan_fold/load/start_turn）无标记表，只渲染不记标记
                marks.append((f"折叠摘要({fold_count}轮)", 0, f"最早{fold_count}轮折叠为结构摘要，原文可recall"))
            body.append({"role": "system", "content": self._ambient(self._folded_summary(fold_count))})
        if marks is None:
            for i in range(fold_count, len(self.turns)):
                body.extend(self._render_turn_frozen(i))
            return body
        # 按档分组渲染 + 标记（与 projection_breakdown 同款分组；顺序不变）
        fold_on = config.load_fold_deep_tools()
        groups: dict[str, list] = {}
        turns_n: dict[str, int] = {}
        metas: dict[str, str] = {}
        order: list[str] = []
        for i in range(fold_count, len(self.turns)):
            if fold_on and self._raw_tier_level(i) > self.max_level:
                gname = "已折叠超深档"
                gmeta = "工具调用折叠成一行标注，保留回复+reasoning原文"
            else:
                lv = self._tier_level(i)
                gname = f"档{lv}"
                gmeta = f"工具结果上限{max(self.detail_base >> (lv-1), DETAIL_FLOOR)}字/步"
            if gname not in groups:
                groups[gname] = []
                turns_n[gname] = 0
                metas[gname] = gmeta
                order.append(gname)
            groups[gname].extend(self._render_turn_frozen(i))
            turns_n[gname] += 1
        for gname in order:
            _start = len(body)   # 段开始位置（与顶层 marks 的 len(msgs) 语义一致——都是 extend 前快照）
            body.extend(groups[gname])
            marks.append((f"{gname}历史({turns_n[gname]}轮)", _start, metas[gname]))
        return body

    def _plan_fold(self):
        """轮边界折叠+毕业计划（start_turn 时机）：把投影压到 max_effective_context_window 的 75% 以下。
        【升档也在这里做】——先升档（压缩老档）再折叠（终极兜底），两步都算到 75% 目标。
        轮内 _build 以计划结果为起点零调整（75%~100% 之间纯追加，前缀缓存最优）。
        无窗口配置时计划为 0（现状）。估算用近似前缀（system+指引+静态记忆）+ 完整 body——
        与 _build 的真实估算差个动态 tail，75% 余量下可忽略。"""
        if not self.max_effective_context_window:
            self._planned_fold = 0
            self._planned_graduates = 0
            return
        target = int(self.max_effective_context_window * FOLD_TARGET_RATIO)
        prefix = [{"role": "system", "content": self.system}]
        if self._task_guidance_provider:
            try:
                _tg = self._task_guidance_provider()
                if _tg:
                    prefix.append({"role": "system", "content": _tg})
            except Exception:
                pass
        if self._ltm_static_provider:
            try:
                _b = self._ltm_static_provider()
                if _b:
                    prefix.append({"role": "system", "content": _b})
            except Exception:
                pass
        # —— 卫生性强制毕业（不依赖窗口压力）——
        # 当前档（最后边界之后的段）> GRADUATE_FORCE_TURNS 时分批升前 30 轮：
        # 档1 是全量披露档（"近期窗口"语义），窗口宽绰时压力循环永不触发会让它无限膨胀
        # （用户在 8000 实例观察到 64 轮/58.6%）。分批语义复用 _graduate_once（每刀 30，近期轮保持）。
        last_completed = len(self.turns) - 1
        _seg_start = (self._tier_boundaries[-1] + 1) if self._tier_boundaries else 0
        if last_completed - _seg_start + 1 > GRADUATE_FORCE_TURNS:
            _before = len(self._tier_boundaries)
            while len(self._tier_boundaries) < len(self.turns) // GRADUATE_BATCH_TURNS + self.max_level + 2:
                _lc = len(self.turns) - 1
                _ss = (self._tier_boundaries[-1] + 1) if self._tier_boundaries else 0
                if _lc - _ss + 1 <= GRADUATE_FORCE_TURNS:
                    break
                if not self._graduate_once():
                    break
            g = len(self._tier_boundaries) - _before   # 并入总刀数（日志/报告）
            _LOG.info("卫生性强制毕业 +%d 刀（当前档曾 >%d 轮）", g, GRADUATE_FORCE_TURNS)
        # 先升档：反复 graduate 直到 ≤75%（或无可升）。估算 = prefix + 历史 + 当前轮近似
        # 当前轮估算剥除 recent-file 块（_rf_stripped）：rf 是轮内易变项（归档即消失），
        # 它的体积不该推动升档/折叠——panic 轮内路径调用本函数时 cur_est 含 rf 会过激压缩
        cur_est = self._rf_stripped(self._seg_msgs_user_message() + self._seg_msgs_steps())   # 当前轮（tail 量小不计）
        g = 0
        # 上限宽松化：分批毕业后一次 _plan_fold 可能连切数刀（90 轮大档=3 刀），
        # max_level 封顶的是【档位级别】而非【边界数】——按轮数/批宽 + max_level 算足够上限
        g_cap = len(self.turns) // GRADUATE_BATCH_TURNS + self.max_level + 2
        while g < g_cap:
            if self._estimate_tokens(prefix + self._render_tiered_history(0) + cur_est) <= target:
                break
            if not self._graduate_once():
                break
            g += 1
        # 再折叠：若升档后仍 >75%，折叠到 ≤75%（或无可折）。
        # 首刀大刀（_fold_leap_target）：至少吞超深档的一半——边界密集时碎刀（每刀 1-2 轮）
        # 触发过勤，超深态留存太短；之后仍超线再碎刀微调。
        fc = 0
        for _ in range(len(self.turns) + 4):
            if self._estimate_tokens(prefix + self._render_tiered_history(fc) + cur_est) <= target:
                break
            nxt = self._fold_leap_target(fc) if fc == 0 else self._next_fold_target(fc)
            if nxt is None:
                break
            fc = nxt
        if fc != self._planned_fold or g != self._planned_graduates:
            _LOG.info("轮边界计划：升档 %d 档 + 折叠 %d 轮（目标 ≤75%%×%d）",
                      g, fc, self.max_effective_context_window)
        self._planned_fold = fc
        self._planned_graduates = g

    def _graduate_once(self) -> bool:
        """毕业一批 turn：append 新边界到 _tier_boundaries（边界之前的轮 level+1=顺移），
        并清掉 level 变了的冻结缓存让其按新级别重渲染。
        批量语义（GRADUATE_BATCH_TURNS）：当前段（最后边界之后）≤30 轮 → 整段一次升（旧行为）；
        >30 轮 → 只升【前 30 轮】（新边界=段起点+29），近期轮保持 level1 不动——
        大档分批毕业，升档粒度可控（压缩需要多少升多少，近的保真）。
        当前段无已完成 turn → 返回 False（_plan_fold/保命阀循环据此停止）。"""
        last_completed = len(self.turns) - 1
        if last_completed < 0:
            return False
        seg_start = (self._tier_boundaries[-1] + 1) if self._tier_boundaries else 0
        if seg_start > last_completed:
            return False   # 当前段只剩进行中 turn，无东西可升
        seg_len = last_completed - seg_start + 1
        new_b = last_completed if seg_len <= GRADUATE_BATCH_TURNS \
            else seg_start + GRADUATE_BATCH_TURNS - 1
        self._tier_boundaries.append(new_b)
        # 冻结缓存失效判定用完整 key (level, fold, base)——毕业顺移可能只改 raw 不改封顶 level
        # （raw 4→5 时 tier 仍 4 但 fold 位 False→True），base 变化（/config/切模型窗口变）同理
        _fold_on = config.load_fold_deep_tools()
        _db = self.detail_base
        for i in range(len(self.turns)):
            fr = self._frozen_renders.get(i)
            if fr and fr[0] != (self._tier_level(i),
                                _fold_on and self._raw_tier_level(i) > self.max_level,
                                _db):
                self._frozen_renders.pop(i, None)
        return True

    def _next_fold_target(self, fold_count: int):
        """下一个折叠点 = 超过 fold_count 的最小 boundary +1（折掉一整档最早的 turn）。
        所有 boundary 都已折叠则返回 None（无可再折）。"""
        for b in sorted(self._tier_boundaries):
            if b + 1 > fold_count:
                return b + 1
        return None

    def _fold_leap_target(self, fc: int):
        """fc 大刀首折目标：至少吞掉【超深档的一半】到 fc 结构摘要（用户裁定 2026-08-28：
        边界密集（滚动毕业 ~1.9 轮/边界）时碎刀偏勤——每轮边界触发、每刀只折 1-2 轮，
        超深态（answer/reasoning 原文）留存太短。一次大刀一半，触发间隔翻倍、留存翻倍）。
        超深段 = [fc, bs[-max_level]]（raw>max_level 的轮；最后一个超深轮 = 倒数第 max_level 个边界，
        因 raw_level(i)=1+count(b>=i)，count(bs[-max_level])=max_level → raw=max_level+1）。
        fold_deep_tools 关 / 档梯未满（无超深段）→ 退化为碎刀 _next_fold_target。"""
        if not config.load_fold_deep_tools():
            return self._next_fold_target(fc)
        bs = sorted(self._tier_boundaries)
        if len(bs) <= self.max_level:
            return self._next_fold_target(fc)   # 边界数 ≤ max_level：档梯未满，无超深段
        deep_end = bs[-self.max_level]          # 最后一个超深轮（合法折叠点 = deep_end+1）
        if deep_end + 1 <= fc:
            return self._next_fold_target(fc)   # 超深段已折完：剩档内段，碎刀
        half = fc + max(1, (deep_end + 1 - fc) // 2)   # 至少吞一半（≥1 段）
        for b in bs:                            # 对齐到 ≥half 的最小合法折叠点（boundary+1）
            if b + 1 >= half:
                return min(b + 1, deep_end + 1)
        return deep_end + 1

    @staticmethod
    def _summarize_answer(answer: str) -> str:
        """代码摘要 answer（非 LLM）：第一行(总结句) + 以 ## / ** 开头的标题行；
        无 markdown 结构则回退前 100 字；总长封顶 150 字。"""
        answer = (answer or "").strip()
        if not answer:
            return ""
        lines = [ln.strip() for ln in answer.splitlines() if ln.strip()]
        headings = [ln for ln in lines if ln.startswith("##") or ln.startswith("**")]
        if headings:
            parts, seen = [], set()
            for ln in [lines[0]] + headings:   # 第一行 + 标题行（去重保序）
                if ln not in seen:
                    seen.add(ln); parts.append(ln)
            s = " / ".join(parts)
        else:
            s = answer[:100]                    # 无 markdown → 回退字符截断
        return s[:150]

    def _folded_summary(self, fold_count: int) -> str:
        """被折叠的早期轮次概览：每轮 user + (已折叠N次工具调用) + recap/answer摘要/中断(未回答)。
        纯结构信息、无需 LLM。tail 优先级：recap（turn_end 本地小模型生成的一句话——语义密度高于
        answer 代码摘要的"首行+标题"，后者常是"完成并推送"类横幅文案）→ answer 代码摘要 → 中断标注。
        逐字原文用 recall 召回。"""
        lines = []
        for i, t in enumerate(self.turns[:fold_count]):
            n = sum(len(s.tool_calls) for s in t.steps)
            u = (t.user_message or "").strip().replace("\n", " ")[:80]
            mid = f" (已折叠{n}次工具调用) " if n else " "
            tail = ((t.recap or "").strip()
                    or self._summarize_answer(t.answer)
                    or "中断(未回答)")
            lines.append(f"[第{i + 1}轮] {u}{mid}→ {tail}")
        return "【已折叠的早期轮次（逐字原文用 recall 召回）】\n" + "\n".join(lines)

    def set_turn_recap(self, idx: int, recap: str) -> None:
        """回写某轮的 recap（turn_end 异步生成完成时调用）：Turn.recap + 追加 recaps.jsonl。
        持久化使 /restart 后 fc 折叠摘要仍能用 recap（events 重放不含 recap——它是事后异步产物）。
        idx 越界静默跳过（rewind 后迟到的旧回写无害丢弃）；同值跳过（防重复 append）。
        同 idx 重复 append（resume 后重新 finish 同一轮）由 load 侧 last-wins 兜底。"""
        try:
            recap = (recap or "").strip().split("\n")[0].strip()[:60]
            if not recap or idx < 0 or idx >= len(self.turns):
                return
            if (self.turns[idx].recap or "") == recap:
                return
            self.turns[idx].recap = recap
            sdir = getattr(self, "session_dir", None)
            if sdir:
                p = Path(sdir) / "recaps.jsonl"
                try:
                    with open(p, "a", encoding="utf-8") as f:
                        f.write(json.dumps({"idx": idx, "recap": recap, "ts": int(time.time())},
                                           ensure_ascii=False) + "\n")
                except Exception:
                    pass   # 持久化失败不影响内存（本进程内 fc 摘要仍可用）
        except Exception:
            pass

    def _load_recaps(self, sdir: Path) -> None:
        """从 recaps.jsonl 恢复各轮 recap（load 时调用）。同 idx 多条 last-wins。"""
        p = sdir / "recaps.jsonl"
        if not p.exists():
            return
        try:
            rc: dict = {}
            for line in p.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                i = r.get("idx")
                if isinstance(i, int) and (r.get("recap") or "").strip():
                    rc[i] = str(r["recap"]).strip()[:60]
            for i, t in enumerate(self.turns):
                if i in rc:
                    t.recap = rc[i]
        except Exception:
            pass

    @staticmethod
    def _count_chars(msgs: list[dict]) -> int:
        """投影字符数（分子）：content（str 或多模态块）+ tool_calls 的 name/arguments。
        与历史 _estimate_tokens 内联的分子公式完全同口径——校准（observe）与估算（estimate）
        必须共用同一分子，chars/token 比率才闭环。"""
        n = 0
        for m in msgs:
            c = m.get("content")
            if isinstance(c, str):
                n += len(c)
            elif isinstance(c, list):
                for b in c:
                    n += len(b.get("text", "")) if isinstance(b, dict) else 0
            for tc in (m.get("tool_calls") or []):
                fn = tc.get("function") or {}
                n += len(str(fn.get("name", ""))) + len(str(fn.get("arguments", "")))
        return n

    def _estimate_tokens(self, msgs: list[dict], include_schema: bool = True) -> int:
        """估算 token = (chars + tools schema 字符) / _chars_per_token。
        分子与 observe_llm_usage 校准【同口径】（校准 = (chars+extra_chars)÷prompt）——
        补齐 schema 后折叠计划/保命阀按"真实将发出的 token"判阈，不再系统性少算。
        初值 4=旧行为；observe_llm_usage 用回包实测持续校准该比率。够阈值判断，不必精确。
        include_schema=False：纯内容口径（/context 段统计用——schema 是请求级只计一次，
        逐段调用若含 schema 会把它重复计入每段：N 段各带一份 schema 底噪，段间之和
        虚高且 ≠ 合计；schema 由段统计单列一段展示）。"""
        n = self._count_chars(msgs) + (self._tools_schema_chars if include_schema else 0)
        return int(n / self._chars_per_token)

    # ========== 实测 token 校准 + 超窗/panic 判阈（react 回包喂入） ==========
    def _load_calibration(self) -> None:
        """启动种子校准：读 ~/.agt/token_usage.jsonl 末尾（限 64KB），取最近一条【同模型】记录的
        chars_per_token 作初值——比率跨 session 复用，首个投影就按真实口径估算。
        无记录/不同模型/失败一律静默（保持初值 4.0）。"""
        try:
            if not TOKEN_USAGE_FILE.exists():
                return
            model = getattr(self.llm, "model_name", "") or ""
            with open(TOKEN_USAGE_FILE, "rb") as f:
                f.seek(0, 2)
                size = f.tell()
                f.seek(max(0, size - 65536))
                tail = f.read().decode("utf-8", errors="ignore")
            for line in reversed(tail.splitlines()):   # 从末尾往回找最近一条同模型记录
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue   # 截断的半行（64KB 窗口边界）跳过
                if rec.get("model") == model and rec.get("chars_per_token"):
                    r = float(rec["chars_per_token"])
                    if 1.0 <= r <= 8.0:
                        self._chars_per_token = r
                    break
        except Exception:
            pass

    @staticmethod
    def _append_token_usage(rec: dict) -> None:
        """追加一条实测记录到 ~/.agt/token_usage.jsonl（超窗观察日志 + 校准数据源）。
        超 1MB 重写保留末尾 2000 行（防无界增长）。失败静默，绝不影响主循环。"""
        try:
            TOKEN_USAGE_FILE.parent.mkdir(parents=True, exist_ok=True)
            if TOKEN_USAGE_FILE.exists() and TOKEN_USAGE_FILE.stat().st_size > 1_048_576:
                lines = TOKEN_USAGE_FILE.read_text(encoding="utf-8", errors="ignore").splitlines()
                Session._atomic_write_lines(TOKEN_USAGE_FILE, lines[-2000:])
            with open(TOKEN_USAGE_FILE, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except Exception:
            pass

    def _rf_stripped(self, msgs: list[dict]) -> list[dict]:
        """返回剥除 <recent-file> 块的消息副本（不动原消息——投影产物不可变）。
        毕业（_plan_fold 的 cur_est）/ 保命阀（_history_tiered_msgs 的 rest）估算用：
        rf 是轮内易变项（归档即消失），不该推动升档/折叠等不可逆历史压缩（用户裁定 2026-08-29）。"""
        out = []
        for m in msgs:
            c = m.get("content")
            if isinstance(c, str) and "<recent-file" in c:
                m = dict(m, content=_RE_RF_BLOCK.sub("", c))
            out.append(m)
        return out

    def _rf_in_msgs(self, msgs: list[dict]) -> int:
        """msgs 中 role:tool 的 content 里实际附加的 <recent-file> 块总字符数（真实投影口径——
        用户裁定 2026-08-29：判阈刨除按【实际附加了多少】算，而非映射总量：full/字符串
        等附加条件都已体现在渲染产物里，直接量它最准）。"""
        n = 0
        for m in msgs:
            if m.get("role") != "tool":
                continue
            c = m.get("content")
            if isinstance(c, str) and "<recent-file" in c:
                n += sum(len(x) for x in _RE_RF_BLOCK.findall(c))
        return n

    def _rf_latest_map(self) -> dict:
        """当前轮 recent-file 最新映射（用户设计 2026-08-29）：filename -> {cid, path, version, text}。
        同文件多次 edit 后写覆盖——只有【最新一次改它的 tool_call】会在投影时命中挂快照。
        归档轮/历史段的 call_id 不在映射里（映射只从 _current 构建），天然"前面的轮不管"。"""
        if self._current is None:
            return {}
        latest: dict[str, dict] = {}
        for s in self._current.steps:
            for cid, snap in (s.file_snapshots or {}).items():
                if isinstance(snap, dict) and snap.get("path"):
                    latest[str(snap["path"])] = {"cid": cid, "path": str(snap["path"]),
                                                 "version": str(snap.get("version", "")),
                                                 "text": str(snap.get("text", ""))}
        return latest

    def _rf_chars(self) -> int:
        """当前轮 recent-file（跟屁虫快照）的总字符数——按文件去重后的集合口径。
        判阈刨除用（用户裁定 2026-08-29）：rf 是轮内易变项（归档即消失），
        它的体积不该推动毕业等不可逆历史压缩。"""
        return sum(len(m["text"]) for m in self._rf_latest_map().values())

    def observe_llm_usage(self, msgs: list[dict], usage: dict, extra_chars: int = 0) -> None:
        """react 每次成功 LLM 调用后由 agent 喂入回包 usage（拿到 resp 即成功——失败走 raise）：
        1. 校准：投影字符数 ÷ prompt_tokens → 实测 chars/token，EMA(0.5) 平滑进 _chars_per_token，
           _estimate_tokens / _plan_fold / 保命阀随即按真实口径工作；
        2. 落盘：记录追加 ~/.agt/token_usage.jsonl（含 over/panic 标志）；
        3. 判阈（按实测 total_tokens，含本步输出——它将成为下一步输入，前瞻且保守）：
           超 panic → 立即 _plan_fold() 紧急压缩（升档+折叠即刻改投影，下一步请求即压缩后形态）；
           超 win 未超 panic → 置 _over_window_mark，下轮 start_turn 的 _plan_fold 以校准比率重规划。
        extra_chars：随请求计费但不在 msgs 里的字符（tools schema）——prompt_tokens 含它，
        分子补上才不把比率系统性估高（否则会过早压缩）。win 未配置（窗口模式）完全 no-op。"""
        win = self.max_effective_context_window
        if not win or not usage:
            return
        prompt = int(usage.get("prompt_tokens") or 0)
        completion = int(usage.get("completion_tokens") or 0)
        total = int(usage.get("total_tokens") or (prompt + completion))
        if prompt <= 0 and total <= 0:
            return
        chars = self._count_chars(msgs) + int(extra_chars or 0)
        if prompt > 0 and chars > 0:   # 校准用 prompt：chars 对应的正是输入侧
            ratio = min(max(chars / prompt, 1.0), 8.0)
            self._chars_per_token = round(0.5 * self._chars_per_token + 0.5 * ratio, 3)
        panic = config.load_panic_window() or win
        # 触发判定（用户裁定 2026-08-29）：实测 prompt_tokens 刨除 recent-file 估算后仍超 win 才算 over——
        # rf 是轮内易变项（同文件后写覆盖、归档即消失），它的体积不该推动下轮的历史压缩（毕业不可逆）。
        # panic 判阈不刨（按真实请求体积保命：请求确实超了就必须救——rf 也在真实请求里）。
        rf_tok = self._rf_chars() / max(0.1, self._chars_per_token)
        over = (prompt - rf_tok) > win
        hit_panic = total > panic
        self._append_token_usage({
            "ts": int(time.time()), "model": getattr(self.llm, "model_name", "") or "",
            "chars": chars, "prompt_tokens": prompt, "completion_tokens": completion,
            "total_tokens": total, "chars_per_token": self._chars_per_token,
            "rf_tok": int(rf_tok),
            "over": over, "panic": hit_panic,
        })
        if hit_panic:
            _LOG.warning("实测 token=%d 超 panic=%d：立即紧急压缩（升档+折叠，下一步投影生效）",
                         total, panic)
            self._plan_fold()
        elif over:
            _LOG.info("实测 token=%d（刨 recent-file %d 估算后仍超 win=%d）：标记下轮边界重规划",
                      prompt, rf_tok, win)
            self._over_window_mark = True

    @staticmethod
    def _ambient(content: str) -> str:
        """把"环境/背景上下文"包进 <system-reminder> 语义分隔。

        这类块（历史摘要 / 长期记忆 / 计划 / 后台状态）是给模型的【背景信息】，不是用户在发指令——
        用 XML 标签与核心 system 人设、以及控制流消息（打断/模式切换）区分开，避免模型把它们当指令。
        对照 Claude Code 线上协议：动态上下文(claudeMd/memory/env)正是用 <system-reminder> 包裹注入。
        """
        return f"<system-reminder>\n{content}\n</system-reminder>"

    def _project_imgs(self, text):
        """text 里的 <img>name</img> 占位：当前模型视觉→[text块 + image_url块]（读 repo images/ 转data URL）；
        非视觉→文字占位 str（并提示委托视觉子 agent）。无标签或空文本原样返回。"""
        if not text or "<img>" not in text:
            return text
        matches = list(_IMG_TAG_RE.finditer(text))
        if not matches:
            return text
        vision = getattr(getattr(self, "llm", None), "vision_supported", False)
        if not vision:
            def _sub(m):
                n = m.group(1)
                return (f'[图片 {n}，你无法直接查看；如需理解其内容请委托视觉子 agent：'
                        f'agent_prompt("vision", "请描述 <img>{n}</img> 的内容")]')
            return _IMG_TAG_RE.sub(_sub, text)
        out, last = [], 0
        for m in matches:
            if m.start() > last:
                out.append({"type": "text", "text": text[last:m.start()]})
            try:
                p = repo_images_dir(self.workspace) / m.group(1)
                mime = mimetypes.guess_type(str(p))[0] or "image/png"
                b64 = base64.b64encode(p.read_bytes()).decode()
                out.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}})
            except Exception:
                out.append({"type": "text", "text": f"[图片 {m.group(1)} 读取失败]"})
            last = m.end()
        if text[last:]:
            out.append({"type": "text", "text": text[last:]})
        return out

    def _user_content(self, turn: "Turn"):
        """构造 user 消息内容。turn.images(用户贴图 data URL)→image_url 块(现状)；
        user_message 里的 <img>name</img>(工具图/子agent委托)按当前模型 vision 投影。"""
        text = self._project_imgs(turn.user_message)
        if not turn.images:
            return text
        blocks = list(text) if isinstance(text, list) else [{"type": "text", "text": text}]
        blocks.extend({"type": "image_url", "image_url": {"url": img}} for img in turn.images)
        return blocks

    def _summarize_text(self, text: str, limit: int, call_id: str) -> str:
        """按 limit 摘要工具结果文本；超限截断并在末尾标注 call_id，提示模型用 get_tool_detail 拉完整。"""
        text = text or ""
        if len(text) <= limit:
            return text
        return (text[:limit] + f"\n…(共{len(text)}字，按步距衰减已截断；完整见 id={call_id}，"
                f"调 get_tool_detail(\"{call_id}\") 拉取)")

    def _summarize_args(self, args, limit: int, call_id: str) -> str:
        """摘要工具入参，保持 JSON 合法：只截断超 limit 的字符串值（如 run_python 的 code、edit 的 old_string）。"""
        def _trunc(v):
            if isinstance(v, str):
                return v if len(v) <= limit else (v[:limit] + f"…(共{len(v)}字，截断，get_tool_detail(\"{call_id}\") 取完整)")
            if isinstance(v, dict):
                return {k: _trunc(val) for k, val in v.items()}
            if isinstance(v, list):
                return [_trunc(x) for x in v]
            return v
        return json.dumps(_trunc(args or {}), ensure_ascii=False)

    def _steps_to_messages(self, steps: list[Step], max_steps: int = 0,
                           base: int = None, full_window: int = None, rf_map: dict = None) -> list[dict]:
        """把一组 Step 还原成 role 消息：assistant(tool_calls + reasoning_content) + 各 tool 结果。
        工具名/入参/结果从 toollog 按 call_id 召回。step 级策略（分组投影，缓存友好）：
        - 每 GROUP_STEPS 步一组，组内所有步 limit 一致 → byte-stable（利于前缀缓存）；
        - 组号差 = 当前步所在组 - 本步所在组：
            · 差 ≤ 1（当前组 + 上一组）→ 【全量】披露（当前轮 FULL_STEP_CAP_CHARS 上限；
              老轮则用 base=该档最大字数，不额外衰减）；
            · 差 ≥ 2 → limit = eff_base - GROUP_STEPS * detail_step * 组号差（≥DETAIL_FLOOR）；
        - reasoning 永远原样挂 reasoning_content（不压缩，含 step0 的核心设计思考）。
        max_steps>0 只保留最近 max_steps 步。"""
        msgs = []
        _rf_hit = {m["cid"]: m for m in (rf_map or {}).values()}   # call_id→快照（O(1) 命中查；rf_map 仅当前轮传入）
        if max_steps and len(steps) > max_steps:
            skipped = len(steps) - max_steps
            steps = steps[-max_steps:]
            msgs.append({"role": "system", "content": f"（本轮的 {skipped} 个早期步骤已省略，仅保留最近 {max_steps} 步）"})
        total = len(steps)
        cur_group = (total - 1) // GROUP_STEPS if total else 0   # 当前步所在组（0-based）
        eff_base = base if base is not None else self.detail_base   # 当前轮 base=None → 用统一 base（显式配置/窗口推导）
        for idx, step in enumerate(steps):
            if not step.tool_calls:
                continue
            # 本步之前的"用户中途补充"（user 角色，带标签）：插在上一组 tool 结果之后、本步 assistant 之前
            if step.preceding_hint:
                msgs.append({"role": "user", "content": _MIDTURN_TAG + step.preceding_hint})
            group_diff = cur_group - (idx // GROUP_STEPS)   # 本步组与当前组的组号差（0=同组）
            if group_diff <= 1 and base is None:
                full = True        # 当前轮：当前组 + 上一组全量
                limit = 0          # full 分支不使用 limit
            elif group_diff <= 1:
                full = False       # 老轮：当前组 + 上一组用该档最大字数，不额外衰减
                limit = base
            else:
                full = False       # 更早组：按组号差线性衰减
                limit = max(eff_base - GROUP_STEPS * config.load_detail_step() * group_diff,
                            DETAIL_FLOOR)
            a_tool_calls = []
            for i, tc in enumerate(step.tool_calls):
                name, args, _r = self.toollog.view(tc.call_id)
                args_str = (json.dumps(args, ensure_ascii=False) if full
                            else self._summarize_args(args, limit, tc.call_id))
                a_tool_calls.append({
                    "id": tc.call_id or str(i), "type": "function",
                    "function": {"name": name, "arguments": args_str},
                })
            a_msg = {"role": "assistant", "content": None, "tool_calls": a_tool_calls}
            if step.reasoning:
                a_msg["reasoning_content"] = step.reasoning   # 思考原样，不压缩
            msgs.append(a_msg)
            for i, tc in enumerate(step.tool_calls):
                _n, _a, result = self.toollog.view(tc.call_id)
                content = (self._cap_full_result(result, tc.call_id) if full
                           else self._summarize_text(result, limit, tc.call_id))
                content = self._project_imgs(content)
                # recent-file（用户设计 2026-08-29·第三版）：按 call_id 从映射命中——快照附加在
                # 【该次工具调用的 result content】尾部（因果位置：紧跟改它的调用）。映射只由
                # _seg_msgs_steps 从当前轮构建（filename→最新改它的 cid）：同文件多次 edit 仅
                # 最新 call 命中（旧 call 不挂）；归档轮/历史段不传映射（cid 不在映射里）天然不挂。
                if rf_map and full and (tc.call_id or "") in _rf_hit and isinstance(content, str):
                    _m = _rf_hit[tc.call_id or ""]
                    content += (f"\n<recent-file file='{_m['path']}' version='{_m['version']}'>\n"
                                f"{_m['text']}\n</recent-file>")
                msgs.append({"role": "tool", "tool_call_id": tc.call_id or str(i), "content": content})
        return msgs

    def _cap_full_result(self, result: str, call_id: str) -> str:
        """全量步的结果披露：原样保留，但单步超 FULL_STEP_CAP_CHARS(≈8000token) 时截断并标注 call_id。"""
        result = result or ""
        if len(result) <= FULL_STEP_CAP_CHARS:
            return result
        return (result[:FULL_STEP_CAP_CHARS] +
                f"\n…(本步过长，共{len(result)}字，已截断至约8000token；完整见 id={call_id}，"
                f"调 get_tool_detail(\"{call_id}\") 拉取)")

    # ========== 窗口外摘要缓存（不再截断 turns）==========
    def _refresh_summary_cache(self):
        """维护 global_summary = 窗口外各轮 summary 的拼接（超长则压缩，按签名缓存）。
        关键：不再截断 self.turns——完整原文永久保留，这里只决定「窗口外的轮喂给模型时的摘要形态」。
        分档模式不走 global_summary（用 _folded_summary），直接跳过——省掉拼接 + 超长时的 LLM 压缩。"""
        if self.max_effective_context_window:
            return
        if len(self.turns) <= self.recent_window_turns:
            self.global_summary = ""
            self._summary_sig = ()
            return
        outside = self.turns[:-self.recent_window_turns]
        sig = (len(outside), len(self.turns))  # 窗口外集合变了才重算
        if sig == self._summary_sig and self.global_summary:
            return
        parts = [f"[第{i + 1}轮] {(t.summary or t.user_message[:40]).strip()}"
                 for i, t in enumerate(outside)]
        self.global_summary = "\n".join(parts)
        if len(self.global_summary) > GLOBAL_SUMMARY_CAP:
            self.global_summary = self._compress_summary()
        self._summary_sig = sig

    def _summarize_turn(self, turn: Turn) -> str:
        """用一次短 LLM 调用把一轮压成 2-3 句中文摘要。
        分档模式跳过（recall/_folded_summary 都改用 user+answer，不再需要 LLM 摘要）。"""
        if self.max_effective_context_window:
            return ""
        parts = []
        for step in turn.steps:
            for tc in step.tool_calls:
                n, a, r = self.toollog.view(tc.call_id)
                parts.append(f"{n}({a})→{r[:80]}")
        tools = "; ".join(parts)[:600]
        prompt = (
            "把下面这一轮对话压成 2-3 句中文摘要，保留：用户意图、用了什么工具/做了什么、关键结果。\n"
            f"用户: {turn.user_message}\n"
            f"工具调用: {tools or '无'}\n"
            f"最终回答: {turn.answer[:300]}"
        )
        try:
            return (self.utility_llm or self.llm).chat(
                [{"role": "user", "content": prompt}], scene="summary").content.strip()
        except Exception as e:
            _LOG.warning("轮次摘要失败: %s", e)
            return f"[摘要失败 {e}] 用户: {turn.user_message[:60]}；回答: {turn.answer[:60]}"

    def _compress_summary(self) -> str:
        prompt = ("把下面这段多轮会话摘要进一步压缩成一个更短的整体摘要"
                  "（保留关键决策、当前状态、重要结论），不超过 800 字:\n\n" + self.global_summary)
        try:
            return (self.utility_llm or self.llm).chat(
                [{"role": "user", "content": prompt}], scene="summary").content.strip()
        except Exception:
            return self.global_summary[-GLOBAL_SUMMARY_CAP:]  # 兜底：截断保留最近部分

    # ========== 会话文件夹 ==========
    def _ensure_session_dir(self):
        """创建 session 专属文件夹并返回路径。预设了 session_dir（子 agent）直接建它；否则按时间戳现算。"""
        if self.session_dir is not None:
            self.session_dir.mkdir(parents=True, exist_ok=True)
            return self.session_dir
        self.session_dir = _new_session_dir(self.workspace, self.created_at)
        return self.session_dir

    def _bind_persistence_paths(self):
        """name 就绪后：建专属文件夹 + 绑定 events/toollog/llm_calls 路径 + 切日志 handler。
        幂等——session_dir 已建则不重建，_bind_event_path 缓冲为空不覆盖已有文件。
        把「绑定路径」从 _ensure_name 命名逻辑里解耦，供 rename_session 工具抢先命名时补绑
        （否则 _ensure_name 因 self.name 已设而跳过 → events 不落盘）。"""
        sdir = self._ensure_session_dir()
        self._bind_event_path(sdir / "events.jsonl")
        self.toollog.set_path(sdir / "toollog.jsonl")
        self.llm_calls.set_path(sdir / "llm_calls.jsonl")
        if self._log_handler is not None:
            try:
                self._log_handler.set_session(self.workspace, self.name)
            except Exception as e:
                _LOG.warning("日志 handler 切换失败: %s", e)

    # ========== 自动命名 ==========
    def _ensure_name(self):
        """首轮完成后自动给 session 命名（一句话总结首轮）。name 一旦设定不再变。
        在落盘前调用，确保 _autosave 有稳定文件名。
        若 _ensure_name_early 已在工具调用前异步拿到 name，则此处直接跳过。"""
        if self.name:
            return
        if not self.turns:
            return  # 还没有完成的轮次，等早期命名或下一轮
        first = self.turns[0]
        prompt = ("用一句话（≤20个中文字）总结下面这轮对话的主题，作为会话标题。"
                  "只输出标题文字本身，不要引号、不要任何解释、不要句末标点：\n"
                  f"用户: {first.user_message[:200]}\n回答: {first.answer[:200]}")
        title = ""
        try:
            title = (self.utility_llm or self.llm).chat(
                [{"role": "user", "content": prompt}], scene="title").content.strip()
            title = title.split("\n")[0].strip().strip("。.！!？?\"'“”‘’")
        except Exception:
            title = ""
        safe = _NAME_SAFE_RE.sub("_", title)[:30].strip("_") if title else ""
        with self._name_lock:
            if self.name:   # 双重检查：_ensure_name_early 可能已抢先拿到 name
                return
            if safe:
                self.name = safe
            else:
                # fallback：用首轮 user_message 片段，再不行用时间戳
                seed = _NAME_SAFE_RE.sub("", first.user_message[:12]).strip()
                self.name = ("session_" + seed) if seed else f"session_{int(time.time())}"
            # name 刚就绪：绑定所有持久化路径（建文件夹 + events/toollog/llm_calls + 日志）
            self._bind_persistence_paths()

    def _ensure_name_early(self, user_message: str, reasoning: str = "", tool_names: list = None):
        """第一次工具调用前异步为 session 命名 + 落盘（daemon 线程，不阻塞工具执行）。
        用 LLM 的首轮思考 + 计划调用的工具名替代最终回答，提前推断对话主题。
        与 _ensure_name 通过 _name_lock 互斥：先拿到锁的胜出，另一个在双重检查后跳过。"""
        if self.name:
            return

        def _do_name():
            # 快速检查（无锁）：大概率 _ensure_name 还没跑
            if self.name:
                return
            tools_hint = f" 计划使用工具: {', '.join(tool_names[:5])}" if tool_names else ""
            prompt = ("用一句话（≤20个中文字）总结下面这段对话的主题，作为会话标题。"
                      "只输出标题文字本身，不要引号、不要任何解释、不要句末标点：\n"
                      f"用户: {user_message[:200]}\n"
                      f"思考: {reasoning[:200] or '(无)'}{tools_hint}")
            title = ""
            try:
                title = (self.utility_llm or self.llm).chat(
                    [{"role": "user", "content": prompt}], scene="title").content.strip()
                title = title.split("\n")[0].strip().strip("。.！!？?\"'""''")
            except Exception:
                title = ""
            safe = _NAME_SAFE_RE.sub("_", title)[:30].strip("_") if title else ""
            with self._name_lock:
                if self.name:   # 双重检查：_ensure_name 可能已抢先拿到 name
                    return
                if safe:
                    self.name = safe
                else:
                    seed = _NAME_SAFE_RE.sub("", user_message[:12]).strip()
                    self.name = ("session_" + seed) if seed else f"session_{int(time.time())}"
                # name 刚就绪：绑定所有持久化路径（建文件夹 + events/toollog/llm_calls + 日志）
                self._bind_persistence_paths()
            # 落盘放锁外：_autosave 内部用 _save_lock（不同锁），避免死锁且不阻塞命名线程
            self._autosave()

        threading.Thread(target=_do_name, daemon=True).start()

    # ========== 异步自动落盘 ==========
    def _capture_state(self):
        """落盘前从 Agent 收集附加运行时状态（plan/自主模式等）进 extra_state。
        Agent 通过 self._state_provider 回调注册收集器；未注册则跳过。"""
        if self._state_provider is not None:
            try:
                self.extra_state = self._state_provider() or {}
            except Exception:
                pass

    def _autosave(self):
        """每轮结束后异步落盘（daemon 线程，不阻塞主循环）。失败静默，绝不影响主循环。
        注意：不在本层持锁——save() 内部用同一把锁保护「快照+序列化+写文件」整段，
        本层再持锁会和 save() 二次获取同一把不可重入 Lock 导致死锁。"""
        name = self.name
        if not name:
            return  # name 未就绪本轮跳过（_ensure_name 已尽量保证非空）
        # _capture_state 由 save() 内部完成，此处只负责异步触发 save
        def _write():
            try:
                self.save(name)
            except Exception as e:
                _LOG.error("会话自动落盘失败 %s: %s", name, e)
        threading.Thread(target=_write, daemon=True).start()

    # ========== 召回（Agent / 用户按需查完整原文）==========
    def search_turns(self, keywords, max_hits: int = 20) -> list:
        """按关键字在各轮 user_message+answer+summary 里子串匹配初筛（Agentic RAG 第一阶段，无 LLM）。
        返回 [(turn_idx, turn, [命中的关键字])]，最多 max_hits 条。镜像 toollog.search。"""
        kws = [k for k in (keywords or []) if k]
        if not kws:
            return []
        hits = []
        for i, t in enumerate(self.turns):
            text = ((t.user_message or "") + " " + (t.answer or "") + " " + (t.summary or ""))
            matched = [k for k in kws if k in text]
            if matched:
                hits.append((i, t, matched))
        return hits[:max_hits]

    def recall(self, query: str, contains_reasoning: bool = False) -> str:
        """按关键词/语义在【全部】历史轮次里搜索，返回匹配轮的完整上下文。
        contains_reasoning=False（默认）不含思考过程；True 则带上每步 reasoning 与回答的 reasoning。

        检索策略（自动降级）：
          1. 配了 embed 模型(self.vec_store 非 None) → 语义召回 top-K 轮（换说法也能搜到）
          2. 否则 → summary+user+answer 子串匹配（大小写不敏感，中文直接子串）
        两条都跨当前会话全部 turns；语义路径还覆盖 reasoning 内容（密度更高）。
        """
        if not self.turns:
            return "（当前会话还没有历史轮次）"
        q = (query or "").strip()
        if not q:
            return "（请提供要搜索的关键词）"
        # —— 1) 语义召回（配了 embed 才走）——
        if self.vec_store is not None:
            try:
                hits = self._semantic_hits(q, top_k=5)
            except Exception:
                hits = []   # 向量库异常 → 不阻断，退子串
            if hits:
                out, total, CAP = [f"语义召回 {len(hits)} 轮匹配「{query}」的历史："], 0, 4000
                for i, t, score in hits:
                    block = self._format_turn_full(i + 1, t, contains_reasoning)
                    tag = f" (相似度 {score:.2f})" if score else ""
                    block = f"━━━ 【第{i + 1}轮】{t.summary or '(无摘要)'}{tag}\n" + block.split("\n", 1)[1] \
                        if "\n" in block else block
                    if total + len(block) > CAP:
                        out.append(f"\n…（还有 {len(hits) - len(out) + 1} 轮命中已省略）")
                        break
                    out.append(block)
                    total += len(block)
                return "\n".join(out)
        # —— 2) 子串兜底（没配 embed，或语义无结果）——
        ql = q.lower()
        hits = [(i, t) for i, t in enumerate(self.turns)
                if ql in (t.summary + "\n" + t.user_message + "\n" + t.answer).lower()]
        if not hits:
            return f"未找到包含「{query}」的历史轮次。可用 /recall 换个关键词，或 /show 看概览。"
        out, total, CAP = [f"找到 {len(hits)} 轮匹配「{query}」的历史："], 0, 4000
        for i, t in hits:
            block = self._format_turn_full(i + 1, t, contains_reasoning)
            if total + len(block) > CAP:
                out.append(f"\n…（还有 {len(hits) - len(out) + 1} 轮命中已省略）")
                break
            out.append(block)
            total += len(block)
        return "\n".join(out)

    def _semantic_hits(self, query: str, top_k: int = 5) -> list:
        """语义召回 → 映射到当前 session 的 turns。返回 [(turn_idx, Turn, score)]。

        只取当前 session 的 turn（vec_store 跨 session 索引，但 recall 限本会话；
        跨 session 由 before_turn_retrieval 工作流的 semantic_search_history 负责）。"""
        sid = self.name or (self.session_dir.name if self.session_dir else "")
        results = self.vec_store.search(query, top_k=top_k, session_id=sid)
        # vec_store 的 turn_no 是 1-based（与 _format_turn_full 的 n 对齐）
        hits = []
        for r in results:
            tno = r["turn_no"] - 1     # → 0-based turns 索引
            if 0 <= tno < len(self.turns):
                hits.append((tno, self.turns[tno], r.get("score", 0)))
        return hits

    def _format_turn_full(self, n: int, t: Turn, contains_reasoning: bool = False) -> str:
        """把一轮格式化成可读文本（召回展示用）。contains_reasoning=True 时带上每步与回答的 reasoning。"""
        lines = [f"━━━ 【第{n}轮】{t.summary or '(无摘要)'}", f"用户: {t.user_message}"]
        for step in t.steps:
            if contains_reasoning and step.reasoning:
                lines.append(f"  💭 {step.reasoning}")
            for tc in step.tool_calls:
                name, a, r = self.toollog.view(tc.call_id)
                args_s = json.dumps(a, ensure_ascii=False)
                lines.append(f"  🔧 {name}({args_s}) → {(r or '')[:300]}")
        lines.append("回答:")
        lines.append(render_cli(t.answer))
        if contains_reasoning and t.answer_reasoning:
            lines.append(f"  💭(回答推理) {t.answer_reasoning}")
        return "\n".join(lines)

    def to_history(self, fold_count: int = 0, start_turn: int = 0, end_turn: int = None) -> list:
        """导出历史（结构化），供 webui resume 后渲染。含每步 reasoning 与回答的 reasoning。
        tool_calls 的 result 截断到 500 字（渲染够用）。
        start_turn/end_turn：只渲染 [start_turn, end_turn) 区间的轮（0-based，end 缺省=到末尾），
        turn 字段仍是全 session 的绝对轮号（前端展开时序号正确）。读档时 server 用
        _tier_boundaries 传 start_turn=当前档起点，前端点"展开更早"再请求更早一档。"""
        out = []
        turns = self.turns[start_turn:(end_turn if end_turn is not None else len(self.turns))]
        offset = start_turn
        for i, t in enumerate(turns):
            steps = []
            for s in t.steps:
                tcs = []
                for tc in s.tool_calls:
                    n, a, r = self.toollog.view(tc.call_id)
                    tcs.append({"name": n, "arguments": a, "result": (r or "")[:500],
                                "call_id": tc.call_id})
                if tcs:
                    steps.append({"tool_calls": tcs, "reasoning": s.reasoning or ""})
            out.append({"turn": offset + i + 1, "user": t.user_message, "answer": t.answer,
                        "summary": t.summary, "steps": steps,
                        "answer_reasoning": t.answer_reasoning or ""})
        return out

    def to_history_full(self, max_turns: int = None) -> list:
        """导出全量历史的【未截断】版：结构与 to_history 相同，但 tool_calls 的 result 不截断
        （从 toollog.view 取完整原文）。供 get_session_history 工具给工作流节点编排检索/重排/投影用——
        工作流节点拿全量后自行决定怎么过滤、投影、截断。max_turns 非空只返回最近 N 轮。"""
        turns = self.turns[-max_turns:] if max_turns else self.turns
        offset = len(self.turns) - len(turns)
        out = []
        for i, t in enumerate(turns):
            steps = []
            for s in t.steps:
                tcs = []
                for tc in s.tool_calls:
                    n, a, r = self.toollog.view(tc.call_id)
                    tcs.append({"name": n, "arguments": a, "result": r or "",
                                "call_id": tc.call_id})
                if tcs:
                    steps.append({"tool_calls": tcs, "reasoning": s.reasoning or ""})
            out.append({"turn": offset + i + 1, "user": t.user_message, "answer": t.answer,
                        "summary": t.summary, "steps": steps,
                        "answer_reasoning": t.answer_reasoning or ""})
        return out

    # ========== 持久化 ==========
    def save(self, name: Optional[str] = None) -> Path:
        """落盘 meta.json 到 session 专属文件夹。name 参数（可选）用于 /save <name> 改名另存——
        只改 meta.json 里的 name 字段，不挪文件夹（文件夹名始终是创建时间戳）。
        文件夹未就绪（session_dir 为 None，新 session 还没 _ensure_name）时先创建。"""
        if name:
            self.name = name  # /save <name> 改名：只改 self.name，meta.json 会写入新名
        self._capture_state()  # 落盘前收集 Agent 附加状态（plan/自主模式等），无论谁触发 save
        sdir = self._ensure_session_dir()  # 确保文件夹存在（新 session 首次 save）
        # repo 级 _origin.txt（记录工作区路径，便于排查）
        (sdir.parent.parent / "_origin.txt").write_text(str(self.workspace.resolve()), encoding="utf-8")
        path = sdir / "meta.json"
        # 锁保护「快照 turns + 序列化 + 写文件」整段：与 _autosave 的 daemon 线程、
        # 以及 /save 命令的并发写互斥；list(self.turns) 快照后，主线程 append 新 turn 不影响本次落盘。
        with self._save_lock:
            # turns/toollog 不存这里——turns 走 events.jsonl（append-only 事件流），
            # toollog 走 toollog.jsonl。meta.json 只存小体量元信息+状态，全量写无压力。
            data = {
                "name": self.name or f"session_{int(self.created_at)}",
                "created_at": self.created_at,           # 创建时间戳（文件夹名来源 + 排序依据）
                "system": self.system,
                "global_summary": self.global_summary,
                "recent_window_turns": self.recent_window_turns,
                "max_steps_per_turn": self.max_steps_per_turn,
                "extra_state": self.extra_state,          # 附加运行时状态（plan/自主模式等）
                "tier_boundaries": self._tier_boundaries,  # 分档毕业边界（持久化；_frozen_renders 内存重算）
                "saved_at": int(time.time()),
            }
            # 原子写：先写 .tmp 再 os.replace，避免 autosave(daemon 线程) 与 load 并发时读到半个文件
            tmp_path = path.with_suffix(path.suffix + ".tmp")
            tmp_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp_path, path)
        return path

    def rename(self, new_name: str) -> str:
        """重命名当前会话：只改 meta.json 里的 name 字段 + 更新 self.name。
        文件夹名是创建时间戳，不随 rename 改变。新名冲突或非法抛 ValueError。
        未命名（还没存档）时只设 name，下次 save 落盘时会写入新名。"""
        new_name = self._sanitize_session_name(new_name)
        if not new_name:
            raise ValueError("新会话名不能为空")
        if new_name == self.name:
            return self.name
        # 检查名字冲突：遍历所有 session 的 meta.json
        sdir = self._ensure_session_dir()
        for other_dir in sdir.parent.iterdir():
            if not other_dir.is_dir() or other_dir == sdir:
                continue
            meta = other_dir / "meta.json"
            if meta.exists():
                try:
                    data = json.loads(meta.read_text(encoding="utf-8"))
                    if data.get("name") == new_name:
                        raise ValueError(f"已存在同名会话「{new_name}」，换个名字")
                except Exception:
                    pass
        old_name = self.name
        self.name = new_name
        # 抢先命名（rename_session 工具在首轮 _ensure_name 前调用）时补绑 events/toollog
        # 路径——否则 _ensure_name 因 self.name 已设而跳过，events 不落盘。
        if self._event_path is None:
            self._bind_persistence_paths()
        self.save()  # 总是 save：session_dir 已建，覆盖写 meta.json 把 name 刷成新的
        return new_name

    @staticmethod
    def _sanitize_session_name(name: str) -> str:
        """清洗会话名（作文件名）：去首尾空白，非法字符（/ \\ : * ? " < > |）替成 _。"""
        s = (name or "").strip()
        s = re.sub(r'[/\\:*?"<>|]', "_", s)
        return s.strip()

    @classmethod
    def load(cls, path_or_name: str, llm: Optional[LLMClient] = None, workspace=None) -> "Session":
        ws = workspace or Path.cwd()
        path = _resolve_session_path(path_or_name, ws)
        data = json.loads(path.read_text(encoding="utf-8"))
        s = cls(system=data["system"], llm=llm,
                recent_window_turns=data.get("recent_window_turns", 4),
                max_steps_per_turn=data.get("max_steps_per_turn", 80), workspace=ws)
        s.name = data.get("name") or path.parent.name  # 旧存档/无 name → 用文件夹名或文件名
        s.created_at = data.get("created_at") or _ts_from_dirname(path.parent) or time.time()
        s.global_summary = data.get("global_summary", "")
        s.extra_state = data.get("extra_state", {})
        s._tier_boundaries = data.get("tier_boundaries", []) or []
        # 判断是新文件夹结构还是旧扁平结构
        is_new_structure = path.name == "meta.json"
        if is_new_structure:
            # —— 新文件夹结构：<timestamp>/meta.json + events.jsonl + toollog.jsonl + llm_calls.jsonl ——
            sdir = path.parent
            s.session_dir = sdir
            events_path = sdir / "events.jsonl"
            toollog_path = sdir / "toollog.jsonl"
            llm_calls_path = sdir / "llm_calls.jsonl"
        else:
            # —— 旧扁平结构：<name>.json + <name>.events.jsonl + ...（一次性迁移成新结构）——
            stem = path.stem
            sdir = _new_session_dir(ws, s.created_at)
            s.session_dir = sdir
            events_path = sdir / "events.jsonl"
            toollog_path = sdir / "toollog.jsonl"
            llm_calls_path = sdir / "llm_calls.jsonl"
            old_events = path.parent / f"{stem}.events.jsonl"
            old_toollog = path.parent / f"{stem}.toollog.jsonl"
            old_llm_calls = path.parent / f"{stem}.llm_calls.jsonl"
            # 旧文件存在则复制到新文件夹（后续按新路径读写）
            if old_events.exists():
                shutil.copy2(old_events, events_path)
            if old_toollog.exists():
                shutil.copy2(old_toollog, toollog_path)
            if old_llm_calls.exists():
                shutil.copy2(old_llm_calls, llm_calls_path)
        if events_path.exists():
            # —— 新格式：重放事件流重建 turns（未完成 turn 进 turns，不丢弃）——
            s.turns = _replay_events(_read_events(events_path))
            if toollog_path.exists():
                s.toollog.load_from_jsonl(toollog_path)
            s.toollog.set_path(toollog_path)
            if llm_calls_path.exists():
                s.llm_calls.load_from_jsonl(llm_calls_path)
            s._bind_event_path(events_path)   # 绑定（缓冲为空，不覆盖已有）
            s._load_recaps(sdir)              # 恢复各轮 recap（异步产物不进事件流，sidecar 持久化）
        elif "turns" in data:
            # —— 旧格式迁移：meta.json 里有 turns（+ 可能 toollog 字段），一次性转成事件流 ——
            s.toollog.load_list(data.get("toollog", []))            # 0.7.4 嵌入字段进内存
            old_turns = [_turn_from_dict(t, s.toollog) for t in data["turns"]]  # 更老的 ToolCall 在此迁移 record(buffer)
            s.toollog.set_path(toollog_path)                         # flush toollog 内存（含迁移项）建 jsonl
            s._bind_event_path(events_path)                          # 建 events.jsonl
            for t in old_turns:                                      # 旧 turns → 事件 append
                s._emit_event({"event": "turn_start", "user": t.user_message, "images": t.images})
                if t.snapshot_sha:
                    s._emit_event({"event": "snapshot", "sha": t.snapshot_sha})
                for step in t.steps:
                    s._emit_event({"event": "step", "reasoning": step.reasoning or "",
                                   "call_ids": [tc.call_id for tc in step.tool_calls]})
                s._emit_event({"event": "turn_end", "answer": t.answer,
                               "answer_reasoning": t.answer_reasoning, "summary": t.summary})
            s.turns = old_turns
        else:
            s.turns = []
        s.llm_calls.set_path(llm_calls_path)  # 绑定 llm_calls（老存档无此文件则空建）
        s._summary_sig = ()  # 让首次 _refresh_summary_cache 重算
        s._plan_fold()       # 读档即计划（首个投影前 _planned_fold 就绪；turns/boundaries 已恢复）
        return s

    # ========== 展示 ==========
    def summary_str(self) -> str:
        lines = []
        if self.name:
            lines.append(f"会话名称: {self.name}")
        lines.append(f"已完成轮数: {len(self.turns)}")
        lines.append(f"近期窗口: 最近 {self.recent_window_turns} 轮（原文），更早的以摘要喂给模型、原文仍可召回")
        if self.global_summary:
            lines.append(f"窗口外摘要({len(self.global_summary)}字): {self.global_summary[:200]}...")
        lines.append("近期轮次:")
        for i, t in enumerate(self.turns[-5:], 1):
            n_tools = sum(len(s.tool_calls) for s in t.steps)
            lines.append(f"  {i}. 「{t.user_message[:30]}」→ {n_tools}次工具调用 →「{t.answer[:30]}」")
        return "\n".join(lines)

    def __repr__(self):
        return f"Session(name={self.name!r}, turns={len(self.turns)}, summary={'yes' if self.global_summary else 'no'})"


def _turn_from_dict(d: dict, toollog) -> Turn:
    t = Turn(user_message=d.get("user_message", ""),
             images=d.get("images", []),
             snapshot_sha=d.get("snapshot_sha", ""),
             answer=d.get("answer", ""), answer_reasoning=d.get("answer_reasoning", ""),
             summary=d.get("summary", ""))
    for s in d.get("steps", []):
        step = Step(reasoning=s.get("reasoning", ""))
        for tc in s.get("tool_calls", []):
            cid = tc.get("call_id")
            if cid and toollog.get(cid) is not None:
                # 新格式：详情已在 toollog（load_list 已恢复），ToolCall 只存 id
                step.tool_calls.append(ToolCall(call_id=cid))
            else:
                # 旧格式（有 name/arguments/result、无 call_id/toollog）或孤儿：迁移进 toollog
                cid = toollog.next_id()
                toollog.record(cid, tc.get("name", ""), tc.get("arguments", {}), tc.get("result", ""))
                step.tool_calls.append(ToolCall(call_id=cid))
        t.steps.append(step)
    return t


def _read_events(path) -> list:
    """流式读 events.jsonl 全部事件（每行一个 JSON）。"""
    events = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    events.append(json.loads(line))
                except Exception:
                    continue
    except Exception:
        pass
    return events


def _replay_events(events: list) -> list:
    """重放事件流重建 turns。
    - turn_start/snapshot/step/turn_end 还原 Turn>Step>ToolCall 树；
    - restore 事件截断（保留前 keep 轮）；
    - 未完成 turn（有 turn_start 无 turn_end）→ 进 turns（无 answer，不丢弃，作历史保留）。"""
    turns = []
    cur = None
    for e in events:
        et = e.get("event")
        if et == "turn_start":
            if cur is not None:
                turns.append(cur)   # 防御：上个 turn 未等到 turn_end
            cur = Turn(user_message=e.get("user", ""), images=e.get("images", []),
                       snapshot_sha="", steps=[])
        elif et == "snapshot" and cur is not None:
            cur.snapshot_sha = e.get("sha", "")
        elif et == "step" and cur is not None:
            cur.steps.append(Step(reasoning=e.get("reasoning", ""),
                                  tool_calls=[ToolCall(call_id=c) for c in e.get("call_ids", [])]))
        elif et == "turn_resume":
            # 中断轮恢复事件（resume_interrupted 发）：最后一个已归档 turn 弹回进行中状态。
            # 重放闭环：turn_end(中断) → turn_resume → step… → turn_end(最终)。
            if cur is None and turns:
                cur = turns.pop()
                if _is_interrupt_mark(cur.answer or ""):
                    cur.answer = ""
                    cur.answer_reasoning = ""
        elif et == "turn_end":
            if cur is not None:
                cur.answer = e.get("answer", "")
                cur.answer_reasoning = e.get("answer_reasoning", "")
                cur.summary = e.get("summary", "")
                turns.append(cur)
                cur = None
        elif et == "restore":
            turns = turns[:e.get("keep", 0)]
            cur = None   # 回溯丢弃进行中的 turn
    if cur is not None:
        turns.append(cur)   # 未完成 turn：不丢弃，作为无 answer 的历史 turn
    return turns


def _find_session_dir_by_name(workspace, name: str) -> Optional[Path]:
    """按 session name 查找对应的文件夹（遍历 sessions/ 下各时间戳文件夹的 meta.json）。
    找不到返回 None。"""
    repo_dir = _repo_sessions_dir(workspace)
    for ts_dir in repo_dir.iterdir():
        if not ts_dir.is_dir():
            continue
        meta_path = ts_dir / "meta.json"
        if not meta_path.exists():
            continue
        try:
            data = json.loads(meta_path.read_text(encoding="utf-8"))
            if data.get("name") == name:
                return ts_dir
        except Exception:
            continue
    return None


def _resolve_session_path(path_or_name: str, workspace=None) -> Path:
    """查找会话 meta.json 文件：
    1. 如果 path_or_name 是绝对路径且存在，直接返回其 meta.json
    2. 如果是时间戳文件夹名（YYYYMMDD_HHMMSS 或带 _N 后缀），直接构造路径
    3. 否则按 name 搜索所有 session 的 meta.json
    4. 回退旧扁平结构 ~/.agt/sessions/<hash>/*.json
    返回的都是 meta.json 文件路径。"""
    ws = workspace or Path.cwd()
    repo_dir = _repo_sessions_dir(ws)
    legacy_dir = SESSIONS_DIR / _repo_hash(ws)

    # 1. 绝对路径
    abs_cand = Path(path_or_name)
    if abs_cand.is_absolute() and abs_cand.exists():
        if abs_cand.name.endswith(".json"):
            return abs_cand
        # 假设是时间戳文件夹，返回其 meta.json
        meta_cand = abs_cand / "meta.json"
        if meta_cand.exists():
            return meta_cand

    # 2. 时间戳文件夹名（纯数字 + 下划线）
    if re.match(r"^\d{8}_\d{6}(_\d+)?$", path_or_name):
        ts_dir = repo_dir / path_or_name
        meta_cand = ts_dir / "meta.json"
        if meta_cand.exists():
            return meta_cand

    # 3. 按 name 搜索
    found = _find_session_dir_by_name(ws, path_or_name)
    if found:
        return found / "meta.json"

    # 4. 回退旧扁平结构
    for cand in (Path(path_or_name), legacy_dir / path_or_name, legacy_dir / (path_or_name + ".json")):
        if cand.exists():
            return cand

    raise FileNotFoundError(f"找不到会话: {path_or_name}（可在 /list 查看）")


def list_sessions(workspace=None) -> list[dict]:
    """列出该工作区所有 session，返回 [{id, name, created_at, turns, first}] 列表。
    id = 时间戳文件夹名（用于 load 时定位）；按 created_at 倒序（新的在前）。
    自动扫描 sessions/ 下各时间戳文件夹的 meta.json；兼容旧扁平结构。"""
    ws = workspace or Path.cwd()
    repo_dir = _repo_sessions_dir(ws)
    legacy_dir = SESSIONS_DIR / _repo_hash(ws)
    results = []

    # 新结构：扫描时间戳文件夹
    for ts_dir in repo_dir.iterdir():
        if not ts_dir.is_dir():
            continue
        meta_path = ts_dir / "meta.json"
        if not meta_path.exists():
            continue
        try:
            data = json.loads(meta_path.read_text(encoding="utf-8"))
            turns_count = len(data.get("turns", []))  # 旧格式兼容
            first = ""
            if turns_count > 0 and "turns" in data:
                first = (data["turns"][0].get("user_message", "") or "")[:30]
            results.append({
                "id": ts_dir.name,
                "name": data.get("name") or ts_dir.name,
                "created_at": data.get("created_at"),
                "turns": turns_count,
                "first": first,
            })
        except Exception:
            continue

    # 旧扁平结构兼容：*.json
    if legacy_dir.exists():
        for f in legacy_dir.glob("*.json"):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                turns = data.get("turns", [])
                first = (turns[0].get("user_message", "") if turns else "")[:30]
                results.append({
                    "id": f.stem,
                    "name": data.get("name") or f.stem,
                    "created_at": data.get("created_at"),  # 旧格式可能无
                    "turns": len(turns),
                    "first": first,
                })
            except Exception:
                continue

    # 按 created_at 倒序（无 created_at 的排最后）
    results.sort(key=lambda x: x.get("created_at") or 0, reverse=True)
    return results


def session_meta(p: Path) -> dict:
    """轻量读一个会话的展示元信息：{id, name, created_at, turns, first}。
    p 可以是 meta.json 路径或时间戳文件夹路径。读取出错返回兜底。"""
    # 标准化为 meta.json 路径
    if p.is_dir():
        p = p / "meta.json"
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        turns = data.get("turns", [])
        first = (turns[0].get("user_message", "") if turns else "")[:30]
        parent = p.parent
        return {
            "id": parent.name if parent.name != "sessions" else p.stem,
            "name": data.get("name") or p.stem,
            "created_at": data.get("created_at"),
            "turns": len(turns),
            "first": first,
        }
    except Exception:
        parent = p.parent if p.is_file() else p
        return {"id": parent.name, "name": p.stem, "created_at": None, "turns": 0, "first": "(读取失败)"}
