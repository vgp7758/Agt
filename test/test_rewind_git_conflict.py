# rewind 撞车检测功能测试（用户提案 2026-09-06）：真 git 仓库 + 快照全链路三场景
import subprocess, sys, tempfile, types
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from snapshots import SnapshotManager, user_repo_head
from chat import restore_snapshot

def _git(ws, *args):
    r = subprocess.run(["git", "-C", str(ws), *args], capture_output=True, text=True)
    assert r.returncode == 0, f"git {args} 失败: {r.stderr}"
    return r.stdout.strip()

ws = Path(tempfile.mkdtemp(prefix="rewind_test_"))
_git(ws, "init"); _git(ws, "config", "user.email", "t@t"); _git(ws, "config", "user.name", "t")
(ws / "a.txt").write_text("v0", encoding="utf-8")
_git(ws, "add", "-A"); _git(ws, "commit", "-m", "c0")
H0 = user_repo_head(ws)

snap = SnapshotManager(ws)
class _FakeSession:
    def __init__(self): self.m = {}
    def git_head_at_snapshot(self, sha): return self.m.get(sha, "")
    def restore_to_snapshot(self, sha): return "被截的那轮 user_message"
session = _FakeSession()
agent = types.SimpleNamespace(snapshot_manager=snap, session=session)

ok = True
def check(name, cond, detail=""):
    global ok
    print(("✅" if cond else "❌"), name, detail)
    ok = ok and cond

# —— 场景 1：检查点后无提交 → 正常回溯 ——
sha1 = snap.snapshot(); session.m[sha1] = H0
t = restore_snapshot(agent, sha1)
check("场景1 无提交：回溯成功返回目标轮", t == "被截的那轮 user_message")

# —— 场景 2：检查点后有提交 → 默认 block 拦截 ——
H_mid = user_repo_head(ws)
sha2 = snap.snapshot(); session.m[sha2] = H_mid        # 此时 HEAD 仍=H0（c0）
(ws / "a.txt").write_text("v1-future", encoding="utf-8")
_git(ws, "add", "-A"); _git(ws, "commit", "-m", "c1-future")   # 检查点之后的提交
H1 = user_repo_head(ws)
check("前置：HEAD 已前进（撞车条件成立）", H1 != H_mid)
try:
    restore_snapshot(agent, sha2)
    check("场景2 有提交：默认被拦截", False, "（未抛异常——拦失败了）")
except RuntimeError as e:
    msg = str(e)
    check("场景2 有提交：默认被拦截（RuntimeError）", True)
    check("  错误信息含提交清单", "c1-future" in msg)
    check("  错误信息含 --git reset 指引", "--git reset" in msg)
    check("  拦截后 HEAD 未动", user_repo_head(ws) == H1)
check("  拦截后 a.txt 仍是未来内容", (ws / "a.txt").read_text(encoding="utf-8") == "v1-future")

# —— 场景 3：--git reset → HEAD 退回检查点时刻 + 回溯完成 ——
t3 = restore_snapshot(agent, sha2, git_policy="reset")
check("场景3 reset：回溯成功", t3 == "被截的那轮 user_message")
check("  HEAD 已退回检查点时刻", user_repo_head(ws) == H_mid)
check("  a.txt 内容随 --hard 回退", (ws / "a.txt").read_text(encoding="utf-8") == "v0")
check("  被退提交 reflog 可找回", "c1-future" in _git(ws, "reflog", "--all"))

# —— 场景 4：git_head 无记录（旧档/非 git 仓库）→ 不拦（保持旧行为）——
sha4 = snap.snapshot(); session.m[sha4] = ""            # 旧档轮无 git_head
t4 = restore_snapshot(agent, sha4)
check("场景4 旧档无记录：不拦截直接回溯", t4 == "被截的那轮 user_message")

import shutil; shutil.rmtree(ws, ignore_errors=True)
print("\n" + ("ALL PASS ✅" if ok else "SOME FAILED ❌"))
sys.exit(0 if ok else 1)
