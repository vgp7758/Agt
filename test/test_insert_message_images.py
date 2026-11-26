"""插话（insert_message）带图入队（用户实锤 2026-11-26：回归修复）。

回归：autonomous 时代的 insert 帧只带 text、服务端 insert_message 分支也不读 images——
忙时插话的附图被悄悄丢弃（前端清了缩略图、后端只入队文字）。
现：前端随帧带 images；服务端入队通道把 data URL 落盘并转 <img> 标签随文本入队
（投影 _project_imgs 按 vision 门控展开），空闲兜底走原生多模态（images 直传 work_q）。
"""
import asyncio
import base64
import io
import json
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from PIL import Image  # noqa: E402

import server  # noqa: E402
import session as session_mod  # noqa: E402


def _png_data_url() -> str:
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), "red").save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


def _isolate(monkeypatch, tmp_path):
    """重定向注入态 + 图片落盘根：repo_images_dir 落在 <REPOS_DIR>/<repo_key>/images/
    （数据根下，不在 workspace 内）——不重定向会把测试图写进真实 ~/.agt/repos。"""
    monkeypatch.setattr(server, "_workspace", tmp_path)
    monkeypatch.setattr(session_mod, "REPOS_DIR", tmp_path / "repos")


def _written(tmp_path):
    return list((tmp_path / "repos").rglob("user_*.png"))


class _WS:
    def __init__(self):
        self.sent = []

    async def send_text(self, s):
        self.sent.append(json.loads(s))


class _Agent:
    def __init__(self):
        self.pending_messages = []
        self.session = types.SimpleNamespace(_current=None)

    def queue_user_message(self, text):
        self.pending_messages.append(text)


class _Q:
    def __init__(self):
        self.items = []

    def put(self, item):
        self.items.append(item)


def _run(monkeypatch, tmp_path, payload, *, busy, work_q=None, client=None):
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setattr(server, "_state", {"busy": busy, "answered": False})
    monkeypatch.setattr(server, "_work_q", work_q)
    ws, ag = _WS(), _Agent()
    asyncio.run(server._handle_user_input(ws, ag, json.dumps(payload), None, None, None, client))
    return ws, ag


def test_busy_with_image_queues_img_tag_and_writes_file(monkeypatch, tmp_path):
    ws, ag = _run(monkeypatch, tmp_path, {"action": "insert_message", "text": "看这个报错", "images": [_png_data_url()]},
                  busy=True)
    assert len(ag.pending_messages) == 1
    q = ag.pending_messages[0]
    assert q.startswith("看这个报错") and "<img" in q            # 文本 + <img> 标签入队
    files = _written(tmp_path)
    assert len(files) == 1                                      # data URL 已落盘到 repo images/
    assert files[0].name in q                                   # 标签引用的就是落盘文件名
    assert any("已入队" in m.get("text", "") for m in ws.sent)


def test_busy_image_only_text_empty(monkeypatch, tmp_path):
    _, ag = _run(monkeypatch, tmp_path, {"action": "insert_message", "text": "", "images": [_png_data_url()]}, busy=True)
    assert len(ag.pending_messages) == 1 and "<img" in ag.pending_messages[0]   # 纯图插话也可入队


def test_busy_text_only_unchanged(monkeypatch, tmp_path):
    _, ag = _run(monkeypatch, tmp_path, {"action": "insert_message", "text": "继续"}, busy=True)
    assert ag.pending_messages == ["继续"]                      # 无图：行为与原先一致（无多余空白）


def test_busy_empty_text_and_no_image_not_queued(monkeypatch, tmp_path):
    _, ag = _run(monkeypatch, tmp_path, {"action": "insert_message", "text": "", "images": []}, busy=True)
    assert ag.pending_messages == []


def test_idle_fallback_uses_native_multimodal(monkeypatch, tmp_path):
    q = _Q()
    _, ag = _run(monkeypatch, tmp_path,
                 {"action": "insert_message", "text": "看这个", "images": [_png_data_url()]}, busy=False, work_q=q)
    assert ag.pending_messages == []                            # 空闲：不入队
    assert q.items == [("user", "看这个", q.items[0][2])] and q.items[0][2][0].startswith("data:image/png;base64,")
    assert not _written(tmp_path)                               # 走原生多模态通道，不重复落盘


def test_subagent_target_gets_materialized_text(monkeypatch, tmp_path):
    sub = _Agent()
    entry = types.SimpleNamespace(agent=sub)
    reg = types.SimpleNamespace(lookup=lambda t: entry)
    ag_main = _Agent()
    ag_main.registry = reg
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setattr(server, "_state", {"busy": True, "answered": False})
    monkeypatch.setattr(server, "_work_q", None)
    ws = _WS()
    asyncio.run(server._handle_user_input(ws, ag_main, json.dumps(
        {"action": "insert_message", "text": "子任务补充", "images": [_png_data_url()]}),
        None, None, None, {"target": "worker-x"}))
    assert ag_main.pending_messages == []                       # 不误投主 Agent
    assert len(sub.pending_messages) == 1 and "子任务补充" in sub.pending_messages[0] and "<img" in sub.pending_messages[0]
    assert len(_written(tmp_path)) == 1                         # 附图落盘在隔离根下
