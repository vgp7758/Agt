"""集中管理配置。模型来源优先级：
  1. ~/.agt/models.json（WebUI 可编辑的用户配置）
  2. models.py（项目根，向后兼容，含 token，已 gitignore）
.env 只保留 AgenTank 等非模型配置。
"""
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

# 读与 config.py 同目录（项目根）的 .env，与启动 cwd 解耦——从任意目录启动都能拿到配置。
load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=True)

# models.py 在项目根(含 token, gitignored)，确保根目录在 sys.path
_root = Path(__file__).resolve().parent.parent
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

# === 用户配置目录 ===
_AGT_DIR = Path.home() / ".agt"
_AGT_MODELS = _AGT_DIR / "models.json"


def _load_models() -> tuple[dict, str]:
    """加载模型字典：优先 ~/.agt/models.json，其次 models.py。返回 (MODELS, DEFAULT_MODEL)。"""
    # 1) ~/.agt/models.json
    if _AGT_MODELS.exists():
        try:
            data = json.loads(_AGT_MODELS.read_text(encoding="utf-8"))
            models = data.get("models", {})
            default = data.get("default", list(models.keys())[0] if models else "glm")
            if models:
                return models, default
        except Exception:
            pass
    # 2) models.py 兜底
    try:
        from models import MODELS, DEFAULT_MODEL
        return MODELS, DEFAULT_MODEL
    except ImportError:
        pass
    # 3) 如果都没有——返回空，运行时 WebUI 可添加
    return {}, ""


def save_user_models(models: dict, default_model: str = ""):
    """保存模型配置到 ~/.agt/models.json（WebUI 用）。写盘后自动 reload_models()——当前进程内存立即生效。"""
    _AGT_DIR.mkdir(parents=True, exist_ok=True)
    data = {"models": models, "default": default_model or (list(models.keys())[0] if models else "")}
    _AGT_MODELS.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    reload_models()


def reload_models():
    """重读 ~/.agt/models.json → 刷新模块级 MODELS/DEFAULT_MODEL 及兼容别名。
    WebUI 保存模型配置后自动调用；/reload models 手动触发（改 models.py 后也用它）。
    注意：LLMClient 实例的 profile 是创建时固化的——reload 后需重建实例才用新配置
    （主 Agent 的 self.llm 由调用方按需 switch_model；utility_client 由 /config utility_model 清缓存）。"""
    global MODELS, DEFAULT_MODEL, _NO_MODELS, _active
    MODELS, DEFAULT_MODEL = _load_models()
    _NO_MODELS = not MODELS
    _active = get_profile(DEFAULT_MODEL) if not _NO_MODELS else {}
    # 兼容别名同步刷新
    global MODELSCOPE_BASE_URL, LLM_BASE_URL, MODELSCOPE_API_KEY, LLM_API_KEY
    global MODEL_NAME, LLM_MODEL, LLM_THINKING_SUPPORTED
    MODELSCOPE_BASE_URL = LLM_BASE_URL = _active.get("base_url", "")
    MODELSCOPE_API_KEY = LLM_API_KEY = (_active.get("api_tokens") or [""])[0]
    MODEL_NAME = LLM_MODEL = _active.get("model", "")
    LLM_THINKING_SUPPORTED = _active.get("thinking", False)


# === 加载模型 ===
# 允许空启动：首次安装时用户尚无任何模型配置，此时不报错——让 WebUI 能起来，
# 由用户在设置页添加第一个模型。真正发 LLM 请求时若仍无模型，LLMClient 会给友好提示。
MODELS, DEFAULT_MODEL = _load_models()
_NO_MODELS = not MODELS
if _NO_MODELS:
    print("⚠️ 尚未配置任何模型。请运行 agt-web，在浏览器「设置」页添加第一个模型，"
          "或在 site-packages/src/ 下复制 models.example.py 为 models.py 并填入 token。")


def get_profile(name: str) -> dict:
    """按名字取模型 profile；未知名字抛 KeyError。
    api_token 统一为 list（支持多账号轮流）。"""
    if name not in MODELS:
        raise KeyError(f"未知模型 '{name}'，可用：{list(MODELS)}")
    p = dict(MODELS[name])
    tok = p.get("api_token", "")
    if isinstance(tok, str):
        # 支持逗号分隔的多 token 字符串（直接编辑 models.json 时的写法）
        p["api_tokens"] = [t.strip() for t in tok.split(",") if t.strip()]
    elif isinstance(tok, list):
        p["api_tokens"] = tok
    else:
        p["api_tokens"] = [str(tok)]
    return p


