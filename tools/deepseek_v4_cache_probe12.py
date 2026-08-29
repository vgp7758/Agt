"""DeepSeek v4 缓存探针 R12（终局）：agent 节奏生长式步进（2026-08-29）。

官方账单结论：账号/端点正常（cc 79%、探针 87%），问题特定于 Agt 模式——
单请求 20-32 万 tok × 高频连续（16 点段 81 请求 miss 1480 万）。

R12 精确复刻 agent 的 react 步进：
  每步 = 上一步请求 + [assistant(tool_calls+reasoning) + tool 结果] + 变化的 tail 时间块
  （前缀永远 byte-stable，只在尾部生长——与 agent 完全同构）
  10 步 × 间隔 30 秒 × 每步 22 万+ token

判读：
  全程 ≈99%   → 端点降级假说否证，Agt 侧仍有未发现差异（需请求体全量 dump）
  中途掉个位数 → 实锤：v4 对「超大请求×快速步进」缓存写入降级——Agt 低命中根因盖棺
"""
import json, pathlib, time, sys, urllib.request

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

CFG = json.loads((pathlib.Path.home() / ".agt" / "models.json").read_text(encoding="utf-8"))["models"]
P = CFG["deepseek"]
URL = P["base_url"].rstrip("/") + "/chat/completions"
TOKEN = P["api_token"][0] if isinstance(P["api_token"], list) else P["api_token"]
MODEL = P["model"]
PROJ = pathlib.Path(r"C:\Users\vgp77\.agt\repos\E--Programs-comfy\sessions\20260811_225429\projections\t253_s0_202403.json")
TOOLS = json.loads(pathlib.Path(r"D:\AI_Usings\Agt\tools\comfy_session_tools.json").read_text(encoding="utf-8"))

def call(m, label):
    body = json.dumps({"model": MODEL, "messages": m, "tools": TOOLS, "max_tokens": 16,
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
    c = max((det.get("cached_tokens") or 0) if isinstance(det, dict) else 0,
            u.get("prompt_cache_hit_tokens") or 0)
    v = "✅" if c * 100 >= 90 * max(pt, 1) else "❌"
    print(f"  [{label}] prompt={pt:>8,} cached={c:>8,} 命中={c*100//max(pt,1):>3}% {v} ({time.time()-t0:.1f}s)")
    return c, pt

msgs = json.loads(json.loads(json.dumps(PROJ.read_text(encoding="utf-8"))))
base = msgs[:398] + [{"role": "user", "content": "继续任务：处理这个文件。"}]

REASONING = "嗯，用户要求继续。我先读取文件确认当前内容，然后决定怎么改。这一步应该先看现状。" * 4
def step_messages(i):
    """第 i 步的尾部：i 组 assistant(tool_calls+reasoning)+tool + 变化的 tail 时间块。"""
    out = list(base)
    for k in range(1, i + 1):
        cid = f"call_r12_{k}"
        out.append({"role": "assistant", "content": None, "reasoning_content": REASONING,
                    "tool_calls": [{"id": cid, "type": "function",
                                    "function": {"name": "read_file",
                                                 "arguments": "{\"path\": \"f%d.py\"}" % k}}]})
        out.append({"role": "tool", "tool_call_id": cid,
                    "content": f"文件 f{k}.py 共 120 行，第 30 行起是目标函数 def target_{k}()……（模拟结果）"})
    # tail 时间块：每步内容不同（模拟 agent 每步重渲染的当前时间）
    out.append({"role": "system",
                "content": f"<system-reminder>\n当前时间：2026-08-29 21:4{k}:00 Saturday\n（tail 第{i}步）\n</system-reminder>"})
    return out

print(f"[R12·agent 节奏生长步进] base={len(base)} 条 + 每步 +2 条 + 变化 tail；10 步 × 30s")
for i in range(1, 11):
    call(step_messages(i), f"step{i:>2}")
    if i < 10:
        time.sleep(30)

print("\n=== 判读：全程✅ → 需请求体 dump 抓 Agt 残差 / 中途❌ → 端点对大请求步进降级实锤 ===")
