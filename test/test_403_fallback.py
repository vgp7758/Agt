"""复现验证：403 PermissionDeniedError 是否触发回退链（而不是炸轮）。
mock 两个 provider：当前模型恒 403（余额不足），回退链下一个正常返回。"""
import sys, os, json
sys.path.insert(0, r"D:\AI_Usings\Agt\src")
from unittest.mock import patch
from openai import PermissionDeniedError
import httpx
from llm_client import LLMClient, LLMResponse

def make_403():
    req = httpx.Request("POST", "https://router.flatkey.ai/v1/chat/completions")
    body = {"error": {"message": "Failed to pre-deduct quota, user remaining quota: $0.668, required: $3.116"}}
    resp = httpx.Response(403, request=req, json=body)
    return PermissionDeniedError(message=json.dumps(body), response=resp, body=body)

# 构造：model_name=fk-haiku（恒403），fallback_chain=["proxy"]（正常）
c = LLMClient(model_name="fk-ds-flash", fallback_chain=["proxy"], fallback_policy="reset")

calls = {"n": 0}
def fake_inner(self, messages, **kw):
    calls["n"] += 1
    if c.model_name == "fk-ds-flash":
        raise make_403()
    return LLMResponse(content=f"OK from {c.model_name}", reasoning="",
                       tool_calls=None, finish_reason="stop", usage={})

ok_resp = None; raised = None
with patch.object(LLMClient, "_chat_inner", fake_inner):
    try:
        ok_resp = c.chat([{"role": "user", "content": "hi"}], tools=None)
    except Exception as e:
        raised = e

print("attempts:", calls["n"])
print("final model:", c.model_name)
print("cooldown keys:", list(c._provider_cooldown.keys()))
print("raised:", type(raised).__name__ if raised else None, str(raised)[:120] if raised else "")
print("resp:", ok_resp.content if ok_resp else None)
assert raised is None, "403 不应再炸轮！"
assert ok_resp and ok_resp.content == "OK from proxy", "应已回退到 proxy"
assert "fk-ds-flash" in str(list(c._provider_cooldown.keys())), "403 provider 应进冷却"
print("\n✅ PASS：403 → 冷却 + 回退到链上下一个 provider，轮不再中断")
