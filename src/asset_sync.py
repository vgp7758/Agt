"""asset_sync.py —— 随包播种资产的版本对比与一键热更新。

问题：pip 升级后，随包种子（src/workflows/、src/agents/）是新的，但已播种到用户
repo 的副本（.agent/workflows/ 等）仍是旧的——播种只在目标不存在时拷贝，用户改过
的同名文件也不能覆盖。本模块提供 hash 对比 + 分级更新：

状态判定（三方 hash：随包 seed / 本地 local / 上次播种基线 baseline）：
  same            本地 == 随包               → 最新，无需动作
  seed_newer      本地 == baseline ≠ 随包     → 用户没改过，随包有新版 → 可安全更新
  local_modified  本地 ≠ baseline ≠ 随包      → 用户改过 → 默认跳过（force 才覆盖）
  unknown         本地 ≠ 随包 且无 baseline   → 历史播种（升级前装的）无法区分改没改
                                                → apply 不动，force 覆盖（hash==随包时补记基线）
  missing         本地不存在                  → 直接安装（= 播种）

基线存储：.agent/seed_state.json  {相对路径: sha1}——播种/更新时写入，diff 时
hash 相等自动补记（迁移旧安装）。

工具脚本（tools_builtin）与节点插件（nodes_builtin）直接从随包 assets 目录扫描
（不播种副本），pip 升级即新版——不在本模块管辖内。

更新即生效：工作流每轮重扫、声明每次 agent_prompt 读、persona md 每轮投影读——
更新后无需 /restart（reload_hot 钩子对 .agent/ 下文件的语义一致）。
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from real_tools import WORKSPACE

_STATE_NAME = "seed_state.json"


def _sha(p: Path) -> str:
    try:
        return hashlib.sha1(p.read_bytes()).hexdigest()[:16]
    except Exception:
        return ""


def _state_path(workspace: Path) -> Path:
    return workspace / ".agent" / _STATE_NAME


def _load_state(workspace: Path) -> dict:
    p = _state_path(workspace)
    try:
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    except Exception:
        return {}


def _save_state(workspace: Path, st: dict) -> None:
    p = _state_path(workspace)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception:
        pass


def _seed_dirs(pkg_root: Path, workspace: Path = None) -> list[tuple[str, Path, Path]]:
    """[(类别, 随包源目录, 本地目标目录)]——随包没有的类别自动缺席（老版本 pip 包）。
    本地目录随 workspace 参数走（默认全局 WORKSPACE）。"""
    workspace = workspace or WORKSPACE
    out = []
    wf = (pkg_root / "workflows", workspace / ".agent" / "workflows")
    ag = (pkg_root / "agents", workspace / ".agent" / "agents")
    for kind, (src, dst) in (("workflow", wf), ("agent", ag)):
        if src.is_dir():
            out.append((kind, src, dst))
    return out


def diff_seed_assets(workspace: Path = None, pkg_root: Path = None) -> tuple[list[dict], dict]:
    """对比随包种子 vs 本地播种副本。
    返回 (条目列表, 汇总)。条目：{kind, name, rel, status, note}——
    status ∈ same/seed_newer/local_modified/unknown/missing。
    hash 相等的旧安装自动补记基线（迁移），返回汇总里带 backfilled 数。"""
    workspace = workspace or WORKSPACE
    pkg_root = pkg_root or Path(__file__).resolve().parent
    st = _load_state(workspace)
    backfilled = 0
    items: list[dict] = []

    for kind, src_dir, dst_dir in _seed_dirs(pkg_root, workspace):
        # 播种面 = 随包全部文件（yml/md/xml；排除 __pycache__ 与 meta）
        seeds = sorted(p for p in src_dir.rglob("*")
                       if p.is_file() and p.suffix in (".xml", ".yml", ".md", ".json")
                       and "__pycache__" not in p.parts and not p.name.endswith(".meta"))
        for sp in seeds:
            rel = sp.relative_to(src_dir).as_posix()
            dst = dst_dir / rel
            seed_h = _sha(sp)
            if not dst.exists():
                items.append({"kind": kind, "name": rel, "rel": rel, "status": "missing",
                              "note": "本地缺（未播种过）→ 安装"})
                continue
            local_h = _sha(dst)
            key = f"{kind}/{rel}"
            if local_h == seed_h:
                if st.get(key) != seed_h:      # 旧安装迁移：hash 一致但无基线 → 补记
                    st[key] = seed_h
                    backfilled += 1
                items.append({"kind": kind, "name": rel, "rel": rel, "status": "same", "note": ""})
                continue
            base_h = st.get(key, "")
            if base_h == local_h:
                items.append({"kind": kind, "name": rel, "rel": rel, "status": "seed_newer",
                              "note": "本地未改，随包有新版 → 可更新"})
            elif base_h:
                items.append({"kind": kind, "name": rel, "rel": rel, "status": "local_modified",
                              "note": "本地已修改（基线不符）→ 默认跳过，--force 覆盖"})
            else:
                items.append({"kind": kind, "name": rel, "rel": rel, "status": "unknown",
                              "note": "与随包不同且无基线（旧版安装/手改）→ 默认跳过，--force 覆盖"})

    if backfilled:
        _save_state(workspace, st)
    counts = {s: sum(1 for i in items if i["status"] == s)
              for s in ("same", "seed_newer", "local_modified", "unknown", "missing")}
    return items, {"counts": counts, "backfilled": backfilled, "total": len(items)}


def update_seed_assets(apply: bool = False, force: bool = False, workspace: Path = None,
                       pkg_root: Path = None) -> str:
    """/update-assets 实现：预览（apply=False）或执行更新。
    安全面：missing 安装 + seed_newer 更新；保护面 local_modified/unknown 仅 force 覆盖
    （覆盖前基线刷新为新随包 hash——此后它进入正常的 seed_newer 生命周期）。"""
    workspace = workspace or WORKSPACE
    pkg_root = pkg_root or Path(__file__).resolve().parent
    items, summary = diff_seed_assets(workspace, pkg_root)
    counts = summary["counts"]
    header = (f"随包播种资产对比（{summary['total']} 项）："
              f"最新 {counts['same']}｜可更新 {counts['seed_newer']}｜"
              f"本地已改 {counts['local_modified']}｜未知 {counts['unknown']}｜缺 {counts['missing']}"
              + (f"（补记基线 {summary['backfilled']}）" if summary["backfilled"] else ""))

    actionable = [i for i in items if i["status"] in ("missing", "seed_newer")]
    protected = [i for i in items if i["status"] in ("local_modified", "unknown")]

    if not apply:
        lines = [header, ""]
        if actionable or protected:
            lines.append("  状态      类别      文件")
            for i in items:
                if i["status"] == "same":
                    continue
                tag = {"missing": "缺/装", "seed_newer": "可更新", "local_modified": "已改!",
                       "unknown": "未知?"}[i["status"]]
                lines.append(f"  {tag:8s} {i['kind']:8s} {i['rel']}")
        else:
            lines.append("  全部最新，无需更新 ✅")
        lines.append("")
        lines.append("执行：/update-assets apply（安全更新：装缺+更新未改项）"
                     + ("；加 --force 覆盖已改/未知项" if protected else ""))
        return "\n".join(lines)

    # ===== apply =====
    st = _load_state(workspace)
    done, skipped = [], []

    def _copy(kind: str, src_dir_map, rel: str, dst_dir_map):
        sp = src_dir_map / rel
        dst = dst_dir_map / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(sp.read_bytes())   # 字节级复制：write_text 的 newline 转换会把 \n 变 \r\n，hash 对不上
        st[f"{kind}/{rel}"] = _sha(sp)

    dirmap = {kind: (s, d) for kind, s, d in _seed_dirs(pkg_root, workspace)}
    for i in actionable:
        s, d = dirmap[i["kind"]]
        try:
            _copy(i["kind"], s, i["rel"], d)
            done.append(i)
        except Exception as e:
            skipped.append((i, f"复制失败: {type(e).__name__}"))

    forced = []
    if force:
        for i in protected:
            s, d = dirmap[i["kind"]]
            try:
                _copy(i["kind"], s, i["rel"], d)   # 覆盖 + 基线刷新 → 回到正常生命周期
                forced.append(i)
            except Exception as e:
                skipped.append((i, f"复制失败: {type(e).__name__}"))
    else:
        skipped.extend((i, "保护跳过（本地已改/未知）") for i in protected)

    if done or forced:
        _save_state(workspace, st)

    lines = [f"✅ 更新完成：安装/更新 {len(done)} 项" + (f"，强制覆盖 {len(forced)} 项" if forced else ""),
             f"   跳过 {len(skipped)} 项"]
    for i in done + forced:
        lines.append(f"   {'🆕' if i['status'] == 'missing' else '⬆'} {i['kind']}/{i['rel']}")
    for i, why in skipped:
        lines.append(f"   ⏭ {i['kind']}/{i['rel']} — {why}")
    lines.append("")
    lines.append("更新即生效（工作流每轮重扫/声明每次读取），无需重启。"
                 "被更新工作流的 enabled/hidden 等本地 .meta 设置不受影响。")
    return "\n".join(lines)
