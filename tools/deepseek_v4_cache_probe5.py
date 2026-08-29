"""DeepSeek v4 缓存探针 R5：真实 tools 复刻（2026-08-29）。

前情：R2/R3/R4 已排除 messages 内容、长度、tools 量级（假 tools）、增量请求形态——
全部 99%。唯一未测变量：**当时真实的 tools schema 内容**（用户怀疑 schema 里有
某种字符/结构让 DeepSeek 端点缓存键处理异常）。

步骤：
  ① 在 comfy 仓库现场用 build_agent 重建工具箱（内置 + 工作流 wf_* + MCP），
     dump schemas 到 comfy_session_tools.json
  ② 真实 tools + t253_s0 真实投影，agent 节奏三连发 + 增量变体

判读：热发掉到 ~14k → 实锤真实 tools 内容触发断裂（再二分定位工具）
      全 99% → 端点当时故障/灰度（现已恢复），Agt 无罪结案
"""
import json, os, pathlib, sys, time, urllib.request

AGT_SRC = r"D:\AI_Usings\Agt\src"
COMFY = pathlib.Path(r"E:\Programs\comfy")
os.chdir(COMFY)                      # WORKSPACE = Path.cwd() → comfy 仓库
sys.path.insert(0, AGT_SRC)
sys.path.insert(0, str(COMFY))

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

# ---- ① 重建真实工具箱 ----
print("=== ① build_agent 重建 comfy 工具箱 ===")
from mcp_client import MCPManager
mcp_mgr = MCPManager()
try:
    mcp_mgr.connect_all()   # 全局 ~/.agt/mcp.json；失败不炸（探针主体继续）
except Exception as e:
    print(f"  MCP 连接失败（跳过 MCP 工具）: {e}")

from chat import build_agent
agent = build_agent(mcp_mgr, verbose=False, workspace=COMFY)
# 工作流工具：agent 首轮才扫描注册？手动触发一轮注册（幂等）
try:
    from workflow import refresh_workflow_tools
    refresh_workflow_tools(agent)
except Exception as e:
    try:
        from workflow import scan_and_register
        scan_and_register(agent)
    except Exception:
        print(f"  （工作流注册函数未找到/失败：{e}——用当前工具箱）")

tools = agent.tools.schemas()
dump = pathlib.Path(r"D:\AI_Usings\Agt\tools\comfy_session_tools.json")
dump.write_text(json.dumps(tools, ensure_ascii=False), encoding="utf-8")
n_chars = len(json.dumps(tools, ensure_ascii=False))
print(f"  工具箱：{len(tools)} 个工具 / {n_chars:,} 字符 → {dump.name}")
names = [t["function"]["name"] for t in tools]
print(f"  工具名（前 60）：{names[:60]}")

# ---- ② 缓存探针 ----
print("\n=== ② 真实 tools + 真实投影缓存探针 ===")
CFG = json.loads((pathlib.Path.home() / ".agt" / "models.json").read_text(encoding="utf-8"))["models"]
PROFILE = CFG["deepseek"]
URL = PROFILE["base_url"].rstrip("/") + "/chat/completions"
TOKEN = PROFILE["api_token"][0] if isinstance(PROFILE["api_token"], list) else PROFILE["api_token"]
MODEL = PROFILE["model"]
PROJ = pathlib.Path(r"C:\Users\vgp77\.agt\repos\E--Programs-comfy\sessions\20260811_225429\projections\t253_s0_202403.json")

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

call(msgs, tools, "1(冷·真tools+全量)")
time.sleep(20)
call(msgs, tools, "2(热·20s)")
time.sleep(20)
call(prefix + tail_new, tools, "3(增量：前缀+新尾)")

print("\n=== 判读：2/3 ≈99% → Agt/内容全无罪，当时端点故障；掉到~14k → 真 tools 内容触发 ===")
