"""feedback.py —— 用户反馈提交（本地落盘 + 飞书 webhook 推送）。

让用户（CLI / WebUI / Agent）一键提交反馈：
  - 总是落盘到 ~/.agt/feedback/<时间戳>_<类型>.json（兜底，绝不丢）
  - webhook 启用且配了 URL（飞书 incoming）时，组装交互卡片 POST 推送，实时到作者手机
  - enabled=false 时只落盘不上报（隐私可关，用户在 ~/.agt/feedback.json 改）

与 download.py 对称：纯函数 + 命令/工具/前端共用。
配置在 ~/.agt/feedback.json：{webhook_url, enabled}。webhook_url 留空则用随包 DEFAULT_WEBHOOK_URL。
"""
from __future__ import annotations

import json
import platform
import urllib.request
import urllib.error
from datetime import datetime
from pathlib import Path
from typing import Optional

from tools import Tool

_AGT_DIR = Path.home() / ".agt"
_FEEDBACK_DIR = _AGT_DIR / "feedback"
_FEEDBACK_CONFIG = _AGT_DIR / "feedback.json"

# 作者的飞书 incoming webhook（随包默认）。用户可在 ~/.agt/feedback.json 覆盖或 enabled:false 关闭。
# 发布前填入自己的飞书机器人 webhook：
#   https://open.feishu.cn → 自建应用 → 添加「机器人」→ 复制 webhook 地址
DEFAULT_WEBHOOK_URL = "https://open.feishu.cn/open-apis/bot/v2/hook/b2eb1a8e-3311-4cb3-b13c-3943837366b0"

# 作者联系方式（反馈流程里展示给用户，方便深入交流）。留空项不显示。
# 发布前填入：让用户提完反馈知道怎么直接找你（群/微信/邮箱/GitHub）。
AUTHOR_CONTACT = {
    "wechat_group": "",   # 群二维码图片链接 / 群号
    "wechat": "mrbrick123",         # 个人微信号
    "email": "vgp123@foxmail.com",
    "github": "github.com/vgp7758",
}
_CONTACT_LABEL = {"wechat_group": "微信群", "wechat": "微信", "email": "邮箱", "github": "GitHub"}


def author_contact_str() -> str:
    """把 AUTHOR_CONTACT 非空项拼成可读文本（反馈成功文案/弹框用）。全空返回 ''。"""
    parts = [f"{_CONTACT_LABEL[k]}：{v}" for k, v in AUTHOR_CONTACT.items() if v]
    return " · ".join(parts)


VALID_KINDS = ["bug", "建议", "问题", "赞美"]

# 飞书卡片 header 配色 / emoji（按反馈类型）
_KIND_COLOR = {"bug": "red", "建议": "blue", "问题": "orange", "赞美": "green"}
_KIND_EMOJI = {"bug": "🐞", "建议": "💡", "问题": "❓", "赞美": "❤️"}


def _agent_version() -> str:
    """读 src/__init__.py 的 __version__（文件解析，不依赖 import 机制，和 download 读 manifest 同思路）。"""
    try:
        init = Path(__file__).resolve().parent / "__init__.py"
        for line in init.read_text(encoding="utf-8").splitlines():
            s = line.strip()
            if s.startswith("__version__") and "=" in s:
                return s.split("=", 1)[1].strip().strip('"').strip("'")
    except Exception:
        pass
    return "unknown"


