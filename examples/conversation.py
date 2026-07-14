"""Conversation —— 会话记忆管理。

让模型"记住"上下文：内部维护一个 messages 列表，每次调用把完整历史喂回模型。
要点：
  1. 只存 content，不存 reasoning_content（思考过程一次性，回喂会让上下文爆炸）。
  2. 历史变长时滑动窗口截断，保留最近若干轮。
  3. 支持工具调用往返：能存"带 tool_calls 的助手消息"和"工具结果消息(role=tool)"。
     （Step 4 引入。注意：当前截断不保证 tool_call/result 成对保留，长历史场景后续再优化。）
"""
from __future__ import annotations

import json
from typing import Optional


class Conversation:
    def __init__(self, system: Optional[str] = None, max_messages: int = 20):
        """
        :param system:       系统提示词，始终置顶、永不被截断。
        :param max_messages: 滑动窗口大小——保留的"非系统"消息条数上限。
        """
        self._system = system
        self.max_messages = max_messages
        self._history: list[dict] = []

    def add_user(self, text: str) -> None:
        self._history.append({"role": "user", "content": text})
        self._trim()

    def add_assistant(self, text: str = "", tool_calls: Optional[list[dict]] = None) -> None:
        """追加助手消息。

        :param text: 助手正文（纯工具调用时可为空字符串）。
        :param tool_calls: 干净形式 [{id, name, arguments(dict)}, ...]，
                           会自动转成 OpenAI 线上格式存入历史。
        """
        msg: dict = {"role": "assistant", "content": text or None}
        if tool_calls:
            msg["tool_calls"] = [
                {
                    "id": tc["id"],
                    "type": "function",
                    "function": {
                        "name": tc["name"],
                        "arguments": json.dumps(tc["arguments"], ensure_ascii=False),
                    },
                }
                for tc in tool_calls
            ]
        self._history.append(msg)
        self._trim()

    def add_tool_result(self, tool_call_id: str, result: str) -> None:
        """追加工具结果消息。每个 tool_call 都必须对应一条结果，模型才能接上。"""
        self._history.append({
            "role": "tool",
            "tool_call_id": tool_call_id,
            "content": result,
        })
        self._trim()

    def _trim(self) -> None:
        if len(self._history) > self.max_messages:
            drop = len(self._history) - self.max_messages
            self._history = self._history[drop:]

    @property
    def messages(self) -> list[dict]:
        """喂给模型的完整消息列表：system 在前 + 历史。"""
        msgs = []
        if self._system:
            msgs.append({"role": "system", "content": self._system})
        msgs.extend(self._history)
        return msgs

    def clear(self) -> None:
        """清空对话历史（保留 system）。"""
        self._history = []

    def __len__(self) -> int:  # 历史消息条数（不含 system）
        return len(self._history)

    def __repr__(self) -> str:
        return f"Conversation(turns={len(self._history)}, system={'yes' if self._system else 'no'})"
