"""Jev 批量判断节点插件（type jev_batch_judge）：长文本 → 按行分组 → NanoJev 批量命题判断 → 聚合。

设计（用户提案 2026-09-25）：
· 切片不用 LLM：文本 split 多行 → 顺序贪心装组（组内总字符 ≤ group_max_chars 即继续装，
  装不下开新组；超长单行截断到上限独占一组）——如 100 行分 38/32/30 三组。
· 每组一个 state（事实陈述由调用方保证——jev 对「命题 vs 陈述」敏感，问句式 state 质量差，
  用户实锤 2026-09-25），判断题 = boolean 命题（<question name>命题文本</question>）。
· 聚合策略：agg ∈ {max, min, avg} × 时机 ∈ {早停, 全量}：
    - early_threshold 非空 → 逐组推理，每完成一组用已见 p_true 算中途 agg，≥ 阈值即停
      （「最大值·任意组超过阈值」即 agg=max + early；min/agg 数学上良定义，几乎不早停=等价全量）
    - early_threshold 空 → 一次 submit 全组并行（states 数组，单次前向），跑完按 agg 聚合
· 输出：results{题名: {agg, value, per_group[], groups_done, stopped_early}} / max_agg / hit（先达阈值题）。

链路复用 intent_nano 的服务发现（settings jev_base_url 优先 → 本机 8766 默认自动拉起）。
注意 jev max_length=512 硬上限（超限整组 ValueError——group_max_chars 默认 600 字符中文安全；
英文可调大到 ~1500）。
"""
import json as _json
import time as _time
import urllib.request as _urlreq

PARAMS = [
    {"key": "text", "type": "string", "required": True,
     "desc": "待判断长文本（ref 上游字段；按行分组后逐组送 NanoJev）"},
    {"key": "group_max_chars", "type": "number", "required": False, "default": 600,
     "desc": "每组最大字符数（顺序贪心装行；默认 600=中文安全，英文可 ~1500）"},
    {"key": "agg", "type": "string", "required": False, "default": "max",
     "enum": ["max", "min", "avg"],
     "desc": "聚合函数：max（最大值）/ min（最小值）/ avg（平均值）——各组 p_true 的聚合"},
    {"key": "early_threshold", "type": "number", "required": False, "default": 0.0,
     "desc": "早停阈值（0-1，空/0=全量推理）：非空时逐组推理，中途聚合值 ≥ 阈值即停"},
    {"key": "temperature", "type": "number", "required": False, "default": 1.0,
     "desc": "NanoJev 采样温度（默认 1.0）"},
]


def _http_json(url, payload=None, timeout=15, token=""):
    if payload is None:
        req = _urlreq.Request(url)
    else:
        req = _urlreq.Request(url, data=_json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                              method="POST")
        req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with _urlreq.urlopen(req, timeout=timeout) as r:
        return _json.loads(r.read().decode("utf-8"))


def _jev_target():
    """settings jev_base_url 优先（与 intent_nano 同源约定）；未配置 → 本机 8766 默认。"""
    try:
        import config as _cfg
        c = _cfg.load_jev_config()
        if c.get("base_url"):
            return c["base_url"].rstrip("/"), c.get("api_token") or "", False
    except Exception:
        pass
    return "http://127.0.0.1:8766", "", True


def _ensure_server(base, token="", timeout=60):
    """探活；不通且为本机默认路径时自动拉起（与 intent_nano 同款命令）。"""
    try:
        _http_json(f"{base}/api/health", timeout=4, token=token)
        return True
    except Exception:
        pass
    import os as _os
    import subprocess as _sub
    cmd_path = r"D:\models\NanoJev\tools\nanojev_server.py"
    try:
        if not _os.path.exists(cmd_path):
            return False
        _sub.Popen(["python", cmd_path, "--checkpoint", r"D:\models\NanoJev", "--port", "8766"],
                   creationflags=0x00000008 | 0x00000200,
                   stdout=_sub.DEVNULL, stderr=_sub.DEVNULL, stdin=_sub.DEVNULL)
    except Exception:
        return False
    deadline = _time.time() + timeout
    while _time.time() < deadline:
        _time.sleep(2)
        try:
            _http_json(f"{base}/api/health", timeout=4, token=token)
            return True
        except Exception:
            continue
    return False


