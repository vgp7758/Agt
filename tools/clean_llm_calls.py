#!/usr/bin/env python3
"""clean_llm_calls.py —— 给 llm_calls.jsonl 中缺 resp_model 的旧数据补标注（一次性清洗）。

判别规则（由带 resp_model 的新记录校准 + 用户经验）：
  非 proxy 直连（glm/qwen/deepseek/...）：resp_model = models.json[provider].model（一一对应）
  proxy 内部路由（按回包特征反推）：
    1. error 含 kimi/temperature            → kimi-k2.6
    2. usage 有 prompt_cache_hit_tokens 字段 → deepseek-chat（DeepSeek 原生字段，命中与否都会返回）
    3. prompt_tokens_details.cached_tokens>0 → glm-5.3（bigmodel OpenAI 兼容格式）
    4. 无缓存命中 + reasoning_len>=1000      → Qwen/Qwen3.5-397B-A17B（qwen reasoning 罗嗦）
    5. 无缓存命中 + reasoning_len<1000       → ZhipuAI/GLM-5.2（modelscope 免费主力）
  已有 resp_model 的记录不动。

用法：python tools/clean_llm_calls.py [jsonl路径]   （默认取当前 repo 最新 session）
      --dry-run 只看统计不写回。原文件备份为 *.bak
"""
from __future__ import annotations

import json
import shutil
import sys
import time
from pathlib import Path

# proxy 内部端点（providers_openai.json 的 model 字段）
KIMI = "kimi-k2.6"
DS_CHAT = "deepseek-chat"
GLM53 = "glm-5.3"
QWEN = "Qwen/Qwen3.5-397B-A17B"
GLM52 = "ZhipuAI/GLM-5.2"
QWEN_VERBOSE_RL = 1000   # qwen reasoning 罗嗦阈值（字符）


def load_models_map() -> dict:
    p = Path.home() / ".agt" / "models.json"
    try:
        return {k: v.get("model", "") for k, v in
                json.loads(p.read_text(encoding="utf-8")).get("models", {}).items()}
    except Exception as e:
        print(f"⚠️ 读 models.json 失败: {e}")
        return {}


def infer(r: dict, models_map: dict) -> str:
    m = r.get("model") or ""
    if m != "proxy":
        # 直连：provider → models.json 的 model 字段（含 completer 记录的 provider）
        return models_map.get(m, "")
    # proxy：按回包特征反推
    err = (r.get("error") or "")
    if "kimi" in err.lower() or "temperature" in err.lower():
        return KIMI
    u = r.get("usage") or {}
    if isinstance(u.get("prompt_cache_hit_tokens"), int):
        return DS_CHAT
    if (((u.get("prompt_tokens_details") or {}).get("cached_tokens")) or 0) > 0:
        return GLM53
    return QWEN if (r.get("reasoning_len") or 0) >= QWEN_VERBOSE_RL else GLM52


def main():
    dry = "--dry-run" in sys.argv
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if args:
        f = Path(args[0])
    else:
        base = Path.home() / ".agt" / "repos" / "D--AI_Usings-Agt" / "sessions"
        cands = sorted(base.glob("*/llm_calls.jsonl"), key=lambda p: p.stat().st_mtime)
        if not cands:
            print("未找到 llm_calls.jsonl"); return
        f = cands[-1]
    print(f"目标: {f}")

    lines = f.read_text(encoding="utf-8").splitlines()
    models_map = load_models_map()
    stats: dict = {"skip": 0}
    out = []
    for line in lines:
        if not line.strip():
            continue
        r = json.loads(line)
        if r.get("resp_model"):
            stats["skip"] += 1
        else:
            rm = infer(r, models_map)
            if rm:
                r["resp_model"] = rm
                stats[rm] = stats.get(rm, 0) + 1
            else:
                stats["unknown"] = stats.get("unknown", 0) + 1
        out.append(json.dumps(r, ensure_ascii=False))

    print(f"\n清洗统计（共 {len(out)} 条，已标注跳过 {stats['skip']} 条，本次补充 {len(out)-stats['skip']} 条）：")
    for k, v in sorted(stats.items(), key=lambda x: -x[1]):
        if k != "skip":
            print(f"  {k:28s} {v}")

    if dry:
        print("\n[dry-run] 未写回。")
        return
    bak = f.with_suffix(".jsonl.bak")
    shutil.copy2(f, bak)
    f.write_text("\n".join(out) + "\n", encoding="utf-8")
    print(f"\n✅ 已写回 {f}（备份: {bak.name}）")


if __name__ == "__main__":
    main()
