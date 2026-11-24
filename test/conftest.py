"""pytest 全局夹具（src/ 侧测试共用）。

外部生态技能目录 ~/.agents/skills/ 是免激活直接生效的（用户裁定 2026-11-24），且跟 Path.home()
走、不受 AGT_HOME 隔离——开发机上真实装了的外部技能会漏进所有技能断言（load_skills_index(ws) == []
之类会随机翻红）。这里把 agent_config._EXT_SKILLS_ROOT 指到每个测试独立的【不存在】目录；
要测外部层的用例自己再 monkeypatch 成带内容的目录（见 test_global_skills.py 的 xroot）。
"""
import sys
from pathlib import Path

import pytest

_SRC = str(Path(__file__).resolve().parent.parent / "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)


@pytest.fixture(autouse=True)
def _isolate_external_skills(tmp_path, monkeypatch):
    import agent_config as ac
    monkeypatch.setattr(ac, "_EXT_SKILLS_ROOT", tmp_path / "_no_external_skills")
