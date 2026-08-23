"""HTTP 请求节点插件（type 45）：method/url/headers/params/body/auth。
URL/JSON 体支持 {{变量名}} 模板（输入参数引用）。
"""
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
    return {"type": "45", "label": "HTTP", "handler": _handle_http}
