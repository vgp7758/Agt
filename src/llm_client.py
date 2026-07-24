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
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from openai import OpenAI, RateLimitError, APITimeoutError, APIConnectionError, BadRequestError

import config

_LOG = logging.getLogger("agt.llm")  # 直接用标准 logging（不 import log.py）；handler 由 agent 配置时挂到 agt root

# —— max_tokens 默认值与截断重试上限 ——
# 推理模型 reasoning 可能很长；若不设 max_tokens 会用 provider 默认值（有的很小），导致 reasoning
# 吃光预算、content 空/半截（finish_reason=length）。这里给推理模型一个宽裕默认，并在检测到
# finish_reason=length 时加倍重试（封顶 _MAX_TOKENS_CAP）。
_REASONING_DEFAULT_MAX_TOKENS = 8192
_MAX_TOKENS_CAP = 16384


def _bump_max_tokens(cur):
    """截断重试时上调 max_tokens：翻倍，封顶 _MAX_TOKENS_CAP（防超出模型能力）。"""
    base = cur or _REASONING_DEFAULT_MAX_TOKENS
    return min(base * 2, _MAX_TOKENS_CAP)

# DeepSeek/ModelScope 等在并行多工具调用时，偶尔不通过标准 tool_calls 字段返回，
# 而是把工具调用以内部 DSML 文本塞进 content：
#   <｜｜DSML｜｜tool_calls><｜｜DSML｜｜invoke name="工具名">..参数..<／｜｜DSML｜｜invoke>
# （｜是全角竖线 U+FF5C）。此时标准解析会误判"无工具调用"→把整段 DSML 当最终答案。
# 下面的兜底解析把这种文本还原成标准 tool_calls，并从 content 剥除 DSML 文本。
_DSML = "｜｜DSML｜｜"   # ｜｜DSML｜｜
_INVOKE_RE = re.compile(
    re.escape(_DSML) + r'invoke name="([^"]+)">(.*?)</' + re.escape(_DSML) + r'invoke>',
    re.DOTALL,
)
_PARAM_RE = re.compile(
    re.escape(_DSML) + r'parameter name="([^"]+)"\s+string="(true|false)">(.*?)</'
    + re.escape(_DSML) + r'parameter>',
    re.DOTALL,
)
_BARE_TAG_RE = re.compile(r'</?' + re.escape(_DSML) + r'[a-z_]*>')


