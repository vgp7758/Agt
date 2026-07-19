"""测试 full_demo.xml 工作流：三条意图路径（计算/查询/default）"""
import sys, re
sys.path.insert(0, "src")
sys.stdout.reconfigure(encoding="utf-8")
from types import SimpleNamespace
from workflow_xml import xml_to_canvas
import workflow

CANVAS = xml_to_canvas(open(".agent/workflows/full_demo.xml", encoding="utf-8").read())


class MockLLM:
    """意图分类按编号返回；其它返回问候语。"""
    def chat(self, msgs, **kw):
        last = [m for m in msgs if m.get("role") == "user"][-1]["content"]
        if "判断用户输入属于" in last:
            m = re.search(r"用户输入：(.+)", last)
            q = (m.group(1).strip() if m else "")
            if "计算" in q or "+" in q:
                return SimpleNamespace(content="1")
            if "查询" in q:
                return SimpleNamespace(content="2")
            return SimpleNamespace(content="0")
        return SimpleNamespace(content="（LLM问候）你好")


class MockTools:
    def __contains__(self, n):
        return n == "to_uppercase"

    def call(self, n, a):
        return str(a.get("text", "")).upper()


def run(question):
    return workflow.execute(CANVAS, {"question": question},
                            tools=MockTools(), llm=MockLLM(),
                            workspace=workflow.Path("."), return_exit_dict=True)


print("=== full_demo.xml 三条意图路径 ===")
ok = True
for q, expect_substr in [("计算 3+5", "计算分支"), ("查询天气", "LLM"), ("闲聊你好", "LLM")]:
    try:
        r = run(q)
        result = str(r.get("result", ""))
        passed = expect_substr in result
        ok = ok and passed
        print(f"  [{'✅' if passed else '❌'}] {q!r}: {result[:80]}")
    except Exception as e:
        ok = False
        print(f"  [❌] {q!r}: {type(e).__name__}: {str(e)[:80]}")

print(f"\n{'✅ full_demo 全部通过' if ok else '❌ 有失败'}")
sys.exit(0 if ok else 1)
