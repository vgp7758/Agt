"""DeepSeek v4 缓存探针 R3：tools + messages 完整请求复刻（2026-08-29）。

R2 结论：真实投影 messages（143k tok）3s/45s 重发全 99%；A1 冷请求命中 14080 =
当时会话写入的缓存几小时后只剩头部 14080 存活。TTL/淘汰/消息形态排除。
真实 s0 与 s1 之间唯一未复刻的差异：**agent 请求前带 ~78k token 的 tools schema**
（221,975 - 143,669 = 78,306，探针 R2 未带 tools 时 prompt 正好 143,669）。

本轮：假 tools（40 函数 × ~2k tok ≈ 78k，字节级稳定）+ 真实投影 messages，
三连发（冷 → 20s → 热 → 20s → 热2）——完全复刻 agent 的请求形态与节奏。

判读：
  热/热2 ≈ 99%  → tools 形态也无罪 → 指向"当时 tools 每步实际在变"（需给 Agt 加
                  tools 投影 dump 实锤）或账号级缓存竞争
  热掉到 1 万级  → 实锤 v4 对「tools 前缀 + 大 messages」组合的缓存断裂
"""
import json, pathlib, time, sys, urllib.request
if __name__ == "__main__":   # 探针脚本：仅直接执行时跑（防被 script_tools 当插件 import 时执行）
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
        """n 个假函数 schema，description 各 ~desc_chars 中文字 → 总量 ~78k token（对齐真实差值）。"""
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

    def call(messages, tools, label):
        body = json.dumps({"model": MODEL, "messages": messages, "tools": tools, "max_tokens": 16,
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

    msgs = json.loads(json.loads(json.dumps(PROJ.read_text(encoding="utf-8"))))   # 深拷贝
    tools = make_tools()
    print(f"真实投影 {len(msgs)} 条 + 假 tools {len(tools)} 个（{len(json.dumps(tools, ensure_ascii=False)):,} 字符）")

    print("\n[R3·tools + messages·agent 节奏]")
    call(msgs, tools, "1(冷)")
    time.sleep(20)
    r = call(msgs, tools, "2(热·20s)")
    time.sleep(20)
    call(msgs, tools, "3(热2·20s)")

    print("\n=== 判读：热≈99% → tools 无罪需查 Agt 实际 tools / 热掉万级 → v4 组合断裂实锤 ===")
