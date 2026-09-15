"""一次性验证：当前配置的模型 provider 是否支持前缀缓存（prompt caching）。

发两次【相同大前缀 + 不同小尾巴】的请求：
  - 第 1 次：冷启，应无命中（写入缓存）
  - 第 2 次：前缀应命中 → usage 里出现 cached/hit tokens
不同 provider 把命中数放在不同字段，这里把 usage 全字段打出来 + 重点扫几个常见键。

用法： python verify_prefix_cache.py [模型名]   （默认 config.DEFAULT_MODEL）
跑完可删。
"""
import sys, json, time
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from openai import OpenAI
import config

model_name = sys.argv[1] if len(sys.argv) > 1 else config.DEFAULT_MODEL
profile = config.get_profile(model_name)
client = OpenAI(base_url=profile["base_url"], api_key=(profile.get("api_tokens") or [""])[0])
model = profile["model"]

# 稳定前缀：一段会被两次请求原样复用的长文本（>1024 token 起步缓存阈值）
PREFIX = "前缀缓存测试：这是一段会被反复发送的稳定上下文，用于验证 provider 是否缓存了它。" * 250

CACHE_KEYS = ("prompt_cache_hit_tokens", "prompt_cache_miss_tokens",
              "cached_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")


def cached_of(d: dict) -> int:
    for k in ("prompt_cache_hit_tokens", "cached_tokens", "cache_read_input_tokens"):
        if d.get(k):
            return d[k]
    ptd = d.get("prompt_tokens_details") or {}
    if isinstance(ptd, dict) and ptd.get("cached_tokens"):
        return ptd["cached_tokens"]
    return 0


def call(tag: str, tail: str) -> dict:
    resp = client.chat.completions.create(
        model=model,
        messages=[{"role": "system", "content": "你是测试助手，简短回答。"},
                  {"role": "user", "content": PREFIX + "\n\n" + tail}],
        max_tokens=8,
        extra_body={"enable_thinking": False} if profile.get("thinking") else {},
    )
    u = resp.usage
    d = u.model_dump() if hasattr(u, "model_dump") else dict(u)
    hits = {k: v for k, v in d.items() if k in CACHE_KEYS}
    ptd = d.get("prompt_tokens_details") or {}
    if isinstance(ptd, dict) and ptd.get("cached_tokens"):
        hits["prompt_tokens_details.cached_tokens"] = ptd["cached_tokens"]
    print(f"\n[{tag}]")
    print(f"  usage 全字段: {json.dumps(d, ensure_ascii=False)}")
    print(f"  缓存相关: {hits or '（无）'}")
    return d


print(f"=== 前缀缓存验证 ===")
print(f"provider={profile['base_url']}\nmodel   ={model}\n前缀≈{len(PREFIX)} 字符")

try:
    call("第1次·冷启", "问题A：1+1 等于几？")
    time.sleep(2)
    d2 = call("第2次·应命中", "问题B：2+2 等于几？")
except Exception as e:
    print(f"\n❌ 调用失败：{type(e).__name__}: {e}")
    sys.exit(1)

print("\n" + "=" * 52)
c = cached_of(d2)
if c:
    print(f"✅ 检测到前缀缓存命中（第2次 cached≈{c} tokens）")
    print("   → provider 支持自动前缀缓存；分档投影的冻结历史段会被命中（0.1x）。")
else:
    print("❌ 第2次未检测到缓存命中字段。")
    print("   → 可能不支持自动前缀缓存，或命中数放在别的字段名下（看上面 usage 全字段确认）。")
