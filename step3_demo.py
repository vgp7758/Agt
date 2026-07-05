"""Step 3 演示 —— System Prompt / 角色人格的威力。

三个演示：
  demo_personalities() : 同一问题、不同人设，对比风格差异。
  demo_date_injection() : 注入"今天日期"前后，模型能否回答"今天几号"。
  demo_persona_chat()  : 人设 + 记忆组合，苏格拉底老师连续引导。

跑法：python step3_demo.py
（交互式 REPL 没单独再做，套路和 step2_demo 的 --chat 完全一样，只是把
 system 换成 build_system(persona) 即可；需要可随时加。）
"""
from conversation import Conversation
from llm_client import LLMClient
from prompts import PERSONAS, build_system


def demo_personalities():
    print("=" * 60)
    print("Demo 1: 同一问题、不同人设 —— “用两三句话解释黑洞”")
    print("=" * 60)
    client = LLMClient(enable_thinking=False)  # 风格演示，关思考省 token
    question = "用两三句话解释什么是黑洞。"
    for name in ["严谨科学家", "苏格拉底式老师", "毒舌评论员"]:
        print(f"\n{'—' * 18} 人设：{name} {'—' * 18}")
        conv = Conversation(system=build_system(name))
        conv.add_user(question)
        resp = client.chat(conv.messages)
        print(resp.content.strip())


def demo_date_injection():
    print("\n" + "=" * 60)
    print('Demo 2: 动态上下文注入 —— “今天是几月几号？星期几？”')
    print("=" * 60)
    client = LLMClient(enable_thinking=False)
    question = "今天是几月几号？星期几？"

    # —— 对照组：原始人设，不含日期 ——
    print("\n[不注入日期] system = 原始人设：")
    conv = Conversation(system=PERSONAS["默认助手"])
    conv.add_user(question)
    resp = client.chat(conv.messages)
    print("模型回答:", resp.content.strip())

    # —— 实验组：build_system 注入运行时日期 ——
    sys_with_date = build_system("默认助手")
    print("\n[注入日期] system 内容如下：")
    for line in sys_with_date.splitlines():
        print("    " + line)
    conv2 = Conversation(system=sys_with_date)
    conv2.add_user(question)
    resp2 = client.chat(conv2.messages)
    print("模型回答:", resp2.content.strip())


def demo_persona_chat():
    print("\n" + "=" * 60)
    print("Demo 3: 人设 + 记忆 —— 苏格拉底老师连续引导“小天”")
    print("=" * 60)
    client = LLMClient(enable_thinking=False)
    conv = Conversation(system=build_system("苏格拉底式老师", user_name="小天"))
    turns = [
        "我不太懂编程里的“变量”是什么，能直接告诉我吗？",
        "嗯……是不是像一个装东西的盒子？",
    ]
    for t in turns:
        print(f"\n🧑 小天: {t}")
        conv.add_user(t)
        resp = client.chat(conv.messages)
        conv.add_assistant(resp.content)
        print(f"🤖 老师: {resp.content.strip()}")


if __name__ == "__main__":
    demo_personalities()
    demo_date_injection()
    demo_persona_chat()
