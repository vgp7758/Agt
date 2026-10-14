# -*- coding: utf-8 -*-
"""dashboard 冒烟·清理器：taskkill /T /F 杀掉启动器拉起的三进程树 + 删隔离运行时目录。"""
import json
import shutil
import subprocess
import sys
from pathlib import Path

SMOKE = Path(__file__).resolve().parent


def main(purge_dirs: bool = True) -> None:
    pf = SMOKE / "pids.json"
    if pf.exists():
        pids = json.loads(pf.read_text(encoding="utf-8"))
        for tag, pid in pids.items():
            r = subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                               capture_output=True, text=True)
            print(f"kill {tag}({pid}): rc={r.returncode} {(r.stderr or r.stdout).strip()[:80]}")
        pf.unlink(missing_ok=True)
    if purge_dirs:
        for d in list(SMOKE.glob("home_*")) + list(SMOKE.glob("ws_*")):
            shutil.rmtree(d, ignore_errors=True)
            print(f"rm {d.name}")
    for lg in SMOKE.glob("*.out.log"):
        lg.unlink(missing_ok=True)
    for lg in SMOKE.glob("*.err.log"):
        lg.unlink(missing_ok=True)
    print("cleanup done")


if __name__ == "__main__":
    main(purge_dirs="--keep-dirs" not in sys.argv)
