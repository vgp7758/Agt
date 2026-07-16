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
import queue
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from tools import Tool, Toolbox

# 工作区 = 启动时的当前目录(cwd)。文件读写、代码执行都以这里为根与沙箱边界。
# 故可从任意目录 `python /path/to/chat.py` 启动，在当前目录执行任务。
WORKSPACE = Path.cwd()

# 代码执行与 shell 的超时秒数（可通过 set_timeout 工具运行时调整）
TOOL_TIMEOUT = 10

# 工具执行进度回调（由 agent 在执行工具前设置；流式输出/心跳通过它推给 UI）
_tool_emit = None


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
def _run_subprocess_streaming(args, name, shell=False):
    """运行子进程，实时流式输出 + 30 秒心跳进度。reader 线程兼容 Windows。
    通过 _tool_emit 回调推送 tool_stream / tool_progress 事件。"""
    proc = subprocess.Popen(
        args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, cwd=str(WORKSPACE), shell=shell,
        bufsize=1, encoding="utf-8", errors="replace",
    )
    start = time.time()

    # reader 线程：逐行读 stdout → queue
    line_q: queue.Queue = queue.Queue()

    def _reader():
        try:
            for line in proc.stdout:
                line_q.put(line)
        except Exception:
            pass
        line_q.put(None)  # EOF

    threading.Thread(target=_reader, daemon=True).start()

    output_lines = []
    stream_buf = []
    last_hb = start
    last_flush = start

    while True:
        try:
            line = line_q.get(timeout=0.5)
        except queue.Empty:
            line = "__poll__"  # 无输出，走心跳/超时检查

        if line is None:
            break  # EOF
        elif line != "__poll__":
            output_lines.append(line)
            stream_buf.append(line)

        now = time.time()
        elapsed = now - start

        # 流式输出（每 ~1 秒 flush 一次，避免事件风暴）
        if stream_buf and now - last_flush >= 1.0:
            if _tool_emit:
                _tool_emit({"type": "tool_stream", "name": name,
                            "text": "".join(stream_buf), "elapsed": round(elapsed, 1)})
            stream_buf = []
            last_flush = now

        # 心跳（每 30 秒）
        if now - last_hb >= 30.0:
            if _tool_emit:
                preview = "".join(output_lines[-5:])[-200:]
                _tool_emit({"type": "tool_progress", "name": name,
                            "elapsed": round(elapsed, 1), "lines": len(output_lines),
                            "preview": preview})
            last_hb = now

        # 超时
        if elapsed > TOOL_TIMEOUT:
            proc.kill()
            proc.wait()
            if stream_buf and _tool_emit:
                _tool_emit({"type": "tool_stream", "name": name, "text": "".join(stream_buf)})
            return (f"[执行超时（>{TOOL_TIMEOUT}s），已终止。"
                    f"如任务确实需要更长时间，先调用 set_tool_timeout(seconds) "
                    f"调大超时（最大 7200s=2小时），再重试。]")

    proc.wait()
    # 最终 flush
    if stream_buf and _tool_emit:
        _tool_emit({"type": "tool_stream", "name": name, "text": "".join(stream_buf)})

    return "".join(output_lines).strip() or "(无输出)"


def run_python(code: str) -> str:
    """运行一段 Python 代码，实时流式输出（支持长任务进度）。独立子进程执行。"""
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False, encoding="utf-8") as f:
        f.write(code)
        tmp = f.name
    try:
        return _run_subprocess_streaming([sys.executable, tmp], "run_python")
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def read_file(path: str, start_line: int = None, end_line: int = None) -> str:
    """读取 workspace 内某个文件的内容（文本/Word/Excel/PDF 自动提取）。
    start_line/end_line: 只读指定行范围（1-based，含两端；不传=全文）。"""
    target = _resolve(path)
    if not target.exists():
        return f"[文件不存在] {path}"
    if target.suffix.lower() in {".docx", ".xlsx", ".xlsm", ".xltx", ".pdf"}:
        text = _extract_text(target)
        if text is None:
            text = target.read_text(encoding="utf-8", errors="ignore")
        lines = text.splitlines()
    else:
        text = target.read_text(encoding="utf-8")
        lines = text.splitlines()
    total = len(lines)
    if start_line is None and end_line is None:
        return text
    start = max(1, start_line or 1) - 1
    end = min(total, end_line or total)
    if start >= total:
        return f"[行号越界] 文件共 {total} 行，请求 start_line={start_line}"
    selected = lines[start:end]
    header = f"[{path} L{start+1}-L{end}/{total}]\n"
    return header + "\n".join(selected)


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


