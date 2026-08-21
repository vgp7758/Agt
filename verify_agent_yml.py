"""verify_agent_yml.py —— 验证 agent 元信息 .yml 化（main.yml / 子 agent .yml）+ 钩子 DSL 重构。

覆盖：
  1. load_agent_yml：.yml 直读 / .md frontmatter 兼容
  2. assembly func: 项 + text 内联 {func:load_models()} 插值
  3. hooks 解析：workflow/cmd/emit 项 + | async 标志 + 未知位置忽略
  4. _hook_tasks：yml 声明优先 / 回退扫描；emit/cmd 项分派
  5. seed_main_agent 播种 ~/.agt/main.yml；主 system 走 assembly text 项
  6. migrate_agents_md_to_yml / load_agents_index 幂等 + yml 优先

跑法：python verify_agent_yml.py
"""
import sys, tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

import yaml
from agent_config import (load_agent_yml, _split_frontmatter, resolve_assembly_func,
                          migrate_agents_md_to_yml, load_agents_index, seed_main_agent)
from multiagent import _parse_assembly, _parse_hooks

passed, failed = [], []
def check(name, cond, extra=""):
    (passed if cond else failed).append(name)
    print(("  ✅ " if cond else "  ❌ ")+name+(f"  ({extra})" if extra and not cond else ""))


# —— 1. load_agent_yml ——
print("【1】load_agent_yml")
d = Path(tempfile.mkdtemp())
yml = d / "x.yml"
yml.write_text("name: x\nmodel: glm\nassembly:\n  - text: persona\n", encoding="utf-8")
meta, body = load_agent_yml(yml)
check("yml 直读 meta", meta.get("name") == "x" and meta.get("model") == "glm", str(meta))
check("yml 无正文（正文进 assembly）", body == "", repr(body))

md = d / "y.md"
md.write_text("---\nname: y\ndescription: d\n---\n这是正文\n", encoding="utf-8")
m2, b2 = load_agent_yml(md)
check("md frontmatter 兼容", m2.get("name") == "y", str(m2))
check("md 正文提取", b2 == "这是正文\n", repr(b2))

# —— 2. func 插值 ——
print("\n【2】assembly func 项")
from session import Session
from session import _interp_funcs
s = Session("")
out = _interp_funcs("可用模型：{func:load_models()}。")
check("text 内联 func 插值", "可用模型：" in out and "glm" in out and "{func:" not in out, out[:80])
check("resolve_assembly_func 未知返回空", resolve_assembly_func("no_such") == "")

# func 项走 assembly
s2 = Session("")
s2.set_assembly_plan([{"kind": "seg", "name": "system"},
                      {"kind": "func", "func": "load_models"},
                      {"kind": "seg", "name": "user_message"},
                      {"kind": "seg", "name": "steps"}])
c = " || ".join(str(m.get("content")) for m in s2.messages_for_llm())
check("func 项注入模型清单", "glm" in c, c[:80])

# —— 3. hooks 解析 ——
print("\n【3】_parse_hooks")
h = _parse_hooks({"hooks": {
    "before_turn": ["workflow: wiki_auto_query", "cmd: python x.py -y"],
    "before_tool": ["emit: confirm_tool_use"],
    "before_answer": ["workflow: wiki_auto_maintenance | async"],
    "turn_end": [{"workflow": "recap_gen | async"}],
    "bogus_hook": ["workflow: x"],
}})
check("before_turn 两 item（workflow+cmd）", len(h["before_turn"]) == 2 and h["before_turn"][0]["kind"] == "workflow" and h["before_turn"][1]["kind"] == "cmd", str(h.get("before_turn")))
check("emit 项", h["before_tool"][0] == {"kind": "emit", "value": "confirm_tool_use"}, str(h.get("before_tool")))
check("| async 标志", h["before_answer"][0].get("async") is True, str(h.get("before_answer")))
check("dict 写法 workflow+async", h["turn_end"][0]["kind"] == "workflow" and h["turn_end"][0].get("async") is True, str(h.get("turn_end")))
check("未知位置忽略", "bogus_hook" not in h)
check("无 hooks 返回空", _parse_hooks({}) == {})

