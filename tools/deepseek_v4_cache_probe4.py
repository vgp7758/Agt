"""DeepSeek v4 缓存探针 R4：增量请求判别（2026-08-29）——决定性实验。

前情：
  R1 纯填充冷=0；R2 真实投影重发 99%（3s/45s）；R3 假tools+真实投影重发 99%（20s）。
  但 R2/R3 冷发都命中 ~14k，且 R3 的假 tools 前缀从未被写入 → 命中非前缀、
  是与存量条目的"内容块重叠"——v4 缓存匹配粒度存疑。
  Agent 场景的本质是【增量请求】：前缀 byte-stable + 尾部追加/变化。
  完全相同 payload 重发 ≠ 增量请求。这是最后未复刻的变量。

判别（payload 前缀严格 byte 相同，仅尾部差异）：
  req1 = messages[:397]（截尾，user 结尾）
  req2 = messages 完整（= req1 前缀 + 追加 assistant/tool/新 tail 模拟下一步）
  req3 = messages[:397] + 不同的追加尾部（模拟再下一步）
  —— 各间隔 20s（agent 节奏）

  req2/req3 命中 ≈ req1 写入量 → 前缀缓存正常（回到 tools 变化假说，需加 dump）
  req2/req3 命中只 ~14k    → 【实锤】v4 前缀缓存在增量请求下失效——
                             agent 多步场景缓存形同虚设的根因
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
PROJ = pathlib.Path(r"C:\Users\vgp77\.agt\repos\E--Programs-comfy\sessions\20260811_225429\projections\t253_s0_202403.json")

def call(messages, label):
    body = json.dumps({"model": MODEL, "messages": messages, "max_tokens": 16,
                       "stream": False, "temperature": 0.2}).encode()
    req = urllib.request.Request(URL, data=body, headers={
        "Content-Type": "application/json", "Authorization": f"Bearer {TOKEN}"})
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            u = json.loads(r.read())["usage"]
    except Exception as e:
        print(f"  [{label}] ERR {e}")
        return None
    pt = u.get("prompt_tokens", 0)
    det = u.get("prompt_tokens_details") or {}
    cached = max((det.get("cached_tokens") or 0) if isinstance(det, dict) else 0,
                 u.get("prompt_cache_hit_tokens") or 0)
    print(f"  [{label}] prompt={pt:>8,} cached={cached:>8,} 命中={cached*100//max(pt,1):>3}% ({time.time()-t0:.1f}s)")
    return cached, pt

msgs = json.loads(json.loads(json.dumps(PROJ.read_text(encoding="utf-8"))))
# 确定干净截点：最后一条 user 的位置（idx 397 是 user？s0 的 idx398 是 tail system）
# msgs[0:398] = 除去尾部 tail 的全部（前缀 byte 与完整版一致）
cut = 398
while cut > 0 and msgs[cut-1].get("role") != "user":
    cut -= 1
prefix = msgs[:cut]
print(f"真实投影 {len(msgs)} 条；干净前缀取前 {cut} 条（user 结尾）")

tail_a = msgs[cut:]                       # 原尾部（tail system 等）
tail_b = [{"role": "user", "content": "模拟下一步：用户追问一个新问题，前缀保持完全一致。"}]

print("\n[R4·增量请求（前缀 byte-stable，仅尾部变化）]")
r1 = call(prefix, "req1(截尾·写入前缀)")
time.sleep(20)
r2 = call(prefix + tail_a, "req2(前缀+原尾部)")
time.sleep(20)
r3 = call(prefix + tail_b, "req3(前缀+新尾部)")
time.sleep(20)
r4 = call(prefix, "req4(截尾·复测=req1 原样重发对照)")

print("\n=== 判读：req2/3 高=前缀缓存正常 / req2/3 只~14k 而 req4 高 = 增量请求失效实锤 ===")