def _extract_text(target: Path) -> str | None:
    """对 Word/Excel/PDF 提取纯文本；不支持则返回 None。"""
    suffix = target.suffix.lower()
    try:
        if suffix == ".docx":
            import docx
            doc = docx.Document(str(target))
            return "\n".join(p.text for p in doc.paragraphs)
        if suffix in (".xlsx", ".xlsm", ".xltx"):
            import openpyxl
            wb = openpyxl.load_workbook(str(target), read_only=True, data_only=True)
            parts = []
            for name in wb.sheetnames:
                ws = wb[name]
                parts.append(f"=== Sheet: {name} ===")
                for row in ws.iter_rows(values_only=True):
                    parts.append("\t".join(str(c) if c is not None else "" for c in row))
            wb.close()
            return "\n".join(parts)
        if suffix == ".pdf":
            text = ""
            try:
                import fitz
                doc = fitz.open(str(target))
                for page in doc:
                    text += page.get_text()
                doc.close()
            except Exception:
                import PyPDF2
                reader = PyPDF2.PdfReader(str(target))
                for page in reader.pages:
                    t = page.extract_text()
                    if t:
                        text += t + "\n"
            return text.strip()
        return None
    except Exception as e:
        return f"[文档解析失败: {type(e).__name__}: {e}]"


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
    DOC_EXT = {".docx", ".xlsx", ".xlsm", ".xltx", ".pdf"}
    matches, scanned = [], 0
    for fp in sorted(root.rglob("*")):
        if not fp.is_file() or (glob and not fnmatch.fnmatch(fp.name, glob)):
            continue
        text = None
        if fp.suffix.lower() in DOC_EXT:
            extracted = _extract_text(fp)
            if extracted and not extracted.startswith("[文档解析失败"):
                text = extracted
        else:
            try:
                text = fp.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue
        if text is None:
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
    """执行一条系统 shell 命令，实时流式输出。超时由 TOOL_TIMEOUT 控制（可用 set_tool_timeout 调大）。"""
    return _run_subprocess_streaming(command, "run_shell", shell=True)


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


# ===== 内置轻量工具（工作流编排可用）=====

def add(a: float, b: float) -> float:
    """两个数相加，返回和。"""
    return a + b

def subtract(a: float, b: float) -> float:
    """a 减 b，返回差。"""
    return a - b

def multiply(a: float, b: float) -> float:
    """两个数相乘，返回积。"""
    return a * b

def divide(a: float, b: float) -> float:
    """a 除以 b，返回商。b 为 0 返回错误提示。"""
    if b == 0:
        return "[错误] 除数不能为 0"
    return a / b

def join(items: list, separator: str = ",") -> str:
    """用分隔符把字符串列表拼接成一个字符串（类似 string.join）。items: 字符串列表；separator: 分隔符。"""
    return separator.join(str(x) for x in (items or []))

def split(text: str, separator: str = ",") -> list:
    """按分隔符把字符串切成列表（类似 string.split）。text: 原文；separator: 分隔符。"""
    return text.split(separator) if text else []

def length(obj) -> int:
    """返回字符串/列表/字典的长度。"""
    try:
        return len(obj)
    except TypeError:
        return len(str(obj))

def to_uppercase(text: str) -> str:
    """字符串转大写。"""
    return (text or "").upper()

