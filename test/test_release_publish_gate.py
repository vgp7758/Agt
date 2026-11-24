"""release_publish 的成败判定只认退出码（2026-11-25，v0.35.17 发版实锤）。

此前判定是 `rc != 0 or ("❌" in out or "失败" in out)`，而 release.py 的发布报告会把
commit message 全文打印——说明里带「失败」二字（"修 X 失败" 是发布说明的常见措辞）就被
误判成失败，跳过 version_file 同步 / 打 tag / PyPI 验证三步，只能手工补尾。
"""
import json
import subprocess
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / ".agent" / "tools"))

import release_tools  # noqa: E402


class _R:
    def __init__(self, rc=0, out="", err=""):
        self.returncode, self.stdout, self.stderr = rc, out, err


def _report(commit_line: str) -> str:
    return ("📦 版本：0.35.17 → 0.35.18\n✅ 已提交 abc1234\n✅ 构建完成：x.whl, x.tar.gz\n"
            "✅ 已上传 PyPI：https://pypi.org/project/agt-agent/0.35.18/\n\n"
            "========== 发布报告 ==========\n"
            f"  版本     0.35.17 → 0.35.18\n  提交     abc1234  {commit_line}\n"
            "  文件     1 个：src/paths.py\n  构建     x.whl, x.tar.gz\n"
            "  PyPI     https://pypi.org/project/agt-agent/0.35.18/\n"
            "  推送     origin main (aaaaaaa..abc1234)\n"
            "==============================\n"
            "提示：pip 索引生效有 ~20s 延迟；pip install -U agt-agent 后重启生效。\n")


def _install(monkeypatch, calls, rc, out):
    def fake_run(cmd, **kw):
        calls.append(list(cmd))
        if list(cmd[:2]) == ["git", "status"]:
            return _R(0, "", "")
        if len(cmd) > 1 and cmd[1] == "release.py":
            return _R(rc, out, "")
        return _R(0, "", "")
    monkeypatch.setattr(subprocess, "run", fake_run)


class _Resp:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return json.dumps({"urls": [{"filename": "agt_agent-0.35.18-py3-none-any.whl"}]}).encode()


def test_commit_message_with_fail_word_is_not_misjudged(monkeypatch):
    """rc=0 + 说明里含「失败」→ 必须判成功并继续后续三步（v0.35.17 被这个关键字打回）。"""
    calls = []
    msg = "feat: vector_hub 共享服务（空闲自关/失败回退本地）；fix: 修 X 失败时重试"
    _install(monkeypatch, calls, 0, _report(msg))
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: _Resp())
    res = release_tools.release_publish(message=msg)
    assert res.startswith("✅ 发布完成"), res
    assert "✅ tag v0.35.18 已打并推送" in res
    assert "✅ PyPI 已上线 0.35.18" in res
    assert any(c[:2] == ["git", "tag"] and c[2] == "v0.35.18" for c in calls), "应继续打 tag"
    assert any(c[:2] == ["git", "commit"] and "version_file" in " ".join(c) for c in calls), "应继续同步 version_file"


def test_real_failure_still_stops(monkeypatch):
    """rc≠0 → 报失败且不执行任何后续步骤。"""
    calls = []
    _install(monkeypatch, calls, 1, "📦 版本：0.35.17 → 0.35.18\n❌ git push 失败：\nfatal: unable to access")
    res = release_tools.release_publish()
    assert res.startswith("❌ release.py 失败（后续步骤未执行）")
    assert not any(c[:2] == ["git", "tag"] for c in calls), "真失败时不得继续打 tag"
    assert not any(c[:2] == ["git", "commit"] for c in calls)


def test_clean_message_success_path_unchanged(monkeypatch):
    calls = []
    _install(monkeypatch, calls, 0, _report("feat: something"))
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: _Resp())
    assert release_tools.release_publish(message="feat: something").startswith("✅ 发布完成")
