"""模型字典模板 —— 复制本文件为 models.py 并填入你的真实 token。

  cp models.example.py models.py   (Windows: copy models.example.py models.py)

字段说明见 models.py。models.py 已被 gitignore，不会提交。
"""

MODELS = {
    # ⚠️ prompt cache 按 api_token 隔离的 provider（GLM/bigmodel 等）：
    # 同一 token 交错用于【react 长上下文调用】和【utility 短调用】（recap/RAG检索/工作流LLM）
    # 会互相驱逐对方缓存 → 命中率清零。对策：utility 用独立条目 + 独立 api_token（见 glm-utility），
    # 再 /config utility_model glm-utility。同理条目内配多 token 时请勿依赖轮换分摊——
    # agt 已改为 sticky（限流才换 token），主动轮换=自己交错驱逐自己的缓存。
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
    # utility 专用条目示例：与主模型同 provider 也【必须用不同 api_token】（缓存按 token 隔离，
    # 同 token 长短调用交错互相驱逐缓存）。去 provider 后台再申请一个 key 填这里，
    # 然后 /config utility_model glm-utility 生效。
    # "glm-utility": {
    #     "base_url": "https://open.bigmodel.cn/api/coding/paas/v4",
    #     "api_token": "另一个-key（不要与主条目共用）",
    #     "model": "glm-5.3",
    #     "desc": "utility 短调用专用（recap/RAG检索/工作流LLM），独立 token 保主链缓存命中",
    #     "thinking": False,
    #     "vision": False,
    # },
}

DEFAULT_MODEL = "deepseek"