def _parse_dsml_calls(content: str) -> Optional[tuple[str, list[dict]]]:
    """若 content 含 DSML 工具调用文本，解析成 (剥除 DSML 后的 content, [tool_calls])。
    无 DSML 标记返回 None（调用方据此判断是否兜底）。"""
    if not content or _DSML not in content:
        return None
    calls = []
    for m in _INVOKE_RE.finditer(content):
        name, body = m.group(1), m.group(2)
        args = {}
        for pm in _PARAM_RE.finditer(body):
            pname, is_str, pval = pm.group(1), pm.group(2), pm.group(3).strip()
            if is_str == "false":
                # 非 string：尝试 JSON，再退到数字，最后原样
                try:
                    args[pname] = json.loads(pval)
                except Exception:
                    try:
                        args[pname] = int(pval)
                    except Exception:
                        try:
                            args[pname] = float(pval)
                        except Exception:
                            args[pname] = pval
            else:
                args[pname] = pval
        calls.append({"id": f"dsml_{len(calls)}", "name": name, "arguments": args})

    # 剥除 DSML 文本：有 <｜｜DSML｜｜tool_calls> 头则从头截断(保留前面真实思考)，
    # 否则逐块移除 invoke 块，再清残留裸标签。
    head = "<" + _DSML + "tool_calls>"
    if head in content:
        cleaned = content.split(head, 1)[0]
    else:
        cleaned = _INVOKE_RE.sub("", content)
    cleaned = _BARE_TAG_RE.sub("", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return cleaned, calls


def _postprocess_response(resp: "LLMResponse") -> "LLMResponse":
    """兜底：标准 tool_calls 为空时，尝试从 content 里解析 DSML 工具调用。
    解析出调用 → 覆盖 tool_calls 并用剥除后的 content；解析不到则原样返回。"""
    if resp.tool_calls:
        return resp  # API 已给标准 tool_calls，优先信它
    parsed = _parse_dsml_calls(resp.content or "")
    if parsed is None:
        return resp
    cleaned, calls = parsed
    resp.content = cleaned
    if calls:
        resp.tool_calls = calls
    return resp


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
        self.fallback_policy: str = "sticky"  # 回退后下一轮：sticky=永久降级 / reset=每轮回退链首
        self.reasoning_completer: Optional[str] = None  # 思考模型只回 reasoning 无 content 时，用此非思考模型据 reasoning 补正文（None=关）
        self._apply_profile(profile)

    def _apply_profile(self, profile: dict):
        """按 profile 配置 base_url/api_key/model/thinking，并重建底层 client。
        支持多 api_token 轮流使用。"""
        self.base_url = profile["base_url"]
        self.api_tokens = profile.get("api_tokens") or [profile.get("api_token", "")]
        self._token_idx = 0
        self.api_key = self.api_tokens[0]
        self.model = profile["model"]
        self.thinking_supported = profile.get("thinking", False)
        # max_tokens：profile 可显式指定；否则推理模型(thinking)用宽裕默认（防长 reasoning 被 provider
        # 默认小值截断 → content 空/半截），非推理模型用 None（交 provider 默认）。
        self.max_tokens = profile.get("max_tokens") or (_REASONING_DEFAULT_MAX_TOKENS if self.thinking_supported else None)
        self._client = OpenAI(base_url=self.base_url, api_key=self.api_key)

    def _rotate_token(self):
        """轮流切换到下一个 api_token，返回是否成功切换。"""
        if len(self.api_tokens) <= 1:
            return False
        self._token_idx = (self._token_idx + 1) % len(self.api_tokens)
        self.api_key = self.api_tokens[self._token_idx]
        self._client = OpenAI(base_url=self.base_url, api_key=self.api_key)
        return True

    def switch_model(self, name: str) -> "LLMClient":
        """运行时热切换模型。Agent 与 Session 共用同一个 LLMClient 对象，切换即全生效。"""
        self._apply_profile(config.get_profile(name))
        self.model_name = name
        return self

    def _maybe_reset_to_head(self):
        """reset 策略：每次调用前若已偏离回退链首模型，先切回去。
        限流常是临时波动，首选模型可能已恢复，故下一轮重新从链首尝试。
        sticky 策略或空回退链时不动作（不干扰手动 /model 选模型）。"""
        if (self.fallback_policy == "reset"
                and self.fallback_chain
                and self.model_name != self.fallback_chain[0]):
            self.switch_model(self.fallback_chain[0])

    def _build_kwargs(self, messages, stream: bool, **overrides) -> dict:
        """组装请求参数。tools / tool_choice 等可通过 overrides 透传。
        enable_thinking / timeout 可经 overrides 按 call 覆盖实例默认值（工作流 LLM 节点 per-node 设置）。"""
        enable_thinking = overrides.pop("enable_thinking", self.enable_thinking)
        timeout = overrides.pop("timeout", None)
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
            kwargs["extra_body"] = {"enable_thinking": enable_thinking}
        if timeout is not None:
            kwargs["timeout"] = timeout
        kwargs.update(overrides)
        return kwargs

    def _backoff(self, attempt: int) -> float:
        """退避序列：5, 6, 11, 17, 28……（5/6 起的斐波那契式增长）。
        初始间隔稍长，避免短间隔连撞限流被反复拒绝。"""
        a, b = 5, 6
        for _ in range(attempt):
            a, b = b, a + b
        return float(a)

    def _complete_via_completer(self, messages, reasoning: str) -> str:
        """用 reasoning_completer 指定的非思考模型，据 reasoning 补出 content。
        用临时 client，不扰动主 client 的模型/token 状态；失败返回 ''（回退原处理，不引入新故障）。"""
        try:
            p = config.get_profile(self.reasoning_completer)
            tok = (p.get("api_tokens") or [p.get("api_token", "")])[0]
            tmp = OpenAI(base_url=p["base_url"], api_key=tok)
            primed = list(messages) + [{"role": "system", "content":
                "下面是推理模型刚才的完整思考过程。请直接据此输出最终回答正文（结论/答案），"
                "不要复述思考过程：\n\n" + reasoning}]
            r = tmp.chat.completions.create(model=p["model"], messages=primed, temperature=self.temperature)
            ch = r.choices or []
            return ((ch[0].message.model_dump().get("content") if ch else "") or "").strip()
        except Exception as e:
            _LOG.warning("reasoning_completer(%s) 补全失败：%s，回退原处理", self.reasoning_completer, e)
            return ""

    def chat(self, messages, **overrides) -> LLMResponse:
        """普通（非流式）调用。空响应退避重试；多 token 轮流；耗完后沿回退链切换模型。"""
        self._maybe_reset_to_head()
        tried_tokens = 0
        tried = [self.model_name]
        while True:
            try:
                resp = self._chat_inner(messages, **overrides)
                # 成功后预旋转到下一个 token（下次调用自动用不同账号）
                if len(self.api_tokens) > 1:
                    self._rotate_token()
                # reasoning 补全：思考模型只回 reasoning 无 content → 交非思考模型据 reasoning 补正文
                if (self.reasoning_completer and (resp.reasoning or "").strip()
                        and not (resp.content or "").strip() and not resp.tool_calls):
                    _LOG.info("思考模型只回 reasoning 无 content，交 %s 据 reasoning 补正文", self.reasoning_completer)
                    content = self._complete_via_completer(messages, resp.reasoning)
                    if content:
                        resp.content = content   # 保留原 reasoning
                return resp
            except (RuntimeError, RateLimitError, APITimeoutError, APIConnectionError, BadRequestError) as e:
                # BadRequestError 也触发回退：某些模型会因请求格式拒绝整个请求（典型如 DeepSeek 思考模式
                # 要求工具调用轮回传 reasoning_content；跨模型混用、历史缺 reasoning 时会 400）。
                # 换下一个模型即可，不该让单模型的可恢复拒绝把整轮/整条链崩掉。
                # 先试下一个 api_token（同模型多账号）
                if isinstance(e, RateLimitError) and tried_tokens < len(self.api_tokens) - 1:
                    tried_tokens += 1
                    self._rotate_token()
                    _LOG.warning("限流，换 token 重试 (%d/%d) 原因=%s",
                                 tried_tokens + 1, len(self.api_tokens), type(e).__name__)
                    continue
                # 再走模型回退链
                if not self.fallback_chain:
                    _LOG.error("调用失败且无回退链: %s: %s", type(e).__name__, e)
                    raise
                try:
                    idx = self.fallback_chain.index(self.model_name)
                except ValueError:
                    raise
                next_m = self.fallback_chain[idx + 1] if idx + 1 < len(self.fallback_chain) else None
                if not next_m or next_m in tried:
                    _LOG.error("回退链耗尽 tried=%s 原因=%s", tried, type(e).__name__)
                    raise RuntimeError(
                        f"回退链中所有模型均已尝试({', '.join(tried)})：{e}") from e
                tried.append(next_m)
                tried_tokens = 0
                _LOG.warning("回退 %s→%s 原因=%s 退避%.0fs",
                             self.model_name, next_m, type(e).__name__, self._backoff(len(tried) - 1))
                self.switch_model(next_m)
                time.sleep(self._backoff(len(tried) - 1))

    def _chat_inner(self, messages, **overrides) -> LLMResponse:
        """单模型调用 + 重试（空响应 / max_tokens 截断）。被 chat() 的回退循环包裹。"""
        last_info = None
        # 本轮可动态上调的 max_tokens（截断时加倍重试）；overrides 显式传的优先于 profile 默认
        cur_max_tokens = overrides.get("max_tokens", self.max_tokens)
        choices = []
        usage = None
        for attempt in range(self.max_retries):
            _t0 = time.time()
            _LOG.debug("尝试 %d/%d model=%s", attempt + 1, self.max_retries, self.model_name)
            kw = dict(overrides)
            if cur_max_tokens is not None:
                kw["max_tokens"] = cur_max_tokens
            resp = self._client.chat.completions.create(
                **self._build_kwargs(messages, stream=False, **kw)
            )
            _elapsed = time.time() - _t0
            choices = resp.choices or []
            usage = resp.usage.model_dump() if resp.usage else None
            if choices:
                fr = choices[0].finish_reason
                if fr == "length":
                    # 被 max_tokens 截断（典型：长 reasoning 吃光预算 → content 空/半截）→ 加大上限重试
                    bumped = _bump_max_tokens(cur_max_tokens)
                    _LOG.warning("⚠️ 响应被 max_tokens 截断(finish_reason=length, %s→%s) 重试 %d/%d model=%s",
                                 cur_max_tokens, bumped, attempt + 1, self.max_retries, self.model_name)
                    cur_max_tokens = bumped
                    last_info = (usage, "truncated finish_reason=length")
                    time.sleep(self._backoff(attempt))
                    continue
                _toks = usage.get("total_tokens") if usage else None
                _LOG.info("成功 model=%s tokens=%s 耗时%.1fs%s", self.model_name,
                          _toks, _elapsed, f" (重试{attempt}次)" if attempt else "")
                msg = choices[0].message.model_dump()
                return _postprocess_response(LLMResponse(
                    content=msg.get("content") or "",
                    reasoning=msg.get("reasoning_content") or "",
                    tool_calls=_parse_tool_calls(msg),
                    usage=usage,
                    raw_message=msg,
                ))
            last_info = (usage, str(resp.model_dump())[:300])
            _LOG.warning("空响应(疑似限流) 重试 %d/%d 退避%.0fs 耗时%.1fs usage=%s",
                         attempt + 1, self.max_retries, self._backoff(attempt), _elapsed, usage)
            time.sleep(self._backoff(attempt))

        # 重试耗尽：若是 max_tokens 截断（仍有 choices），返回截断结果（保留 partial 内容）而非抛错；
        # 若是空响应，则抛错交给 chat() 走模型回退。
        if choices and choices[0].finish_reason == "length":
            _LOG.warning("⚠️ 加大 max_tokens 后仍截断，返回截断结果(输出可能不完整) model=%s max_tokens=%s",
                         self.model_name, cur_max_tokens)
            msg = choices[0].message.model_dump()
            return _postprocess_response(LLMResponse(
                content=msg.get("content") or "",
                reasoning=msg.get("reasoning_content") or "",
                tool_calls=_parse_tool_calls(msg),
                usage=usage,
                raw_message=msg,
            ))
        _LOG.error("连续 %d 次空响应，放弃 model=%s", self.max_retries, self.model_name)
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
        self._maybe_reset_to_head()
        last_usage = None
        for attempt in range(self.max_retries):
            _t0 = time.time()
            _LOG.debug("流式尝试 %d/%d model=%s", attempt + 1, self.max_retries, self.model_name)
            stream = self._client.chat.completions.create(
                **self._build_kwargs(messages, stream=True, **overrides)
            )
            content_parts, reasoning_parts, tool_call_fragments = [], [], {}
            usage = None
            finish_reason = None
            for chunk in stream:
                if chunk.usage:
                    usage = chunk.usage.model_dump()
                if not chunk.choices:
                    continue
                ch0 = chunk.choices[0]
                if ch0.finish_reason:
                    finish_reason = ch0.finish_reason
                delta = ch0.delta.model_dump()
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
                _toks = usage.get("total_tokens") if usage else None
                _LOG.info("流式成功 model=%s tokens=%s 耗时%.1fs%s", self.model_name,
                          _toks, time.time() - _t0, f" (重试{attempt}次)" if attempt else "")
                if finish_reason == "length":
                    _LOG.warning("⚠️ 流式响应被 max_tokens 截断(finish_reason=length) model=%s —— 输出可能不完整", self.model_name)
                return _postprocess_response(LLMResponse(
                    content="".join(content_parts),
                    reasoning="".join(reasoning_parts),
                    tool_calls=tool_calls,
                    usage=usage,
                ))
            last_usage = usage
            _LOG.warning("流式空 重试 %d/%d 退避%.0fs usage=%s",
                         attempt + 1, self.max_retries, self._backoff(attempt), usage)
            time.sleep(self._backoff(attempt))

        _LOG.error("连续 %d 次流式空响应，放弃 model=%s", self.max_retries, self.model_name)
        raise RuntimeError(
            f"连续 {self.max_retries} 次流式调用都返回空（疑似限流/服务波动）。"
            f" 最后 usage={last_usage}"
        )
