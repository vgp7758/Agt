"""视觉代看 JSON 结构化输出单测（用户提案 2026-11-06 · plan p_0cc1fdb1 新单）。

覆盖：
- llm_client._parse_img_json 五场景：纯 JSON / ```json 围栏 / 前后杂文本 / 坏 JSON→None /
  缺字段→None；
- llm_client._render_img_json 换算正确性：bbox [500,500→600,550] 在 2048x1152 上
  → 像素 [1024,576→1229,634]（round 语义：1228.8→1229、633.6→634）；orig_size 优先于
  frame_size 且带（原尺寸）标记；无基准 → 只留归一化、无基准行；
- llm_client.describe_image_structured 回退路径（mock describe_image_with_vision）：
  首次坏文本二次正常文本 → 走文本回退；首次正常 JSON → 走渲染；
- 两个消费端接线回归（零视觉调用，mock describe_image_structured）：
  agent._materialize_tool_result（frame=落盘图 PIL 尺寸 / orig=结果文本「原图 WxHpx」附注）
  与 session._imgfeed_proxy_text（frame=data URL PIL 尺寸 / orig=哨兵第4段 orig:WxH）。

隔离：全部 mock，不调真实网络、不写真实 img_desc_cache.db。
"""
import base64
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import pytest

import llm_client as L
import img_cache as IC
from agent import Agent
from session import Session, repo_images_dir

# 1x1 红点 PNG（PIL 可解码，供帧/落盘尺寸读取）
RED_PNG_B64 = ("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAAC0lEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")
DATA_URL = f"data:image/png;base64,{RED_PNG_B64}"

GOOD_JSON = ('{"summary": "一张登录页", "texts": [{"t": "登录", "bbox": [500,500,600,550]}], '
             '"elements": [{"name": "提交按钮", "bbox": [500,500,600,550], "desc": "蓝色"}], '
             '"details": "无异常"}')


# ── _parse_img_json ──────────────────────────────────────────

def test_parse_plain_json():
    r = L._parse_img_json(GOOD_JSON)
    assert isinstance(r, dict) and r["summary"] == "一张登录页"
    assert r["elements"][0]["name"] == "提交按钮"


def test_parse_json_fence():
    r = L._parse_img_json("```json\n" + GOOD_JSON + "\n```")
    assert r and r["summary"] == "一张登录页"
    # 纯 ``` 围栏（无语言标记）
    r2 = L._parse_img_json("```\n" + GOOD_JSON + "\n```")
    assert r2 and r2["summary"] == "一张登录页"


def test_parse_with_surrounding_noise():
    r = L._parse_img_json("好的，以下是分析结果：\n" + GOOD_JSON + "\n以上供参考。")
    assert r and r["summary"] == "一张登录页"


def test_parse_bad_json_returns_none():
    assert L._parse_img_json("这不是 JSON") is None
    assert L._parse_img_json('{"summary": "缺右括号') is None
    assert L._parse_img_json('{"summary": 123, "texts": [}') is None


def test_parse_missing_fields_returns_none():
    assert L._parse_img_json('{"texts": [{"t": "x", "bbox": [1,2,3,4]}]}') is None   # 无 summary 无 elements
    assert L._parse_img_json('{"elements": [{"name": "按钮"}]}') is None              # 缺 bbox
    assert L._parse_img_json('{"elements": [{"name": "按钮", "bbox": [1,2,3]}]}') is None   # bbox 3 个数
    assert L._parse_img_json('{"elements": [{"name": 1, "bbox": [1,2,3,4]}]}') is None      # name 非字符串
    assert L._parse_img_json("{}") is None
    assert L._parse_img_json("") is None
    assert L._parse_img_json(None) is None


# ── _render_img_json ─────────────────────────────────────────

def _render_json(frame_size=None, orig_size=None):
    data = L._parse_img_json(GOOD_JSON)
    return L._render_img_json(data, frame_size=frame_size, orig_size=orig_size)


