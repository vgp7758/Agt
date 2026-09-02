"""cache_tools.py — 缓存断点分析（外置件）。

分析两次连续 LLM 调用的投影 dump，找出缓存前缀在哪里断裂——
断点所在的段位（SYSTEM/折叠摘要/历史档位/当前轮步骤/tail）、
消息索引、字符位置、变化前后对比窗口。

用法：cache_breakpoint(turn=582, step=2)
  → 对比 t582_s2 的投影与它的上一次调用（t582_s1）的投影。
  step=0 时自动取上一轮最后一步。
"""

import json
import re
from pathlib import Path


# ==================== 定位 ====================

def _find_projections_dir(session_dir: str = "") -> Path:
    """找 projections 目录。session_dir 显式指定优先；否则全局扫最近活跃的。"""
    if session_dir:
        p = Path(session_dir)
        if p.is_dir() and p.name != "projections":
            p = p / "projections"
        if p.is_dir():
            return p
    root = Path.home() / ".agt" / "repos"
    best_mtime, best = 0, None
    for repo in root.iterdir():
        if not repo.is_dir():
            continue
        sess = repo / "sessions"
        if not sess.is_dir():
            continue
        for s in sess.iterdir():
            pdir = s / "projections"
            if not pdir.is_dir():
                continue
            try:
                mt = max(f.stat().st_mtime for f in pdir.iterdir()
                         if f.name.endswith(".json") and not f.name.endswith(".meta"))
            except (ValueError, OSError):
                continue
            if mt > best_mtime:
                best_mtime, best = mt, pdir
    return best


def _list_projections(pdir: Path) -> list:
    """列出所有投影 [(turn, step, path), ...]，按 (turn, step) 排序。"""
    out = []
    for f in pdir.iterdir():
        m = re.match(r"t(\d+)_s(\d+)_\d+\.json$", f.name)
        if m:
            out.append((int(m.group(1)), int(m.group(2)), f))
    out.sort(key=lambda x: (x[0], x[1]))
    return out


# ==================== 加载与比较 ====================

def _load_msgs(path: Path) -> list:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return data
        return data.get("messages", [])
    except Exception:
        return []


def _msg_sig(m) -> str:
    return json.dumps(m, ensure_ascii=False, sort_keys=True)


def _content_str(m) -> str:
    c = m.get("content")
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        return "\n".join(str(b.get("text", "")) for b in c if isinstance(b, dict))
    return str(c or "")