# —— 4. _hook_tasks 分派（需真实 Agent，构造轻量）——
print("\n【4】hook_tasks 分派")
from agent import Agent
from tools import Toolbox
from real_tools import REAL_TOOLS
ag = Agent("", REAL_TOOLS, enable_thinking=False)
ag.session.hook_specs = {"before_turn": [{"kind": "cmd", "value": "echo hello_hook"}],
                          "before_tool": [{"kind": "emit", "value": "confirm_tool_use"}]}
# cmd 任务（不跑 workflow 分支，直接验证分派结构）
emit_events = []
ag._emit = lambda e: emit_events.append(e)
# cmd 项：直接注入 stdout
notes = ag._run_hooks("before_turn", {"user_message": "hi"})
c_notes = " ".join(str(n.get("result")) for n in notes)
check("cmd 项执行并注入 stdout", "hello_hook" in c_notes, c_notes)
# emit 项：发事件
ag._run_hooks("before_tool", {"tool_name": "x"})
check("emit 项触发事件", any(e.get("type") == "confirm_tool_use" for e in emit_events), str(emit_events))

# —— 5. main.yml 播种 + 主 system 走 text 项 ——
print("\n【5】main.yml 播种")
src_main = Path(__file__).resolve().parent / "src" / "assets" / "main.yml"
check("随包 main.yml 存在", src_main.exists())
mmeta, _ = load_agent_yml(src_main)
check("main.yml 有 assembly text 项", any(it.get("kind") == "text" for it in _parse_assembly(mmeta)))
check("main.yml 有 hooks 声明", bool(_parse_hooks(mmeta)), str(list(_parse_hooks(mmeta).keys())))

# —— 6. md→yml 迁移 ——
print("\n【6】md→yml 迁移")
ws = Path(tempfile.mkdtemp())
ag_dir = ws / ".agent" / "agents"
ag_dir.mkdir(parents=True)
(ag_dir / "foo.md").write_text("---\nname: foo\ndescription: ddd\n---\n正文persona\n", encoding="utf-8")
n = migrate_agents_md_to_yml(ws)
check("迁移 1 个", n == 1, str(n))
check("foo.yml 生成", (ag_dir / "foo.yml").exists())
yml_meta, _ = load_agent_yml(ag_dir / "foo.yml")
check("yml 正文→assembly 首个 text", yml_meta["assembly"][0] == {"text": "正文persona"}, str(yml_meta["assembly"]))
idx = load_agents_index(ws)
check("索引读到 foo（yml 优先）", any(a["name"] == "foo" for a in idx), str(idx))
n2 = migrate_agents_md_to_yml(ws)
check("幂等（二次迁移 0）", n2 == 0, str(n2))

# —— 7. tool: 装配动作 ——
print("\n【7】tool: 装配动作（rules 展开）")
from multiagent import _parse_tool_expr
check("tool 表达式解析", _parse_tool_expr("read_file(AGENTS.md)") == ("read_file", "AGENTS.md"))
check("tool glob 参数解析", _parse_tool_expr("concat_files(.agent/rules/*.md)")[0] == "concat_files")
it = _parse_assembly({"assembly": [{"tool": "read_file(AGENTS.md)"}, "user_message"]})
tool_it = next(x for x in it if x.get("kind") == "tool")
check("yml tool 项带 tool_name/tool_args", tool_it.get("tool_name") == "read_file" and tool_it.get("tool_args") == "AGENTS.md", str(tool_it))
s7 = Session("")
s7.set_assembly_plan([{"kind": "seg", "name": "system"},
                      {"kind": "tool", "tool": "read_file(AGENTS.md)", "tool_name": "read_file", "tool_args": "AGENTS.md", "timing": "turn"},
                      {"kind": "seg", "name": "user_message"},
                      {"kind": "seg", "name": "steps"}])
from real_tools import REAL_TOOLS
s7._asm_workflow_tools = REAL_TOOLS
c7 = " || ".join(str(m.get("content")) for m in s7.messages_for_llm())
check("tool:read_file 执行并注入", "assembly:tool" in c7 and len(c7) > 100, c7[:80])
check("未知工具跳过不炸", s7._asm_evaluate({"kind": "tool", "tool_name": "no_such_tool", "tool_args": ""}) == "")
from real_tools import LIGHT_TOOLS as _LT
check("concat_files glob 拼接", "===" in _LT.call("concat_files", {"pattern": ".agent/rules/*.md"}))

print(f"\n{'🎉 全通过' if not failed else '⚠️ '+str(len(failed))+' 项失败'}（{len(passed)} 通过）")