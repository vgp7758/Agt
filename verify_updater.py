"""verify_updater.py —— 验证自动更新逻辑（mock 网络 + pip，无真实副作用）。

覆盖：版本比较(含 0.10>0.9 非字典序)、本仓库 editable→skip、决策(updated/notify/latest/netfail)、
24h 节流(force=False 不重复请求、force=True 绕过)。
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

import updater  # noqa: E402

passed, failed = [], []


def check(n, c, e=""):
    (passed if c else failed).append(n)
    print(("  ✅ " if c else "  ❌ ") + n + (f"  ({e})" if e and not c else ""))


# ===== 1) 版本比较 =====
check("newer 0.9.6>0.9.5", updater._is_newer("0.9.6", "0.9.5"))
check("equal 不算新", not updater._is_newer("0.9.5", "0.9.5"))
check("0.10.0>0.9.9 (非字典序)", updater._is_newer("0.10.0", "0.9.9"), "字典序会判错")
check("1.0.0>0.9.9", updater._is_newer("1.0.0", "0.9.9"))
check("降级不算新", not updater._is_newer("0.9.4", "0.9.5"))

# ===== 2) 本仓库 editable → 跳过 =====
kind = updater.install_kind()
check("本仓库 install_kind 非 pypi(应跳过)", kind != "pypi", f"kind={kind}")
res = updater.check_and_update(force=True, announce=lambda m: None)
check("editable 安装 → status=skip", res["status"] == "skip", res["status"])

# ===== 3) 决策逻辑（mock fetch_latest + do_upgrade + install_kind=pypi）=====
orig = (updater.fetch_latest, updater.do_upgrade, updater.install_kind, updater._auto_update_enabled)
calls = {"fetch": 0, "upgrade": 0}


def mk_fetch(ret):
    def _f(*a, **k):
        calls["fetch"] += 1
        return ret
    return _f


def mk_upgrade(ok):
    def _f():
        calls["upgrade"] += 1
        return (ok, "ok" if ok else "boom")
    return _f


updater.install_kind = lambda: "pypi"
updater._save_state({"last_check": 0, "latest": None})   # 清缓存，force 也走 fetch
try:
    # 有新版 + auto 开 → 升级
    updater.fetch_latest = mk_fetch("9.9.9")
    updater._auto_update_enabled = lambda: True
    updater.do_upgrade = mk_upgrade(True)
    calls["upgrade"] = 0
    r = updater.check_and_update(force=True, announce=lambda m: None)
    check("有新版+auto开 → updated", r["status"] == "updated" and calls["upgrade"] == 1, r["status"])

    # 有新版 + auto 关 → 只提示不升级
    updater._auto_update_enabled = lambda: False
    calls["upgrade"] = 0
    r = updater.check_and_update(force=True, announce=lambda m: None)
    check("有新版+auto关 → notify 不升级", r["status"] == "notify" and calls["upgrade"] == 0, r["status"])

    # 已是最新
    updater.fetch_latest = mk_fetch(updater.current_version())
    r = updater.check_and_update(force=True, announce=lambda m: None)
    check("已是最新 → latest", r["status"] == "latest", r["status"])

    # 网络失败
    updater.fetch_latest = mk_fetch(None)
    r = updater.check_and_update(force=True, announce=lambda m: None)
    check("取不到最新版 → netfail", r["status"] == "netfail", r["status"])

    # 升级失败 → fail
    updater.fetch_latest = mk_fetch("9.9.9")
    updater._auto_update_enabled = lambda: True
    updater.do_upgrade = mk_upgrade(False)
    r = updater.check_and_update(force=True, announce=lambda m: None)
    check("升级失败 → fail", r["status"] == "fail", r["status"])
finally:
    (updater.fetch_latest, updater.do_upgrade, updater.install_kind, updater._auto_update_enabled) = orig
    updater._save_state({"last_check": 0, "latest": None})

# ===== 4) 节流 =====
calls["fetch"] = 0
updater.fetch_latest = mk_fetch("9.9.9")
updater.install_kind = lambda: "pypi"
updater._save_state({"last_check": int(time.time()), "latest": "9.9.9"})   # 刚查过
try:
    updater.get_latest(force=False)
    check("节流：24h 内 force=False 不发请求", calls["fetch"] == 0, f"fetch {calls['fetch']} 次")
    updater.get_latest(force=True)
    check("节流：force=True 绕过", calls["fetch"] >= 1, f"fetch {calls['fetch']} 次")
finally:
    (updater.fetch_latest, updater.install_kind) = (orig[0], orig[2])
    updater._save_state({"last_check": 0, "latest": None})

print(f"\n{'='*40}\n通过 {len(passed)} / 失败 {len(failed)}")
if failed:
    print("失败：", failed)
    sys.exit(1)
print("全部通过 ✅")
