"""演示分档投影的三段式效果（不烧 LLM token：用真实仓库源码当工具结果）。
对比同一组历史 turn 在不同 max_effective_context_window 下的投影：
  - 小窗口：触发 毕业压缩 + 折叠（最老轮退化为摘要，靠 recall 召回）
  - 大窗口(50万)：全部全量渲染（验证「大窗口内一切保持细节」）
跑完可删。
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from session import Session, Turn, Step, ToolCall

FILES = ["src/real_tools.py", "src/server.py", "src/workflow.py",
         "src/workflow_xml.py", "src/commands.py", "src/agent.py"]
_CONTENT = {f: Path(f).read_text(encoding="utf-8", errors="ignore") for f in FILES}


def build(window):
    s = Session(system="你是 Agt 助手。")
    s._current = None; s._frozen_renders.clear(); s._tier_boundaries = []
    s.max_effective_context_window = window
    for i, f in enumerate(FILES):
        cid = f"c{i}"
        s.toollog.record(cid, "read_file", {"path": f}, _CONTENT[f])
        t = Turn(user_message=f"读 {f} 讲讲它干啥",
                 steps=[Step(reasoning="", tool_calls=[ToolCall(call_id=cid)])],
                 answer=f"{f} 的职责见上。")
        t.summary = f"第{i+1}轮：读了 {Path(f).name}（{len(_CONTENT[f])}字）"
        s.turns.append(t)
    s.start_turn("基于这些文件总结整体架构")   # 进行中的当前轮（全量 tail）
    return s


def show(window):
    s = build(window)
    msgs = s.messages_for_llm()
    print(f"\n===== max_effective_context_window = {window} =====")
    folded = [m for m in msgs if isinstance(m.get("content"), str) and "已折叠" in m["content"]]
    tools = [m for m in msgs if m.get("role") == "tool"]
    print(f"boundaries={s._tier_boundaries}  折叠摘要块={len(folded)}  逐条tool渲染={len(tools)}")
    rendered_cids = {m.get("tool_call_id") for m in msgs if m.get("role") == "tool"}
    for i in range(len(s.turns)):
        name = Path(FILES[i]).name
        orig = len(_CONTENT[FILES[i]])
        cid = f"c{i}"
        if cid in rendered_cids:
            tm = next(m for m in msgs if m.get("tool_call_id") == cid)
            fr = s._frozen_renders.get(i)
            print(f"  turn{i} {name:18} level={fr[0] if fr else '?'}  {orig:>6}→{len(tm['content']):<6} 字")
        else:
            print(f"  turn{i} {name:18} [已折叠→摘要，recall 召回]  (原 {orig} 字)")
    if folded:
        print(f"  折叠摘要预览: {folded[0]['content'][:140].replace(chr(10),' ')}...")
    print(f"  → 投影估算 ≈ {s._estimate_tokens(msgs)} tokens")


# 基线：全量不压缩是多少
s0 = build(10 ** 9)
print(f"6 个真实源文件全量渲染基线 ≈ {s0._estimate_tokens(s0.messages_for_llm())} tokens\n（这是「不压缩」每轮都要发的量）")

show(500000)      # 50 万：全部全量（验证你的判断）
show(4000)        # 中：毕业压缩即可，无需折叠（常见情况）
show(150)         # 极小：压缩到头仍超 → 折叠最前档进摘要
