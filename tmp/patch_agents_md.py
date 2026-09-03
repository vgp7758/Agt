# -*- coding: utf-8 -*-
"""给 VideoGameTeam 全员 AGENTS.md 补 gdd Junction 说明 + 修 gdd/README.md 过时版本约定"""
import io, os, sys

BASE = r"D:\Projects\VideoGameTeam"
MEMBERS = ["director", "screenwriter", "scriptwriter", "programmer"]

# 统一的总说明 bullet（按成员微调草稿目录）
SEG_JUNCTION = (
    "- 共享 GDD 库 `gdd/`：团队唯一事实源（真实位置 D:/Projects/VideoGameTeam/gdd/，是 CNB 上的 git 仓库）"
    "——你目录下的 `gdd/` 是指向它的 Junction，全团队读写同一份文件。"
    "一律用相对路径 `gdd/...` 读写（在你 workspace 权限内，read_file/edit/write_file 直接可用，不要用 ../ 或绝对路径）\n"
    "- ⚠️ 写 `gdd/` = 写共享库 = 会进入 git 仓库：只放正式产出（{draft_hint}），"
    "不要放临时草稿/中间产物/大文件；变更提交用 `run_shell(\"git -C gdd add -A && git -C gdd commit -m \\\"...\\\"\")`，push 到 CNB 由导演统一"
)

DRAFT_HINT = {
    "director":     "登记表/milestones 直接改",
    "screenwriter": "草稿放本地 draft/",
    "scriptwriter": "分镜表一场一文件",
    "programmer":   "临时文件放本地 tmp/",
}

def patch(path, repls, insert_after, insert_line):
    with io.open(path, "r", encoding="utf-8") as f:
        txt = f.read()
    orig = txt
    for old, new in repls:
        if old in txt:
            txt = txt.replace(old, new)
        else:
            print(f"  [skip 未命中] {old[:60]!r}")
    if insert_after and insert_after in txt and insert_line not in txt:
        txt = txt.replace(insert_after, insert_after + "\n" + insert_line)
    if txt != orig:
        with io.open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(txt)
        return True
    return False

ok = []
for m in MEMBERS:
    p = os.path.join(BASE, m, "AGENTS.md")
    seg = SEG_JUNCTION.format(draft_hint=DRAFT_HINT[m])
    # 各文件原有 gdd 条目改为指向 junction 说明；统一 ../gdd/ -> gdd/
    if m == "director":
        repls = [(
            "- 共享 GDD 库：D:/Projects/VideoGameTeam/gdd/（读剧本/分镜，维护 assets-registry.md 与 milestones.md）",
            seg,
        )]
    elif m == "screenwriter":
        repls = [(
            "- 产出统一写入 D:/Projects/VideoGameTeam/gdd/：worldbuilding/（世界观）、characters/（角色）、chapters/（章节剧本）",
            seg + "\n- 产出目录：worldbuilding/（世界观）、characters/（角色）、chapters/（章节剧本）",
        )]
    elif m == "scriptwriter":
        repls = [(
            "- 输入：gdd/chapters/（剧本）+ 导演的分镜要求",
            seg + "\n- 输入：gdd/chapters/（剧本）+ 导演的分镜要求",
        )]
    else:  # programmer
        repls = [(
            "- 数据源：gdd/storyboards/（分镜结构）、gdd/assets-registry.md（资产 URL）、gdd/tech-stack.md（你维护的技术决策记录）",
            seg + "\n- 数据源：gdd/storyboards/（分镜结构）、gdd/assets-registry.md（资产 URL）、gdd/tech-stack.md（你维护的技术决策记录）",
        )]
    changed = patch(p, repls + [("../gdd/", "gdd/")], None, None)
    ok.append((m, changed))
    print(f"{m}: {'PATCHED' if changed else 'no-change'}")

# gdd/README.md 版本约定段更新（原文是"每个 repo 建议 git init"——已过时）
readme = os.path.join(BASE, "gdd", "README.md")
old_ver = (
    "## 版本约定\n"
    "- 每个 repo 建议 `git init`（当前未初始化）；GDD 变更由写者负责提交语义化信息\n"
    "- 大改设定前在对应文件顶部加「变更说明」段，避免读者混淆"
)
new_ver = (
    "## 版本约定\n"
    "- 本目录是一个统一的 git 仓库（remote 在 CNB）；各成员目录下的 `gdd/` 均是指向本目录的 Junction——同一份文件，没有副本\n"
    "- 变更由写者负责提交语义化信息：`git -C gdd add -A && git -C gdd commit -m \"...\"`（在成员自己 workspace 内即可执行）；push 到 CNB 由导演统一\n"
    "- 大改设定前在对应文件顶部加「变更说明」段，避免读者混淆"
)
changed = patch(readme, [(old_ver, new_ver)], None, None)
print(f"gdd/README.md: {'PATCHED' if changed else 'no-change'}")
print("ALL DONE", ok)
