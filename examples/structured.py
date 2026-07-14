"""structured.py —— 让模型输出可靠的结构化数据（Pydantic）。

核心是 complete_structured(model_cls, prompt)：
  传入一个 Pydantic 模型类和一个任务描述，返回一个【校验通过的对象】。

为什么需要它：到目前为止 Agent 输出都是自由文本。但真实应用常需要结构化数据
（提取字段、生成配置、返回计划……）。难点是模型偶尔会输出格式不对的 JSON，
所以这里用两个手段保证可靠：
  1. 强指令 + 把 JSON Schema 明确喂给模型；
  2. Pydantic 校验，失败就把错误【反馈给模型让它改】并重试（又一个容错模式）。

不依赖服务端的 response_format（兼容性最好），纯靠"指令 + 校验 + 重试"。
"""
from __future__ import annotations

import json
from typing import Optional, Type, TypeVar

from pydantic import BaseModel, ValidationError

from llm_client import LLMClient

T = TypeVar("T", bound=BaseModel)


def _extract_json(text: str) -> str:
    """从模型输出里抠出 JSON 字符串：去 markdown 代码块，再截取首个 { 到末个 }。"""
    text = text.strip()
    if text.startswith("```"):                       # 去掉 ```json / ``` 围栏
        lines = text.split("\n")
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        text = text[start:end + 1]
    return text


def complete_structured(
    model_cls: Type[T],
    prompt: str,
    *,
    max_retries: int = 3,
    enable_thinking: bool = False,
    system: Optional[str] = None,
) -> T:
    """让模型按 model_cls 的 schema 输出，返回校验通过的对象。失败自动重试。"""
    client = LLMClient(enable_thinking=enable_thinking, temperature=0.3)
    schema = model_cls.model_json_schema()

    sys_msg = system or (
        "你是一个严格的数据结构化助手。只输出符合要求的 JSON，"
        "不要任何解释、前后缀文字或 markdown 代码块。"
    )
    instruction = (
        f"{prompt}\n\n"
        f"请严格按照下面的 JSON Schema 输出（只输出 JSON 本身）：\n"
        f"{json.dumps(schema, ensure_ascii=False, indent=2)}"
    )
    messages = [
        {"role": "system", "content": sys_msg},
        {"role": "user", "content": instruction},
    ]

    last_error, last_raw = None, ""
    for attempt in range(max_retries):
        resp = client.chat(messages)
        last_raw = resp.content
        try:
            data = json.loads(_extract_json(resp.content))
            return model_cls.model_validate(data)
        except (json.JSONDecodeError, ValidationError, ValueError) as e:
            last_error = e
            # 把错误反馈给模型，让它修正后重试
            messages.append({"role": "assistant", "content": resp.content})
            messages.append({
                "role": "user",
                "content": f"上一次输出无法解析或校验失败：{e}\n请重新只输出符合 schema 的合法 JSON。",
            })

    raise RuntimeError(
        f"结构化输出连续 {max_retries} 次失败。最后错误：{last_error}\n最后一次输出：{last_raw}"
    )