def test_render_pixel_conversion_2048x1152():
    out = _render_json(frame_size=(2048, 1152))
    # round 语义锚定：500*2048/1000=1024 整；600*2048/1000=1228.8→1229；
    # 500*1152/1000=576 整；550*1152/1000=633.6→634
    assert "像素[1024,576→1229,634]" in out
    assert "归一化[500,500→600,550]" in out
    assert out.splitlines()[0] == "[坐标基准：2048x1152px | 归一化=0-1000]"   # 首行基准说明，无（原尺寸）
    assert "【整体】一张登录页" in out
    assert "【元素】提交按钮 归一化[500,500→600,550] 像素[1024,576→1229,634]：蓝色" in out
    assert "【文字】登录 像素[1024,576→1229,634]" in out
    assert "【细节】无异常" in out


def test_render_orig_size_priority():
    out = _render_json(frame_size=(2048, 1152), orig_size=(3000, 2000))
    assert out.splitlines()[0] == "[坐标基准：3000x2000px（原尺寸）| 归一化=0-1000]"
    # 600*3000/1000=1800、550*2000/1000=1100
    assert "像素[1500,1000→1800,1100]" in out
    assert "2048x1152" not in out      # 帧尺寸不参与基准


def test_render_no_basis_only_normalized():
    out = _render_json()               # frame_size / orig_size 均缺
    assert "坐标基准" not in out
    assert "像素[" not in out
    assert "归一化[500,500→600,550]" in out
    assert "【文字】登录 归一化[500,500→600,550]" in out


def test_render_skips_empty_sections():
    out = L._render_img_json({"summary": "纯风景图"})
    assert out == "【整体】纯风景图"
    out2 = L._render_img_json({"elements": [{"name": "按钮"}]})   # 无 desc → 无冒号段
    assert out2 == "【元素】按钮"


# ── describe_image_structured 回退路径 ───────────────────────

def test_structured_falls_back_to_text(monkeypatch):
    """首次坏文本（JSON 解析失败）→ 二次默认文本姿势原样返回。"""
    calls = []

    def fake_desc(data_url, instruction="", tries=3, timeout=60.0):
        calls.append(instruction)
        if len(calls) == 1:
            assert instruction == L.IMG_DESC_JSON_INSTRUCTION   # 第一发用 JSON 提示词
            return ("vis-A", "抱歉我没看懂")
        assert instruction == ""                                # 回退走默认文本提示词
        return ("vis-B", "【整体】一张登录页（文本版）")

    monkeypatch.setattr(L, "describe_image_with_vision", fake_desc)
    r = L.describe_image_structured(DATA_URL, frame_size=(2048, 1152))
    assert r == ("vis-B", "【整体】一张登录页（文本版）")      # 原样返回，不渲染
    assert len(calls) == 2


def test_structured_json_path_renders(monkeypatch):
    """首次正常 JSON → 走 _render_img_json（带基准行）。"""
    def fake_desc(data_url, instruction="", tries=3, timeout=60.0):
        assert instruction == L.IMG_DESC_JSON_INSTRUCTION
        return ("vis-A", GOOD_JSON)

    monkeypatch.setattr(L, "describe_image_with_vision", fake_desc)
    r = L.describe_image_structured(DATA_URL, frame_size=(2048, 1152))
    assert r[0] == "vis-A"
    assert r[1].startswith("[坐标基准：2048x1152px | 归一化=0-1000]")
    assert "【元素】提交按钮" in r[1]


def test_structured_none_when_no_card(monkeypatch):
    monkeypatch.setattr(L, "describe_image_with_vision", lambda *a, **k: None)
    assert L.describe_image_structured(DATA_URL) is None


# ── 消费端接线回归（mock describe_image_structured，零视觉调用） ──