def load_feedback_config() -> dict:
    """读 ~/.agt/feedback.json。不存在/读失败返回默认 {webhook_url, enabled}。"""
    default = {"webhook_url": DEFAULT_WEBHOOK_URL, "enabled": True}
    try:
        if _FEEDBACK_CONFIG.exists():
            data = json.loads(_FEEDBACK_CONFIG.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return {**default, **data}
    except Exception:
        pass
    return default


def save_feedback_config(cfg: dict):
    """写 ~/.agt/feedback.json。"""
    _AGT_DIR.mkdir(parents=True, exist_ok=True)
    _FEEDBACK_CONFIG.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


def _gather_env(env_info: Optional[dict], agent=None) -> dict:
    """组装环境信息。显式 env_info 优先，否则现采（版本/OS/模型）。"""
    if env_info is not None:   # None=现采；传 dict（含空 {}）=按传入，{} 表示不带环境
        return dict(env_info)
    env = {"version": _agent_version(), "os": platform.platform()}
    if agent is not None:
        try:
            env["model"] = getattr(agent, "model_name", "")
        except Exception:
            pass
    return env


def _save_local(record: dict) -> Path:
    """落盘到 ~/.agt/feedback/<ts>_<kind>.json。返回路径。"""
    _FEEDBACK_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    kind = record.get("kind") or "feedback"
    # 只过滤路径非法字符（保留中文可读性），而非把非 ASCII 全转下划线
    _BAD = '<>:"/\\|?*'
    safe = "".join((c if c not in _BAD else "_") for c in kind).strip().rstrip(".") or "feedback"
    path = _FEEDBACK_DIR / f"{ts}_{safe}.json"
    path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _build_feishu_card(record: dict) -> dict:
    """把反馈记录组装成飞书交互卡片 payload。"""
    kind = record.get("kind") or "反馈"
    content = record.get("content") or "(空)"
    contact = record.get("contact") or "(未留)"
    env = record.get("env") or {}
    env_str = " · ".join(f"{k}={v}" for k, v in env.items()) or "(未知)"
    ts = record.get("time") or ""
    emoji = _KIND_EMOJI.get(kind, "💬")
    md = (f"**{emoji} {kind}**\n\n"
          f"{content}\n\n"
          f"---\n"
          f"**联系方式**：{contact}\n"
          f"**环境**：{env_str}")
    return {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": f"Agt 反馈 · {kind}"},
                "template": _KIND_COLOR.get(kind, "blue"),
            },
            "elements": [
                {"tag": "div", "text": {"tag": "lark_md", "content": md}},
                {"tag": "note",
                 "elements": [{"tag": "plain_text", "content": f"{ts} · 已存本地"}]},
            ],
        },
    }


def _post_feishu(webhook_url: str, payload: dict) -> tuple[bool, str]:
    """POST 到飞书 webhook。返回 (是否成功, 说明)。失败不抛。"""
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(webhook_url, data=data, method="POST",
                                headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=6) as resp:
            body = resp.read().decode("utf-8", "ignore")
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code}"
    except Exception as e:
        return False, type(e).__name__
    try:
        rj = json.loads(body)
        # 飞书成功：新接口 code=0；旧接口 StatusCode=0
        if rj.get("code") == 0 or rj.get("StatusCode") == 0:
            return True, "ok"
        return False, f"feishu code={rj.get('code')} msg={rj.get('msg')}"
    except Exception:
        return False, "resp parse"


def submit_feedback(kind: str, content: str, contact: str = "",
                    env_info: Optional[dict] = None, agent=None) -> str:
    """提交反馈。本地一定落盘；webhook 启用且配了 URL 才推送。返回结果文案。

    kind 不在 VALID_KINDS 时归为 '建议'；content 空则报错不写。
    """
    kind = kind if kind in VALID_KINDS else "建议"
    content = (content or "").strip()
    if not content:
        return "⚠️ 反馈内容不能为空"

    env = _gather_env(env_info, agent=agent)
    record = {
        "kind": kind,
        "content": content,
        "contact": (contact or "").strip(),
        "env": env,
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }

    # 1) 本地落盘（永远做）
    try:
        local_path = _save_local(record)
    except Exception as e:
        return f"❌ 本地保存失败：{type(e).__name__}: {e}"

    # 2) webhook 推送（可选）；成功文案末尾带作者联系方式（方便用户后续直接联系）
    tail = author_contact_str()
    suffix = f"\n  如需直接联系作者：{tail}" if tail else ""
    cfg = load_feedback_config()
    if not cfg.get("enabled", True):
        return f"✅ 已记录（仅本地，已关闭上报）：{local_path.name}{suffix}"
    url = cfg.get("webhook_url") or DEFAULT_WEBHOOK_URL
    if not url:
        return (f"✅ 已记录（仅本地，未配 webhook）：{local_path.name}\n"
                f"  作者：在 ~/.agt/feedback.json 填 webhook_url 即可实时收到。{suffix}")
    ok, note = _post_feishu(url, _build_feishu_card(record))
    if ok:
        return f"✅ 已记录并推送到飞书：{local_path.name}{suffix}"
    return (f"✅ 已记录（推送失败：{note}，已存本地）：{local_path.name}\n"
            f"  文件：{local_path}{suffix}")


def make_feedback_tools(agent) -> list:
    """Agent 自主用的反馈工具（与 /feedback 命令同源）。"""

    def submit_feedback(kind: str = "建议", content: str = "", contact: str = "") -> str:
        """提交一条用户反馈给作者（bug/建议/问题/赞美）。content 必填，contact 可选。
        用户表达不满、建议或赞美、且希望作者收到时，可用此工具代为提交。"""
        # globals() 显式取模块级 submit_feedback，避免本闭包同名遮蔽（同 download.py 手法）。
        return globals()["submit_feedback"](kind, content, contact, agent=agent)

    return [Tool(submit_feedback)]
