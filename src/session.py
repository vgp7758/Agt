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
import bisect
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
GRADUATE_FORCE_TURNS = 30  # 卫生性强档触发线：当前档超过此轮数时，无窗口压力也强制分批毕业（防档1 无限膨胀——
GRADUATE_FORCE_BATCH = 15  # 卫生性每刀批量：触发后一次升【前 15 轮】（用户裁定 2026-09-16：触发线 30 / 每刀 15）
RF_MAX_CHARS = 100_000  # recent-file 快照单文件上限·施工期内嵌口径（用户裁定 2026-08-31）：超大文件全文注入让近期缓存上蹿下跳
RF_SEG_MAX_CHARS = 15_000  # 非施工段式口径（用户裁定 2026-09-15）：尾部 <recent-file> 段是每步
                          # 重渲染的易变项，>15K 即转 outline——段体积压小，尾部 miss 区代价低
                         # （index.html 130K 单步稀释命中率 99%→81%）——超过则跳过全文、只挂一行提示
                           # 8000 实例实测 64 轮档1 占 58.6%：窗口宽绰时压力循环永不触发，档1 失去"近期窗口"语义）
RECENT_FULL_STEPS = GROUP_STEPS   # 兼容旧引用（组号差≤1 = 当前组+上一组 ≈ 最近 1~2 组全量）
FULL_STEP_CAP_CHARS = 32000   # 全量步的单步上限（≈8000 token；超过则截断标注 call_id，可 get_tool_detail 取完整）
# <img>name</img> 标签：工具图片落盘后的占位（投影时按模型 vision 能力转 image_url 或文字占位）
_IMG_TAG_RE = re.compile(r"<img>([^<]+)</img>")
_GEN_IMG_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}   # 生成图自动可视的扩展白名单（2026-10-07）
# recent-file 段（2026-09-07·第四版：独立装配段 _seg_msgs_recent_file，不再内嵌 tool result）：
# _RE_RF_BLOCK/_rf_stripped/_rf_in_msgs 保留旧内嵌形态的兜底（剥离/诊断口径）
_RE_RF_BLOCK = re.compile(r"\n<recent-file[\s\S]*?</recent-file>")

# 会话存档放用户主目录：~/.agt/repos/<repo-hash>/sessions/。每个 repo 一棵目录树
# （sessions/ + 未来可加其它子目录），互相隔离。放包目录会在 pip 安装后写进
# site-packages（不可写/难找），故统一到 ~/.agt，与 models.json/settings.json 同惯例。
from paths import AGT_DIR
REPOS_DIR = AGT_DIR / "repos"
# 实测 token 用量流水（react 每次成功回包 observe_llm_usage 追加一条）：既是超窗观察日志，
# 也是 chars/token 校准比率的持久化源——新 session init 回读末尾同模型记录作比率初值。
TOKEN_USAGE_FILE = AGT_DIR / "token_usage.jsonl"
# 旧位置（用于一次性自动迁移；SESSIONS_DIR 同时保留作 legacy 别名供 commands.py 等 import）：
SESSIONS_DIR = AGT_DIR / "sessions"                              # 上一版 ~/.agt/sessions/<hash>/
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


def _sessions_root_of(sdir: Path) -> Path:
    """从会话目录反查 sessions 根（2026-10-09 平铺化，用户裁定：分支不再嵌 branches/）。
    新形态：分支与主线平级直接在根下（<root>/<id>/）→ parent 即根；
    旧形态（迁移前残留）：分支嵌在主线 branches/ 下（<root>/<主线>/branches/<id>/）→ parents[2]。"""
    return sdir.parents[1] if sdir.parent.name == "branches" else sdir.parent


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


def _next_letter_id(existing: set) -> str:
    """字母序短 id（用户提案 2026-10-09）：a/b/c/…/z/aa/ab/…——最小未占用（跳空复用）。
    session 目录名 / 分支目录名共用（身份≠名称：name 是 meta.json 的显示字段，目录名即 id）。"""
    def _inc(s: str) -> str:
        if not s:
            return "a"
        if s[-1] < "z":
            return s[:-1] + chr(ord(s[-1]) + 1)
        return _inc(s[:-1]) + "a"
    cand = "a"
    while cand in existing:
        cand = _inc(cand)
    return cand


