"""Step 5 演示 —— 自主 Agent 的 ReAct 主循环。

三个演示，难度递进：
  demo_single()  : 单步任务（回归验证循环没破坏简单场景）。
  demo_chain()   : 多步链式 —— (15 + 27) × 3，自主连续调用两次工具。
  demo_complex() : 多步推理 —— 矩形周长 vs 面积，需要多次工具调用 + 比较。

跑法：python step5_demo.py
"""
from agent import Agent
from tools import DEFAULT_TOOLS

SYSTEM = (
    "你是一个会使用计算工具的自主助手。遇到任何数学计算都通过工具完成，绝不心算。"
    "需要多步计算时一步一步来：每步调一次工具，看到结果后再决定下一步。"
)


def demo_single():
    print("=" * 60)
    print("Demo 1: 单步任务（回归验证）")
    print("=" * 60)
    Agent(system=SYSTEM, tools=DEFAULT_TOOLS).run("帮我算 8 乘以 9。")


def demo_chain():
    print("\n" + "=" * 60)
    print("Demo 2: 多步链式 —— 计算 (15 + 27) × 3")
    print("=" * 60)
    Agent(system=SYSTEM, tools=DEFAULT_TOOLS).run("请计算 (15 + 27) × 3 的结果。")


def demo_complex():
    print("\n" + "=" * 60)
    print("Demo 3: 多步推理 —— 矩形周长 vs 面积")
    print("=" * 60)
    Agent(system=SYSTEM, tools=DEFAULT_TOOLS).run(
        "有一个矩形，长 12、宽 8。请分别算出周长（2×(长+宽)）和面积（长×宽），"
        "然后告诉我周长和面积哪个数值更大。"
    )


if __name__ == "__main__":
    demo_single()
    demo_chain()
    demo_complex()
