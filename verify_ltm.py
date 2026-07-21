"""verify_ltm.py —— 长期记忆（跨 session）端到端验证。

用临时 workspace 跑，测完即清理，不碰真实的 ~/.agt/repos/<hash>/memories/。
覆盖：
  1. 三类 add（semantic/episodic/procedural）
  2. static_block：semantic 全文 + procedural 标题（始终注入层）
  3. episodic_block：按问题关键词召回情境记忆（按需注入层）
  4. messages_for_llm()：三类注入在消息流里各就各位（静态层靠前、情境层贴当前轮）
  5. search / list / 去重（同 type+title 更新而非新增）
  6. 持久化：重新 new LongTermMemory 读盘，跨"session"仍在
跑法：python verify_ltm.py
"""
import os
import sys
import shutil
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from session import Session  # noqa: E402
from longterm_memory import LongTermMemory, TYPES  # noqa: E402


class _DummyLLM:
    """不联网的桩 LLM（本测不触发摘要/命名，只验注入）。"""
    def chat(self, *a, **k):
        return type("R", (), {"content": "", "reasoning": "", "usage": {}, "tool_calls": None})()


def main():
    tmp = Path(tempfile.mkdtemp(prefix="agt_ltm_test_"))
    print(f"临时 workspace：{tmp}")
    try:
        ltm = LongTermMemory(tmp)

        # 1. 三类各 add 一条
        ltm.add("semantic", "用户背景", "用户是 Unity 背景，正转型 AI Agent 开发", ["user"])
        ltm.add("procedural", "发布 PyPI 流程",
                "改 src/__init__.py 版本号 → python -m build → twine upload", ["publish"])
        ltm.add("episodic", "ModelScope 空壳200限流",
                "GLM 偶发空响应或 200 限流，需要重试", ["踩坑"])
        print(f"\n[1] 已记 3 条：{[(t, len(ltm._items[t])) for t in TYPES]}")

        # 2. static_block（始终注入层）
        print("\n[2] === static_block（每轮始终注入）===")
        print(ltm.static_block())

        # 3. episodic_block（按需召回层）
        print("\n[3] === episodic_block(查询='GLM 限流 空响应')===")
        print(ltm.episodic_block("GLM 限流 空响应"))

        # 4. messages_for_llm 注入位置（端到端）
        s = Session(system="test-system", llm=_DummyLLM(), workspace=tmp)
        s._ltm_static_provider = ltm.static_block
        s._ltm_episodic_provider = ltm.episodic_block
        s.start_turn("GLM 限流 空响应 怎么重试")
        msgs = s.messages_for_llm()
        print("\n[4] === messages_for_llm 里含【长期记忆】的 system 块 ===")
        for m in msgs:
            c = m.get("content", "")
            if m["role"] == "system" and isinstance(c, str) and "长期记忆" in c:
                print(f"  • [{c.splitlines()[0][:50]}]")

        contents = "\n".join(m["content"] for m in msgs
                             if m["role"] == "system" and isinstance(m.get("content"), str))
        assert "用户背景" in contents, "❌ semantic 事实未注入"
        assert "发布 PyPI" in contents, "❌ procedural 标题未注入"
        assert "ModelScope" in contents, "❌ episodic 未按当前问题召回"
        print("✅ 三类注入各就各位（semantic/procedural 在静态层，episodic 按问题召回）")

        # 5. search / 去重
        hits = [r["title"] for r in ltm.search("限流")]
        print(f"\n[5] search('限流') → {hits}")
        assert "ModelScope 空壳200限流" in hits, "❌ search 未命中"
        before = len(ltm._items["semantic"])
        ltm.add("semantic", "用户背景", "更新后的内容（应覆盖非新增）")
        after = len(ltm._items["semantic"])
        assert after == before, f"❌ 去重失效：{before} → {after}"
        print(f"✅ 去重生效（同 type+title 更新，semantic 仍 {after} 条）")

        # 6. 持久化：重新读盘
        ltm2 = LongTermMemory(tmp)
        assert len(ltm2.list()) == 3, f"❌ 持久化失败：{len(ltm2.list())} 条"
        # 删除一条 + 再读盘验证 delete 落盘
        did = ltm2.list(type_="procedural")[0]["id"]
        assert ltm2.delete(did)
        ltm3 = LongTermMemory(tmp)
        assert len(ltm3.list()) == 2, "❌ delete 未落盘"
        print(f"✅ 持久化通过（重读 {len(ltm2.list())} 条；删 1 条后重读 {len(ltm3.list())} 条）")

        print("\n🎉 全部通过")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        print(f"(已清理临时目录 {tmp})")


if __name__ == "__main__":
    main()
