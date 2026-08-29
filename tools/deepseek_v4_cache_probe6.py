"""DeepSeek v4 缓存探针 R6：假 tools + 增量对照（2026-08-29）。

R5 复现断裂：真 tools + 增量（前缀 byte 相同+尾部换）= 6%；同 payload 重发 = 99%。
R4 无 tools 增量 = 99%。R3 假 tools 只测了重发（99%），没测增量——留了这个洞。

R6 补上：R3 同款假 tools + R5 同款增量形态。
  假 tools 增量 ≈99% → 断裂由【真 tools 内容】触发（用户字符/结构怀疑成立）
  假 tools 增量 ~14k → 断裂由【tools 存在 + 增量请求】组合触发（端点 bug，内容无关）
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

def make_tools(n=40, desc_chars=1500):
    base = "模拟真实工具箱的确定性描述填充，用于复刻 agent 请求里 tools schema 的量级与字节稳定性。"
    tools = []
    for i in range(n):
        desc = f"工具{i}：执行确定性模拟操作。" + (base * (desc_chars // len(base) + 1))
        tools.append({
            "type": "function",
            "function": {
                "name": f"fake_tool_{i}",
                "description": desc[:desc_chars],
                "parameters": {"type": "object", "properties": {
                    "arg1": {"type": "string", "description": f"参数一（工具{i}）"},
                    "arg2": {"type": "integer", "description": f"参数二（工具{i}）"},
                }, "required": ["arg1"]},
            },
        })
    return tools

def call(messages, tls, label):
    body = json.dumps({"model": MODEL, "messages": messages, "tools": tls, "max_tokens": 16,
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
prefix = msgs[:398]
tail_new = [{"role": "user", "content": "模拟下一步追问，前缀完全一致。"}]
tools = make_tools()

print("[R6·假 tools + 增量（R3 tools × R5 增量形态）]")
call(prefix + tail_new, tools, "1(冷·假tools+增量形态)")
time.sleep(20)
call(msgs, tools, "2(假tools+全量·同R3冷)")
time.sleep(20)
call(prefix + tail_new, tools, "3(热·=req1 原样重发)")
time.sleep(20)
call(prefix + [{"role": "user", "content": "另一个不同的追问尾部。"}], tools, "4(增量·前缀同+另一新尾)")

print("\n=== 判读：4 掉到~14k → tools+增量组合断裂（端点 bug） / 4 ≈99% → 真 tools 内容触发 ===")