def _new_session_dir(workspace, created_ts: float) -> Path:
    """为一个新 session 创建以字母 id 命名的专属文件夹（a/b/c/…/z/aa/ab…，最小未占用）。
    用户提案 2026-10-09：目录名即 session_id（反正都是字符串）；name 只做 meta.json 显示
    字段（/rename 不动目录）。旧时间戳目录（YYYYMMDD_HHMMSS）共存，互不影响——解析层
    （_resolve_session_path）按 meta.name 搜索，对两种形态天然兼容。
    created_at 语义不变（存 meta，目录名不再承载时间信息）。"""
    repo_dir = _repo_sessions_dir(workspace)
    try:
        existing = {d.name for d in repo_dir.iterdir()
                    if d.is_dir() and re.fullmatch(r"[a-z]+", d.name)}
    except Exception:
        existing = set()
    sid = _next_letter_id(existing)
    base = repo_dir / sid
    base.mkdir(parents=True, exist_ok=True)
    return base

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
    changed: list = field(default_factory=list)  # 该调用前后真实产生的文件 diff 清单（快照对比；前端「有 diff 不可折叠」判定 + events 重放持久化）


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
# "（异常中断："（用户实锤 2026-09-22·D--Programs-env session）：run() 异常逃出路径
# abort_current_turn(f"（异常中断：{type(e).__name__}）") 写入的形态——此前集合漏了它，
# resume_interrupted 把异常中断轮判成"已正常完成"拒绝恢复（点继续恒报错）。
_INTERRUPT_MARKS = ("（中断，本轮未完成", "（被中断", "（被用户停止", "（被用户中断", "（异常中断：")
#   ↑ 前缀一律不带尾括号：既匹配裸文案"（被中断）"，也匹配带原因后缀的
#     "（中断，本轮未完成——LLM 502: ...）"/"（异常中断：RuntimeError）"（用户实锤 2026-09-22：
#     带尾括号的前缀对后缀形态 startswith 恒 False → 异常中断轮被判"已正常完成"拒绝恢复）


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
    git_head: str = ""                               # 该轮发送前用户真仓库 HEAD（rewind 撞车检测：检查点后
                                                     # 有 git 提交则回溯会与 git 历史冲突——chat.restore_snapshot 拦）
    steps: list = field(default_factory=list)        # list[Step]
    answer: str = ""
    answer_reasoning: str = ""                       # 最终回答那步的 reasoning_content（GLM 等要求回传）
    summary: str = ""                                # 该轮的一句话摘要（finish 时生成，贴在该轮最后）
    recap: str = ""                                  # 一句话 recap（turn_end 异步生成：队友看板 + fc 折叠摘要行）
    changed: list = field(default_factory=list)      # 本轮文件变更清单 [{"file","change"}]（各步快照 diff 按
                                                     # 文件去重后写覆盖——webui answer 下补充渲染用）


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
    未注册名 → 保留原占位（不炸装配）；执行异常 → 保留占位（不炸装配）。
    内插空判（用户决定 2026-08-29）：任一【已注册】占位求值为空串 → 整段返回 ""（装配侧
    据此丢弃该段）——混合文本（'【远程实例】\\n{func:load_remote_instances()}'）无连接时
    只剩标题空壳，不如不注入；纯占位项空结果本就丢弃，此判定把语义补齐到混合形态。
    load_remote_instances 无连接时静默不注入的设计正依赖此语义——空结果≠失败。
    声明投影（load_agents 等）每次 build 重读——create_agent 后立即派活（高频场景）当轮生效；
    轮内编辑声明破缓存属低频可接受代价。"""
    import re as _re
    empty = False
    def _rep(m):
        nonlocal empty
        from agent_config import FUNC_REGISTRY
        name = m.group(1).strip().rstrip("()").strip()
        fn = FUNC_REGISTRY.get(name)
        if fn is None:
            return m.group(0)          # 未注册：保留占位（提示声明写错了）
        try:
            out = str(fn() or "")      # 已注册：空串也替换（无连接=不注入）
        except Exception:
            return m.group(0)          # 执行异常：保留占位（不炸装配）
        if not out.strip():
            empty = True
        return out
    txt = _re.sub(r"\{func:([^}]+)\}", _rep, text)
    return "" if empty else txt


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
        # 复用模式投影开关（子 Agent agent_prompt 默认复用 / reuse=no 显式新建时 False）：True 时历史轮一律不投影，
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
        # 生成图自动可视（2026-10-07 用户提案）：after_tool 收集本轮新增图片 → 投影尾部
        # 伪造「read_file 调用+结果(带 image_url)」对——视觉模型"以为"自己调用过 read_file。
        # 瞬态：组装层注入，不落 event.jsonl/step 存档。
        self._gen_images_pending: list = []   # [{path, call_id, data(dataURL), kb}]
        self._gen_images_done: set = set()    # 去重记账（同文件一次任务只注入一次）
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
        # 分支元数据（用户提案 2026-10-08）：None=主线/普通会话；分支会话为
        # {branch_of: 主线目录名(时间戳文件夹名), inherit_lines: 继承主线 events.jsonl 前 N 行,
        #  base_hash: 主线前N行内容 sha256[:16]（加载时校验基底漂移）}
        # 分支的 events/toollog/llm_calls 只写自己目录（写侧天然隔离）；读侧 Session.load
        # 合成「主线前N行 + 分支行」完整事件流喂重放器——投影/tier/折叠引擎零感知。
        self.branch_meta: Optional[dict] = None
        self._event_path = None   # 事件日志路径 <name>.events.jsonl；None 时事件 buffer 在内存（name 未就绪）
        self._event_buffer: list[dict] = []  # name 就绪前缓冲的事件（turn_start/step/snapshot/...）
        # —— 分档上下文投影（provider 设 max_effective_context_window 才启用，否则走原 recent_window+summary）——
        self.max_effective_context_window = getattr(self.llm, "max_effective_context_window", None)
        # 折叠目标线比例（per-provider，llm profile 的 fold_target_ratio；None=引擎默认 0.75）——
        # 缓存未命中折扣悬殊的 provider（DeepSeek≈60x vs GLM≈4x）应调高：晚折叠、保前缀稳定。
        # 同步点：switch_model / /reload models / WebUI 保存模型配置（与窗口同步同款）。
        self.fold_target_ratio = getattr(self.llm, "fold_target_ratio", None)
        # 组间步距衰减（per-provider 覆盖全局 settings；0=不衰减——所有组 limit 恒定，
        # 老步骤渲染字节稳定 → 前缀缓存打满。DeepSeek 类 60x 差价 provider 推荐 0：
        # 宁可投影大（命中便宜）也不让组边界衰减重截老步骤断缓存——用户裁定 2026-08-30）。
        self.profile_detail_step = getattr(self.llm, "profile_detail_step", None)
        self._detail_base = None   # 惰性缓存（detail_base property；/config / switch_model / /context 时失效重读）
        self.max_level = config.load_max_level()
        self._tier_boundaries: list[int] = []                    # 已毕业的 turn 索引边界，如 [5,10]
        self._frozen_renders: dict[int, tuple[int, list]] = {}   # turn_idx -> (level, msgs) 冻结渲染缓存
        self._last_fold_count: int = 0   # 最近一次分档 build 的折叠轮数（to_history 用它折叠前端历史）
        self._planned_fold: int = 0      # 轮边界折叠计划（start_turn 时算好折到 75%；轮内 _build 以它为起点，不再轮内折叠）
        self._planned_graduates: int = 0 # 轮边界毕业计划（start_turn 时算好升几档；轮内 _build 以它为起点，不再轮内升档）
        # sos（summary of summary，用户提案 2026-10-02）：折半到全折仍超预算时的终极压缩——
        # fc 结构摘要清单的前 _sos_count 轮由 LLM 浓缩成一份叙事摘要（_sos_text）替代，
        # 清单只保留次早期段。内容跨模型通用（切模型不重生成）；持久化到 meta。
        self._sos_text: str = ""
        self._sos_count: int = 0
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
        # —— system 段 append-not-replace 账本（2026-09-12·用户提案，DSH 断点清账架构；spec s_eb14a8fd）——
        # 实测背书（api.deepseek.com·deepseek-flash）：尾部 append 一条 system 前缀 hit 94.3% 完整命中；
        # 头部 replace 则 0%。决策表（_apply_system_ledger）：
        #   新渲染 == 上次投影形态      → 原样（byte-stable，全命中）
        #   变化 && in_history_system && !dirty → append 新版本【按轮锚定】（v2·形态A）：轮 N 进行中插
        #       当前轮 user 前；轮 N 归档后由 _render_tiered_history 固定插在轮 N 块前——位置永不漂移，
        #       前缀跨轮稳定（首版"浮动插入"每轮漂移断缓存，用户两图对照抓出后修正）
        #   dirty / 不支持 / 堆积>4    → 归一化单条（断点处免费清账：毕业/折叠/tools hash 变化
        #       本就断缓存，顺手收敛版本）
        # appends: [{"turn": N(1-based), "text": ...}]——多版本共存于历史原位（DSH system/message 节点同款）。
        # last_text 存含回答风格提示的最终文本（比较点在 _append_answer_style 拼接之后）。
        self._system_ledger: dict = {"last_text": "", "appends": [], "dirty": True}
        self._in_history_system: bool = False  # provider 能力位（agent 启动/切模型时设置；False=现状归一化）
        self._ledger_form: str = ""            # 最近一次投影的 system 段形态（byte-stable/appended vN/normalized——/context 观测用）
        self._load_calibration()              # 回读 ~/.agt/token_usage.jsonl 末尾同模型记录作初值（跨 session 校准）
        # —— 投影分段统计（真实装配时顺手记录，/context 直接读——见 messages_for_llm 尾部）——
        # None=本进程还没跑过投影（projection_breakdown 回退现算）；否则 {"sections":[...], ts, turn, step, ...}
        self._proj_stats: Optional[dict] = None
        self._hist_marks: Optional[list] = None   # 装配进行中的 history 子段标记 [(name, 段内偏移, meta)]（临时态）
        # 语义召回层（build_agent 注入；None=未配 embed → recall 退回子串）
        self.vec_store = None
        # —— 施工投影缓冲（2026-09-15·用户提案，治施工期缓存命中上蹿下跳）——
        # turn 级 append-only：每个 step 以 full 形态一次定型渲染成 msgs，append 后永不
        # 改写 → 前缀 LCP 只增长不失配。原 _steps_to_messages 每次投影全量重渲染，同 step
        # 快照采集时机（agent.py add_step 后统一采）+ 后续写操作会回溯改写前序 tool result
        # 尾部的 <recent-file> 内嵌块——t861_s38-s76 实测命中率上蹿下跳的根因。
        # 惰性 sync（_seg_msgs_steps 施工分支调 _constr_sync）：非施工轮零开销；施工激活前
        # 的 explore steps / 后续新 step / 重启 resume 回填统一走同一路径，形态一致字节稳定。
        # 运行时内存不落盘：重启后缓冲为空，首次投影按 steps 回填（file_snapshots 运行时
        # 重建，形态与 append 同口径）。
        self._constr_buf: list[list[dict]] = []   # 每 step 一组定型 msgs（与 _current.steps 对齐）
        # —— 跨轮施工流（2026-09-17·用户裁定：从施工开始的轮全部保留）——
        # 用户权衡：上下文膨胀快，但施工起始总上下文低（history 不装配）+ 持续 append-only
        # → 缓存与思维链跨轮连续，完成任务更快。每轮 finish 时把 [本轮 user + 本轮 steps 定型]
        # 追加进流；施工期投影在 user_message 段位置整体输出（历史施工轮 u/s 交替）+ 当前轮 user，
        # steps 段输出当前轮 buf——序列 = 完整施工聊天记录。plan 全完成（收工）即清空。
        # 持久化：extra_state["constr_start_idx"]（起始轮号）——重启后按 turns 惰性重建。
        self._constr_stream: list[dict] = []

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

    def fold_target(self) -> int:
        """折叠目标线（tok）= win × ratio。ratio 来自 llm profile（fold_target_ratio，
        per-provider 缓存经济学参数——DeepSeek 未命中≈60x 应调高如 0.95：晚折叠、
        保前缀稳定；GLM≈4x 用默认即可）或引擎默认 FOLD_TARGET_RATIO(0.75)。
        win 未配置（分档禁用）返回 0。轮边界计划与保命阀回落目标共用本线。"""
        win = self.max_effective_context_window
        if not win:
            return 0
        ratio = self.fold_target_ratio if self.fold_target_ratio else FOLD_TARGET_RATIO
        return int(win * ratio)

    @property
    def detail_step(self) -> int:
        """组间步距衰减（字/组距）：只认 profile.detail_step（模型卡片 per-provider），
        未填默认 0=不衰减（用户裁定 2026-09-15：全局 settings 的 detail_step 字段删除——
        全局 15 曾让所有未配置 provider 的轮内组边界持续回缩，频繁触发轮内小毕业断缓存）。
        0 = 不衰减——当前轮更早组的 limit 与近组相同，渲染字节永不回缩 → 前缀缓存打满。"""
        if self.profile_detail_step is not None:
            return self.profile_detail_step
        return 0

    # ========== 投影分段估算（/context 诊断用，只读） ==========
    def _save_proj_stats_sidecar(self, stats: dict) -> None:
        """投影分段统计旁车持久化（用户提案 2026-08-29）：session_dir/proj_stats.json
        覆盖写最新一份，含档位边界快照（tier_boundaries/fold_count/max_level）——
        /context 重启后也能读"上次真实投影"的 live 口径（内存 _proj_stats 随进程消失）。
        原子写；session_dir 未就绪/失败静默（旁车只是诊断增强，绝不影响投影）。"""
        sdir = getattr(self, "session_dir", None)
        if sdir is None or not stats:
            return
        try:
            data = dict(stats)
            data["tier_boundaries"] = list(self._tier_boundaries)
            data["fold_count"] = self._last_fold_count
            data["max_level"] = self.max_level
            if self._sos_count and self._sos_text:      # sos 档（用户提案 2026-10-02）：LLM 浓缩摘要
                data["sos_count"] = self._sos_count
                data["sos_summary"] = self._sos_text
            data["chars_per_token"] = self._chars_per_token
            # 窗口快照（诊断盲点补齐）：触发毕业/折叠时的 win 与目标线直接可查——
            # "投影 200K 为何每轮毕业"这类问题不再需要从行为反推 live 窗口值
            # （profile 改配置后 reload/restart 前的旧值正是元凶，旁车留证据）。
            data["win"] = self.max_effective_context_window
            if self.max_effective_context_window:
                data["fold_ratio"] = self.fold_target_ratio or FOLD_TARGET_RATIO
                data["fold_target"] = self.fold_target()   # win×ratio（per-provider 缓存经济学参数）
                try:
                    import config as _cfg
                    data["panic"] = _cfg.load_panic_window()
                except Exception:
                    pass
            p = Path(sdir) / "proj_stats.json"
            tmp = p.with_suffix(p.suffix + ".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, p)
        except Exception:
            pass

    def _load_proj_stats_sidecar(self) -> Optional[dict]:
        """读旁车 proj_stats.json（读档后/重启后无内存缓存时的最近真实投影口径）。
        返回前改标 source=sidecar（/context 据此显示跨重启口径）。失败/不存在返回 None。"""
        sdir = getattr(self, "session_dir", None)
        if sdir is None:
            return None
        try:
            p = Path(sdir) / "proj_stats.json"
            if not p.exists():
                return None
            data = json.loads(p.read_text(encoding="utf-8"))
            if data and data.get("sections"):
                data = dict(data)
                data["source"] = "sidecar"
                return data
        except Exception:
            pass
        return None

    def _llm_calls_proj(self) -> Optional[dict]:
        """llm_calls.jsonl 尾扫：最后一条带 proj 的 react 记录（用户提案 2026-09-16）。
    优先级位于 sidecar 之后、现算之前——跨重启且比现算准（那是当时真实发给 LLM 的分布），
    但比 sidecar 略旧（sidecar 每次投影都写，含未成功调用的投影）。
    proj 记录侧由 agent._install_recorder 的 wrapper 附加（d425358 修 set_session 覆盖）。"""
        try:
            sdir = getattr(self, "session_dir", None)
            if not sdir:
                return None
            p = Path(sdir) / "llm_calls.jsonl"
            if not p.exists():
                return None
            with open(p, "rb") as f:
                f.seek(0, 2)
                size = f.tell()
                chunk = min(size, 96 * 1024)   # 尾部 96KB 足够覆盖最近若干条
                f.seek(size - chunk)
                data = f.read().decode("utf-8", "replace")
            for line in reversed(data.splitlines()):
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                if not r.get("proj"):
                    continue
                secs = [{"name": s.get("n") or "?", "tokens": int(s.get("tok") or 0),
                         "msgs": 0, "chars": 0,
                         "meta": f"{s.get('pct', 0)}%"} for s in r["proj"]]
                return {"sections": secs,
                        "total_tokens": sum(x["tokens"] for x in secs),
                        "total_chars": 0, "source": "llm_calls",
                        "ts": r.get("ts", 0), "turn": r.get("turn"), "step": r.get("step"),
                        "model": r.get("resp_model") or r.get("model") or "",
                        "note": "msgs/chars 不在此口径（采自调用记录 proj）"}
            return None
        except Exception:
            return None

    def projection_breakdown(self) -> dict:
        """分段统计四级读取（用户提案 2026-08-29 + 2026-09-16）：内存 live（真实装配时顺手记录的
        _proj_stats，含 ts/turn/step 元信息）→ 旁车 sidecar（session_dir/proj_stats.json，
        跨重启的最近真实投影+档位边界快照）→ llm_calls 尾扫（最后一条带 proj 的 react 记录，
        真实调用口径）→ 现算兑底（_walk_plan 同一走查重算）。
        返回 {sections: [{name, msgs, chars, tokens, meta}], total_tokens, total_chars, source?}。"""
        if self._proj_stats and self._proj_stats.get("sections"):
            return dict(self._proj_stats)   # 浅拷贝：调用方改动不污染缓存
        sc = self._load_proj_stats_sidecar()
        if sc:
            return sc
        lc = self._llm_calls_proj()
        if lc:
            return lc
        out = {"sections": [], "total_tokens": 0, "total_chars": 0}
        # 现算兜底与 messages_for_llm 同一走查（_walk_plan）——口径天然一致（含系统信息合并）。
        # 代价是会路过保命阀（_history_tiered_msgs 的估算循环，极端情况顺带升档/折叠），但走到
        # 这一步说明 live/旁车都没有——会话基本没投影过，实际触发不了。
        msgs: list[dict] = []
        try:
            self._walk_plan(msgs, out["sections"])
        except Exception as e:
            _LOG.warning("现算分段统计失败（用已算出的部分）：%s", e)
        finally:
            self._hist_marks = None
        for s in out["sections"]:
            out["total_tokens"] += s["tokens"]
            out["total_chars"] += s["chars"]
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

    def record_snapshot(self, sha: str, git_head: str = ""):
        """记录工作区快照 sha（+ 用户真仓库 HEAD，rewind 撞车检测用）到当前 turn。agent 打快照后调用。"""
        if self._current is not None:
            self._current.snapshot_sha = sha
            self._current.git_head = git_head or ""
            self._emit_event({"event": "snapshot", "sha": sha, "git_head": git_head or ""})

    def git_head_at_snapshot(self, sha: str) -> str:
        """返回 snapshot_sha==sha 那轮记录的用户真仓库 HEAD（无记录/找不到返回空串）。"""
        for t in self.turns:
            if t.snapshot_sha == sha:
                return getattr(t, "git_head", "") or ""
        return ""

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
        self._constr_buf = []   # 施工投影缓冲：新轮清空（turn 级 append-only）
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
                          "call_ids": [tc.call_id for tc in step.tool_calls],
                           "changes": [[tc.call_id, tc.changed] for tc in step.tool_calls if tc.changed]})

    def finish_turn(self, answer: str, answer_reasoning: str = ""):
        if self._current is None:
            return
        self._pinned_ctx = None   # context_messages 用完即焚（本轮投影期间已展开；复用实例下一轮不带）
        self._current.answer = answer
        self._current.answer_reasoning = answer_reasoning
        # 本轮文件变更清单（各步快照 diff 按文件去重，后写覆盖——webui answer 下补充渲染用：
        # LLM 未以 [!名](路径) 交代的文件，前端在 answer 尾部补资产框，2026-09-04 用户提案）
        _chg: dict = {}
        for _s in self._current.steps:
            for _tc in _s.tool_calls:
                for _it in (_tc.changed or []):
                    try:
                        _chg[str(_it.get("file"))] = _it.get("change") or "modified"
                    except Exception:
                        pass
        self._current.changed = [{"file": f, "change": c} for f, c in _chg.items()]
        # 生成该轮 summary（贴在该轮最后：作语义索引 + 窗口外摘要源 + 召回匹配文本）
        try:
            self._current.summary = self._summarize_turn(self._current)
        except Exception:
            self._current.summary = ""
        self.turns.append(self._current)
        finished = self._current
        self._constr_migrate_turn(finished)   # 跨轮施工流：施工中 → 本轮 [user + steps 定型] 入流
        self._current = None
        self._constr_buf = []   # 施工投影缓冲：归档即弃（运行时内存，不滞留）
        self._ensure_name()            # name 就绪 → 绑定 events/toollog 路径并 flush 缓冲
        self._emit_event({"event": "turn_end", "answer": finished.answer,
                          "answer_reasoning": finished.answer_reasoning,
                          "summary": finished.summary,
                          "changed": finished.changed})
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
        self._constr_migrate_turn(finished)   # 中断归档同口径：施工中同样入流
        self._current = None
        self._constr_buf = []   # 施工投影缓冲：中断归档同样清（与 finish_turn 同口径）
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
                # 截断后增量过滤（回滚 2026-09-30：保结构——recalc 会丢末端密集边界引发折叠螺旋）
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
                events.append({"event": "snapshot", "sha": t.snapshot_sha,
                               "git_head": getattr(t, "git_head", "") or ""})
            for s in t.steps:
                events.append({"event": "step", "reasoning": s.reasoning or "",
                               "call_ids": [tc.call_id for tc in s.tool_calls],
                               "changes": [[tc.call_id, tc.changed] for tc in s.tool_calls if tc.changed]})
            events.append({"event": "turn_end", "answer": t.answer or "",
                           "answer_reasoning": t.answer_reasoning or "", "summary": t.summary or "",
                           "changed": t.changed or []})   # 快照 diff 聚合（读档重放恢复 turn.changed）
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
        """原子写 jsonl：先 .tmp 再 os.replace，防并发/崩溃读到半个文件。
        落盘容错（night_tasks #1 2026-09-02）：写失败（磁盘满/文件被杀软或 OneDrive 锁定）
        只告警不抛——toollog 内存仍在（view 可查），绝不阻塞 react 主循环。"""
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(path.suffix + ".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                for r in rows:
                    f.write(json.dumps(r, ensure_ascii=False) + "\n")
            os.replace(tmp, path)
        except Exception as e:
            _LOG.warning("toollog 落盘失败（内存继续，不阻塞）：%s %s", path.name, e)

    # ========== 融合上下文（关键）==========
    # ========== assembly DSL v2：清单驱动投影 ==========
    # 默认装配清单（无声明 = 主 Agent / 未写 assembly 的路径）——与历史版硬编码投影顺序一致：
    # system(人设) → rules(AGENTS/规则/技能，每轮重读) → history(滑动窗口+摘要 或 分档毕业)
    # → ltm(长期记忆静态层) → user_message(当前轮) → steps(当前轮步骤) → tail.*(易变环境块拆段)。
    # ltm 统一放在 history 之后：它偶发变化（agent 写记忆），越靠后变化时的缓存爆炸半径越小。
    # tail 拆平铺段（2026-09-01·用户提案：写死内容组装化）：time/system/plan/spec/episodic/remote
    # 各自可配顺序/开关/增删；旧 tail 段在 set_assembly_plan 自动展开（yml 兼容）。
    # recent_file 段（2026-09-07·用户提案：快照不再内嵌 tool result，独立段走装配）：steps 后、tail 前。
    _DEFAULT_ASSEMBLY_PLAN = [
        {"kind": "seg", "name": "system"},
        {"kind": "seg", "name": "rules"},
        {"kind": "seg", "name": "history"},
        {"kind": "seg", "name": "ltm"},
        {"kind": "seg", "name": "user_message"},
        {"kind": "seg", "name": "steps"},
        {"kind": "seg", "name": "recent_file"},
        {"kind": "seg", "name": "tail"},
    ]

    def set_assembly_plan(self, plan: Optional[list]):
        """设置 assembly 清单（multiagent 解析 .md frontmatter / agent_prompt 参数覆盖后调用）。
        None = 恢复默认清单。同时派生旧版开关 dict（assembly.get("hooks") 等消费点不变）：
        可关段在清单中出现 → True，未出现 → False（白名单语义）；hooks 不占投影位置，仅开关。
        旧 tail 段自动展开成六个 tail.* 子段（2026-09-01·拆段兼容——yml 不改不断）。"""
        if plan is not None:
            plan = self._expand_tail(plan)
        self.assembly_plan = plan
        self._assembly_once_cache = {}
        if plan is None:
            self.assembly = {}
            return
        segs = {it.get("name") for it in plan if it.get("kind") == "seg"}
        # tail 开关：任一 tail.* 子段在清单 → True（拆段后语义：tail 家族有内容才装配）
        self.assembly = {t: (t in segs) for t in ("rules", "history", "ltm")}
        self.assembly["tail"] = any(str(s).startswith("tail.") for s in segs)
        hist = next((it for it in plan if it.get("kind") == "seg" and it.get("name") == "history"), None)
        if hist and hist.get("mode"):
            self.assembly["history_mode"] = hist["mode"]

    # tail 拆段默认展开序（旧 tail 段 → 六个子段，与 _DEFAULT_ASSEMBLY_PLAN 的尾部一致）
    _TAIL_EXPAND = ("tail.time", "tail.system", "tail.plan", "tail.spec", "tail.episodic", "tail.remote")

    @classmethod
    def _expand_tail(cls, plan: list) -> list:
        """（2026-09-02 撤拆段——用户简化：动态内容用 func: 项放清单尾部，无需 tail.* 段名）
        保留方法以兼容调用点，恒等直通。"""
        return plan

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
        settle = self.fold_target()   # per-provider ratio（DeepSeek≈60x 折扣悬殊→配高如 0.95：晚折叠保前缀）
        fold_count = self._planned_fold
        # 估算辅助：history 之后的段（ltm/当前轮/tail），循环外算一次（tail 含 episodic 召回，避免循环内反复 embed）。
        # recent_file 段不计入（2026-09-07·段式化后语义不变）：rf 是轮内易变项（归档即消失），
        # 不该推动升档/折叠等不可逆历史压缩（用户裁定 2026-08-29）——_rf_stripped 兜底旧内嵌形态。
        rest = self._rf_stripped(self._seg_msgs_ltm() + self._seg_msgs_user_message()
                                 + self._seg_msgs_steps() + self._seg_msgs_tail())
        _est = lambda k: self._estimate_tokens(prefix_msgs + self._render_tiered_history(k) + rest)
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
            if self._graduate_once():                       # ① 先升档（无损：只降文字档上限）止血
                continue
            if self._deepen_oldest_tier(est_fn=_est, target=settle, fold_count=fold_count):  # ② 再推老档进工具折叠档
                continue
            # 应急首刀同款大刀（超深一半）；起点之后的微调碎刀
            nxt = (self._fold_leap_target(fold_count, _est, settle) if fold_count == self._planned_fold
                   else self._next_fold_target(fold_count, est_fn=_est, target=settle))
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
        """长期记忆·静态层（semantic 事实 + procedural 标题清单）：常驻背景知识块
        （裸内容——系统信息由 _walk_plan 决定合并，不在这里包标签）。"""
        if self._ltm_static_provider:
            try:
                block = self._ltm_static_provider()
                if block:
                    return [{"role": "system", "content": block}]
            except Exception:
                pass
        return []

    def _seg_msgs_user_message(self) -> list[dict]:
        """当前进行中轮的 user 消息（before_turn 钩子提示 merge 到 content 末尾）。"""
        if self._current is None:
            return []
        _c = self._user_content(self._current)
        _bt = getattr(self._current, "_before_turn_hint", None)
        if _bt:
            # 钩子注入 merge 化（2026-09-01·三区重构）：before_turn hint 不再独立成条——
            # merge 到 user 消息 content 末尾（触发位置的上一条 = 当前轮 user）；跨轮变化
            # 只影响本条（本来就在未命中区），消息形状由对话本体决定
            if isinstance(_c, str):
                _c = _c + "\n" + _bt
            elif isinstance(_c, list):
                _c = list(_c) + [{"type": "text", "text": "\n" + _bt}]
            else:
                _c = _bt
        return [{"role": "user", "content": _c}]

    def _constr_step_msgs(self, step: Step) -> list[dict]:
        """单个 step 的 full 形态定型渲染（施工投影缓冲 _constr_buf 的 append 单元）：
        preceding_hint + assistant(tool_calls 全量 args + reasoning) + 各 tool result
        （_cap_full_result 截断 + _project_imgs + 施工内嵌 <recent-file> 快照块）。
        与 _steps_to_messages 的 full 分支（base=None/组差0）同口径；行号宽度、version、
        快照文本在此一次定型——append 后字节冻结，后续文件变化/步数增长均不影响已定型的块。"""
        msgs: list[dict] = []
        if step.preceding_hint:
            # 插话原生看图（2026-10-03）：hint 内 <img> 标签按 vision 门控展开（无标签时原样 str，byte-stable 不破坏）
            msgs.append({"role": "user", "content": self._project_imgs(_MIDTURN_TAG + step.preceding_hint)})
        if not step.tool_calls:
            return msgs
        # 失联调用不进投影（用户裁定 2026-10-09）：toollog 无记录的 call_id 成对剔除
        # （tool_calls + 对应 tool result），整步失联则只留 preceding_hint——
        # 避免 LLM 看到 name="(详情已失效)" 的空调用对更蒙。
        live_tcs = [tc for tc in step.tool_calls if self.toollog.get(tc.call_id)]
        if not live_tcs:
            return msgs
        a_tool_calls = []
        for i, tc in enumerate(live_tcs):
            name, args, _r = self.toollog.view(tc.call_id)
            a_tool_calls.append({
                "id": tc.call_id or str(i), "type": "function",
                "function": {"name": name,
                             "arguments": json.dumps(args, ensure_ascii=False)},
            })
        a_msg = {"role": "assistant", "content": None, "tool_calls": a_tool_calls}
        if step.reasoning:
            a_msg["reasoning_content"] = step.reasoning   # 思考原样，不压缩（与 full 分支一致）
        msgs.append(a_msg)
        for i, tc in enumerate(live_tcs):
            _n, _a, result = self.toollog.view(tc.call_id)
            content = self._cap_full_result(result, tc.call_id)
            content = self._project_imgs(content)
            snap = (step.file_snapshots or {}).get(tc.call_id)
            if isinstance(snap, dict) and snap.get("path"):
                content += self._rf_inline_block(snap)
            msgs.append({"role": "tool", "tool_call_id": tc.call_id or str(i), "content": content})
        return msgs

    def _constr_sync(self):
        """施工投影缓冲对齐当前轮 steps（append-only 回填）。统一覆盖三种场景：施工激活前的
        explore steps（首次投影回填）、后续新 step、重启/resume 后的旧 steps——同一路径同一
        形态（full），字节稳定。steps 归档不回退，缓冲只增不减；防御性截断理论不触发。"""
        if self._current is None:
            return
        if len(self._constr_buf) > len(self._current.steps):   # 防御：steps 不回退，理论不达
            self._constr_buf = self._constr_buf[:len(self._current.steps)]
        while len(self._constr_buf) < len(self._current.steps):
            self._constr_buf.append(
                self._constr_step_msgs(self._current.steps[len(self._constr_buf)]))

    def _constr_migrate_turn(self, turn: "Turn") -> None:
        """跨轮施工流迁移（2026-09-17·用户裁定：从施工开始的轮全部保留）：本轮归档时若 plan 仍在
        施工中 → 把 [本轮 user + 各 step 定型 msgs] 追加进 _constr_stream（投影在 user_message 段
        位置整体输出——连续聊天记录，append-only 跨轮）。plan 全完成（收工）/无 plan → 清空流
        （恢复常规历史装配，形态跳变一次）。同步记 extra_state['constr_start_idx']（起始轮号）——
        重启后按 turns 惰性重建。中断归档同口径（abort_current_turn 也调本方法）。"""
        try:
            if not self._construction_mode():
                if self._constr_stream:
                    self._constr_stream = []                     # 收工/非施工：清流
                self.extra_state.pop("constr_start_idx", None)
                return
            self._constr_sync()   # 保证 buf 覆盖本轮全部 steps（定型器与投影同路径）
            self._constr_stream.append({"role": "user", "content": turn.user_message or ""})
            for _grp in self._constr_buf:
                self._constr_stream.extend(_grp)
            if self.extra_state.get("constr_start_idx") is None:
                self.extra_state["constr_start_idx"] = len(self.turns) - 1   # 起始轮号
            self._constr_buf = []
        except Exception as e:
            _LOG.warning("施工流迁移失败（跳过）：%s", e)

    def _constr_rebuild(self) -> None:
        """重启后施工流惰性重建（同裁定）：_constr_stream 为内存，重启丢失——施工中且
        extra_state 有起始轮号时，用 turns[start:] 重定型每轮 [user + steps]（与 _constr_step_msgs
        同定型器；归档轮 file_snapshots 不持久化→无内嵌快照块，形态从重建时刻重新 byte-stable）。"""
        if self._constr_stream:
            return          # 已有流（轮内由 _constr_migrate_turn 逐轮累积）
        try:
            _st = self.extra_state.get("constr_start_idx")
            if not isinstance(_st, int) or not self._construction_mode():
                return
            # 只重建【已归档】轮（turns[_st:]）；当前进行中的轮不进流（其 user/steps 走常规段）
            for _t in self.turns[_st:]:
                self._constr_stream.append({"role": "user", "content": _t.user_message or ""})
                for _s in _t.steps:
                    self._constr_stream.extend(self._constr_step_msgs(_s))
            self._constr_buf = []
        except Exception as e:
            _LOG.warning("施工流重建失败（跳过）：%s", e)

    def _seg_msgs_steps(self) -> list[dict]:
        """当前轮已完成的步骤 + 本步 pending 的用户中途补充（带标签，发出后滚入历史中部）。
        （2026-09-07 起 recent-file 不再内嵌 tool result 尾部——独立段 _seg_msgs_recent_file，
        走装配清单可配位置/开关。）
        施工模式（2026-09-15·用户提案）：走 turn 级 append-only 缓冲 _constr_buf——每 step
        定型一次（full 形态 + 内嵌快照渲染），投影只拼不改 → 前缀字节稳定（治 t861_s38-s76
        命中率上蹿下跳：原 _steps_to_messages 每次全量重渲染，前序 tool result 尾部的内嵌块
        随后续写操作漂移断缓存）。max_steps 截断在施工分支不生效（当前轮语义；超长轮由
        panic 阀兜底）。施工完成（plan 全 completed）切回原路，形态跳变一次可接受。"""
        if self._current is None:
            return []
        if self._construction_mode():
            self._constr_sync()
            out = [m for grp in self._constr_buf for m in grp]
        else:
            out = list(self._steps_to_messages(self._current.steps, self.max_steps_per_turn,
                                               full_window=RECENT_FULL_STEPS))
        _psh = getattr(self._current, "_pending_step_hint", None)
        if _psh:
            out.append({"role": "user", "content": self._project_imgs(_MIDTURN_TAG + _psh)})
        return out

    def _seg_msgs_recent_file(self) -> list[dict]:
        """recent-file 段（2026-09-07·用户提案·第四版）：当前轮改过的文件快照以独立段投影
        （2026-08-29 第三版是内嵌在那次工具调用 result 尾部）。结构（用户给定）：
            <recent-file>
            <file path="xxx.py" version="a1b2">        小文件：行号化全文
            1| import os
            </file>
            <file path="big.md" version="c3d4" size="130537">   大文件（非施工 >RF_SEG_MAX_CHARS=15K）：
            <overview>结构大纲</overview>              py=函数/类行号结构 / md=标题大纲（_rf_outline）
            <content note="文件过大省略——需要时 read_file 分段读取"/>
            </file>
            </recent-file>
        数据源 _rf_latest_map：同文件多次 edit 只有最新快照命中；归档轮天然不在映射（前面的轮不管）。
        空映射 → 空段（零噪声）。version=快照记录的 file_version（乐观锁版本）。
        施工模式（2026-09-13·用户裁定）返回空：施工期快照回内嵌形态——贴在该次写调用的
        tool result 尾部（当时的版本、不去重、不限数量），独立段不投影防双份（见
        _steps_to_messages / _rf_inline_block）。"""
        if self._construction_mode():
            return []
        m = self._rf_latest_map()
        if not m:
            return []
        parts = []
        for info in m.values():
            if info.get("skip"):
                ov = str(info.get("outline") or "").strip() or "(结构提取失败)"
                parts.append(
                    f'<file path="{info["path"]}" version="{info["version"]}" size="{info["skip"]}">\n'
                    f"<overview>\n{ov}\n</overview>\n"
                    f'<content note="文件过大（{info["skip"]:,} 字符 > {RF_SEG_MAX_CHARS:,}）——此处省略，'
                    f'需要时 read_file 分段读取"/>\n</file>')
            else:
                _lines = str(info["text"]).split("\n")
                _w = max(2, len(str(len(_lines))))          # 行号宽度自适应（与 read_file 口径一致）
                numbered = "\n".join(f"{i:>{_w}}| {ln}" for i, ln in enumerate(_lines, 1))
                parts.append(f'<file path="{info["path"]}" version="{info["version"]}">\n{numbered}\n</file>')
        block = "<recent-file>\n" + "\n".join(parts) + "\n</recent-file>"
        return [{"role": "user", "content": block}]

    def _tail_block_msgs(self, name: str) -> list[dict]:
        """tail.* 拆段的单段块收集（2026-09-01·用户提案：写死内容组装化）：按子段名取各自
        provider 的块 → _ambient_group 分组渲染。返回 [] 则该段不出现（零噪声）。
        子段：time/system/plan/spec/episodic/remote（remote 由 chat.py 挂 _remote_provider）。"""
        blocks = []
        sub = str(name).split(".", 1)[1] if "." in str(name) else str(name)
        if sub == "time":
            self._collect_ambient(blocks, self._time_provider)
        elif sub == "system":
            self._collect_ambient(blocks, self._system_extra_provider)
        elif sub == "plan":
            self._collect_ambient(blocks, self._plan_provider)
        elif sub == "spec":
            self._collect_ambient(blocks, self._spec_provider)
        elif sub == "episodic":
            # 情境层（episodic）按问题召回
            if self._ltm_episodic_provider and self._current is not None and self._current.user_message:
                try:
                    block = self._ltm_episodic_provider(self._current.user_message)
                    if block and block.strip():
                        blocks.append(block.strip())
                except Exception:
                    pass
        elif sub == "remote":
            # 远程实例清单（2026-09-01 从 SYSTEM 头部迁移到 tail——连接变化零缓存代价）
            self._collect_ambient(blocks, getattr(self, "_remote_provider", None))
        # 区3 统一包裹（三区重构）：裸块拼接（空行连接）——<system-reminder> 由 _walk_plan
        # 尾部 merge 段统一包一层（各段不各自包裹，避免多层标签）
        grouped = "\n\n".join(b for b in blocks if b and b.strip())
        return [{"role": "user", "content": grouped}] if grouped else []

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
        # role=user（对齐 Claude Code 线上协议的动态注入形态）：DeepSeek v4 端点对 messages 里的
        # system 消息做规范化（重排/合并进缓存键），tail 每步重渲染变化 → system role 会让整个
        # 序列的缓存从头部就断（实测 6%/恒定14k 残段命中；改 user 后 99%，探针 R12b 2026-08-29）
        return [{"role": "user", "content": grouped_tail}] if grouped_tail else []

    def _asm_action_msgs(self, item: dict) -> list[dict]:
        """assembly 清单里的动作项（file/dir/cmd/workflow/text）→ 一条 system 消息（裸内容——
        不加 [assembly:] 前缀、不包 <system-reminder>；与相邻系统信息段的合并由 _walk_plan 决定）。
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
        return [{"role": "system", "content": txt}]

    def _asm_evaluate(self, item: dict) -> str:
        """动作项求值（workspace 沙箱 / 超时 / 失败跳过）。"""
        kind = item.get("kind")
        try:
            if kind == "text":
                return _interp_funcs(str(item.get("text") or ""))
            if kind == "func":
                from agent_config import resolve_assembly_func
                # viewer_id：func 求值所属的 agent（session._asm_agent_id）——get_team_profiles 等
                # 需要区分「谁在看」的函数用它 exclude 自己（子 Agent 装配看板时不再 exclude 主 Agent）
                return resolve_assembly_func(str(item.get("func") or ""),
                                             viewer_id=str(getattr(self, "_asm_agent_id", "") or ""))
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
            if kind == "image_feed":
                # 实时画面装配段（2026-10-06·用户提案）：每步从画面服务取最新帧 → base64 挂投影末尾。
                # src 支持 http(s)://（内存流，零落盘——推荐）与文件路径（workspace 相对）。
                # vision 门控：非视觉模型整段静默跳过；失联/空帧降级一行文字（不炸轮）。
                # 哨兵返回：@@IMGFEED@@data:<mime>;base64,<b64>@@——桶收集处抽进图片通道，不进文本桶。
                _src = str(item.get("image_feed") or item.get("src") or "").strip()
                if not _src:
                    return ""
                if not getattr(getattr(self, "llm", None), "vision_supported", False):
                    return ""   # 非视觉模型：静默跳过（不注入 base64，省 token + 防 400）
                import urllib.request as _ur, base64 as _b64
                try:
                    if _src.lower().startswith(("http://", "https://")):
                        with _ur.urlopen(_src, timeout=3) as _r:
                            data = _r.read(6_000_000)
                            mime = (_r.headers.get("Content-Type") or "image/jpeg").split(";")[0].strip()
                    else:
                        _p = Path(_src)
                        if not _p.is_absolute():
                            _p = Path(self.workspace) / _p
                        data = _p.read_bytes()
                        mime = mimetypes.guess_type(str(_p))[0] or "image/jpeg"
                except Exception as _e:
                    return f"[image_feed 不可用：{type(_e).__name__}——画面服务未启动或不可达（{_src}）]"
                if not data:
                    return f"[image_feed 空帧：{_src}]"
                return f"@@IMGFEED@@data:{mime};base64,{_b64.b64encode(data).decode()}@@"
            if kind == "workflow":
                return _eval_assembly_workflow(str(item.get("name") or ""), self)
            if kind == "tool":
                return _eval_assembly_tool(item, self)
            return ""
        except Exception as e:
            _LOG.warning("assembly %s 项求值失败（%s），跳过：%s", kind, item.get("name") or item.get("path") or item.get("cmd"), e)
            return ""

    def _construction_mode(self) -> bool:
        """施工模式（2026-09-13·spec s_e1804804，用户提案）：活动 plan 存在未完成步时投影切换
        施工视图——history 不装配 + 头部施工牌（plan design）。判定源 agent_config._RUNTIME_AGENT
        （与 plan_content() 同源，无第二份状态）；绑定 session 同一性——子 Agent 的 session 不是
        活动 plan 所属 agent 的 session，不受主 Agent 施工影响（其装配的 history 段照常）。"""
        try:
            from agent_config import _RUNTIME_AGENT
            if _RUNTIME_AGENT is None or getattr(_RUNTIME_AGENT, "session", None) is not self:
                return False
            p = getattr(_RUNTIME_AGENT, "active_plan", None)
            steps = (p or {}).get("steps") or []
            return bool(steps) and any(str(x.get("status")) != "completed" for x in steps)
        except Exception:
            return False

    def messages_for_llm(self) -> list[dict]:
        """投影 = assembly 清单驱动：按 self.assembly_plan 的顺序装配段/动作项（走查看 _walk_plan——
        连续系统信息段合并成一条 system）；无声明走默认清单（与历史版硬编码顺序一致）。
        current_turn_only（子 Agent reuse）：history/ltm 段强制跳过——每次任务上下文干净、
        token 不随复用次数增长；历史轮仍完整归档（agent_query_events / recall 可查）。
        装配时顺手记录分段统计到 _proj_stats（/context 三级读取的 live 源——真实投影口径，
        比事后重算的 projection_breakdown 更可信；后者退化为无旁车时的兜底）。"""
        msgs: list[dict] = []
        try:
            sections: list[dict] = []
            self._walk_plan(msgs, sections)
            # 2026-09-04·回答风格提示（写死代码，用户提案）：装配后第一条 system 消息（人设）尾部
            # 追加 Markdown 回答规范与资产引用语法告知——Agent 不知道 webui 支持 [!名](路径) 渲染。
            # 幂等：已含标记则跳过（防重复装配叠加）；恒定文本不引入额外缓存扰动。
            self._append_answer_style(msgs)
            # —— 施工牌（2026-09-13·spec s_e1804804·用户提案）：施工模式第二条 system = plan design
            # 全文。施工期恒定 → 前缀 byte-stable（缓存吃满）；不进 _apply_system_ledger 的
            # 快照口径（账本只认 msgs[0]）。位置=头部 system 之后（user/rules 的合并 system 是 msgs[0]）。
            if self._construction_mode():
                try:
                    from agent_config import _RUNTIME_AGENT
                    _p = getattr(_RUNTIME_AGENT, "active_plan", None) or {}
                    _d = str(_p.get("design") or "").strip()
                    if _d:
                        _t = str(_p.get("title") or "")
                        _h = f"【施工牌·进行中计划】{_p.get('id', '')}" + (f" · {_t}" if _t else "")
                        msgs.insert(1, {"role": "system", "content":
                                        f"{_h}\n设计：\n{_d}\n\n"
                                        "（施工模式：历史对话未装配——背景以本设计为准；"
                                        "需要历史细节用 recall 召回。）"})
                        sections.insert(1, {"name": "施工牌(plan design)", "msgs": 1,
                                            "chars": len(_d) + 80,
                                            "tokens": int((len(_d) + 80) / self._chars_per_token),
                                            "meta": "施工模式·恒定前缀（byte-stable）",
                                            "sample": _h})
                except Exception as e:
                    _LOG.warning("施工牌注入失败（跳过）：%s", e)
                # —— 上一轮施工摘要（2026-09-16·用户裁定）：跨轮接力细节。牌保持恒定不动
                #（byte-stable 缓存吃满）；摘要作为独立小段紧随牌后——每轮只换这一条（≤60 字，
                # recap 数据），缓存断点=摘要消息开头，其后 user/steps 本就是新内容，牺牲极小。
                try:
                    _prev = self.turns[-1] if self.turns else None
                    _rc = (_prev.recap or "").strip() if _prev is not None else ""
                    if _rc:
                        msgs.insert(2, {"role": "system", "content":
                                        f"【上一轮施工摘要】{_rc}\n（详细过程已归档，需要用 recall 召回。）"})
                        sections.insert(2, {"name": "施工摘要(prev recap)", "msgs": 1,
                                            "chars": len(_rc) + 40,
                                            "tokens": int((len(_rc) + 40) / self._chars_per_token),
                                            "meta": "施工模式·每轮一条（recap 接力）",
                                            "sample": f"【上一轮施工摘要】{_rc[:36]}"})
                except Exception as e:
                    _LOG.warning("施工摘要注入失败（跳过）：%s", e)
            self._apply_system_ledger(msgs)   # 三态后处理（必须在 answer_style 之后——比较口径含提示文本）
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
                                "system_form": self._ledger_form or "-",  # system 段形态（byte-stable/appended vN/normalized——append-not-replace 观测）
                                "source": "live"}
            self._save_proj_stats_sidecar(self._proj_stats)   # 旁车持久化（含档位边界快照——跨重启可读）
        except Exception as e:
            _LOG.warning("投影装配异常（保底返回已装配部分）：%s", e)
        finally:
            self._hist_marks = None
        return msgs
    # —— 回答风格（2026-09-04·用户提案，写死代码）：webui 支持 Markdown 资产引用渲染，
    # Agent 默认不知道；装配后追加到第一条 system 消息（人设）末尾统一告知。
    # 本项目回答风格约定：首行一句话总结 + --- 分隔线 + Markdown 正文（recap_gen 工作流
    # 的 check_style 短路依赖此格式——模型不遵守则 recap 每次走 LLM，省不了推理）。
    # v2（09:40）：措辞从「尽可能（建议）」升级为「必须（强约束）」并给四段模板——
    # 实测「尽可能」级别模型不严格遵守，回答仍是老风格。
    _ANSWER_STYLE_HINT = (
        "\n\n【回答风格·必须遵守】每次完成本轮任务后，你的最终回答【必须】使用以下固定格式：\n"
        "  第一行：用一句话总结你做了什么（30 字以内，直接陈述，不加标题符号）\n"
        "  第二行：---\n"
        "  第三行起：详细的 Markdown 回答正文（表格/代码块/列表均可）\n"
        "  正文末尾再单独一行 ---\n"
        "  注: Markdown 中引用本地文件资产用 [名称](path/to/file)——渲染为【名称+资产控件】"
        "（图片图框/音频播放条/视频播放器/文本预览，名称显示）；"
        "要隐藏名称的纯资产用 ![alt](path)。或用 <https://xx.xx.xx> 的方式插入链接。\n"
        "若本轮确实执行了任务（而非无法完成），就必须以上述格式结尾——这是系统约定，不是建议。\n"
        "【卡点与等待行为】遇到搞不定/被阻塞的问题时：如实报告卡点是什么、已尝试了什么、"
        "下一步计划（或需要用户决策的选项）；禁止用「夜深了/您先休息/明天再搞」这类话术收尾"
        "——你是常驻 agent，不是值班客服（用户主动表示要休息除外）；能继续推进的事挂后台干"
        "（长任务转后台/定时续跑/异步派发，完成时自动汇报）——用户休息你在干活是常态；"
        "真正无事可做才收工，收工时留现状清单与次日待办。"
    )

    def _append_answer_style(self, msgs: list[dict]) -> None:
        """把回答风格提示拼到第一条 system 消息（人设）content 末尾。
        幂等：content 已含「回答风格提示」标记则跳过（防重复装配叠加）；
        浅拷贝重建该消息——绝不就地改共享引用（防污染 self.system 等持久数据）。"""
        for i, m in enumerate(msgs):
            if isinstance(m, dict) and m.get("role") == "system":
                c = str(m.get("content") or "")
                if "回答风格提示" in c:
                    return
                m2 = dict(m)
                m2["content"] = c + self._ANSWER_STYLE_HINT
                msgs[i] = m2
                return

    def mark_system_dirty(self, reason: str) -> None:
        """标记 system 账本需要归一化（断点事件：毕业/折叠执行、tools schema hash 变化）。
        这些事件本身就会断 provider 前缀缓存，归一化在断点处执行零额外成本——DSH「断点清账」。
        幂等：已 dirty 不重复记日志。"""
        L = self._system_ledger
        if not L.get("dirty"):
            L["dirty"] = True
            _LOG.info("system账本置dirty（下次投影归一化）：%s", reason)

    def _profile_fingerprint(self) -> str:
        """投影相关 profile 指纹（模型名|vision 位|窗口）：save 落盘、load 对比——
        变了才置 dirty 全量重刷（重启后首次请求按当前 profile 定型，用户提案
        2026-09-30）；一致则延续账本（同模型重启 byte-stable → 端点缓存命中）。"""
        llm = getattr(self, "llm", None)
        return "|".join(str(x) for x in (
            getattr(llm, "model_name", None),
            bool(getattr(llm, "vision_supported", False)),
            getattr(self, "max_effective_context_window", None)))

    def invalidate_projection(self, reason: str) -> None:
        """投影惰性状态全失效，下次 messages_for_llm 按新 profile 全量重定型
        （用户提案 2026-09-30：手动切模型时把投影按投影规则整个重刷一遍）。
        原理：投影真相源 = 内存 turns/steps（events.jsonl 的回放态），历史轮/冻结块
        的可变部分都在惰性缓存里——清空后下次投影自动按新模型能力全量重算（含
        vision 门控的图片投影、per-provider 窗口/衰减），等效 events 重放零 IO。
        时机：模型切换必然断 provider 前缀缓存 → 断点免费清账（与毕业/折叠同哲学）。
        覆盖：账本置 dirty / _frozen_renders（旧 vision 定型）/ _constr_buf+
        _constr_stream（施工定型重回填）/ _proj_stats（段统计新口径）。"""
        self.mark_system_dirty(f"投影全量重刷（{reason}）")
        try:
            self._frozen_renders.clear()
            self._constr_buf = []
            if self._constr_stream:
                self._constr_stream = []   # 施工中→下次投影 _constr_rebuild 按 turns[start:] 重建
            self._proj_stats = None
            _LOG.info("投影惰性状态已全量失效（%s）——下次投影按新 profile 重定型", reason)
        except Exception as e:
            _LOG.warning("invalidate_projection 部分失败（忽略）：%s", e)

    def apply_simple_tiering(self, near_turns: int = 10) -> int:
        """切模型/异 profile 重启时的简化分层一次定型（用户提案 2026-10-02）。

        全量精确收敛（_plan_fold/_history_tiered_msgs 的迭代 × 每步全量渲染）对新
        provider 接手性价比低——切模型/换端点必然断前缀缓存，旧形态连续性没有缓存
        价值。简化一次定型（O(1) 决策 + 极少渲染步）：
          ① 档1 = 近 near_turns 轮（全量披露窗口）；
          ② 更早轮每 GRADUATE_FORCE_BATCH(15) 轮一刀升档 → 最老段 raw>max_level
             自动落入工具折叠档（阶梯中间档自然形成，渲染器原生处理）；
          ③ 估算超预算（win×ratio）→ 复用应急收敛件：大刀(_fold_leap_target) 1 次 +
             微调(_next_fold_target) ≤6 步，把最老轮折叠进结构摘要；
          ④ 结果写入 _planned_fold/_last_fold_count——后续轮以它为起点零调整
             （_plan_fold 未顶窗路径），byte-stable 从新 provider 第一步重新积累。
        返回 fold_count。"""
        if not self.max_effective_context_window:
            return self._planned_fold
        n = len(self.turns)
        target = self.fold_target()
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
        # ① 边界一次铺好（ascending 多重集；new_b 语义与 _graduate_once 对齐）
        bs: list = []
        if n > near_turns:
            b = (n - near_turns) - 1
            while b >= 0:
                bs.append(b)
                b -= GRADUATE_FORCE_BATCH
            bs.reverse()
        self._tier_boundaries = bs
        self._frozen_renders.clear()   # 档位全变 → 冻结渲染必须重算
        # ② fc：预算判定 + 折半大刀收敛（用户提案 2026-10-02：每次把工具折叠档的
        #    【一半】折进结构摘要，达标即停；否则对剩余轮数再折半——步数 log2 级，
        #    单调无震荡，比碎刀微调少一个数量级的渲染次数）
        _est = lambda k: self._estimate_tokens(prefix + self._render_tiered_history(k))
        fc = 0
        cuts = 0
        hi = max(0, n - near_turns)          # 可折区间 [0, hi)：近窗永不折
        if _est(0) > target and hi > 0:
            while cuts < 12:
                half = max(1, (hi - fc) // 2)
                fc_try = fc + half
                cuts += 1
                if _est(fc_try) <= target:
                    fc = fc_try              # 这一半已达标——收刀（宁略多折，摘要+recall 兜底）
                    break
                fc = fc_try                  # 不达标：这半已进摘要，对剩余再折半
                if hi - fc < 1:
                    break                    # 全折仍不达标（极端）：兜底停
        # ②b sos（summary of summary，用户提案 2026-10-02）：折半到头（全折/刀数上限）
        #    仍超预算——真的压不动了。fc 清单前半（fc//2 轮）由 LLM 浓缩成一份叙事摘要
        #    替代（sos 档），清单只留次早期段。内容跨模型通用（切模型不重生成）；
        #    LLM 失败/仍超则降级接受（warning 记录，纯清单形态可用）。
        sos_done = 0
        if fc and _est(fc) > target:
            lo = int(getattr(self, "_sos_count", 0) or 0)   # 已有 sos 段续接（内容跨模型通用）
            for _ in range(3):   # sos 递进 ≤3 段（半→再半→再半），达标即停；输入恒为原始清单段
                end = min(fc, lo + max(1, (fc - lo) // 2))
                if end <= lo:
                    break
                _LOG.info("sos 浓缩第 %d~%d 轮清单（est=%d > 预算 %d）", lo + 1, end, _est(fc), target)
                part = self._generate_sos(lo, end)
                if not part:
                    break
                self._sos_text = (self._sos_text + "\n\n" + part).strip() if self._sos_text else part
                self._sos_count, lo, sos_done = end, end, sos_done + 1
                if _est(fc) <= target or end >= fc:
                    break
            if _est(fc) > target:
                _LOG.warning("sos 后仍超预算（est=%d > %d）——近窗全量披露天生占宽，接受或调窗",
                             _est(fc), target)
        # ③ 落位（与 _plan_fold 尾段同款：fc 之前的边界是死重，清掉）
        if fc <= int(getattr(self, "_sos_count", 0) or 0):   # fc 过小时 sos 不适用（rewind 等场景）
            self._sos_text, self._sos_count = "", 0
        if fc > 0:
            self._tier_boundaries = [b for b in self._tier_boundaries if b >= fc]
        self._planned_fold = fc
        self._planned_graduates = 0
        self._last_fold_count = fc
        self.mark_system_dirty(f"简化分层定型（fc={fc}，档位重排）")
        _LOG.info("简化分层定型：轮=%d 近窗=%d 边界=%d fc=%d est≈%d/预算%d（折半%d刀）",
                  n, near_turns, len(self._tier_boundaries), fc, _est(fc), target, cuts)
        return fc

    def _apply_system_ledger(self, msgs: list) -> None:
        """system 段 append-not-replace 后处理（spec s_eb14a8fd；2026-09-12 用户提案；形态 A 修正）。

        实测背书（api.deepseek.com·deepseek-flash）：尾部 append 一条 system 前缀 hit 94.3%，replace 则 0%。
        账本：last_text=头部快照字节（归一化时刷新）；appends=[{turn, text}] 按轮锚定的追加版本
        （≤4；多版本共存于历史原位——DSH system/message surface 节点同款）；dirty=断点标记。

        形态 A（用户两图对照裁定）：append 在轮 N 发生 → 轮 N 进行中插在当前轮 user 前；
        轮 N 归档后由 _render_tiered_history 固定插在轮 N 块前——位置永不漂移，前缀跨轮稳定。
        （首版"浮动插入"每轮跟着当前 user 走 → 每轮断一次缓存，已废弃。）

        决策：cur==上次形态版本 → 重放/原样（byte-stable，含"append 已固化进历史"的情形——
        历史渲染自动带了它，消息层不再插）；cur==last → 撤回末条 append；变化且支持且 !dirty
        且 len(appends)<4 → append（同轮变更原地替换末条，跨轮新追加）；否则归一化清账。"""
        self._ledger_form = ""
        if not msgs or msgs[0].get("role") != "system":
            return                                  # 无头部 system（异常形态）——不动账本按现状
        cur = str(msgs[0].get("content") or "")
        L = self._system_ledger
        appends = L.setdefault("appends", [])
        last = str(L.get("last_text") or "")
        cur_turn = len(self.turns) + 1              # 当前轮号（1-based，与 to_history 口径一致）
        pend = appends[-1] if appends else None
        pend_live = bool(pend) and pend.get("turn") == cur_turn   # 末条 append 在当前轮（未固化）

        def _insert_before_user(text: str) -> None:
            ins = len(msgs) - 1
            while ins > 0 and msgs[ins].get("role") != "user":
                ins -= 1                            # 从尾部找最后一条 user（当前轮提问）
            if ins <= 0:
                ins = len(msgs)                     # 无 user（纯 steps 轮）→ 退到末尾
            msgs.insert(ins, {"role": "system", "content": text})

        def _can_append() -> bool:
            return (self._in_history_system and not L.get("dirty")
                    and len(appends) < 4)

        if not last:
            L.update(last_text=cur, appends=[], dirty=False)
            self._ledger_form = "normalized"
            _LOG.info("system段 首建快照（%d 字）", len(cur))
            return
        if pend:
            if cur == str(pend.get("text")):
                # 末条 append 即当前版本：轮内（live）重插 user 前重放；已固化（历史渲染自动带）不重复插
                msgs[0] = {"role": "system", "content": last}
                if pend_live:
                    _insert_before_user(cur)
                self._ledger_form = f"appended v{len(appends)}"
                return
            if cur == last:                         # 回归快照版本 → 撤回末条 append
                appends.pop()
                self._ledger_form = "append撤回"
                _LOG.info("system段 回归快照版本：撤回末条 append（前缀至撤回点稳定）")
                return
        elif cur == last:
            self._ledger_form = "byte-stable"
            return
        # 变化版本：追加（头部快照不动）或归一化
        if _can_append():
            if pend_live:
                appends.pop()                       # 同轮内版本替换（v2→v3 原位，不堆积）
            appends.append({"turn": cur_turn, "text": cur})
            msgs[0] = {"role": "system", "content": last}
            _insert_before_user(cur)
            self._ledger_form = f"appended v{len(appends)}"
            _LOG.info("system段 append-not-replace：v%d @t%d（前缀保持，%d 字）",
                      len(appends), cur_turn, len(cur))
            return
        # 归一化（= system 归档点，本就断缓存）：长期记忆快照在此失效——add_memory 落盘的
        # 新内容到点才进投影（2026-09-17·用户提案：只落盘不即时投影，保 ltm 段前缀 byte-stable）
        self._ltm_refresh_epoch = getattr(self, "_ltm_refresh_epoch", 0) + 1
        L.update(last_text=cur, appends=[], dirty=False)
        # 摘除历史渲染循环已插入的本批 append（时序：_render_tiered_history 先读账本插入了，
        # 此处清账要同步摘掉——否则废弃版本残留一条在历史里，下次投影才消失）
        _dead = {str(a.get("text")) for a in appends}
        for i in range(len(msgs) - 1, 0, -1):
            if msgs[i].get("role") == "system" and str(msgs[i].get("content")) in _dead:
                msgs.pop(i)
        self._ledger_form = "normalized"
        _LOG.info("system段 归一化（dirty=%s appends=%d in_history=%s，%d 字）",
                  bool(L.get("dirty")), len(appends), self._in_history_system, len(cur))

    def _walk_plan(self, msgs: list, sections: list) -> None:
        """清单走查（messages_for_llm 装配主体 / projection_breakdown 现算兜底共用，填 msgs+sections）。
        系统信息合并（用户设计 2026-08-29）：history/user_message/steps 之外的一切（system/rules/
        ltm/tail 段 + asm 动作项）都是系统信息，连续出现合并成一条 system 消息，content 空行连接、
        块本身不加前缀不包标签——只有 tail 的动态块组保留 <system-reminder>（静态指令裸、动态注入
        才带标签，对齐 Claude Code 线上协议）。对话本体段装配前先冲刷系统信息缓冲，各段独立成消息。
        sections 为块级口径：每段按自身内容计 chars/tokens；合并 run 的非首段 msgs=0（内容并进首段
        那条 system，meta 注明），sum(sections.msgs)==len(msgs) 对得上。
        异常向上抛——msgs/sections 里留着已装配部分，调用方保底可用。"""
        plan = self.assembly_plan or self._DEFAULT_ASSEMBLY_PLAN
        self._hist_marks = []               # history 子段标记（_render_tiered_history/_history_window_msgs 填充）
        run: list[dict] = []                # 系统信息 run 缓冲（各段 0/1 条消息）
        run_secs: list[tuple[str, list, str]] = []   # run 内各段 (段名, own msgs, meta)
        tail_images: list = []              # 区3 图片桶（image_feed 哨兵抽取，2026-10-06·用户提案）：
                                            # merge 段组装成 image_url 块挂末条（瞬态：不落 events/存档）
        tail_merge_text: str = ""           # 区3 收集桶（tail.* + steps 后的 asm 项·默认/reminder posture）：
                                            # 装配后统一包裹并入末条 content
        tail_stat_text: str = ""            # 统计专用镜像桶（2026-09-16·用户抓到重复计算）：与
                                            # tail_merge_text 同步收集但【不含 recent_file】——rf 已单独
                                            # _sec 记账，而真实 merge 时它并入 tail 同一条消息，统计若
                                            # 用完整 text 会把 rf 的 tokens 算两次
        reasoning_merge_text: str = ""      # 区3 收集桶（steps 后声明 reasoning pose 的 asm 项）：
                                            # 装配后作为思考链注入末条 assistant reasoning_content 前缀
        tail_mode = "reminder"              # 尾段注入模式（steps=reasoning 声明改写；2026-09-03 粒度演进
                                            # 后为"未标 pose 项"的整体默认）：reminder=包裹并入末条 content；
                                            # reasoning=作为思考链注入末条 assistant 的 reasoning_content 前缀
                                            # （steps 段自身不再触发 reasoning——改由每 asm 动作项带 pose）
        user_end_idx: int = -1              # user_message 段装配完后的 msgs 边界（reasoning 注入的搜索起点——
                                            # 只认 user 之后的 assistant，s0 无 assistant 回退 reminder）
        passed_steps = False                # 三区状态（2026-09-01）：steps 段处理过后 → 区3（尾部 merge 语义）

        def _sec(name: str, own: list, meta: str = "", msgs_n: int = -1, note: str = "") -> None:
            if not own:
                return
            sections.append({"name": name, "msgs": len(own) if msgs_n < 0 else msgs_n,
                             "chars": self._count_chars(own),
                             # 段 tokens 用纯内容口径（include_schema=False）——schema 是请求级，
                             # 逐段含 schema 会重复计入（每段带一份底噪、段间之和≠合计）；schema 单列一段
                             "tokens": self._estimate_tokens(own, include_schema=False),
                             "meta": f"{meta}；{note}" if meta and note else (note or meta),
                             "sample": str(own[0].get("content") or "")[:120].replace("\n", " ")})
                             # 首条消息样本：段统计异常时（如 msgs=1 却巨大）直接看切片里是什么——
                             # live 异常无法跨进程静态复现，埋此诊断

        def _flush_run() -> None:
            if not run:
                return
            if len(run) > 1:   # 连续系统信息 → 一条 system（空行连接）
                msgs.append({"role": "system",
                             "content": "\n\n".join(str(m.get("content") or "") for m in run)})
            else:
                msgs.append(run[0])
            merged = len(run) > 1
            for i, (nm, own, meta) in enumerate(run_secs):
                if merged and i:
                    _sec(nm, own, meta, msgs_n=0, note="并入上方相邻段（同一条 system）")
                else:
                    _sec(nm, own, meta)
            run.clear()
            run_secs.clear()

        for item in plan:
            kind = item.get("kind")
            if kind == "seg":
                if item.get("opt"):
                    continue   # optional 段默认不装配（agent_prompt assembly="seg=on" 清标记后才会到这里）
                name = item.get("name")
                if self.current_turn_only and name in ("history", "ltm"):
                    continue   # reuse 投影隔离：历史系段一律不投影
                if name in ("system", "rules", "ltm", "tail", "recent_file"):
                    own = (self._seg_msgs_system() if name == "system" else
                           self._seg_msgs_rules() if name == "rules" else
                           self._seg_msgs_ltm() if name == "ltm" else
                           self._seg_msgs_tail() if name == "tail" else self._seg_msgs_recent_file())
                    if own:
                        if name == "tail":
                            # tail.* 拆段（2026-09-01·用户提案：写死内容组装化）：各子段独立收集，
                            # 依清单顺序合并进 tail_merge_text（装配后 merge 到末条 content——
                            # 见 _flush_run 后的 merge 段）；空段自动跳过（零噪声）。
                            # 剥自带 <system-reminder> 包裹（_ambient_group 会包一层，而 merge 段
                            # 统一再包——不剥则双层嵌套，2026-09-02 实锤于本 session 每步注入）
                            _b = "\n".join(str(m.get("content") or "") for m in own).strip()
                            if _b.startswith("<system-reminder>"):
                                _b = _b[len("<system-reminder>"):].strip()
                            if _b.endswith("</system-reminder>"):
                                _b = _b[:-len("</system-reminder>")].strip()
                            tail_merge_text = (tail_merge_text + "\n" + _b) if tail_merge_text else _b
                            tail_stat_text = (tail_stat_text + "\n" + _b) if tail_stat_text else _b
                            continue
                        if name == "recent_file":
                            # recent_file 段（2026-09-07·用户提案·段式化）：与 tail 同桶——
                            # <recent-file> 块并入 tail_merge_text（装配后统一 <system-reminder>
                            # 包裹 merge 到末条 content；末条在未命中区，每步变化零缓存扰动）。
                            _b = "\n".join(str(m.get("content") or "") for m in own).strip()
                            tail_merge_text = (tail_merge_text + "\n" + _b) if tail_merge_text else _b
                            _sec("recent_file(改文件快照段)", own, "并入末条(reminder)", msgs_n=0)
                            continue
                        run.extend(own)
                        run_secs.append((
                            {"system": "system(人设+环境)",
                             "rules": "rules(AGENTS+规则+技能)",
                             "ltm": "长期记忆·静态"}[name], own, ""))
                    continue
                _flush_run()   # 对话本体段：先把缓冲里的系统信息落成消息
                if name == "history":
                    if self._construction_mode():
                        # 施工模式（2026-09-13·spec s_e1804804）：历史不投影——背景以施工牌
                        # （头部第二条 system=design 全文）为准，需要细节 recall 召回
                        sections.append({"name": "history段", "msgs": 0, "chars": 0, "tokens": 0,
                                         "meta": "跳过(施工模式——plan 未完成，历史不装配；recall 可查)"})
                        continue
                    st = len(msgs)
                    msgs.extend(self._seg_msgs_history(item.get("mode"), msgs))
                    subs = [(nm, st + off, mt) for (nm, off, mt) in (self._hist_marks or [])]
                    if not subs:
                        subs = [("history段", st, "")]
                    for j, (nm, s2, mt) in enumerate(subs):
                        end2 = subs[j + 1][1] if j + 1 < len(subs) else len(msgs)
                        _sec(nm, msgs[s2:end2], mt)
                elif name == "user_message":
                    # context_messages 直通（agent_prompt 注入的一次性前置上下文）：
                    # 展开在 user 之前——还原的历史对话/工作记录；finish_turn 即焚（不落 turns）
                    _pinned = getattr(self, "_pinned_ctx", None)
                    if _pinned:
                        st = len(msgs)
                        msgs.extend({"role": str(m.get("role")), "content": m.get("content")}
                                    for m in _pinned if isinstance(m, dict))
                        _sec(f"前置上下文({len(_pinned)}条)", msgs[st:])
                    own = self._seg_msgs_user_message()
                    # 跨轮施工流（2026-09-17·用户裁定）：施工期在 user 前整体输出历史施工轮
                    # [u,s,u,s...]（append-only 连续记录——缓存与思维链跨轮连续）；惰性重建
                    # 兼顾重启（extra_state 起始轮号 → turns 重定型）
                    if self._construction_mode():
                        self._constr_rebuild()
                        if self._constr_stream:
                            _st2 = len(msgs)
                            msgs.extend(self._constr_stream)
                            _sec(f"施工历史流({self.extra_state.get('constr_start_idx', 0)}轮起·append-only)",
                                 msgs[_st2:])
                    msgs.extend(own)
                    _sec(f"当前轮user(第{len(self.turns)+1}轮)", own)
                    user_end_idx = len(msgs)   # reasoning 注入边界：此后出现的 assistant 才是注入候选
                elif name == "steps":
                    own = self._seg_msgs_steps()
                    msgs.extend(own)
                    _sec(f"当前轮steps({len(self._current.steps) if self._current else 0}步)", own)
                    passed_steps = True   # 三区状态翻转：之后的段/动作项进区3（尾部 merge）
                    _m = str(item.get("mode") or "").strip().lower()
                    # steps=reasoning 旧写法废弃（2026-09-13·用户实测抓到副作用）：607 轮 pose 粒度
                    # 下放到各 asm 动作项（mode: reasoning）后，段级整体默认已无存在价值——且它把
                    # 没有姿势选择器的内建段（tail.* 六子段 / recent_file / 钩子旁注，设计=并入末条
                    # content）也整体拖进思考链，glm 场景下 reminder/reasoning 两桶全进 reasoning_content
                    # （t781_s2 投影实证）。此后仅 asm 项自身的 mode 生效；旧声明读日志提示迁移。
                    if _m == "reasoning":
                        _LOG.warning("steps=reasoning 旧写法已废弃（pose 已下放到各 asm 项 mode 字段）——"
                                     "本声明被忽略；请把 yml 里 '- steps=reasoning' 改为 '- steps'")
                # 其它段名（hooks 已在 _normalize 移除；未知名静默跳过）
            else:
                own = self._asm_action_msgs(item)
                if own:
                    nm = f"asm:{item.get('kind')} {item.get('path') or item.get('name') or item.get('cmd') or ''}".strip()
                    meta = ("once" if item.get("timing") == "once" or
                            (item.get("kind") == "workflow" and not item.get("timing")) else "turn")
                    if passed_steps:
                        # 区3（2026-09-01·三区重构；2026-09-03 双桶演进）：steps 之后的 asm 动作项
                        # 不进 run 缓冲（不独立成条）。按每项 pose（item.mode）归桶：
                        # · reasoning → reasoning_merge_text（装配后注入末条 assistant 思考链）
                        # · 默认/reminder → tail_merge_text（装配后包裹并入末条 content）
                        _pose = str(item.get("mode") or "").strip().lower()
                        _b = "\n".join(str(m.get("content") or "") for m in own)
                        # image_feed 哨兵抽取（2026-10-06）：@@IMGFEED@@data:...@@ 不进文本桶——
                        # 抽进 tail_images（merge 段组装 image_url 挂末条；base64 不进统计文本桶）
                        _imgs = re.findall(r"@@IMGFEED@@(.+?)@@", _b)
                        if _imgs:
                            tail_images.extend(u for u in _imgs if u.startswith("data:"))
                            _b = re.sub(r"@@IMGFEED@@.+?@@", "", _b).strip()
                        if _pose == "reasoning":
                            reasoning_merge_text = (reasoning_merge_text + "\n\n" + _b) if reasoning_merge_text else _b
                            _sec(f"{nm}（尾部·思考链）", own, meta + "·注入思考链(reasoning)", msgs_n=0)
                        else:
                            tail_merge_text = (tail_merge_text + "\n\n" + _b) if tail_merge_text else _b
                            tail_stat_text = (tail_stat_text + "\n\n" + _b) if tail_stat_text else _b
                            _sec(f"{nm}（尾部）", own, meta + "·并入末条(reminder)", msgs_n=0)
                    else:
                        if any("@@IMGFEED@@" in str(m.get("content") or "") for m in own):
                            _LOG.warning("assembly image_feed 项应声明在 steps 段之后（区3 尾部）——图片不进 system run，已跳过")
                            continue
                        run.extend(own)
                        run_secs.append((nm, own, meta))
        # —— 区3 统一 merge（三区重构 2026-09-01，双桶演进 2026-09-03）——
        # steps 后的所有段（tail.* 拆段 + asm 动作项）按 pose 分两批注入：
        # · reminder（默认桶 tail_merge_text）：<system-reminder> 包裹 → 并入最后一条 message 的
        #   content 末尾（不额外创建 message）。缓存友好：末条本来就在未命中区，动态变化不再扰动
        #   已缓存前缀；也避开动态 user/system 消息被端点规范化（R12b 探针的坑）。
        # · reasoning（桶 reasoning_merge_text，2026-09-03 粒度演进——每 asm 动作项可独自选
        #   reasoning pose）：作为思考链一部分——user 之后最后一条 assistant 的 reasoning_content
        #   前缀（"当前状态：{result}\n"+原文）。语义：环境状态是思考的输入而非对话内容，不占
        #   content 位；末条 assistant 同样在未命中区，缓存代价相同。
        # 例外回退：末条是 assistant（罕见，如恢复场景）或 msgs 空 → 独立 user 消息（旧行为）；
        # reasoning 桶 user 后无 assistant（s0 首步）→ 回退 remainder 并入末条语义。
        def _apply_reasoning_bucket(text: str, _src_idx: int, _why: str, stat_text: str = None):
            _stat = text if stat_text is None else stat_text
            _src = msgs[_src_idx]
            _rc = str(_src.get("reasoning_content") or "")
            _inj = f"当前状态：{text}"
            _m2 = {**_src}   # 浅拷贝——末条可能是 session 数据的共享引用，绝不就地改（防污染持久数据）
            _m2["reasoning_content"] = (f"{_inj}\n{_rc}" if _rc else _inj)
            msgs[_src_idx] = _m2
            _sec("tail(易变环境块·思考链)", [{"role": "assistant", "content": f"当前状态：{_stat}"}],
                 f"reasoning前缀注入末条assistant(#{_src_idx})·{_why}", msgs_n=0)

        def _apply_reminder_bucket(text: str, stat_text: str = None) -> None:
            # stat_text（2026-09-16·用户抓到重复计算）：统计专用文本——不含 recent_file 的部分
            #（rf 已单独 _sec("recent_file(改文件快照段)")，但真实 merge 时它并入 tail 桶同一条
            # 消息——若统计用完整 text，rf 的 tokens 被算两次）。默认与 text 相同（无 rf 场景）。
            _stat = text if stat_text is None else stat_text
            _wrapped = "<system-reminder>\n" + text + "\n</system-reminder>"
            _tail_own = [{"role": "user", "content": "<system-reminder>\n" + _stat + "\n</system-reminder>"}]
            if msgs and msgs[-1].get("role") != "assistant":
                _last = msgs[-1]
                _c = _last.get("content")
                _m2 = {**_last}   # 浅拷贝——末条可能是 session 数据的共享引用，绝不就地改（防污染持久数据）
                if isinstance(_c, str):
                    _m2["content"] = _c + "\n" + _wrapped
                elif isinstance(_c, list):
                    _m2["content"] = list(_c) + [{"type": "text", "text": "\n" + _wrapped}]
                else:
                    _m2["content"] = _wrapped
                msgs[-1] = _m2
                _sec("tail(易变环境块·并入末条)", _tail_own, "统一<system-reminder>包裹·并入末条", msgs_n=0)
            else:
                msgs.append({"role": "user", "content": _wrapped})
                _sec("tail(易变环境块·并入末条)", _tail_own, "")

        def _find_assistant_after_user() -> int:
            if user_end_idx < 0:
                return -1
            for i in range(len(msgs) - 1, user_end_idx - 1, -1):
                if msgs[i].get("role") == "assistant":
                    return i
            return -1

        # —— reasoning 桶：作为思考链注入末条 assistant reasoning_content 前缀 ——
        # 触发（2026-09-03 粒度演进后）：仅当有 asm 项显式声明 reasoning pose。s0（无 assistant）
        # 或末条已非 assistant → 内容并入 reminder 桶（整桶退回 reminder 语义，不丢文字）。
        if reasoning_merge_text:
            _rai = _find_assistant_after_user()
            if _rai >= 0:
                _apply_reasoning_bucket(reasoning_merge_text, _rai, "asm项 reasoning pose")
            else:
                # 无可用 assistant（s0 首步）→ 并入 reminder 桶合并注入，内容不丢
                tail_merge_text = (tail_merge_text + "\n\n" + reasoning_merge_text) if tail_merge_text else reasoning_merge_text
                tail_stat_text = (tail_stat_text + "\n\n" + reasoning_merge_text) if tail_stat_text else reasoning_merge_text
                _sec("tail(易变环境块·reasoning→reminder 回退)", [{"role": "user"}], "s0无assistant，reasoning桶并入正文", msgs_n=0)

        # —— reminder 桶：<system-reminder> 包裹 → 并入末条 content ——
        # 额外保留 steps=reasoning 的兼容 + requires_reasoning 空槽承载（用户提案 2026-09-02）：
        # steps 段声明的 reasoning 仍使 reminder 桶整体改思考链注入（向后兼容旧写法——未标 pose
        # 的项显式/隐式走该路径）；requires_reasoning_in_history 模型且末条 assistant 思考槽为空
        # （DeepSeek 占位反正补空串）→ 动态状态占空槽优于空占位。
        if tail_merge_text:
            _ai = _find_assistant_after_user()
            _why = ""
            # steps=reasoning（seg 级，向后兼容旧写法）仍整体把 reminder 桶改思考链注入；
            # requires_reasoning_in_history 模型且末条 assistant 思考槽为空 → 动态状态占空槽。
            if _ai >= 0 and tail_mode == "reasoning":
                _why = "steps=reasoning(后兼容，未标pose项整体默认)"
            elif (_ai >= 0 and getattr(self.llm, "requires_reasoning_in_history", False)
                  and not str(msgs[_ai].get("reasoning_content") or "").strip()):
                _why = "requires_reasoning·空槽承载动态状态"
            if _why:
                _apply_reasoning_bucket(tail_merge_text, _ai, _why, stat_text=tail_stat_text)
            else:
                _apply_reminder_bucket(tail_merge_text, tail_stat_text)

        # —— image_feed 通道（2026-10-06·用户提案：实时画面装配段）——
        # tail_images 的 data URL 组装成 image_url 块：末条 user → content 数组化追加；
        # 末条 assistant → 独立 user 消息承载（部分端点不允许 assistant 挂图）。
        # 瞬态语义：组装层注入（不落 events.jsonl/step 存档）；每步求值 = 每步最新帧。
        if tail_images:
            _feed_note = f"[image_feed · 实时画面 · {time.strftime('%H:%M:%S')}]"
            if msgs and msgs[-1].get("role") == "user":
                _last = msgs[-1]
                _m2 = {**_last}   # 浅拷贝——绝不就地改共享引用
                _c = _m2.get("content")
                if isinstance(_c, str):
                    _blocks = [{"type": "text", "text": _c}] if _c else []
                elif isinstance(_c, list):
                    _blocks = list(_c)
                else:
                    _blocks = []
                _m2["content"] = _blocks + [{"type": "text", "text": _feed_note}] \
                                       + [{"type": "image_url", "image_url": {"url": u}}
                                          for u in tail_images]
                msgs[-1] = _m2
            else:
                msgs.append({"role": "user",
                             "content": [{"type": "text", "text": _feed_note}]
                                        + [{"type": "image_url", "image_url": {"url": u}}
                                           for u in tail_images]})
            _sec("image_feed(实时画面)", [{"role": "user", "content": f"[{len(tail_images)} 帧实时画面]"}],
                 "末条追加image_url（瞬态·不落档）", msgs_n=0)

        # —— 生成图自动可视（2026-10-07 用户提案：step 生成图片自动带 image_url）——
        # 伪造「read_file 调用+结果」对插在投影末尾：视觉模型"以为"自己调用过 read_file
        # 看图（叙事连贯），实际是框架自动注入——省一次真实 read_file。
        # 瞬态：组装层注入（不落 events.jsonl/step 存档）；vision 门控（非视觉不注入）。
        if self._gen_images_pending and getattr(getattr(self, "llm", None), "vision_supported", False):
            for gi in self._gen_images_pending:
                msgs.append({"role": "assistant", "content": None,
                             "tool_calls": [{"id": gi["call_id"], "type": "function",
                                             "function": {"name": "read_file",
                                                          "arguments": json.dumps({"path": gi["path"]}, ensure_ascii=False)}}]})
                msgs.append({"role": "tool", "tool_call_id": gi["call_id"],
                             "content": [{"type": "text", "text": f"✅ 已读取 {gi['path']}（{gi['kb']}KB 图片，像素级内容见下图）"},
                                         {"type": "image_url", "image_url": {"url": gi["data"]}}]})
            _sec("gen_images(生成图自动可视)", [{"role": "user", "content": f"[{len(self._gen_images_pending)} 张生成图]"}],
                 "伪造read_file对·瞬态不落档", msgs_n=0)

    # ========== 分档上下文投影（max_effective_context_window 启用）==========
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
        key = (level, fold, self.detail_base, self.detail_step)   # 含衰减参数：切 provider（detail_step 变）缓存失效重渲染
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
            # 超深档不投影 reasoning（用户裁定 2026-09-16：answer content 原文信息量已足，
            # reasoning 仅留在档位渲染与存档中，超深档只保留标注行 + answer 原文）
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
        # system append-not-replace 按轮锚定插入表（形态A·2026-09-12 用户裁定）：append 的 system
        # 固定插在【append 发生轮】的渲染块之前——轮归档后位置永不漂移，前缀跨轮稳定。
        _ap = {int(a.get("turn", 0)): str(a.get("text") or "")
               for a in (self._system_ledger.get("appends") or [])}

        def _turn_block(i: int) -> list[dict]:
            blk = self._render_turn_frozen(i)
            t = _ap.get(i + 1)                      # 轮号 1-based
            return ([{"role": "system", "content": t}] + blk) if t is not None else blk

        if fold_count > 0:
            if marks is not None:   # 装配外调用（_plan_fold/load/start_turn）无标记表，只渲染不记标记
                marks.append((f"折叠摘要({fold_count}轮)", 0, f"最早{fold_count}轮折叠为结构摘要，原文可recall"))
            body.append({"role": "system", "content": self._ambient(self._folded_summary(fold_count))})
        if marks is None:
            for i in range(fold_count, len(self.turns)):
                body.extend(_turn_block(i))
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
            groups[gname].extend(_turn_block(i))
            turns_n[gname] += 1
        for gname in order:
            _start = len(body)   # 段开始位置（与顶层 marks 的 len(msgs) 语义一致——都是 extend 前快照）
            body.extend(groups[gname])
            marks.append((f"{gname}历史({turns_n[gname]}轮)", _start, metas[gname]))
        return body

    def _plan_fold(self):
        """轮边界折叠+毕业计划（start_turn 时机）：
        触发线 = max_effective_context_window（投影顶窗才触发）；触发后压到 win×ratio
        （保留水位——ratio 是"触发后保留多少"，不是触发阈值；低 ratio 压得狠、下次顶窗间隔长）。
        【升档也在这里做】——先升档（压缩老档）再折叠（终极兜底），两步都以保留水位为目标。
        未触发区间（target~win）零动作纯追加，前缀缓存最优（轮内 _build 以计划结果为起点不再调整）。
        无窗口配置时计划为 0（现状）。估算用近似前缀（system+指引+静态记忆）+ 完整 body——
        与 _build 的真实估算差个动态 tail，余量下可忽略。"""
        if not self.max_effective_context_window:
            self._planned_fold = 0
            self._planned_graduates = 0
            return
        target = self.fold_target()   # 保留水位（win×ratio）；触发线见下方 win 判定——语义经用户两次澄清
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
        # 当前档（最后边界之后的段）> GRADUATE_FORCE_TURNS(30) 时分批升前 GRADUATE_FORCE_BATCH(15) 轮：
        # 档1 是全量披露档（"近期窗口"语义），窗口宽绰时压力循环永不触发会让它无限膨胀
        # （用户在 8000 实例观察到 64 轮/58.6%）。分批语义复用 _graduate_once（batch=15，
        # 近期轮保持 level1；循环到当前档 ≤30 时停——触发线与停刀线同一阈值，语义自洽）。
        last_completed = len(self.turns) - 1
        _seg_start = self._last_boundary() + 1 if self._tier_boundaries else 0
        if last_completed - _seg_start + 1 > GRADUATE_FORCE_TURNS:
            _before = len(self._tier_boundaries)
            while len(self._tier_boundaries) < len(self.turns) // GRADUATE_FORCE_BATCH + self.max_level + 2:
                _lc = len(self.turns) - 1
                _ss = self._last_boundary() + 1
                if _lc - _ss + 1 <= GRADUATE_FORCE_TURNS:
                    break
                if not self._graduate_once(batch=GRADUATE_FORCE_BATCH):
                    break
            g = len(self._tier_boundaries) - _before   # 并入总刀数（日志/报告）
            _LOG.info("卫生性强制毕业 +%d 刀（当前档曾 >%d 轮，每刀升前 %d）", g, GRADUATE_FORCE_TURNS, GRADUATE_FORCE_BATCH)
        # 先升档：反复 graduate 直到 ≤75%（或无可升）。估算 = prefix + 历史 + 当前轮近似
        # 当前轮估算剥除 recent-file 块（_rf_stripped）：rf 是轮内易变项（归档即消失），
        # 它的体积不该推动升档/折叠——panic 轮内路径调用本函数时 cur_est 含 rf 会过激压缩
        cur_est = self._rf_stripped(self._seg_msgs_user_message() + self._seg_msgs_steps())   # 当前轮（tail 量小不计）
        # —— 触发判定（修复：曾以 target 为触发线——投影在 target~win 健康区间也每轮升档毕业）——
        # 触发线 = 窗口本身：未顶窗零动作；顶窗后升档/折叠压到 target（保留水位）。
        # 口径修复（2026-08-31·用户报告"实测 381K<win 仍每轮毕业"）：以【实际生效口径】
        # （当前折叠计划 _planned_fold 渲染）估算——曾硬编码 fc=0 假想（假装零折叠渲染全部
        # 历史），对已折叠 session 该假想恒超线（实测 856K vs 实际投影 320K），导致每轮
        # 进压力路径→升档刀每轮顺移（"每轮毕业"）。fc=0 时两口径等价（首次判定语义不变）。
        if self._estimate_tokens(prefix + self._render_tiered_history(self._planned_fold) + cur_est) <= self.max_effective_context_window:
            # 未顶窗：保留现有折叠计划（含 load 恢复值）——"折叠粘性"（缓存稳定优先，用户裁定 2026-08-31）。
            # 曾在此清零（自适应退折：窗口宽绰时放弃折叠）——但 fc 变化会重写历史段头部（fc 摘要边界），
            # restart 后前缀全断（t506·s0 实测 12.7% 命中 / 300K tok 全价）。退折场景罕见（窗口调大），
            # 且加刀路径仍在（真超窗继续折叠）；强制退折可手动清 meta.json 的 fold_count。
            self._planned_graduates = 0
            return
        g = 0
        # 上限：分批毕业后一次 _plan_fold 可能连切数刀（90 轮大档=3 刀）；
        # _deepen_oldest_tier 还要再切 max_level 刀把老档推进工具折叠档（重复边界不占 max_level），
        # 故上限 = 轮数/批宽 + 2×max_level + 4。
        g_cap = len(self.turns) // GRADUATE_BATCH_TURNS + 2 * self.max_level + 4
        _est0 = lambda k: self._estimate_tokens(prefix + self._render_tiered_history(k) + cur_est)
        while g < g_cap:
            if _est0(0) <= target:
                break
            # ① 先切最年轻段（无损：只降文字档上限，近期保真）
            # ② 年轻端切完 → 推老档进工具折叠档（阶梯中间一级：工具调用折一行、answer/reasoning 原文保留）
            # ③ 再没有可推的 → break 出去走折叠（结构摘要，终极兜底）
            # 注意先估再动（推老档每刀都要重渲染 1-2 档；300 轮超深档实测单刀 ~1s）
            if self._estimate_tokens(prefix + self._render_tiered_history(0) + cur_est) <= target:
                break
            if self._graduate_once():
                g += 1
                continue
            if self._deepen_oldest_tier(est_fn=_est0, target=target):
                g += 1
                continue
            break
        # 再折叠：若升档后仍 >75%，折叠到 ≤75%（或无可折）。
        # 首刀大刀（_fold_leap_target）：至少吞超深档的一半——边界密集时碎刀（每刀 1-2 轮）
        # 触发过勤，超深态留存太短；之后仍超线再碎刀微调。
        fc = 0
        _est = lambda k: self._estimate_tokens(prefix + self._render_tiered_history(k) + cur_est)
        for _ in range(len(self.turns) + 4):
            if _est(fc) <= target:
                break
            nxt = (self._fold_leap_target(fc, _est, target) if fc == 0
                   else self._next_fold_target(fc, est_fn=_est, target=target))
            if nxt is None:
                break
            fc = nxt
        if fc != self._planned_fold or g != self._planned_graduates:
            _LOG.info("轮边界计划：升档 %d 档 + 折叠 %d 轮（目标 ≤%.0f%%×%d）",
                      g, fc, 100 * (self.fold_target_ratio or FOLD_TARGET_RATIO),
                      self.max_effective_context_window)
            # 历史段形态真实变化（毕业顺移/折叠重排）= 前缀缓存必断——system 账本顺带归一化（DSH 断点清账，
            # 用户裁定 2026-09-12：append 以后的归一化挂在这一时刻）。计划未变（纯追加轮）不置。
            self.mark_system_dirty(f"毕业/折叠执行（升{g}档+折{fc}轮）")
        if fc > 0 and self._tier_boundaries:
            # 已折叠轮不再参与渲染：raw_level(i>=fc) 只数 b >= i 的边界 → < fc 的边界是死重，清掉：
            # ① 语义干净（_fold_leap_target 的 bs[-max_level] 在超深段折空时正确退化为按轮吃）；
            # ② meta.json 体积（本 session 曾有 183 个边界、绝大多数 < fc）。
            # 对未折轮零影响（它们的 count 不含这些 b）→ 无需清冻结缓存。
            _kept = [b for b in self._tier_boundaries if b >= fc]
            if len(_kept) != len(self._tier_boundaries):
                self._tier_boundaries = _kept
        self._planned_fold = fc
        self._planned_graduates = g

    def _graduate_once(self, batch: int = None) -> bool:
        """毕业一批 turn：append 新边界到 _tier_boundaries（边界之前的轮 level+1=顺移），
        并清掉 level 变了的冻结缓存让其按新级别重渲染。
        批量语义（batch 默认 GRADUATE_BATCH_TURNS=30）：当前段（最后边界之后）≤批时 → 整段一次升（旧行为）；
        超过批 → 只升【前 N 轮】（新边界=段起点+N-1），近期轮保持 level1 不动——
        大档分批毕业，升档粒度可控（压缩需要多少升多少，近的保真）。
        卫生性路径传 batch=GRADUATE_FORCE_BATCH（15，用户裁定 2026-09-16）。
        当前段无已完成 turn → 返回 False（_plan_fold/保命阀循环据此停止）。"""
        last_completed = len(self.turns) - 1
        if last_completed < 0:
            return False
        seg_start = self._last_boundary() + 1 if self._tier_boundaries else 0
        if seg_start > last_completed:
            return False   # 当前段只剩进行中 turn，无东西可升
        _b = batch or GRADUATE_BATCH_TURNS
        seg_len = last_completed - seg_start + 1
        new_b = last_completed if seg_len <= _b \
            else seg_start + _b - 1
        self._tier_boundaries.append(new_b)
        self._invalidate_frozen_after_graduate()
        return True

    def _last_boundary(self) -> int:
        """最新毕业边界（= 最大边界）。_tier_boundaries 是【多重集】——_deepen_oldest_tier 会在
        老位置（fc）插入重复边界承载"多切一刀"的档位深度（raw_level 只数个数），故取 max 而非 [-1]。"""
        return max(self._tier_boundaries) if self._tier_boundaries else -1

    def _recompute_tier_boundaries(self) -> list:
        """按卫生性毕业规则（纯结构、确定性：当前档 >GRADUATE_FORCE_TURNS(30) → 每刀升前
        GRADUATE_FORCE_BATCH(15)）从零模拟到当前轮数（用户提案 2026-09-30：tier_boundaries
        不再持久化——投影规则的唯一真源是当前代码，重启/rewind 后按规则重算定型）。

        - 与 _graduate_once(batch=15) 的 new_b 语义逐刀对齐（seg_start+batch-1）→ 同规则下
          模拟结果 = 运行期卫生性演化的结果（确定性）；
        - 压力毕业不重放（依赖历史 profile 快照的估算，不可重放）——体积压力由首次投影的
          _plan_fold 折叠兜底（fc 按当前体积现算）；
        - 规则/参数演化后重启即按新规则定型（断点免费，与切模型重刷同哲学）；
        - 调用方须保证 _frozen_renders 为空（档位变了渲染必须按新边界重算）。"""
        n = len(self.turns)
        bs: list = []
        seg_start = 0
        last_completed = n - 1
        while last_completed - seg_start + 1 > GRADUATE_FORCE_TURNS:
            new_b = seg_start + GRADUATE_FORCE_BATCH - 1
            bs.append(new_b)
            seg_start = new_b + 1
            if len(bs) > n:   # 防御（每刀升 15 轮，刀数天然 ≤ n/15，不应触达）
                break
        return bs

    def _invalidate_frozen_after_graduate(self) -> None:
        """毕业/推进边界后的冻结缓存失效（_graduate_once / _deepen_oldest_tier 共用）。
        判定用完整 key (level, fold, base)——毕业顺移可能只改 raw 不改封顶 level
        （raw 4→5 时 tier 仍 4 但 fold 位 False→True），base 变化（/config/切模型窗口变）同理。"""
        _fold_on = config.load_fold_deep_tools()
        _db = self.detail_base
        _ds = self.detail_step
        for i in range(len(self.turns)):
            fr = self._frozen_renders.get(i)
            if fr and fr[0] != (self._tier_level(i),
                                _fold_on and self._raw_tier_level(i) > self.max_level,
                                _db, _ds):
                self._frozen_renders.pop(i, None)

    def _deepen_oldest_tier(self, est_fn=None, target: int = 0, fold_count: Optional[int] = None) -> bool:
        """推进压缩阶梯的中间一级：把最老的【未折叠档】整档推进【工具折叠档】（raw_level > max_level）。
        背景（用户裁定 2026-09-14）：升档刀 _graduate_once 只切最年轻段（最后边界之后的段），
        年轻端切完即返回 False → 升档循环 break → 直接折叠整档进结构摘要，中间的「工具折叠档」
        （工具调用折成一行标注、保留 answer/reasoning 原文）被整段跳过。本函数补上这一级。
        原理：raw_level(i) = 1 + count(b >= i)——未折区【最小边界】b_min 是"最老档"
        （i ∈ [fc, b_min]，raw_level 最大的那些轮）的上界：在 b_min 处再插一个边界，
        该档整档 count+1（跨过 max_level 即整档工具折叠），而 i > b_min 的年轻轮 count 不变、
        保真度零损失。每插一个即多切一刀，直到最老未折轮 raw_level > max_level（整档已折叠）为止。
        带 est_fn/target 时按【实际压缩收益】决策——但收益判定不阻断阶梯（用户实锤 2026-09-24：
        此前「收益 <2% → return False」会让整个中间级被弃、压力循环直接 break 进折叠大刀，
        文字档轮未经工具折叠直接折进结构摘要（跳级）。现改为：仍超 target 时永不因小收益
        放弃推进——每刀推最老档跨入超深（结构正确），小收益也推；循环回来自然轮到次老档，
        阶梯逐级渐进。达标路径由调用方循环条件保证（est ≤ target 即 break，不会多推）。"""
        if not config.load_fold_deep_tools():
            return False
        fc = self._planned_fold if fold_count is None else fold_count
        if fc > len(self.turns) - 1:
            return False                     # 没有未折叠的已完成轮
        if self._raw_tier_level(fc) > self.max_level:
            return False                     # 最老未折轮已在超深档：这一级已用尽
        seg = [b for b in self._tier_boundaries if b >= fc]
        if not seg:
            return False                     # 未折区无边界：全员 +1 是 _graduate_once 的活
        # 位置取【未折区最小边界】：正好是"最老档"的上界（见 docstring）
        bisect.insort(self._tier_boundaries, min(seg))
        self._invalidate_frozen_after_graduate()
        return True

    def _next_fold_target(self, fold_count: int, est_fn=None, target: int = 0):
        """下一个折叠点。
        带 est_fn/target（用户裁定 2026-09-14）：被折叠的轮不再参与档位渲染，对齐 boundary 已无意义
        → 从 fold_count 起【按轮】吃，返回恰好达标（估算 ≤ target）的轮数：一次到位，既不浪费时间
        （不多吃一轮），又不产生"每刀只折 1-2 轮"的碎刀（碎刀每轮都断前缀缓存）。
        【按轮吃的上限 = 超深段末端】（用户实锤 2026-09-24）：fold_deep_tools 开时只吃【工具折叠档】
        的轮（fc..deep_end）——文字档轮必须先经 _deepen_oldest_tier 推进超深（阶梯顺序：升档 → 工具
        折叠 → 才折叠进结构摘要）；吃满超深段仍不达标 → 返回 cap（示意"超深已折尽"，下轮压力循环
        会先推进②扩大工具折叠档再折——跨触发渐进）。
        吃完全部轮仍不达标 → 返回全折（last_completed+1）；已经全折 → None。
        不带 est_fn（旧调用点）→ 旧语义：超过 fold_count 的最小 boundary+1（按整档吃）。"""
        last_completed = len(self.turns) - 1
        if est_fn is None or not target:
            for b in sorted(self._tier_boundaries):
                if b + 1 > fold_count:
                    return b + 1
            return None
        cap = last_completed + 1
        if config.load_fold_deep_tools():
            bs = sorted(self._tier_boundaries)
            if len(bs) > self.max_level:
                cap = bs[-self.max_level] + 1   # deep_end+1：最后一个超深轮之后（不吃文字档）
        for cand in range(fold_count + 1, min(last_completed + 1, cap) + 1):
            if est_fn(cand) <= target:
                return min(cand, cap)
        return cap if cap > fold_count else None

    def _fold_leap_target(self, fc: int, est_fn=None, target: int = 0):
        """fc 大刀首折目标：至少吞掉【超深档的一半】到 fc 结构摘要（用户裁定 2026-08-28：
        边界密集（滚动毕业 ~1.9 轮/边界）时碎刀偏勤——每轮边界触发、每刀只折 1-2 轮，
        超深态（answer/reasoning 原文）留存太短。一次大刀一半，触发间隔翻倍、留存翻倍）。
        超深段 = [fc, bs[-max_level]]（raw>max_level 的轮；最后一个超深轮 = 倒数第 max_level 个边界，
        因 raw_level(i)=1+count(b>=i)，count(bs[-max_level])=max_level → raw=max_level+1）。
        fold_deep_tools 关 / 档梯未满（无超深段）→ 退化为碎刀 _next_fold_target。"""
        if not config.load_fold_deep_tools():
            return self._next_fold_target(fc, est_fn, target)
        bs = sorted(self._tier_boundaries)
        if len(bs) <= self.max_level:
            return self._next_fold_target(fc, est_fn, target)   # 档梯未满：交按轮吃
        deep_end = bs[-self.max_level]          # 最后一个超深轮（合法折叠点 = deep_end+1）
        if deep_end + 1 <= fc:
            return self._next_fold_target(fc, est_fn, target)   # 超深段已折完：剩档内段，交按轮吃
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

    def _folded_summary_lines(self, start: int, end: int) -> list:
        """fc 清单的逐轮行生成（[_folded_summary 的内部实现，sos 分段用]）。
        start/end 为轮索引区间 [start, end)。"""
        lines = []
        for i in range(start, min(end, len(self.turns))):
            t = self.turns[i]
            n = sum(len(s.tool_calls) for s in t.steps)
            u = (t.user_message or "").strip().replace("\n", " ")[:80]
            mid = f" (已折叠{n}次工具调用) " if n else " "
            tail = ((t.recap or "").strip()
                    or self._summarize_answer(t.answer)
                    or "中断(未回答)")
            lines.append(f"[第{i + 1}轮] {u}{mid}→ {tail}")
        return lines

    def _folded_summary(self, fold_count: int) -> str:
        """被折叠的早期轮次概览：每轮 user + (已折叠N次工具调用) + recap/answer摘要/中断(未回答)。
        纯结构信息、无需 LLM。tail 优先级：recap（turn_end 本地小模型生成的一句话——语义密度高于
        answer 代码摘要的"首行+标题"，后者常是"完成并推送"类横幅文案）→ answer 代码摘要 → 中断标注。
        逐字原文用 recall 召回。
        sos 形态（用户提案 2026-10-02）：_sos_count>0 且 fold_count>_sos_count 时，前段由
        LLM 浓缩摘要（_sos_text）替代，清单只保留次早期段（轮号保持真实——recall 按轮号召回）。"""
        sos_n = int(getattr(self, "_sos_count", 0) or 0)
        sos_t = (getattr(self, "_sos_text", "") or "").strip()
        if sos_n > 0 and fold_count > sos_n and sos_t:
            tail_lines = self._folded_summary_lines(sos_n, fold_count)
            return ("【早期轮次 sos 浓缩摘要（LLM 总结，覆盖第1~%d轮；逐字原文用 recall 召回）】\n%s\n\n"
                    "【次早期轮次清单（第%d~%d轮）】\n%s") % (
                        sos_n, sos_t, sos_n + 1, fold_count, "\n".join(tail_lines))
        return "【已折叠的早期轮次（逐字原文用 recall 召回）】\n" + \
               "\n".join(self._folded_summary_lines(0, fold_count))

    def _generate_sos(self, start: int, end: int) -> str:
        """sos（summary of summary，用户提案 2026-10-02）：把 fc 清单 [start, end) 轮交给
        LLM 浓缩成一份叙事摘要（保留关键事实/决策/踩坑/版本/路径，≤1200 字）。
        折半到全折仍超预算时的终极压缩；输入恒为原始清单段（非再压缩，信息保真）。
        失败返回 ""（降级纯清单，不阻塞投影）。"""
        try:
            src_lines = self._folded_summary_lines(start, end)
            if not src_lines:
                return ""
            prompt = ("以下是一个长期开发会话中第 %d~%d 轮的逐轮流水清单（每轮：用户诉求 → 一句话结果）。"
                      "请把它浓缩成一份【叙事摘要】，供新接手的 AI 继续这个会话时作为这段时期的背景记忆。\n"
                      "要求：保留关键事实与决策（做了什么/为什么）、重要踩坑与教训、涉及的项目/文件/版本号/"
                      "服务端口等具体锚点、随时间演进的主线脉络；丢弃一次性琐事与重复模式；"
                      "不超过 1200 字，直接输出摘要正文（不要开头结尾客套）。\n\n清单：\n%s"
                      % (start + 1, end, "\n".join(src_lines)))
            resp = (self.utility_llm or self.llm).chat(
                [{"role": "user", "content": prompt}], scene="sos")
            txt = (getattr(resp, "content", "") or "").strip()
            return txt[:4000]
        except Exception as e:
            _LOG.warning("sos 浓缩失败（降级纯清单）：%s", e)
            return ""

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
        rf 是轮内易变项（归档即消失），不该推动升档/折叠等不可逆历史压缩（用户裁定 2026-08-29）。
        判定按 tool_call_id ∈ rf_map 命中集合（用户设计）：与附加时的命中条件同源——
        附加按 cid 命中，剥离也按 cid，因果一致不漂移，不做 content 子串检测；
        sub 对无块 content 是 no-op，多模态 list 由 isinstance 防御天然跳过。
        施工模式命中集合全量扩展（_rf_hit_cids——内嵌不去重，剥离随附加口径）。"""
        _hit = self._rf_hit_cids()
        out = []
        for m in msgs:
            c = m.get("content")
            if (m.get("role") == "tool" and m.get("tool_call_id") in _hit
                    and isinstance(c, str)):
                m = dict(m, content=_RE_RF_BLOCK.sub("", c))
            out.append(m)
        return out

    def _rf_in_msgs(self, msgs: list[dict]) -> int:
        """msgs 中实际附加的 <recent-file> 块总字符数（诊断/验证口径）。判定同
        _rf_stripped：tool_call_id ∈ rf_map 命中集合（不做 content 子串检测）。
        施工模式命中集合全量扩展（_rf_hit_cids——与内嵌附加口径同源）。"""
        _hit = self._rf_hit_cids()
        n = 0
        for m in msgs:
            if (m.get("role") == "tool" and m.get("tool_call_id") in _hit
                    and isinstance(m.get("content"), str)):
                n += sum(len(x) for x in _RE_RF_BLOCK.findall(m["content"]))
        return n

    @staticmethod
    def _rf_outline(path: str, text: str) -> str:
        """超大 recent-file 的结构摘要（用户提案 2026-08-31）：py → ast 函数/类行号结构；
        md → 标题大纲（复用 real_tools._md_outline——与 dir_outline/_md_snapshot 同源）；
        其它 → 行数 + 头部缩略。限 60 行（防超大类清单本身膨胀）。"""
        try:
            p = str(path).lower()
            if p.endswith(".md"):
                from real_tools import _md_outline
                o = _md_outline(text)
                return o[:4000]
            if p.endswith(".py"):
                import ast
                tree = ast.parse(text)
                out = []
                def _walk(node, prefix=""):
                    for ch in ast.iter_child_nodes(node):
                        if isinstance(ch, (ast.FunctionDef, ast.AsyncFunctionDef)):
                            end = getattr(ch, "end_lineno", None) or ch.lineno
                            out.append(f"{prefix}def {ch.name}()  [L{ch.lineno}-L{end}]")
                        elif isinstance(ch, ast.ClassDef):
                            end = getattr(ch, "end_lineno", None) or ch.lineno
                            out.append(f"{prefix}class {ch.name}  [L{ch.lineno}-L{end}]")
                            _walk(ch, prefix + "  ")
                _walk(tree)
                if len(out) > 60:
                    out = out[:60] + [f"…（共 {len(out)} 项，截断显示前 60）"]
                return "\n".join(out) or "(无顶层定义)"
            lines = text.splitlines()
            head = [l.strip()[:60] for l in lines if l.strip()][:5]
            return f"（{len(lines)} 行）\n" + "\n".join(head)
        except Exception as e:
            return f"(结构提取失败: {type(e).__name__})"

    def _rf_hit_cids(self) -> set:
        """rf 命中 cid 集合（剥离 _rf_stripped / 诊断 _rf_in_msgs 的判定源，与附加口径同源）：
        非施工 = _rf_latest_map 的 cid（同文件只有最新一次命中，段式集中投影）；
        施工模式（2026-09-13·用户裁定）= 当前轮全部写调用 cid——内嵌不去重/不限数量，
        每个写调用各挂各的当时快照，剥离/诊断集合随之全量扩展。"""
        if self._current is None:
            return set()
        if self._construction_mode():
            return {cid for s in self._current.steps for cid in (s.file_snapshots or {})}
        return {m["cid"] for m in self._rf_latest_map().values()}

    def _rf_inline_block(self, snap: dict) -> str:
        """施工期 <recent-file> 内嵌块（用户裁定 2026-09-13）：贴在该次写调用的 tool result
        尾部，内容 = 该次调用【当时】的快照（file_snapshots 按 call_id 存的就是当时版本——
        不去重、不限数量）。小文件行号化全文（与段式段 / read_file 口径一致）；超大文件
        （>RF_MAX_CHARS=100K）outline + 省略提示。注意阈值与非施工段式（RF_SEG_MAX_CHARS=15K，
        2026-09-15 收紧）不同——施工内嵌块走 _constr_buf 定型（字节冻结不重渲染），
        大一些不伤缓存；段式是尾部每步重渲染的易变项，阈值更紧。
        块以 \\n 开头——_RE_RF_BLOCK 剥离/诊断正则天然命中。"""
        path, ver = str(snap.get("path", "")), str(snap.get("version", ""))
        text = str(snap.get("text", ""))
        if len(text) > RF_MAX_CHARS:
            ov = self._rf_outline(path, text).strip() or "(结构提取失败)"
            return (f'\n<recent-file file="{path}" version="{ver}" size="{len(text)}">\n'
                    f"<overview>\n{ov}\n</overview>\n"
                    f'<content note="文件过大（{len(text):,} 字符 > {RF_MAX_CHARS:,}）——此处省略，'
                    f'需要时 read_file 分段读取"/>\n</recent-file>')
        _lines = text.split("\n")
        _w = max(2, len(str(len(_lines))))          # 行号宽度自适应（与 read_file / 段式口径一致）
        numbered = "\n".join(f"{i:>{_w}}| {ln}" for i, ln in enumerate(_lines, 1))
        return f'\n<recent-file file="{path}" version="{ver}">\n{numbered}\n</recent-file>'

    def _rf_latest_map(self) -> dict:
        """当前轮 recent-file 最新映射（用户设计 2026-08-29）：filename -> {cid, path, version, text}。
        同文件多次 edit 后写覆盖——只有【最新一次改它的 tool_call】会在投影时命中挂快照。
        归档轮/历史段的 call_id 不在映射里（映射只从 _current 构建），天然"前面的轮不管"。
        超大文件跳过（用户裁定 2026-08-31 起阈值 RF_MAX_CHARS=100K；2026-09-15 非施工收紧为
        RF_SEG_MAX_CHARS=15K——段式是尾部每步重渲染的易变项，小文件全文也该转 outline 压体积；
        施工期内嵌仍走 100K（_rf_inline_block 口径不变））：置空 + skip 标记——投影时挂
        outline + 省略提示而非全文（巨型文件单步稀释命中率：index.html 130K 实测 99%→81%）。"""
        if self._current is None:
            return {}
        latest: dict[str, dict] = {}
        for s in self._current.steps:
            for cid, snap in (s.file_snapshots or {}).items():
                if isinstance(snap, dict) and snap.get("path"):
                    _txt = str(snap.get("text", ""))
                    _too_big = len(_txt) > RF_SEG_MAX_CHARS
                    latest[str(snap["path"])] = {"cid": cid, "path": str(snap["path"]),
                                                 "version": str(snap.get("version", "")),
                                                 "text": "" if _too_big else _txt,
                                                 "skip": len(_txt) if _too_big else 0,
                                                 # 结构摘要（用户提案 2026-08-31）：超大文件不再只挂
                                                 # 提示行——投影 py 的函数行号结构 / md 的大纲（_rf_outline）
                                                 "outline": (self._rf_outline(str(snap["path"]), _txt)
                                                             if _too_big else "")}
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
        vision = getattr(getattr(self, "llm", None), "vision_supported", False)
        if not vision:
            def _sub(m):
                n = m.group(1)
                return (f'[图片 {n}，当前模型无视觉能力无法查看；'
                        f'如需理解图片内容，可委托视觉子 agent（agent_prompt("vision", "请描述 图片 {n}")）]')
            return _IMG_TAG_RE.sub(_sub, text)
        out, last = [], 0
        for m in matches:
            if m.start() > last:
                out.append({"type": "text", "text": text[last:m.start()]})
            try:
                p = repo_images_dir(self.workspace) / m.group(1)
                mime = mimetypes.guess_type(str(p))[0] or "image/png"
                b64 = base64.b64encode(p.read_bytes()).decode()
                out.append({"type": "image_url",
                            "image_url": {"url": _norm_img_data_url(f"data:{mime};base64,{b64}")}})
            except Exception:
                out.append({"type": "text", "text": f"[图片 {m.group(1)} 读取失败]"})
            last = m.end()
        if text[last:]:
            out.append({"type": "text", "text": text[last:]})
        return out

    def _user_content(self, turn: "Turn"):
        """构造 user 消息内容。turn.images(用户贴图 data URL)→image_url 块(仅视觉模型)；
        user_message 里的  [图片 name 读取失败] (工具图/子agent委托)按当前模型 vision 投影。
        非视觉模型收到贴图 → 文字占位（防 provider 400: content.type 只允许 text）。"""
        text = self._project_imgs(turn.user_message)
        if not turn.images:
            return text
        vision = getattr(getattr(self, "llm", None), "vision_supported", False)
        blocks = list(text) if isinstance(text, list) else [{"type": "text", "text": text}]
        if vision:
            blocks.extend({"type": "image_url", "image_url": {"url": _norm_img_data_url(img)}}
                          for img in turn.images)
        else:
            blocks.append({"type": "text", "text":
                           f"[用户贴图 {len(turn.images)} 张，当前模型无视觉能力无法查看；"
                           "如需理解图片内容，可委托视觉子 agent（agent_prompt('vision', ...) 并在 prompt 中带 <img>标签）]"})
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
                           base: int = None, full_window: int = None) -> list[dict]:
        """把一组 Step 还原成 role 消息：assistant(tool_calls + reasoning_content) + 各 tool 结果。
        工具名/入参/结果从 toollog 按 call_id 召回。step 级策略（分组投影，缓存友好）：
        - 每 GROUP_STEPS 步一组，组内所有步 limit 一致 → byte-stable（利于前缀缓存）；
        - 组号差 = 当前步所在组 - 本步所在组：
            · 差 ≤ 1（当前组 + 上一组）→ 【全量】披露（当前轮 FULL_STEP_CAP_CHARS 上限；
              老轮则用 base=该档最大字数，不额外衰减）；
            · 差 ≥ 2 → limit = eff_base - GROUP_STEPS * detail_step * 组号差（≥DETAIL_FLOOR）；
        - reasoning 永远原样挂 reasoning_content（不压缩，含 step0 的核心设计思考）。
        施工模式（2026-09-13·用户裁定）：recent-file 快照回内嵌——该次写调用【当时】的快照贴在
        该次 tool result 尾部（不去重/不限数量：每个写调用各挂各的，施工要"每次操作时文件长什么样"
        的因果上下文）；非施工模式不内嵌（第四版段式 _seg_msgs_recent_file 走装配）。
        max_steps>0 只保留最近 max_steps 步。"""
        msgs = []
        constr = self._construction_mode()
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
                msgs.append({"role": "user", "content": self._project_imgs(_MIDTURN_TAG + step.preceding_hint)})
            group_diff = cur_group - (idx // GROUP_STEPS)   # 本步组与当前组的组号差（0=同组）
            if group_diff <= 1 and base is None:
                full = True        # 当前轮：当前组 + 上一组全量
                limit = 0          # full 分支不使用 limit
            elif group_diff <= 1:
                full = False       # 老轮：当前组 + 上一组用该档最大字数，不额外衰减
                limit = base
            else:
                full = False       # 更早组：按组号差线性衰减
                limit = max(eff_base - GROUP_STEPS * self.detail_step * group_diff,
                            DETAIL_FLOOR)
            a_tool_calls = []
            # 失联调用不进投影（用户裁定 2026-10-09，与 _current_turn_msgs 同款）：
            # toollog 无记录的 call_id 成对剔除；整步失联则跳过该 step（reasoning 一并跳）
            live_tcs = [tc for tc in step.tool_calls if self.toollog.get(tc.call_id)]
            if not live_tcs:
                continue
            for i, tc in enumerate(live_tcs):
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
            for i, tc in enumerate(live_tcs):
                _n, _a, result = self.toollog.view(tc.call_id)
                content = (self._cap_full_result(result, tc.call_id) if full
                           else self._summarize_text(result, limit, tc.call_id))
                content = self._project_imgs(content)
                # recent-file 挂载点：2026-09-07 起改独立段走装配（_seg_msgs_recent_file）；
                # 施工模式（2026-09-13·用户裁定）回内嵌形态——该次调用【当时】的快照贴在该次
                # tool result 尾部：不去重（同文件多次编辑各挂当时的版本）、不限数量（每个写
                # 调用都挂）——施工需要"每次操作时文件长什么样"的因果上下文。归档轮
                # file_snapshots 不持久化（空 dict）+ 非施工不内嵌——双保险不走旧路。
                if constr:
                    snap = (step.file_snapshots or {}).get(tc.call_id)
                    if isinstance(snap, dict) and snap.get("path"):
                        content += self._rf_inline_block(snap)
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

    def recall(self, query: str, contains_reasoning: bool = False, tools: str = "brief") -> str:
        """按关键词/语义在【全部】历史轮次里搜索，返回匹配轮的上下文。
        contains_reasoning=False（默认）不含思考过程；True 则带上每步 reasoning 与回答的 reasoning。
        tools="brief"（默认）只给整段 user+answer 原文 + 工具调用折叠成一行（工具过程细节用
        agent_query_tool_detail 查）；"full" 展开每个工具调用的入参与结果。

        检索策略（自动降级）：
          1. 配了 embed 模型(self.vec_store 非 None) → 语义召回 top-K 轮（换说法也能搜到）
          2. 否则 → summary+user+answer 匹配；query 支持多关键词（| / 空格 / 中英文逗号 / 顿号 / 分号
             分隔，任一命中即命中；OR 语义）与通配符（* ? []，fnmatch）——用户提案 2026-09-20
        两条都跨当前会话全部 turns（含被折叠为结构摘要的轮——其 user/answer 原文始终保留可召回）;
        语义路径还覆盖 reasoning 内容（密度更高）。
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
                    block = self._format_turn_full(i + 1, t, contains_reasoning, tools)
                    tag = f" (相似度 {score:.2f})" if score else ""
                    block = f"━━━ 【第{i + 1}轮】{t.summary or '(无摘要)'}{tag}\n" + block.split("\n", 1)[1] \
                        if "\n" in block else block
                    if total + len(block) > CAP:
                        out.append(f"\n…（还有 {len(hits) - len(out) + 1} 轮命中已省略）")
                        break
                    out.append(block)
                    total += len(block)
                return "\n".join(out)
        # —— 2) 拆词 OR + 通配兜底（没配 embed，或语义无结果；用户提案 2026-09-20）——
        import re as _re
        import fnmatch as _fn
        parts = [p for p in _re.split(r"[|\s,，、;；]+", q) if p] or [q]
        def _hit(_part: str, _tl: str) -> bool:
            if any(c in _part for c in "*?["):          # 通配符 → fnmatch（前后包 * = 文本内任意处命中）
                try:
                    return _fn.fnmatch(_tl, "*" + _part.lower() + "*")
                except Exception:
                    return _part.lower() in _tl
            return _part.lower() in _tl                  # 普通词 → 子串（大小写不敏感）
        hits = [(i, t) for i, t in enumerate(self.turns)
                if any(_hit(p, (t.summary + "\n" + t.user_message + "\n" + t.answer).lower()) for p in parts)]
        if not hits:
            return f"未找到包含「{'/'.join(parts)}」的历史轮次。可用 /recall 换个关键词，或 /show 看概览。"
        out, total, CAP = [f"找到 {len(hits)} 轮匹配「{'/'.join(parts)}」的历史："], 0, 4000
        for i, t in hits:
            block = self._format_turn_full(i + 1, t, contains_reasoning, tools)
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

    def _format_turn_full(self, n: int, t: Turn, contains_reasoning: bool = False, tools: str = "brief") -> str:
        """把一轮格式化成可读文本（召回展示用）。contains_reasoning=True 时带上每步与回答的 reasoning。
        tools="brief"（默认）：工具调用折叠成一行「🔧 工具调用 N 个: …」——被折叠为结构摘要的轮命中时
        以整段 user+answer 原文召回（工具过程细节用 agent_query_tool_detail 按 call_id 查）；
        tools="full"：展开每个工具调用的入参与结果。用户提案 2026-09-20。"""
        lines = [f"━━━ 【第{n}轮】{t.summary or '(无摘要)'}", f"用户: {t.user_message}"]
        if tools == "full":
            for step in t.steps:
                if contains_reasoning and step.reasoning:
                    lines.append(f"  💭 {step.reasoning}")
                for tc in step.tool_calls:
                    name, a, r = self.toollog.view(tc.call_id)
                    args_s = json.dumps(a, ensure_ascii=False)
                    lines.append(f"  🔧 {name}({args_s}) → {(r or '')[:300]}")
        else:
            names = []
            for step in t.steps:
                if contains_reasoning and step.reasoning:
                    lines.append(f"  💭 {step.reasoning}")
                for tc in step.tool_calls:
                    names.append(self.toollog.view(tc.call_id)[0])
            if names:
                shown = ", ".join(names[:15])
                lines.append(f"  🔧 工具调用 {len(names)} 个: {shown}{'…' if len(names) > 15 else ''}")
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
                        "answer_reasoning": t.answer_reasoning or "",
                        "changed": t.changed or []})
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
        # repo 级 _origin.txt（记录工作区路径，便于排查）——失败仅告警（night_tasks #1）
        try:
            (sdir.parent.parent / "_origin.txt").write_text(str(self.workspace.resolve()), encoding="utf-8")
        except Exception as e:
            _LOG.warning("_origin.txt 写入失败（跳过）：%s", e)
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
                # tier_boundaries 恢复持久化（2026-09-30 回滚——recalc-only 方案在长会话重启时
                # 引发 _plan_fold 折叠螺旋：运行期演化出的"末端密集边界"（压力毕业/deepen 的多重集）
                # 无法被卫生性规则复现 → 重启后 L0 窗口暴涨 → 逐步碎刀+全量重渲染 → 数分钟无响应 +
                # fc 一路吃到 len(turns)（近期上下文全进摘要）。存档优先、缺失才 recalc 兜底）
                "tier_boundaries": self._tier_boundaries,
                "fold_count": self._planned_fold,           # 折叠计划持久化（缓存稳定）：重启沿用、未顶窗不清零
                "system_ledger": self._system_ledger,       # system append-not-replace 账本（last_text 快照字节——重启后前缀仍稳定）
                "profile_fp": self._profile_fingerprint(),  # 投影相关 profile 指纹（load 时对比——变了才全量重刷，保住同模型重启的缓存延续）
                "sos_count": self._sos_count,                # sos 档（用户提案 2026-10-02）：LLM 浓缩摘要覆盖的轮数
                "sos_summary": self._sos_text,               # sos 叙事摘要（内容跨模型通用——切模型不重生成）
                "saved_at": int(time.time()),
            }
            # 分支元数据（用户提案 2026-10-08）：分支会话才写——load 时据此合成
            # 「主线前N行 + 分支行」完整事件流重放；主线/普通会话不写该字段（零侵入）。
            if self.branch_meta:
                data["branch"] = self.branch_meta
            # 原子写：先写 .tmp 再 os.replace，避免 autosave(daemon 线程) 与 load 并发时读到半个文件。
            # 落盘容错（night_tasks #1）：写失败仅告警——内存 session 仍是真相，autosave/主循环不炸
            try:
                tmp_path = path.with_suffix(path.suffix + ".tmp")
                tmp_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
                os.replace(tmp_path, path)
            except Exception as e:
                _LOG.warning("meta.json 落盘失败（内存继续，不阻塞）：%s", e)
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
        # 分支元数据（用户提案 2026-10-08）：meta 带 branch 字段的会话 = 从某主线某一轮分出的支线
        # （仅新文件夹结构支持——旧扁平迁移路径无主线可指）
        s.branch_meta = data.get("branch") if (isinstance(data.get("branch"), dict) and path.name == "meta.json") else None
        s._system_ledger = data.get("system_ledger") or {"last_text": "", "count": 0, "dirty": True}
        # 投影 profile 指纹对比（用户提案 2026-09-30·对称切模型重刷）：存档与当前 profile
        # 不一致（重启期间换过模型/能力位/窗口变）→ 账本置 dirty 归一化 + 冻结渲染等惰性态
        # 本就为空会按新 profile 重算——首次请求即当前 profile 全量定型。一致 → 延续账本
        # （同模型重启：新渲染==last_text → byte-stable，端点缓存 TTL 内命中——508 轮优化的前提）。
        try:
            _fp_now = s._profile_fingerprint()
            if data.get("profile_fp") and data.get("profile_fp") != _fp_now:
                s._system_ledger["dirty"] = True
                s._simple_pending = True   # 异 profile 重启：load 完成后走简化分层定型（用户提案 2026-10-02）
                _LOG.info("重启后 profile 变化（存档 %s → 当前 %s）——账本置 dirty，首次投影全量重刷",
                          data.get("profile_fp"), _fp_now)
        except Exception as e:
            _LOG.warning("profile 指纹对比失败（跳过）：%s", e)
        # tier_boundaries：存档优先（2026-09-30 回滚 recalc-only）；缺失/空（旧存档或首次）才
        # 按卫生性规则重算兜底。运行期演化的末端密集边界（压力毕业多重集）只有存档能保真。
        s._tier_boundaries = list(data.get("tier_boundaries") or [])
        # 折叠计划恢复（缓存稳定，用户裁定 2026-08-31）：重启后沿用旧折叠形态——历史段头部
        # （fc 摘要）与重启前逐字节一致，前缀缓存不断。曾因 fc 不持久化 + _plan_fold 未顶窗清零，
        # restart 后投影重算归零 → 历史段头部重排 → t506·s0 实测 12.7% 命中（300K tok 全价）。
        s._planned_fold = int(data.get("fold_count") or 0)
        s._last_fold_count = s._planned_fold
        s._sos_count = int(data.get("sos_count") or 0)      # sos 档恢复（用户提案 2026-10-02）
        s._sos_text = str(data.get("sos_summary") or "")
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
        # —— 分支基底链合成（用户提案 2026-10-08；v2 支持支线上再分叉：链式逐层收集）——
        # 在 events 判断之前（新建分支无自己的 events.jsonl，首次 load 也要有基底记忆）。
        # meta.branch 沿 branch_of 链逐层收集基底（根→叶）：主线层=主线 events 前N；支线层=支线
        # 自己 events 前M——拼接即完整记忆流，喂重放器（投影/tier/折叠引擎零感知）。
        # branch_of 支持 "<主线ts>"（一级）或 "<主线ts>/branches/<支线名>"（二级+，相对 sessions 根）。
        # 写侧句柄全绑分支目录（隔离）；每层指纹独立校验（漂移→警告仍合成）。
        base_events = []
        _lc_base = []
        _chain_dirs = []      # 根→叶各层目录（toollog/llm_calls/recaps 逐层合载）
        if s.branch_meta:
            # 分支 ToolLog 前缀化（用户提案 2026-10-09）：call_prefix 存在 → 重建带前缀的
            # ToolLog（分支自己的调用 id = m1000-c1…，前缀内续号；基底合载不顶 counter）。
            # 旧分支（meta 无 call_prefix）保持无前缀旧行为，完全兼容。
            _cpfx = (s.branch_meta or {}).get("call_prefix") or ""
            if _cpfx:
                _saved = s.toollog._data          # __init__ 后可能已载入的部分（不丢）
                s.toollog = ToolLog(prefix=_cpfx)
                s.toollog._data = _saved
            _chain_dirs, _layers = [], _branch_chain_bases(s.branch_meta, _sessions_root_of(sdir))
            # 失链自愈回填（2026-10-09）：branch_of 指向的目录已被改名（时间戳名→字母 id）时，
            # 解析层按 created_at 反查命中了新目录——把 meta.branch.branch_of 刷成现路径，
            # 下次 load 走直连（不再依赖每次反查）。失败只警告（运行期已按反查结果合成，不阻塞）。
            try:
                _bo = str((s.branch_meta or {}).get("branch_of") or "")
                if _bo and _layers:
                    _canon = _layers[-1][0].relative_to(_sessions_root_of(sdir)).as_posix()
                    if _canon != _bo:
                        _meta_p = sdir / "meta.json"
                        _m = json.loads(_meta_p.read_text(encoding="utf-8"))
                        _m.setdefault("branch", {})["branch_of"] = _canon
                        _meta_p.write_text(json.dumps(_m, ensure_ascii=False, indent=2),
                                           encoding="utf-8")
                        s.branch_meta = _m.get("branch") or s.branch_meta
                        _LOG.info("branch_of 失链自愈回填：%s → %s", _bo, _canon)
            except Exception as e:
                _LOG.warning("branch_of 回填失败（忽略，本次已按反查解析）：%s", e)
            for layer_dir, lay_ev, lay_hash in _layers:
                if lay_hash and _events_fingerprint(lay_ev) != lay_hash:
                    _LOG.warning("分支基底漂移：%s 前 %d 行指纹不符（该层可能被回溯重写）——继续合成，"
                                 "个别 call_id 可能解析不到", layer_dir.name, len(lay_ev))
                base_events.extend(lay_ev)
                _chain_dirs.append(layer_dir)
                # 各层 toollog 合载（不清空 dict，counter 全局续号）
                lay_tl = layer_dir / "toollog.jsonl"
                if lay_tl.exists():
                    s.toollog.load_from_jsonl(lay_tl)
                lay_lc = layer_dir / "llm_calls.jsonl"
                if lay_lc.exists():
                    s.llm_calls.load_from_jsonl(lay_lc)
                    _lc_base.extend(s.llm_calls.all_records())   # 逐层取走拼接（LLMCallLog 清空语义）
        if events_path.exists():
            # —— 新格式：重放事件流重建 turns（未完成 turn 进 turns，不丢弃）——
            s.turns = _replay_events(base_events + _read_events(events_path))
            if toollog_path.exists():
                s.toollog.load_from_jsonl(toollog_path)
            s.toollog.set_path(toollog_path)
            if llm_calls_path.exists():
                s.llm_calls.load_from_jsonl(llm_calls_path)
                if _lc_base:
                    s.llm_calls._records = _lc_base + s.llm_calls._records   # 基底链在前、分支在后
            s._bind_event_path(events_path)   # 绑定（缓冲为空，不覆盖已有）
            for _ld in _chain_dirs:
                s._load_recaps(_ld)             # 各层基底轮 recap（idx 与合成流对齐）
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
                    s._emit_event({"event": "snapshot", "sha": t.snapshot_sha,
                                   "git_head": getattr(t, "git_head", "") or ""})
                for step in t.steps:
                    s._emit_event({"event": "step", "reasoning": step.reasoning or "",
                                   "call_ids": [tc.call_id for tc in step.tool_calls],
                           "changes": [[tc.call_id, tc.changed] for tc in step.tool_calls if tc.changed]})
                s._emit_event({"event": "turn_end", "answer": t.answer,
                               "answer_reasoning": t.answer_reasoning, "summary": t.summary})
            s.turns = old_turns
        else:
            # 无自己的事件流（普通新会话 / 刚 /branch 创建的分支）——分支时基底链即全部记忆
            if base_events:
                s.turns = _replay_events(base_events)
                s.toollog.set_path(toollog_path)      # 绑分支路径（文件不存在则 flush 基底合载，自包含）
                s._bind_event_path(events_path)       # 绑 events 路径（分支首轮写直接落分支目录）
                for _ld in _chain_dirs:
                    s._load_recaps(_ld)                 # 各层基底轮 recap（链式，idx 对齐）
            else:
                s.turns = []
        s.llm_calls.set_path(llm_calls_path)  # 绑定 llm_calls（老存档无此文件则空建）
        s._summary_sig = ()  # 让首次 _refresh_summary_cache 重算
        # 陈旧 tier/fold 状态修剪（2026-10-09 分支失链实锤）：基底链断（branch_of 指向被改名的
        # 旧时间戳目录、解析失败降级为空）时 turns 会缩水，而存档里的 tier_boundaries/fold_count
        # 还是基底齐全时的值——边界(1430) ≥ 轮数(6) → 读档渲染 0 轮、前端"展开更早"永远切空且
        # 卡死在加载态。此处按实际轮数裁剪；失链自愈基底恢复后由 _plan_fold 正常重定。
        _nt = len(s.turns)
        if s._tier_boundaries and max(s._tier_boundaries) >= _nt:
            _kept = [b for b in s._tier_boundaries if 0 < b < _nt]
            _LOG.warning("tier_boundaries 越界修剪：%s → %s（turns=%d，疑似基底链断裂缩水）",
                         s._tier_boundaries, _kept, _nt)
            s._tier_boundaries = _kept
        if s._planned_fold > _nt:
            _LOG.warning("fold_count 越界修剪：%d → %d（turns=%d）", s._planned_fold, _nt, _nt)
            s._planned_fold = _nt
            s._last_fold_count = min(s._last_fold_count, _nt)
        if s._sos_count > _nt:
            s._sos_count = _nt
        # tier_boundaries 兜底（2026-09-30 回滚）：存档缺失/空（旧存档/异常）才按卫生性规则重算；
        # 正常路径存档优先——运行期演化的末端密集边界只有存档能保真（recalc-only 曾致折叠螺旋）
        if not s._tier_boundaries:
            s._tier_boundaries = s._recompute_tier_boundaries()
        if getattr(s, "_simple_pending", False):
            s._simple_pending = False
            try:
                s.apply_simple_tiering()   # 异 profile 重启：简化分层一次定型（用户提案 2026-10-02）
            except Exception as e:
                _LOG.warning("简化分层定型失败（忽略，走 _plan_fold 常规路径）：%s", e)
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

    def collect_generated_images(self, changed: list) -> None:
        """本轮新增图片 → 伪造 read_file 对暂存（2026-10-07 用户提案）：投影尾部附加
        「read_file 调用+结果(带 image_url)」——视觉模型"以为"自己调用过 read_file 看图，
        省一次真实调用；瞬态不落 event.jsonl/step 存档。上限：单图 ≤1.5MB、一次 ≤4 张。"""
        import base64, hashlib
        for rel in changed:
            try:
                p = Path(rel) if Path(rel).is_absolute() else (self.workspace / rel)
            except Exception:
                continue
            key = str(p).lower()
            if key in self._gen_images_done:
                continue
            if p.suffix.lower() not in _GEN_IMG_EXTS:
                continue
            try:
                if not p.is_file() or p.stat().st_size > 1_572_864:   # 1.5MB 上限
                    continue
                b64 = base64.b64encode(p.read_bytes()).decode("ascii")
            except OSError:
                continue
            mime = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
                    "webp": "image/webp", "gif": "image/gif"}.get(p.suffix.lower().lstrip("."), "image/png")
            self._gen_images_pending.append({
                "path": str(p),
                "call_id": "genimg_" + hashlib.md5(key.encode()).hexdigest()[:10],
                "data": f"data:{mime};base64,{b64}",
                "kb": p.stat().st_size // 1024,
            })
            self._gen_images_done.add(key)
            if len(self._gen_images_pending) >= 4:
                break

    def __repr__(self):
        return f"Session(name={self.name!r}, turns={len(self.turns)}, summary={'yes' if self.global_summary else 'no'})"


