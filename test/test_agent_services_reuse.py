"""services 声明跨实例服务复用单测（plan p_922370a8 步骤 6，用户补充 2026-10-16）。

覆盖：
- _svc_spec：旧形态纯命令 / 新形态 "cmd | reuse=<URL>"（带不带空格均认）；
- _probe_alive 三态：2xx 活 / 4xx+image 特征活（防复用"别人的服务"）/ 连接拒绝死；
- _ensure_agent_services：探测活→跳过拉起（复用不拥有）；探测死→拉起；
  无 reuse 后缀→不探测直接拉起（旧声明零回归）；
- e2e 往返：真起假帧服务→装备（探测活→复用，start 未调）；杀掉→装备重拉（start 被调）。

隔离：fake agent（不碰真实服务池）；HTTP 服务起在随机高位端口、测试内即起即停。
"""
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import pytest
import multiagent as ma


class _FakeServices:
    def __init__(self):
        self.started = []

    def start(self, name, cmd, cwd, quiet_secs=0):
        self.started.append((name, cmd, cwd))
        return "started(fake)"


class _FakeAgent:
    def __init__(self, ws: Path):
        self.services = _FakeServices()
        self.session = SimpleNamespace(workspace=str(ws))


# ── ① _svc_spec 解析 ────────────────────────────────────

def test_svc_spec_old_form():
    assert ma._svc_spec("python tools/x.py 8765") == ("python tools/x.py 8765", "")


def test_svc_spec_reuse_form():
    cmd, url = ma._svc_spec("python tools/image_feed_poc.py 8765 | reuse=http://127.0.0.1:8765/frame")
    assert cmd == "python tools/image_feed_poc.py 8765"
    assert url == "http://127.0.0.1:8765/frame"
    # 无空格变体也认
    cmd2, url2 = ma._svc_spec("python x.py|reuse=http://a:1/f")
    assert (cmd2, url2) == ("python x.py", "http://a:1/f")


# ── ② _probe_alive 三态（真 socket）──────────────────────

@pytest.fixture
def fake_frame_server():
    """起一个假帧服务：/frame 返回 200+image/jpeg；/other 404+text/html。"""
    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/frame":
                self.send_response(200)
                self.send_header("Content-Type", "image/jpeg")
                self.end_headers()
                self.wfile.write(b"\xff\xd8\xff\xe0fake")
            else:
                self.send_response(404)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(b"<html>not found</html>")

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    port = srv.server_address[1]
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    yield f"http://127.0.0.1:{port}", srv
    srv.shutdown()


def test_probe_alive_2xx(fake_frame_server):
    base, _ = fake_frame_server
    ok, detail = ma._probe_alive(base + "/frame")
    assert ok and "200" in detail


def test_probe_4xx_html_is_not_ours(fake_frame_server):
    """4xx + text/html = 别人的服务，不复用（特征判据）。"""
    base, _ = fake_frame_server
    ok, detail = ma._probe_alive(base + "/other")
    assert not ok and "404" in detail


def test_probe_dead_connection():
    """连接拒绝 = 死（拉起）。"""
    ok, _ = ma._probe_alive("http://127.0.0.1:1/nope", timeout=0.5)
    assert ok is False


# ── ③ _ensure_agent_services 决策 ─────────────────────────

def _meta(url: str | None, cmd: str = "python tools/x.py 8765") -> dict:
    v = cmd + (f" | reuse={url}" if url else "")
    return {"services": [{"fake-svc": v}]}


def test_reuse_skips_start(tmp_path, monkeypatch, fake_frame_server):
    """探测活 → 跳过拉起（复用方不拥有服务）。"""
    base, _ = fake_frame_server
    ag = _FakeAgent(tmp_path)
    monkeypatch.setattr(ma, "_probe_alive", lambda u, timeout=2.0: (True, "HTTP 200"))
    ma._ensure_agent_services(_meta(base + "/frame"), ag)
    assert ag.services.started == []          # 不重复拉起


def test_dead_starts(tmp_path, monkeypatch):
    """探测死 → 拉起。"""
    ag = _FakeAgent(tmp_path)
    monkeypatch.setattr(ma, "_probe_alive", lambda u, timeout=2.0: (False, "ConnectionRefused"))
    ma._ensure_agent_services(_meta("http://127.0.0.1:1/frame"), ag)
    assert len(ag.services.started) == 1
    name, cmd, cwd = ag.services.started[0]
    assert name == "fake-svc" and cmd == "python tools/x.py 8765" and cwd == str(tmp_path)
    assert "reuse" not in cmd                 # 拉起的命令已剥离探测后缀


def test_no_reuse_no_probe(tmp_path, monkeypatch):
    """旧形态（无 reuse 后缀）不探测直接拉起——零回归。"""
    ag = _FakeAgent(tmp_path)
    called = []
    monkeypatch.setattr(ma, "_probe_alive", lambda *a, **k: called.append(1) or (True, "x"))
    ma._ensure_agent_services(_meta(None), ag)
    assert called == [] and len(ag.services.started) == 1


# ── ④ e2e 往返（真 HTTP 假帧服务）─────────────────────────

def test_e2e_reuse_then_restart(tmp_path, fake_frame_server):
    """真起假帧服务→装备复用（start 未调）；杀掉→装备重拉（start 被调）。"""
    base, srv = fake_frame_server
    ag = _FakeAgent(tmp_path)
    ma._ensure_agent_services(_meta(base + "/frame"), ag)
    assert ag.services.started == []                      # 活 → 复用
    srv.shutdown()                                        # 杀掉
    srv.server_close()                                    # 关监听 socket（shutdown 只停循环）
    import time
    for _ in range(40):                                   # 等端口真正释放（probe 死了才继续）
        ok, _detail = ma._probe_alive(base + "/frame", timeout=0.5)
        if not ok:
            break
        time.sleep(0.05)
    else:
        pytest.fail(f"假帧服务端口未按预期释放（最后探测：{_detail}）")
    ag2 = _FakeAgent(tmp_path)
    ma._ensure_agent_services(_meta(base + "/frame"), ag2)
    assert len(ag2.services.started) == 1                 # 死 → 重拉


# ── ⑤ desktop-operator 声明形态（extra_assemblies 迁移后，2026-10-16 p_76f6623b）──

def test_desktop_operator_svc_line_parses():
    """迁移后 yml 无 services/image_feed 段（画面帧收进 desktop-ops SKILL.md 装配声明，
    帧提供者=screen_frame 工具，不再拉 8765 帧服务）。行式解析语义（WebUI 兼容）用字面行保测。"""
    from agent_config import load_agent_yml
    root = Path(__file__).resolve().parent.parent
    meta, _ = load_agent_yml(root / ".agent" / "agents" / "desktop-operator" / "desktop-operator.yml")
    assert not meta.get("services"), "yml services 已收进技能装配（迁移后不声明）"
    cmd, url = ma._svc_spec("python x.py 8765 | reuse=http://127.0.0.1:8765/frame")
    assert cmd == "python x.py 8765" and url == "http://127.0.0.1:8765/frame"   # 行式解析语义不变
    skill_md = (root / ".agent" / "skills" / "desktop-ops" / "SKILL.md").read_text(encoding="utf-8")
    assert "server.py" not in skill_md and "services:" not in skill_md   # desktop-ops 纯工具形态（无服务）
    from agent import Agent
    from tools import Toolbox
    main = Agent(system="t", tools=Toolbox(), model_name=None, verbose=False)
    sk = ma._agent_skill_gear("desktop-operator", meta, main)
    assert sk["services"] == [] and sk["error"] == ""                     # 技能聚合 services 为空
