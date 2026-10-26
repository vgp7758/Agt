# -*- coding: utf-8 -*-
"""视觉代看描述的 repo 级 SQLite 缓存（用户裁定 2026-10-27）。

~/.agt/repos/<repo_key>/img_desc_cache.db —— 跨 session / 跨重启共享（同 repo 的所有
session、子 Agent、image_feed 帧共用一份；repo_key 与 toollog/repos 目录同款公式）。

两条触发路径先查命中，miss 才调视觉模型（llm_client.describe_image_with_vision）并记录：
  ① read_file 等工具结果物化（agent._materialize_tool_result）
  ② image_feed 非视觉宿主投影组装（session._imgfeed_proxy_text）

缓存故障（文件锁/磁盘/损坏）一律吞掉返回 None——不挡主流程，最多多花一次视觉调用。
"""

import hashlib
import sqlite3
import threading
import time
from pathlib import Path

_LOCK = threading.Lock()   # 串行化连接开关（低频操作，进程内足够）


def _repo_key(workspace) -> str:
    """repo 定位键：绝对路径的 \\ / : 全替换为 -（与 toollog/repos 目录同款公式）。"""
    return str(Path(workspace or Path.cwd()).resolve()).replace("\\", "-").replace("/", "-").replace(":", "-")


def _db_path(workspace) -> Path:
    from paths import repos_root
    d = repos_root() / _repo_key(workspace)
    d.mkdir(parents=True, exist_ok=True)
    return d / "img_desc_cache.db"


def _conn(workspace):
    con = sqlite3.connect(_db_path(workspace), timeout=10)
    con.execute("CREATE TABLE IF NOT EXISTS img_desc "
                "(h TEXT PRIMARY KEY, model TEXT, descr TEXT, ts REAL)")
    return con


def hash_of(data_url: str) -> str:
    """缓存键：data URL 全文的 md5 前 16 位（不含 base64 本体，省空间；同图不同 mime 不撞）。"""
    return hashlib.md5((data_url or "").encode()).hexdigest()[:16]


def get_desc(workspace, h: str):
    """命中返回 (model, descr)；miss / 缓存故障返回 None。"""
    try:
        with _LOCK:
            con = _conn(workspace)
            try:
                row = con.execute("SELECT model, descr FROM img_desc WHERE h=?", (h,)).fetchone()
            finally:
                con.close()
        return (row[0], row[1]) if row else None
    except Exception:
        return None


def put_desc(workspace, h: str, model: str, descr: str):
    """记录一条（INSERT OR REPLACE；失败静默）。"""
    try:
        with _LOCK:
            con = _conn(workspace)
            try:
                con.execute("INSERT OR REPLACE INTO img_desc VALUES (?,?,?,?)",
                            (h, model, descr, time.time()))
                con.commit()
            finally:
                con.close()
    except Exception:
        pass
