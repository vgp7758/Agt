"""snapshots.py —— 工作区文件快照与回溯（独立 git 仓库，与用户的 .git 完全隔离）。

在 workspace/.agt/snapshots 建一个独立的 git 仓库（git-dir 与用户自己的 .git 分开），
打快照 = 把当前工作区文件树写成 commit（write-tree + commit-tree，非破坏，只造对象）；
回溯 = 把工作区文件树还原到该快照（read-tree + checkout-index + clean，删掉快照后新建的文件）。
配合 Session 的对话截断，实现"回到某条用户指令发送前"。

注意：只覆盖本地文件；外部副作用（MCP challenge/publish、联网提交等）不可逆，管不了。
所有 git 操作用一把锁串行化，避免多连接并发打快照时损坏仓库。
"""
from __future__ import annotations

import subprocess
import threading
import time
from pathlib import Path


class SnapshotManager:
    def __init__(self, workspace):
        self.workspace = Path(workspace).resolve()
        self.git_dir = self.workspace / ".agt" / "snapshots"
        self._lock = threading.Lock()  # 串行化 git 操作，防并发损坏

    def _run(self, args, check=True) -> str:
        cmd = ["git", "--git-dir", str(self.git_dir),
               "--work-tree", str(self.workspace), *args]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if check and r.returncode != 0:
            raise RuntimeError(f"git {args[0]} 失败: {r.stderr.strip() or r.stdout.strip()}")
        return r.stdout.strip()

    def ensure_repo(self):
        """首次使用时初始化快照仓库（bare + 指定 worktree），并排除自身/用户 .git。"""
        if (self.git_dir / "HEAD").exists():
            return
        self.git_dir.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "init", "--bare", str(self.git_dir)],
                       check=True, capture_output=True)
        self._run(["config", "core.bare", "false"])
        self._run(["config", "core.worktree", str(self.workspace)])
        self._run(["config", "user.name", "Agt Snapshots"])          # commit-tree 需要
        self._run(["config", "user.email", "snapshots@agt.local"])
        exclude = self.git_dir / "info" / "exclude"
        exclude.parent.mkdir(parents=True, exist_ok=True)
        existing = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
        for p in (".agt/", ".git/"):                                 # 不快照自身 & 用户的 .git
            if p not in existing:
                existing += f"\n{p}"
        exclude.write_text(existing.strip() + "\n", encoding="utf-8")

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
        """把工作区文件树还原到 sha 对应的快照（含删除快照之后新建的文件）。"""
        with self._lock:
            self.ensure_repo()
            self._run(["cat-file", "-t", sha])  # 校验 sha 存在，不存在会抛错
            tree = self._run(["rev-parse", f"{sha}^{{tree}}"])
            self._run(["read-tree", tree])
            self._run(["checkout-index", "-a", "-f"])
            # 删除快照之后新建的文件（快照仓库视角下的未跟踪文件；.agt/.git 已在 exclude）
            self._run(["clean", "-fd", "-e", ".agt"])
