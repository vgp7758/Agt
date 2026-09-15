# -*- coding: utf-8 -*-
"""LLM API 反代：本机 http → 上游 https（给 SSH -R 反向隧道后面的无网容器用）
用法: python llm_relay.py            # 127.0.0.1:19999 → api.deepseek.com
      python llm_relay.py 19998 https://api-inference.modelscope.cn/v1  # 自定义
"""
import sys, asyncio
import httpx
from fastapi import FastAPI, Request
from fastapi.responses import Response
import uvicorn

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 19999
UPSTREAM = sys.argv[2] if len(sys.argv) > 2 else "https://api.deepseek.com"

app = FastAPI()
DROP = {"host", "content-length", "connection", "accept-encoding"}
client = httpx.AsyncClient(timeout=httpx.Timeout(600.0, connect=15.0))

@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"])
async def relay(path: str, request: Request):
    url = f"{UPSTREAM}/{path}"
    headers = {k: v for k, v in request.headers.items() if k.lower() not in DROP}
    body = await request.body()
    r = await client.request(request.method, url, headers=headers, content=body)
    rh = {k: v for k, v in r.headers.items() if k.lower() not in ("content-length", "transfer-encoding", "content-encoding", "connection")}
    return Response(r.content, r.status_code, headers=rh)

if __name__ == "__main__":
    print(f"[llm_relay] 127.0.0.1:{PORT} -> {UPSTREAM}")
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning")
