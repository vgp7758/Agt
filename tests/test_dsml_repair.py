# -*- coding: utf-8 -*-
"""L3 单测：DSML 兜底（正则解析放宽 + 短调用修正）。不连真实 LLM。"""
import sys, json, pathlib as _pl
sys.path.insert(0, str(_pl.Path(__file__).resolve().parents[1] / "src"))

from llm_client import (_DSML, _parse_dsml_calls, looks_like_dsml_toolcall,
                        _postprocess_response, LLMResponse)
import agent as ag

D = _DSML
ok = fail = 0


def chk(name, cond, extra=""):
    global ok, fail
    if cond:
        ok += 1
        print(f"  ✅ {name}")
    else:
        fail += 1
        print(f"  ❌ {name}  {extra}")


print("== 1. 正则兜底解析（4 形态）==")
c1 = f'思考…\n<{D}tool_calls>\n<{D}invoke name="read_file">\n<{D}parameter name="path" string="true">a.py</{D}parameter>\n<{D}parameter name="n" string="false">12</{D}parameter>\n</{D}invoke>\n</{D}tool_calls>'
r = _parse_dsml_calls(c1)
chk("标准形态 → 1 call + 参数类型正确", r and len(r[1]) == 1 and r[1][0]["arguments"] == {"path": "a.py", "n": 12}, str(r))
chk("标准形态 → content 剥除干净", r and r[0] == "思考…", repr(r[0] if r else None))

r2 = _parse_dsml_calls(f'<{D}invoke name="read_file"><{D}parameter name="path">a.py</{D}parameter></{D}invoke>')
chk("缺 string 属性 → 参数不丢（按字符串）", r2 and r2[1][0]["arguments"] == {"path": "a.py"}, str(r2))

c3 = f'思考\n<{D}tool_calls><{D}invoke name="read_file"><{D}parameter name="path" string="true">a.py</{D}parameter></invoke></{D}tool_calls>'
r3 = _parse_dsml_calls(c3)
chk("闭合变体 → 解析出 1 call（旧版漏网）", r3 and len(r3[1]) == 1, str(r3))

c4 = '<||DSML||invoke name="read_file"><||DSML||parameter name="path" string="true">a.py</||DSML||parameter></||DSML||invoke>'
r4 = _parse_dsml_calls(c4)
chk("半角竖线 → 解析出 1 call（旧版 None）", r4 and len(r4[1]) == 1 and r4[1][0]["arguments"] == {"path": "a.py"}, str(r4))

print("== 2. 泄漏判据（不误伤讨论文本）==")
chk("真泄漏（全角）", looks_like_dsml_toolcall(f'<{D}invoke name="x">'))
chk("真泄漏（半角）", looks_like_dsml_toolcall('||DSML||invoke name="x"'))
chk("讨论文本：源码引用不误判", not looks_like_dsml_toolcall(f'_DSML = "{D}"  # 常量定义，后文还有 invoke 字样'))
chk("讨论文本：正常回答", not looks_like_dsml_toolcall("本次修复处理 DSML 泄漏，涉及 invoke 解析"))

print("== 3. _postprocess_response 挂 dsml_raw ==")
res = LLMResponse(content=f'<{D}tool_calls>残缺头，没有合法 invoke 块</{D}tool_calls>')
res = _postprocess_response(res)
chk("解析不出 call → dsml_raw 带原文、content 不被清空", bool(res.dsml_raw) and not res.tool_calls and "残缺头" in res.content,
    f"raw={res.dsml_raw!r} cnt={res.content!r}")

res_ok = _postprocess_response(LLMResponse(content=c1))
chk("解析成功 → tool_calls 回填、dsml_raw 为空", bool(res_ok.tool_calls) and not res_ok.dsml_raw)

print("== 4. 短调用修正（5 场景）==")


class FakeCli:
    def __init__(self, content=None, exc=None):
        self._c, self._exc, self.seen = content, exc, []

    def chat(self, messages, **kw):
        self.seen.append((messages, kw))
        if self._exc:
            raise self._exc
        return LLMResponse(content=self._c)


