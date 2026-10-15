"""snapshots.py —— 工作区文件快照与回溯（独立 git 仓库，与用户的 .git 完全隔离）。

在 workspace/.agt/snapshots 建一个独立的 git 仓库（git-dir 与用户自己的 .git 分开），
打快照 = 把当前工作区文件树写成 commit（write-tree + commit-tree，非破坏，只造对象）；
回溯 = 把工作区文件树还原到该快照（read-tree + checkout-index + clean，删掉快照后新建的文件）。
配合 Session 的对话截断，实现"回到某条用户指令发送前"。

注意：只覆盖本地文件；外部副作用（MCP challenge/publish、联网提交等）不可逆，管不了。
所有 git 操作用一把锁串行化，避免多连接并发打快照时损坏仓库。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import threading
import time
from pathlib import Path


_GIT_TIMEOUT = 120               # 单条快照 git 命令墙钟上限（秒）


class _CmdTimeout(TimeoutError):
    """快照 git 子命令超时（进程树已杀）——调用方捕获转译/直接当快照失败（agent.run 的
    except 会 emit warn，轮照常收尾，busy 复位），绝不再无限挂。"""


def _kill_tree(proc) -> None:
    """杀子进程【树】：git 偶发拉起孙进程（auto maintenance/credential helper 等）并被子进程
    继承 stdout 句柄——只 kill 父进程管道不关，communicate() 的 reader 线程永等。"""
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                           capture_output=True, timeout=10,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        else:
            proc.kill()
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def _run_cmd(cmd, timeout=120, cwd=None):
    """跑子命令 → (returncode, stdout, stderr)。

    为什么不用 subprocess.run(timeout=...)：run 的超时路径是「kill 父进程 → 再 communicate()
    （无超时）」——子进程若拉起过孙进程（继承 stdout 句柄），父死后管道永不 EOF，
    第二次 communicate() 永等（2026-10-16 9634/9635 实例 py-spy 实锤：worker 卡死在
    snapshots._run → subprocess join，busy 恒真，前端灯不灭）。这里显式
    communicate(timeout) + 超时杀树释放句柄 → 抛 _CmdTimeout 快速失败。
    stdin 恒 DEVNULL（2026-09-16 9300 实例 git init 挂死的已验证药方：防读继承管道）；
    文本按 UTF-8 解（sha/报错含仓库文件名，locale 解码会乱）。cwd 必须传 worktree 根：
    git 的路径参数（.agt、. 等 pathspec）按「调用进程 cwd」解析——服务进程 cwd 一般就是
    workspace 所以从未炸过，但诊断脚本/其它 cwd 调进来会静默打偏（2026-10-16 验证实锤：
    rm --cached .agt 在仓库根 cwd 下 pathspec 不匹配被 check=False 吞掉）。"""
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            stdin=subprocess.DEVNULL, text=True, encoding="utf-8",
                            errors="replace", cwd=cwd,
                            creationflags=(subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0))
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_tree(proc)
        try:
            out, err = proc.communicate(timeout=5)
        except Exception:
            out, err = "", ""
        raise _CmdTimeout(f"{' '.join(cmd[:2])} … 超时（{timeout}s，进程树已杀）: "
                          f"{(err or out or '').strip()[:200]}") from None
    return proc.returncode, (out or ""), (err or "")


class SnapshotManager:
    def __init__(self, workspace):
        self.workspace = Path(workspace).resolve()
        self.git_dir = self.workspace / ".agt" / "snapshots"
        self._lock = threading.Lock()  # 串行化 git 操作，防并发损坏

    def _run(self, args, check=True) -> str:
        cmd = ["git", "--git-dir", str(self.git_dir),
               "--work-tree", str(self.workspace), *args]
        code, out, err = _run_cmd(cmd, timeout=_GIT_TIMEOUT, cwd=str(self.workspace))
        if check and code != 0:
            raise RuntimeError(f"git {args[0]} 失败: {(err or out).strip()}")
        return (out or "").strip()

    def ensure_repo(self):
        """首次使用时初始化快照仓库（bare + 指定 worktree），并排除自身/用户 .git。
        HEAD 存在 ≠ 初始化完整（2026-10-16 _regr_ws 实锤：init 后 exclude 未落盘的半成品仓
        会在此永久早退——.agt/ 被 add -A 整体跟踪（index 11k 条目，含快照对象库自身），
        每轮 add -A 全量扫对象库（秒级且随对象数膨胀），restore 又把 .agt/snapshots 内部
        文件当 tracked 还原 → 自覆盖报错）。排除块缺失时补写，并摘除 index 里的历史误跟踪。"""
        exclude = self.git_dir / "info" / "exclude"
        if not (self.git_dir / "HEAD").exists():
            self.git_dir.mkdir(parents=True, exist_ok=True)
            # timeout + DEVNULL（2026-09-16 9300 实例实测挂死：git init 在无 console 的服务进程
            # 上下文里偶发不退——_run_cmd 统一超时兜底 + 杀树 + stdin 显式断开防继承管道）
            try:
                _run_cmd(["git", "init", "--bare", str(self.git_dir)], timeout=120)
            except _CmdTimeout as e:
                raise RuntimeError("git init --bare 超时（120s）——快照仓库初始化受阻，检查磁盘/杀毒") from e
            self._run(["config", "core.bare", "false"])
            self._run(["config", "core.worktree", str(self.workspace)])
            self._run(["config", "user.name", "Agt Snapshots"])          # commit-tree 需要
            self._run(["config", "user.email", "snapshots@agt.local"])
        old = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
        existing = old
        for p in (".agt/", ".git/"):                                 # 不快照自身 & 用户的 .git
            if p not in existing:
                existing += f"\n{p}"
        if existing != old:   # 本次有补写 = 半成品/旧仓修复路径
            exclude.parent.mkdir(parents=True, exist_ok=True)
            exclude.write_text(existing.strip() + "\n", encoding="utf-8")
            # exclude 只拦「未跟踪」；已被误跟踪的 .agt 仍会被每轮 add -A 扫描/提交，须显式摘除
            # （-f 必须带：个别条目 staged 内容与工作区/HEAD 均不同时会拒绝部分摘除，残留
            #   .agt/snapshots/index 等自噬条目——2026-10-16 _regr_ws 验证实锤。--cached 下
            #   -f 只动 index，工作区文件不动）
            self._run(["rm", "-r", "--cached", "-f", "-q", ".agt"], check=False)

    def snapshot(self) -> str:
        """对当前工作区打快照，返回 commit SHA。"""
        with self._lock:
            self.ensure_repo()
            self._run(["add", "-A"])
            tree = self._run(["write-tree"])
            sha = self._run(["commit-tree", tree, "-m", f"snap-{int(time.time())}"])
            self._run(["update-ref", f"refs/agt/snap/{sha}", sha])  # 挂 ref 防止被 GC
            return sha

    def restore(self, sha: str) -> None:
        """把工作区文件树还原到 sha 对应的快照（含删除快照之后新建的文件）。

        用 git restore（而非 read-tree+checkout-index -f）：只重写内容有差异的文件，
        内容未变的文件 mtime 保持——rewind 不再刷全仓 mtime（否则 tools 热重载/静态页
        mtime 缓存全部失效重读）。git < 2.23 无 restore 时退回老路径（会刷 mtime，语义等价）。"""
        with self._lock:
            self.ensure_repo()
            self._run(["cat-file", "-t", sha])  # 校验 sha 存在，不存在会抛错
            tree = self._run(["rev-parse", f"{sha}^{{tree}}"])
            try:
                self._run(["restore", f"--source={tree}", "--staged", "--worktree", "--", "."])
            except RuntimeError:
                self._run(["read-tree", tree])
                self._run(["checkout-index", "-a", "-f"])
            # 删除快照之后新建的文件（快照仓库视角下的未跟踪文件；.agt/.git 已在 exclude）
            self._run(["clean", "-fd", "-e", ".agt"])

    def clean(self) -> int:
        """删除整个快照仓库（workspace/.agt/snapshots），从 0 开始。返回释放字节数（估算）。

        用户提案 2026-10-14（/clean 命令）：快照仓库随轮数增长（大仓库可达数十 GB），
        旧快照的回溯价值随会话推进递减——一刀清空、下一轮 snapshot() 经 ensure_repo 自动
        重建，重新累积。与 snapshot/restore 拿同一把锁（防与正在进行的 git 操作并发）。

        后果（调用方须提示用户）：session 各 turn 记录的 snapshot_sha 随 git 对象一并
        消失——/rewind 到清理前的轮会失败（快照点不存在）；清理后的轮照常可回溯。"""
        with self._lock:
            if not self.git_dir.exists():
                return 0
            total = 0
            for f in self.git_dir.rglob("*"):
                try:
                    if f.is_file():
                        total += f.stat().st_size
                except OSError:
                    pass
            # Windows：git 对象文件是 read-only 属性，裸 rmtree 报 PermissionError 静默残留——
            # onerror 先 chmod 可写再重试（Linux 无只读障碍，回调不触发）。
            import stat as _stat
            def _rm_onerr(func, p, _exc):
                try:
                    os.chmod(p, _stat.S_IWRITE)
                    func(p)
                except Exception:
                    pass
            shutil.rmtree(self.git_dir, onerror=_rm_onerr)
            return total


# ---------- 用户真仓库 HEAD 工具（rewind 撞车检测，用户提案 2026-09-06）----------
# 快照仓库只管工作区文件树；用户的 .git（真仓库）HEAD 不受快照回溯影响——
# 检查点之后若有 git 提交，回溯会导致「session 回到过去、git 历史在未来」的撞车
# （之后 add -A 会把回溯差异整笔提交）。三个小工具给 chat.restore_snapshot 用。

def _user_git(workspace, args, check=True) -> str:
    code, out, err = _run_cmd(["git", "-C", str(workspace), *args], timeout=60,
                              cwd=str(workspace))
    if check and code != 0:
        raise RuntimeError(f"git {args[0]} 失败: {(err or out).strip()}")
    return (out or "").strip()

def user_repo_head(workspace) -> str:
    """用户真仓库当前 HEAD sha；非 git 仓库/失败返回空串（=不启用撞车检测）。"""
    try:
        if not (Path(workspace) / ".git").exists():
            return ""
        return _user_git(workspace, ["rev-parse", "HEAD"], check=False)
    except Exception:
        return ""

def git_log_between(workspace, old_head: str, new_head: str) -> str:
    """old_head..new_head 的 oneline 提交清单（撞车提示用）。"""
    try:
        return _user_git(workspace, ["log", "--oneline", f"{old_head}..{new_head}"], check=False)
    except Exception:
        return ""

def user_repo_reset_hard(workspace, sha: str) -> None:
    """真仓库 git reset --hard <sha>（rewind 撞车的处置方案 B：HEAD 一并退到检查点时刻）。
    被退掉的提交仍在 reflog 可找回；若已 push 过，之后需 push --force 覆盖远端。
    防护：reset 前把 .agt/ 从 index 摘除（若被真仓库误跟踪）——--hard 只重置 tracked 文件，
    摘除后 .agt 变 untracked 保留在工作区，防快照仓库(.agt/snapshots)被连带删除
    （测试实测踩中：真仓库 add -A 跟踪了 .agt → reset --hard 把整个快照仓库删了）。"""
    try:
        _user_git(workspace, ["rm", "-r", "--cached", "-q", ".agt"], check=False)
    except Exception:
        pass
    _user_git(workspace, ["reset", "--hard", sha])
