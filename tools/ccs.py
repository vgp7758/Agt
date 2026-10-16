#!/usr/bin/env python3
"""ccs.py — Claude Code stream-json 流式服务包装（REPL 协议，2026-10-16）

把 `claude -p --input-format stream-json --output-format stream-json` 长进程包成
可监控的 REPL 服务：stdin 收命令、stdout 吐【人/模型可读的事件进度行】。

消息流解析（Claude Code 的事件就这几类）：
  system(init) → [session] ...
  assistant.content[]:
    text       → [text] ...
    thinking   → [thinking] ...
    tool_use   → [tool_use] Read({"file_path":...})
  user.content[tool_result] → [tool_result] N chars
  result      → [result] 最终文本 | cost | 耗时 | 轮数      ← 一轮结束标记

命令（stdin，一行一条）：
  SEND <prompt>        派任务（非 / 开头的行 = 隐式 SEND）
  /status              REPL 协议状态查询（最近事件 + 是否在跑）
  /quit                退出

配置（环境变量，均有默认——与 cc.ps1 同款指向本地 proxy）：
  CCS_CLAUDE / CCS_BASE_URL / CCS_API_KEY / CCS_MODEL / CCS_CWD
"""
import json
import os
import subprocess
import sys
import threading
import time

# ---- 配置 ----
CLAUDE = os.environ.get("CCS_CLAUDE", r"C:\Users\vgp77\AppData\Roaming\npm\claude.cmd")
ENV = {**os.environ,
       "ANTHROPIC_BASE_URL": os.environ.get("CCS_BASE_URL", "http://127.0.0.1:9877/api/llm"),
       "ANTHROPIC_API_KEY": os.environ.get("CCS_API_KEY", "proxy-fallback"),
       "ANTHROPIC_DEFAULT_OPUS_MODEL": os.environ.get("CCS_MODEL", "glm-5.2")}
CWD = os.environ.get("CCS_CWD", os.getcwd())

TRUNC = 200          # 每块摘要截断长度
_recent: list[str] = []   # 最近事件行（/status 用，环形 40 条）
_busy = False
_session_id = ""
_rounds = 0


def emit(s: str = ""):
    """服务输出（flush 必须开——stdout 是管道，不 flush 下游永远看不到）。"""
    print(s, flush=True)
    if s:
        _recent.append(f"{time.strftime('%H:%M:%S')} {s}")
        del _recent[:-40]


def _short(v, n=120):
    try:
        s = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
    except Exception:
        s = str(v)
    s = " ".join(s.split())
    return s if len(s) <= n else s[:n] + "…"


def on_event(line: str):
    """claude stream-json 事件 → 紧凑进度行。"""
    global _session_id, _rounds
    try:
        ev = json.loads(line)
    except Exception:
        return
    t = ev.get("type")
    if t == "system":
        _session_id = ev.get("session_id", "")
        emit(f"[session] model={ev.get('model', '?')} tools={len(ev.get('tools', []) or [])} cwd={ev.get('cwd', '?')}")
    elif t == "assistant":
        for b in ev.get("message", {}).get("content", []) or []:
            bt = b.get("type")
            if bt == "text":
                emit(f"[text] {_short(b.get('text', ''), TRUNC)}")
            elif bt == "thinking":
                emit(f"[thinking] {_short(b.get('thinking', ''), TRUNC)}")
            elif bt == "tool_use":
                emit(f"[tool_use] {b.get('name', '?')}({_short(b.get('input', {}), 160)})")
    elif t == "user":
        for b in ev.get("message", {}).get("content", []) or []:
            if isinstance(b, dict) and b.get("type") == "tool_result":
                c = b.get("content")
                n = len(c) if isinstance(c, str) else len(json.dumps(c, ensure_ascii=False))
                err = " (error)" if b.get("is_error") else ""
                emit(f"[tool_result]{err} {n} chars")
    elif t == "result":
        _rounds += 1
        emit(f"[result] {_short(ev.get('result', ''), 500)}")
        emit(f"[done] rounds={_rounds} cost=${ev.get('total_cost_usd', 0)} "
             f"{ev.get('duration_ms', 0)}ms turns={ev.get('num_turns', '?')}")


def main():
    global _busy
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    proc = subprocess.Popen(
        [CLAUDE, "-p", "--input-format", "stream-json", "--output-format",
         "stream-json", "--verbose", "--dangerously-skip-permissions"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        env=ENV, text=True, encoding="utf-8", errors="replace", cwd=CWD)

    def pump():
        global _busy
        for line in proc.stdout:
            line = line.strip()
            if line:
                on_event(line)
        _busy = False
    threading.Thread(target=pump, daemon=True).start()
    emit(f"[ccs] 服务就绪 pid={proc.pid} cwd={CWD} model={ENV['ANTHROPIC_DEFAULT_OPUS_MODEL']}")

    for raw in sys.stdin:
        cmd = raw.strip()
        if not cmd:
            continue
        low = cmd.lower()
        if low in ("/quit", "/exit", "/stop"):
            emit("[ccs] 再见")
            proc.terminate()
            return
        if low == "/status":
            state = "RUNNING（任务执行中）" if _busy else "idle（等任务）"
            emit(f"[status] {state} session={_session_id[:16] or '-'} rounds={_rounds} 最近事件:")
            for r in _recent[-8:]:
                emit(f"  {r}")
            emit("")          # /status 应答结束标记
            continue
        prompt = cmd[5:].strip() if cmd.upper().startswith("SEND ") else cmd
        if not prompt:
            continue
        _busy = True
        try:
            proc.stdin.write(json.dumps({"type": "user", "message": {
                "role": "user", "content": prompt}}, ensure_ascii=False) + "\n")
            proc.stdin.flush()
            emit(f"[ccs] → 已派发（{len(prompt)} chars），等待完成标记…")
        except Exception as e:
            _busy = False
            emit(f"[ccs] 派发失败: {type(e).__name__}: {e}")


if __name__ == "__main__":
    main()