# 空配置兜底：无模型时不调 get_profile（会 KeyError），给空 profile 让兼容别名有值。
_active = get_profile(DEFAULT_MODEL) if not _NO_MODELS else {}


# 向后兼容别名（step0_hello.py 等旧代码引用）—— 指向当前默认 profile；无模型时空值
MODELSCOPE_BASE_URL = LLM_BASE_URL = _active.get("base_url", "")
MODELSCOPE_API_KEY = LLM_API_KEY = (_active.get("api_tokens") or [""])[0]
MODEL_NAME = LLM_MODEL = _active.get("model", "")
LLM_THINKING_SUPPORTED = _active.get("thinking", False)

# === 运行时设置持久化 ===
_AGT_SETTINGS = _AGT_DIR / "settings.json"

def load_runtime_settings() -> dict:
    """从 ~/.agt/settings.json 加载运行时设置。"""
    if _AGT_SETTINGS.exists():
        try:
            return json.loads(_AGT_SETTINGS.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}

def save_runtime_settings(settings: dict):
    """写入运行时设置到 ~/.agt/settings.json。"""
    _AGT_DIR.mkdir(parents=True, exist_ok=True)
    _AGT_SETTINGS.write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8")


def load_max_level() -> int:
    """全局最大分档级别（~/.agt/settings.json 的 max_level；默认 4，<1 视为 4）。
    分档投影里超过此级别的老档不再顺移压缩（保住前缀缓存 + 防远古信息被压没）。"""
    try:
        ml = int(load_runtime_settings().get("max_level", 4))
    except Exception:
        ml = 4
    return ml if ml >= 1 else 4


def get_utility_model() -> str:
    """统一的辅助模型（settings.json 的 utility_model；空 = 跟随主模型）。
    所有 LLM 短调用场景共用：recap 总结 / RAG 检索抽关键字精排 / reasoning 补全默认 /
    工作流 LLM 节点与意图识别节点的默认模型。建议配便宜非思考模型。
    兼容旧字段：retrieval_model / recap_model（已弃用，读取时作 fallback）。"""
    try:
        rt = load_runtime_settings()
        m = (rt.get("utility_model", "") or "").strip()
        if not m:
            m = (rt.get("retrieval_model", "") or "").strip()   # 旧字段兼容
        if not m:
            m = (rt.get("recap_model", "") or "").strip()       # 旧字段兼容
    except Exception:
        m = ""
    return m


def get_retrieval_model() -> str:
    """[兼容别名] = get_utility_model()，兜底 DEFAULT_MODEL。旧调用点用。"""
    return get_utility_model() or DEFAULT_MODEL


def load_detail_base() -> int:
    """步距衰减的初始摘要字数（settings.json 的 detail_base；默认 1500）。"""
    try:
        return int(load_runtime_settings().get("detail_base", 1500))
    except Exception:
        return 1500


def load_panic_window() -> int:
    """轮内保命阀阈值（settings.json 的 panic_context_window；0=跟随 max_effective_context_window）。
    独立于分档窗口：分档窗口常设为总窗口 ~50%（_plan_fold 折到 75%×它），保命线可设为
    总窗口 ~80%——轮内投影在 75%×win ~ panic 之间纯追加零调整（缓存最优），超 panic 才应急。"""
    try:
        return int(load_runtime_settings().get("panic_context_window", 0) or 0)
    except Exception:
        return 0


def load_detail_step() -> int:
    """步距衰减的每步减少字数（settings.json 的 detail_step；默认 15）。"""
    try:
        return int(load_runtime_settings().get("detail_step", 15))
    except Exception:
        return 15


# === RAG 配置持久化（全局 embed + per-repo 索引策略） ===
# 全局：~/.agt/rag.json 只存 embed 相关（provider/model_path/api_*），供所有 repo 共用。
# Per-repo：~/.agt/repos/<hash>/rag.json 存 enabled/docs_dir/exts/top_k 等索引策略 +
#   session_vec 开关等 per-repo 字段。embed 字段在 load 时从全局 merge 补全，
#   在 save 时自动提升到全局——WebUI 侧只需一张表单。
#   旧存量 repo 级 embed 字段首次 load 时自动迁移到全局。

_GLOBAL_EMBED_KEYS = frozenset({
    "embed_provider", "embed_model_path",
    "embed_api_url", "embed_api_token", "embed_api_model", "embed_api_dim",
})

