"""verify_kw_cache.py —— 验证 extract_keywords 子工作流的应用级 KV 缓存。

覆盖：
  1. kv_cache_read/write 工具：miss→hit 行为、namespace 隔离、Toolbox JSON 往返
  2. extract_keywords 子工作流：第一次执行走 LLM（cached=false）；第二次同输入命中缓存
     （cached=true 且 fake llm 调用计数不增加）；换消息重新 miss
  3. 两个改造后的调用方 XML（before_turn_retrieval / wiki_auto_query）解析无破坏，
     LLM 关键词节点已替换为 workflowId=extract_keywords 的 subworkflow 引用

跑法：python verify_kw_cache.py
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

import real_tools
from workflow import execute
from workflow_xml import xml_to_canvas

passed, failed = [], []
def check(name, cond, extra=""):
    (passed if cond else failed).append(name)
    print(("  ✅ " if cond else "  ❌ ")+name+(f"  ({extra})" if extra and not cond else ""))

WF_DIR = Path(__file__).resolve().parent / ".agent" / "workflows"

# —— 1. 工具层 ——
print("【1】kv_cache_read/write 工具")
real_tools._KV_CACHE.clear()
real_tools.kv_cache_write("你好世界", ["缓存", "测试"], namespace="t")
r = real_tools.kv_cache_read("你好世界", namespace="t")
check("write 后 read 命中且值一致", r == {"hit": True, "value": ["缓存", "测试"]}, str(r))
check("namespace 隔离：换名不命中", real_tools.kv_cache_read("你好世界", namespace="other")["hit"] is False)
check("未写过的 key 不命中", real_tools.kv_cache_read("不存在", namespace="t")["hit"] is False)
raw = real_tools.LIGHT_TOOLS.call("kv_cache_read", {"key": "你好世界", "namespace": "t"})
check("经 Toolbox 调用返回 JSON 文本（plugin 节点 _try_parse 可还原）",
      isinstance(raw, str) and '"hit"' in raw, raw[:60])

# —— 2. 子工作流两次执行 ——
print("\n【2】extract_keywords 子工作流：miss→LLM→write，第二次命中不调 LLM")

class _FakeResp:
    def __init__(self, content): self.content, self.reasoning = content, ""

class _FakeLLM:
    model_name = "local-qwen"   # 与节点 <model> 一致：_get_llm 直接复用 ctx.llm
    calls = 0
    def chat(self, msgs, **kw):
        _FakeLLM.calls += 1
        return _FakeResp('{"keywords": ["工作流", "缓存", "关键词"]}')

canvas = xml_to_canvas((WF_DIR / "extract_keywords.xml").read_text(encoding="utf-8"))
real_tools._KV_CACHE.clear()
_FakeLLM.calls = 0
fake = _FakeLLM()
MSG = "帮我看看工作流的关键词缓存怎么配"

r1 = execute(canvas, {"user_message": MSG}, tools=real_tools.LIGHT_TOOLS,
             llm=fake, return_exit_dict=True)
check("第一次：走 LLM 分支（cached=false）", r1.get("cached") is False, str(r1))
check("第一次：LLM 被调用 1 次", _FakeLLM.calls == 1, str(_FakeLLM.calls))
check("第一次：keywords 是 fake LLM 的列表", r1.get("keywords") == ["工作流", "缓存", "关键词"], str(r1.get("keywords")))

r2 = execute(canvas, {"user_message": MSG}, tools=real_tools.LIGHT_TOOLS,
             llm=fake, return_exit_dict=True)
check("第二次同输入：命中缓存（cached=true）", r2.get("cached") is True, str(r2))
check("第二次：LLM 调用数不增加（仍 1）", _FakeLLM.calls == 1, str(_FakeLLM.calls))
check("第二次：keywords 与第一次一致", r2.get("keywords") == r1.get("keywords"), str(r2.get("keywords")))

r3 = execute(canvas, {"user_message": MSG + "（换个说法）"}, tools=real_tools.LIGHT_TOOLS,
             llm=fake, return_exit_dict=True)
check("换消息：重新 miss（cached=false）且 LLM 再调一次", r3.get("cached") is False and _FakeLLM.calls == 2,
      f"cached={r3.get('cached')} calls={_FakeLLM.calls}")

# —— 3. 两个调用方 XML ——
print("\n【3】调用方 XML：subworkflow 替换无破坏")
for name in ("before_turn_retrieval", "wiki_auto_query", "extract_keywords"):
    try:
        xml_to_canvas((WF_DIR / f"{name}.xml").read_text(encoding="utf-8"))
        check(f"{name}.xml 解析通过", True)
    except Exception as e:
        check(f"{name}.xml 解析通过", False, f"{type(e).__name__}: {e}")

for name in ("before_turn_retrieval", "wiki_auto_query"):
    c = xml_to_canvas((WF_DIR / f"{name}.xml").read_text(encoding="utf-8"))
    subs = [n for n in c["nodes"] if str(n.get("type")) == "9"]
    wfids = [str(n.get("data", {}).get("inputs", {}).get("workflowId", "")) for n in subs]
    check(f"{name}：含 workflowId=extract_keywords 的 subworkflow 节点", "extract_keywords" in wfids, str(wfids))
    xml_text = (WF_DIR / f"{name}.xml").read_text(encoding="utf-8")
    check(f"{name}：无 keywards/旧 LLM 提取节点残留", "keywards" not in xml_text)

print(f"\n{'🎉 全通过' if not failed else '⚠️ '+str(len(failed))+' 项失败'}（{len(passed)} 通过）")
