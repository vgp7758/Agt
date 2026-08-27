# verify_debug_hook.py —— /debug hook 冒烟验证（临时，不入库）
# 场景：真实 Agent + 真实 _run_hooks 路径（cmd 钩子，不烧 LLM），
# 验证：不落盘（session 零写入）、钩子跑完即 return、注入预览正确渲染。
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, "src")
sys.stdout.reconfigure(encoding="utf-8")

from agent import Agent
from commands import CommandContext, build_default_registry
from tools import Toolbox

PASS, FAIL = [], []


def check(tag, cond, detail=""):
    (PASS if cond else FAIL).append(tag)
    print(("  ✅ " if cond else "  ❌ ") + tag + (("  -> " + str(detail)[:200]) if detail else ""))


tmp = tempfile.mkdtemp(prefix="agt_dbg_hook_")

# 真实 Agent（空工具箱；verbose=False 免装配噪音）
ag = Agent(system="", tools=Toolbox(), verbose=False, session_dir=Path(tmp))
ag.session._finalize_and_archive() if False else None   # 保持 session 空态

turns_before = len(ag.session.turns)
events_before = 0
try:
    ev_path = ag.session.session_dir / "events.jsonl"
    events_before = len(ev_path.read_text(encoding="utf-8").splitlines()) if ev_path.exists() else 0
except Exception:
    pass

# 注入一个 cmd 钩子（真实 _run_hooks 的 cmd 分支：subprocess + stdout 注入，不烧 LLM）
ag._hook_tasks = lambda hook: ([{
    "kind": "cmd", "name": "echo_probe", "value": "echo [probe] hook-ran-ok",
    "async": False, "recap": False, "meta": {}}] if hook == "before_turn" else [])

reg = build_default_registry()
ctx = CommandContext(agent=ag, state={"busy": False})

print("== T1: /debug hook 正常路径（cmd 钩子）==")
import io, contextlib
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    ok = reg.dispatch("/debug hook 帮我查一下上周的部署记录", ctx)
out = buf.getvalue()
print(out)
check("dispatch 返回 True（识别为命令）", ok is True)
check("打印了 debug hook 头", "[debug hook] before_turn" in out)
check("钩子真实执行（cmd stdout 注入）", "[probe] hook-ran-ok" in out)
check("打印 system-reminder 预览", '<system-reminder pos="before_turn">' in out)
check("预览含 hook 包裹", '<hook name="cmd:echo_probe">' in out)
check("打印耗时", "钩子执行完成" in out)

print("== T2: 不落盘验证 ==")
turns_after = len(ag.session.turns)
ev_after = 0
try:
    ev_path = ag.session.session_dir / "events.jsonl"
    ev_after = len(ev_path.read_text(encoding="utf-8").splitlines()) if ev_path.exists() else 0
except Exception:
    pass
check("session turns 零新增", turns_after == turns_before, f"{turns_before}→{turns_after}")
check("events.jsonl 零新增", ev_after == events_before, f"{events_before}→{ev_after}")
check("_current 无 before_turn_hint 残留", getattr(ag.session._current, "_before_turn_hint", None) is None)

print("== T3: 守卫分支 ==")
ag._hook_tasks = lambda hook: []
buf2 = io.StringIO()
with contextlib.redirect_stdout(buf2):
    reg.dispatch("/debug hook 任意", ctx)
check("无钩子声明 → 提示而非空跑", "未声明 before_turn 钩子" in buf2.getvalue(), buf2.getvalue()[:100])

ag.session.assembly = {"hooks": False}
buf3 = io.StringIO()
with contextlib.redirect_stdout(buf3):
    reg.dispatch("/debug hook 任意", ctx)
check("hooks=off → 提示未启用", "钩子未启用" in buf3.getvalue(), buf3.getvalue()[:100])
ag.session.assembly = {}

buf4 = io.StringIO()
with contextlib.redirect_stdout(buf4):
    reg.dispatch("/debug hook", ctx)
check("缺提示词 → 用法提示", "用法" in buf4.getvalue())

ctx_busy = CommandContext(agent=ag, state={"busy": True})
ag._hook_tasks = lambda hook: [{"kind": "cmd", "name": "x", "value": "echo x", "async": False, "recap": False, "meta": {}}]
buf5 = io.StringIO()
with contextlib.redirect_stdout(buf5):
    reg.dispatch("/debug hook 任意", ctx_busy)
check("busy → 拒绝执行", "busy" in buf5.getvalue())

print("== T4: /debug prompt 原路径未破坏 ==")
buf6 = io.StringIO()
with contextlib.redirect_stdout(buf6):
    reg.dispatch("/debug", ctx)
check("/debug 无参 → 双用法提示", "debug prompt" in buf6.getvalue() and "debug hook" in buf6.getvalue())

print("\n======== 汇总 ========")
print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
if FAIL:
    print("失败项:", FAIL)
    sys.exit(1)
