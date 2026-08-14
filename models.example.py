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
        "vision": False,
        # DeepSeek【思考模型】(如 deepseek-reasoner/v4-pro thinking 模式)要求历史中带
        # tool_calls 的 assistant 消息必须有 reasoning_content 字段；跨模型混用历史缺该字段时 400。
        # 设 true 后发请求前自动给缺字段的消息补空串占位。非思考模型保持 False 即可。
        "requires_reasoning_in_history": False,
    },
    "qwen": {
        "base_url": "https://api-inference.modelscope.cn/v1",
        "api_token": "ms-你的-modelscope-token",
        "model": "Qwen/Qwen3.5-397B-A17B",
        "desc": "ModelScope Qwen3.5-397B 推理模型（视觉）",
        "thinking": True,
        "vision": True,
    },
}

DEFAULT_MODEL = "deepseek"
