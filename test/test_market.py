"""插件市场（marketplace）单测（plan p_1945c5a0）。

覆盖：索引加载与完整性（条目字段/随包文件真实存在）、install 三通道
（manifest 唯一名转 download_asset / 多文件 / 目录型）、重复装跳过、
--force 覆盖、installed 态判定、uninstall（整目录删/空父目录清理/落位根保留）、
未知名与 HTTP API（GET /api/marketplace + POST install/uninstall）。

install 落位全部隔离到 tmp_path，不碰真实 workspace。
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import pytest
from pathlib import Path

import market


@pytest.fixture
def ws(tmp_path):
    """隔离 workspace（install/uninstall 只在 tmp 里落位）。"""
    d = tmp_path / "ws"
    d.mkdir()
    return d


# ---------- 索引完整性 ----------

def test_index_loads_and_fields():
    idx = market.load_index()
    items = idx["items"]
    assert len(items) >= 30
    types = {i["type"] for i in items}
    assert types == {"workflow", "agent_decl", "plugin", "skill", "tool"}
    for it in items:
        assert it.get("name") and it.get("type") and it.get("desc")
        assert it.get("source") == "builtin"
        assert it.get("file") or it.get("dir")
        assert it.get("install_dir") and it.get("installed")


def test_index_files_exist_in_pkg():
    """每条目的随包源（file/dir）必须真实存在——策展与仓库同步。"""
    for it in market.load_index()["items"]:
        if it.get("dir"):
            assert (market._PKG_ROOT / it["dir"]).is_dir(), it["name"]
        else:
            files = it["file"] if isinstance(it["file"], list) else [it["file"]]
            for f in files:
                assert (market._PKG_ROOT / f).exists(), (it["name"], f)


def test_index_names_unique():
    names = [i["name"] for i in market.load_index()["items"]]
    assert len(names) == len(set(names))


def test_marketplace_items_shape(ws):
    items = market.marketplace_items(workspace=ws)
    assert all("installed" in x and "available" in x and "type_label" in x for x in items)
    assert all(x["installed"] is False for x in items)   # 空 workspace 全部未装


# ---------- 安装三通道 ----------

def test_install_workflow_via_download_asset(ws):
    """工作流（manifest 唯一名）走 /download 同通道落位。"""
    out = market.install("reload_hot", workspace=ws)
    assert "已下载" in out or "已安装" in out
    assert (ws / ".agent/workflows/reload_hot.xml").exists()
    assert (ws / ".agent/workflows").is_dir()


def test_install_agent_decl_dir(ws):
    """子 Agent 声明目录型落位（与 seed_default_agents 同构：<name>/<name>.yml+md）。"""
    out = market.install("coder", workspace=ws)
    assert "已安装" in out
    assert (ws / ".agent/agents/coder/coder.yml").exists()
    assert (ws / ".agent/agents/coder/coder.md").exists()


def test_install_plugin_and_skill(ws):
    out = market.install("webide", workspace=ws)
    assert "已安装" in out
    assert (ws / ".agent/plugins/webide/backend.py").exists()
    out = market.install("test-svc", workspace=ws)
    assert "已安装" in out
    assert (ws / ".agent/skills/test-svc/SKILL.md").exists()


def test_install_tool_file(ws):
    out = market.install("ccs", workspace=ws)
    assert (ws / ".agent/tools/ccs.py").exists()


def test_install_skip_existing_and_force(ws):
    market.install("webide", workspace=ws)
    # 用户改动已装文件 → 重装默认不动
    (ws / ".agent/plugins/webide/backend.py").write_text("# user edit", encoding="utf-8")
    out = market.install("webide", workspace=ws)
    assert "已安装（未动）" in out or "未动" in out
    assert (ws / ".agent/plugins/webide/backend.py").read_text(encoding="utf-8") == "# user edit"
    # force 覆盖回随包版
    out = market.install("webide", force=True, workspace=ws)
    assert "已安装" in out
    assert (ws / ".agent/plugins/webide/backend.py").read_text(encoding="utf-8") != "# user edit"


def test_install_unknown(ws):
    out = market.install("__nope__", workspace=ws)
    assert "未找到" in out


def test_installed_state_after_install(ws):
    market.install("webide", workspace=ws)
    market.install("ccs", workspace=ws)
    items = {x["name"]: x for x in market.marketplace_items(workspace=ws)}
    assert items["webide"]["installed"] is True
    assert items["ccs"]["installed"] is True
    assert items["feedback"]["installed"] is False


# ---------- 卸载 ----------

def test_uninstall_dir_and_file(ws):
    market.install("coder", workspace=ws)
    market.install("desktop-operator", workspace=ws)
    market.install("reload_hot", workspace=ws)
    out = market.uninstall("coder", workspace=ws)
    assert "已卸载" in out
    assert not (ws / ".agent/agents/coder").exists()          # 目录型整删（含 md）
    assert (ws / ".agent/agents").is_dir()                    # 落位根保留
    assert (ws / ".agent/agents/desktop-operator").exists()   # 不误伤邻居
    out = market.uninstall("reload_hot", workspace=ws)
    assert not (ws / ".agent/workflows/reload_hot.xml").exists()
    assert (ws / ".agent/workflows").is_dir()


def test_uninstall_not_installed_and_unknown(ws):
    assert "未安装" in market.uninstall("webide", workspace=ws)
    assert "未找到" in market.uninstall("__nope__", workspace=ws)


def test_uninstall_cleans_empty_parent(ws):
    """文件型卸载后空父目录自动清理（止步落位根）。"""
    market.install("wiki-updater", workspace=ws)   # .agent/agents/wiki-updater/
    assert (ws / ".agent/agents/wiki-updater").exists()
    market.uninstall("wiki-updater", workspace=ws)
    assert not (ws / ".agent/agents/wiki-updater").exists()


# ---------- HTTP API ----------

def test_api_marketplace(ws, monkeypatch):
    from fastapi.testclient import TestClient
    import server
    monkeypatch.setattr(server, "_workspace", ws)
    with TestClient(server.app) as c:
        r = c.get("/api/marketplace")
        assert r.status_code == 200
        d = r.json()
        assert d["total"] >= 30 and d["installed_count"] == 0
        assert {i["type"] for i in d["items"]} == {"workflow", "agent_decl", "plugin", "skill", "tool"}

        r = c.get("/api/marketplace", params={"type": "plugin"})
        assert r.json()["total"] == 6

        r = c.post("/api/marketplace/install", json={"name": "webide"})
        assert r.status_code == 200 and r.json()["ok"] is True
        assert (ws / ".agent/plugins/webide/backend.py").exists()

        d = c.get("/api/marketplace", params={"type": "plugin"}).json()
        w = [x for x in d["items"] if x["name"] == "webide"][0]
        assert w["installed"] is True

        r = c.post("/api/marketplace/uninstall", json={"name": "webide"})
        assert r.json()["ok"] is True
        assert not (ws / ".agent/plugins/webide").exists()

        assert c.post("/api/marketplace/install", json={}).json()["ok"] is False
        assert c.post("/api/marketplace/install", content=b"x").json()["ok"] is False
