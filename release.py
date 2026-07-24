#!/usr/bin/env python3
"""release.py —— Agt 一键发布（替代手动 bump/build/upload/push）。

用法：
  python release.py              # 预览 + 确认后发布（patch 版本 +1，如 0.7.14 → 0.7.15）
  python release.py -y           # 跳过确认，直接一键发布
  python release.py -m "信息"    # 自定义 commit 信息（默认自动生成）
  python release.py minor        # minor 版本（0.7.x → 0.8.0）；写 major 同理
  python release.py 0.8.0        # 直接指定版本号
  python release.py --dry-run    # 只预览 + build，不 commit / upload / push

流程（与历史手动发布一致）：
  bump 版本号 → git add src/ + .agent/workflows/（永不纳入 coze-studio 子模块）→
  commit → 清理并 python -m build → twine upload（~/.pypirc 已配 token，失败自动重试）→
  git push → 输出发布报告。
"""
from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent          # 脚本所在目录即仓库根（与 cwd 解耦）
INIT = ROOT / "src" / "__init__.py"
# 默认纳入发布的改动路径；coze-studio 子模块永不纳入（保持历史约定：留在本地不提交）
STAGE_PATHS = ["src", ".agent/workflows"]
EXCLUDE = "coze-studio"
PKG = "agt_agent"
PYPI_URL = "https://pypi.org/project/agt-agent"


def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    """在仓库根跑命令（捕获输出），不抛异常，返回结果。"""
    return subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)


def _must(proc: subprocess.CompletedProcess, what: str) -> subprocess.CompletedProcess:
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-8:]
        sys.exit(f"❌ {what} 失败：\n" + "\n".join(tail))
    return proc


def current_version() -> str:
    m = re.search(r'__version__\s*=\s*["\']([\d.]+)["\']', INIT.read_text(encoding="utf-8"))
    if not m:
        sys.exit("❌ 在 src/__init__.py 里没找到 __version__")
    return m.group(1)


def bump(ver: str, part: str) -> str:
    x, y, z = (int(p) for p in ver.split("."))
    return {"major": f"{x + 1}.0.0", "minor": f"{x}.{y + 1}.0"}.get(part, f"{x}.{y}.{z + 1}")


def write_version(ver: str):
    t = INIT.read_text(encoding="utf-8")
    INIT.write_text(re.sub(r'__version__\s*=\s*["\'][\d.]+["\']', f'__version__ = "{ver}"', t),
                    encoding="utf-8")


def stage() -> list[str]:
    """git add 默认路径 + 防御性 unstage coze-studio；返回已暂存文件列表。"""
    for p in STAGE_PATHS:
        if (ROOT / p).exists():
            _must(_run(["git", "add", p]), f"git add {p}")
    _run(["git", "reset", "-q", "--", EXCLUDE])   # 防御：确保 coze-studio 不进暂存
    proc = _must(_run(["git", "diff", "--cached", "--name-only"]), "git diff --cached")
    return [l for l in proc.stdout.splitlines() if l.strip()]


def build() -> list[str]:
    """清理产物目录后 python -m build，返回 dist 产物文件名列表。"""
    for d in (ROOT / "dist", ROOT / "build"):
        shutil.rmtree(d, ignore_errors=True)
    for e in ROOT.glob("src/*.egg-info"):
        shutil.rmtree(e, ignore_errors=True)
    _must(_run([sys.executable, "-m", "build"]), "python -m build")
    return sorted(p.name for p in (ROOT / "dist").glob(f"{PKG}-*"))


def upload(ver: str, retries: int = 3):
    """twine upload，网络偶发超时时自动重试（upload.pypi.org 国内偶发读超时）。"""
    last = ""
    for i in range(1, retries + 1):
        proc = _run([sys.executable, "-m", "twine", "upload", f"dist/{PKG}-{ver}*"])
        if proc.returncode == 0:
            return
        last = (proc.stderr or proc.stdout or "").strip()
        print(f"   ⚠️ 上传失败（第 {i}/{retries} 次），重试…")
    sys.exit(f"❌ twine upload 连续 {retries} 次失败：\n{last.splitlines()[-5:]}")


def main() -> int:
    ap = argparse.ArgumentParser(description="Agt 一键发布脚本")
    ap.add_argument("part", nargs="?", default="patch",
                    help="patch(默认) / minor / major / 或直接给版本号如 0.8.0")
    ap.add_argument("-m", "--message", default="", help="自定义 commit 信息（默认自动生成）")
    ap.add_argument("-y", "--yes", action="store_true", help="跳过发布前确认")
    ap.add_argument("--dry-run", action="store_true", help="只预览 + build，不 commit/upload/push")
    a = ap.parse_args()

    cur = current_version()
    new = a.part if re.fullmatch(r"\d+\.\d+\.\d+", a.part) else bump(cur, a.part)
    print(f"📦 版本：{cur} → {new}")

    write_version(new)
    files = stage()
    others = [f for f in files if not f.endswith("__init__.py")]
    print(f"🗂  暂存 {len(files)} 个文件" + ("（仅版本号改动，无源码变化！）" if not others else "："))
    for f in files:
        print(f"      {f}")
    msg = a.message or f"chore(release): v{new}"

    if a.dry_run:
        arts = build()
        print("🧪 dry-run：仅构建不上传。产物：" + ", ".join(arts))
        write_version(cur)   # dry-run 不改版本号
        _run(["git", "reset", "-q"])  # 撤掉暂存，保持工作区干净
        print(f"   （版本号与暂存已还原，未提交任何东西）")
        return 0

    if not a.yes:
        if input("🚀 确认提交并发布到 PyPI？[y/N] ").strip().lower() not in ("y", "yes"):
            print("已取消（版本号改动与暂存仍在，可 git checkout -- src/__init__.py 还原）")
            return 1

    _must(_run(["git", "commit", "-q", "-m", msg]), "git commit")
    commit = _must(_run(["git", "rev-parse", "--short", "HEAD"]), "git rev-parse").stdout.strip()
    print(f"✅ 已提交 {commit}")

    arts = build()
    print(f"✅ 构建完成：{', '.join(arts)}")

    upload(new)
    print(f"✅ 已上传 PyPI：{PYPI_URL}/{new}/")

    old = _run(["git", "rev-parse", "--short", "origin/main"]).stdout.strip() or "?"
    _must(_run(["git", "push", "origin", "HEAD"]), "git push")
    newref = _must(_run(["git", "rev-parse", "--short", "HEAD"]), "git rev-parse").stdout.strip()

    print("\n========== 发布报告 ==========")
    print(f"  版本     {cur} → {new}")
    print(f"  提交     {commit}  {msg}")
    print(f"  文件     {len(files)} 个：" + ", ".join(files))
    print(f"  构建     {', '.join(arts)}")
    print(f"  PyPI     {PYPI_URL}/{new}/")
    print(f"  推送     origin main ({old}..{newref})")
    print("==============================")
    print("提示：pip 索引生效有 ~20s 延迟；pip install -U agt-agent 后重启生效。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
