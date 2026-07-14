"""LLMClient —— 对大模型调用的统一封装。

之后所有模块（会话记忆、工具调用、Agent 主循环）都通过这个类跟模型打交道。
职责：
  - 集中管理模型名 / base_url / 默认参数；
  - 自动拆出推理模型的 reasoning_content；
  - 解析 tool_calls 成干净结构；
  - 内置"空响应 → 指数退避重试"，应对限流/服务波动。
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from openai import OpenAI, RateLimitError, APITimeoutError, APIConnectionError

import config


@dataclass
class LLMResponse:
    """一次模型调用的干净结果。"""

    content: str                          # 最终答案正文
    reasoning: str = ""                   # 推理模型的思考过程（非推理模型为空）
    tool_calls: list = field(default_factory=list)  # 干净形式 [{id, name, arguments(dict)}]
    usage: Optional[dict] = None          # token 用量
    raw_message: Optional[dict] = field(default=None, repr=False)  # 原始 message，调试用

    @property
    def has_tool_calls(self) -> bool:
        return bool(self.tool_calls)


def _parse_tool_calls(msg: dict) -> list[dict]:
    """把原始 tool_calls 解析成干净形式 [{id, name, arguments(dict)}]。"""
    out = []
    for tc in (msg.get("tool_calls") or []):
        fn = tc.get("function", {})
        args_raw = fn.get("arguments", "{}")
        try:
            args = json.loads(args_raw) if isinstance(args_raw, str) else args_raw
        except json.JSONDecodeError:
            args = {"_raw_arguments": args_raw}  # 模型偶尔输出非法 JSON，兜底
        out.append({"id": tc.get("id"), "name": fn.get("name"), "arguments": args})
    return out


class LLMClient:
    def __init__(
        self,
        profile: Optional[dict] = None,
        *,
        model_name: Optional[str] = None,
        temperature: float = 0.7,
        enable_thinking: bool = True,
        max_tokens: Optional[int] = None,
        max_retries: int = 3,
    ):
        if profile is None:
            model_name = model_name or config.DEFAULT_MODEL
            profile = config.get_profile(model_name)
        self.model_name = model_name or config.DEFAULT_MODEL
        self.enable_thinking = enable_thinking
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.max_retries = max_retries
        self.fallback_chain: list[str] = []   # 回退优先级链(如 glm,deepseek,qwen)
        self._apply_profile(profile)

    def _apply_profile(self, profile: dict):
        """按 profile 配置 base_url/api_key/model/thinking，并重建底层 client。"""
        self.base_url = profile["base_url"]
        self.api_key = profile["api_token"]
        self.model = profile["model"]
        self.thinking_supported = profile.get("thinking", False)
        self._client = OpenAI(base_url=self.base_url, api_key=self.api_key)

    def switch_model(self, name: str) -> "LLMClient":
        """运行时热切换模型。Agent 与 Session 共用同一个 LLMClient 对象，切换即全生效。"""
        self._apply_profile(config.get_profile(name))
        self.model_name = name
        return self

    def _build_kwargs(self, messages, stream: bool, **overrides) -> dict:
        """组装请求参数。tools / tool_choice 等可通过 overrides 透传。"""
        kwargs = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
            "stream": stream,
        }
        if self.max_tokens is not None:
            kwargs["max_tokens"] = self.max_tokens
        # enable_thinking 是 Qwen/ModelScope 专有透传参数；对不支持的 provider 不发，避免 400。
        if self.thinking_supported:
            kwargs["extra_body"] = {"enable_thinking": self.enable_thinking}
        kwargs.update(overrides)
        return kwargs

    def _backoff(self, attempt: int) -> float:
        """斐波那契退避（从 1 开始）：1, 1, 2, 3, 5, 8……比指数更平缓。"""
        a, b = 1, 1
        for _ in range(attempt):
            a, b = b, a + b
        return float(a)

    def chat(self, messages, **overrides) -> LLMResponse:
        """普通（非流式）调用。空响应退避重试；耗完后若设了回退链则沿链切换模型再试。"""
        tried = [self.model_name]
        while True:
            try:
                return self._chat_inner(messages, **overrides)
            except (RuntimeError, RateLimitError, APITimeoutError, APIConnectionError) as e:
                if not self.fallback_chain:
                    raise
                try:
                    idx = self.fallback_chain.index(self.model_name)
                except ValueError:
                    raise  # 当前模型不在回退链，不处理
                next_m = self.fallback_chain[idx + 1] if idx + 1 < len(self.fallback_chain) else None
                if not next_m or next_m in tried:
                    raise RuntimeError(
                        f"回退链中所有模型均已尝试({', '.join(tried)})：{e}") from e
                tried.append(next_m)
                self.switch_model(next_m)
                time.sleep(self._backoff(len(tried) - 1))  # 切换前小退避

    def _chat_inner(self, messages, **overrides) -> LLMResponse:
        """单模型调用 + 空响应重试（被 chat() 的回退循环包裹）。"""
        last_info = None
        for attempt in range(self.max_retries):
            resp = self._client.chat.completions.create(
                **self._build_kwargs(messages, stream=False, **overrides)
            )
            choices = resp.choices or []
            usage = resp.usage.model_dump() if resp.usage else None
            if choices:
                msg = choices[0].message.model_dump()
                return LLMResponse(
                    content=msg.get("content") or "",
                    reasoning=msg.get("reasoning_content") or "",
                    tool_calls=_parse_tool_calls(msg),
                    usage=usage,
                    raw_message=msg,
                )
            last_info = (usage, str(resp.model_dump())[:300])
            time.sleep(self._backoff(attempt))

        raise RuntimeError(
            f"连续 {self.max_retries} 次得到空 choices（疑似限流/服务波动）。"
            f" 最后 usage={last_info[0]} raw={last_info[1]}"
        )

    def chat_stream(
        self,
        messages,
        on_reasoning: Optional[Callable[[str], None]] = None,
        on_content: Optional[Callable[[str], None]] = None,
        **overrides,
    ) -> LLMResponse:
        """流式调用。逐块回调思考/正文，最后返回完整 LLMResponse。

        整条流一个 token 都没产出时退避重试。
        """
        last_usage = None
        for attempt in range(self.max_retries):
            stream = self._client.chat.completions.create(
                **self._build_kwargs(messages, stream=True, **overrides)
            )
            content_parts, reasoning_parts, tool_call_fragments = [], [], {}
            usage = None
            for chunk in stream:
                if chunk.usage:
                    usage = chunk.usage.model_dump()
                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta.model_dump()
                if delta.get("reasoning_content"):
                    reasoning_parts.append(delta["reasoning_content"])
                    if on_reasoning:
                        on_reasoning(delta["reasoning_content"])
                if delta.get("content"):
                    content_parts.append(delta["content"])
                    if on_content:
                        on_content(delta["content"])
                # 工具调用的 arguments 是分块到达的，按 index 累积
                for tc in (delta.get("tool_calls") or []):
                    idx = tc.get("index", 0)
                    frag = tool_call_fragments.setdefault(idx, {"id": None, "name": "", "arguments": ""})
                    if tc.get("id"):
                        frag["id"] = tc["id"]
                    fn = tc.get("function") or {}
                    if fn.get("name"):
                        frag["name"] += fn["name"]
                    if fn.get("arguments"):
                        frag["arguments"] += fn["arguments"]
            if content_parts or reasoning_parts or tool_call_fragments:
                tool_calls = []
                for idx in sorted(tool_call_fragments):
                    f = tool_call_fragments[idx]
                    try:
                        args = json.loads(f["arguments"]) if f["arguments"] else {}
                    except json.JSONDecodeError:
                        args = {"_raw_arguments": f["arguments"]}
                    tool_calls.append({"id": f["id"], "name": f["name"], "arguments": args})
                return LLMResponse(
                    content="".join(content_parts),
                    reasoning="".join(reasoning_parts),
                    tool_calls=tool_calls,
                    usage=usage,
                )
            last_usage = usage
            time.sleep(self._backoff(attempt))

        raise RuntimeError(
            f"连续 {self.max_retries} 次流式调用都返回空（疑似限流/服务波动）。"
            f" 最后 usage={last_usage}"
        )
