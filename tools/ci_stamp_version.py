"""ci_stamp_version.py —— CI 发布构建的版本戳（仅 Actions 调用，本地发布不走这里）。

用法：python tools/ci_stamp_version.py <ref_type> <ref_name>
  ref_type=tag / branch …（workflow_dispatch 时 GitHub 给的 ref_type=branch）
  是 tag → 把 src/paths.py 的 VERSION（唯一真源，__version__ 反向导入它）与
  packaging/version_file.txt 同步为 tag 名（去 v 前缀），保证 release 产物
  版本号 == tag 版本号。
  非 tag（手动触发试装）→ 不改，用仓库当前版本号。

Windows runner 的 stdout 默认 cp1252（Python 3.13）——中文 print 直接
UnicodeEncodeError 崩（实测）。统一 reconfigure utf-8，杜绝 CI 输出编码坑。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

# CI Windows runner 输出编码兜底：cp1252 → utf-8（中文日志防崩）
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

ROOT = Path(__file__).resolve().parent.parent
PATHS = ROOT / "src" / "paths.py"


def main() -> int:
    ref_type = sys.argv[1] if len(sys.argv) > 1 else ""
    ref_name = sys.argv[2] if len(sys.argv) > 2 else ""
    if ref_type != "tag":
        m = re.search(r'VERSION = "([\d.]+)"', PATHS.read_text(encoding="utf-8"))
        print(f"手动触发（ref_type={ref_type}）——沿用仓库版本号 {m.group(1) if m else '?'}")
        return 0
    ver = ref_name.lstrip("v")
    if not re.fullmatch(r"\d+\.\d+\.\d+", ver):
        print(f"⚠️ tag 名 {ref_name!r} 不是 x.y.z 形态，跳过版本戳（用仓库当前值）")
        return 0
    # 唯一真源是 paths.VERSION（src/__init__.py 反向导入它）；exe 版本资源由
    # packaging/version_file.txt 承担，CI 里由 workflow 的 stamp 步骤一并同步。
    t = PATHS.read_text(encoding="utf-8")
    new, n = re.subn(r'VERSION = "[\d.]+"', f'VERSION = "{ver}"', t)
    if n == 0:
        print("⚠️ paths.py 未匹配到 VERSION 字段，跳过")
        return 1
    PATHS.write_text(new, encoding="utf-8")
    print(f"✅ paths.py VERSION → {ver}（__version__ 随其同步）")
    vf = ROOT / "packaging" / "version_file.txt"
    if vf.exists():
        vt = vf.read_text(encoding="utf-8")
        nv, cnt = re.subn(r"0\.\d+\.\d+", ver, vt)
        if cnt:
            vf.write_text(nv, encoding="utf-8")
            print(f"✅ version_file.txt → {ver}（{cnt} 处）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
