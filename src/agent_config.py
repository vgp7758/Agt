"""agent_config.py —— .agent/ 工作区配置：rules + skills（渐进式披露 + 自动沉淀）。

读取启动目录(cwd=WORKSPACE)下的 .agent/：
  .agent/rules/*              → 始终生效的规则，启动时读进 SYSTEM。
  .agent/skills/<名>/SKILL.md → 技能(YAML frontmatter name/description/when_to_use + markdown SOP)。
                                只把 frontmatter 摘要放进 SYSTEM；LLM 用 read_skill(name) 按需读完整 SOP。
save_skill 让 Agent 自主把可复用任务的 SOP 沉淀成新技能(或更新)，积累经验。
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml

from real_tools import WORKSPACE
from tools import Tool, Toolbox

_AGENT_DIR = ".agent"
_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def _split_frontmatter(text: str) -> tuple[dict, str]:
    """按首尾 --- 切分 YAML frontmatter 与 markdown 正文。无 frontmatter 返回 ({}, 全文)。"""
    if not text.startswith("---"):
        return {}, text
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}, text
    try:
        meta = yaml.safe_load(parts[1]) or {}
    except yaml.YAMLError:
        meta = {}
    return meta, parts[2].lstrip("\n")


def load_rules(workspace: Path) -> str:
    """拼接 .agent/rules/ 下所有文件内容（按文件名排序）。无则空串。"""
    d = workspace / _AGENT_DIR / "rules"
    if not d.exists():
        return ""
    chunks = [f.read_text(encoding="utf-8", errors="ignore").strip()
              for f in sorted(d.iterdir()) if f.is_file()]
    return "\n\n".join(chunks)


def load_skills_index(workspace: Path) -> list[dict]:
    """扫 .agent/skills/*/SKILL.md，返回 [{name, description, when_to_use, path}, ...]。"""
    d = workspace / _AGENT_DIR / "skills"
    out = []
    if not d.exists():
        return out
    for skill_md in sorted(d.glob("*/SKILL.md")):
        try:
            meta, _ = _split_frontmatter(skill_md.read_text(encoding="utf-8", errors="ignore"))
        except Exception:
            continue
        out.append({
            "name": meta.get("name", skill_md.parent.name),
            "description": meta.get("description", ""),
            "when_to_use": meta.get("when_to_use", ""),
            "path": str(skill_md.relative_to(workspace)).replace("\\", "/"),
        })
    return out


def skills_summary(workspace: Path) -> str:
    """拼成 SYSTEM 里一行一技能的摘要。无技能返回空串。"""
    idx = load_skills_index(workspace)
    if not idx:
        return ""
    lines = []
    for s in idx:
        when = f"（使用时机: {s['when_to_use']}）" if s["when_to_use"] else ""
        lines.append(f"- {s['name']}: {s['description']}{when}")
    return "\n".join(lines)


# ===== 子 Agent 声明（.agent/agents/*.md，声明式 + 按需实例化 + 一次性）=====

def load_agents_index(workspace: Path) -> list[dict]:
    """扫 .agent/agents/*.md，返回 [{name, description, tools, model, path}, ...]。
    与 load_skills_index 同构，但 agents 是 <name>.md 单文件（非 */SKILL.md）。"""
    d = workspace / _AGENT_DIR / "agents"
    out = []
    if not d.exists():
        return out
    for md in sorted(d.glob("*.md")):
        try:
            meta, _ = _split_frontmatter(md.read_text(encoding="utf-8", errors="ignore"))
        except Exception:
            continue
        out.append({
            "name": meta.get("name", md.stem),
            "description": meta.get("description", ""),
            "tools": meta.get("tools", ""),
            "model": meta.get("model", ""),
            "path": str(md.relative_to(workspace)).replace("\\", "/"),
        })
    return out


def agents_summary(workspace: Path) -> str:
    """拼成 SYSTEM 里一行一个子 agent 的摘要（name + description/何时调用）。无则空串。"""
    idx = load_agents_index(workspace)
    if not idx:
        return ""
    return "\n".join(f"- {a['name']}: {a['description']}" for a in idx)


def seed_default_agents(workspace: Path) -> int:
    """首次启动把随包默认子 agent 模板（src/agents/*.md）播种到 .agent/agents/。
    目标已存在则跳过（不覆盖用户修改）。返回播种数量。照搬 workflow.seed_default_workflows。"""
    bundled = Path(__file__).resolve().parent / "agents"
    dst = workspace / _AGENT_DIR / "agents"
    if not bundled.exists():
        return 0
    dst.mkdir(parents=True, exist_ok=True)
    n = 0
    for src in sorted(bundled.glob("*.md")):
        target = dst / src.name
        if target.exists():
            continue
        target.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
        n += 1
    return n


# ===== 技能工具（注册进 Agent，子 Agent 继承）=====

def read_skill(name: str) -> str:
    """读取某个技能的完整 SKILL.md（含详细 SOP）。任务匹配某技能时，先调它取执行步骤。
    name: 技能名(即 .agent/skills/<name> 文件夹名)。"""
    if not _NAME_RE.match(name or ""):
        return f"[非法名称] '{name}'，技能名只能含字母数字、下划线、连字符"
    p = WORKSPACE / _AGENT_DIR / "skills" / name / "SKILL.md"
    if not p.exists():
        return f"[未找到技能] {name}（可用技能见 SYSTEM 的【可用技能】清单）"
    return p.read_text(encoding="utf-8")


def save_skill(name: str, description: str, when_to_use: str, sop: str) -> str:
    """把一个可复用任务的 SOP 沉淀为技能（写/更新 .agent/skills/<name>/SKILL.md）。
    name: 技能名；description: 一句话作用；when_to_use: 使用时机；sop: 详细步骤(markdown)。"""
    if not _NAME_RE.match(name or ""):
        return f"[非法名称] '{name}'，技能名只能含字母数字、下划线、连字符"
    d = WORKSPACE / _AGENT_DIR / "skills" / name
    d.mkdir(parents=True, exist_ok=True)
    meta = yaml.safe_dump(
        {"name": name, "description": description, "when_to_use": when_to_use},
        allow_unicode=True, sort_keys=False,
    ).strip()
    (d / "SKILL.md").write_text(f"---\n{meta}\n---\n\n{sop.strip()}\n", encoding="utf-8")
    return f"✅ 已保存技能 '{name}' -> {(d / 'SKILL.md').relative_to(WORKSPACE)}"


SKILL_TOOLS = Toolbox(Tool(read_skill), Tool(save_skill))