# 拼 DEFAULT_RAG_CONFIG 时仅保 per-repo 字段；embed 由 merge 补
_REPO_RAG_KEYS = frozenset({
    "enabled", "docs_dir", "exts", "exclude_globs", "index_dir",
    "vector_store_type", "top_k", "reranker_enabled", "reranker_path",
    "rerank_pool", "lines_per", "overlap", "batch",
    "session_index_enabled", "session_search_top_k",
})

DEFAULT_RAG_CONFIG = {
    "embed_provider": "local",      # global
    "embed_model_path": "",         # global
    "embed_api_url": "",            # global
    "embed_api_token": "",          # global
    "embed_api_model": "",          # global
    "embed_api_dim": 0,             # global
    "enabled": False,
    "docs_dir": "",
    "exts": [".md", ".txt", ".json"],
    "exclude_globs": ["*_Audit.*"],
    "index_dir": "",
    "vector_store_type": "faiss_hnsw",
    "top_k": 5,
    "reranker_enabled": False,
    "reranker_path": "",
    "rerank_pool": 0,
    "lines_per": 60,
    "overlap": 15,
    "batch": 32,
    "session_index_enabled": False,
    "session_search_top_k": 5,
}

# 全局 embed 配置路径
_GLOBAL_RAG_PATH = _AGT_DIR / "rag.json"


def _rag_config_path(workspace) -> Path:
    """RAG 配置 per-repo 存用户目录：~/.agt/repos/<fixed-cwd>/rag.json（与 sessions 同根，不污染项目仓库）。"""
    from session import REPOS_DIR, _repo_key   # 局部 import 避免循环
    return REPOS_DIR / _repo_key(workspace) / "rag.json"


def load_global_rag_config() -> dict:
    """加载全局 embed 配置 (~/.agt/rag.json)。不存在返回默认（只含 embed 字段）。"""
    if _GLOBAL_RAG_PATH.exists():
        try:
            data = json.loads(_GLOBAL_RAG_PATH.read_text(encoding="utf-8"))
            out = {}
            for k in _GLOBAL_EMBED_KEYS:
                out[k] = data.get(k, DEFAULT_RAG_CONFIG[k])
            return out
        except Exception:
            pass
    return {k: DEFAULT_RAG_CONFIG[k] for k in _GLOBAL_EMBED_KEYS}


