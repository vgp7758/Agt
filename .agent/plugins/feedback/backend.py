from fastapi import Request
"""feedback —— 插件后端：反馈元信息 + 提交两条路由（原 server.py WS action 的插件化搬迁）。
- POST /api/feedback/meta    → {kinds, contact, env}（下拉选项/作者联系方式/环境预览）
- POST /api/feedback/submit  → submit_feedback 结果文案（本地必落盘 ~/.agt/feedback/，可选 webhook 推送）
核心逻辑零复制：直接 import 引擎 feedback.py（同源 /feedback 命令与 Agent 工具）。"""


async def _meta(request: 'Request'):
    from feedback import VALID_KINDS, author_contact_str, _gather_env
    return 200, {"kinds": VALID_KINDS, "contact": author_contact_str(),
                 "env": _gather_env(None, agent=_AGENT())}


async def _submit(request: 'Request'):
    from feedback import submit_feedback
    try:
        body = await request.json()
    except Exception:
        body = {}
    msg = submit_feedback(str(body.get("kind") or "建议").strip(),
                          str(body.get("content") or "").strip(),
                          str(body.get("contact") or "").strip(),
                          env_info=None if body.get("include_env", True) else {},
                          agent=_AGENT())
    return 200, {"ok": msg.startswith("✅"), "text": msg}


def _AGENT():
    """主 Agent 只读引用（无实例时 None——feedback 侧 env 少个 model 字段而已）。"""
    import sys
    agt = sys.modules.get("plugins")
    return getattr(agt, "_RT", None) and agt._RT.agent


def setup(api):
    api.register_route("POST", "/api/feedback/meta", _meta)
    api.register_route("POST", "/api/feedback/submit", _submit)
    api.log("feedback 已装配（路由2）")
