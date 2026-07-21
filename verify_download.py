"""verify_download.py —— /download 资产下载验证（临时 workspace，不污染真实库）。"""
import sys
import tempfile
import shutil
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from download import load_manifest, list_assets, download_asset, make_download_tools  # noqa: E402
from commands import build_default_registry  # noqa: E402
from session import _repo_hash, REPOS_DIR  # noqa: E402


def main():
    tmp = Path(tempfile.mkdtemp(prefix="agt_dl_test_"))
    print(f"临时 workspace: {tmp}")
    try:
        m = load_manifest()
        assert len(m) == 3, f"manifest 应有 3 项，实际 {len(m)}"
        print(f"[1] manifest {len(m)} 项: {[a['name'] for a in m]}")

        items = list_assets(workspace=tmp)
        assert all(not a["exists"] for a in items)
        print("[2] list_assets 全部 exists=False ✓")

        r = download_asset("cs_auto_diag", workspace=tmp)
        assert "已下载" in r and (tmp / ".agent/workflows/cs_auto_diag.xml").exists()
        print(f"[3] {r}")

        r2 = download_asset("cs_auto_diag", workspace=tmp)
        assert "已存在" in r2
        print(f"[4] {r2}")

        r3 = download_asset("cs_auto_diag", workspace=tmp, force=True)
        assert "已下载" in r3
        print(f"[5] {r3}  (--force)")

        r4 = download_asset("wiki_auto_query", target_dir="custom/dir", workspace=tmp)
        assert (tmp / "custom/dir/wiki_auto_query.xml").exists()
        print(f"[6] {r4}")

        r5 = download_asset("not_exist", workspace=tmp)
        assert "未找到" in r5
        print(f"[7] {r5}")

        class _S:  # noqa: E302
            workspace = tmp
        class _A:  # noqa: E302
            session = _S()
        tools = make_download_tools(_A())
        tnames = sorted(t.name for t in tools)
        assert tnames == ["download_asset", "list_downloadable"], tnames
        print(f"[8] Agent 工具名: {tnames}")
        print(f"[8b] list_downloadable():\n{next(t for t in tools if t.name == 'list_downloadable').run()}")

        reg = build_default_registry()
        for c in ("download", "memory", "logs"):
            assert c in reg._cmds, f"/help 缺 {c}"
        print(f"[9] /help 含 download/memory/logs ✓")
        print(f"    /download: {reg._cmds['download'][1]}")

        print("\n🎉 验证通过")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        h = _repo_hash(tmp)
        rd = REPOS_DIR / h
        if rd.exists():
            shutil.rmtree(rd, ignore_errors=True)
            print(f"(清理 ~/.agt/repos/{h})")


if __name__ == "__main__":
    main()
