"""子 Agent yml `skills:` 字段装备机制单测（plan p_922370a8 步骤 5）。

覆盖：
- _agent_skill_gear：技能 tools/ 注册进工具箱（外置工具协议不变）、
  SKILL.md SOP 提取（system 尾注入原料）、未知技能名清晰报错、
  无 skills 字段零影响、字符串形态兼容；
- system 尾拼接语义（persona 空/非空两态）；
- desktop-operator 实例化 e2e：真实声明文件 + 真实技能包 →
  键鼠 10 工具在箱 / SOP 在 system / image_feed 装配段照常。

隔离：临时技能用例走 tmp_path + monkeypatch multiagent.WORKSPACE，不污染真实 repo；
e2e 用例直用 repo 真实声明与技能包（只读，不派活不连网）。
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import json
import pytest
import multiagent as ma
from agent import Agent
from tools import Toolbox

KEY_MOUSE_TOOLS = {"mouse_click", "mouse_move", "mouse_drag", "key_press", "key_type",
                   "key_hotkey", "clipboard_read", "clipboard_write", "window_list", "window_focus"}

SKILL_SCRIPT = '''
def hello_tool(msg: str = "hi") -> str:
    """打招呼工具（测试用外置工具）。"""
    return f"hello {msg}"


def agt_register():
    """外置工具注册（同 tools/builtin 约定——scan_script_tools 消费）。"""
    return [{"name": "hello_tool", "func": hello_tool, "hidden": False, "group": "test", "version": 1}]
'''


@pytest.fixture
def ws(tmp_path, monkeypatch):
    """隔离 workspace：技能寻址/工具扫描都落 tmp_path，不碰真实 repo。"""
    monkeypatch.setattr(ma, "WORKSPACE", tmp_path)
    return tmp_path


def _mk_skill(ws_root, name, script=None, body="SOP body"):
    d = ws_root / ".agent" / "skills" / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: d\nwhen_to_use: w\n---\n\n# {name}\n\n{body}\n",
        encoding="utf-8")
    if script:
        (d / "tools").mkdir()
        (d / "tools" / "t.py").write_text(script, encoding="utf-8")
    return d


# ── ① 技能 tools/ 注册 ──────────────────────────────────

def test_skill_tools_registered(ws):
    _mk_skill(ws, "pack-a", script=SKILL_SCRIPT)
    tools, sop, err = ma._agent_skill_gear("x", {"skills": ["pack-a"]}, None)
    assert err == ""
    assert [t.name for t in tools] == ["hello_tool"]      # 外置工具协议不变（函数+docstring→Tool）
    assert sop.startswith("## 装备技能：pack-a") and "SOP body" in sop


def test_skill_without_tools_dir_only_sop(ws):
    _mk_skill(ws, "pack-doc")
    tools, sop, err = ma._agent_skill_gear("x", {"skills": ["pack-doc"]}, None)
    assert (tools, err) == ([], "") and "SOP body" in sop


# ── ② SOP 注入（system 尾拼接语义，与两处调用点同式）──────

def test_sop_merge_semantics():
    sop = "## 装备技能：pack-a\n\nSOP"
    # persona 非空（.md 旧格式）
    assert ((  "你是助手"  ).rstrip() + "\n\n" + sop).strip().startswith("你是助手\n\n## 装备技能")
    # persona 空（yml v2：正文在 assembly file 项，system 段只装 SOP）
    assert ((  ""          ).rstrip() + "\n\n" + sop).strip().startswith("## 装备技能")


# ── ③ 未知技能名清晰报错 ────────────────────────────────

def test_unknown_skill_error(ws):
    tools, sop, err = ma._agent_skill_gear("x", {"skills": ["ghost"]}, None)
    assert (tools, sop) == ([], "")
    assert "ghost" in err and "已安装" in err              # 报错含名字 + 已装清单


def test_unknown_mixed_with_known(ws):
    _mk_skill(ws, "pack-a", script=SKILL_SCRIPT)
    tools, sop, err = ma._agent_skill_gear("x", {"skills": ["pack-a", "ghost"]}, None)
    assert (tools, sop) == ([], "") and "ghost" in err     # 有未知 → 整体拒绝（不半装备）


# ── ④ 无 skills 字段零影响 / 字符串形态兼容 ──────────────

def test_no_skills_noop():
    assert ma._agent_skill_gear("x", {}, None) == ([], "", "")
    assert ma._agent_skill_gear("x", {"skills": []}, None) == ([], "", "")


def test_string_form(ws):
    _mk_skill(ws, "pack-a", script=SKILL_SCRIPT)
    tools, _, err = ma._agent_skill_gear("x", {"skills": "pack-a"}, None)
    assert err == "" and len(tools) == 1


# ── ⑤ desktop-operator 实例化 e2e（真实声明+真实技能包）──

def test_desktop_operator_instantiation():
    from agent_config import load_agent_yml
    root = Path(__file__).resolve().parent.parent
    meta, system = load_agent_yml(root / ".agent" / "agents" / "desktop-operator" / "desktop-operator.yml")
    assert meta.get("skills") == ["desktop-ops"]
    assert system == ""                                    # yml v2：persona 在 assembly

    main = Agent(system="t", tools=Toolbox(), model_name=None, verbose=False)
    sk_tools, sk_sop, sk_err = ma._agent_skill_gear("desktop-operator", meta, main)
    assert sk_err == ""
    toolbox, _ = ma._resolve_tools(main, meta.get("tools", ""))
    toolbox = Toolbox(*list(toolbox), *ma._agent_own_tools("desktop-operator", main), *sk_tools)
    system = ((system or "").rstrip() + "\n\n" + sk_sop).strip()

    sub = ma.SubAgent("desktop-operator", None, system, toolbox, registry=None, agent_id="do_e2e_1")
    names = {t.name for t in sub.agent.tools}
    assert KEY_MOUSE_TOOLS <= names, f"键鼠工具缺: {KEY_MOUSE_TOOLS - names}"   # 键鼠 10 工具在箱
    assert "坐标规范" in sub.agent.base_system and "操作纪律" in sub.agent.base_system   # SOP 在 system

    plan = ma._parse_assembly(meta)                        # image_feed 装配照常
    feeds = [it for it in plan if it.get("kind") == "image_feed"]
    assert feeds and feeds[0].get("image_feed") == "http://127.0.0.1:8765/frame"
    assert any(it.get("kind") == "file" and "desktop-operator.md" in json.dumps(it) for it in plan)


def test_desktop_operator_tools_dir_no_stray_tools():
    """agent 目录 tools/ 只剩 image_feed_poc.py（服务脚本，非 agt_register 工具）→
    _agent_own_tools 扫它不产出工具，键鼠能力唯一来源=技能。"""
    root = Path(__file__).resolve().parent.parent
    from agent import Agent
    main = Agent(system="t", tools=Toolbox(), model_name=None, verbose=False)
    own = ma._agent_own_tools("desktop-operator", main)
    assert [t.name for t in own] == [], f"agent 目录不应再有键鼠工具: {[t.name for t in own]}"
