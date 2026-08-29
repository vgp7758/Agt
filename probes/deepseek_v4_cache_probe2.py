"""DeepSeek v4 缓存探针 R2：真实投影复现（2026-08-29）。

R1 结论：阶梯长度到 70k、多消息形态，3 秒重发全 99%——上限假说否证，通道正常。
本轮拿 comfy session t253_s0 的真实投影（220k tok、带 tool_calls/reasoning_content
的消息 399 条）原样重发，隔离剩余变量：

  A 组：真实投影冷→3s→热       → 若热≈99%：payload 形态排除，指向时间/淘汰
                                  若热低：payload 内在因素（再拆 tools/reasoning）
  B 组：同 payload 45s 后再发   → 短 TTL / 块级 LRU 淘汰判别
  C 组：裁剪到 ~70k（同形态）→3s→热 → 长度 × 形态交叉（R1 的 70k 是纯文本）

花费 ≈ 220k×3 + 70k×2 ≈ 80 万输入 token。
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

    msgs = json.loads(PROJ.read_text(encoding="utf-8"))
    # 深拷贝保证两次字节级一致；content 为 None/多模态 list 原样透传
    msgs = json.loads(json.dumps(msgs))
    print(f"真实投影 t253_s0：{len(msgs)} 条消息")

    def cut_to(msgs, target_msgs):
        """裁前 N 条并保证以 user 结尾（tool_calls 尾巴服务端可能拒）。"""
        for n in range(target_msgs, 0, -1):
            if msgs[n-1].get("role") == "user":
                return msgs[:n]
        return msgs

    print(f"\n[A 真实投影 220k·全量]")
    call(msgs, "A1(冷)")
    time.sleep(3)
    r = call(msgs, "A2(热·3s)")

    print(f"\n[B 同 payload·45s 后再发]（TTL/块淘汰判别——A2 若高，看时间是否吃掉命中）")
    time.sleep(45)
    call(msgs, "B1(45s后)")

    print(f"\n[C 裁剪 ~70k·同形态]")
    sub = cut_to(msgs, 90)
    print(f"  裁到 {len(sub)} 条")
    call(sub, "C1(冷)")
    time.sleep(3)
    call(sub, "C2(热·3s)")

    print("\n=== 判读：A2 高→形态排除(时间因素) / A2 低+B1 更低→payload+时间 / C2 高→长度相关 ===")
