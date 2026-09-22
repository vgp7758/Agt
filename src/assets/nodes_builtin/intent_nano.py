"""NanoJev 快速意图路由节点插件（type intent_nano）：判别式意图分类——内置 Intent（type 22）的加强版。

与内置 intent（LLM 生成式：提示词→回复编号→解析）的区别：
· 判别式：NanoJev 0.6B 决策模型（Qwen3-0.6B backbone + 决策头）一次前向直接输出候选概率分布
  ——不生成文本，无编号解析歧义，CPU fp32 单次前向 ~1s、零 token 费用
· 可解释：输出完整概率分布 + 置信度；top1 置信度 < threshold 时视为不确定 → 走 default
  分支（不确定样本不硬路由——生成式 intent 做不到的软拒识）
· 意图描述直供模型：XML 里 <intent name="提问">用户想了解…</intent> 的描述体作为
  criteria 语义描述（内置 intent 只用 name 拼提示词）

服务链路（用户提案 2026-09-22 通用化）：settings 的 jev_base_url（可配自建 Jev 兼容服务 +
 jev_api_token 鉴权）优先；未配置 → 本机 8766 默认（lfm_services.json 的 nanojev，本机路径
 存在时自动拉起最长 60s）。任一环节不可用 → 降级 LLM 生成式分类（conf=-1 标记降级模式，
 节点在任何环境可用、不炸工作流）。

下游兼容：出口端口与内置 intent 同构（branch_N 按意图序，未命中 default）——
已有工作流可直接替换节点迁移。
"""
import json as _json
import subprocess as _subprocess
import time as _time
import urllib.request as _urlreq

PORT = 8766
DEFAULT_BASE = f"http://127.0.0.1:{PORT}"
LAUNCH_CMD = ["python", r"D:\models\NanoJev\tools\nanojev_server.py",
              "--checkpoint", r"D:\models\NanoJev", "--port", str(PORT)]


def _jev_target():
    """连接目标（用户提案 2026-09-22 通用化）：settings 的 jev_base_url（带 jev_api_token）优先；
    未配置 → 本机 8766 默认（lfm_services.json 的 nanojev）。返回 (base, token, is_local_default)。"""
    try:
        import config as _cfg
        c = _cfg.load_jev_config()
        if c.get("base_url"):
            return c["base_url"].rstrip("/"), c.get("api_token") or "", False
    except Exception:
        pass
    return DEFAULT_BASE, "", True

PARAMS = [
    {"key": "query", "type": "string", "required": True,
     "desc": "待分类文本（通常 ref 上游 user_message 等）"},
    {"key": "intents", "type": "list", "required": True,
     "desc": "意图列表：name=意图名（对应 branch_N 出口），description=语义描述（供模型判别；空则用 name）"},
    {"key": "temperature", "type": "number", "required": False, "default": 1.0,
     "desc": "NanoJev 采样温度（默认 1.0；想更确定性可调低，如 0.3）"},
    {"key": "threshold", "type": "number", "required": False, "default": 0.0,
     "desc": "置信度阈值（0-1）：top1 概率低于它 → 走 default（软拒识不确定样本）"},
]


def _http_json(url, payload=None, timeout=15, token=""):
    if payload is None:
        req = _urlreq.Request(url)
    else:
        req = _urlreq.Request(url, data=_json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                              method="POST")
        req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")   # 自建 Jev 服务鉴权（可选）
    with _urlreq.urlopen(req, timeout=timeout) as r:
        return _json.loads(r.read().decode("utf-8"))


def _ensure_server(base, token="", timeout=60, allow_launch=False):
    """Jev 服务探活；不通且 allow_launch（本机默认且拉起命令存在）时拉起并等就绪。
    自建远程服务（settings 配置的）不拉——探活失败由调用方降级 LLM。"""
    try:
        _http_json(f"{base}/api/health", timeout=4, token=token)
        return True
    except Exception:
        pass
    if not allow_launch:
        return False
    import os as _os
    try:
        if not _os.path.exists(LAUNCH_CMD[1]):
            return False   # 本机无 NanoJev 安装（其它机器）——不拉，降级
        _subprocess.Popen(LAUNCH_CMD, creationflags=0x00000008 | 0x00000200,
                          stdout=_subprocess.DEVNULL, stderr=_subprocess.DEVNULL,
                          stdin=_subprocess.DEVNULL)
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


def _decide(query, criteria, instructions, temperature, base, token=""):
    """submit → 轮询 result → 返回 answers 或 None。"""
    body = {"states": [{
        "id": "route",
        "state": str(query),
        "questions": {"intent": {
            "type": "choice",
            "instructions": instructions,
            "criteria": criteria,
        }}}],
        "temperature": float(temperature or 1.0)}
    sub = _http_json(f"{base}/api/submit", body, timeout=20, token=token)
    rid = sub.get("request_id")
    if not rid:
        return None
    for _ in range(60):
        _time.sleep(1.0)
        res = _http_json(f"{base}/api/result/{rid}", timeout=10, token=token)
        if res.get("status") in ("done", "failed", "error"):
            if res.get("status") != "done":
                return None
            try:
                return res["result"]["states"][0]["answers"]["intent"]
            except Exception:
                return None
    return None


