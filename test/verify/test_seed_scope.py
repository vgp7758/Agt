# -*- coding: utf-8 -*-
"""seed_scope 单元验证（隔离目录，patch asset_sync.AGT_DIR，零生产污染）。"""
import sys, tempfile, shutil, json, re
from pathlib import Path
sys.path.insert(0, r"D:\AI_Usings\Agt\src")

import asset_sync
import workflow as wfmod
from workflow_xml import xml_to_canvas, canvas_to_xml

TMP = Path(tempfile.mkdtemp(prefix="seedscope_"))
FAKE_AGT = TMP / "agthome"; FAKE_AGT.mkdir()
asset_sync.AGT_DIR = str(FAKE_AGT)          # 模块级引用 → 播种/对比都落在隔离目录
wfmod.AGT_DIR = str(FAKE_AGT)                 # seeder 的全局层同样指向隔离目录

BUNDLED = Path(r"D:\AI_Usings\Agt\src\workflows")
GLOBALS = {"recap_gen.xml", "extract_keywords.xml", "before_turn_retrieval.xml",
           "rerank_topk.xml", "skill_suggest.xml"}
ok = lambda name, cond: print(("✓" if cond else "✗ FAIL"), name) or (cond or sys.exit(1))

# ── 1) is_global_seed_workflow 判别 ──
ok("5 个标记文件判 global", all(asset_sync.is_global_seed_workflow(BUNDLED / n) for n in GLOBALS))
ok("未标记判 repo 类", not asset_sync.is_global_seed_workflow(BUNDLED / "py_auto_diag.xml"))

# ── 2) 全新 workspace 播种：全局类只进全局层 ──
ws1 = TMP / "ws1"
n = wfmod.seed_default_workflows(ws1)
repo_wfs = {p.name for p in (ws1 / ".agent" / "workflows").glob("*.xml")}
glob_wfs = {p.name for p in (FAKE_AGT / "workflows").glob("*.xml")}
ok("repo 层无全局类", not (repo_wfs & GLOBALS))
ok("全局层有 5 个", GLOBALS <= glob_wfs)
ok("repo 层有非全局类（py_auto_diag 等）", "py_auto_diag.xml" in repo_wfs)
st = json.loads((ws1 / ".agent" / "seed_state.json").read_text(encoding="utf-8"))
ok("基线记录含全局类", any(k == "workflow/recap_gen.xml" for k in st))

# ── 3) 遮蔽副本清理：未改过→删；改过→留 ──
ws2 = TMP / "ws2"
d2 = ws2 / ".agent" / "workflows"; d2.mkdir(parents=True)
# 未改过的旧副本（与随包同 bytes）
(d2 / "recap_gen.xml").write_bytes((BUNDLED / "recap_gen.xml").read_bytes())
# 用户改过的副本
mod = (BUNDLED / "extract_keywords.xml").read_text(encoding="utf-8") + "\n<!-- 用户定制 -->"
(d2 / "extract_keywords.xml").write_text(mod, encoding="utf-8")
wfmod.seed_default_workflows(ws2)
ok("未改过的 repo 副本被移除", not (d2 / "recap_gen.xml").exists())
ok("用户改过的 repo 副本保留", (d2 / "extract_keywords.xml").exists())

# ── 4) /update-assets 三方对比：全局类以全局层为本地 ──
pkg = TMP / "pkg"; (pkg / "workflows").mkdir(parents=True)
(pkg / "workflows" / "my_global.xml").write_text(
    '<workflow name="mg" description="d" seed_scope="global"><node id="100001" type="start"/></workflow>', encoding="utf-8")
(pkg / "workflows" / "my_repo.xml").write_text(
    '<workflow name="mr" description="d"><node id="100001" type="start"/></workflow>', encoding="utf-8")
ws3 = TMP / "ws3"
items, summ = asset_sync.diff_seed_assets(ws3, pkg)
stat = {i["rel"]: i["status"] for i in items}
ok("全新对比：两个都 missing", stat["my_global.xml"] == "missing" and stat["my_repo.xml"] == "missing")
out = asset_sync.update_seed_assets(apply=True, workspace=ws3, pkg_root=pkg)
ok("apply 后：全局类落全局层", (FAKE_AGT / "workflows" / "my_global.xml").exists())
ok("apply 后：repo 类落 repo 层", (ws3 / ".agent" / "workflows" / "my_repo.xml").exists())
ok("repo 层没有全局类副本", not (ws3 / ".agent" / "workflows" / "my_global.xml").exists())

# ── 5) meta 捕获 + 编辑器 round-trip ──
xml = (BUNDLED / "recap_gen.xml").read_text(encoding="utf-8")
m = re.search(r'<workflow\b[^>]*seed_scope="global"', xml)
ok("随包根标记存在", bool(m))
scan = [it for it in wfmod.scan_workflows(ws1)]
rg = [it for it in scan if it["name"] == "recap_gen"]
ok("scan 捕获 seed_scope meta", rg and rg[0]["meta"].get("seed_scope") == "global")
canvas = xml_to_canvas(xml)
meta = dict(rg[0]["meta"])
out_xml = canvas_to_xml(canvas, meta)
ok("编辑器保存 round-trip 不丢标记", 'seed_scope="global"' in out_xml)

print("\n全部 12 项通过 ✅（隔离目录:", TMP, "）")
shutil.rmtree(TMP, ignore_errors=True)
