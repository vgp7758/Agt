"""模型字典模板 —— 复制本文件为 models.py 并填入你的真实 token。

  cp models.example.py models.py   (Windows: copy models.example.py models.py)

字段说明见 models.py。models.py 已被 gitignore，不会提交。
"""

MODELS = {
    "deepseek": {
        "base_url": "https://api.deepseek.com",
        "api_token": "sk-你的-deepseek-key",
        "model": "deepseek-v4-flash",
        "desc": "DeepSeek（备用）",
        "thinking": False,
    },
    "qwen": {
        "base_url": "https://api-inference.modelscope.cn/v1",
        "api_token": "ms-你的-modelscope-token",
        "model": "Qwen/Qwen3.5-397B-A17B",
        "desc": "ModelScope Qwen3.5-397B 推理模型",
        "thinking": True,
    },
}

DEFAULT_MODEL = "deepseek"
