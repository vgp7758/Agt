#!/usr/bin/env python3
"""
本地 LLM 推理测试脚本
- 读取 candidates_dict.json 和 prompt.txt
- 向 localhost:8080 发送流式请求
- 统计总耗时 & 首 token 延迟 (TTFT)
"""

import json
import time
import sys
from pathlib import Path

try:
    import requests
except ImportError:
    print("请先安装 requests: pip install requests")
    sys.exit(1)

# ==================== 配置 ====================
BASE_URL = "http://localhost:8080"
MODEL_NAME = None  # 若为 None，自动探测可用模型
TEMPERATURE = 0.1
MAX_TOKENS = 512

CANDIDATES_FILE = Path("candidates_dict.json")
PROMPT_FILE = Path("prompt.txt")

# ==================== 读取输入文件 ====================
def load_inputs():
    if not CANDIDATES_FILE.exists():
        print(f"[错误] 未找到 {CANDIDATES_FILE}")
        sys.exit(1)
    if not PROMPT_FILE.exists():
        print(f"[错误] 未找到 {PROMPT_FILE}")
        sys.exit(1)

    with open(CANDIDATES_FILE, "r", encoding="utf-8") as f:
        candidates_dict = json.load(f)

    with open(PROMPT_FILE, "r", encoding="utf-8") as f:
        prompt_text = f.read()

    return candidates_dict, prompt_text

# ==================== 构造消息 ====================
def build_messages(candidates_dict: dict, prompt_text: str):
    system_prompt = """你是历史检索精排器。根据用户消息，从候选历史记录里选出最相关的1-3条（多了冗余）。
候选以 {id: text} 字典提供，id 形如 cand_0、cand_1。

只输出一个JSON对象，不要markdown：
{"selected": ["cand_0", "cand_2"]}

规则：
- id 必须从候选字典的 key 中选取，不要编造
- 优先选：直接回应问题、包含关键信息、有可复用结论的
- 没把握时少选（1条也好），不要凑数"""

    user_content = f"""用户消息：
{prompt_text}

候选历史记录（id → 文本）：
{json.dumps(candidates_dict, ensure_ascii=False, indent=2)}"""

    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_content},
    ]

# ==================== 探测模型名 ====================
def detect_model():
    global MODEL_NAME
    if MODEL_NAME:
        return MODEL_NAME
    try:
        r = requests.get(f"{BASE_URL}/v1/models", timeout=5)
        r.raise_for_status()
        models = r.json().get("data", [])
        if models:
            MODEL_NAME = models[0]["id"]
            print(f"[信息] 自动探测到模型: {MODEL_NAME}")
            return MODEL_NAME
    except Exception as e:
        print(f"[警告] 探测模型失败: {e}")
    MODEL_NAME = "local-model"
    return MODEL_NAME

# ==================== 流式请求 & 计时 ====================
def stream_chat(messages):
    payload = {
        "model": detect_model(),
        "messages": messages,
        "temperature": TEMPERATURE,
        "max_tokens": MAX_TOKENS,
        "stream": True,
    }

    headers = {"Content-Type": "application/json"}

    print("=" * 60)
    print("开始请求...")
    print(f"模型: {MODEL_NAME}")
    print(f"候选数量: {len(messages[1]['content'])}")  # 仅参考
    print("-" * 60)

    start_time = time.perf_counter()
    first_token_time = None
    full_text = ""
    token_count = 0

    try:
        with requests.post(
            f"{BASE_URL}/v1/chat/completions",
            headers=headers,
            json=payload,
            stream=True,
            timeout=120,
        ) as resp:
            resp.raise_for_status()

            for line in resp.iter_lines(decode_unicode=True):
                if not line or not line.startswith("data: "):
                    continue
                data = line[6:]
                if data == "[DONE]":
                    break

                try:
                    chunk = json.loads(data)
                except json.JSONDecodeError:
                    continue

                delta = chunk.get("choices", [{}])[0].get("delta", {})
                content = delta.get("content", "")
                if content:
                    if first_token_time is None:
                        first_token_time = time.perf_counter()
                    full_text += content
                    token_count += 1
                    print(content, end="", flush=True)

    except requests.exceptions.ConnectionError:
        print(f"\n[错误] 无法连接到 {BASE_URL}，请确认服务已启动")
        sys.exit(1)
    except Exception as e:
        print(f"\n[错误] 请求异常: {e}")
        sys.exit(1)

    end_time = time.perf_counter()

    # 统计
    total_time = end_time - start_time
    ttft = (first_token_time - start_time) if first_token_time else 0.0
    tps = token_count / total_time if total_time > 0 else 0

    print("\n")
    print("=" * 60)
    print("📊 性能统计")
    print("-" * 60)
    print(f"  总耗时 (Total)     : {total_time:.3f} s")
    print(f"  首 Token 延迟(TTFT): {ttft:.3f} s")
    print(f"  生成 Token 数      : {token_count}")
    print(f"  吞吐 (TPS)         : {tps:.2f} tokens/s")
    print("=" * 60)

    return full_text

# ==================== 主函数 ====================
def main():
    candidates_dict, prompt_text = load_inputs()
    messages = build_messages(candidates_dict, prompt_text)
    result = stream_chat(messages)

    # 尝试解析 JSON 输出
    print("\n📦 解析结果:")
    try:
        # 尝试从文本中提取 JSON
        text = result.strip()
        if text.startswith("```json"):
            text = text[7:]
        if text.startswith("```"):
            text = text[3:]
        if text.endswith("```"):
            text = text[:-3]
        parsed = json.loads(text.strip())
        print(json.dumps(parsed, ensure_ascii=False, indent=2))
    except Exception as e:
        print(f"无法解析为 JSON: {e}")
        print("原始输出:")
        print(result)

if __name__ == "__main__":
    main()
