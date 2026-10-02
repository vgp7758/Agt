"""verify_session_class_intact.py —— 结构化回归守卫：Session 类方法集完整性。

背景（2026-09-29 事故）：commit d13adca 以模块级缩进（列 0）把 `_norm_img_data_url`
插进了 `class Session` 体内，Python 在该行结束类定义 → 其后 27 个方法（load / save /
to_history / _project_imgs / recall …）被静默吞成该函数的嵌套定义（死代码）。
症状：重启后无法 /resume 恢复 session（Session.load 不存在）、模型切换后 agent.run
投影崩溃无响应。py_compile 不会报（语法合法），只有 AST 结构检查能抓到。

本脚本做三件事：
  1. Session 的方法集与 d13adca^（事故前）逐名比对 —— 不允许丢方法
  2. 关键方法（load/save/to_history/...）确实挂在类上、可调用
  3. _norm_img_data_url 必须是模块级函数（且不在 Session 体内）
不触发 LLM、不读磁盘 session。退出码非 0 = 有回归。
"""
import ast
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
SRC = ROOT / "src" / "session.py"

passed, failed = [], []


def check(name, cond, extra=""):
    (passed if cond else failed).append(name)
    print(("  ✅ " if cond else "  ❌ ") + name + (f"  ({extra})" if extra and not cond else ""))


def class_methods(src: str, cls: str = "Session"):
    """返回源码中某顶层类的方法名集合；类不存在返回 None。"""
    tree = ast.parse(src)
    for node in tree.body:                      # 只看顶层 —— 嵌套类/函数里的不算
        if isinstance(node, ast.ClassDef) and node.name == cls:
            return {n.name for n in node.body if isinstance(n, ast.FunctionDef)}
    return None


def is_module_level_func(src: str, fn: str) -> bool:
    tree = ast.parse(src)
    return any(isinstance(n, ast.FunctionDef) and n.name == fn for n in tree.body)


now_src = SRC.read_text(encoding="utf-8")

# —— 基线：事故前最后一个版本（d13adca 的父提交）。找不到（浅克隆等）则跳过比对 ——
base_methods = None
try:
    prev = subprocess.run(["git", "show", "d13adca^:src/session.py"],
                          cwd=str(ROOT), capture_output=True, text=True,
                          encoding="utf-8", timeout=30)
    if prev.returncode == 0 and prev.stdout:
        base_methods = class_methods(prev.stdout)
except Exception:
    pass

now_methods = class_methods(now_src)
check("Session 类可被 AST 定位", now_methods is not None)

if base_methods is not None and now_methods is not None:
    lost = sorted(base_methods - now_methods)
    check("Session 方法集未丢（对照 d13adca^）", not lost, f"丢失 {len(lost)} 个: {lost[:8]}")
else:
    print("  ⏭️  跳过基线比对（git 历史不可用）")

# —— 关键方法必须在类上（这是本次事故直接打死的调用点）——
CRITICAL = ["load", "save", "rename", "to_history", "to_history_full",
            "_project_imgs", "_user_content", "_steps_to_messages",
            "recall", "search_turns", "_summarize_turn", "__repr__"]
if now_methods is not None:
    miss = [m for m in CRITICAL if m not in now_methods]
    check("关键方法全部挂在 Session 类上", not miss, f"缺失: {miss}")
else:
    check("关键方法全部挂在 Session 类上", False, "Session 类未解析出")

# —— 图片规范化函数必须是模块级（插错位置就是本次事故的根因）——
check("_norm_img_data_url 是模块级函数", is_module_level_func(now_src, "_norm_img_data_url"))

# —— 动态校验：真的 import 一次，确认方法可调用 ——
sys.path.insert(0, str(ROOT / "src"))
try:
    from session import Session, _norm_img_data_url  # noqa: F401
    check("import session 后 Session.load 可调用", callable(getattr(Session, "load", None)))
    check("import session 后 Session.to_history 可调用", callable(getattr(Session, "to_history", None)))
except Exception as e:
    check("import session 成功", False, f"{type(e).__name__}: {e}")

print(f"\n{len(passed)} passed, {len(failed)} failed")
sys.exit(1 if failed else 0)