# 视觉 API 图片白名单（2026-09-29 用户实锤：VM 里贴 webp → 智谱 400 "Unsupported image format"）
# 智谱/多数 provider 只吃 PNG/JPEG；前端 FileReader.readAsDataURL 原样保留文件 mime（webp/gif/bmp/heic
# 全可能），后端 _user_content 此前原样透传 → 被拒。此处统一规范化。
_MAX_IMG_EDGE = 2048   # 与 real_tools._MAX_IMG_EDGE 同值（视觉 API 尺寸上限）


def _norm_img_data_url(url: str) -> str:
    """data URL 图片规范化：白名单（image/png、image/jpeg）且 ≤2048px → 原样返回；
    否则用 PIL 重编码为 PNG（GIF/多帧取首帧）并等比缩到限内。
    PIL 不可用 / 解析失败 / 非 data URL → 原样返回（尽力而为，不炸投影）。"""
    if not isinstance(url, str) or not url.startswith("data:image/"):
        return url
    try:
        head, _, b64 = url.partition(",")
        mime = head[5:].split(";")[0].lower()
        need_conv = mime not in ("image/png", "image/jpeg")
        raw = base64.b64decode(b64)
        from PIL import Image
        import io
        img = Image.open(io.BytesIO(raw))
        try:
            img.seek(0)   # GIF/多帧：首帧
        except Exception:
            pass
        w, h = img.size
        if not need_conv and w <= _MAX_IMG_EDGE and h <= _MAX_IMG_EDGE:
            return url
        if w > _MAX_IMG_EDGE or h > _MAX_IMG_EDGE:
            r = min(_MAX_IMG_EDGE / w, _MAX_IMG_EDGE / h)
            img = img.resize((max(1, int(w * r)), max(1, int(h * r))), Image.LANCZOS)
        if img.mode in ("P", "LA", "PA"):
            img = img.convert("RGBA")
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()
    except Exception:
        return url


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


