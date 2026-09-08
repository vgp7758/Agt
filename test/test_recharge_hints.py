"""充值入口链路端到端验证：失败归类 → URL 提取 → last_failures → interrupted hints。"""
import sys, json
sys.path.insert(0, r"D:\AI_Usings\Agt\src")
from unittest.mock import patch
import httpx
from llm_client import (LLMClient, LLMResponse, _classify_err, _extract_url,
                        _recharge_url_for, PermissionDeniedError)

# ---------- 1. 归类 / URL 提取 ----------
fk_msg = ("Error code: 403 - {'error': {'message': 'Failed to pre-deduct quota, user remaining quota: "
          "$0.668, required pre-deduct quota: $3.116 Add credits at https://console.flatkey.ai/wallet to keep going'}}")
def mk403(msg):
    req = httpx.Request("POST", "https://router.flatkey.ai/v1/chat/completions")
    body = {"error": {"message": msg}}
    resp = httpx.Response(403, request=req, json=body)
    return PermissionDeniedError(message=json.dumps(body), response=resp, body=body)

assert _classify_err(mk403(fk_msg)) == "quota", "403+quota 关键词 → quota"
assert _extract_url(fk_msg) == "https://console.flatkey.ai/wallet", "应从消息提取 wallet 链接"
prov, url = _recharge_url_for("fk-ds-flash", "Error code: 403 - plain")
assert prov == "flatkey" and url == "https://console.flatkey.ai/wallet", f"preset 兜底应给 flatkey wallet: {prov},{url}"
print("1. 归类/URL 提取/preset 兜底 ✅")

# ---------- 2. 全链失败 → last_failures + agent hints ----------
c = LLMClient(model_name="fk-ds-flash", fallback_chain=["fk-cl-haiku-4-5-20251001"], fallback_policy="reset")
def fake_inner(self, messages, **kw):
    raise mk403(fk_msg)
raised = None
with patch.object(LLMClient, "_chat_inner", fake_inner):
    try:
        c.chat([{"role": "user", "content": "hi"}], tools=None)
    except RuntimeError as e:
        raised = e
assert raised is not None, "全链失败应抛 RuntimeError"
lf = c.last_failures
assert len(lf) == 2 and all(f["cls"] == "quota" for f in lf), f"两条 quota 失败: {json.dumps(lf, ensure_ascii=False)[:300]}"
assert all(f["url"] == "https://console.flatkey.ai/wallet" for f in lf)
print("2. 全链失败 → last_failures 2 条 quota + wallet URL ✅")

# ---------- 3. agent 侧 hints 提取（与 agent.py except 块同款逻辑）----------
hints = []
_seen = set()
for f in (getattr(c, "last_failures", None) or []):
    if f.get("cls") in ("quota", "auth") and f.get("url") and f["url"] not in _seen:
        _seen.add(f["url"])
        hints.append({"provider": f.get("provider") or f.get("model", ""), "model": f.get("model", ""),
                      "url": f["url"], "reason": (f.get("msg") or "")[:120]})
assert len(hints) == 1 and hints[0]["provider"] == "flatkey", f"按 URL 去重后 1 条 flatkey: {hints}"
print("3. hints 去重提取 → 1 个 flatkey 充值按钮 ✅")
print("\n全部 PASS")