def test_materialize_passes_sizes(monkeypatch, tmp_path):
    """非视觉分支：frame=落盘图 PIL 尺寸；orig=结果文本里已注入的「原图 WxHpx」附注。"""
    from session import repo_images_dir as _rid  # noqa: F401  （确保模块可导入）
    import llm_client
    captured = {}

    def fake_structured(data_url, frame_size=None, orig_size=None, tries=3, timeout=60.0):
        captured.update(frame_size=frame_size, orig_size=orig_size)
        return ("mock-vis", "[坐标基准：2048x1152px（原尺寸）| 归一化=0-1000]\n【整体】画面")

    monkeypatch.setattr(llm_client, "describe_image_structured", fake_structured)
    monkeypatch.setattr(IC, "get_desc", lambda *a, **k: None)
    monkeypatch.setattr(IC, "put_desc", lambda *a, **k: None)

    class _FakeSession:
        workspace = tmp_path

    class _FakeLLM:
        vision_supported = False

    class _FakeAgent:
        session = _FakeSession()
        llm = _FakeLLM()

    note = "[注：原图 3000x2000px，已等比缩至 2048x1366px（上限2048）] "
    result = Agent._materialize_tool_result(
        _FakeAgent(), f"截图：{note}{DATA_URL}\n后续文本", "shot", {}, "cj1")
    assert captured["orig_size"] == (3000, 2000)              # 附注里的原图尺寸
    assert captured["frame_size"] == (1, 1)                   # 落盘图（1x1 PNG）真实尺寸
    assert "mock-vis 代看" in result
    assert "坐标含归一化与像素两套" in result                  # 前缀文案已对齐
    img_tag = '<' + 'img>cj1_0.png<' + '/img>'
    assert img_tag in result                                  # img 标签照放（可回看像素）
    assert (repo_images_dir(tmp_path) / "cj1_0.png").exists()
    # 无附注（没缩放）→ orig 缺省（像素基准回落帧尺寸）
    captured.clear()
    Agent._materialize_tool_result(_FakeAgent(), f"截图：{DATA_URL}", "shot", {}, "cj2")
    assert captured["orig_size"] is None


def test_imgfeed_proxy_passes_sizes(monkeypatch, tmp_path):
    """帧代看：frame=data URL PIL 尺寸；orig=哨兵第4段 orig:WxH。"""
    import llm_client
    captured = {}

    def fake_structured(data_url, frame_size=None, orig_size=None, tries=3, timeout=60.0):
        captured.update(frame_size=frame_size, orig_size=orig_size)
        return ("mock-vis", "[坐标基准：1x1px（原尺寸）| 归一化=0-1000]\n【整体】画面")

    monkeypatch.setattr(llm_client, "describe_image_structured", fake_structured)
    monkeypatch.setattr(IC, "get_desc", lambda *a, **k: None)
    monkeypatch.setattr(IC, "put_desc", lambda *a, **k: None)

    s = Session.__new__(Session)
    s.workspace = tmp_path
    sen4 = f"@@IMGFEED@@1754300000@@{DATA_URL}@@orig:2160x1088@@"
    out = s._imgfeed_proxy_text(sen4)
    assert out.startswith("当前模型为非视觉")
    assert "mock-vis" in out and "【整体】画面" in out
    assert captured["frame_size"] == (1, 1)
    assert captured["orig_size"] == (2160, 1088)
    assert "帧 1x1px，原屏 2160x1088 等比缩放" in out          # 前缀尺寸段（新文案）
    assert "坐标含归一化与像素两套" in out
    # 3 段旧形态（无 orig）→ 不炸、orig 缺省
    captured.clear()
    out3 = s._imgfeed_proxy_text(f"@@IMGFEED@@1754300001@@{DATA_URL}@@")
    assert captured["orig_size"] is None and "mock-vis" in out3
    # 非哨兵输入原样透传
    assert s._imgfeed_proxy_text("随便一段文本") == "随便一段文本"


def test_prompt_ver_bumped():
    """v3-json：JSON 结构化改版后旧 v2-coord 文本缓存自然失效。"""
    assert IC.PROMPT_VER == "v3-json"