def _llm_fallback(query, intents, ctx, note=""):
    """Jev 服务不可用时的降级（用户提案 2026-09-22）：LLM 生成式意图分类（与内置 intent
    同款提示词/编号解析）。输出结构对齐判别式：conf=-1 标记降级模式（无真实置信度）、
    无分布——下游按 threshold 判定时 conf=-1 < 任何非零阈值 → 天然走 default（保守安全）。"""
    from workflow_node_api import get_llm
    names = [n for n, _d in intents]
    list_str = "\n".join(f"{i+1}. {n}" + (f"（{d}）" if d else "") for i, (n, d) in enumerate(intents))
    prompt = (f"判断用户输入属于下列哪个意图，只回复对应编号（数字），不要任何解释。\n"
              f"可选意图：\n{list_str}\n\n用户输入：{query}\n\n若无任何匹配，回复 0。")
    try:
        resp = get_llm(ctx, "").chat([{"role": "user", "content": prompt}])
        answer = (getattr(resp, "content", "") or "").strip()
    except Exception as e:
        return {"outputs": {"intent": "", "confidence": -1.0, "probabilities": {}, "raw": "",
                            "error": f"Jev 不可达且 LLM 降级也失败: {e}"}, "port": "default"}
    digits = "".join(ch for ch in answer if ch.isdigit())
    idx = None
    if digits:
        n = int(digits)
        if 1 <= n <= len(names):
            idx = n - 1
    if idx is None:
        for i, name in enumerate(names):
            if name and (name == answer or name in answer):
                idx = i
                break
    out = {"intent": names[idx] if idx is not None else "", "confidence": -1.0,
           "probabilities": {}, "raw": f"[llm_fallback{' · ' + note if note else ''}] {answer[:500]}",
           "error": ""}
    if idx is not None:
        return {"outputs": out, "port": f"branch_{idx}"}
    return {"outputs": out, "port": "default"}


def _handle_intent_nano(node: dict, ctx) -> dict:
    from workflow_node_api import resolve_input_params

    inputs = node.get("data", {}).get("inputs", {})
    params = resolve_input_params(inputs.get("inputParameters", []), ctx)
    intents = [(i.get("name", "") or "", (i.get("description") or "").strip())
               for i in inputs.get("intents", []) if (i.get("name") or "").strip()]
    query = params.get("query") or next((v for v in params.values() if v), "")
    try:
        temperature = float(params.get("temperature") or 1.0)
    except Exception:
        temperature = 1.0
    try:
        threshold = float(params.get("threshold") or 0.0)
    except Exception:
        threshold = 0.0

    if not intents or not str(query).strip():
        return {"outputs": {"intent": "", "confidence": 0.0, "probabilities": {}, "raw": "",
                            "error": "query/intents 为空"}, "port": "default"}
    base, token, is_local = _jev_target()
    if not _ensure_server(base, token, allow_launch=is_local):
        return _llm_fallback(query, intents, ctx, note=f"Jev 不可达（{base}）")

    criteria = {name: (desc or name) for name, desc in intents}
    instructions = str(params.get("instructions") or "判断输入文本属于哪个意图类别")
    ans = _decide(query, criteria, instructions, temperature, base, token)
    if not ans:
        return _llm_fallback(query, intents, ctx, note="nanojev 决策失败/超时")

    probs = ans.get("probabilities") or {}
    choice = str(ans.get("choice") or "")
    conf = float(probs.get(choice, 0.0) or 0.0)
    out = {"intent": choice if conf >= threshold else "",
           "confidence": round(conf, 4),
           "probabilities": {k: round(float(v), 4) for k, v in probs.items()},
           "raw": _json.dumps(ans, ensure_ascii=False)[:2000], "error": ""}
    if out["intent"]:
        idx = next((i for i, (name, _d) in enumerate(intents) if name == out["intent"]), None)
        if idx is not None:
            return {"outputs": out, "port": f"branch_{idx}"}
    return {"outputs": out, "port": "default"}


def agt_node():
    return {"type": "intent_nano", "label": "Intent·Nano", "handler": _handle_intent_nano, "catalog": _CATALOG}


# ===== 节点目录条目（list_workflow_nodes / query_workflow_node 动态聚合自插件声明）=====
_CATALOG = {"name": "意图识别·NanoJev（判别式加强版）",
            "desc": "本地 NanoJev 0.6B 决策模型做意图分类：一次前向输出候选概率分布（~1s、零 token 费用、"
                    "无编号解析歧义），输出 intent/confidence/probabilities，top1 低于阈值走 default（软拒识）。"
                    "出口与内置 Intent 同构（branch_N/default），可直接替换迁移。",
            "xml": "<!-- NanoJev 判别式意图路由：描述体直供模型判别 -->\n"
                   "<node id=\"160001\" type=\"intent_nano\" title=\"意图路由\">\n"
                   "  <in name=\"query\" ref=\"100001.user_message\"/>\n"
                   "  <in name=\"temperature\" type=\"number\">1.0</in>\n"
                   "  <in name=\"threshold\" type=\"number\">0.35</in>\n"
                   "  <intent name=\"code\">要求编写、修改、调试代码</intent>\n"
                   "  <intent name=\"search\">要求检索资料、查文档、搜索信息</intent>\n"
                   "  <intent name=\"chat\">闲聊、打招呼或常识问答</intent>\n"
                   "  <out name=\"intent\" type=\"string\"/>\n"
                   "  <out name=\"confidence\" type=\"number\"/>\n"
                   "  <out name=\"probabilities\" type=\"object\"/>\n"
                   "</node>\n"
                   "<!-- 出口：branch_0(code) branch_1(search) branch_2(chat) default(未命中/低置信) -->"}
