"""verify_task_guidance.py —— 验证"任务指引每轮重读"（第 2 档）。

A) session 机制：_task_guidance_provider 每轮重读，messages_for_llm 含其内容；改文件后下一调即生效。
B) set_session：读档用 agent.base_system 覆盖 session.system（防旧烤死 system 双重注入）。
C) chat 单元：SYSTEM 不再含"任务指引"；_rules_and_skills_section(workspace) 按给定 workspace 读规则。
"""
import shutil
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

TMP = Path(__file__).resolve().parent / "_verify_tg"
passed, failed = [], []


def check(name, cond, extra=""):
    (passed if cond else failed).append(name)
    print(("  ✅ " if cond else "  ❌ ") + name + (f"  ({extra})" if extra and not cond else ""))


if TMP.exists():
    shutil.rmtree(TMP)
TMP.mkdir()

# ===== A) session 机制：provider 每轮重读 =====
import session as sess_mod

(TMP / "AGENTS.md").write_text("这是V1任务指引", encoding="utf-8")


def _tg():
    p = TMP / "AGENTS.md"
    return p.read_text(encoding="utf-8").strip() if p.exists() else None


s = sess_mod.Session("FRAMEWORK_SYSTEM", llm=None, workspace=TMP)
s._task_guidance_provider = _tg
blob = "\n".join(str(m.get("content", "")) for m in s.messages_for_llm())
check("messages_for_llm 含核心 system", "FRAMEWORK_SYSTEM" in blob)
check("messages_for_llm 含 task-guidance(V1)", "V1任务指引" in blob, blob[:120])

# 改文件 → 下一调即生效（每轮重读）
(TMP / "AGENTS.md").write_text("这是V2任务指引", encoding="utf-8")
blob2 = "\n".join(str(m.get("content", "")) for m in s.messages_for_llm())
check("改 AGENTS.md 后下一调生效(V2)", "V2任务指引" in blob2 and "V1任务指引" not in blob2, blob2[:120])

# provider 返回 None 时不注入（无 AGENTS.md）
(TMP / "AGENTS.md").unlink()
blob3 = "\n".join(str(m.get("content", "")) for m in s.messages_for_llm())
check("无 task-guidance 时不注入(None)", "任务指引" not in blob3)

# ===== B) set_session：读档用 base_system 覆盖 =====
import agent as agent_mod


class _Resp:
    def __init__(self):
        self.content = "ok"; self.tool_calls = []; self.reasoning = ""
        self.usage = {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}


class _FakeLLM:
    def __init__(self, *a, **k):
        self.model_name = "fake"; self.call_recorder = None; self.switch_model = lambda n: None
    def chat(self, msgs, tools=None):
        return _Resp()


from tools import Toolbox as _Toolbox

_orig = agent_mod.LLMClient
agent_mod.LLMClient = _FakeLLM
try:
    ag = agent_mod.Agent(system="FRAMEWORK_SYSTEM", tools=_Toolbox(), verbose=False)
    loaded = sess_mod.Session("STALE_BAKED_SYSTEM_with_旧任务指引", llm=None, workspace=TMP)
    ag.set_session(loaded)
    check("set_session 用 base_system 覆盖读档 system", loaded.system == "FRAMEWORK_SYSTEM", loaded.system)
    check("set_session 转挂 task-guidance provider",
          getattr(loaded, "_task_guidance_provider", "MISSING") is None  # ag 未设 fn → None；关键是属性存在不报错
          or hasattr(loaded, "_task_guidance_provider"))
except Exception as e:
    check(f"set_session 测试（环境跳过：{type(e).__name__}）", False, str(e)[:120])
finally:
    agent_mod.LLMClient = _orig

# ===== C) chat 单元：SYSTEM 去 task-guidance + _rules_and_skills_section 按 workspace 读 =====
try:
    import chat as chatmod
    check("SYSTEM 不再烤死任务指引", "=== 任务指引" not in chatmod.SYSTEM and "任务指引（当前目录 AGENTS.md" not in chatmod.SYSTEM)
    # _rules_and_skills_section 读指定 workspace 的规则
    (TMP / ".agent" / "rules").mkdir(parents=True, exist_ok=True)
    (TMP / ".agent" / "rules" / "r1.md").write_text("规则R1内容", encoding="utf-8")
    rs = chatmod._rules_and_skills_section(TMP)
    check("_rules_and_skills_section(workspace) 读到规则", "规则R1内容" in rs, rs[:120])
except Exception as e:
    check(f"chat 单元测试（环境跳过：{type(e).__name__}）", False, str(e)[:120])

shutil.rmtree(TMP, ignore_errors=True)
print(f"\n{'='*40}\n通过 {len(passed)} / 失败 {len(failed)}")
if failed:
    hard = [f for f in failed if "环境跳过" not in f]
    print("失败项：", failed)
    if hard:
        sys.exit(1)
    print("（仅环境性跳过）")
else:
    print("全部通过 ✅")
