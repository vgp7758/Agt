"""DeepSeek v4 缓存探针 R8：正交拆解 tools真假 × 增量前有无重发（2026-08-29）。

矛盾样本：
  R5：真tools，写入→重发→增量 = 增量 6% 断
  R6：假tools，写入→全量→重发→增量 = 增量 99%
  R7：真tools，写入→增量（无重发在前）= 99%
两变量（tools 真假、增量前是否夹过完全重发）各只一次样本，正交补齐：

  组B：真 tools，写入→20s→重发→20s→增量   （R5 时序复刻，再取样本）
  组C：假 tools，写入→20s→重发→20s→增量   （R6 时序复刻）
  组D：真 tools，写入→20s→增量            （R7 时序第二样本）
每组间隔 5s。若 B 断 C/D 好 → 「重发+真tools」组合实锤；全好 → R5 偶发，
需扩大样本（服务端非确定性路由）。
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
    TOOLS = json.loads(pathlib.Path(r"D:\AI_Usings\Agt\probes\comfy_session_tools.json").read_text(encoding="utf-8"))

    def fake_tools(n=40, desc_chars=1500):
        base = "模拟真实工具箱的确定性描述填充，用于复刻 agent 请求里 tools schema 的量级与字节稳定性。"
        out = []
        for i in range(n):
            desc = f"工具{i}：执行确定性模拟操作。" + (base * (desc_chars // len(base) + 1))
            out.append({"type": "function", "function": {
                "name": f"fake_tool_{i}", "description": desc[:desc_chars],
                "parameters": {"type": "object", "properties": {
                    "arg1": {"type": "string", "description": f"参数一（工具{i}）"},
                    "arg2": {"type": "integer", "description": f"参数二（工具{i}）"}},
                    "required": ["arg1"]}}})
        return out

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
        verdict = "✅" if cached * 100 >= 90 * max(pt, 1) else "❌"
        print(f"  [{label}] prompt={pt:>8,} cached={cached:>8,} 命中={cached*100//max(pt,1):>3}% {verdict} ({time.time()-t0:.1f}s)")
        return cached, pt

    msgs = json.loads(json.loads(json.dumps(PROJ.read_text(encoding="utf-8"))))
    prefix = msgs[:398]
    tail_a = [{"role": "user", "content": f"写入尾A。"}]
    tail_b = [{"role": "user", "content": f"增量尾B——与尾A不同。"}]
    FT = fake_tools()

    def group(tag, tls, with_resend):
        print(f"\n[组{tag}] {'写入→重发→增量' if with_resend else '写入→增量'}")
        call(prefix + tail_a, tls, f"{tag}·1写入")
        time.sleep(20)
        if with_resend:
            call(prefix + tail_a, tls, f"{tag}·2重发")
            time.sleep(20)
        call(prefix + tail_b, tls, f"{tag}·末增量")

    group("B·真tools·夹重发", TOOLS, True)
    time.sleep(5)
    group("C·假tools·夹重发", FT, True)
    time.sleep(5)
    group("D·真tools·无重发", TOOLS, False)
