"""agentid_tools.py —— ModelScope Agent 身份（Agent Identity Protocol）本地工具。

身份：Ed25519 密钥对 + AgentID（~/.agt/.agentid/modelscope/agents/agt/，官方目录约定）。
签发：私钥对 "agent_id|kid|audience|timestamp" 做 Ed25519 签名（base64url 无 padding），
POST {idp}/agent_id/token 换目标应用（audience）的短期 JWT —— 协议已与官方
agent-id-client-sdk 源码比对一致（2026-09-11 实测破解）。

依赖：cryptography + requests（引擎环境均有）。不随包播种（个人身份工具）。
/reload tools 热加载。
"""
from __future__ import annotations

import base64
import json
import os
import time
from pathlib import Path

import requests

_TOKEN_CACHE: dict[str, tuple[str, float]] = {}   # audience -> (jwt, expires_at)


def _agent_dir() -> Path:
    """多身份支持：读 AGT_AGENTID_NAME env（默认 agt）。目录 ~ 或 ~/.agent 两款约定都探测。"""
    name = os.environ.get("AGT_AGENTID_NAME", "").strip() or "agt"
    home = os.environ.get("AGT_HOME", "").strip()
    roots = [Path(home)] if home else [Path.home() / ".agt", Path.home() / ".agent"]
    for r in roots:
        d = r / ".agentid" / "modelscope" / "agents" / name
        if (d / "agent.json").exists():
            return d
    return roots[0] / ".agentid" / "modelscope" / "agents" / name


def _load_identity() -> dict:
    d = _agent_dir()
    meta_path = d / "agent.json"
    if not meta_path.exists():
        return {}
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    meta["_dir"] = str(d)
    meta["_has_key"] = (d / "private_key").exists()
    return meta


def _b64url(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def _sign_token_request(priv_bytes: bytes, agent_id: str, kid: str, audience: str, ts: int) -> str:
    """与官方 agent-id-client-sdk 的 Identity.sign_token_request 逐字节一致。"""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    sk = Ed25519PrivateKey.from_private_bytes(priv_bytes)
    msg = f"{agent_id}|{kid}|{audience}|{ts}".encode()
    return _b64url(sk.sign(msg))


def agentid_status() -> str:
    """查看本机 ModelScope Agent 身份（AgentID/密钥/身份目录）。无参数。
    返回身份元数据与就绪状态——用于确认身份配置是否完整、当前身份是谁。"""
    m = _load_identity()
    if not m:
        d = _agent_dir()
        return ("未找到 Agent 身份。初始化步骤：\n"
                f"  1) 生成密钥并注册（目录 {d}）：参考 "
                "https://www.modelscope.cn/docs/agents/agent-identity\n"
                "  2) 注册后把 agent_id 回填 agent.json")
    ok_key = m.get("_has_key")
    ok_id = bool(m.get("agent_id"))
    lines = [f"身份目录：{m['_dir']}",
             f"agent_id：{m.get('agent_id') or '（未注册/未回填）'}",
             f"kid：{m.get('kid')}  name：{m.get('name')}",
             f"idp_url：{m.get('idp_url')}",
             f"私钥：{'✅ private_key 就绪' if ok_key else '❌ 缺 private_key'}"]
    lines.append("签发就绪：" + ("✅ 可申请 JWT" if (ok_key and ok_id) else "❌ 缺配置（见上）"))
    return "\n".join(lines)


def agentid_get_token(audience: str) -> str:
    """向 ModelScope Agent 身份服务申请目标应用（audience）的短期 JWT 通行证。
    audience: 目标应用的 client_id（由该应用/竞技场运营方提供）。
    返回 JWT（作 Bearer Token 访问该应用；短期有效，本工具自动缓存并在到期前续签）。
    需先完成身份注册（agentid_status 检查就绪状态）。"""
    aud = str(audience or "").strip()
    if not aud:
        return "[错误] audience 不能为空（目标应用的 client_id）"
    m = _load_identity()
    agent_id, kid, idp = m.get("agent_id", ""), m.get("kid", ""), m.get("idp_url", "")
    if not (agent_id and kid and idp and m.get("_has_key")):
        return "[错误] 身份未就绪——先用 agentid_status 检查（需 agent_id + 私钥）"
    cached = _TOKEN_CACHE.get(aud)
    now = time.time()
    if cached and now < cached[1] - 60:
        return f"🎫 JWT（缓存，至 {time.strftime('%H:%M:%S', time.localtime(cached[1]))} 过期）：\n{cached[0]}"
    try:
        priv = (Path(m["_dir"]) / "private_key").read_bytes()
    except OSError as e:
        return f"[错误] 读私钥失败：{e}"
    ts = int(now)
    sig = _sign_token_request(priv, agent_id, kid, aud, ts)
    try:
        r = requests.post(f"{idp}/agent_id/token",
                          json={"agent_id": agent_id, "kid": kid, "audience": aud,
                                "timestamp": ts, "signature": sig}, timeout=20)
    except Exception as e:
        return f"[请求失败] {type(e).__name__}: {e}"
    if r.status_code == 404:
        return (f"[404] audience {aud!r} 不是已注册的互联应用——client_id 需向该应用运营方获取"
                f"（签名验证已通过，身份本身 OK）。响应：{r.text[:200]}")
    if r.status_code != 200:
        return f"[HTTP {r.status_code}] {r.text[:300]}"
    data = r.json()
    token = data.get("token") or data.get("jwt") or data.get("access_token") or ""
    if not token:
        return f"[响应无 token 字段] {json.dumps(data, ensure_ascii=False)[:300]}"
    # exp 从 JWT payload 读（无依赖解码）
    try:
        payload = json.loads(base64.urlsafe_b64decode(token.split(".")[1] + "=="))
        exp = payload.get("exp", now + 300)
    except Exception:
        exp = now + 300
    _TOKEN_CACHE[aud] = (token, exp)
    return (f"🎫 JWT 已签发（audience={aud}，至 {time.strftime('%H:%M:%S', time.localtime(exp))} 过期）：\n{token}")


def agt_register():
    return [
        {"name": "agentid_status", "func": agentid_status, "hidden": False, "version": 1,
         "params": {}},
        {"name": "agentid_get_token", "func": agentid_get_token, "hidden": False, "version": 1,
         "params": {"audience": "目标应用的 client_id（互联应用运营方提供）"}},
    ]
