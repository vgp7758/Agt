"""DeepSeek v4 缓存探针 R7：断裂最小化 + 工具二分定位（2026-08-29）。

R5/R6 分野：真 tools + 增量 = 6%；假 tools / 无 tools + 增量 = 99%。
→ 真实 tools schema 内容触发（用户字符/结构怀疑成立）。

R7 流程：
  ① 断裂最小化：真 tools + 短 messages（~5k tok）+ 增量——能否复现？
     能 → 二分成本大降（每轮 ~20k tok）；不能 → 断裂需大 messages，换大 payload 二分
  ② 工具二分：候选集 T 两半，各测 [写入→20s→增量]——6% 的那半继续二分
     直到定位到单个（或一组）工具

用法：python tools/deepseek_v4_cache_probe7.py
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
TOOLS = json.loads(pathlib.Path(r"D:\AI_Usings\Agt\tools\comfy_session_tools.json").read_text(encoding="utf-8"))

def call(messages, tls, label, quiet=False):
    body = json.dumps({"model": MODEL, "messages": messages, "tools": tls, "max_tokens": 16,
                       "stream": False, "temperature": 0.2}).encode()
    req = urllib.request.Request(URL, data=body, headers={
        "Content-Type": "application/json", "Authorization": f"Bearer {TOKEN}"})
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            u = json.loads(r.read())["usage"]
    except Exception as e:
        if not quiet:
            print(f"  [{label}] ERR {e}")
        return None
    pt = u.get("prompt_tokens", 0)
    det = u.get("prompt_tokens_details") or {}
    cached = max((det.get("cached_tokens") or 0) if isinstance(det, dict) else 0,
                 u.get("prompt_cache_hit_tokens") or 0)
    if not quiet:
        print(f"  [{label}] prompt={pt:>8,} cached={cached:>8,} 命中={cached*100//max(pt,1):>3}% ({time.time()-t0:.1f}s)")
    return cached, pt

def increment_ok(tools_subset, prefix_msgs, tag) -> bool:
    """写入（前缀+尾A）→20s→增量（前缀+尾B），返回增量是否高命中（True=正常）。"""
    ta = [{"role": "user", "content": f"写入尾A（{tag}）。"}]
    tb = [{"role": "user", "content": f"增量尾B（{tag}）——与尾A不同。"}]
    call(prefix_msgs, tools_subset, f"{tag}·写入", quiet=True)
    time.sleep(20)
    r = call(prefix_msgs + tb, tools_subset, f"{tag}·增量", quiet=True)
    if r is None:
        return None
    ok = r[0] * 100 >= 90 * max(r[1], 1)
    print(f"  [{tag}] prompt={r[1]:>8,} cached={r[0]:>8,} {'✅ 正常' if ok else '❌ 断裂'}")
    return ok

msgs = json.loads(json.loads(json.dumps(PROJ.read_text(encoding="utf-8"))))

# ---- ① 断裂最小化：短 messages ----
print("=== ① 真 tools + 短 messages 增量（断裂能否复现）===")
short = msgs[:40]
while short and short[-1].get("role") != "user":
    short = short[:-1]
print(f"短前缀：{len(short)} 条")
increment_ok(TOOLS, short, "短msgs·全99工具")
time.sleep(5)
increment_ok(TOOLS, msgs[:398], "长msgs·全99工具（R5 复核）")

# ---- ② 工具二分 ----
print("\n=== ② 工具二分（用 ① 里能复现断裂的最短前缀）===")
prefix_use = short   # 若 ① 显示短前缀不断裂，这里应换 msgs[:398]
half = len(TOOLS) // 2
r = increment_ok(TOOLS[:half], prefix_use, f"前半 {half} 个")
time.sleep(5)
r2 = increment_ok(TOOLS[half:], prefix_use, f"后半 {len(TOOLS)-half} 个")
print("\n（下一轮对 ❌ 断裂的那半继续二分——改本文件 TOOLS 切片重跑）")
