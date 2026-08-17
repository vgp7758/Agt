"""updater.py —— 启动时自动检查 PyPI 新版本并（全自动模式）后台 pip 升级。

策略（用户可配 auto_update 开关，默认开）：
- editable / 本地 / 未识别安装（开发仓库、源码直跑）→ 跳过（自动更新对它无意义）。
- 24h 内查过 → 用缓存的 latest，不重复请求 PyPI（`/update` 命令 force=True 绕过节流）。
- 有新版 + auto_update 开 → 后台 subprocess `pip install -U --no-input agt-agent`，
  打印「已升级 X→Y，重启 agt 生效」（当前进程内存仍是旧代码，新版本下次启动生效）。
- 网络 / pip 失败 → 静默或一行小字，绝不阻塞启动。

仅对终端用户的 `pip install agt-agent` 安装生效；editable 开发安装自动跳过。
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

PACKAGE = "agt-agent"
PYPI_URL = f"https://pypi.org/pypi/{PACKAGE}/json"
_AGT_DIR = Path.home() / ".agt"
_STATE_FILE = _AGT_DIR / "update.json"
_CHECK_INTERVAL = 24 * 3600   # 节流：两次 PyPI 查询最少间隔


# ========== 版本 ==========

def current_version() -> str:
    """已安装版本：importlib.metadata 是权威值；缺失时回退读 src/__init__.py。"""
    try:
        from importlib.metadata import version
        return version(PACKAGE)
    except Exception:
        pass
    try:
        here = Path(__file__).resolve().parent
        txt = (here / "__init__.py").read_text(encoding="utf-8")
        m = re.search(r'__version__\s*=\s*["\']([^"\']+)', txt)
        if m:
            return m.group(1)
    except Exception:
        pass
    return "0.0.0"


def _parse_ver(v: str):
    """PEP 440 比较；packaging 不可用时回退到整元组（够 0.9.5 这类比较）。"""
    try:
        from packaging.version import parse
        return parse(v)
    except Exception:
        nums = []
        for part in (v or "").split("."):
            d = re.findall(r"\d+", part)
            nums.append(int(d[0]) if d else 0)
        return tuple(nums)


def _is_newer(latest: str, current: str) -> bool:
    try:
        return _parse_ver(latest) > _parse_ver(current)
    except Exception:
        return False


# ========== 安装类型 ==========

def _dist_info_dir():
    """找 PACKAGE 的 .dist-info 目录（扫 sys.path，跨 Python 版本稳）。"""
    norm = PACKAGE.replace("-", "_")
    for sp in sys.path:
        if not sp:
            continue
        try:
            for d in sorted(Path(sp).glob(f"{norm}-*.dist-info")):
                if d.is_dir():
                    return d
        except Exception:
            continue
    return None


def install_kind() -> str:
    """安装类型：'editable' / 'local' / 'pypi' / 'unknown'。仅 'pypi' 才自动更新。

    - editable：PEP 610 direct_url.json 的 dir_info.editable=true（pip install -e）
    - local：direct_url.url=file://（本地路径安装）
    - pypi：http(s):// 来源（含镜像），或旧式无 direct_url 的 pip 安装
    - unknown：找不到 dist-info（源码直跑等）→ 不自动更新
    """
    d = _dist_info_dir()
    if d is None:
        return "unknown"
    du = d / "direct_url.json"
    if du.exists():
        try:
            info = json.loads(du.read_text(encoding="utf-8"))
            if info.get("dir_info", {}).get("editable"):
                return "editable"
            url = info.get("url") or ""
            if url.startswith("file://"):
                return "local"
            return "pypi"   # http(s):// PyPI / 镜像
        except Exception:
            pass
    return "pypi"   # 旧式安装无 direct_url，保守认为可更新


# ========== PyPI 查询 + 节流 ==========

def fetch_latest(timeout: float = 5):
    """GET PyPI JSON API 取最新正式版；失败返回 None。"""
    try:
        with urllib.request.urlopen(PYPI_URL, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8"))
        return data.get("info", {}).get("version")
    except Exception:
        return None


def _load_state() -> dict:
    try:
        return json.loads(_STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_state(d: dict):
    try:
        _AGT_DIR.mkdir(parents=True, exist_ok=True)
        _STATE_FILE.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def get_latest(force: bool = False, timeout: float = 5):
    """返回 PyPI 最新版（24h 节流，失败也缓存防刷；force=True 绕过节流）。"""
    st = _load_state()
    now = time.time()
    if not force and st.get("last_check") and now - st["last_check"] < _CHECK_INTERVAL:
        return st.get("latest")   # 缓存值（含 None=上次失败）
    latest = fetch_latest(timeout=timeout)
    _save_state({"last_check": int(now), "latest": latest})
    return latest


# ========== 升级 ==========

def do_upgrade():
    """pip install -U --no-input agt-agent。返回 (ok, msg)。"""
    cmd = [sys.executable, "-m", "pip", "install", "--upgrade", "--no-input", PACKAGE]

    def _run(c):
        return subprocess.run(c, capture_output=True, text=True, timeout=180,
                              encoding="utf-8", errors="replace",
                              creationflags=(subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0))

    try:
        proc = _run(cmd)
    except subprocess.TimeoutExpired:
        return False, "升级超时（>180s）"
    except Exception as e:
        return False, f"升级失败：{type(e).__name__}: {e}"
    if proc.returncode == 0:
        return True, "已升级"
    # 权限不足 → 加 --user 重试
    err = (proc.stderr or "") + (proc.stdout or "")
    if "permission" in err.lower() or "denied" in err.lower() or "read-only" in err.lower():
        try:
            p2 = _run(cmd + ["--user"])
            if p2.returncode == 0:
                return True, "已升级（--user）"
        except Exception:
            pass
    return False, f"pip 失败 rc={proc.returncode}：{err.strip()[-200:]}"


def _auto_update_enabled() -> bool:
    """读 ~/.agt/settings.json 的 auto_update（默认 True）。"""
    try:
        import config
        return bool(config.load_runtime_settings().get("auto_update", True))
    except Exception:
        return True


# ========== 编排 ==========

def check_and_update(*, force: bool = False, announce=print) -> dict:
    """检查并（auto 模式）升级。返回 {current, latest, status, msg}。

    status: skip(editable/local/unknown 跳过) / netfail(取不到最新版) / latest(已最新) /
            notify(有新版但 auto 关) / updated(已升级) / fail(升级失败)。
    force=True 时（/update 命令）绕过节流、且各分支都 announce；启动后台调用 force=False。
    """
    cur = current_version()
    kind = install_kind()
    if kind != "pypi":
        why = {"editable": "editable/开发安装", "local": "本地路径安装", "unknown": "未识别安装（可能源码直跑）"}.get(kind, kind)
        res = {"current": cur, "latest": None, "status": "skip",
               "msg": f"跳过自动更新（{why}；自动更新仅对 pip install 的终端用户生效）"}
        if force:
            announce(res["msg"])
        return res

    latest = get_latest(force=force)
    if latest is None:
        res = {"current": cur, "latest": None, "status": "netfail",
               "msg": "无法获取 PyPI 最新版（网络不通或需代理）"}
        if force:
            announce(res["msg"])
        return res

    if not _is_newer(latest, cur):
        res = {"current": cur, "latest": latest, "status": "latest", "msg": f"已是最新版 {cur}"}
        if force:
            announce(res["msg"])
        return res

    # 有新版
    if not _auto_update_enabled():
        res = {"current": cur, "latest": latest, "status": "notify",
               "msg": f"📦 有新版 {latest}（当前 {cur}）；自动更新已关闭，手动升级：pip install -U {PACKAGE}"}
        announce(res["msg"])
        return res

    ok, m = do_upgrade()
    if ok:
        res = {"current": cur, "latest": latest, "status": "updated",
               "msg": f"✅ 已升级 {cur} → {latest}；重启 agt 生效（当前进程仍是旧代码）"}
    else:
        res = {"current": cur, "latest": latest, "status": "fail",
               "msg": f"⚠️ 升级失败（{m}）；手动升级：pip install -U {PACKAGE}"}
    announce(res["msg"])
    return res


def start_background_check(announce=print) -> threading.Thread:
    """启动时后台 daemon 线程检查更新（不阻塞 REPL）；失败静默。"""
    def _run():
        try:
            check_and_update(force=False, announce=announce)
        except Exception:
            pass   # 更新检查任何异常都不影响启动

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return t
