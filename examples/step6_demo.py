"""Step 6 演示 —— 真实工具集。

四个演示，覆盖四类工具：
  demo_code()     : run_python —— 算 2^100 及其位数。
  demo_combined() : run_python + 文件读写 —— 生成平方表并存盘、再读回。
  demo_search()   : web_search —— 搜最新信息（可能因网络失败，属正常降级）。
  demo_shell()    : run_shell —— 安全的 echo 演示。

跑法：python step6_demo.py
"""
from agent import Agent
from real_tools import REAL_TOOLS

SYSTEM = (
    "你是一个强大的自主 Agent，拥有这些工具：\n"
    "- run_python(code)：写并运行 Python 代码，处理任意计算/数据处理；\n"
    "- read_file / write_file / list_dir：在 workspace 目录读写文件；\n"
    "- web_search(query)：联网搜索实时信息（国内可能需代理）；\n"
    "- run_shell(command)：执行系统命令（最强大也最危险，能用别的就别用它）。\n"
    "原则：计算优先 run_python；要持久化就 write_file；不确定的事实用 web_search。"
    "一步一步来，每步只用必要的工具。"
)


def demo_code():
    print("=" * 60)
    print("Demo 1: 代码执行 —— 2^100 是多少？有几位？")
    print("=" * 60)
    Agent(system=SYSTEM, tools=REAL_TOOLS).run("2 的 100 次方等于多少？它是一个几位数？")


def demo_combined():
    print("\n" + "=" * 60)
    print("Demo 2: 组合技 —— 生成平方表存盘再读回")
    print("=" * 60)
    Agent(system=SYSTEM, tools=REAL_TOOLS).run(
        "用 Python 生成 1 到 10 的平方数表（每行 'n -> n*n'），"
        "写到 squares.txt，然后再读出来展示给我看。"
    )


def demo_search():
    print("\n" + "=" * 60)
    print("Demo 3: 联网搜索（国内网络可能失败，属正常）")
    print("=" * 60)
    Agent(system=SYSTEM, tools=REAL_TOOLS).run("搜一下当前 Python 的最新稳定版本是多少。")


def demo_shell():
    print("\n" + "=" * 60)
    print("Demo 4: Shell 命令（安全 echo 演示）")
    print("=" * 60)
    Agent(system=SYSTEM, tools=REAL_TOOLS).run("请用 run_shell 执行 echo，打印一句你想对我说的座右铭。")


if __name__ == "__main__":
    demo_code()
    demo_combined()
    demo_search()
    demo_shell()
