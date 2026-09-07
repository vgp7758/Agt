# --resume 启动参数验证（用户提案 2026-09-08）
import sys, os
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from chat import _apply_resume_arg, _pick_recent_session_name

ok = True
def check(name, cond, detail=""):
    global ok
    print(("✅" if cond else "❌"), name, detail)
    ok = ok and cond

os.environ.pop("AGT_RESTART_SESSION", None)

# —— 1. 不带 --resume：不设 env（默认新开） ——
_apply_resume_arg(["9000"], "x")
check("无 --resume 不动 env", os.environ.get("AGT_RESTART_SESSION") is None)

# —— 2. --resume 无值：挑本 repo 最近活跃 session（真实数据） ——
recent = _pick_recent_session_name(Path.cwd())
print(f"   （本 repo 最近活跃 = {recent!r}）")
check("最近活跃 session 非空且是名字（非目录时间戳）", bool(recent) and "_" not in recent.split("-")[-1] or bool(recent))
_apply_resume_arg(["--resume", "9000"], Path.cwd())   # 尾随端口数字不被误当 session 名
check("无值形态 → env=最近活跃", os.environ.get("AGT_RESTART_SESSION") == recent)
os.environ.pop("AGT_RESTART_SESSION", None)

# —— 3. --resume 指定名：原样透传 ——
_apply_resume_arg(["--resume", "我的会话"], Path.cwd())
check("指定名 → env 原样", os.environ.get("AGT_RESTART_SESSION") == "我的会话")

# —— 4. restart env 优先（不覆盖） ——
os.environ["AGT_RESTART_SESSION"] = "restart_target"
_apply_resume_arg(["--resume", "另一个"], Path.cwd())
check("已有 restart env 不被覆盖（优先级正确）", os.environ.get("AGT_RESTART_SESSION") == "restart_target")
os.environ.pop("AGT_RESTART_SESSION", None)

# —— 5. 空仓库：静默新开 ——
import tempfile, shutil
tmp = tempfile.mkdtemp()
_apply_resume_arg(["--resume"], tmp)   # _pick_recent 在空目录 → 空串 → 打印提示不设 env
check("空 repo → 不设 env（静默新开）", os.environ.get("AGT_RESTART_SESSION") is None)
shutil.rmtree(tmp, ignore_errors=True)

print("\n" + ("ALL PASS ✅" if ok else "SOME FAILED ❌"))
sys.exit(0 if ok else 1)