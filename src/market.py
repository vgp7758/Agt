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

     双层感知（2026-10-17 缺口修复）：workflow/skill/plugin 有用户级全局层
     ~/.agt/<dir>/（seed_scope=global 播种的工作流、全局技能/插件）；
     installed_scope 返回 repo/global/both/空串，install(..., scope="global")
     落 ~/.agt/<dir>/，uninstall 卸实际存在的层（both 两层都卸）。
     agent_decl/tool 无全局约定（repo-only）。

与 seed（asset_sync / seed_default_skills）的分工：seed 是【隐式自动】首启播种；
本模块是【显式可控】市场——看得见清单、可安装、可卸载、可重装。两者都"已存在不覆盖"。
"""
from __future__ import annotations

import json
import os
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
    """installed 检测（兼容包装）：任一层（repo/全局）齐全即视为已装。"""
    return installed_scope(item, workspace) != ""


# ---- 双层检测（2026-10-17 缺口①）：全局层 ~/.agt 约定 ----
# workflow（seed_scope=global 播种）/skill/plugin 有用户级全局层；
# agent_decl/tool 无全局约定（repo-only）。
_GLOBAL_DIRS = {"workflow": "workflows", "skill": "skills", "plugin": "plugins"}
_GLOBAL_ROOT: Path | None = None   # 测试注入点（monkeypatch market._GLOBAL_ROOT=临时根）；
                                   # 缺省 None → 运行时经 _global_root() 读 paths.AGT_DIR
                                   # （AGT_HOME 隔离环境全局层跟随，不再写死 ~/.agt）


def _global_root() -> Path:
    """全局层根唯一入口：测试注入的 _GLOBAL_ROOT 优先，否则 paths.AGT_DIR。"""
    if _GLOBAL_ROOT is not None:
        return Path(_GLOBAL_ROOT)
    import paths
    return Path(paths.AGT_DIR)


def _rel_under(installed_path: str, install_dir: str) -> Path:
    """installed 路径相对落位目录的剩余部分（installed 恒在 install_dir 之下；
    不在时防御性退化为文件名）。"""
    try:
        return Path(installed_path).relative_to(Path(install_dir or "."))
    except ValueError:
        return Path(installed_path).name


def installed_scope(item: dict, workspace=None) -> str:
    """双层检测：repo 层与全局层（~/.agt/<dir>/）各自"声明路径全部存在"算该层已装。
    返回 "repo" / "global" / "both" / ""。"""
    inst = item.get("installed")
    repo = False
    if inst:
        paths = [inst] if isinstance(inst, str) else list(inst)
        repo = all(_ws_path(workspace, p).exists() for p in paths)
    glob = False
    gdir = _GLOBAL_DIRS.get(item.get("type", ""))
    if gdir and inst:
        paths = [inst] if isinstance(inst, str) else list(inst)
        glob = all((_global_root() / gdir / _rel_under(p, item.get("install_dir", ""))).exists()
                   for p in paths)
    if repo and glob:
        return "both"
    return "repo" if repo else "global" if glob else ""


def _scope_installed(item: dict, scope: str, workspace=None) -> bool:
    """单层已装判定（install 的"已存在跳过"/uninstall 定位用）。"""
    return installed_scope(item, workspace) in (scope, "both")


def _is_available(item: dict) -> bool:
    """随包文件是否在（wheel 安装不完整时可诊断）。"""
    if item.get("dir"):
        return (_PKG_ROOT / item["dir"]).is_dir()
    files = item.get("file")
    if not files:
        return False
    files = [files] if isinstance(files, str) else list(files)
    return all((_PKG_ROOT / f).exists() for f in files)


def _scan_plugin_dirs(workspace=None) -> dict:
    """扫两层插件目录（repo=<ws>/.agent/plugins、global=<AGT_DIR>/plugins），
    返回 {name: {"repo": Path|None, "global": Path|None, "manifest": PluginManifest}}。
    manifest 解析失败（无效插件）跳过；不调 plugins.discover——它走 paths.AGT_DIR 且只返回
    覆盖后的一份；这里要两层完整视图，且全局层必须走 _global_root()（测试 monkeypatch
    market._GLOBAL_ROOT 即完全隔离）。"""
    import plugins as _plugins
    ws = Path(workspace) if workspace else Path.cwd()
    layers = {"repo": ws / ".agent" / "plugins", "global": _global_root() / "plugins"}
    out: dict = {}
    for scope, base in layers.items():
        if not base.is_dir():
            continue
        for d in sorted(base.iterdir()):
            if (not d.is_dir() or d.name.startswith(".")
                    or not (d / "manifest.yml").is_file()):
                continue   # 隐藏/临时目录（如转移中断残留的 .<名>.moving-*）不算插件
            try:
                m = _plugins._parse_manifest(d)
            except Exception:
                continue   # 无效插件（manifest 缺失/坏）：市场不展示、不可转移
            e = out.setdefault(m.name, {"repo": None, "global": None, "manifest": m})
            e[scope] = d
    return out


def marketplace_items(workspace=None) -> list[dict]:
    """市场清单：索引条目 + installed/installed_scope/available 运行时态（type_label 中文名同步）；
    末尾合并「本地插件」——两层插件目录里不在索引的已装插件（source=local），仅可层间转移。"""
    out = []
    for it in load_index()["items"]:
        item = dict(it)
        item["type_label"] = _TYPE_LABELS.get(item.get("type", ""), item.get("type", ""))
        scope = installed_scope(it, workspace)
        item["installed"] = scope != ""
        item["installed_scope"] = scope   # repo/global/both/""（前端徽章用）
        item["available"] = _is_available(it)
        out.append(item)
    indexed = {x.get("name") for x in out}
    for name, e in sorted(_scan_plugin_dirs(workspace).items()):
        if name in indexed:
            continue   # 与索引同名：索引条目已表达（它还带随包源/安装通道），不重复列
        m = e["manifest"]
        r, g = e["repo"], e["global"]
        scope = "both" if (r and g) else "repo" if r else "global"
        out.append({"name": name, "type": "plugin", "type_label": "插件",
                    "desc": m.description, "version": m.version, "tags": ["本地"],
                    "source": "local", "local": True, "install_dir": ".agent/plugins",
                    "installed": True, "installed_scope": scope, "available": True})
    return out


def _find(name: str) -> dict | None:
    return next((x for x in load_index()["items"] if x.get("name") == name), None)


def _manifest_unique(name: str) -> bool:
    """名字在随包 manifest.json 中恰好一条 → 可走 download_asset 通道。"""
    from download import load_manifest
    return sum(1 for a in load_manifest() if a.get("name") == name) == 1


def install(name: str, force: bool = False, workspace=None, scope: str = "repo") -> str:
    """安装市场条目。scope="repo"（默认）落工作区（.agent/...，文件型唯一名复用
    download_asset 通道）；scope="global" 落用户级全局层 ~/.agt/<dir>/（仅限有全局
    约定的类型：workflow/skill/plugin；agent_decl/tool 无全局约定，拒绝）。
    已存在且非 force 跳过（只看目标 scope 那一层）。返回结果文案（多行）。"""
    item = _find(name)
    if not item:
        names = ", ".join(x["name"] for x in load_index()["items"])
        return f"[未找到] 市场没有「{name}」。/marketplace 查看清单（{names}）"
    if scope not in ("repo", "global"):
        return f"[参数] scope 只支持 repo/global，收到：{scope}"
    if not _is_available(item):
        return f"[缺失] 随包文件不存在：{item.get('dir') or item.get('file')}（安装可能不完整）"
    gdir = _GLOBAL_DIRS.get(item.get("type", ""))
    if scope == "global" and not gdir:
        return (f"[不支持] 「{name}」（{_TYPE_LABELS.get(item.get('type', ''), item.get('type', ''))}）"
                f"无全局层约定，仅支持安装到 repo（去掉 --global）")
    if not force and _scope_installed(item, scope, workspace):
        where = "全局层 ~/.agt" if scope == "global" else "repo 工作区"
        return f"⏭ 「{name}」已安装（{where}，未动）。--force 可覆盖重装。"

    # 全局层：统一本模块复制（不走 download_asset——它固定落 workspace）。
    # 落位 ~/.agt/<gdir>/<name>/，与 installed_scope 的全局检测映射一致。
    if scope == "global":
        lines = []
        if item.get("dir"):
            src = _PKG_ROOT / item["dir"]
            dst = _global_root() / gdir / src.name
            if dst.exists() and not force:
                return f"⏭ 已存在（未覆盖）：~/.agt/{gdir}/{src.name}（--force 覆盖重装）"
            shutil.copytree(src, dst, dirs_exist_ok=bool(force))   # copytree 自建含父目录
            lines.append(f"✅ 已安装到全局 {item['type']}「{name}」→ ~/.agt/{gdir}/{src.name}/")
        else:
            files = item["file"]
            files = [files] if isinstance(files, str) else list(files)
            for f in files:
                src = _PKG_ROOT / f
                dst_dir = _global_root() / gdir
                dst_dir.mkdir(parents=True, exist_ok=True)
                dst = dst_dir / src.name
                if dst.exists() and not force:
                    lines.append(f"⏭ 已存在（未覆盖）：~/.agt/{gdir}/{dst.name}")
                    continue
                shutil.copy2(src, dst)
                lines.append(f"  → ~/.agt/{gdir}/{dst.name}")
            lines.insert(0, f"✅ 已安装到全局 {item['type']}「{name}」（{len(files)} 个文件）")
        lines.append(_after_install_hint(item))
        return "\n".join(lines)

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


def uninstall(name: str, workspace=None, scope=None):
    """卸载市场条目：卸 installed_scope 实际存在的层（both 两层都卸；默认）。
    scope="repo"/"global" 可只卸指定层。目录型整目录删，文件型只删声明文件
    （空父目录清理止步落位根），不碰用户其它文件。"""
    item = _find(name)
    if not item:
        return f"[未找到] 市场没有「{name}」。/marketplace 查看清单"
    if scope in (None, "", "all"):
        cur = installed_scope(item, workspace)
        if not cur:
            return f"⏭ 「{name}」未安装（无需卸载）"
        targets = ["repo", "global"] if cur == "both" else [cur]
    elif scope in ("repo", "global"):
        if not _scope_installed(item, scope, workspace):
            where = "全局层 ~/.agt" if scope == "global" else "repo 工作区"
            return f"⏭ 「{name}」未安装在{where}（无需卸载）"
        targets = [scope]
    else:
        return f"[参数] scope 只支持 repo/global/省略（卸全部已装层），收到：{scope}"
    removed = []
    for sc in targets:
        removed.extend(_remove_scope(item, sc, workspace))
    return "🗑️ 已卸载「{}」（{}，{}）：\n  {}".format(
        name, item.get("type", ""), "+".join(targets), "\n  ".join(removed))


# ---- 层间转移（2026-11-27 plan p_a8823fda）：已装插件 repo ⇄ 全局 ----

def move_plugin(name: str, workspace=None, to: str = "global", force: bool = False) -> str:
    """已装插件层间转移（移动不是复制）：to="global"（repo → 全局 ~/.agt/plugins/）/
    "repo"（全局 → .agent/plugins/）。安全流程：copytree → 目标层落位目录下的临时名
    （防跨盘 EXDEV——os.replace 必须同盘；ignore __pycache__/*.pyc）→ 校验 manifest
    可解析 → force 时先删旧目标 → os.replace 就位 → 删源。任何一步异常：清理临时目录、
    源保持原样（[失败] 前缀）。纯文件操作不 reload——CLI（无 agent）也用它；
    端点层（server /api/marketplace/move）负责成功后的热重载。
    失败前缀约定：[参数]/[未找到]/[已存在]/[失败]；信息性跳过以 "⏭ " 开头。"""
    if to not in ("global", "repo"):
        return f"[参数] to 只支持 global/repo，收到：{to}"
    found = _scan_plugin_dirs(workspace).get(name)
    if found is None:
        return (f"[未找到] 插件「{name}」两层都没有"
                f"（.agent/plugins/ 与 {_global_root() / 'plugins'}/）")
    src_scope = "repo" if to == "global" else "global"
    src = found.get(src_scope)
    if src is None:
        where = "repo 工作区" if src_scope == "repo" else "全局层"
        return f"⏭ 「{name}」未安装在{where}，无需转移"
    dst_dir = (_global_root() / "plugins") if to == "global" else _ws_path(workspace, ".agent/plugins")
    dst = dst_dir / src.name
    if dst.exists() and not force:
        return (f"[已存在] 目标层已有同名「{name}」（{dst}）。"
                f"--force / 二次确认可用源这份覆盖")
    label = f"「{name}」→ ~/.agt/plugins/{src.name}/" if to == "global" \
        else f"「{name}」→ .agent/plugins/{src.name}/"
    if src.resolve() == dst.resolve():
        # 两层是同一目录（AGT_HOME 指到 <ws>/.agt 这类配置）：移动=自删，直接拒绝
        return (f"⏭ 「{name}」的两层是同一目录（{dst}），无需转移"
                f"（全局层根 {_global_root()} 与 repo 层重合）")
    dst_dir.mkdir(parents=True, exist_ok=True)
    # 清历史残留（进程曾中断在复制与就位之间）：同名的 .moving-* 全清，不只本 pid 的
    for stale in dst_dir.glob(f".{src.name}.moving-*"):
        shutil.rmtree(stale, ignore_errors=True)
    tmp = dst_dir / f".{src.name}.moving-{os.getpid()}"
    old = None
    import plugins as _plugins
    try:
        shutil.copytree(src, tmp, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        if not (tmp / "manifest.yml").is_file():
            raise RuntimeError("转移副本缺 manifest.yml（不会被识别为插件）")
        _plugins._parse_manifest(tmp)   # 解析失败抛 ValueError → 走清理分支
        if dst.exists():                # force：旧目标原子挪开而非直接删（删一半失败会留残缺目录）
            old = dst_dir / f".{dst.name}.moving-old-{os.getpid()}"
            os.replace(dst, old)
        os.replace(tmp, dst)
    except Exception as e:
        shutil.rmtree(tmp, ignore_errors=True)
        if old is not None and old.exists() and not dst.exists():
            os.replace(old, dst)        # 旧目标已挪开而新副本就位失败：把旧的放回去
        return f"[失败] 转移{label}未完成（源保持原样）：{type(e).__name__}: {e}"
    if old is not None:
        shutil.rmtree(old, ignore_errors=True)   # 旧版本清不掉也无碍（隐藏目录，下次转移时清）
    origin = "原 repo 目录已移除" if to == "global" else "原全局目录已移除"
    arrow = "repo → 全局 ~/.agt/plugins/" if to == "global" else "全局 → repo .agent/plugins/"
    out = f"✅ 已转移「{name}」{arrow}{src.name}/（{origin}）"
    try:
        shutil.rmtree(src)
    except Exception as e:   # 已就位但删源失败：两层都在（repo 级仍生效）——如实提示手动清理
        out += (f"\n  ⚠️ 目标层已就位，但源目录删除失败（现为双层都有，请手动删除 {src}）："
                f"{type(e).__name__}: {e}")
    return out


def _remove_scope(item: dict, scope: str, workspace=None) -> list[str]:
    """删除某一层的已装文件/目录，返回展示行列表。"""
    if scope == "global":
        gdir = _GLOBAL_DIRS.get(item.get("type", ""))
        if not gdir:
            return []
        root = _global_root() / gdir                       # 全局落位根（相当于 install_dir）
        prefix, base = "~/.agt/", _global_root()
    else:
        root = _ws_path(workspace, item.get("install_dir", "."))
        prefix, base = "", _ws_path(workspace, ".")      # 展示相对 workspace 的路径

    def _show(target: Path) -> str:
        try:
            return prefix + target.relative_to(base).as_posix()
        except ValueError:
            return target.as_posix()

    if item.get("dir"):
        # 目录型（插件/技能/子Agent声明）：落位目录整删（installed 只列主文件会漏 tools/ 等随行物）
        target = root / Path(item["dir"]).name
        if target.is_dir():
            shutil.rmtree(target)
            return [_show(target) + "/"]
        return []
    inst = item.get("installed")
    paths = [inst] if isinstance(inst, str) else list(inst)
    removed = []
    for p in paths:
        target = root / _rel_under(p, item.get("install_dir", ""))
        if target.is_dir():
            shutil.rmtree(target)
        elif target.exists():
            target.unlink()
        removed.append(_show(target))
        # 清理因此变空的父目录（止步落位根目录，不越界）
        parent = target.parent
        while parent != root and root in parent.parents:
            try:
                parent.rmdir()          # 非空会抛错 → 自然终止
            except OSError:
                break
            parent = parent.parent
    return removed
