"""Step 2 演示 —— 带记忆的多轮对话。

两部分：
  demo_memory() : 脚本化的多轮对话，证明模型记住了上文（可自动验证，无需人工输入）。
  repl()        : 交互式聊天，你打一句、它流式回一句、全程记住上下文。

跑法：
  python step2_demo.py            # 跑 demo_memory
  python step2_demo.py --chat     # 进入交互 REPL
"""
import sys

from conversation import Conversation
from llm_client import LLMClient


def demo_memory():
    print("=" * 60)
    print("Demo: 多轮对话 —— 模型能记住上文吗？")
    print("=" * 60)

    # 简单聊天关掉思考，省 token；记忆能力跟思考无关。
    client = LLMClient(enable_thinking=False)
    conv = Conversation(system="你是一个友好的中文助手，回答简洁。")

    turns = [
        "我叫小明，我最喜欢吃火锅。",
        "我刚才说我叫什么名字？喜欢吃什么？",  # 杀手锏：考验记忆
        "那结合我刚才说的，推荐一个适合我的晚餐？",  # 考验结合上文推理
    ]

    for user_text in turns:
        print(f"\n🧑 你: {user_text}")
        conv.add_user(user_text)

        resp = client.chat(conv.messages)   # 把完整历史喂回去
        conv.add_assistant(resp.content)    # 只存 content，不存 reasoning

        print(f"🤖 助手: {resp.content}")
        print(f"   (历史消息数={len(conv)}, 本次 prompt_tokens={resp.usage['prompt_tokens']})")


def repl():
    print("=" * 60)
    print("交互式聊天  （输入 quit / Ctrl+C / Ctrl+D 退出）")
    print("=" * 60)

    client = LLMClient(enable_thinking=False)
    conv = Conversation(system="你是一个友好的中文助手。")

    while True:
        try:
            user_text = input("\n🧑 你: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n再见！")
            break
        if not user_text:
            continue
        if user_text.lower() in {"quit", "exit", "q"}:
            print("再见！")
            break

        conv.add_user(user_text)
        print("🤖 助手: ", end="", flush=True)
        resp = client.chat_stream(
            conv.messages,
            on_content=lambda t: print(t, end="", flush=True),
        )
        print()
        conv.add_assistant(resp.content)


if __name__ == "__main__":
    if "--chat" in sys.argv:
        repl()
    else:
        demo_memory()
