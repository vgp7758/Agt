"""verify_download.py —— /download 资产下载验证（临时 workspace，不污染真实库）。

验证 load_manifest / list_assets / download_asset 的核心路径（force/target_dir/未找到）
以及 /download 命令在默认注册表里。Agent 工具装配的外置校验见 verify_assembly.py。
"""
import sys
import tempfile
import shutil
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from download import load_manifest, list_assets, download_asset  # noqa: E402
from commands import build_default_registry  # noqa: E402
from session import _repo_hash, REPOS_DIR  # noqa: E402


def main():
    tmp = Path(tempfile.mkdtemp(prefix="agt_dl_test_"))
    print(f"临时 workspace: {tmp}")
    try:
        m = load_manifest()
        assert len(m) >= 1, f"manifest 应非空，实际 {len(m)}"
        preview = [a["name"] for a in m[:6]]
        print(f"[1] manifest {len(m)} 项: {preview}{'…' if len(m) > 6 else ''}")

        items = list_assets(workspace=tmp)
        assert all(not a["exists"] for a in items)
        print("[2] list_assets 全部 exists=False ✓")

        # 取一个真实存在的 workflow 条目做下载往返
        wf = next((a for a in m if a.get("type") == "workflow"), None)
        assert wf, "manifest 无 workflow 条目，无法测下载"
        name = wf["name"]
        r = download_asset(name, workspace=tmp)
        assert "已下载" in r, r
        print(f"[3] {r}")

        r2 = download_asset(name, workspace=tmp)
        assert "已存在" in r2, r2
        print(f"[4] {r2}")

        r3 = download_asset(name, workspace=tmp, force=True)
        assert "已下载" in r3, r3
        print(f"[5] {r3}  (--force)")

        r4 = download_asset(name, target_dir="custom/dir", workspace=tmp)
        assert "custom" in r4 and (tmp / "custom" / "dir" / wf["src"].split("/")[-1]).exists(), r4
        print(f"[6] {r4}")

        r5 = download_asset("not_exist_xyz", workspace=tmp)
        assert "未找到" in r5, r5
        print(f"[7] {r5}")

        reg = build_default_registry()
        for c in ("download", "memory", "logs"):
            assert c in reg._cmds, f"/help 缺 {c}"
        print(f"[8] /help 含 download/memory/logs ✓")
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
