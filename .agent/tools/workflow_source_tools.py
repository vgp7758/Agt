"""workflow_source_tools.py —— 工作流种源回灌工具（本 repo 自用，不进随包）。

背景（用户裁定 2026-10-01）：工作流的「随包种源」在 `src/workflows/`（git 管理、随 wheel
打包，pip 装后位于 site-packages/agt_agent/workflows/）；运行时副本在
`<cwd>/.agent/workflows/`（repo 层，优先）与 `~/.agt/workflows/`（全局层，多 repo 共享）。
编辑器保存/手改落在**运行时副本**上——不回流种源的话，新机器安装、/update-assets、
重新播种拿到的仍是旧版（2026-10-01 真实踩过：三个工作流的 seed_scope 标记与手改内容漂移）。

本工具把**运行时生效版**回灌种源：
  对每个 src/workflows/*.xml，按运行时层序（repo > global）找实际生效的版本，
  与种源比较（比较时归一化 seed_scope 标记——只差标记不算改动）；
  有差异则写回种源（保留种源原有的 seed_scope 标记；写前校验 XML/JSON 可解析）。
只改种源文件，**不动任何运行时副本**。回灌后请自行 `git diff` 检查并提交。

用法：
    sync_workflow_sources()                    # 回灌全部有差异的
    sync_workflow_sources(dry_run=True)        # 只报差异，不落盘
    sync_workflow_sources(names=["recap_gen"]) # 只处理指定工作流
"""
from __future__ import annotations

import difflib
import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path

_MARK_RE = re.compile(r'\s*seed_scope="[^"]*"')


def _ws() -> Path:
    from real_tools import WORKSPACE
    return Path(WORKSPACE)


def _runtime_dirs() -> list[tuple[str, Path]]:
    """运行时层序：repo 优先（scan 语义），global 兜底。"""
    from paths import AGT_DIR
    ws = _ws()
    return [("repo", ws / ".agent" / "workflows"),
            ("global", Path(AGT_DIR) / "workflows")]


def _norm(text: str) -> str:
    """比较用归一：去掉 seed_scope 标记（只差标记不算改动）。"""
    return _MARK_RE.sub("", text)


def _inject_marker(text: str, marker: str) -> str:
    if not marker or "seed_scope=" in text[:6000]:
        return text
    m = re.search(r"<workflow\b[^>]*?>", text, re.S)
    if not m:
        return text
    return text[:m.end() - 1] + f' {marker}' + text[m.end() - 1:]


def sync_workflow_sources(dry_run: bool = False, names: list = None) -> str:
    """把运行时生效版的工作流回灌到种源 src/workflows/（本 repo 自用）。

    dry_run=True 只报差异不落盘；names 限定处理的工作流名（可带/不带扩展名）。
    返回逐文件状态报告（identical / 回灌 / 跳过原因）。
    """
    ws = _ws()
    src_dir = ws / "src" / "workflows"
    if not src_dir.is_dir():
        return f"[错误] 种源目录不存在：{src_dir}（本工具面向 Agt 源码仓自用）"
    want = None
    if names:
        want = {str(n).strip().removesuffix(".xml").removesuffix(".json") for n in names if str(n).strip()}
    layers = _runtime_dirs()

    rows, n_synced, n_diff, n_same, n_skip = [], 0, 0, 0, 0
    for sp in sorted(list(src_dir.glob("*.xml")) + list(src_dir.glob("*.json"))):
        stem = sp.stem
        if want and stem not in want:
            continue
        try:
            src_text = sp.read_text(encoding="utf-8")
        except OSError as e:
            rows.append(f"  {stem:28} [跳过] 种源读取失败: {e}")
            n_skip += 1
            continue
        marker_m = re.search(r'seed_scope="[^"]*"', src_text[:6000])
        marker = marker_m.group(0) if marker_m else ""

        eff = eff_layer = None
        for lname, d in layers:
            p = d / sp.name
            if p.exists():
                eff, eff_layer = p, lname
                break
        if eff is None:
            rows.append(f"  {stem:28} [跳过] 运行时两层均无副本（未播种）")
            n_skip += 1
            continue
        try:
            eff_text = eff.read_text(encoding="utf-8")
        except OSError as e:
            rows.append(f"  {stem:28} [跳过] 运行时副本读取失败: {e}")
            n_skip += 1
            continue

        if _norm(eff_text) == _norm(src_text):
            rows.append(f"  {stem:28} identical（{eff_layer} 层与种源一致）")
            n_same += 1
            continue

        # 校验运行时可解析（防把坏文件灌进种源）
        try:
            if sp.suffix == ".xml":
                ET.fromstring(eff_text)
            else:
                json.loads(eff_text)
        except Exception as e:
            rows.append(f"  {stem:28} [跳过] 运行时副本解析失败（{type(e).__name__}: {e}）")
            n_skip += 1
            continue

        new_text = _inject_marker(eff_text, marker)
        diff_lines = sum(1 for l in difflib.unified_diff(
            src_text.splitlines(), new_text.splitlines(), n=0) if l[:1] in "+-" and l[:3] not in ("+++", "---"))
        n_diff += 1
        if dry_run:
            rows.append(f"  {stem:28} 有差异（{eff_layer} 层，±{diff_lines} 行）[dry-run 未落盘]")
            continue
        try:
            sp.write_text(new_text, encoding="utf-8")
            rows.append(f"  {stem:28} ✅ 已回灌（{eff_layer} 层 → 种源，±{diff_lines} 行）")
            n_synced += 1
        except OSError as e:
            rows.append(f"  {stem:28} [错误] 写入种源失败: {e}")
            n_skip += 1

    head = ("[dry-run]" if dry_run else "[已回灌]") + \
           f" 处理 {n_same + n_diff + n_skip} 个：一致 {n_same} / 有差异 {n_diff} / 跳过 {n_skip}"
    tail = "" if (dry_run or n_synced == 0) else \
        "\n种源已更新——请 `git diff src/workflows/` 检查后提交（本工具不改运行时副本）。"
    return head + "\n" + "\n".join(rows) + tail


def agt_register():
    return [
        {"name": "sync_workflow_sources", "func": sync_workflow_sources,
         "params": {
             "dry_run": "True=只报差异不落盘（默认 False 直接回灌）",
             "names": "限定处理的工作流名数组（可带/不带 .xml；留空=全部）",
         },
         "outputs": [{"name": "raw", "type": "string",
                      "description": "逐文件状态报告（identical/已回灌/跳过原因）"}],
         "hidden": False, "group": "workflow", "version": 1},
    ]
