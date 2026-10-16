"""market.py —— 插件市场（marketplace）：官方精选索引 + 安装/卸载。

两层结构（2026-10-17 用户提案）：
  ① 市场索引 src/assets/market/index.json（repo 内策展，随包分发）——
     每项含 name/type/desc/version/tags/source/file|dir/install_dir/installed 检测路径。
     收录：随包资产（工作流/子Agent声明）+ 插件 + 技能 + ccs 类工具，共 30 条。
  ② 安装通道：
     - 文件型且 manifest.json 唯一名（17 条工作流）→ 复用 download.download_asset
       （落位/已存在不覆盖/--force 语义与 /download 完全一致）；
     - 多文件（子 Agent 声明 md+yml）与目录型（插件/技能）→ 本模块统一复制
       （同语义：已存在跳过，force 覆盖）。
     卸载：只删索引条目声明的 installed 路径（不碰用户其它文件）。

与 seed（asset_sync / seed_default_skills）的分工：seed 是【隐式自动】首启播种；
本模块是【显式可控】市场——看得见清单、可安装、可卸载、可重装。两者都"已存在不覆盖"。
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

_PKG_ROOT = Path(__file__).resolve().parent          # site-packages/src/
_INDEX = _PKG_ROOT / "assets" / "market" / "index.json"

_TYPE_LABELS = {
    "workflow": "工作流",
    "agent_decl": "子Agent",
    "plugin": "插件",
    "skill": "技能",
    "tool": "工具",
}


def load_index() -> dict:
    """读市场索引。返回 {"updated":..., "items":[...]}；读失败返回 {"items": []}。
    兼容裸数组形态（与 assets/manifest.json 同款）。"""
    try:
        data = json.loads(_INDEX.read_text(encoding="utf-8"))
    except Exception:
        return {"updated": "", "items": []}
    if isinstance(data, list):
        return {"updated": "", "items": data}
    return {"updated": data.get("updated", ""), "items": data.get("items", []) or []}


def _ws_path(workspace, rel: str) -> Path:
    ws = Path(workspace) if workspace else Path.cwd()
    return ws / rel


def _is_installed(item: dict, workspace=None) -> bool:
    """installed 检测：声明的全部路径存在才算已装（目录型指向主文件更稳）。"""
    inst = item.get("installed")
    if not inst:
        return False
    paths = [inst] if isinstance(inst, str) else list(inst)
    return all(_ws_path(workspace, p).exists() for p in paths)


def _is_available(item: dict) -> bool:
    """随包文件是否在（wheel 安装不完整时可诊断）。"""
    if item.get("dir"):
        return (_PKG_ROOT / item["dir"]).is_dir()
    files = item.get("file")
    if not files:
        return False
    files = [files] if isinstance(files, str) else list(files)
    return all((_PKG_ROOT / f).exists() for f in files)


def marketplace_items(workspace=None) -> list[dict]:
    """市场清单：索引条目 + installed/available 运行时态（type_label 中文名同步给前端）。"""
    out = []
    for it in load_index()["items"]:
        item = dict(it)
        item["type_label"] = _TYPE_LABELS.get(item.get("type", ""), item.get("type", ""))
        item["installed"] = _is_installed(it, workspace)
        item["available"] = _is_available(it)
        out.append(item)
    return out


def _find(name: str) -> dict | None:
    return next((x for x in load_index()["items"] if x.get("name") == name), None)


def _manifest_unique(name: str) -> bool:
    """名字在随包 manifest.json 中恰好一条 → 可走 download_asset 通道。"""
    from download import load_manifest
    return sum(1 for a in load_manifest() if a.get("name") == name) == 1


def install(name: str, force: bool = False, workspace=None) -> str:
    """安装市场条目。文件型唯一名 → download_asset（复用 /download 通道）；
    多文件/目录型 → 统一复制。已存在且非 force 跳过。返回结果文案（多行）。"""
    item = _find(name)
    if not item:
        names = ", ".join(x["name"] for x in load_index()["items"])
        return f"[未找到] 市场没有「{name}」。/marketplace 查看清单（{names}）"
    if not _is_available(item):
        return f"[缺失] 随包文件不存在：{item.get('dir') or item.get('file')}（安装可能不完整）"
    if not force and _is_installed(item, workspace):
        return f"⏭ 「{name}」已安装（未动）。--force 可覆盖重装。"

    # ① 文件型 + manifest 唯一名 → 复用 download_asset 通道（17 条工作流走这）
    if not item.get("dir") and _manifest_unique(name):
        from download import download_asset
        return download_asset(name, force=force, workspace=workspace)

    ws = Path(workspace) if workspace else Path.cwd()
    lines = []
    if item.get("dir"):
        # ② 目录型（插件/技能）：整目录复制
        src = _PKG_ROOT / item["dir"]
        dst_dir = ws / item.get("install_dir", ".")
        dst = dst_dir / src.name
        if dst.exists() and not force:
            return f"⏭ 已存在（未覆盖）：{item.get('install_dir')}/{src.name}（--force 覆盖重装）"
        shutil.copytree(src, dst, dirs_exist_ok=bool(force))
        lines.append(f"✅ 已安装 {item['type']}「{name}」→ {item.get('install_dir')}/{src.name}/")
    else:
        # ③ 多文件（子 Agent 声明 md+yml 等）：逐文件复制
        files = item["file"]
        files = [files] if isinstance(files, str) else list(files)
        for f in files:
            src = _PKG_ROOT / f
            dst_dir = ws / item.get("install_dir", ".")
            dst_dir.mkdir(parents=True, exist_ok=True)
            dst = dst_dir / src.name
            if dst.exists() and not force:
                lines.append(f"⏭ 已存在（未覆盖）：{dst.name}")
                continue
            shutil.copy2(src, dst)
            lines.append(f"  → {item.get('install_dir')}/{dst.name}")
        lines.insert(0, f"✅ 已安装 {item['type']}「{name}」（{len(files)} 个文件）")
    lines.append(_after_install_hint(item))
    return "\n".join(lines)


def _after_install_hint(item: dict) -> str:
    """安装后生效提示（按类型给出热生效动作）。"""
    t = item.get("type")
    if t == "plugin":
        return "  生效：/reload plugins（改 backend.py 后同样）"
    if t == "workflow":
        return "  生效：/workflows reload（或下轮自动重扫）"
    if t == "agent_decl":
        return "  生效：即时（声明每次读取时生效）"
    if t == "skill":
        return "  生效：下一轮（技能清单每轮重扫）"
    if t == "tool":
        return "  生效：/reload tools（若为 agt_register 脚本工具）；独立脚本直接 python 运行"
    return ""


def uninstall(name: str, workspace=None) -> str:
    """卸载市场条目：目录型整目录删除；文件型只删声明的 installed 文件（不碰用户其它文件）。"""
    item = _find(name)
    if not item:
        return f"[未找到] 市场没有「{name}」。/marketplace 查看清单"
    if not _is_installed(item, workspace):
        return f"⏭ 「{name}」未安装（无需卸载）"
    if item.get("dir"):
        # 目录型（插件/技能/子Agent声明）：落位目录整删（installed 只列主文件会漏 tools/ 等随行物）
        src_name = Path(item["dir"]).name
        target = _ws_path(workspace, item.get("install_dir", ".")) / src_name
        if target.is_dir():
            shutil.rmtree(target)
        return f"🗑️ 已卸载「{name}」（{item.get('type', '')}）：\n  {target}"
    inst = item.get("installed")
    paths = [inst] if isinstance(inst, str) else list(inst)
    removed = []
    install_root = _ws_path(workspace, item.get("install_dir", "."))
    for p in paths:
        target = _ws_path(workspace, p)
        if target.is_dir():
            shutil.rmtree(target)
        elif target.exists():
            target.unlink()
        removed.append(p)
        # 清理因此变空的父目录（止步落位根目录，不越界）
        parent = target.parent
        while parent != install_root and install_root in parent.parents:
            try:
                parent.rmdir()          # 非空会抛错 → 自然终止
            except OSError:
                break
            parent = parent.parent
    return "🗑️ 已卸载「{}」（{}）：\n  {}".format(name, item.get("type", ""), "\n  ".join(removed))
