"""全局技能双层体系单测（spec s_4ac5ccb6 / plan p_8e16d6ed）。

覆盖：统一寻址（本地优先 shadow / 全局须激活）、激活清单解析、
skills_summary 🌐 前缀、skill_run_code 路径逃逸拦截与正常执行、
skill_evaluate 三级降级（evaluate.py / EVAL.md / 明确提示）。

全局目录通过 monkeypatch _global_skills_root 隔离到 tmp_path，不碰真 ~/.agt。
"""
import sys, os
sys.path.insert(0, r"D:\AI_Usings\agt\src")

import json
import pytest
from pathlib import Path
import agent_config as ac


@pytest.fixture
def env(tmp_path, monkeypatch):
    """构造隔离环境：ws=repo 工作区，groot=全局技能根。返回 (ws, groot)。"""
    ws = tmp_path / "repo"
    groot = tmp_path / "global_skills"
    (ws / ".agent" / "skills").mkdir(parents=True)
    monkeypatch.setattr(ac, "_global_skills_root", lambda: groot)
    monkeypatch.setattr(ac, "WORKSPACE", ws)
    return ws, groot


def _mk_skill(root: Path, name: str, desc: str = "desc", body: str = "SOP body", scripts: dict | None = None):
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {desc}\nwhen_to_use: anytime\n---\n\n# {name}\n\n{body}\n",
        encoding="utf-8")
    for rel, content in (scripts or {}).items():
        p = d / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    return d


# ── 寻址与索引 ──────────────────────────────────────────

def test_no_manifest_zero_global(env):
    ws, groot = env
    _mk_skill(groot, "g-skill")
    idx = ac.load_skills_index(ws)
    assert idx == []                                   # 无激活清单 → 零全局
    d, scope = ac._resolve_skill("g-skill", ws)
    assert d is None and scope == ""                   # resolve 也找不到


def test_manifest_activates_global(env):
    ws, groot = env
    _mk_skill(groot, "g-skill")
    (ws / ".agent/skills/global-skills.json").write_text('["g-skill"]', encoding="utf-8")
    idx = ac.load_skills_index(ws)
    assert [s["scope"] for s in idx] == ["global"]
    assert ac.skills_summary(ws).startswith("- 🌐 g-skill:")
    d, scope = ac._resolve_skill("g-skill", ws)
    assert scope == "global" and d == groot / "g-skill"


def test_local_shadows_global(env):
    ws, groot = env
    _mk_skill(ws / ".agent/skills", "dup", "local one")
    _mk_skill(groot, "dup", "global one")
    (ws / ".agent/skills/global-skills.json").write_text('["dup"]', encoding="utf-8")
    idx = ac.load_skills_index(ws)
    assert len(idx) == 1 and idx[0]["scope"] == "local" and idx[0]["description"] == "local one"
    d, scope = ac._resolve_skill("dup", ws)
    assert scope == "local"
    assert "local one" in ac.read_skill("dup")


def test_manifest_not_list_ignored(env):
    ws, groot = env
    _mk_skill(groot, "g-skill")
    (ws / ".agent/skills/global-skills.json").write_text('{"enabled": ["g-skill"]}', encoding="utf-8")
    assert ac.load_skills_index(ws) == []               # 非纯数组（对象形态）= 忽略


def test_manifest_corrupt_ignored(env):
    ws, groot = env
    _mk_skill(groot, "g-skill")
    (ws / ".agent/skills/global-skills.json").write_text('not json]', encoding="utf-8")
    assert ac.load_skills_index(ws) == []               # 损坏 = 零全局，不炸


def test_enabled_but_missing_package(env):
    ws, groot = env
    (ws / ".agent/skills/global-skills.json").write_text('["ghost"]', encoding="utf-8")
    assert ac.load_skills_index(ws) == []               # 激活了但包不存在 → 静默跳过


# ── skill_run_code 安全 ────────────────────────────────

def _enable(env, name, scripts):
    ws, groot = env
    _mk_skill(groot, name, scripts=scripts)
    (ws / ".agent/skills/global-skills.json").write_text(json.dumps([name]), encoding="utf-8")
    return ws, groot


def test_run_code_executes_in_skill_dir(env):
    _enable(env, "s1", {"scripts/echo.py": "import sys, os\nprint(os.getcwd())\nprint(sys.argv[1:])\n"})
    out = ac.skill_run_code("s1", "scripts/echo.py", "--flag x")
    assert "[exit=0]" in out
    assert out.count("global_skills") == 1 or "s1" in out    # cwd=技能目录
    assert "['--flag', 'x']" in out.replace('"', "'").replace(",", ", ") or "--flag" in out


def test_run_code_blocks_dotdot(env):
    _enable(env, "s1", {"scripts/ok.py": "print(1)\n"})
    assert ac.skill_run_code("s1", "../evil.py").startswith("[非法脚本")
    assert ac.skill_run_code("s1", "scripts/../../evil.py").startswith("[非法脚本")
    assert ac.skill_run_code("s1", "SKILL.md").startswith("[非法脚本")     # 非 .py


def test_run_code_missing_lists_pys(env):
    _enable(env, "s1", {"tools/a.py": "print(1)\n"})
    out = ac.skill_run_code("s1", "scripts/nope.py")
    assert out.startswith("[脚本不存在]") and "tools/a.py" in out


# ── skill_evaluate 降级链 ──────────────────────────────

def test_evaluate_no_entry(env):
    _enable(env, "s1", {})
    assert ac.skill_evaluate("s1").startswith("[无评测入口]")


def test_evaluate_eval_md(env):
    _enable(env, "s1", {"EVAL.md": "# 评测说明\n人工对照检查。"})
    out = ac.skill_evaluate("s1")
    assert out.startswith("📋") and "评测说明" in out


def test_evaluate_script_stdin(env):
    _enable(env, "s1", {"scripts/evaluate.py":
                        "import sys\ntxt = sys.stdin.read().strip()\nprint('LEN', len(txt))"})
    out = ac.skill_evaluate("s1", input="hello world")
    assert "[exit=0]" in out and "LEN 11" in out


# ── skill_navigate ─────────────────────────────────────

def test_navigate_sections(env):
    ws, groot = env
    _mk_skill(groot, "s1", body="intro\n\n## 用法\nstep1\n\n## 脚本\nrun it\n")
    (ws / ".agent/skills/global-skills.json").write_text('["s1"]', encoding="utf-8")
    tree = ac.skill_navigate("s1")
    assert "用法" in tree and "脚本" in tree and "🌐 全局" in tree
    sec = ac.skill_navigate("s1", section="脚本")
    assert "run it" in sec and "step1" not in sec          # 只含本章节
    missing = ac.skill_navigate("s1", section="不存在")
    assert missing.startswith("📦") and "可用章节" in missing
