"""zai_tools.py —— Z.AI（智谱 BigModel）web_search 本地工具。

端点：POST https://open.bigmodel.cn/api/coding/paas/v4/web_search
鉴权：复用 models.json 里已配置的 z.ai provider 的 api_token（WebUI 设置页配过
一次即可，无需单独配 env）。token 解析零引擎依赖（轻量读 ~/.agt/models.json，
与 kv_tools 同款三级路径语义：AGT_HOME env > ~/.agt）。
不随包播种（用户个人工具）——改完本文件用 /reload tools 热加载。
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import requests

_URL = "https://open.bigmodel.cn/api/coding/paas/v4/web_search"
_RECENCY = ("noLimit", "oneDay", "oneWeek", "oneMonth", "oneYear")


def _zai_token() -> str:
    """从 models.json 找智谱系（z.ai / glm-official 等 base_url 含 bigmodel.cn）的 token。
    兼容新旧两种结构：新版顶层 {"models": {provider: {...}}}（t561 两级重构）/ 旧版扁平。"""
    home = os.environ.get("AGT_HOME", "").strip()
    base = Path(home) if home else Path.home() / ".agt"
    try:
        data = json.loads((base / "models.json").read_text(encoding="utf-8"))
    except Exception:
        return ""
    if isinstance(data, dict) and isinstance(data.get("models"), dict):
        data = data["models"]          # 新版两级：下钻 provider 层
    for name, cfg in (data or {}).items():
        if not isinstance(cfg, dict):
            continue
        if name == "z.ai" or "bigmodel.cn" in str(cfg.get("base_url", "")):
            tok = cfg.get("api_token") or ""
            if isinstance(tok, str) and tok.strip():
                return tok.strip()
            if isinstance(tok, list) and tok:          # api_token 作为 list 的形态（实测）
                t0 = str(tok[0]).strip()
                if t0:
                    return t0
            toks = cfg.get("api_tokens") or []
            if isinstance(toks, list) and toks:
                return str(toks[0]).strip()
    return ""


def zai_web_search(query: str, count: int = 10, recency: str = "noLimit", domains: str = "") -> str:
    """用 Z.AI（智谱）联网搜索引擎搜索。适合需要实时信息的查询（新闻/版本号/文档更新/价格等），
    比 DuckDuckGo 在中文与国内技术内容上通常更准。query: 搜索词；count: 结果条数(1~30，默认10)；
    recency: 时效过滤 noLimit|oneDay|oneWeek|oneMonth|oneYear（默认 noLimit）；
    domains: 限定域名，逗号分隔（如 docs.python.org,github.com，留空不限）。
    需在设置里配过 z.ai 的 api_token（复用同一 key）。"""
    q = str(query or "").strip()
    if not q:
        return "[错误] query 不能为空"
    try:
        n = int(count)
    except (TypeError, ValueError):
        n = 10
    n = max(1, min(30, n))
    rec = str(recency or "noLimit").strip() or "noLimit"
    if rec not in _RECENCY:
        return f"[错误] recency 需为 {'/'.join(_RECENCY)}，收到 {rec!r}"
    token = _zai_token()
    if not token:
        return ("[错误] 未找到 Z.AI 的 api_token：请在 WebUI 设置里添加 z.ai 模型"
                "（base_url=https://open.bigmodel.cn/api/paas/v4 + 你的 key），"
                "本工具复用该 key，配一次即可。")
    payload = {
        "search_query": q,
        "search_engine": "search_std",
        "search_intent": False,
        "count": n,
        "search_recency_filter": rec,
    }
    dm = str(domains or "").strip()
    if dm:
        payload["search_domain_filter"] = dm
    try:
        r = requests.post(_URL, headers={"Authorization": f"Bearer {token}",
                                         "Content-Type": "application/json"},
                          json=payload, timeout=30)
    except Exception as e:
        return f"[请求失败] {type(e).__name__}: {e}"
    if r.status_code != 200:
        return f"[HTTP {r.status_code}] {r.text[:400]}"
    try:
        data = r.json()
    except Exception:
        return f"[响应非 JSON] {r.text[:400]}"
    items = (data.get("search_result") or data.get("results")
             or data.get("data", {}).get("search_result") if isinstance(data, dict) else None) or []
    if not items:
        return f"（无结果）原始响应：{json.dumps(data, ensure_ascii=False)[:400]}"
    out = [f"🔍 Z.AI 搜索「{q}」{len(items)} 条（recency={rec}"
           + (f"，domains={dm}" if dm else "") + "）："]
    for i, it in enumerate(items[:n], 1):
        if not isinstance(it, dict):
            continue
        title = it.get("title") or it.get("name") or "(无标题)"
        link = it.get("link") or it.get("url") or it.get("refer") or ""
        snip = (it.get("content") or it.get("snippet") or "").strip()
        if len(snip) > 200:
            snip = snip[:200] + "…"
        out.append(f"\n[{i}] {title}\n    {link}\n    {snip}" if link
                   else f"\n[{i}] {title}\n    {snip}")
    return "\n".join(out)
    return "\n".join(out)


def zai_web_reader(url: str) -> str:
    """用 Z.AI（智谱）web-reader 抓取网页正文。适合精读单个 URL（替代 open_url 的
    通用抓取——对部分站点/JS 渲染页更干净）。url: 目标网页地址。
    需在设置里配过 z.ai / glm-official（bigmodel.cn）的 api_token。"""
    u = str(url or "").strip()
    if not u:
        return "[错误] url 不能为空"
    token = _zai_token()
    if not token:
        return ("[错误] 未找到智谱系 api_token：请在设置里添加 z.ai 或 glm-official"
                "（base_url=open.bigmodel.cn），本工具复用其 key。")
    try:
        r = requests.post(
            "https://open.bigmodel.cn/api/coding/paas/v4/reader",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={"url": u}, timeout=45)
    except Exception as e:
        return f"[请求失败] {type(e).__name__}: {e}"
    if r.status_code != 200:
        return f"[HTTP {r.status_code}] {r.text[:400]}"
    try:
        data = r.json()
    except Exception:
        return f"[响应非 JSON] {r.text[:500]}"
    # 实测响应：{"reader_result": {"content":..., "title":..., "url":...}, "model":"web-reader"}
    node = (data.get("reader_result") or data.get("result")
            or data.get("web_reader_result")) if isinstance(data, dict) else None
    text = None; title = ""
    if isinstance(node, dict):
        title = node.get("title") or ""
        text = node.get("content") or node.get("text") or node.get("markdown")
    if not text:
        text = data.get("content") or data.get("text") if isinstance(data, dict) else None
    if not text:
        return f"（正文为空）原始响应：{json.dumps(data, ensure_ascii=False)[:400]}"
    return f"📄 {title or u}\n\n{str(text)[:6000]}"


def zai_file_parser(path: str, file_type: str = "") -> str:
    """用 Z.AI（智谱）同步解析本地文档并返回提取文本（PDF/Word/Excel/PPT/Markdown/TXT 等）。
    适合处理用户附带的文件：读入文本后可直接据此总结/改写/提取要点。
    path: 本地文件绝对或相对路径（需存在）；file_type: 文件类型，默认按扩展名自动识别
    （.pdf→pdf / .docx→docx / .xlsx→xlsx / .pptx→pptx / .md→md / .txt→txt），不确定时显式传。
    需在设置里配过 z.ai / glm-official 的 api_token。"""
    p = str(path or "").strip()
    if not p:
        return "[错误] path 不能为空"
    fp = Path(p)
    if not fp.is_file():
        return f"[错误] 文件不存在：{p}"
    ft = str(file_type or "").strip().lower()
    if not ft:
        ft = fp.suffix.lstrip(".").lower() or "txt"   # 按扩展名自动识别
    try:
        raw = fp.read_bytes()
    except OSError as e:
        return f"[读取失败] {type(e).__name__}: {e}"
    token = _zai_token()
    if not token:
        return ("[错误] 未找到智谱系 api_token：请在设置里添加 z.ai 或 glm-official"
                "（base_url=open.bigmodel.cn），本工具复用其 key。")
    try:
        # 实测：文件解析端点在 /api/paas/v4（不带 coding 前缀），必填 file_type
        r = requests.post(
            "https://open.bigmodel.cn/api/paas/v4/files/parser/sync",
            headers={"Authorization": f"Bearer {token}"},
            files={"file": (fp.name, raw)},
            data={"tool_type": "prime-sync", "file_type": ft}, timeout=90)
    except Exception as e:
        return f"[请求失败] {type(e).__name__}: {e}"
    if r.status_code != 200:
        return f"[HTTP {r.status_code}] {r.text[:400]}"
    try:
        data = r.json()
    except Exception:
        return f"[响应非 JSON] {r.text[:500]}"
    if not isinstance(data, dict):
        return f"（响应非对象）{str(data)[:300]}"
    if data.get("status") == "failed":
        return f"[解析失败] {data.get('message') or ''}"
    # 实测响应：{"status":"succeeded", "content": "...", "task_id":...}
    text = data.get("content") or data.get("file_content") or data.get("text")
    if not text and isinstance(data.get("result"), dict):
        text = data["result"].get("content") or data["result"].get("text")
    if not text:
        return f"（正文为空）原始响应：{json.dumps(data, ensure_ascii=False)[:400]}"
    return f"📄 {fp.name}（{ft}，{len(raw)} 字节）\n\n{str(text)[:12000]}"


def agt_register():
    return [
        {"name": "zai_web_search", "func": zai_web_search, "hidden": False, "version": 1,
         "params": {
             "query": "搜索关键词",
             "count": "结果条数 1~30，默认 10",
             "recency": "时效过滤：noLimit|oneDay|oneWeek|oneMonth|oneYear",
             "domains": "限定域名（逗号分隔，如 github.com，留空不限）",
         }},
        {"name": "zai_web_reader", "func": zai_web_reader, "hidden": False, "version": 1,
         "params": {"url": "要抓取的网页地址"}},
        {"name": "zai_file_parser", "func": zai_file_parser, "hidden": False, "version": 1,
         "params": {
             "path": "本地文件路径（PDF/Word/Excel/Markdown/代码等）",
             "file_type": "文件类型（默认按扩展名自动识别：pdf/docx/xlsx/pptx/md/txt）",
         }},
    ]
