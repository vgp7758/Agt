"""DeepSeek v4 前缀缓存上限探针（2026-08-29）。

背景：comfy session t253 实测 v4-flash 220k 级 prompt 命中 2-3%，且全 session 所有
命中值（8320/14080/11776/8064…）均 ≤14080<16384、全为 64 的倍数。v3 时代同投影 99%。
假说：v4 后端的前缀缓存写入/查询存在 ~14-16k token 的上限，超出部分形同虚设。

判别：
  阶梯长度 payload（单条 user 消息）各连发两次（字节级相同）——
    第二次命中≈全量        → 该长度缓存正常
    第二次命中封顶在 ~14k   → 实锤上限（转折档位即上限所在）
    第二次命中 0            → 该长度根本不写缓存
  命中 <90% 的档 20 秒后自动补发第三次（排除写入异步延迟）。
  末组 H 用多消息形态（1 条大 system + 多轮 user/assistant）复测转折档——排除
  "单条超长消息"与"多消息拼接"的形态差异。

跑法：python tools/deepseek_v4_cache_limit_probe.py（花费约 30-40 万输入 token）
"""
import json, pathlib, time, sys, urllib.request

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

CFG = json.loads((pathlib.Path.home() / ".agt" / "models.json").read_text(encoding="utf-8"))["models"]
PROFILE = CFG["deepseek"]
URL = PROFILE["base_url"].rstrip("/") + "/chat/completions"
TOKEN = PROFILE["api_token"][0] if isinstance(PROFILE["api_token"], list) else PROFILE["api_token"]
MODEL = PROFILE["model"]

def call(messages, label):
    body = json.dumps({"model": MODEL, "messages": messages, "max_tokens": 16,
                       "stream": False, "temperature": 0.2}).encode()
    req = urllib.request.Request(URL, data=body, headers={
        "Content-Type": "application/json", "Authorization": f"Bearer {TOKEN}"})
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            u = json.loads(r.read())["usage"]
    except Exception as e:
        print(f"  [{label}] ERR {e}")
        return None
    pt = u.get("prompt_tokens", 0)
    det = u.get("prompt_tokens_details") or {}
    cached = (det.get("cached_tokens") or 0) if isinstance(det, dict) else 0
    hit = u.get("prompt_cache_hit_tokens") or 0
    cached = max(cached, hit)
    print(f"  [{label}] prompt={pt:>8,} cached={cached:>8,} 命中={cached*100//max(pt,1):>3}% ({time.time()-t0:.1f}s)")
    return cached, pt

# 中文填充：DeepSeek tokenizer 约 1 token ≈ 1.6 汉字，按目标 token 数 ×1.6 造字符
BASE = "前缀缓存上限探针的确定性中文填充句子，用于把请求撑到目标长度并保持字节级稳定。"

def make_single(idx, target_tokens):
    """档 idx 的唯一 payload：单条 user 消息（档间内容不同——防跨档命中干扰）。"""
    fill = (f"【档{idx}】" + chr(0x4E00 + idx) * 13 + BASE) * (int(target_tokens * 1.6) // len(BASE) + 1)
    return [{"role": "user", "content": f"【档{idx}·独有前缀】\n{fill}\n请只回复：OK"}]

def make_multi(idx, target_tokens):
    """多消息形态：1 条大 system（占 ~85%）+ 交替 user/assistant（模拟 agent 投影）。"""
    big = ("【系统】" + chr(0x4E00 + idx) * 11 + BASE) * (int(target_tokens * 1.6 * 0.85) // len(BASE) + 1)
    msgs = [{"role": "system", "content": big}]
    tail = BASE * 4
    msgs += [{"role": "user", "content": f"问题{idx}-1：{tail}"},
             {"role": "assistant", "content": f"回答{idx}-1：{tail}"},
             {"role": "user", "content": f"问题{idx}-2：{tail}"},
             {"role": "user", "content": f"【档{idx}H】当前问题：请只回复 OK"}]
    return msgs

def probe(label, messages):
    print(f"\n[{label}] msgs={len(messages)}")
    r1 = call(messages, f"{label}-1(冷)")
    time.sleep(3)
    r2 = call(messages, f"{label}-2(热)")
    if r2 and r2[0] * 100 < 90 * max(r2[1], 1):
        time.sleep(20)
        call(messages, f"{label}-3(补发验写入延迟)")

print(f"=== DeepSeek v4 缓存上限探针 model={MODEL} ===")
# 阶梯：6k 起步到 48k；转折预期在 12k-24k 之间
for idx, tt in [(1, 6_000), (2, 12_000), (3, 16_000), (4, 20_000),
                (5, 24_000), (6, 32_000), (7, 48_000)]:
    probe(f"档{idx}·{tt//1000}k", make_single(idx, tt))

# H 组：多消息形态复测 24k 档（形态差异排除）
probe("H·24k·多消息", make_multi(20, 24_000))

print("\n=== 完成：看各档'热'行命中——全量=正常 / 封顶~14k=上限实锤 / 0=不写 ===")
