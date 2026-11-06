"""子 Agent yml `skills:` 字段装备机制单测（plan p_922370a8 步骤 5）。

覆盖：
- _agent_skill_gear：技能 tools/ 注册进工具箱（外置工具协议不变）、
  SKILL.md SOP 提取（system 尾注入原料）、未知技能名清晰报错、
  无 skills 字段零影响、字符串形态兼容；
- system 尾拼接语义（persona 空/非空两态）；
- desktop-operator 实例化 e2e：真实声明文件 + 真实技能包 →
  键鼠工具【不】在箱（迁 stdin 服务后走 skill_use 命令口）/ 工具箱不含键鼠 /
  SOP 在 system / image_feed 装配段由技能声明提供（帧源 skill_command screen_capture）。

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
    b = ma._agent_skill_gear("x", {"skills": ["pack-a"]}, None)
    assert b["error"] == ""
    assert [t.name for t in b["tools"]] == ["hello_tool"]      # 外置工具协议不变（函数+docstring→Tool）
    assert b["sop"].startswith("## 装备技能：pack-a") and "SOP body" in b["sop"]


def test_skill_without_tools_dir_only_sop(ws):
    _mk_skill(ws, "pack-doc")
    b = ma._agent_skill_gear("x", {"skills": ["pack-doc"]}, None)
    assert (b["tools"], b["error"]) == ([], "") and "SOP body" in b["sop"]


# ── ② SOP 注入（system 尾拼接语义，与两处调用点同式）──────

def test_sop_merge_semantics():
    sop = "## 装备技能：pack-a\n\nSOP"
    # persona 非空（.md 旧格式）
    assert ((  "你是助手"  ).rstrip() + "\n\n" + sop).strip().startswith("你是助手\n\n## 装备技能")
    # persona 空（yml v2：正文在 assembly file 项，system 段只装 SOP）
    assert ((  ""          ).rstrip() + "\n\n" + sop).strip().startswith("## 装备技能")


# ── ③ 未知技能名清晰报错 ────────────────────────────────

def test_unknown_skill_error(ws):
    b = ma._agent_skill_gear("x", {"skills": ["ghost"]}, None)
    assert (b["tools"], b["sop"]) == ([], "")
    assert "ghost" in b["error"] and "已安装" in b["error"]              # 报错含名字 + 已装清单


def test_unknown_mixed_with_known(ws):
    _mk_skill(ws, "pack-a", script=SKILL_SCRIPT)
    b = ma._agent_skill_gear("x", {"skills": ["pack-a", "ghost"]}, None)
    assert (b["tools"], b["sop"]) == ([], "") and "ghost" in b["error"]     # 有未知 → 整体拒绝（不半装备）


# ── ④ 无 skills 字段零影响 / 字符串形态兼容 ──────────────

def test_no_skills_noop():
    empty = {"tools": [], "sop": "", "extras": [], "services": [], "error": ""}
    assert ma._agent_skill_gear("x", {}, None) == empty
    assert ma._agent_skill_gear("x", {"skills": []}, None) == empty


def test_string_form(ws):
    _mk_skill(ws, "pack-a", script=SKILL_SCRIPT)
    b = ma._agent_skill_gear("x", {"skills": "pack-a"}, None)
    assert b["error"] == "" and len(b["tools"]) == 1


# ── ⑤ desktop-operator 实例化 e2e（真实声明+真实技能包）──

def test_desktop_operator_instantiation():
    """desktop-operator 能实例化且装配形态正确（键鼠走 stdin 服务命令口，不装配成工具）。"""
    from agent_config import load_agent_yml, _split_frontmatter
    root = Path(__file__).resolve().parent.parent
    meta, system = load_agent_yml(root / ".agent" / "agents" / "desktop-operator" / "desktop-operator.yml")
    assert meta.get("skills") == ["desktop-ops"]
    assert system == ""                                    # yml v2：persona 在 assembly

    # 技能包形态：SKILL.md 无 tools 声明、键鼠全量走 server.py 命令口
    skill_dir = root / ".agent" / "skills" / "desktop-ops"
    fm, _ = _split_frontmatter((skill_dir / "SKILL.md").read_text(encoding="utf-8"))
    assert "tools" not in fm, f"键鼠已迁 stdin 服务，SKILL.md 不应再声明 tools: {fm.get('tools')}"
    assert (skill_dir / "server.py").is_file()                                   # 命令口本体（skill_use 拉起）
    assert (skill_dir / "scripts" / "commands" / "screen_capture.py").is_file()   # 帧命令实现

    main = Agent(system="t", tools=Toolbox(), model_name=None, verbose=False)
    sk = ma._agent_skill_gear("desktop-operator", meta, main)
    assert sk["error"] == ""
    assert sk["tools"] == [], f"技能不该再注册键鼠工具: {[t.name for t in sk['tools']]}"
    toolbox, _ = ma._resolve_tools(main, meta.get("tools", ""))
    toolbox = Toolbox(*list(toolbox), *ma._agent_own_tools("desktop-operator", main), *sk["tools"])
    system = ((system or "").rstrip() + "\n\n" + sk["sop"]).strip()

    sub = ma.SubAgent("desktop-operator", None, system, toolbox, registry=None, agent_id="do_e2e_1")
    names = {t.name for t in sub.agent.tools}
    assert not (KEY_MOUSE_TOOLS & names), f"键鼠应走 skill_use 命令口，不该在箱: {KEY_MOUSE_TOOLS & names}"
    assert "坐标规范" in sub.agent.base_system and "操作纪律" in sub.agent.base_system   # SOP 在 system

    plan = ma._parse_assembly(meta)                        # yml 基线：image_feed 段已迁进技能 SKILL.md
    assert not [it for it in plan if it.get("kind") == "image_feed"]   # yml 不再手写 image_feed（瘦身）
    assert meta.get("services") in (None, [], {}), "yml services 声明已收进技能（不再有 8765 帧服务）"
    # 技能自带装配声明合并 → image_feed 段回来（帧源=技能命令口 screen_capture，装备时冻结）
    plan2, _, errs = ma._apply_extra_assemblies(plan, None, sk["extras"])
    assert errs == [] and sk["extras"], f"desktop-ops 应带装配声明: {sk['extras']}"
    assert sk["extras"][0]["position"] == "after:steps"
    assert sk["extras"][0]["_skill_src"] == "desktop-ops"     # 来源标记（装卸闭环按标记撤销）
    feeds = [it for it in plan2 if it.get("kind") == "image_feed"]
    assert len(feeds) == 1, f"应插 1 个 image_feed 段: {feeds}"
    frame_src = str(feeds[0].get("image_feed"))
    assert "skill_command" in frame_src and "screen_capture" in frame_src, \
        f"帧提供者应为技能命令口 screen_capture: {feeds}"
    assert feeds[0].get("_skill_src") == "desktop-ops"
    assert any(it.get("kind") == "file" and "desktop-operator.md" in json.dumps(it) for it in plan2)


def test_desktop_operator_tools_dir_no_stray_tools():
    """agent 目录 tools/ 只剩 image_feed_poc.py（服务脚本，非 agt_register 工具）→
    _agent_own_tools 扫它不产出工具，键鼠能力唯一来源=技能。"""
    root = Path(__file__).resolve().parent.parent
    from agent import Agent
    main = Agent(system="t", tools=Toolbox(), model_name=None, verbose=False)
    own = ma._agent_own_tools("desktop-operator", main)
    assert [t.name for t in own] == [], f"agent 目录不应再有键鼠工具: {[t.name for t in own]}"
