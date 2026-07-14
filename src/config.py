"""集中管理配置。模型来源是 models.py 里的 MODELS 字典（含 token，已 gitignore）。
.env 只保留 AgenTank 等非模型配置。
"""
import os
from pathlib import Path

from dotenv import load_dotenv

# 读与 config.py 同目录（项目根）的 .env，与启动 cwd 解耦——从任意目录启动都能拿到配置。
# override=True：.env 优先于系统同名环境变量。
load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=True)

# === 模型字典（来源 models.py）===
try:
    from models import MODELS, DEFAULT_MODEL
except ImportError as e:
    raise RuntimeError(
        "找不到 models.py。请复制 models.example.py 为 models.py 并填入 token。"
    ) from e


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

# === AgenTank 比赛配置 ===
AGT_BASE_URL = os.getenv("AGT_BASE_URL", "https://agentank.ai")
AGT_TANK_KEY = os.getenv("AGT_TANK_KEY") or os.getenv("AGT_AGENT_KEY")
AGT_NAME = os.getenv("AGT_NAME", "Qwen")  # 发布代码时的 submittedBy 徽章名

if not _active.get("api_token"):
    raise RuntimeError(f"默认模型 '{DEFAULT_MODEL}' 缺 api_token，请检查 models.py")
