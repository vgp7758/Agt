"""real_tools.py —— 真实强力工具（Step 6）。

把玩具计算器升级为能干实事的工具集：
  run_python              : 执行模型写的 Python（独立子进程 + 超时）。
  read_file/write_file/list_dir : 读写文件，限定在 workspace/ 内（控制爆炸半径）。
  web_search              : 联网搜索（DuckDuckGo，无需 key；国内可能需代理）。
  run_shell               : 执行系统命令（最强大也最危险，超时 + 日志）。

安全策略：
  - 代码/命令在独立子进程中执行，带超时，超时即终止，不会卡死 Agent。
  - 文件操作限定在 workspace/ 目录，越界拒绝，防误伤系统文件。
  - 任何工具出错都转成文本回传模型（不抛异常炸流程）。
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

from tools import Tool, Toolbox

# 工作区 = 启动时的当前目录(cwd)。文件读写、代码执行都以这里为根与沙箱边界。
# 故可从任意目录 `python /path/to/chat.py` 启动，在当前目录执行任务。
WORKSPACE = Path.cwd()

# 代码执行与 shell 的超时秒数（可通过 set_timeout 工具运行时调整）
TOOL_TIMEOUT = 10


def _resolve(path: str) -> Path:
    """把路径解析到 workspace 内；越界则抛 PermissionError（会被 Tool.run 转成文本）。"""
    base = WORKSPACE.resolve()
    target = (base / path).resolve()
    try:
        target.relative_to(base)  # 不在 base 下会抛 ValueError
    except ValueError:
        raise PermissionError(f"拒绝访问 workspace 外的路径: {path}")
    return target


def _py_check(target: Path) -> str:
    """对刚写入的 Python 文件做即时语法检查（compile，不解引用，不跑代码，零开销）。
    有语法错误则返回可操作的报错行（Agent 看到后可自行修正）。"""
    if target.suffix not in (".py", ".pyw"):
        return ""
    try:
        code = target.read_text(encoding="utf-8")
        compile(code, str(target), "exec")
    except SyntaxError as e:
        return f"\n⚠️ 语法错误 {e.filename or target.name}:{e.lineno}:{e.offset} — {e.msg}"
    return ""
def run_python(code: str) -> str:
    """运行一段 Python 代码，返回标准输出（出错时附 stderr）。独立子进程执行，超时 10 秒。"""
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False, encoding="utf-8") as f:
        f.write(code)
        tmp = f.name
    try:
        proc = subprocess.run(
            [sys.executable, tmp],
            capture_output=True, text=True, timeout=TOOL_TIMEOUT, cwd=str(WORKSPACE),        )
        out = proc.stdout
        if proc.returncode != 0 and proc.stderr:
            out += ("\n[stderr]\n" + proc.stderr) if out else proc.stderr
        return out.strip() or "(无输出)"
    except subprocess.TimeoutExpired:
        return f"[执行超时（>{TOOL_TIMEOUT}s），已终止]"
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def read_file(path: str) -> str:
    """读取 workspace 内某个文本文件的内容。"""
    target = _resolve(path)
    if not target.exists():
        return f"[文件不存在] {path}"
    return target.read_text(encoding="utf-8")


def write_file(path: str, content: str) -> str:
    """把 content 写入 workspace 内的文件（覆盖），返回确认信息。"""
    target = _resolve(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    msg = f"已写入 {len(content)} 字符到 {path}"
    if path.endswith(".py") or path.endswith(".pyw"):
        msg += _py_check(target)
    return msg


def list_dir(path: str = ".") -> str:
    """列出 workspace 内某目录下的文件/子目录。"""
    target = _resolve(path)
    if not target.exists():
        return f"[目录不存在] {path}"
    entries = sorted(p.name + ("/" if p.is_dir() else "") for p in target.iterdir())
    return "\n".join(entries) if entries else "(空目录)"


def grep(pattern: str, path: str = ".", glob: str = None, regex: bool = False, max_results: int = 50) -> str:
    """在 workspace 内搜索文件内容，返回 "相对路径:行号:匹配行"。
    pattern: 搜索文本；regex=True 时按正则。path: 起始目录(默认 workspace 根)。
    glob: 文件名过滤如 '*.js'；max_results: 最多返回匹配数。"""
    import fnmatch
    import re
    root = _resolve(path)
    if not root.exists():
        return f"[路径不存在] {path}"
    try:
        rx = re.compile(pattern if regex else re.escape(pattern))
    except re.error as e:
        return f"[正则错误] {e}"
    matches, scanned = [], 0
    for fp in sorted(root.rglob("*")):
        if not fp.is_file() or (glob and not fnmatch.fnmatch(fp.name, glob)):
            continue
        try:
            text = fp.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        scanned += 1
        rel = fp.relative_to(WORKSPACE).as_posix()
        for i, line in enumerate(text.splitlines(), 1):
            if rx.search(line):
                matches.append(f"{rel}:{i}: {line.strip()[:200]}")
                if len(matches) >= max_results:
                    matches.append(f"...（已达 max_results={max_results}，截断）")
                    return f"扫描 {scanned} 个文件，匹配 {max_results}+ 处：\n" + "\n".join(matches)
    if not matches:
        return f"(扫描 {scanned} 个文件，未找到 '{pattern}')"
    return f"扫描 {scanned} 个文件，匹配 {len(matches)} 处：\n" + "\n".join(matches)


def edit(path: str, old_string: str, new_string: str, replace_all: bool = False) -> str:
    """精确替换文件中的一段文本（比 write_file 整体覆盖更安全，保留其余内容）。
    path: workspace 内文件；old_string: 要替换的原文(须唯一，否则用 replace_all 或加更多上下文)；
    new_string: 替换为；replace_all=True 替换全部匹配。"""
    target = _resolve(path)
    if not target.exists():
        return f"[文件不存在] {path}"
    content = target.read_text(encoding="utf-8")
    count = content.count(old_string)
    if count == 0:
        return "[未找到] 文件中没有该 old_string（注意首尾空白/缩进需完全一致）"
    if count > 1 and not replace_all:
        return f"[不唯一] 共匹配 {count} 处，请加更多上下文让 old_string 唯一，或设 replace_all=True"
    if old_string == new_string:
        return "[无变化] old_string 与 new_string 相同"
    new_content = content.replace(old_string, new_string) if replace_all else content.replace(old_string, new_string, 1)
    target.write_text(new_content, encoding="utf-8")
    msg = f"✅ 已替换 {count if replace_all else 1} 处（{path}）"
    if path.endswith(".py") or path.endswith(".pyw"):
        msg += _py_check(target)
    return msg


def web_search(query: str) -> str:
    """用 DuckDuckGo 搜索，返回前几条结果的标题/链接/摘要。无需 API key。
    注意：搜索引擎可能临时限流；国内网络下可能需要代理。"""
    import warnings
    try:
        try:
            from ddgs import DDGS               # 新包名
        except ImportError:
            from duckduckgo_search import DDGS  # 旧包名（会触发重命名警告）
    except ImportError:
        return "[web_search 不可用] 未安装搜索库（pip install ddgs）"
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")      # 屏蔽旧包的重命名警告
            results = list(DDGS().text(query, max_results=5))
    except Exception as e:
        return f"[搜索失败] {type(e).__name__}: {e}\n（搜索引擎可能限流，或国内需代理）"
    if not results:
        return f"(没有搜到关于 '{query}' 的结果)"
    lines = []
    for i, r in enumerate(results, 1):
        title = r.get("title", "")
        url = r.get("href") or r.get("link") or ""
        body = r.get("body") or r.get("snippet") or ""
        lines.append(f"{i}. {title}\n   {url}\n   {body}")
    return "\n\n".join(lines)


def run_shell(command: str) -> str:
    """执行一条系统 shell 命令，返回输出。超时 10 秒。【最强大也最危险，慎用】。"""
    try:
        proc = subprocess.run(
            command, shell=True, capture_output=True, text=True,
            timeout=TOOL_TIMEOUT, cwd=str(WORKSPACE),
        )
        out = proc.stdout
        if proc.stderr:
            out += ("\n[stderr]\n" + proc.stderr) if out else proc.stderr
        return out.strip() or "(无输出)"
    except subprocess.TimeoutExpired:
        return f"[命令超时（>{TOOL_TIMEOUT}s），已终止]"


def set_tool_timeout(seconds: int) -> str:
    """设置 run_python / run_shell 的超时秒数（默认 10）。
    某些工具调用可能很长（如模拟、训练），可调大到 600（10分钟）甚至 1800（30分钟）。
    seconds: 超时秒数（1~7200）。"""
    global TOOL_TIMEOUT
    if not (1 <= seconds <= 7200):
        return f"❌ seconds 需在 1~7200 之间，收到 {seconds}"
    old = TOOL_TIMEOUT
    TOOL_TIMEOUT = seconds
    return f"✅ 工具超时已从 {old}s 改为 {seconds}s"


def get_tool_timeout() -> str:
    """查看当前 run_python / run_shell 的超时秒数。"""
    return f"当前工具超时：{TOOL_TIMEOUT}s"


REAL_TOOLS = Toolbox(
    Tool(run_python),
    Tool(read_file),
    Tool(write_file),
    Tool(edit),
    Tool(list_dir),
    Tool(grep),
    Tool(web_search),
    Tool(run_shell),
    Tool(set_tool_timeout),
    Tool(get_tool_timeout),
)


def make_autonomous_tools(agent) -> list:
    """生成绑定到指定 Agent 的纯自主模式工具。"""
    from datetime import datetime, timedelta

    def set_autonomous_mode(end_time: str = None, duration_minutes: int = None,
                            prompt: str = None) -> str:
        """开启纯自主模式：任务完成后自动继续工作，直到约定时间或手动退出。
        end_time: 结束时间，格式 "HH:MM"（今天）或 "YYYY-MM-DD HH:MM"；
        duration_minutes: 或者指定持续分钟数（如 30=半小时后结束）；
        prompt: 自动继续时使用的提示词（默认："当前为纯自主模式，请继续按照要求完成更多工作"）。
        二者选一即可；退出用 /autonomous off 或 exit_autonomous_mode 工具。"""
        try:
            if duration_minutes is not None:
                target = datetime.now() + timedelta(minutes=int(duration_minutes))
            elif end_time:
                end_time = end_time.strip()
                # 尝试解析 "YYYY-MM-DD HH:MM"
                try:
                    target = datetime.strptime(end_time, "%Y-%m-%d %H:%M")
                except ValueError:
                    # 尝试解析 "HH:MM"（今天）
                    today = datetime.now().date()
                    target = datetime.strptime(f"{today} {end_time}", "%Y-%m-%d %H:%M")
                    # 如果时间已过，算明天
                    if target < datetime.now():
                        from datetime import timedelta
                        target += timedelta(days=1)
            else:
                return "[参数缺失] 请提供 end_time（如 '17:30' 或 '2026-07-08 17:30'）或 duration_minutes（持续分钟数）"

            agent.set_autonomous_mode(target, prompt)
            return f"✅ 纯自主模式已开启，持续到 {target.strftime('%Y-%m-%d %H:%M')}（提示词：{prompt or '默认'}）"
        except Exception as e:
            return f"[开启失败] {type(e).__name__}: {e}"

    def exit_autonomous_mode() -> str:
        """退出纯自主模式。"""
        agent.exit_autonomous_mode()
        return "✅ 纯自主模式已关闭"

    def autonomous_status() -> str:
        """查看纯自主模式当前状态。"""
        if not agent.autonomous_mode:
            return "纯自主模式：未开启"
        if agent.is_autonomous_active():
            return f"纯自主模式：已开启，持续到 {agent.autonomous_end_time.strftime('%Y-%m-%d %H:%M')}\n" \
                   f"自动提示词：{agent.autonomous_prompt}\n" \
                   f"待处理消息队列：{len(agent.pending_messages)} 条"
        else:
            return "纯自主模式：已超时（自动关闭）"

    def set_goal_check(script: str) -> str:
        """设置目标达成验证脚本（Python）。自主循环每轮结束后跑它：输出 'PASS' 表示目标达成、自动结束自主模式；
        否则继续。如：拉坦克天梯分 ≥ 3000 → print('PASS')。"""
        if not script or not script.strip():
            return "[错误] script 不能为空"
        agent.goal_check_script = script.strip()
        return "✅ 目标验证脚本已设置（自主循环每轮结束后自动检查）"

    def check_goal() -> str:
        """手动运行一次目标验证脚本，返回输出（PASS=达成/FAIL=未达成/空=未设目标）。"""
        if not agent.goal_check_script:
            return "(未设置目标验证脚本，用 set_goal_check(script) 设置)"
        return agent.run_goal_check() or "(空输出)"

    return [Tool(set_autonomous_mode), Tool(exit_autonomous_mode), Tool(autonomous_status),
            Tool(set_goal_check), Tool(check_goal)]
