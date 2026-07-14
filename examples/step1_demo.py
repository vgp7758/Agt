"""Step 1 演示 —— 展示 LLMClient 的三种用法。

跑法：python step1_demo.py
"""
from llm_client import LLMClient


# ANSI 颜色：思考用灰色。Windows 11 的 Windows Terminal 支持。
GRAY = "\033[90m"
RESET = "\033[0m"


def demo_basic():
    print("\n" + "=" * 60)
    print("Demo 1: 普通对话（思考 + 正文分离）")
    print("=" * 60)
    client = LLMClient(enable_thinking=True)
    resp = client.chat([
        {"role": "system", "content": "你是一个简洁的助手。"},
        {"role": "user", "content": "用三句话解释什么是递归。"},
    ])
    print(f"\n{GRAY}[思考过程]（节选）{RESET}")
    snippet = resp.reasoning[:300]
    print(GRAY + snippet + ("..." if len(resp.reasoning) > 300 else "") + RESET)
    print("\n[最终回答]")
    print(resp.content)
    print(f"\n[usage] {resp.usage}")


def demo_stream():
    print("\n" + "=" * 60)
    print("Demo 2: 流式输出（思考灰色实时滚动 → 正文正常）")
    print("=" * 60)
    client = LLMClient(enable_thinking=True)

    # 用一个小状态机管理颜色：进入"思考段"时打一次灰色，进入"正文"时复位。
    # 这样不会每个 token 都重复 ANSI 码，输出才干净。
    state = {"phase": None}

    def on_reasoning(t):
        if state["phase"] != "reasoning":
            print(GRAY + "\n[思考] ", end="", flush=True)
            state["phase"] = "reasoning"
        print(t, end="", flush=True)

    def on_content(t):
        if state["phase"] == "reasoning":
            print(RESET, end="", flush=True)  # 结束灰色
        if state["phase"] != "content":
            print("\n[回答] ", end="", flush=True)
            state["phase"] = "content"
        print(t, end="", flush=True)

    resp = client.chat_stream(
        [{"role": "user", "content": "25 的平方根是多少？一句话。"}],
        on_reasoning=on_reasoning,
        on_content=on_content,
    )
    if state["phase"] == "reasoning":
        print(RESET, end="", flush=True)
    print(f"\n\n[usage] {resp.usage}")


def demo_no_thinking():
    print("\n" + "=" * 60)
    print("Demo 3: 关闭思考，对比 token 消耗")
    print("=" * 60)
    question = "北京和上海哪个城市常住人口更多？一句话。"
    on = LLMClient(enable_thinking=True).chat([{"role": "user", "content": question}])
    off = LLMClient(enable_thinking=False).chat([{"role": "user", "content": question}])
    print(f"\n思考开 → {on.usage}")
    print(f"  回答: {on.content.strip()[:80]}")
    print(f"\n思考关 → {off.usage}")
    print(f"  回答: {off.content.strip()[:80]}")
    print("\n（如果两者 token 数差不多，说明服务端没理会 enable_thinking，我们后面换别的方式控制）")


if __name__ == "__main__":
    demo_basic()
    demo_stream()
    demo_no_thinking()
