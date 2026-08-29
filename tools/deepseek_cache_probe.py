"""DeepSeek 缓存行为探针：验证「端点是否默认把 messages 中所有 role:system 合并放到最前」假设。
用户怀疑：分层投影（system 分散在历史中间）在 deepseek 端点被规范化重排 → 缓存前缀断裂 → 命中率低。

判别逻辑：
  D 组（关键）：同内容 system 块、不同位置——若服务端合并 system 到最前，两次规范化后完全
    相同 → 缓存应高命中；若按原始位置缓存 → 位置变化导致低命中。
  E 组：历史 assistant 去掉 reasoning_content → reasoning 是否参与缓存键。
"""
import json, pathlib, time, sys, urllib.request

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
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            u = json.loads(r.read())["usage"]
    except Exception as e:
        print(f"  [{label}] ERR {e}"); return None
    pt = u.get("prompt_tokens", 0)
    det = u.get("prompt_tokens_details") or {}
    cached = det.get("cached_tokens", 0) if isinstance(det, dict) else 0
    ratio = cached / pt if pt else 0
    print(f"  [{label}] prompt={pt} cached={cached} 命中率={ratio*100:.1f}%")
    return ratio

# ---- 构造带 reasoning 的分层投影历史（模拟 agt 装配形态）----
R = "这是历史轮次的思考链过程推理内容，用于测试缓存键是否包含 reasoning_content 字段，该内容应当相当长以便于撑起缓存体积。" * 40
S1 = "你是 Agt 框架的自主 Agent。人设与规则：应当遵守用户指令、使用工具完成复杂任务、对执行结果如实汇报。" + "A" * 600
S2 = "【已折叠的早期轮次】第1轮 hey→问候；第2轮 上下文分析→交付总结；第3轮 架构评估→方案落地。" + "B" * 600
S3 = "【当前会话上下文】档位投影说明：历史按衰减披露，工具结果截断，近轮全量。" + "C" * 600

hist = [
    {"role": "system", "content": S1},
    {"role": "user", "content": "分析一下项目结构，写一份总结"},
    {"role": "assistant", "content": "好的，我先看目录。", "reasoning_content": R[:1500]},
    {"role": "user", "content": "继续，看看 src 下有什么"},
    {"role": "assistant", "content": "发现 src 有 agent.py 等文件。", "reasoning_content": R[1500:3000]},
    {"role": "system", "content": S2},
    {"role": "user", "content": "工具调用测试：读取 config"},
    {"role": "assistant", "content": None, "reasoning_content": R[3000:4500],
     "tool_calls": [{"id": "call_1", "type": "function", "function": {"name": "read_file", "arguments": "{\"path\": \"config.json\"}"}}]},
    {"role": "tool", "tool_call_id": "call_1", "content": "{\"ok\": true, \"data\": \"...\"}"},
    {"role": "assistant", "content": "读取完成，配置正常。", "reasoning_content": R[4500:6000]},
    {"role": "system", "content": S3},
    {"role": "user", "content": "当前问题：验证缓存命中率"},
]

def deepcopy(m):
    return json.loads(json.dumps(m))

print(f"=== DeepSeek 缓存探针 model={MODEL} ===")
print("payload: system 块分散在中段（模拟分层投影）| 历史含 reasoning + tool_calls\n")

# A 基线：相同 payload 连发 2 次
print("[A 基线·同 payload 重发]（预期高命中——验证缓存通道本身）")
m1 = deepcopy(hist)
call(m1, "A1")
time.sleep(1)
call(m1, "A2")

# B 尾部追加一条 user
print("\n[B 尾部追加]（预期高命中——前缀不变）")
m2 = hist + [{"role": "user", "content": "补充一个问题：缓存如何计算？"}]
call(m2, "B1")
time.sleep(1)
call(m2, "B2")

# C 历史中间插入一段新 system（在 S2 之前插）
print("\n[C 中插 system]（预期部分命中——插入点之后断裂）")
m3 = []
for x in hist:
    if x["role"] == "system" and x["content"].startswith("【当前会话上下文】"):
        m3.append({"role": "system", "content": "【新增系统提示】这是一段插进来的新 system 块。" + "D" * 200})
    m3.append(x)
call(m3, "C1")
time.sleep(1)
call(m3, "C2")

# D 关键组：同内容 system 块，第 3 段从中间挪到最前（其它字节完全不变）
print("\n[D system 位置重排]（判别组：若服务端合并 system 到最前 → 应高命中；按原位置缓存 → 低命中）")
m4a = deepcopy(hist)
m4b = [{"role": "system", "content": S3}] + [x for x in hist if not (x["role"] == "system" and x["content"] == S3)]
call(m4a, "D1(原始位置)")
time.sleep(1)
call(m4b, "D2(位置重排)")

# E 去 reasoning：历史 assistant 去掉 reasoning_content（其它不变）
print("\n[E 去 reasoning]（reasoning 是否参与缓存键——命中率高=不参与）")
m5 = []
for x in hist:
    x = dict(x)
    if x["role"] == "assistant":
        x.pop("reasoning_content", None)
    m5.append(x)
call(hist, "E1(带 reasoning)")
time.sleep(1)
call(m5, "E2(去 reasoning)")

print("\n=== 完成 ===")
