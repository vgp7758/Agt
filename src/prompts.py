"""prompts.py —— System Prompt / 人设管理。

人设（persona）定义了 Agent 的角色、语气、能力边界与行为规则。
两个核心概念：
  1. 人设模板 PERSONAS：一组命名好的、可复用的系统提示词。
  2. 动态注入 build_system()：把"运行时上下文"（今天日期、用户名……）拼进系统提示词。
     ——模型天生不知道"今天是几号"、不认识用户，这些信息只能靠 system prompt 喂给它。
"""
from __future__ import annotations

import datetime
from typing import Optional

# === 人设模板 ===
# 用字典存放，key 是人设名，value 是系统提示词文本。
PERSONAS: dict[str, str] = {
    "默认助手": "你是一个友好、简洁的中文助手。",

    "严谨科学家": (
        "你是一位严谨的科学家。回答时：\n"
        "- 用语精确、逻辑清晰；\n"
        "- 区分事实与推测，不确定时明确说明；\n"
        "- 适当使用专业术语，但会解释关键概念；\n"
        "- 不夸大、不编造数据。"
    ),

    "苏格拉底式老师": (
        "你是一位苏格拉底式的老师。你不直接给答案，而是：\n"
        "- 用一连串启发式的问题引导学生自己思考；\n"
        "- 每次只问一个最关键的问题；\n"
        "- 鼓励、有耐心，肯定学生的每一步进展。"
    ),

    "毒舌评论员": (
        "你是一位嘴毒但心善的评论员。回答时：\n"
        "- 语言犀利、金句频出、带点冷幽默；\n"
        "- 但批评有理有据，最终还是会给出有用的建议。"
    ),
}


def list_personas() -> list[str]:
    return list(PERSONAS.keys())


def build_system(
    persona: str = "默认助手",
    *,
    user_name: Optional[str] = None,
    today: Optional[datetime.date] = None,
    extra: Optional[str] = None,
) -> str:
    """构造系统提示词 = 人设 + 动态环境上下文。

    :param persona:   PERSONAS 的键名。
    :param user_name: 用户名（注入后模型能"认识"对方）。
    :param today:     今天日期（注入后模型能回答"今天几号"）。None 时运行时自动获取。
    :param extra:     额外的任意上下文文本。
    """
    if persona not in PERSONAS:
        raise KeyError(f"未知人设 '{persona}'，可选：{list_personas()}")

    parts = [PERSONAS[persona]]

    # —— 动态环境上下文段 ——
    date = today if today is not None else datetime.date.today()
    ctx = [f"当前日期：{date.isoformat()}（{date.strftime('%Y年%m月%d日 %A')}）"]
    if user_name:
        ctx.append(f"你正在对话的用户叫：{user_name}")
    if extra:
        ctx.append(extra)
    parts.append("\n【环境上下文】\n" + "\n".join(ctx))

    return "\n".join(parts)
