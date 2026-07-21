"""download.py —— 随包资产下载（manifest 驱动）。

让用户 / Agent 显式获取随包资产（工作流 / mcp / 脚本 / ...）：
  - list 看清单 + 描述 + 是否已在本地
  - 按名下载到指定目录（默认该资产 default_dir）
  - 目标已存在默认不覆盖（--force 强制）

与 seed_default_workflows 互补：seed 是【隐式自动】播种（仅 workflows/*.xml，用户不可见不可控）；
本模块是【显式可控】（任意资产类型、看得见、可选目录、可指定覆盖）。两者都"已存在不覆盖"，不冲突。

资产清单在 src/assets/manifest.json（随包打进 wheel）。新增资产：放文件 + manifest 加一行。
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Optional

from tools import Tool

_PKG_ROOT = Path(__file__).resolve().parent  # site-packages/src/，manifest.src 相对于此
_MANIFEST = _PKG_ROOT / "assets" / "manifest.json"


def load_manifest() -> list[dict]:
    """读随包资产清单。返回 [{name,type,desc,src,default_dir}]。读失败返回 []。"""
    try:
        data = json.loads(_MANIFEST.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _default_ws() -> Path:
    """workspace 兜底（未传时用 real_tools.WORKSPACE）。lazy import 避免顶层重依赖。"""
    try:
        from real_tools import WORKSPACE
        return Path(WORKSPACE)
    except Exception:
        return Path.cwd()


def list_assets(workspace=None) -> list[dict]:
    """返回清单，每项补一个 exists 字段（是否已存在于本地 default_dir）。"""
    ws = Path(workspace) if workspace else _default_ws()
    out = []
    for a in load_manifest():
        dst = ws / a.get("default_dir", ".") / Path(a["src"]).name
        item = dict(a)
        item["exists"] = dst.exists()
        out.append(item)
    return out


def download_asset(name: str, target_dir: Optional[str] = None,
                   force: bool = False, workspace=None) -> str:
    """下载某资产。target_dir 留空=该资产 default_dir；已存在且非 force 跳过。返回结果文案。"""
    ws = Path(workspace) if workspace else _default_ws()
    assets = load_manifest()
    a = next((x for x in assets if x.get("name") == name), None)
    if not a:
        avail = ", ".join(x["name"] for x in assets) or "(清单为空)"
        return f"[未找到] 资产「{name}」不在清单。可用：{avail}"
    src = _PKG_ROOT / a["src"]
    if not src.exists():
        return f"[缺失] 随包文件不存在：{a['src']}（安装可能不完整）"
    rel_dir = target_dir or a.get("default_dir", ".")
    dst_dir = ws / rel_dir
    dst_dir.mkdir(parents=True, exist_ok=True)
    dst = dst_dir / src.name
    if dst.exists() and not force:
        try:
            where = dst.relative_to(ws)
        except Exception:
            where = dst
        return f"⏭ 已存在（未覆盖）：{where}\n  用 --force 覆盖，或指定其它目录。"
    shutil.copy2(src, dst)
    try:
        where = dst.relative_to(ws)
    except Exception:
        where = dst
    return f"✅ 已下载 {a['type']}「{name}」→ {where}"


def make_download_tools(agent) -> list:
    """Agent 自主用的下载工具（与用户命令 /download 同源）。"""

    def list_downloadable() -> str:
        """列出随包可下载资产（工作流/mcp/脚本），含名称/类型/描述/是否已在本地。
        需要某个随包能力时先看这个清单，再 download_asset(name) 取用。"""
        items = list_assets(workspace=agent.session.workspace)
        if not items:
            return "(无随包资产)"
        lines = [f"共 {len(items)} 项随包资产："]
        for a in items:
            mark = "✅已在本机" if a.get("exists") else "⬇可下载"
            lines.append(f"  [{mark}] {a['name']} ({a['type']}) — {a['desc']}")
        return "\n".join(lines)

    def download_asset(name: str, dir: str = "", force: bool = False) -> str:
        """下载某个随包资产到本地。name 见 list_downloadable；dir 留空=该资产默认目录；force=True 覆盖已有同名文件。"""
        # globals() 显式取模块级 download_asset，避免本闭包同名导致的局部化遮蔽。
        return globals()["download_asset"](name, target_dir=dir or None, force=force,
                                           workspace=agent.session.workspace)

    return [Tool(list_downloadable), Tool(download_asset)]