def _events_fingerprint(events: list) -> str:
    """基底事件指纹（用户提案 2026-10-08 分支机制）：sha256(逐事件 sort_keys 规范序列化)[:16]。
    /branch 创建时对主线前 N 行算出写进 meta.branch.base_hash；Session.load 校验同一函数——
    主线被 rewind 重写后指纹漂移即告警（仍继续合成，记忆尽力保留）。"""
    h = hashlib.sha256()
    for e in events:
        h.update((json.dumps(e, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8"))
    return h.hexdigest()[:16]


def _resolve_base_dir(sessions_root: Path, bo: str) -> Optional[Path]:
    """基底层目录解析（2026-10-09 失链自愈）：bo 形如 "<主线id>" 或 "<主线id>/branches/<支线id>"。
    逐段解析：段目录存在→直用；不存在且形如旧时间戳目录名（YYYYMMDD_HHMMSS，letter-id
    改名前的形态——2026-10-09「目录名即 id」改造会改名既有目录，但 branch_of 字符串
    还指着旧名）→ 按 meta.created_at 反查现目录（目录名会改，创建时间不会）。
    解析失败返回 None（调用方降级为空基底，不断链）。"""
    cur = sessions_root
    for seg in [p for p in str(bo).replace("\\", "/").split("/") if p]:
        if seg == "branches":
            cur = cur / "branches"
            continue
        cand = cur / seg
        if cand.exists():
            cur = cand
            continue
        if re.fullmatch(r"\d{8}_\d{6}", seg):        # 旧时间戳目录名 → created_at 反查
            try:
                _cands = list(cur.iterdir())
            except Exception:
                return None
            for d in _cands:
                if not d.is_dir():
                    continue
                try:
                    m = json.loads((d / "meta.json").read_text(encoding="utf-8"))
                    if _timestamp_dir_name(float(m.get("created_at") or 0)) == seg:
                        cur = d
                        break
                except Exception:
                    continue
            else:
                return None
            continue
        return None
    return cur if cur != sessions_root else None


def _branch_chain_bases(root_meta: dict, sessions_root: Path) -> list:
    """分支基底链解析（2026-10-08 v2·用户提案：支线上再分叉）。
    沿 branch_of 链从叶到根逐层收集，返回根→叶有序 [(层目录, 该层events[:N], 该层base_hash)]。
    - branch_of 形如 "<主线ts>"（一级分支）或 "<主线ts>/branches/<支线名>"（二级+，相对 sessions 根）；
    - 每层的 inherit_lines 语义 = 该层基底文件自己的前 N 行（主线层=主线 events 前N；支线层=支线
      自己 events 前M——链式拼接即为完整记忆流）；
    - 某层缺失 → 该层降级为空（警告），不断链；
    - 环/超深（>8 层）截断。"""
    layers_rev = []
    cur = dict(root_meta or {})
    for _ in range(8):
        bo = str(cur.get("branch_of") or "").replace("\\", "/").strip("/")
        if not bo:
            break
        layer_dir = _resolve_base_dir(sessions_root, bo)
        if layer_dir is None:
            layer_dir = sessions_root / bo   # 保持旧警告路径（缺目录时给出可读位置）
        ev_p = layer_dir / "events.jsonl"
        n = int(cur.get("inherit_lines") or 0)
        if ev_p.exists() and n > 0:
            layers_rev.append((layer_dir, _read_events(ev_p)[:n], cur.get("base_hash") or ""))
        else:
            _LOG.warning("分支基底缺失 %s（inherit_lines=%d）——该层降级为空", ev_p, n)
            layers_rev.append((layer_dir, [], cur.get("base_hash") or ""))
        # 上一层：读该层 meta 的 branch 字段（顶层主线无 → 终止）
        try:
            cur = json.loads((layer_dir / "meta.json").read_text(encoding="utf-8")).get("branch") or {}
        except Exception:
            break
    layers_rev.reverse()
    return layers_rev


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
            cur.git_head = e.get("git_head", "") or ""   # rewind 撞车检测用（旧档无此字段→空串=不拦）
        elif et == "step" and cur is not None:
            _chm = dict(e.get("changes") or [])   # 快照 diff 恢复（有变更的调用——重放进 ToolCall.changed）
            cur.steps.append(Step(reasoning=e.get("reasoning", ""),
                                  tool_calls=[ToolCall(call_id=c, changed=_chm.get(c) or [])
                                    for c in e.get("call_ids", [])]))
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
                cur.changed = e.get("changed") or []   # 快照 diff 聚合（读档恢复——answer 补充渲染用）
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


def _find_branch_dir_by_name(workspace, name: str) -> Optional[Path]:
    """按分支名查找分支目录（2026-10-09 平铺化：分支与主线同级直接在 sessions/ 根下，
    分支关系由 meta.branch.branch_of 相对路径表达——定位不再关心嵌套）。
    支持形式：
      - 纯分支名——根下按 meta.name 全局搜（重名取最新创建）
      - '主线名/分支名'——主线段仅用于消歧的旧习惯写法，分支段仍是根下 meta.name 匹配
      - '主线id/分支id' 旧嵌套写法——兼容迁移前残留（branches/ 直查）
    找不到返回 None。"""
    repo_dir = _repo_sessions_dir(workspace)

    def _by_display_name(disp: str) -> Optional[Path]:
        """根下按 meta.name 全局搜（平铺分支天然被覆盖；重名取最新创建）。"""
        found, found_ct = None, 0
        try:
            it = repo_dir.iterdir()
        except Exception:
            return None
        for d in it:
            if not d.is_dir():
                continue
            mp = d / "meta.json"
            if not mp.exists():
                continue
            try:
                data = json.loads(mp.read_text(encoding="utf-8"))
            except Exception:
                continue
            if data.get("name") == disp and data.get("branch"):   # 必须确是分支
                ct = data.get("created_at") or 0
                if found is None or ct >= found_ct:
                    found, found_ct = d, ct
        return found

    if "/" in name:
        main_name, br_name = name.split("/", 1)
        main_name, br_name = main_name.strip(), br_name.strip()
        if not main_name or not br_name:
            return None
        # 分支段（平铺）：按 meta.name 根下全局搜
        hit = _by_display_name(br_name)
        if hit is not None:
            return hit
        # 旧嵌套残留兼容：主线/branches/分支（迁移前的目录）
        main_dir = _find_session_dir_by_name(workspace, main_name)
        if main_dir is None:
            m2 = repo_dir / main_name
            main_dir = m2 if (m2 / "meta.json").exists() else None
        if main_dir is not None:
            cand = main_dir / "branches" / br_name
            if (cand / "meta.json").exists():
                return cand
        return None
    # 纯分支名
    return _by_display_name(name)


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

    # 3.1 目录名直查（2026-10-09 letter-id 补）：新 session 目录名是字母 id（a/b/aa…），
    # name 只是 meta 显示名——/resume a 应直达 sessions/a/（实测曾 FileNotFoundError：
    # 目录名与显示名脱钩后此通路缺失）。只接受无分隔符的单段名（防路径穿越）。
    if path_or_name and not re.search(r"[\\/]", path_or_name) and ".." not in path_or_name:
        dir_cand = repo_dir / path_or_name
        if (dir_cand / "meta.json").exists():
            return dir_cand / "meta.json"

    # 3.5 分支（用户提案 2026-10-08）：branches/<名>/meta.json——'主线/分支' 精确形式或纯分支名
    br = _find_branch_dir_by_name(ws, path_or_name)
    if br:
        return br / "meta.json"

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

    # 新结构：扫描根下全部目录（主线 + 平铺分支，2026-10-09 平铺化：分支不再嵌 branches/）
    for ts_dir in repo_dir.iterdir():
        if not ts_dir.is_dir():
            continue
        meta_path = ts_dir / "meta.json"
        if not meta_path.exists():
            continue
        try:
            data = json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        # 轮数/首条：meta.turns（旧格式）或数 events.jsonl 的 turn_end（分支/新格式）
        turns_count = len(data.get("turns", []))
        first = ""
        if turns_count > 0 and "turns" in data:
            first = (data["turns"][0].get("user_message", "") or "")[:30]
        binfo = data.get("branch") or {}
        if not binfo:
            # 主线条目
            if not turns_count:
                try:
                    for line in (ts_dir / "events.jsonl").read_text(encoding="utf-8").splitlines():
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            e = json.loads(line)
                        except Exception:
                            continue
                        if e.get("event") == "turn_end":
                            turns_count += 1
                            if not first and e.get("answer"):
                                first = (e.get("answer") or "")[:30]
                        elif e.get("event") == "turn_start" and not first:
                            first = (e.get("user") or "")[:30]
                except Exception:
                    pass
            results.append({
                "id": ts_dir.name,
                "name": data.get("name") or ts_dir.name,
                "created_at": data.get("created_at"),
                "turns": turns_count,
                "first": first,
            })
            continue
        # 平铺分支条目：id = 单段目录名（/resume 直接用）；显示链 display_chain 优先
        b_turns, b_first = 0, ""
        try:
            for line in (ts_dir / "events.jsonl").read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    e = json.loads(line)
                except Exception:
                    continue
                if e.get("event") == "turn_end":
                    b_turns += 1
                    if not b_first and e.get("answer"):
                        b_first = (e.get("answer") or "")[:30]
                elif e.get("event") == "turn_start" and not b_first:
                    b_first = (e.get("user") or "")[:30]
        except Exception:
            pass
        # 显示名：display_chain = 父自己的链（一级分支=主线名，二级=主线⇢一级…）——
        # 拼 上 当前分支名；display_chain 缺失（旧存档）时退单名（2026-10-09 平铺化实锤：
        # 直接用 display_chain 会显示成父的名字，分支名丢失）
        _disp = (binfo.get("display_chain") or "").strip()
        _bname = data.get("name") or ts_dir.name
        b_name = f"{_disp} ⇢ {_bname}" if _disp and _disp != _bname else _bname
        results.append({
            "id": ts_dir.name,
            "name": b_name,
            "created_at": data.get("created_at"),
            "turns": b_turns,
            "first": b_first,
            "branch": True,   # 分支标记（前端样式区分；/resume 用 id 或 name 均可定位）
        })
        # 旧嵌套残留兼容（迁移前的主线）：branches/<名>/ 顺带列出
        broot = ts_dir / "branches"
        if not broot.is_dir():
            continue
        for bd in sorted(broot.iterdir()):
            if not bd.is_dir():
                continue
            bmp = bd / "meta.json"
            if not bmp.exists():
                continue
            try:
                bdata = json.loads(bmp.read_text(encoding="utf-8"))
            except Exception:
                continue
            b_turns, b_first = 0, ""
            try:
                for line in (bd / "events.jsonl").read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        e = json.loads(line)
                    except Exception:
                        continue
                    if e.get("event") == "turn_end":
                        b_turns += 1
                        if not b_first and e.get("answer"):
                            b_first = (e.get("answer") or "")[:30]
                    elif e.get("event") == "turn_start" and not b_first:
                        b_first = (e.get("user") or "")[:30]
            except Exception:
                pass
            results.append({
                "id": f"{ts_dir.name}/{bd.name}",   # 旧复合 id（/resume 兼容解析）
                "name": f"{(bdata.get('branch') or {}).get('display_chain') or (data.get('name') or ts_dir.name)} ⇢ {bdata.get('name') or bd.name}",
                "created_at": bdata.get("created_at"),
                "turns": b_turns,
                "first": b_first,
                "branch": True,
            })

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
