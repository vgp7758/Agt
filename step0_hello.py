"""Step 0 —— 最小可用版本：一次对话调用。

目标：确认三件事都通——
  1. .env 里的 token 能被读到；
  2. base_url + /v1 正确；
  3. model 名 `Qwen/Qwen3.5-397B-A17B` 能被服务端识别。

跑法：python step0_hello.py
"""
from openai import OpenAI

import config


def main():
    # 用官方 openai SDK，只是把 base_url 指向 ModelScope。
    client = OpenAI(
        base_url=config.MODELSCOPE_BASE_URL,
        api_key=config.MODELSCOPE_API_KEY,
    )

    print(f"正在调用模型 [{config.MODEL_NAME}] ...\n")

    response = client.chat.completions.create(
        model=config.MODEL_NAME,
        messages=[
            {"role": "system", "content": "你是一个简洁的助手。"},
            {"role": "user", "content": "用一句话介绍你自己，并告诉我你是哪个模型。"},
        ],
    )

    print("=== 模型回复 ===")
    print(response.choices[0].message.content)
    print("\n=== 调用元信息 ===")
    print(f"服务端返回的 model 字段: {response.model}")
    print(f"token 用量: {response.usage}")
    print(f"finish_reason: {response.choices[0].finish_reason}")


if __name__ == "__main__":
    main()
