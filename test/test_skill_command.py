"""skill_command 轻量命令口（2026-10-17 用户提案）——路由/白名单/回执/超时"""
import sys, pathlib, tempfile, shutil
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))
import agent_config as ac


def _mk_skill(tmp, cmds=("echo",)):
    sk = tmp / "demo-skill"
    (sk / "scripts" / "commands").mkdir(parents=True, exist_ok=True)
    (sk / "SKILL.md").write_text("---\nname: demo-skill\ndescription: t\nwhen_to_use: t\n---\nSOP", encoding="utf-8")
    for c in cmds:
        (sk / "scripts" / "commands" / f"{c}.py").write_text(
            "import sys\nprint('args:', sys.argv[1:])\n", encoding="utf-8")
    return sk


def test_hit_with_quoted_args():
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="skcmd_t_"))
    try:
        sk = _mk_skill(tmp)
        ac._resolve_skill = lambda name, ws=None: (sk, "test")
        out = ac.skill_command("demo-skill", 'echo --x 500 --label "hello world"')
        assert "✓" in out and "exit=0" in out and "'--label', 'hello world'" in out
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_whitelist_reject_lists_available():
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="skcmd_t_"))
    try:
        sk = _mk_skill(tmp, cmds=("echo", "drag"))
        ac._resolve_skill = lambda name, ws=None: (sk, "test")
        out = ac.skill_command("demo-skill", "nope")
        assert "[未知命令] nope" in out and "- echo" in out and "- drag" in out
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_exit_code_and_timeout_and_empty():
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="skcmd_t_"))
    try:
        sk = tmp / "demo-skill"
        _mk_skill(tmp)
        (sk / "scripts" / "commands" / "boom.py").write_text("import time\ntime.sleep(99)", encoding="utf-8")
        ac._resolve_skill = lambda name, ws=None: (sk, "test")
        assert "exit=0" in ac.skill_command("demo-skill", "echo")
        (sk / "scripts" / "commands" / "fail.py").write_text("import sys\nsys.exit(3)", encoding="utf-8")
        assert "✗" in ac.skill_command("demo-skill", "fail") and "exit=3" in ac.skill_command("demo-skill", "fail")
        assert "[超时]" in ac.skill_command("demo-skill", "boom", timeout=5)
        assert "[空命令]" in ac.skill_command("demo-skill", "")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_unknown_skill_passthrough():
    ac._resolve_skill = lambda name, ws=None: (None, "test")
    assert "未找到" in ac.skill_command("ghost", "x") or "非法" in ac.skill_command("ghost", "x")