def _split_groups(text, max_chars):
    """按行顺序贪心装组（用户设计）：累计 ≤ max_chars 继续装；超长单行截断独占。"""
    groups, cur, cur_len = [], [], 0
    for ln in str(text or "").split("\n"):
        line = ln.rstrip()
        add = len(line) + 1
        if cur and cur_len + add > max_chars:
            groups.append("\n".join(cur))
            cur, cur_len = [], 0
        if add > max_chars:                       # 超长单行：截断独占一组（不炸 jev 硬限的兜底）
            groups.append(line[:max_chars])
            continue
        cur.append(line)
        cur_len += add
    if cur:
        groups.append("\n".join(cur))
    return groups


def _is_proxy(base):
    """lfm_proxy 形态（base_url 含 /run/ 路径段）：不支持子路径透传（/api/health 404）——
    走单 POST {base}?wait=1 同步模式（proxy 内部 submit+轮询+按需拉起服务）。"""
    return "/run/" in base


def _submit_states(states_payload, questions, temperature, base, token):
    """统一推理入口 → {state_id: answers}（按 state id 索引）。
    proxy 形态：单 POST {base}?wait=1（同步等全组，proxy 自动拉起后端）；
    直连形态：submit → 轮询 result（8766 / 自建 Jev 兼容服务）。"""
    body = {"states": states_payload, "temperature": float(temperature or 1.0)}
    if _is_proxy(base):
        try:
            r = _http_json(f"{base}?wait=1", body, timeout=600, token=token)
        except Exception:
            return None
        if not isinstance(r, dict) or r.get("status") != "done":
            return None
        out = {}
        for st in (r.get("result") or {}).get("states") or []:
            out[st.get("id")] = st.get("answers") or {}
        return out
    sub = _http_json(f"{base}/api/submit", body, timeout=30, token=token)
    rid = sub.get("request_id")
    if not rid:
        return None
    for _ in range(120):
        _time.sleep(1.0)
        res = _http_json(f"{base}/api/result/{rid}", timeout=10, token=token)
        if res.get("status") in ("done", "failed", "error"):
            if res.get("status") != "done":
                return None
            out = {}
            for st in (res.get("result") or {}).get("states") or []:
                out[st.get("id")] = st.get("answers") or {}
            return out
    return None


def _agg_of(vals, mode):
    if not vals:
        return 0.0
    if mode == "min":
        return min(vals)
    if mode == "avg":
        return sum(vals) / len(vals)
    return max(vals)