def save_global_rag_config(cfg: dict):
    """写入全局 embed 配置到 ~/.agt/rag.json（只写 embed 字段）。"""
    _AGT_DIR.mkdir(parents=True, exist_ok=True)
    data = {}
    # 先读已有（保留非 embed 的杂项字段，虽然正常情况下只有 embed）
    if _GLOBAL_RAG_PATH.exists():
        try:
            data = json.loads(_GLOBAL_RAG_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    for k in _GLOBAL_EMBED_KEYS:
        if k in cfg:
            data[k] = cfg[k]
    _GLOBAL_RAG_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _maybe_migrate_embed_to_global(workspace: Path | str):
    """如果全局尚无 embed 配置，但从 repo 或任意存量里能挖到，自动迁移。

    逻辑：遍历 ~/.agt/repos/*/rag.json，找到第一个有 embed 字段的 → 提到全局。
    只做一次（全局已有则跳过）。此函数在每次 load_rag_config 时调用，开销 = 全局文件
    存在性检查（几乎零成本）。"""
    try:
        if _GLOBAL_RAG_PATH.exists():
            return   # 已迁移过
        from session import REPOS_DIR   # _repo_hash 不再需要——_rag_config_path 已用 _repo_key
        ws = Path(workspace) if isinstance(workspace, str) else workspace
        # 先试当前 workspace 的 repo config
        repo_p = _rag_config_path(ws)
        for candidate in (repo_p,):
            if not candidate.exists():
                continue
            try:
                data = json.loads(candidate.read_text(encoding="utf-8"))
            except Exception:
                continue
            embed_section = {k: data[k] for k in _GLOBAL_EMBED_KEYS & data.keys()
                             if data.get(k)}
            if embed_section:
                _AGT_DIR.mkdir(parents=True, exist_ok=True)
                _GLOBAL_RAG_PATH.write_text(
                    json.dumps(embed_section, ensure_ascii=False, indent=2), encoding="utf-8")
                # 从 repo 里剥掉 embed 字段并重写
                stripped = {k: v for k, v in data.items() if k not in _GLOBAL_EMBED_KEYS}
                candidate.write_text(json.dumps(stripped, ensure_ascii=False, indent=2),
                                     encoding="utf-8")
                return
        # 兜底：扫描所有 repo 目录找 embed（仅当前 candidate 为空时）
        if REPOS_DIR.exists():
            for repo_dir in REPOS_DIR.iterdir():
                rcf = repo_dir / "rag.json"
                if not rcf.exists() or rcf == repo_p:
                    continue
                try:
                    data = json.loads(rcf.read_text(encoding="utf-8"))
                except Exception:
                    continue
                embed_section = {k: data[k] for k in _GLOBAL_EMBED_KEYS & data.keys()
                                 if data.get(k)}
                if embed_section:
                    _AGT_DIR.mkdir(parents=True, exist_ok=True)
                    _GLOBAL_RAG_PATH.write_text(
                        json.dumps(embed_section, ensure_ascii=False, indent=2),
                        encoding="utf-8")
                    stripped = {k: v for k, v in data.items() if k not in _GLOBAL_EMBED_KEYS}
                    rcf.write_text(json.dumps(stripped, ensure_ascii=False, indent=2),
                                   encoding="utf-8")
                    return
    except Exception:
        pass   # 迁移失败不阻断主流程


def load_rag_config(workspace) -> dict:
    """加载 RAG 配置（repo 级 merge 全局 embed）。repo 字段优先，空的 embed 字段从全局补全。

    首次调用时自动迁移旧 repo 级 embed → 全局（_maybe_migrate_embed_to_global）。"""
    _maybe_migrate_embed_to_global(workspace)
    cfg = dict(DEFAULT_RAG_CONFIG)              # 全字段默认
    # 合并全局 embed
    gcfg = load_global_rag_config()
    for k in _GLOBAL_EMBED_KEYS:
        cfg[k] = gcfg.get(k, cfg[k])
    # 合并 repo 字段
    p = _rag_config_path(workspace)
    if p.exists():
        try:
            repo = json.loads(p.read_text(encoding="utf-8"))
            cfg.update(repo)   # repo 字段优先（覆盖默认，也覆盖全局 embed——若 repo 里碰巧有旧 embed 残留）
        except Exception:
            pass
    return cfg


def save_rag_config(workspace, cfg: dict):
    """保存 RAG 配置：embed 字段自动提升到全局 (~/.agt/rag.json)，
    其余存入 repo (~/.agt/repos/<hash>/rag.json)。WebUI 无需感知分层。"""
    # 1) 全局：只写 embed 字段
    embed_section = {k: cfg[k] for k in _GLOBAL_EMBED_KEYS if k in cfg}
    save_global_rag_config(embed_section)
    # 2) Repo：只写 per-repo 字段（不写 embed）
    repo_section = {k: v for k, v in cfg.items()
                    if k in _REPO_RAG_KEYS}
    p = _rag_config_path(workspace)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(repo_section, ensure_ascii=False, indent=2), encoding="utf-8")


def seed_rag_config(workspace) -> bool:
    """首次播种：seed 全局 embed + repo 配置。返回是否新建 repo 配置。"""
    # seed 全局 embed（如果不存在）
    _AGT_DIR.mkdir(parents=True, exist_ok=True)
    if not _GLOBAL_RAG_PATH.exists():
        save_global_rag_config({k: DEFAULT_RAG_CONFIG[k] for k in _GLOBAL_EMBED_KEYS})
    # seed repo 配置
    p = _rag_config_path(workspace)
    if p.exists():
        return False
    p.parent.mkdir(parents=True, exist_ok=True)
    legacy = Path(workspace) / ".agent" / "rag.json"
    if legacy.exists():
        import shutil
        shutil.copy2(legacy, p)
        # 旧项目里的 embed 字段提到全局
        _maybe_migrate_embed_to_global(workspace)
        return True
    repo_default = {k: v for k, v in DEFAULT_RAG_CONFIG.items() if k in _REPO_RAG_KEYS}
    p.write_text(json.dumps(repo_default, ensure_ascii=False, indent=2), encoding="utf-8")
    return True


# === AgenTank 比赛配置 ===
AGT_BASE_URL = os.getenv("AGT_BASE_URL", "https://agentank.ai")
AGT_TANK_KEY = os.getenv("AGT_TANK_KEY") or os.getenv("AGT_AGENT_KEY")
AGT_NAME = os.getenv("AGT_NAME", "Qwen")  # 发布代码时的 submittedBy 徽章名

if not _active.get("api_tokens"):
    if not _NO_MODELS:
        print("⚠️ 默认模型缺 api_token。请在 WebUI 设置中完善模型配置。")
