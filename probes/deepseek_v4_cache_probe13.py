"""DeepSeek v4 缓存探针 R13：机制判别——重排合并 vs role 分层级联（2026-08-29）。

用户假说：不是重排，而是按 role 优先级分层缓存（先 system 层、再 tools、再 user/assistant），
system 层变化 → 级联失效。与"重排合并"在已有观测（R12b）上预测一致，本轮判别：

E3-head：头部第二条 system（折叠摘要位置）变化，尾部 user 固定
   6%        → 位置无关 = 重排合并 或 硬级联（两假说仍不可分）
   部分命中   → 分层且 per-system 独立（用户假说强支持，重排被否定）
E5-tools：messages 全同，tools 列表尾部 +1 新工具（模拟 Agt 工作流每轮扫描）
   断        → tools 变化也是缓存杀手（Agt wf_* 跨轮变化中招，实践意义重大）
   99%       → tools 层独立或尾部追加不破坏
"""
import json, pathlib, time, sys, urllib.request
if __name__ == "__main__":   # 探针脚本：仅直接执行时跑（防被 script_tools 当插件 import 时执行）
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    CFG = json.loads((pathlib.Path.home() / ".agt" / "models.json").read_text(encoding="utf-8"))["models"]
    P = CFG["deepseek"]
    URL = P["base_url"].rstrip("/") + "/chat/completions"
    TOKEN = P["api_token"][0] if isinstance(P["api_token"], list) else P["api_token"]
    MODEL = P["model"]
    msgs = json.loads(json.loads(json.dumps(pathlib.Path(r"C:\Users\vgp77\.agt\repos\E--Programs-comfy\sessions\20260811_225429\projections\t253_s0_202403.json").read_text(encoding="utf-8"))))
    TOOLS = json.loads(pathlib.Path(r"D:\AI_Usings\Agt\probes\comfy_session_tools.json").read_text(encoding="utf-8"))
    base = msgs[:398] + [{"role": "user", "content": "继续任务。"}]

    def call(m, tls, label):
        body = json.dumps({"model": MODEL, "messages": m, "tools": tls, "max_tokens": 16,
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
        pct = c * 100 // max(pt, 1)
        print(f"  [{label}] prompt={pt:>8,} cached={c:>8,} 命中={pct:>3}% ({time.time()-t0:.1f}s)")
        return c, pt

    print("[E3-head·头部第二条 system（折叠摘要位）变化，尾部 user 固定]")
    m_w = [base[0], base[1], *base[2:]]                     # 写入版
    m_i = [base[0], dict(base[1], content=str(base[1]["content"]) + f"\n【追加差异行·增量步】"), *base[2:]]
    call(m_w, TOOLS, "E3·写入")
    time.sleep(20)
    r = call(m_i, TOOLS, "E3·增量(仅第2条system变)")
    if r:
        c, pt = r
        print(f"  → 判读：{c*100//max(pt,1)}% | 6%=位置无关(重排/硬级联) | 部分命中=分层独立(用户假说)")

    print("\n[E5-tools·messages 全同，tools 尾部 +1 新工具（模拟工作流每轮扫描）]")
    new_tool = {"type": "function", "function": {
        "name": "wf_new_scan_tool", "description": "新扫描出来的工作流工具，每轮清单可能增减。" * 10,
        "parameters": {"type": "object", "properties": {"q": {"type": "string"}}, "required": ["q"]}}}
    call(base, TOOLS, "E5·写入(99工具)")
    time.sleep(20)
    r = call(base, TOOLS + [new_tool], "E5·增量(100工具,+1在尾)")
    if r:
        c, pt = r
        print(f"  → 判读：{c*100//max(pt,1)}% | 99%=尾部追加无害 | 低=tools 变化也是缓存杀手")