def to_lowercase(text: str) -> str:
    """字符串转小写。"""
    return (text or "").lower()

def contains(text: str, keyword: str) -> bool:
    """判断 text 是否包含 keyword，返回 true/false。"""
    return keyword in (text or "")


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

# 轻量工具（仅工作流编排用，不注册给 Agent）
LIGHT_TOOLS = Toolbox(
    Tool(add),
    Tool(subtract),
    Tool(multiply),
    Tool(divide),
    Tool(join),
    Tool(split),
    Tool(length),
    Tool(to_uppercase),
    Tool(to_lowercase),
    Tool(contains),
)

# 全部内置工具（编辑器 /api/tools 返回这个）
ALL_BUILTIN_TOOLS = Toolbox(*(list(REAL_TOOLS) + list(LIGHT_TOOLS)))


def infer_tool_outputs(tool) -> list[dict]:
    """从工具的返回值类型注解推断输出 schema。
    str→[{name:'result',type:'string'}], float→number, int→integer, bool→boolean,
    list→list, dict→object, 无注解或无返回值→[{name:'raw',type:'string'}]。"""
    try:
        hints = getattr(tool.func, "__annotations__", {})
    except Exception:
        hints = {}
    ret = hints.get("return")
    mapping = {"str": "string", "int": "integer", "float": "number", "number": "number",
               "bool": "boolean", "list": "list", "dict": "object",
               str: "string", int: "integer", float: "number", bool: "boolean",
               list: "list", dict: "object"}
    if ret is None or ret is type(None):
        return [{"name": "raw", "type": "string", "description": "工具返回"}]
    # ret 可能是字符串（from __future__）或类型
    key = ret if isinstance(ret, str) else (ret.__name__ if hasattr(ret, "__name__") else str(ret))
    if ret in mapping:
        return [{"name": "result", "type": mapping[ret], "description": "工具返回值"}]
    if key in mapping:
        return [{"name": "result", "type": mapping[key], "description": "工具返回值"}]
    return [{"name": "raw", "type": "string", "description": "工具返回"}]


def make_autonomous_tools(agent) -> list:
    """生成绑定到指定 Agent 的纯自主模式工具。"""
    from datetime import datetime, timedelta

    def set_autonomous_mode(end_time: str = None, duration_minutes: int = None,
                            prompt: str = None, goal_check_code: str = None) -> str:
        """开启纯自主模式：任务完成后自动继续工作，直到时间到或目标达成（哪个先满足）。
        end_time: 结束时间 "HH:MM"（今天）或 "YYYY-MM-DD HH:MM"；
        duration_minutes: 持续分钟数（如 180=3小时）；
        goal_check_code: 目标验证 Python 脚本（print('PASS')=达成→自动停止）；
        prompt: 自动继续时的提示词。
        以上四个参数至少提供一个（end_time/duration_minutes/goal_check_code 三选一即可）。"""
        from datetime import datetime, timedelta
        try:
            # 目标脚本
            if goal_check_code:
                agent.goal_check_script = goal_check_code.strip()
            # 结束时间
            target = None
            if duration_minutes is not None:
                target = datetime.now() + timedelta(minutes=int(duration_minutes))
            elif end_time:
                end_time = end_time.strip()
                try:
                    target = datetime.strptime(end_time, "%Y-%m-%d %H:%M")
                except ValueError:
                    today = datetime.now().date()
                    target = datetime.strptime(f"{today} {end_time}", "%Y-%m-%d %H:%M")
                    if target < datetime.now():
                        target += timedelta(days=1)
            if target is None and not goal_check_code:
                return "[参数缺失] 至少提供 end_time / duration_minutes / goal_check_code 之一"
            agent.set_autonomous_mode(target or datetime.max, prompt)
            parts = []
            if target:
                parts.append(f"持续到 {target.strftime('%Y-%m-%d %H:%M')}")
            if goal_check_code:
                parts.append("目标验证脚本已设置")
            return f"✅ 纯自主模式已开启（{'，'.join(parts)}）"
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
