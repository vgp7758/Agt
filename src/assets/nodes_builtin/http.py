"""HTTP 请求节点插件（type 45）：method/url/headers/params/body/auth。
URL/JSON 体支持 {{变量名}} 模板（输入参数引用）。
"""

PARAMS = [
    {"key": "method",   "type": "string", "required": True, "enum": ["GET", "POST", "PUT", "DELETE"],
     "desc": "HTTP 方法"},
    {"key": "url",      "type": "string", "required": True,
     "desc": "目标 URL；{{输入字段名}} 占位符"},
    {"key": "body",     "type": "object", "required": False,
     "desc": "POST/PUT 请求体：{bodyType: EMPTY|JSON|FORM_URLENCODED|RAW_TEXT, bodyData:{...}}"},
    {"key": "timeout",  "type": "number", "required": False, "default": 15,
     "desc": "超时秒数"},
]

import json

from workflow_node_api import resolve_input_params, resolve_value, render_template


def _handle_http(node: dict, ctx) -> dict:
    import urllib.parse
    import urllib.request
    import urllib.error

    inputs = node.get("data", {}).get("inputs", {})
    params = resolve_input_params(inputs.get("inputParameters", []), ctx)   # URL/body 的 {{变量名}} 引用这些输入
    api = inputs.get("apiInfo", {}) or {}
    method = (api.get("method") or "GET").upper()
    url = render_template(api.get("url", ""), params)

    kv = {}
    for p in inputs.get("params", []) or []:
        kv[p.get("name")] = resolve_value(p.get("input"), ctx)
    if kv:
        url += ("&" if "?" in url else "?") + urllib.parse.urlencode(kv, doseq=True)

    headers = {}
    for p in inputs.get("headers", []) or []:
        headers[p.get("name")] = str(resolve_value(p.get("input"), ctx))

    auth = inputs.get("auth") or {}
    if auth.get("authOpen") and auth.get("authType") == "bearer":
        for p in ((auth.get("authData") or {}).get("bearerTokenData") or []):
            if p.get("name") == "token":
                headers["Authorization"] = "Bearer " + str(resolve_value(p.get("input"), ctx))

    body = inputs.get("body") or {}
    bt = body.get("bodyType")
    bd = body.get("bodyData") or {}
    data = None
    if bt == "JSON":
        data = render_template(bd.get("json", ""), params).encode("utf-8")
        headers.setdefault("Content-Type", "application/json")
    elif bt == "RAW_TEXT":
        data = render_template(bd.get("rawText", ""), params).encode("utf-8")
        headers.setdefault("Content-Type", "text/plain")
    elif bt in ("FORM_DATA", "FORM_URLENCODED"):
        fields = bd.get("formURLEncoded") or (bd.get("formData", {}) or {}).get("data") or []
        form = {p.get("name"): str(resolve_value(p.get("input"), ctx)) for p in fields}
        data = urllib.parse.urlencode(form).encode("utf-8")
        headers.setdefault("Content-Type", "application/x-www-form-urlencoded")

    setting = inputs.get("setting") or {}
    timeout = int(setting.get("timeout") or 15)
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw_bytes = resp.read()
            code = resp.getcode()
            ctype = resp.headers.get("Content-Type", "")
            hdrs = json.dumps(dict(resp.headers.items()), ensure_ascii=False)
    except urllib.error.HTTPError as e:
        raw_bytes = e.read() if hasattr(e, "read") else str(e).encode()
        code = e.code
        ctype = ""
        hdrs = "{}"
    # 解码：优先 Content-Type 的 charset；utf-8 失败则回退 gbk（中文站点常见）
    charset = "utf-8"
    for _tok in ctype.split(";"):
        if _tok.strip().lower().startswith("charset="):
            charset = _tok.split("=", 1)[1].strip().strip('"')
    try:
        raw = raw_bytes.decode(charset) if isinstance(raw_bytes, (bytes, bytearray)) else str(raw_bytes)
    except (LookupError, UnicodeDecodeError):
        try:
            raw = raw_bytes.decode("gbk", errors="replace")
        except Exception:
            raw = str(raw_bytes)
    except Exception as e:
        return {"outputs": {"body": f"[HTTP 失败] {type(e).__name__}: {e}", "statusCode": 0, "headers": "{}"},
                "port": None}
    return {"outputs": {"body": raw, "statusCode": code, "headers": hdrs}, "port": None}


def agt_node():
    return {"type": "45", "label": "HTTP", "handler": _handle_http, "catalog": _CATALOG}

# ===== 节点目录条目（list_workflow_nodes / query_workflow_node 动态聚合自插件声明）=====
_CATALOG = {"name": "HTTP 请求 (HTTP)", "desc": "发起 HTTP 请求（GET/POST/PUT/DELETE），支持 headers/params/body/auth 配置，URL 和 body 中可用 {{}} 模板引用上游输出", "xml": "<!-- HTTP 请求节点 -->\n<node id=\"250001\" type=\"http\">\n  <!-- API 信息：method 和 url（url 中可用 {{变量}} 模板） -->\n  <param name=\"method\" literal=\"POST\">POST</param>\n  <param name=\"url\"><![CDATA[https://api.example.com/v1/chat/completions]]></param>\n\n  <!-- 请求头 -->\n  <param name=\"Content-Type\" literal=\"application/json\" header=\"true\">application/json</param>\n  <param name=\"Authorization\" header=\"true\"><![CDATA[Bearer {{api_key}}]]></param>\n\n  <!-- URL 查询参数 -->\n  <param name=\"version\" literal=\"v1\" query=\"true\">v1</param>\n\n  <!-- 模板入参 -->\n  <in name=\"api_key\" ref=\"190001.api_key\"/>\n  <in name=\"body_data\" ref=\"230001.output\"/>\n\n  <!-- 请求体（JSON body） -->\n  <param name=\"bodyType\" literal=\"json\">json</param>\n  <body><![CDATA[{{body_data}}]]></body>\n\n  <!-- 超时和重试 -->\n  <param name=\"timeout\" type=\"integer\">30</param>\n  <param name=\"retryTimes\" type=\"integer\">2</param>\n\n  <out name=\"body\" type=\"string\"/>\n  <out name=\"statusCode\" type=\"integer\"/>\n  <out name=\"headers\" type=\"object\"/>\n</node>\n<!--\n  header=\"true\" 的 param 作为请求头；query=\"true\" 的 param 作为 URL 查询参数\n  body 元素内的 CDATA 为请求体，支持 {{变量}} 模板\n  输出：body（响应体字符串）, statusCode（HTTP 状态码）, headers（响应头JSON对象）\n-->"}
