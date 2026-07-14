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
    """保存模型配置到 ~/.agt/models.json（WebUI 用）。"""
    _AGT_DIR.mkdir(parents=True, exist_ok=True)
    data = {"models": models, "default": default_model or (list(models.keys())[0] if models else "")}
    _AGT_MODELS.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


# === 加载模型 ===
MODELS, DEFAULT_MODEL = _load_models()
if not MODELS:
    raise RuntimeError(
        "没有可用的模型配置。请在 WebUI 设置中添加模型，"
        "或复制 models.example.py 为 models.py 并填入 token。"
    )


def get_profile(name: str) -> dict:
    """按名字取模型 profile；未知名字抛 KeyError。"""
    if name not in MODELS:
        raise KeyError(f"未知模型 '{name}'，可用：{list(MODELS)}")
    return MODELS[name]


_active = get_profile(DEFAULT_MODEL)

# 向后兼容别名（step0_hello.py 等旧代码引用）—— 指向当前默认 profile
MODELSCOPE_BASE_URL = LLM_BASE_URL = _active["base_url"]
MODELSCOPE_API_KEY = LLM_API_KEY = _active["api_token"]
MODEL_NAME = LLM_MODEL = _active["model"]
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

# === AgenTank 比赛配置 ===
AGT_BASE_URL = os.getenv("AGT_BASE_URL", "https://agentank.ai")
AGT_TANK_KEY = os.getenv("AGT_TANK_KEY") or os.getenv("AGT_AGENT_KEY")
AGT_NAME = os.getenv("AGT_NAME", "Qwen")  # 发布代码时的 submittedBy 徽章名

if not _active.get("api_token"):
    print("⚠️ 默认模型缺 api_token。请在 WebUI 设置中完善模型配置。")
