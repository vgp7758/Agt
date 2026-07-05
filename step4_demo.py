"""Step 4 演示 —— Function Calling 完整往返。

流程：用户提问 → 模型决定调用工具 → 我们执行 → 回填结果 → 模型给最终答案。
三部分：
  demo_schema() : 打印自动生成的工具 schema，看清发给服务端的是什么。
  demo_basic()  : 23 × 17 = ? 的完整往返。
  demo_error()  : 100 ÷ 0，工具出错时模型自己处理。

跑法：python step4_demo.py
"""
import json

from conversation import Conversation
from llm_client import LLMClient
from tools import DEFAULT_TOOLS

GRAY, RESET = "\033[90m", "\033[0m"


def demo_schema():
    print("=" * 60)
    print("Part 1: 自动生成的工具 schema（这就是发给服务端的东西）")
    print("=" * 60)
    for t in DEFAULT_TOOLS:
        print(f"\n● {t.name}")
        print(json.dumps(t.schema, ensure_ascii=False, indent=2))


def _run_one_round(client, conv, tools, user_q):
    """跑一次完整往返：提问 → (可能)调工具 → 回填 → 最终回答。"""
    print(f"\n🧑 用户: {user_q}")
    conv.add_user(user_q)
    resp = client.chat(conv.messages, tools=tools.schemas())

    if not resp.tool_calls:
        print(f"🤖 模型直接回答（未调工具）: {resp.content.strip()}")
        conv.add_assistant(resp.content)
        return

    # 记录助手的工具调用消息（必须原样结构回填，模型才接得上）
    conv.add_assistant(resp.content or "", tool_calls=resp.tool_calls)
    for tc in resp.tool_calls:
        print(f"🔧 模型调用工具: {tc['name']}({tc['arguments']})")
        result = tools.call(tc["name"], tc["arguments"])
        print(f"   → 结果: {result}")
        conv.add_tool_result(tc["id"], result)

    print(f"{GRAY}（工具结果已回填，请模型给出最终答案）{RESET}")
    final = client.chat(conv.messages, tools=tools.schemas())
    conv.add_assistant(final.content)
    print(f"🤖 最终回答: {final.content.strip()}")


def demo_basic():
    print("\n" + "=" * 60)
    print("Part 2: 完整往返 —— 23 × 17 = ?")
    print("=" * 60)
    client = LLMClient(enable_thinking=False)
    conv = Conversation(system="你是一个会使用计算工具的助手。遇到数学计算务必调用工具，不要自己心算。")
    _run_one_round(client, conv, DEFAULT_TOOLS, "帮我算一下 23 乘以 17 等于多少？")


def demo_error():
    print("\n" + "=" * 60)
    print("Part 3: 工具执行出错 —— 100 ÷ 0")
    print("=" * 60)
    client = LLMClient(enable_thinking=False)
    conv = Conversation(system="你是一个会使用计算工具的助手。遇到数学计算务必调用工具。")
    _run_one_round(client, conv, DEFAULT_TOOLS, "把 100 除以 0，结果是多少？")


if __name__ == "__main__":
    demo_schema()
    demo_basic()
    demo_error()
