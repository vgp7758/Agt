"""release_tools.py —— agt-agent 一键发布工具（本 repo 自用）。

把发版的外围三步（工作区检查 / version_file 同步提交 / PyPI 上线验证）与
release.py 主体（bump + build + upload + push）串成一个工具调用。
协议即 tools/builtin/README.md 的标准形态：函数（签名+docstring）+ 描述符 {"func", ...}。
"""


def release_publish(part: str = "patch", message: str = "", dry_run: bool = False) -> str:
    """一键发布 agt-agent 到 PyPI + Git：工作区检查 → release.py（版本 bump/build/upload/push）
    → version_file.txt 同步提交 → PyPI 版本端点验证（三连重试）。
    part：patch（默认）/ minor / major 或具体版本号（如 0.35.0）；
    message：发布 commit 信息（缺省自动生成）；dry_run=True 只预览+build 不上传。
    任一步失败即中断并如实报告（upload 失败时不再做后续步骤）。"""
    import subprocess, sys, time, json, urllib.request, re
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent.parent      # tools/builtin/ → repo 根

    def _sh(cmd: list) -> tuple:
        r = subprocess.run(cmd, capture_output=True, text=True, cwd=str(root),
                           encoding="utf-8", errors="replace")
        return r.returncode, (r.stdout or "") + (r.stderr or "")

    # ① 工作区检查（脏则停——release.py 会 bump src/paths.py，混合未提交改动发出去不可控）
    rc, out = _sh(["git", "status", "--short"])
    dirty = [l for l in out.splitlines() if l.strip()]
    if dirty and not dry_run:
        return ("❌ 工作区有未提交改动，先提交再发版：\n  " + "\n  ".join(dirty[:10]))

    # ② release.py 主体
    cmd = [sys.executable, "release.py", part]
    if message:
        cmd += ["-m", message]
    cmd += (["--dry-run"] if dry_run else ["-y"])
    rc, out = _sh(cmd)
    tail = out.strip().splitlines()[-8:]
    if rc != 0 or ("❌" in out or "失败" in out):
        return "❌ release.py 失败（后续步骤未执行）：\n" + "\n".join(tail)

    # 版本号（"📦 版本：0.34.6 → 0.34.7" 的右侧）
    m = re.search(r"版本[：:]\s*[\d.]+\s*→\s*([\d.]+)", out)
    ver = m.group(1) if m else ""

    if dry_run:
        return ("✅ dry-run 完成（未 commit/upload/push）：\n" + "\n".join(tail))

    # ③ version_file.txt 同步提交（桌面版 CI 版本源）
    rc2, out2 = _sh(["git", "add", "packaging/version_file.txt"])
    rc3, out3 = _sh(["git", "commit", "-m", f"chore: 桌面版 version_file.txt 同步 {ver}"])
    rc4, out4 = _sh(["git", "push"])
    vfile_note = ("✅ version_file 同步提交并推送" if rc3 == 0 and rc4 == 0
                  else f"⚠️ version_file 提交/推送异常（rc={rc3}/{rc4}）：{ (out3 + out4).strip()[:200]}")

    # ③.5 打 tag 并推（2026-10-16 收口：桌面版 CI 靠 tag 触发，此前 release.py 只推 main、
    # tag 一直靠手补——v0.35.2 断网漏推、v0.35.3 手工补打。指向 version_file 同步后的最终
    # commit；tag 已存在（重跑场景）rc!=0 时仍尝试推，推送失败不中断发布（PyPI 已上线）。）
    tag = f"v{ver}"
    if ver:
        _sh(["git", "tag", tag])                       # 已存在则 rc!=0，无害
        tag_rc, tag_out = _sh(["git", "push", "origin", tag])
        tag_note = (f"✅ tag {tag} 已打并推送（桌面版 CI 将触发）" if tag_rc == 0
                    else f"⚠️ tag {tag} 推送失败（补推：git push origin {tag}）：{tag_out.strip()[:160]}")
    else:
        tag_note = "⏭ tag 跳过（版本号未解析到）"

    # ④ PyPI 上线验证（索引生效有 ~20s 延迟，三连重试）
    vnote = "⏭ 跳过（版本号未解析到）"
    if ver:
        for i in range(3):
            try:
                with urllib.request.urlopen(f"https://pypi.org/pypi/agt-agent/{ver}/json", timeout=30) as f:
                    files = [u["filename"] for u in json.load(f).get("urls", [])]
                vnote = f"✅ PyPI 已上线 {ver}：{len(files)} 件（{', '.join(files[:2])}）"
                break
            except Exception as e:
                if i == 2:
                    vnote = f"⚠️ PyPI 验证未通过（{type(e).__name__}——索引可能仍在同步，稍后手动确认 {ver}）"
                else:
                    time.sleep(20)

    return ("✅ 发布完成\n" + "\n".join(tail) + "\n" + vfile_note + "\n" + tag_note + "\n" + vnote)


def agt_register():
    return [
        {"name": "release_publish", "func": release_publish, "group": "release",
         "params": {"part": "patch（默认）/minor/major 或具体版本号",
                    "message": "发布 commit 信息（缺省自动生成）",
                    "dry_run": "True=只预览+build，不上传"}},
    ]
