"""agent_watchdog.py —— Agent 卡住看门狗·巡检 daemon（p_aa314b2f 2026-11）。

分工语义（与 agent.py 工具硬超时配套）：
- 工具层超时（_TOOL_STUCK_SECS，agent.py）先触发 → 宿主注入卡死结果自愈继续跑，心跳恢复，巡检不触发；
- 本巡检兜的是【非工具卡点】：锁死锁 / 投影锁 / LLM 网络挂起等——_react_running=True 且心跳静默
  超过 stuck_secs 才触发。触发动作：py-spy dump 本进程存档 → 栈 + 近期事件拼诊断材料 →
  agent_prompt 派 doctor 诊断（caller='user' 防 doctor 回报唤醒烧轮）→ 日志 + llm_log 事件通知。
  第一版【不自动重启】（数据风险留给用户裁定，doctor 回报里给建议命令）。

装配：chat.py web_main / cli 两入口 agent 建好后各一行 `start_watchdog(agent, registry)`。
daemon=True：进程退出自然亡，无清理需求。
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import threading
import time

import config

_LOG = logging.getLogger("agt.watchdog")

_SCAN_INTERVAL = 120       # 巡检周期（秒）：每 2min 扫一次 registry
_COOLDOWN_SECS = 1800      # 同 agent_id 触发冷却（30min 防重入）
_PYSPY_TIMEOUT = 30        # py-spy dump 单次超时
_EVENTS_TAIL = 10          # 诊断材料附带的近期事件条数

_started = False           # 模块级防重复装配（web_main/cli 只会走其一，防御性兜底）


def _read_recent_events(path, n: int = _EVENTS_TAIL) -> list[str]:
    """读 events.jsonl 尾 n 条的摘要行（agent_query_events 同款视角：事件类型 + 主要字段截断）。"""
    if not path:
        return []
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.readlines()
    except Exception:
        return []
    out = []
    for ln in lines[-max(1, n):]:
        try:
            ev = json.loads(ln)
        except Exception:
            continue
        et = ev.get("type") or ev.get("event") or "?"
        detail = ev.get("text") or ev.get("user") or ev.get("name") or ""
        detail = str(detail).replace("\n", " ")[:120]
        out.append(f"  - {et}: {detail}" if detail else f"  - {et}")
    return out


def _dump_stack(out_path) -> str:
    """py-spy dump 本进程全线程栈 → 写 out_path，返回内容（失败把错误写进文件仍返回，doctor 有料可读）。"""
    py_spy = shutil.which("py-spy")
    if py_spy is None:
        text = (f"[watchdog] py-spy 不在 PATH，无法 dump 栈。"
                f"（pip install py-spy 后可用；本文件仅为占位存档）")
        try:
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(text + "\n", encoding="utf-8")
        except Exception:
            pass
        return text
    try:
        r = subprocess.run([py_spy, "dump", "--pid", str(os.getpid())],
                           capture_output=True, text=True, timeout=_PYSPY_TIMEOUT)
        text = (r.stdout or "") + (f"\n[stderr] {r.stderr}" if r.stderr else "")
        if r.returncode != 0 and not text.strip():
            text = f"[watchdog] py-spy dump 退出码 {r.returncode}（无输出）"
    except Exception as e:
        text = f"[watchdog] py-spy dump 失败: {type(e).__name__}: {e}"
    try:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(text + "\n", encoding="utf-8")
    except Exception:
        pass
    return text


def stuck_candidates(entries, now: float, stuck_secs: float, cooldown: dict) -> list:
    """判定候选：_react_running=True 且 status=='running' 且心跳静默 > stuck_secs 且过冷却。
    纯函数（不碰 IO）——单测直接喂伪造 entries 验证四场景与冷却。"""
    out = []
    for e in entries:
        ag = getattr(e, "agent", None)
        if ag is None:
            continue
        if getattr(e, "status", "") != "running":
            continue
        if not getattr(ag, "_react_running", False):
            continue   # 不在 react 循环里（idle/wait 状态静默是正常语义，不判卡）
        hb = getattr(ag, "_hb_ts", None)
        if hb is None:
            continue   # 没心跳的实例（老代码构造）不判——宁可漏报不误报
        silent = now - hb
        if silent <= stuck_secs:
            continue   # 心跳新鲜（工具超时自愈后即恢复于此）
        last = cooldown.get(e.agent_id, 0.0)
        if now - last < _COOLDOWN_SECS:
            continue   # 冷却期内不重入
        out.append((e, silent))
    return out


def probe_stuck_agent(agent, entry, silent: float, stuck_secs: float) -> str:
    """对一个疑似卡死的 Agent 执行触发动作：py-spy dump 存档 → 拼诊断材料 → 派 doctor。
    返回派发结果文本（全 catch 异常，不炸巡检线程）。"""
    aid = entry.agent_id
    name = entry.name
    # ① py-spy dump 存档：<session_dir>/agents/<aid>/stuck_stack.log
    stack_text = ""
    stack_path = None
    try:
        sdir = getattr(getattr(entry.agent, "session", None), "session_dir", None)
        if sdir is not None:
            from pathlib import Path
            stack_path = Path(sdir) / "agents" / aid / "stuck_stack.log"
            stack_text = _dump_stack(stack_path)
    except Exception as e:
        _LOG.warning("watchdog: py-spy dump 存档失败 (%s): %s", aid, e)
    # ② 近期事件摘要（该 Agent 自己的 events.jsonl 尾 10 条）
    try:
        ev_path = getattr(getattr(entry.agent, "session", None), "_event_path", None)
        ev_lines = _read_recent_events(ev_path)
    except Exception:
        ev_lines = []
    # ③ 诊断材料塞 prompt 全文派 doctor（caller='user'：fire-and-forget，不唤醒烧轮）
    task = (entry.task or "(无任务摘要)")[:200]
    prompt_text = (
        f"【看门狗触发】Agent '{name}' [{aid}] 疑似卡死（非工具卡点兜底诊断）\n"
        f"- 状态：react 循环进行中（_react_running=True），心跳静默 {int(silent)}s"
        f"（阈值 {int(stuck_secs)}s）\n"
        f"- 当前任务：{task}\n"
        f"- 存档：{stack_path if stack_path else '(session_dir 未就绪，栈未存档)'}\n\n"
        f"—— 本进程 py-spy 全线程栈 dump ——\n{stack_text or '(无)'}\n\n"
        f"—— 该 Agent 近期事件（events.jsonl 尾 {len(ev_lines)} 条）——\n"
        + ("\n".join(ev_lines) if ev_lines else "  (无事件可读)")
        + "\n\n请按「卡住诊断」工作循环处理：栈分类（锁等待/GIL idle/网络 recv/工具内部）→ "
          "结论与建议（重启命令/等自愈/人工检查点）。只诊断与建议，不修引擎代码、不碰 session；"
          "若栈显示已自愈（工具超时结果已注入、心跳已恢复）则仅归档报告。"
    )
    dispatch = ""
    try:
        r = agent.tools.call("agent_prompt", {"name": "doctor", "prompt": prompt_text,
                                              "caller": "user"})
        dispatch = r if isinstance(r, str) else str(r)
    except Exception as e:
        dispatch = f"[watchdog] doctor 派发失败: {type(e).__name__}: {e}"
    # ④ 通知：log + llm_log 事件（WebUI 日志抽屉）
    msg = (f"[看门狗] Agent '{name}' [{aid}] 心跳静默 {int(silent)}s 判疑似卡死，"
           f"已 py-spy dump 存档并派 doctor 诊断（caller=user，不自动重启）")
    _LOG.warning("%s | doctor 派发: %s", msg, (dispatch or "")[:200])
    try:
        agent._emit({"type": "llm_log", "level": "warning", "text": msg, "scene": "watchdog"})
    except Exception:
        pass
    return dispatch


def check_once(agent, registry, cooldown: dict | None = None, now: float | None = None) -> list:
    """跑一轮巡检（线程循环体与单测共用）：扫 list_active → 判定 → 触发。返回触发的 agent_id 列表。"""
    cd = cooldown if cooldown is not None else _COOLDOWN
    t = time.time() if now is None else now
    stuck_secs = config.load_watchdog_stuck_secs()   # 每轮读盘：settings 热改即生效
    try:
        entries = registry.list_active()
    except Exception as e:
        _LOG.debug("watchdog: list_active 失败（跳过本轮）: %s", e)
        return []
    fired = []
    for e, silent in stuck_candidates(entries, t, stuck_secs, cd):
        cd[e.agent_id] = t   # 先记冷却再触发（派发中异常也不重入）
        fired.append(e.agent_id)
        try:
            probe_stuck_agent(agent, e, silent, stuck_secs)
        except Exception as exc:   # 双保险：触发动作任何异常不炸巡检循环
            _LOG.warning("watchdog: probe %s 异常（吞掉继续）: %s", e.agent_id, exc)
    return fired


# 模块级冷却表 + daemon 线程（每实例进程一份；daemon=True 进程退出自然亡）
_COOLDOWN: dict = {}
_THREAD: threading.Thread | None = None


def _loop(agent, registry):
    while True:
        try:
            check_once(agent, registry)
        except Exception as e:   # 巡检循环本身永不带崩进程
            _LOG.warning("watchdog: 巡检轮异常（继续）: %s", e)
        time.sleep(_SCAN_INTERVAL)


def start_watchdog(agent, registry) -> bool:
    """装配巡检 daemon（chat.py web_main / cli 两入口 agent 建好后各调一次）。已启动则幂等跳过。"""
    global _started, _THREAD
    if _started:
        return False
    # 防呆：必须是 AgentRegistry（有 list_active）——chat.py 里同名局部变量 registry 是
    # CommandRegistry（命令表），传错对象时巡检静默空转，这里显式拒绝并报错。
    if not hasattr(registry, "list_active"):
        _LOG.error("start_watchdog: registry 缺 list_active（疑似传成 CommandRegistry 命令表，"
                   "应传 agent.registry）——巡检不启动")
        return False
    _started = True
    _THREAD = threading.Thread(target=_loop, args=(agent, registry),
                               name="agent-watchdog", daemon=True)
    _THREAD.start()
    _LOG.info("看门狗巡检已装配：周期 %ds，卡死阈值 settings.watchdog_stuck_secs（默认 %ds），"
              "触发动作=py-spy dump + doctor 诊断（不自动重启）", _SCAN_INTERVAL,
              config.load_watchdog_stuck_secs())
    return True
