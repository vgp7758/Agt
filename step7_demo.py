"""Step 7 演示 —— 结构化输出（Pydantic）。

两个演示：
  demo_extract() : 从一段会议纪要里提取结构化待办清单。
  demo_plan()    : 把一个目标拆解成结构化的执行计划（顺带覆盖"任务分解"）。

跑法：python step7_demo.py
"""
from typing import List

from pydantic import BaseModel, Field

from structured import complete_structured


# === Demo 1 用的模型：待办清单 ===
class Todo(BaseModel):
    task: str = Field(description="待办事项内容")
    owner: str = Field(description="负责人姓名")
    deadline: str = Field(description="截止时间，按原文表述")


class TodoList(BaseModel):
    todos: List[Todo] = Field(description="提取出的所有待办事项")


# === Demo 2 用的模型：执行计划 ===
class PlanStep(BaseModel):
    step: int = Field(description="步骤序号，从 1 开始")
    action: str = Field(description="这一步具体做什么")
    tool_hint: str = Field(description="建议使用的工具，如 run_python / web_search / write_file / run_shell")


class Plan(BaseModel):
    goal: str = Field(description="总体目标")
    steps: List[PlanStep] = Field(description="拆解出的执行步骤")


MEETING_MINUTES = """
周三产品周会纪要：
1. 张伟负责在本周五前完成登录模块的回归测试。
2. 下周一之前，李娜要把新版的 API 文档更新到 confluence。
3. 运维组（王磊）需要在 8 月 1 号前把预发环境扩容做完。
4. 设计稿评审推迟，由陈晨在下周三组织重新评审。
"""


def demo_extract():
    print("=" * 60)
    print("Demo 1: 结构化信息提取 —— 会议纪要 → 待办清单")
    print("=" * 60)
    print("\n[原文]")
    print(MEETING_MINUTES.strip())

    result = complete_structured(
        TodoList,
        "从下面的会议纪要里提取所有待办事项。" + MEETING_MINUTES,
    )
    print("\n[提取出的结构化结果]")
    print(result.model_dump_json(indent=2, ensure_ascii=False))
    print(f"\n✅ 共提取 {len(result.todos)} 条待办，类型校验通过。")


def demo_plan():
    print("\n" + "=" * 60)
    print("Demo 2: 任务分解 —— 为一个目标制定结构化计划")
    print("=" * 60)
    result = complete_structured(
        Plan,
        "为下面这个目标制定一个 4~6 步的执行计划：组织一场公司内部的技术分享会。",
    )
    print("\n[结构化计划]")
    print(result.model_dump_json(indent=2, ensure_ascii=False))
    print("\n✅ 计划已结构化，可直接交给 Agent 循环去逐步执行。")


if __name__ == "__main__":
    demo_extract()
    demo_plan()
