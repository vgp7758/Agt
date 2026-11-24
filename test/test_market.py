"""插件市场（marketplace）单测（plan p_1945c5a0 + 全局层缺口增补）。

覆盖：索引加载与完整性（条目字段/随包文件真实存在）、install 三通道
（manifest 唯一名转 download_asset / 多文件 / 目录型）、重复装跳过、
--force 覆盖、installed 态判定、uninstall（整目录删/空父目录清理/落位根保留）、
未知名与 HTTP API（GET /api/marketplace + POST install/uninstall）；
双层检测与全局装卸（installed_scope=repo/global/both、install --global、
uninstall 卸实际存在层、无全局约定类型拒装全局）。

install 落位全部隔离到 tmp_path；全局层根经 autouse fixture 注入 tmp_path
（monkeypatch market._GLOBAL_ROOT），不碰真实 ~/.agt 与 workspace。
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import pytest
from pathlib import Path

import market


@pytest.fixture(autouse=True)
def ghome(tmp_path, monkeypatch):
    """隔离全局层根：所有测试的全局读写都落在 tmp 下的假 ~/.agt。"""
    g = tmp_path / "home" / ".agt"
    g.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(market, "_GLOBAL_ROOT", g)
    return g


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


        # scope=global：装到全局层（隔离根），repo 层不动
        r = c.post("/api/marketplace/install",
                   json={"name": "reload_hot", "scope": "global"})
        assert r.status_code == 200 and r.json()["ok"] is True
        assert (market._GLOBAL_ROOT / "workflows" / "reload_hot.xml").exists()
        assert not (ws / ".agent/workflows/reload_hot.xml").exists()
        d = c.get("/api/marketplace").json()
        w = [x for x in d["items"] if x["name"] == "reload_hot"][0]
        assert w["installed_scope"] == "global"

        # uninstall 默认卸全部实际已装层
        r = c.post("/api/marketplace/uninstall", json={"name": "reload_hot"})
        assert r.json()["ok"] is True
        assert not (market._GLOBAL_ROOT / "workflows" / "reload_hot.xml").exists()

        # 非法 scope → ok=false
        assert c.post("/api/marketplace/install",
                      json={"name": "reload_hot", "scope": "nope"}).json()["ok"] is False
        r = c.post("/api/marketplace/uninstall", json={"name": "webide"})
        assert r.json()["ok"] is True
        assert not (ws / ".agent/plugins/webide").exists()

        assert c.post("/api/marketplace/install", json={}).json()["ok"] is False
        assert c.post("/api/marketplace/install", content=b"x").json()["ok"] is False

# ---------- 双层检测与全局装卸（缺口①②：~/.agt 全局层） ----------

def test_index_global_types():
    """全局映射约定：workflow/skill/plugin 有全局层，agent_decl/tool 无。"""
    assert market._GLOBAL_DIRS == {"workflow": "workflows", "skill": "skills",
                                   "plugin": "plugins"}


def test_global_layer_detection(ws, ghome):
    """全局播种的工作流（seed_scope=global 产物）→ 市场显示已装（global）。"""
    gwf = ghome / "workflows"
    gwf.mkdir(parents=True)
    (gwf / "reload_hot.xml").write_text("<wf/>", encoding="utf-8")
    items = {x["name"]: x for x in market.marketplace_items(workspace=ws)}
    assert items["reload_hot"]["installed"] is True
    assert items["reload_hot"]["installed_scope"] == "global"
    assert items["ccs"]["installed"] is False              # tool 无全局层约定
    assert items["ccs"]["installed_scope"] == ""
    # repo 层补装 → both
    market.install("reload_hot", workspace=ws)
    items = {x["name"]: x for x in market.marketplace_items(workspace=ws)}
    assert items["reload_hot"]["installed_scope"] == "both"


def test_install_global_workflow(ws, ghome):
    """--global：工作流落 ~/.agt/workflows/，repo 层不动。"""
    out = market.install("reload_hot", scope="global", workspace=ws)
    assert "全局" in out
    assert (ghome / "workflows" / "reload_hot.xml").exists()
    assert not (ws / ".agent/workflows/reload_hot.xml").exists()
    items = {x["name"]: x for x in market.marketplace_items(workspace=ws)}
    assert items["reload_hot"]["installed_scope"] == "global"


def test_install_global_skill_and_plugin_dir(ws, ghome):
    """--global：目录型（技能/插件）整目录落 ~/.agt/skills|plugins/<name>/。"""
    out = market.install("test-svc", scope="global", workspace=ws)
    assert (ghome / "skills" / "test-svc" / "SKILL.md").exists()
    assert not (ws / ".agent/skills/test-svc").exists()
    out = market.install("webide", scope="global", workspace=ws)
    assert (ghome / "plugins" / "webide" / "backend.py").exists()
    items = {x["name"]: x for x in market.marketplace_items(workspace=ws)}
    assert items["test-svc"]["installed_scope"] == "global"
    assert items["webide"]["installed_scope"] == "global"


def test_install_global_skip_existing_and_force(ws, ghome):
    out = market.install("reload_hot", scope="global", workspace=ws)
    (ghome / "workflows" / "reload_hot.xml").write_text("# user edit", encoding="utf-8")
    out = market.install("reload_hot", scope="global", workspace=ws)
    assert "未动" in out
    assert (ghome / "workflows" / "reload_hot.xml").read_text(encoding="utf-8") == "# user edit"
    out = market.install("reload_hot", scope="global", force=True, workspace=ws)
    assert (ghome / "workflows" / "reload_hot.xml").read_text(encoding="utf-8") != "# user edit"


def test_install_global_rejects_repo_only_types(ws, ghome):
    """agent_decl/tool 无全局约定 → 拒装全局，不落任何文件。"""
    for name in ("coder", "ccs"):
        out = market.install(name, scope="global", workspace=ws)
        assert "不支持" in out, out
    assert not (ghome / "agents").exists() and not (ghome / "tools").exists()
    assert not (ws / ".agent").exists()


def test_install_bad_scope(ws):
    assert "参数" in market.install("reload_hot", scope="home", workspace=ws)
    assert not (ws / ".agent").exists()


def test_uninstall_by_scope_and_both(ws, ghome):
    """uninstall：默认卸实际存在层（both 两层都卸）；scope 限层。"""
    market.install("reload_hot", workspace=ws)
    market.install("reload_hot", scope="global", workspace=ws)
    out = market.uninstall("reload_hot", scope="repo", workspace=ws)
    assert not (ws / ".agent/workflows/reload_hot.xml").exists()
    assert (ghome / "workflows" / "reload_hot.xml").exists()      # 全局层未动
    out = market.uninstall("reload_hot", workspace=ws)
    assert "global" in out and "已卸载" in out
    assert not (ghome / "workflows" / "reload_hot.xml").exists()

    market.install("reload_hot", workspace=ws)
    market.install("reload_hot", scope="global", workspace=ws)
    out = market.uninstall("reload_hot", workspace=ws)             # 默认 both 全卸
    assert "repo+global" in out
    assert not (ws / ".agent/workflows/reload_hot.xml").exists()
    assert not (ghome / "workflows" / "reload_hot.xml").exists()
    # 卸不存在的层 → 明确提示
    assert "未安装" in market.uninstall("reload_hot", scope="global", workspace=ws)
    assert "参数" in market.uninstall("reload_hot", scope="home", workspace=ws)


def test_uninstall_global_dir_cleans_nested_parent(ws, ghome):
    """全局目录型卸载整目录删；文件型空父目录清理止步全局落位根。"""
    market.install("test-svc", scope="global", workspace=ws)
    market.uninstall("test-svc", scope="global", workspace=ws)
    assert not (ghome / "skills" / "test-svc").exists()

# ---------- 层间转移 move_plugin + 本地插件清单（plan p_a8823fda） ----------

def _mk_plugin(root: Path, name: str, backend: str = "def setup(api):\n    pass\n") -> Path:
    """造一个最小有效插件（manifest.yml + backend.py + 一个应被忽略的 __pycache__）。"""
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "manifest.yml").write_text(
        f"name: {name}\nversion: 1.2.3\ndescription: 测试插件 {name}\n", encoding="utf-8")
    (d / "backend.py").write_text(backend, encoding="utf-8")
    pc = d / "__pycache__"
    pc.mkdir(exist_ok=True)
    (pc / "junk.pyc").write_bytes(b"\x00")
    return d


def test_move_plugin_repo_to_global(ws, ghome):
    _mk_plugin(ws / ".agent" / "plugins", "alpha")
    out = market.move_plugin("alpha", workspace=ws, to="global")
    assert "已转移" in out
    assert (ghome / "plugins/alpha/manifest.yml").exists()
    assert (ghome / "plugins/alpha/backend.py").exists()
    assert not (ghome / "plugins/alpha/__pycache__").exists()      # ignore 掉编译产物
    assert not (ws / ".agent/plugins/alpha").exists()              # 移动不是复制
    items = {x["name"]: x for x in market.marketplace_items(workspace=ws)}
    assert items["alpha"]["installed_scope"] == "global"


def test_move_plugin_global_to_repo(ws, ghome):
    _mk_plugin(ghome / "plugins", "beta")
    out = market.move_plugin("beta", workspace=ws, to="repo")
    assert "已转移" in out
    assert (ws / ".agent/plugins/beta/manifest.yml").exists()
    assert not (ghome / "plugins/beta").exists()
    items = {x["name"]: x for x in market.marketplace_items(workspace=ws)}
    assert items["beta"]["installed_scope"] == "repo"


def test_move_plugin_conflict_rejected_then_force(ws, ghome):
    """目标层同名：默认拒绝（[已存在]，源/目标都原样）；force 才用源这份覆盖。"""
    _mk_plugin(ws / ".agent/plugins", "gamma")
    _mk_plugin(ghome / "plugins", "gamma")
    (ws / ".agent/plugins/gamma/manifest.yml").write_text(
        "name: gamma\nversion: 9.9\n", encoding="utf-8")
    out = market.move_plugin("gamma", workspace=ws, to="global")
    assert out.startswith("[已存在]")
    assert (ws / ".agent/plugins/gamma").exists()                  # 源保持原样
    assert "9.9" not in (ghome / "plugins/gamma/manifest.yml").read_text(encoding="utf-8")
    out = market.move_plugin("gamma", workspace=ws, to="global", force=True)
    assert "已转移" in out
    assert "9.9" in (ghome / "plugins/gamma/manifest.yml").read_text(encoding="utf-8")
    assert not (ws / ".agent/plugins/gamma").exists()


def test_move_plugin_edge_cases(ws, ghome):
    assert market.move_plugin("nope", workspace=ws, to="global").startswith("[未找到]")
    assert "参数" in market.move_plugin("x", workspace=ws, to="home")
    _mk_plugin(ghome / "plugins", "onlyg")
    out = market.move_plugin("onlyg", workspace=ws, to="global")   # 源层（repo）没有
    assert out.startswith("⏭") and "未安装" in out
    assert (ghome / "plugins/onlyg/manifest.yml").exists()         # 原样未动


def test_move_plugin_failure_keeps_source(ws, ghome, monkeypatch):
    """转移中断（manifest 校验失败模拟）：[失败]、源保持原样、目标层无临时目录残留。
    校验桩只对落位临时目录（.moving- 名）抛错——源目录仍需可解析（否则扫描阶段就找不到）。"""
    import plugins as _p
    _mk_plugin(ws / ".agent/plugins", "delta")
    _real = _p._parse_manifest

    def _boom(d):
        if ".moving-" in d.name:
            raise ValueError("boom")
        return _real(d)

    monkeypatch.setattr(_p, "_parse_manifest", _boom)
    out = market.move_plugin("delta", workspace=ws, to="global")
    assert out.startswith("[失败]") and "boom" in out
    assert (ws / ".agent/plugins/delta/manifest.yml").exists()     # 源完好
    gplugs = ghome / "plugins"
    assert not gplugs.exists() or not any(gplugs.iterdir())        # 无 tmp 残留


def test_local_plugins_listed_and_dedup(ws, ghome):
    """非索引本地插件并入清单（source=local）；与索引同名不重复列。"""
    _mk_plugin(ws / ".agent/plugins", "locrepo")
    _mk_plugin(ghome / "plugins", "locglob")
    _mk_plugin(ws / ".agent/plugins", "webide")                    # 索引内同名
    items = {x["name"]: x for x in market.marketplace_items(workspace=ws)}
    a = items["locrepo"]
    assert a["local"] is True and a["source"] == "local" and a["installed"] is True
    assert a["installed_scope"] == "repo" and a["available"] is True
    assert a["type"] == "plugin" and a["type_label"] == "插件" and a["tags"] == ["本地"]
    assert items["locglob"]["installed_scope"] == "global"
    webides = [x for x in market.marketplace_items(workspace=ws) if x["name"] == "webide"]
    assert len(webides) == 1 and webides[0].get("local") is not True   # 索引条目优先


def test_api_marketplace_move(ws, monkeypatch):
    from fastapi.testclient import TestClient
    import server
    monkeypatch.setattr(server, "_workspace", ws)
    monkeypatch.setattr(server, "_agent", None)
    with TestClient(server.app) as c:
        _mk_plugin(ws / ".agent/plugins", "mv1")
        r = c.post("/api/marketplace/move", json={"name": "mv1", "to": "global"})
        d = r.json()
        assert r.status_code == 200 and d["ok"] is True
        assert (market._GLOBAL_ROOT / "plugins/mv1/manifest.yml").exists()
        assert "生效" in d["message"]                              # 无 agent → 提示手动 reload
        assert c.get("/api/marketplace").json()["local_count"] == 1

        # 冲突 → ok=false + conflict=true；force 重发成功
        _mk_plugin(ws / ".agent/plugins", "mv2")
        _mk_plugin(market._GLOBAL_ROOT / "plugins", "mv2")
        d = c.post("/api/marketplace/move", json={"name": "mv2", "to": "global"}).json()
        assert d["ok"] is False and d["conflict"] is True
        assert c.post("/api/marketplace/move",
                      json={"name": "mv2", "to": "global", "force": True}).json()["ok"] is True
        # 未知名 → ok=false（非 conflict）
        d = c.post("/api/marketplace/move", json={"name": "__x__"}).json()
        assert d["ok"] is False and not d.get("conflict")


class _FakeAgent:
    """reload_plugins 桩：load_plugins 只需要 agent 可赋给 _RT.agent（workspace 走参数）。"""


def test_api_marketplace_move_hot_reload(ws, monkeypatch):
    """_agent 在场：转移成功自动热重载，摘要以 🔁 附进 message。"""
    from fastapi.testclient import TestClient
    import server, plugins
    monkeypatch.setattr(server, "_workspace", ws)
    monkeypatch.setattr(server, "_agent", _FakeAgent())
    _mk_plugin(ws / ".agent/plugins", "hre")
    with TestClient(server.app) as c:
        d = c.post("/api/marketplace/move", json={"name": "hre", "to": "global"}).json()
        assert d["ok"] is True and "🔁" in d["message"]
        assert (market._GLOBAL_ROOT / "plugins/hre/manifest.yml").exists()
    plugins.unload_all()   # 清桩运行时，不污染后续用例


def test_move_plugin_same_layer_refused(ws, ghome, monkeypatch):
    """两层是同一目录（AGT_HOME 指到 <ws>/.agt）：移动=自删，必须拒绝且目录原样。"""
    same = ws / ".agt"
    (same / "plugins").mkdir(parents=True)
    monkeypatch.setattr(market, "_GLOBAL_ROOT", same)
    _mk_plugin(same / "plugins", "same1")
    out = market.move_plugin("same1", workspace=ws, to="global", force=True)
    assert out.startswith("⏭")
    assert (same / "plugins/same1/manifest.yml").exists()
    assert not any(p.name.startswith(".") for p in (same / "plugins").iterdir())


def test_move_plugin_skips_and_cleans_stale_tmp(ws, ghome):
    """中断残留的 .<名>.moving-* 不被当插件（清单不误报 both），下次转移同名时顺手清掉。"""
    _mk_plugin(ws / ".agent/plugins", "stale")
    gplugs = ghome / "plugins"
    stale = gplugs / ".stale.moving-9999"
    stale.mkdir(parents=True)                      # 残留：带合法 manifest 的隐藏目录
    (stale / "manifest.yml").write_text("name: stale\nversion: 0.1\n", encoding="utf-8")
    items = {x["name"]: x for x in market.marketplace_items(workspace=ws)}
    assert items["stale"]["installed_scope"] == "repo"          # 残留不算全局层
    assert not any(x["name"].startswith(".") for x in items.values())
    out = market.move_plugin("stale", workspace=ws, to="global")
    assert "已转移" in out
    assert not stale.exists()                                       # 残留已清
    assert sorted(p.name for p in gplugs.iterdir()) == ["stale"]   # 目录里只剩就位的插件


def test_move_plugin_force_restores_old_on_replace_failure(ws, ghome, monkeypatch):
    """force 覆盖：旧目标已挪开而新副本就位失败 → 旧目标放回原样、源完好、无残留。"""
    import os
    _mk_plugin(ws / ".agent/plugins", "omega")
    _mk_plugin(ghome / "plugins", "omega")
    old_text = (ghome / "plugins/omega/manifest.yml").read_text(encoding="utf-8")
    _real = os.replace

    def _boom(a, b, *args, **kw):
        fa = os.fspath(a)
        if ".moving-" in fa and ".moving-old-" not in fa:
            raise OSError("disk full")     # 只让「新副本 → 目标」这一步失败
        return _real(a, b, *args, **kw)

    monkeypatch.setattr(market.os, "replace", _boom)
    out = market.move_plugin("omega", workspace=ws, to="global", force=True)
    assert out.startswith("[失败]")
    assert (ghome / "plugins/omega/manifest.yml").read_text(encoding="utf-8") == old_text
    assert (ws / ".agent/plugins/omega/manifest.yml").exists()
    assert sorted(p.name for p in (ghome / "plugins").iterdir()) == ["omega"]


def test_move_plugin_src_removal_failure_reported(ws, ghome, monkeypatch):
    """就位后删源失败：以 ✅ 开头（调用方照常 reload）+ ⚠️ 提示手动清理；目标层完整。"""
    import shutil as _sh
    _mk_plugin(ws / ".agent/plugins", "theta")
    real_rmtree = _sh.rmtree
    src = (ws / ".agent/plugins/theta").resolve()

    def _boom(p, *a, **kw):
        if Path(_sh.os.fspath(p)).resolve() == src:
            raise PermissionError("in use")
        return real_rmtree(p, *a, **kw)

    monkeypatch.setattr(market.shutil, "rmtree", _boom)
    out = market.move_plugin("theta", workspace=ws, to="global")
    assert out.startswith("✅") and "⚠️" in out and "请手动删除" in out
    assert (ghome / "plugins/theta/manifest.yml").exists()
    assert (ws / ".agent/plugins/theta/manifest.yml").exists()      # 删不掉，如实保留
    assert not any(p.name.startswith(".") for p in (ws / ".agent/plugins").iterdir())
