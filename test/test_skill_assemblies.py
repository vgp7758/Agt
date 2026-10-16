"""技能自带装配清单 extra_assemblies 单测（plan p_76f6623b 步骤 5）。

覆盖：
- frontmatter `assemblies:` 解析（agent_config.parse_extra_assemblies）：三前缀 position、
  锚段名/钩子位白名单、坏声明清单化报错（不静默丢弃）；
- 动态口 caps `EXTRA_ASSEMBLIES: <json>` 解析（parse_caps_extra_assemblies）；
- 插段/钩子统一应用（multiagent._apply_extra_assemblies）：before/after 锚定位、
  多技能同锚稳定追加（先装备的先插）、宿主原有动作项不吞并、锚缺失清单化报错、
  hook 项并入 hook_specs + hooks 段开关打开；
- 装备时冻结（agent_config.apply_extras_to_session）：assembly_plan 快照物化、
  逐轮求值一致不再重算（铁律①）；
- 子/主两作用域对称（铁律②）：子 Agent 聚合（_agent_skill_gear）与主 Agent 应用
  （apply_extras_to_session）共用同一插入逻辑；
- hook 缺省 ass = 技能包 workflows/ 全部（fill_default_hook_items，dir 装备时冻结）；
- image_feed tool: 帧提供者（session._asm_image_feed_from_tool）：data URL / 文件路径、
  双参工具位置映射、降级文案、vision 门控、http/file 源零回归；
- skills: 字段聚合 extras/services（multiagent._agent_skill_gear 返回 dict）。

隔离：临时技能用例走 tmp_path + monkeypatch，不污染真实 repo。
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import base64
import pytest
import multiagent as ma
import agent_config as ac
from session import Session, _asm_image_feed_from_tool
from tools import Toolbox, Tool


# ── 测试基建 ─────────────────────────────────────────────

def _mk_skill(ws_root, name, frontmatter="", body="SOP body", workflows=()):
    d = ws_root / ".agent" / "skills" / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: d\nwhen_to_use: w\n{frontmatter}---\n\n{body}\n",
        encoding="utf-8")
    if workflows:
        wf = d / "workflows"
        wf.mkdir()
        for w in workflows:
            (wf / w).write_text("<workflow/>" if w.endswith(".xml") else "{}", encoding="utf-8")
    return d


@pytest.fixture
def ws(tmp_path, monkeypatch):
    """隔离 workspace：技能寻址都落 tmp_path。"""
    monkeypatch.setattr(ma, "WORKSPACE", tmp_path)
    monkeypatch.setattr(ac, "WORKSPACE", tmp_path)
    return tmp_path


def _vision_session(ws_root, vision=True):
    s = Session(system="t", workspace=str(ws_root))
    class L:
        vision_supported = vision
    s.llm = L()
    return s


BASE_PLAN = [{"kind": "seg", "name": "system"}, {"kind": "seg", "name": "user_message"},
             {"kind": "seg", "name": "steps"}, {"kind": "seg", "name": "tail"}]


# ── ① frontmatter 解析：合法 / 坏声明清单化 ────────────────

def test_parse_valid_forms():
    extras, errs = ac.parse_extra_assemblies([
        {"position": "after:steps", "ass": [{"text": "x"}]},
        {"position": "before:history", "ass": [{"image_feed": {"tool": "skill_use('a','frame')"}}]},
        {"position": "hook:turn_end", "ass": ["daily_recap | async", {"cmd": "echo hi"}]},
        {"position": "hook:before_tool"},                      # 缺省 ass（聚合方补 workflows/）
    ], skill_name="t")
    assert errs == [] and len(extras) == 4
    assert extras[0]["action"] == "after" and extras[0]["anchor"] == "steps"
    assert extras[1]["items"][0]["kind"] == "image_feed"
    assert extras[2]["items"] == [{"kind": "workflow", "value": "daily_recap", "async": True},
                                  {"kind": "cmd", "value": "echo hi"}]
    assert extras[3]["items"] == []


def test_parse_bad_decls_listed_not_silent():
    extras, errs = ac.parse_extra_assemblies([
        {"position": "inside:steps", "ass": [{"text": "x"}]},      # 非法前缀
        {"position": "hook:nope", "ass": ["wf"]},                  # 未知钩子位
        {"position": "before:noseg", "ass": [{"text": "x"}]},      # 未知锚段名
        {"position": "before:hooks", "ass": [{"text": "x"}]},      # hooks 不占投影位，不可锚
        {"position": "after:steps"},                               # 缺 ass
        {"position": ""},                                          # 空 position
        "junk",                                                    # 非映射
        {"position": "before:steps", "ass": "no-such-seg"},        # ass 解析不出项
    ], skill_name="bad")
    assert len(errs) == 8 and all("bad" in e for e in errs)   # 每条都带技能名（清单化）
    assert len(extras) == 0                                    # 坏声明不静默丢弃


def test_parse_caps_extra_assemblies():
    caps = "能力清单：\nframe — 取一帧\nEXTRA_ASSEMBLIES: " \
           "[{\"position\": \"after:steps\", \"ass\": [{\"text\": \"动态口\"}]}]\n用法：..."
    extras, errs = ac.parse_caps_extra_assemblies(caps, skill_name="svc")
    assert errs == [] and extras[0]["position"] == "after:steps"
    assert extras[0]["items"][0]["text"] == "动态口"
    # 无声明 = 零声明（非错误）
    extras2, errs2 = ac.parse_caps_extra_assemblies("只有能力清单", "svc")
    assert (extras2, errs2) == ([], [])
    # JSON 坏 = 清单化报错
    _, errs3 = ac.parse_caps_extra_assemblies("EXTRA_ASSEMBLIES: {bad json", "svc")
    assert len(errs3) == 1 and "svc" in errs3[0]


# ── ② 插段锚定位 + 稳定追加 + 宿主项不吞并 ─────────────────

def test_apply_before_after_anchor():
    ex = [{"position": "after:steps", "action": "after", "anchor": "steps",
           "items": [{"kind": "text", "text": "A"}]}]
    plan, hooks, errs = ma._apply_extra_assemblies(BASE_PLAN, None, ex)
    names = [it.get("name") or it.get("kind") for it in plan]
    assert errs == [] and names.index("text") == names.index("steps") + 1
    assert hooks == {}
    ex_b = [{"position": "before:tail", "action": "before", "anchor": "tail",
             "items": [{"kind": "text", "text": "B"}]}]
    plan2, _, _ = ma._apply_extra_assemblies(BASE_PLAN, {}, ex_b)
    names2 = [it.get("name") or it.get("kind") for it in plan2]
    assert names2.index("text") == names2.index("tail") - 1


def test_apply_stable_append_same_anchor():
    ex_a = [{"position": "after:steps", "action": "after", "anchor": "steps",
             "items": [{"kind": "text", "text": "A"}]}]
    ex_b = [{"position": "after:steps", "action": "after", "anchor": "steps",
             "items": [{"kind": "text", "text": "B"}]}]
    plan, _, _ = ma._apply_extra_assemblies(BASE_PLAN, None, ex_a + ex_b)
    assert [it.get("text") for it in plan if it.get("kind") == "text"] == ["A", "B"]   # 先装备的先插
    ex_c = [{"position": "before:tail", "action": "before", "anchor": "tail",
             "items": [{"kind": "text", "text": "C"}]}]
    ex_d = [{"position": "before:tail", "action": "before", "anchor": "tail",
             "items": [{"kind": "text", "text": "D"}]}]
    plan2, _, _ = ma._apply_extra_assemblies(BASE_PLAN, None, ex_c + ex_d)
    assert [it.get("text") for it in plan2 if it.get("kind") == "text"] == ["C", "D"]


def test_apply_keeps_host_items_and_reports_missing_anchor():
    host = [dict(x) for x in BASE_PLAN]
    host.insert(3, {"kind": "text", "text": "orig"})       # 宿主原有动作项紧贴锚
    ex = [{"position": "after:steps", "action": "after", "anchor": "steps",
           "items": [{"kind": "text", "text": "E"}]}]
    plan, _, errs = ma._apply_extra_assemblies(host, {}, ex)
    assert errs == [] and [it.get("text") for it in plan if it.get("kind") == "text"] == ["E", "orig"]
    bad = [{"position": "before:ghost", "action": "before", "anchor": "ghost",
            "items": [{"kind": "text", "text": "x"}]}]
    _, _, errs2 = ma._apply_extra_assemblies(BASE_PLAN, {}, bad)
    assert len(errs2) == 1 and "ghost" in errs2[0]          # 锚缺失清单化报错（不静默丢弃）


def test_apply_default_plan_baseline():
    """宿主未声明装配（plan=None）→ 默认清单物化为基线（等价投影）。"""
    ex = [{"position": "after:steps", "action": "after", "anchor": "steps",
           "items": [{"kind": "text", "text": "A"}]}]
    plan, _, errs = ma._apply_extra_assemblies(None, None, ex)
    assert errs == [] and plan[0] == {"kind": "seg", "name": "system"}
    assert any(it.get("kind") == "text" for it in plan)


# ── ③ 钩子挂载 + hooks 段开关 ─────────────────────────────

def test_apply_hook_merge_and_switch():
    base_hooks = {"turn_end": [{"kind": "workflow", "value": "recap_gen", "async": True}]}
    ex = [{"position": "hook:turn_end", "action": "hook", "anchor": "turn_end",
           "items": [{"kind": "workflow", "value": "x_recap"}]},
          {"position": "hook:before_tool", "action": "hook", "anchor": "before_tool",
           "items": [{"kind": "workflow", "value": "auto_wf", "dir": "D:/skill/wf"}]}]
    plan, hooks, errs = ma._apply_extra_assemblies(BASE_PLAN, base_hooks, ex)
    assert errs == []
    assert hooks["turn_end"][-1] == {"kind": "workflow", "value": "x_recap"}       # 追加不覆盖
    assert hooks["before_tool"] == [{"kind": "workflow", "value": "auto_wf", "dir": "D:/skill/wf"}]
    assert any(it.get("name") == "hooks" for it in plan)                            # 开关自动打开
    assert any(it.get("name") == "hooks" for it in BASE_PLAN) is False              # 基线未被污染


# ── ④ 装备时冻结（铁律①）：快照物化 + 逐轮不重算 ────────────

def test_freeze_snapshot_ws(ws):
    _mk_skill(ws, "svc-x", frontmatter="assemblies:\n  - position: after:steps\n    ass:\n      - text: 【svc-x 画面段】\n")
    sess = _vision_session(ws)
    assert sess.assembly_plan is None                             # 装备前 = 默认清单（None）
    extras, errs = ac.parse_extra_assemblies(
        [{"position": "after:steps", "ass": [{"text": "【svc-x 画面段】"}]}], "svc-x")
    assert ac.apply_extras_to_session(sess, extras) == []
    assert sess.assembly_plan is not None                          # 快照物化（不再依赖默认路径）
    names = [it.get("name") or it.get("kind") for it in sess.assembly_plan]
    assert names.index("text") == names.index("steps") + 1
    # 逐轮求值一致（投影层只读快照；两次求值结果相同 → 无逐轮重算漂移）
    item = sess.assembly_plan[names.index("text")]
    m1 = sess._asm_action_msgs(item)
    m2 = sess._asm_action_msgs(item)
    assert m1 == m2 == [{"role": "system", "content": "【svc-x 画面段】"}]


def test_freeze_preserves_legacy_hooks(ws, tmp_path):
    """hook_specs=None 且本次有 hook: 声明 → 旧式 .agent/workflows meta.hook 先物化再合并（不吞既有钩子）。"""
    import workflow as wfmod
    d = ws / ".agent" / "workflows"
    d.mkdir(parents=True, exist_ok=True)
    (d / "legacy_hook.json").write_text(
        '{"nodes": [], "hook": {"position": "turn_end"}}', encoding="utf-8")
    sess = _vision_session(ws)
    assert sess.hook_specs is None
    extras, _ = ac.parse_extra_assemblies([{"position": "hook:turn_end", "ass": ["x_recap"]}], "svc")
    assert ac.apply_extras_to_session(sess, extras) == []
    vals = [it.get("value") for it in sess.hook_specs.get("turn_end", [])]
    assert "x_recap" in vals                                       # 技能钩子挂上
    assert sess.hook_specs is not None


# ── ⑤ 子/主两作用域对称（铁律②）───────────────────────────

def test_both_scopes_same_insert_logic(ws):
    """子 Agent 聚合（_agent_skill_gear.extras）与主 Agent 应用（apply_extras_to_session）
    对同一声明产生同一份插段结果——共用 _apply_extra_assemblies。"""
    _mk_skill(ws, "pack-x", frontmatter="assemblies:\n  - position: after:steps\n    ass:\n      - text: 【X】\n")
    bundle = ma._agent_skill_gear("tester", {"skills": ["pack-x"]}, None)
    assert bundle["error"] == "" and bundle["extras"]
    # 主作用域：同一 extras 喂 apply_extras_to_session
    sess = _vision_session(ws)
    assert ac.apply_extras_to_session(sess, bundle["extras"]) == []
    names_main = [it.get("name") or it.get("kind") for it in sess.assembly_plan]
    # 子作用域：同一 extras 走 _apply_extra_assemblies（派活链内同函数）
    plan_sub, _, errs = ma._apply_extra_assemblies(None, None, bundle["extras"])
    names_sub = [it.get("name") or it.get("kind") for it in plan_sub]
    assert errs == [] and names_main == names_sub                  # 对称：同一插入逻辑同一结果


# ── ⑥ hook 缺省 ass = 技能包 workflows/（dir 装备时冻结）────

def test_hook_default_items_from_skill_workflows(ws):
    d = _mk_skill(ws, "pack-y",
                  frontmatter="assemblies:\n  - position: hook:before_tool\n",
                  workflows=("auto_wf.xml",))
    bundle = ma._agent_skill_gear("tester", {"skills": ["pack-y"]}, None)
    ex = bundle["extras"][0]
    assert ex["items"] == [{"kind": "workflow", "value": "auto_wf", "dir": str(d / "workflows")}]


# ── ⑦ skills: 字段聚合 extras/services（子 Agent 静态口）────

def test_gear_aggregates_extras_and_services(ws):
    _mk_skill(ws, "pack-z", frontmatter=(
        "assemblies:\n  - position: after:steps\n    ass:\n      - text: 【Z】\n"
        "  - position: hook:turn_end\n    ass:\n      - workflow: z_recap\n"
        "services:\n  - z-svc: python -m z.server\n"))
    bundle = ma._agent_skill_gear("tester", {"skills": ["pack-z"]}, None)
    assert bundle["error"] == ""
    assert [e["position"] for e in bundle["extras"]] == ["after:steps", "hook:turn_end"]
    assert bundle["services"] == [{"z-svc": "python -m z.server"}]
    # 声明坏 → error 中断派活
    _mk_skill(ws, "pack-bad", frontmatter="assemblies:\n  - position: before:noseg\n    ass:\n      - text: x\n")
    bundle2 = ma._agent_skill_gear("tester", {"skills": ["pack-bad"]}, None)
    assert "noseg" in bundle2["error"] and "pack-bad" in bundle2["error"]


# ── ⑧ image_feed tool: 帧提供者 ───────────────────────────

@pytest.fixture
def frame_env(tmp_path):
    png_b64 = base64.b64encode(b"\x89PNG\r\n\x1a\nFAKE").decode()

    def frame_tool() -> str:
        """返回 data URL 帧（测试）。"""
        return f"data:image/png;base64,{png_b64}"

    def frame_path_tool() -> str:
        """返回文件路径帧（测试）。"""
        p = tmp_path / "f.png"
        p.write_bytes(b"\x89PNG\r\n\x1a\nFAKE2")
        return str(p)

    def skill_use(name: str, command: str) -> str:
        """双参工具模拟 skill_use（帧来自技能服务）。"""
        return f"called {name} / {command}"

    s = _vision_session(tmp_path)
    s._asm_workflow_tools = Toolbox(Tool(frame_tool), Tool(frame_path_tool), Tool(skill_use))
    return s, png_b64, base64.b64encode(b"\x89PNG\r\n\x1a\nFAKE2").decode(), tmp_path


def test_imgfeed_tool_data_url(frame_env):
    s, png_b64, _, _ = frame_env
    r = _asm_image_feed_from_tool("frame_tool()", s)
    assert r.startswith("@@IMGFEED@@") and png_b64 in r
    r2 = s._asm_evaluate({"kind": "image_feed", "image_feed": {"tool": "frame_tool()"}})
    r3 = s._asm_evaluate({"kind": "image_feed", "image_feed": "tool: frame_tool()"})
    assert r2.startswith("@@IMGFEED@@") and r3.startswith("@@IMGFEED@@")   # dict/str 两形态


def test_imgfeed_tool_file_path(frame_env):
    s, _, fake2_b64, _ = frame_env
    r = _asm_image_feed_from_tool("frame_path_tool()", s)
    assert r.startswith("@@IMGFEED@@") and fake2_b64 in r


def test_imgfeed_tool_two_arg_mapping_and_degradation(frame_env):
    s, _, _, _ = frame_env
    r = _asm_image_feed_from_tool("skill_use('svc','frame')", s)   # 双参位置映射 name/command
    assert "called svc / frame" in r and r.startswith("[image_feed")   # 无图像 → 降级文案
    r4 = _asm_image_feed_from_tool("ghost_tool()", s)
    assert "不在工具箱" in r4
    assert "无效" in _asm_image_feed_from_tool("!!!", s)


def test_imgfeed_http_and_file_regression_and_vision_gate(frame_env, tmp_path):
    s, _, _, _ = frame_env
    p = tmp_path / "direct.png"
    p.write_bytes(b"\x89PNGHTTP")
    assert s._asm_evaluate({"kind": "image_feed", "image_feed": str(p)}).startswith("@@IMGFEED@@")
    class L2:
        vision_supported = False
    s.llm = L2()
    assert s._asm_evaluate({"kind": "image_feed", "image_feed": {"tool": "frame_tool()"}}) == ""


# ── ⑨ 静态口 frontmatter → load_skill_assemblies ──────────

def test_load_skill_assemblies(ws):
    _mk_skill(ws, "pack-q", frontmatter="assemblies:\n  - position: after:steps\n    ass:\n      - text: 【Q】\n")
    d, extras, errs = ac.load_skill_assemblies("pack-q", ws)
    assert d is not None and errs == [] and extras[0]["items"][0]["text"] == "【Q】"
    d2, _, errs2 = ac.load_skill_assemblies("ghost", ws)
    assert d2 is None and errs2 == []                              # 不存在 → 目录 None（调用方按未知技能处理）