def mk_agent(cli):
    a = ag.Agent.__new__(ag.Agent)   # 裸对象：只绑 utility_client
    a.utility_client = lambda: cli
    return a


SCHEMAS = [
    {"type": "function", "function": {"name": "read_file", "description": "读文件",
                                      "parameters": {"type": "object", "properties": {"path": {"type": "string"}}}}},
    {"type": "function", "function": {"name": "write_file", "description": "写文件",
                                      "parameters": {"type": "object", "properties": {"path": {"type": "string"}}}}},
]
LEAK = f"<{D}tool_calls><{D}invoke name='read_file'><{D}parameter name='path' string='true'>a.py</{D}parameter></{D}invoke></{D}tool_calls>"


# 4.1 正常修正
cli = FakeCli('{"tool_calls":[{"name":"read_file","arguments":{"path":"a.py","n":3}}]}')
resp = _postprocess_response(LLMResponse(content=LEAK))
a = mk_agent(cli)
rc = a._repair_dsml_via_short_call(resp, SCHEMAS, turn=1, step=2)
chk("正常修正 → True + tool_calls 回填", rc and resp.tool_calls == [{"id": "dsmlfix_0", "name": "read_file", "arguments": {"path": "a.py", "n": 3}}], str(resp.tool_calls))
chk("修正后 content 已剥除 DSML", D not in (resp.content or ""), repr(resp.content))
_sent = cli.seen[0][0][1]["content"]
chk("候选 schema 只给泄漏相关的工具（不含 write_file）", "read_file" in _sent and "write_file" not in _sent)
chk("短调用带 scene/turn/step", cli.seen[0][1].get("scene") == "dsml_repair" and cli.seen[0][1].get("turn") == 1)

# 4.2 带代码围栏
cli2 = FakeCli('```json\n{"tool_calls":[{"name":"read_file","arguments":"{\\"path\\":\\"b.py\\"}"}]}\n```')
resp2 = _postprocess_response(LLMResponse(content=LEAK))
rc2 = mk_agent(cli2)._repair_dsml_via_short_call(resp2, SCHEMAS)
chk("围栏 + 字符串参数 → 修正成功且参数 dict 化", rc2 and resp2.tool_calls[0]["arguments"] == {"path": "b.py"}, str(resp2.tool_calls))

# 4.3 空数组
resp3 = _postprocess_response(LLMResponse(content=LEAK))
rc3 = mk_agent(FakeCli('{"tool_calls":[]}'))._repair_dsml_via_short_call(resp3, SCHEMAS)
chk("空数组 → False 且不回填", (not rc3) and not resp3.tool_calls)

# 4.4 非法 JSON
resp4 = _postprocess_response(LLMResponse(content=LEAK))
rc4 = mk_agent(FakeCli("抱歉我无法完成"))._repair_dsml_via_short_call(resp4, SCHEMAS)
chk("非 JSON → False", (not rc4) and not resp4.tool_calls)

# 4.5 工具名不在表内
resp5 = _postprocess_response(LLMResponse(content=LEAK))
rc5 = mk_agent(FakeCli('{"tool_calls":[{"name":"nope","arguments":{}}]}'))._repair_dsml_via_short_call(resp5, SCHEMAS)
chk("名字不在可用表 → False（校验拦截）", (not rc5) and not resp5.tool_calls)

# 4.6 请求异常
resp6 = _postprocess_response(LLMResponse(content=LEAK))
rc6 = mk_agent(FakeCli(exc=RuntimeError("boom")))._repair_dsml_via_short_call(resp6, SCHEMAS)
chk("请求异常 → False 不炸", (not rc6) and not resp6.tool_calls)

# 4.7 无工具表 → False（不白烧一次请求）
resp7 = _postprocess_response(LLMResponse(content=LEAK))
rc7 = mk_agent(FakeCli('{"tool_calls":[{"name":"read_file","arguments":{}}]}'))._repair_dsml_via_short_call(resp7, [])
chk("无可用工具表 → False", not rc7)

print(f"\n结果：{ok} 过 / {fail} 挂")
sys.exit(1 if fail else 0)
