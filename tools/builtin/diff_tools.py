"""diff_tools.py —— 文本块 Myers Diff（外置件）。

diff_lines：比较两个【文本块】（工作流编排用：两个节点的文本输出 / 两段代码 /
改前改后草稿），无需落盘。算法三件套与框架 real_tools.diff_files 的实现同源
（复制实现而非 import 框架——外置件零框架依赖的约定；Myers 是稳定经典算法，
双份各自带回归即可，改动时两处同步）。
改完本文件用 /reload tools 热加载（不需要重启）。
"""
from __future__ import annotations


def _myers_diff(a_lines, b_lines):
    """Myers Diff 算法。输入两个字符串列表（按行）。
    输出差异列表 [(action, line)]：'-' 删除(A侧行) / '+' 插入(B侧行) / ' ' 相同。"""
    A, B = a_lines, b_lines
    N, M = len(A), len(B)
    MAX = N + M
    V = {1: 0}
    trace = []
    for D in range(MAX + 1):
        trace.append(dict(V))          # trace[d] = d 轮开始前（=d-1 轮结束后）的 V 快照
        for k in range(-D, D + 1, 2):
            if k == -D or (k != D and V.get(k - 1, 0) < V.get(k + 1, 0)):
                x = V.get(k + 1, 0)      # 向下（插入）
            else:
                x = V.get(k - 1, 0) + 1  # 向右（删除）
            y = x - k
            while x < N and y < M and A[x] == B[y]:
                x += 1
                y += 1
            V[k] = x
            if x >= N and y >= M:
                return _myers_backtrack(trace, A, B, D)
    return []


def _myers_backtrack(trace, A, B, D):
    """回溯生成 diff。⚠️ prev_k 判断与 prev_x 取值必须用【同一层】快照 trace[d]（=d-1 轮
    结束状态，即第 d 步编辑的出发点）——取 trace[d-1] 会错一层，回溯偏离合法编辑链。"""
    x, y = len(A), len(B)
    result = []
    for d in range(D, 0, -1):
        V = trace[d]
        k = x - y
        if k == -d or (k != d and V.get(k - 1, 0) < V.get(k + 1, 0)):
            prev_k = k + 1
        else:
            prev_k = k - 1
        prev_x = V.get(prev_k, 0)      # ← 同一层（trace[d]）取，与 prev_k 判断一致
        prev_y = prev_x - prev_k
        while x > prev_x and y > prev_y:   # 对角线（相同段）从后往前收集
            x -= 1
            y -= 1
            result.append((' ', A[x]))
        if x > prev_x:                    # 水平步 = 删除 A 侧
            x -= 1
            result.append(('-', A[x]))
        elif y > prev_y:                  # 垂直步 = 插入 B 侧
            y -= 1
            result.append(('+', B[y]))
    while x > 0 and y > 0:                # d=0 纯对角线（起点前导相同段）
        x -= 1
        y -= 1
        result.append((' ', A[x]))
    result.reverse()
    return result


def _render_unified_diff(A, B, ops, label1, label2, context):
    """Myers ops → unified diff 文本（@@ hunk + 带行号 -/+ 行）。"""
    dels = sum(1 for a, _ in ops if a == '-')
    adds = sum(1 for a, _ in ops if a == '+')
    if not dels and not adds:
        return f"[无差异] {label1} 与 {label2} 内容相同（{len(A)} 行）"
    annot, i1, i2 = [], 0, 0
    for act, ln in ops:
        if act in (' ', '-'):
            i1 += 1
        if act in (' ', '+'):
            i2 += 1
        annot.append((act, ln, i1 if act in (' ', '-') else None,
                      i2 if act in (' ', '+') else None))
    ctx = max(0, int(context))
    changed_idx = [i for i, (a, *_r) in enumerate(annot) if a != ' ']
    hunks, s = [], 0
    for j in range(1, len(changed_idx) + 1):
        if j == len(changed_idx) or changed_idx[j] - changed_idx[j - 1] > 2 * ctx + 1:
            lo = max(0, changed_idx[s] - ctx)
            hi = min(len(annot) - 1, changed_idx[j - 1] + ctx)
            hunks.append((lo, hi))
            s = j
    parts = [f"[diff {label1} ({len(A)}行) vs {label2} ({len(B)}行) | -{dels} +{adds} | {len(hunks)} 处差异]"]
    for lo, hi in hunks:
        seg = annot[lo:hi + 1]
        a_start = next((a2 for _a, _l, a2, _b in seg if a2 is not None), 0)
        b_start = next((b2 for _a, _l, _a2, b2 in seg if b2 is not None), 0)
        a_n = sum(1 for _a, _l, a2, _b in seg if a2 is not None)
        b_n = sum(1 for _a, _l, _a2, b2 in seg if b2 is not None)
        parts.append(f"@@ -{a_start},{a_n} +{b_start},{b_n} @@")
        for act, ln, a2, b2 in seg:
            if act == ' ':
                parts.append(f"  {ln}")
            elif act == '-':
                parts.append(f"-{a2}│ {ln}")
            else:
                parts.append(f"+{b2}│ {ln}")
    out = "\n".join(parts)
    if len(out) > 20000:
        out = out[:20000] + f"\n...（输出截断，全量差异 -{dels} +{adds} 行；可减小 context）"
    return out


def diff_lines(a_text: str, b_text: str, context: int = 2) -> str:
    """Myers Diff 对比两个【文本块】（按行），输出 unified diff 风格差异（@@ hunk + -/+ 行）。
    工作流编排用：比较两个节点的文本输出 / 两段代码 / 改前改后草稿，无需落盘。"""
    A = (a_text or "").splitlines()
    B = (b_text or "").splitlines()
    return _render_unified_diff(A, B, _myers_diff(A, B), "a_text", "b_text", context)


def agt_register():
    return [
        {"name": "diff_lines", "func": diff_lines, "hidden": True, "group": "light",
         "version": 1, "params": {
             "a_text": "改前文本（接上游节点输出）",
             "b_text": "改后文本",
             "context": "每个 hunk 前后的上下文行数（默认 2）",
         }},
    ]
