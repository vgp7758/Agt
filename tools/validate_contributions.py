"""社区贡献 PR 校验器（被 GitHub Actions 调用）。

用法：python tools/validate_contributions.py

逻辑：git diff origin/main...HEAD 拿 PR 改动文件 → 按 community/<type>/<name>/ 分派到四类校验器
→ 复用 agt 现成校验函数（scan_workflows / scan_node_plugins / _split_frontmatter）
→ 退出码 0/1。遵循 verify_*.py 独立脚本风格。

本地跑（无 origin）会回退到扫整个 community/ 校验全部条目。
"""
import sys
import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from workflow import scan_workflows
from node_plugins import scan_node_plugins
from agent_config import _NAME_RE, _split_frontmatter
from download import load_manifest

ERRORS = []
WARNINGS = []

# community 子目录 → manifest 的 type 字符串
_TYPE_OF_SUBDIR = {"workflows": "workflow", "nodes": "node", "skills": "skill", "mcp": "mcp"}
_VALIDATOR = {}  # 填在下面


def _changed_dirs() -> list[tuple[str, str]]:
    """PR 改动命中的 community/<type>/<name> 目录集合。"""
    try:
        out = subprocess.check_output(
            ["git", "diff", "--name-only", "origin/main...HEAD"],
            text=True, cwd=str(ROOT),
        ).strip()
    except Exception:
        # 本地无 origin：回退到扫整个 community/
        com = ROOT / "community"
        return [(d.parent.name, d.name) for d in com.glob("*/*") if d.is_dir()]
    dirs = set()
    for line in out.splitlines():
        p = line.split("/", 3)
        if len(p) >= 3 and p[0] == "community":
            dirs.add((p[1], p[2]))
    return sorted(dirs)


def _validate_entry(d: Path, expected_type: str):
    """校验 entry.yaml 必填字段 + name 规则 + type 一致。"""
    import yaml
    entry = d / "entry.yaml"
    if not entry.exists():
        ERRORS.append(f"{d}: 缺 entry.yaml")
        return None
    try:
        meta = yaml.safe_load(entry.read_text(encoding="utf-8"))
    except Exception as e:
        ERRORS.append(f"{d}/entry.yaml: YAML 解析失败 {e}")
        return None
    if not isinstance(meta, dict):
        ERRORS.append(f"{d}/entry.yaml: 顶层不是 dict")
        return None
    name = meta.get("name")
    if not name or not _NAME_RE.match(str(name)):
        ERRORS.append(f"{d}/entry.yaml: name 非法（须匹配 {_NAME_RE.pattern}）")
    if meta.get("type") != expected_type:
        ERRORS.append(f"{d}/entry.yaml: type 应为 {expected_type}，实际 {meta.get('type')}")
    if not meta.get("desc"):
        ERRORS.append(f"{d}/entry.yaml: desc 必填")
    return meta


def _validate_workflow(d: Path):
    """复用 scan_workflows：临时塞进一个 workspace 目录扫一次。"""
    import shutil
    import tempfile
    src = d / "workflow.xml"
    if not src.exists():
        src = d / "workflow.json"
    if not src.exists():
        ERRORS.append(f"{d}: 缺 workflow.xml 或 workflow.json")
        return
    with tempfile.TemporaryDirectory() as td:
        ws = Path(td) / ".agent" / "workflows"
        ws.mkdir(parents=True)
        shutil.copy2(src, ws / src.name)
        for it in scan_workflows(Path(td)):
            if it.get("error"):
                ERRORS.append(f"workflow {it['name']}: {it['error']}")
            for w in it.get("warnings", []):
                WARNINGS.append(f"workflow {it['name']}: {w}")