def _handle_jev_batch_judge(node: dict, ctx) -> dict:
    from workflow_node_api import resolve_input_params

    inputs = node.get("data", {}).get("inputs", {})
    params = resolve_input_params(inputs.get("inputParameters", []), ctx)
    text = str(params.get("text") or "")
    questions = [(q.get("name", "") or f"q{i+1}", (q.get("text") or "").strip())
                 for i, q in enumerate(inputs.get("questions", []))
                 if (q.get("text") or "").strip()]
    try:
        max_chars = max(50, int(float(params.get("group_max_chars") or 600)))
    except Exception:
        max_chars = 600
    agg_mode = str(params.get("agg") or "max").strip().lower()
    if agg_mode not in ("max", "min", "avg"):
        agg_mode = "max"
    try:
        early = float(params.get("early_threshold") or 0.0)
    except Exception:
        early = 0.0
    try:
        temperature = float(params.get("temperature") or 1.0)
    except Exception:
        temperature = 1.0

    err = {"results": {}, "max_agg": 0.0, "hit": "", "raw": "", "error": ""}
    if not text.strip() or not questions:
        err["error"] = "text/questions 为空"
        return {"outputs": err}

    base, token, _is_local = _jev_target()
    if not _is_proxy(base) and not _ensure_server(base, token):
        err["error"] = f"Jev 服务不可达（{base}）"
        return {"outputs": err}

    groups = _split_groups(text, max_chars)
    qspec = {name: {"type": "boolean", "instructions": prop} for name, prop in questions}
    qnames = [n for n, _p in questions]
    # p_true[题名][组下标]
    p_true = {n: [] for n in qnames}
    stopped_early = False
    groups_done = 0

    if early > 0:
        # 早停：逐组推理（每组一个 state），每完成一组算中途聚合，≥ 阈值即停
        for gi, gtext in enumerate(groups):
            payload = [{"id": f"g{gi}", "state": gtext, "questions": qspec}]
            ans = _submit_states(payload, qspec, temperature, base, token)
            if ans is None:
                err["error"] = f"第 {gi+1} 组推理失败"
                break
            a0 = ans.get(f"g{gi}") or {}
            for n in qnames:
                p = float(((a0.get(n) or {}).get("p_true")) or 0.0)
                p_true[n].append(round(p, 4))
            groups_done = gi + 1
            if any(_agg_of(p_true[n], agg_mode) >= early for n in qnames):
                stopped_early = True
                break
    else:
        # 全量：一次 submit 全组并行（states 数组，单次前向）
        payload = [{"id": f"g{gi}", "state": gt, "questions": qspec} for gi, gt in enumerate(groups)]
        ans = _submit_states(payload, qspec, temperature, base, token)
        if ans is None:
            err["error"] = "推理失败/超时"
        else:
            for gi in range(len(groups)):
                a0 = ans.get(f"g{gi}") or {}
                for n in qnames:
                    p = float(((a0.get(n) or {}).get("p_true")) or 0.0)
                    p_true[n].append(round(p, 4))
            groups_done = len(groups)

    results = {}
    hit = ""
    best = 0.0
    for n in qnames:
        vals = p_true[n]
        a = round(_agg_of(vals, agg_mode), 4)
        results[n] = {"agg": a, "value": bool(a >= 0.5), "per_group": vals,
                      "groups_done": groups_done, "stopped_early": stopped_early,
                      "groups_total": len(groups)}
        if a > best:
            best, hit = a, n
    out = {"results": results, "max_agg": round(best, 4), "hit": hit if stopped_early else "",
           "raw": _json.dumps({"groups": len(groups), "chars": [len(g) for g in groups]},
                              ensure_ascii=False)[:800],
           "error": ""}
    return {"outputs": out}


def agt_node():
    return {"type": "jev_batch_judge", "label": "Jev·批量判断", "handler": _handle_jev_batch_judge,
            "catalog": _CATALOG}


_CATALOG = {"name": "Jev·批量判断（长文本分组命题判断）",
            "desc": "长文本按行分组 → NanoJev 批量命题判断 → 聚合（用户提案 2026-09-25）。"
                    "切片不用 LLM：顺序贪心装组（group_max_chars 上限）；每组一个 state 逐组/全并行推理；"
                    "聚合 max/min/avg × 早停/全量（early_threshold 非空=任意组中途聚合达阈值即停）。"
                    "注意 jev 硬上限 512 token/组（默认 600 字符中文安全，英文可 ~1500）。",
            "xml": "<!-- 长文本分组批量命题判断：切片→逐组→聚合 -->\n"
                   "<node id=\"170001\" type=\"jev_batch_judge\" title=\"Jev批量判断\">\n"
                   "  <in name=\"text\" ref=\"100001.doc\"/>\n"
                   "  <in name=\"group_max_chars\" type=\"number\">600</in>\n"
                   "  <in name=\"agg\" type=\"string\">max</in>\n"
                   "  <in name=\"early_threshold\" type=\"number\">0.7</in>\n"
                   "  <question name=\"敏感\">文本中出现了敏感词。</question>\n"
                   "  <question name=\"代码\">文本中包含代码。</question>\n"
                   "  <out name=\"results\" type=\"object\"/>\n"
                   "  <out name=\"max_agg\" type=\"number\"/>\n"
                   "  <out name=\"hit\" type=\"string\"/>\n"
                   "  <out name=\"raw\" type=\"string\"/>\n"
                   "</node>"}