def _char_diff(a: str, b: str, radius: int = 60):
    """第一个不同字符的位置 + 两侧窗口。完全一致返回 (-1, '', '')。"""
    for j in range(min(len(a), len(b))):
        if a[j] != b[j]:
            lo = max(0, j - radius)
            return j, a[lo:j + radius], b[lo:j + radius]
    if len(a) != len(b):
        short, long_ = (a, b) if len(a) < len(b) else (b, a)
        pos = len(short)
        lo = max(0, pos - radius // 2)
        return pos, short[lo:lo + radius] or "(末尾)", long_[pos:pos + radius] or "(末尾)"
    return -1, "", ""


# ==================== 段位识别 ====================

def _find_cur_user_idx(msgs: list) -> int:
    """从后往前找当前轮的 user 消息（不带 system-reminder 标签的最后一条裸 user）。"""
    for i in range(len(msgs) - 1, -1, -1):
        m = msgs[i]
        if m.get("role") != "user":
            continue
        c = _content_str(m)
        if "<system-reminder" in c[:50] or c.startswith("<retrieval-hint"):
            continue
        if i > 0 and msgs[i - 1].get("role") == "assistant" and "tool_calls" in msgs[i - 1]:
            # 可能是轮内的用户中途补充——继续往前找真正的轮首 user
            continue
        # 启发：轮首 user 之前要么是 system（tail），要么是 assistant（上轮 answer）
        if i == 0 or msgs[i - 1].get("role") in ("system", "assistant"):
            return i
        continue
    return -1


def _identify_zone(idx: int, msg: dict, msgs: list) -> tuple:
    """根据消息位置/角色/内容判断所属段。返回 (段名, 补充说明)。"""
    role = msg.get("role", "?")
    content = _content_str(msg)

    # ── 内容特征 ──
    if "折叠摘要" in content[:30] or "已折叠" in content[:30] or "结构摘要" in content[:30]:
        return "折叠摘要区", ""
    if role == "system" and idx < 3:
        return "SYSTEM/人设区", ""

    # ── 当前轮 user 位置 ──
    cur_user = _find_cur_user_idx(msgs)

    if cur_user >= 0 and idx >= cur_user:
        if idx == cur_user:
            return "当前轮 user_message", ""
        # 当前轮步骤区：按 assistant/tool 交替估步号
        step_est = 0
        for i in range(cur_user + 1, idx + 1):
            if msgs[i].get("role") == "assistant" and msgs[i].get("tool_calls"):
                step_est += 1
        if role == "tool":
            return f"当前轮步骤·第 {step_est - 1} 步的 tool 结果" if step_est > 0 else "当前轮步骤·tool"
        if role == "assistant" and msg.get("tool_calls"):
            return f"当前轮步骤·第 {step_est} 步的 assistant(tool_calls)", ""
        if role == "assistant":
            return "当前轮·assistant 回答", ""
        if role == "user":
            return "当前轮·用户中途补充", ""
        return "当前轮区域", ""

    # ── 历史区（当前轮 user 之前）──
    if role == "user" and idx > 0:
        # 历史轮的 user 消息——提取轮号（如果投影里带标记）
        m = re.search(r"第(\d+)轮", content[:30])
        if m:
            return f"历史区·第 {m.group(1)} 轮 user", ""
    if "个早期步骤已省略" in content:
        return "历史档位区·步骤省略提示", ""
    if role == "system" and ("档" in content[:20] or "历史" in content[:20]):
        return "历史档位区·system 标注", ""
    # 默认
    tier_guess = "档位历史区"
    if cur_user >= 0 and idx < cur_user:
        # 用粗略比例估计是哪个档
        ratio = idx / max(cur_user, 1)
        if ratio > 0.7:
            tier_guess = "档位历史区（近端·大概率档1）"
        elif ratio > 0.3:
            tier_guess = "档位历史区（中段·档2-3）"
        else:
            tier_guess = "档位历史区（远端·深档）"
    return tier_guess, f"role={role}"


# ==================== 主工具 ====================

def cache_breakpoint(turn: int, step: int, session_dir: str = "", context_chars: int = 120) -> str:
    """分析两次连续 LLM 调用的缓存断点位置。

    turn/step: 目标调用（与它的上一次调用对比）。如 turn=582, step=2 → 对比 t582_s2 vs t582_s1。
    step=0 时上一次自动取上一轮最后一步。
    session_dir: 存档目录（留空=自动找最近活跃的 session）。
    返回: 断点所在的消息索引/段位（SYSTEM/折叠摘要/档位历史/当前轮步骤）/变化前后对比窗口。
    """
    pdir = _find_projections_dir(session_dir)
    if not pdir:
        return "[错误] 找不到 projections 目录（确认 session 存档有投影转储）"

    entries = _list_projections(pdir)
    if not entries:
        return "[错误] projections 目录为空"

    # 找目标
    target = None
    for i, (t, s, _) in enumerate(entries):
        if t == turn and s == step:
            target = i
            break
    if target is None:
        avail = f"t{entries[0][0]}_s{entries[0][1]} ~ t{entries[-1][0]}_s{entries[-1][1]}"
        return f"[错误] 找不到 t{turn}_s{step}。可用范围：{avail}"
    if target == 0:
        return f"[错误] t{turn}_s{step} 是最早的投影，没有上一次可对比"

    prev_t, prev_s, prev_path = entries[target - 1]
    cur_t, cur_s, cur_path = entries[target]

    A = _load_msgs(prev_path)
    B = _load_msgs(cur_path)

    # 找第一个不同的消息
    break_idx = -1
    cached_chars = 0
    for i in range(min(len(A), len(B))):
        if _msg_sig(A[i]) != _msg_sig(B[i]):
            break_idx = i
            break
        try:
            cached_chars += len(_msg_sig(A[i]))
        except Exception:
            pass

    parts = [f"📊 缓存断点：t{prev_t}_s{prev_s} → t{cur_t}_s{cur_s}", "=" * 56, ""]

    if break_idx == -1:
        if len(A) == len(B):
            parts.append(f"✅ 完全一致（{len(A)} 条消息，{cached_chars:,} 字符），无缓存断点")
            return "\n".join(parts)
        break_idx = min(len(A), len(B))

    if break_idx == 0:
        parts.append(f"缓存命中区：❌ 无（断点在第 0 条消息——SYSTEM 区就变了）")
    else:
        parts.append(f"缓存命中区：messages[0..{break_idx - 1}]（{break_idx} 条，~{cached_chars:,} 字符）✓")
    parts.append("")

    if break_idx >= len(A):
        msg = B[break_idx]
        zone, note = _identify_zone(break_idx, msg, B)
        parts.append(f"🔴 断点：messages[{break_idx}] 为【新增】")
        parts.append(f"  段位：{zone}" + (f" ({note})" if note else ""))
        parts.append(f"  role={msg.get('role')} | content 前 {context_chars} 字：")
        parts.append(f"    {_content_str(msg)[:context_chars]}")
    elif break_idx >= len(B):
        msg = A[break_idx]
        zone, note = _identify_zone(break_idx, msg, A)
        parts.append(f"🔴 断点：messages[{break_idx}] 在本次中【被移除】")
        parts.append(f"  段位：{zone}" + (f" ({note})" if note else ""))
        parts.append(f"  role={msg.get('role')} | content 前 {context_chars} 字：")
        parts.append(f"    {_content_str(msg)[:context_chars]}")
    else:
        a_msg, b_msg = A[break_idx], B[break_idx]
        zone, note = _identify_zone(break_idx, b_msg, B)
        parts.append(f"🔴 断点：messages[{break_idx}] 内容【变化】")
        parts.append(f"  段位：{zone}" + (f" ({note})" if note else ""))
        if a_msg.get("role") != b_msg.get("role"):
            parts.append(f"  role：{a_msg.get('role')} → {b_msg.get('role')}")

        ac, bc = _content_str(a_msg), _content_str(b_msg)
        pos, wa, wb = _char_diff(ac, bc, radius=max(30, context_chars // 2))

        if pos >= 0:
            parts.append(f"  字符位置：第 {pos:,} 字符起（消息总长 {len(ac):,} → {len(bc):,}）")
            parts.append(f"  ── 之前（t{prev_t}_s{prev_s}）──")
            for ln in wa[:context_chars * 2].splitlines()[:6]:
                parts.append(f"    │{ln}")
            parts.append(f"  ── 之后（t{cur_t}_s{cur_s}）──")
            for ln in wb[:context_chars * 2].splitlines()[:6]:
                parts.append(f"    │{ln}")
        else:
            a_tc = json.dumps(a_msg.get("tool_calls") or [], ensure_ascii=False)[:80]
            b_tc = json.dumps(b_msg.get("tool_calls") or [], ensure_ascii=False)[:80]
            if a_tc != b_tc:
                parts.append(f"  tool_calls 变化：")
                parts.append(f"    旧：{a_tc}")
                parts.append(f"    新：{b_tc}")
            else:
                diff_keys = set(a_msg.keys()) ^ set(b_msg.keys())
                if not diff_keys:
                    same_keys = set(a_msg.keys()) & set(b_msg.keys())
                    for k in same_keys:
                        if a_msg[k] != b_msg[k]:
                            parts.append(f"  字段 '{k}' 变化")
                else:
                    parts.append(f"  键差异：{diff_keys}")

    # 尾部统计
    total_a = sum(len(_content_str(m)) for m in A)
    total_b = sum(len(_content_str(m)) for m in B)
    parts.append("")
    parts.append(f"消息数：{len(A)} → {len(B)}（{len(B) - len(A):+d}） | "
                 f"总字符：{total_a:,} → {total_b:,}（{total_b - total_a:+,}）")

    return "\n".join(parts)


def agt_register():
    return [
        {"name": "cache_breakpoint", "func": cache_breakpoint, "group": "cache", "version": 1,
         "outputs": [{"name": "raw", "type": "string",
                      "description": "缓存断点分析报告（段位/消息索引/字符位置/变化对比窗口）"}]},
    ]