def _validate_node(d: Path):
    """复用 scan_node_plugins：扫贡献目录，warnings 转错/警。"""
    res = scan_node_plugins(dirs=[d])
    for w in res.get("warnings", []):
        # 配对缺失 / 描述符非法 / 核心类型覆盖 —— 社区贡献里都判硬错
        if any(k in w for k in ("无配对", "配对", "描述符", "覆盖", "禁止", "agt_node")):
            ERRORS.append(f"node {d.name}: {w}")
        else:
            WARNINGS.append(f"node {d.name}: {w}")
    if not res.get("handlers"):
        ERRORS.append(f"node {d.name}: 未注册任何 agt_node()（.py 须含 agt_node() 描述符）")


def _validate_skill(d: Path):
    """复用 _split_frontmatter + _NAME_RE。"""
    skill = d / "SKILL.md"
    if not skill.exists():
        ERRORS.append(f"skill {d.name}: 缺 SKILL.md")
        return
    meta, _ = _split_frontmatter(skill.read_text(encoding="utf-8"))
    name = meta.get("name", d.name)
    if not _NAME_RE.match(name or ""):
        ERRORS.append(f"skill {d.name}: frontmatter name 非法（须匹配 {_NAME_RE.pattern}）")
    for k in ("description", "when_to_use"):
        if not meta.get(k):
            ERRORS.append(f"skill {d.name}: frontmatter 缺 {k}")


def _validate_mcp(d: Path):
    """mcp.json schema：mcpServers 对象，每项 command(stdio) 或 url(http/sse)。"""
    mcp = d / "mcp.json"
    if not mcp.exists():
        ERRORS.append(f"mcp {d.name}: 缺 mcp.json")
        return
    try:
        data = json.loads(mcp.read_text(encoding="utf-8"))
    except Exception as e:
        ERRORS.append(f"mcp {d.name}: JSON 解析失败 {e}")
        return
    servers = data.get("mcpServers")
    if not isinstance(servers, dict) or not servers:
        ERRORS.append(f"mcp {d.name}: mcpServers 为空或非对象")
        return
    for sname, conf in servers.items():
        if not isinstance(conf, dict):
            ERRORS.append(f"mcp {d.name}/{sname}: 配置不是对象")
            continue
        if "command" in conf:
            if conf.get("args") is not None and not isinstance(conf.get("args"), list):
                ERRORS.append(f"mcp {d.name}/{sname}: stdio 的 args 须为 list")
        elif "url" in conf:
            if not str(conf["url"]).startswith(("http://", "https://")):
                ERRORS.append(f"mcp {d.name}/{sname}: url 须 http(s)://")
        else:
            ERRORS.append(f"mcp {d.name}/{sname}: 须含 command(stdio) 或 url(http/sse)")


_VALIDATOR = {
    "workflows": _validate_workflow,
    "nodes": _validate_node,
    "skills": _validate_skill,
    "mcp": _validate_mcp,
}


def _check_name_conflicts(metas):
    """社区内部 name 重复 + 与内置 manifest 冲突。"""
    names = [m["name"] for m in metas if m.get("name")]
    if len(names) != len(set(names)):
        dup = [n for n in names if names.count(n) > 1]
        ERRORS.append(f"社区条目 name 重复: {sorted(set(dup))}")
    builtin = {a.get("name") for a in load_manifest()}
    for n in names:
        if n in builtin:
            ERRORS.append(f"name '{n}' 与内置资产冲突")


def main():
    dirs = _changed_dirs()
    if not dirs:
        print("OK: 无 community/ 改动，跳过校验")
        return
    metas = []
    for subdir, name in dirs:
        d = ROOT / "community" / subdir / name
        if not d.is_dir() or subdir not in _TYPE_OF_SUBDIR:
            continue
        meta = _validate_entry(d, _TYPE_OF_SUBDIR[subdir])
        if meta:
            metas.append(meta)
        _VALIDATOR[subdir](d)
    _check_name_conflicts(metas)
    for w in WARNINGS:
        print(f"WARN: {w}")
    for e in ERRORS:
        print(f"ERROR: {e}")
    if ERRORS:
        sys.exit(1)
    print(f"OK: 校验 {len(dirs)} 个社区条目，{len(WARNINGS)} 条 warning")


if __name__ == "__main__":
    main()
