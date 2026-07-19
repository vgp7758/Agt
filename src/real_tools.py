"""real_tools.py —— 真实强力工具（Step 6）。

把玩具计算器升级为能干实事的工具集：
  run_python              : 执行模型写的 Python（独立子进程 + 超时）。
  read_file/write_file/list_dir : 读写文件，限定在 workspace/ 内（控制爆炸半径）。
  web_search              : 联网搜索（DuckDuckGo，无需 key；国内可能需代理）。
  open_url                : 抓取网页提取正文文本（start/max_chars 分页续读）。
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


def edit(path: str, old_string: str, new_string: str, replace_all: bool = False,
         start_line: int = None, end_line: int = None) -> str:
    """精确替换文件中的一段文本。
    path: workspace 内文件；old_string: 要替换的原文；new_string: 替换为；
    replace_all=True 替换全部匹配。
    start_line/end_line: 只在该行范围内搜索替换（1-based，含两端）。"""
    target = _resolve(path)
    if not target.exists():
        return f"[文件不存在] {path}"
    content = target.read_text(encoding="utf-8")
    lines = content.splitlines()
    total = len(lines)
    # 行范围限定
    if start_line is not None or end_line is not None:
        s = max(0, (start_line or 1) - 1)
        e = min(total, end_line or total)
        if s >= total:
            return f"[行号越界] 文件共 {total} 行，start_line={start_line}"
        scope = "\n".join(lines[s:e])
        prefix = "\n".join(lines[:s])
        suffix = "\n".join(lines[e:])
    else:
        scope = content
        prefix, suffix = "", ""
        s = 0
    count = scope.count(old_string)
    if count == 0:
        where = f" L{s+1}-L{min(e,total) if (start_line or end_line) else total}" if (start_line or end_line) else ""
        return f"[未找到]{where} 文件中没有该 old_string"
    if count > 1 and not replace_all:
        return f"[不唯一] 共匹配 {count} 处，请加更多上下文让 old_string 唯一，或设 replace_all=True"
    if old_string == new_string:
        return "[无变化] old_string 与 new_string 相同"
    new_scope = scope.replace(old_string, new_string) if replace_all else scope.replace(old_string, new_string, 1)
    new_content = (prefix + ("\n" if prefix else "") + new_scope + ("\n" if suffix else "") + suffix) if (start_line or end_line) else new_scope
    target.write_text(new_content, encoding="utf-8")
    msg = f"✅ 已替换 {count if replace_all else 1} 处（{path}" + (f" L{start_line}-L{end_line}" if start_line or end_line else "") + ")"
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


def _html_to_text(html: str) -> tuple[str, str]:
    """HTML → (title, 正文文本)。剥 script/style，块级标签换行，压缩空白。标准库实现，零依赖。"""
    import re
    from html.parser import HTMLParser

    class _Extractor(HTMLParser):
        SKIP = {"script", "style", "noscript", "template", "svg", "iframe"}
        BLOCK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6",
                 "section", "article", "header", "footer", "ul", "ol", "table",
                 "blockquote", "pre", "hr", "form", "nav", "aside"}

        def __init__(self):
            super().__init__(convert_charrefs=True)
            self.parts: list[str] = []
            self.title_parts: list[str] = []
            self._skip_depth = 0
            self._in_title = False

        def handle_starttag(self, tag, attrs):
            if tag in self.SKIP:
                self._skip_depth += 1
            elif tag == "title":
                self._in_title = True
            elif tag in self.BLOCK:
                self.parts.append("\n")

        def handle_endtag(self, tag):
            if tag in self.SKIP:
                self._skip_depth = max(0, self._skip_depth - 1)
            elif tag == "title":
                self._in_title = False
            elif tag in self.BLOCK:
                self.parts.append("\n")

        def handle_data(self, data):
            if self._skip_depth:
                return
            if self._in_title:
                self.title_parts.append(data)
            elif data.strip():
                self.parts.append(data)

    p = _Extractor()
    try:
        p.feed(html)
        p.close()
    except Exception:
        pass  # 残缺 HTML 也尽量用已解析的部分
    lines = [re.sub(r"[ \t　]+", " ", ln).strip() for ln in "".join(p.parts).splitlines()]
    return "".join(p.title_parts).strip(), "\n".join(ln for ln in lines if ln)


def open_url(url: str, start: int = 0, max_chars: int = 8000) -> str:
    """抓取网页并提取正文文本（HTML 剥标签；JSON/纯文本原样），支持分页续读。
    url: 网页地址（http/https）；start: 从第几个字符开始读（0-based，默认 0）；
    max_chars: 本次最多返回字符数（默认 8000）。返回头部含总字数，未读完时按提示传 start 续读。"""
    import requests
    if not url.lower().startswith(("http://", "https://")):
        url = "https://" + url
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                             "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"}
    try:
        resp = requests.get(url, headers=headers, timeout=15)
    except Exception as e:
        return f"[抓取失败] {type(e).__name__}: {e}\n（网络不通或国内需代理）"
    # header 未声明 charset 时 requests 默认 ISO-8859-1，中文页会乱码 → 用探测编码
    if not resp.encoding or resp.encoding.lower() == "iso-8859-1":
        resp.encoding = resp.apparent_encoding
    ctype = (resp.headers.get("Content-Type") or "").lower()
    body = resp.text or ""
    if "html" in ctype or (not ctype and body.lstrip()[:1] == "<"):
        title, text = _html_to_text(body)
    else:
        title, text = "", body  # JSON / 纯文本等直接原样
    total = len(text)
    if total == 0:
        return f"[{url} HTTP {resp.status_code}] （提取不到正文，可能是纯 JS 渲染页面）"
    start = max(0, int(start))
    if start >= total:
        return f"[越界] 正文共 {total} 字符，start={start} 超出范围"
    end = min(start + max(1, int(max_chars)), total)
    status = "" if resp.status_code == 200 else f" HTTP {resp.status_code} |"
    title_part = f" 标题:{title} |" if title else ""
    more = f"，续读传 start={end}" if end < total else "，已读完"
    return f"[{url}{status}{title_part} 第 {start}-{end-1} 字 / 共 {total} 字{more}]\n" + text[start:end]


def _paginate_text(text: str, label: str, start: int, max_chars: int) -> str:
    """对纯文本分页：返回带头部（第 X-Y 字 / 共 N 字）的切片。越界报错。"""
    total = len(text)
    start = max(0, int(start))
    if start >= total:
        return f"[越界] 共 {total} 字符，start={start} 超出范围"
    end = min(start + max(1, int(max_chars)), total)
    more = f"，续读传 start={end}" if end < total else "，已读完"
    return f"[{label} | 第 {start}-{end-1} 字 / 共 {total} 字{more}]\n" + text[start:end]


_WORKFLOW_SPEC_URL = "https://raw.githubusercontent.com/vgp7758/Agt/main/docs/workflow-spec.md"
_WORKFLOW_SPEC_LOCAL = Path(__file__).resolve().parent.parent / "docs" / "workflow-spec.md"


def read_workflow_spec(start: int = 0, max_chars: int = 6000) -> str:
    """读取工作流规范全文（docs/workflow-spec.md）。【写工作流前务必先读】了解节点类型/字段/变量引用。
    从线上 git raw 读取（与本地 docs/ 同源），网络不通时回退本地 docs/。
    start: 从第几个字符开始读(0-based)；max_chars: 本次最多返回字符数(默认 6000)。"""
    text = None
    # 1) 优先线上 git raw（保证拿到最新版）
    try:
        import requests
        r = requests.get(_WORKFLOW_SPEC_URL, headers={"User-Agent": "agt-agent"}, timeout=15)
        if r.status_code == 200 and r.text:
            text = r.text
    except Exception:
        pass
    # 2) 兜底本地 docs/（pip 安装后随包附带；开发期在仓库根）
    if text is None and _WORKFLOW_SPEC_LOCAL.exists():
        try:
            text = _WORKFLOW_SPEC_LOCAL.read_text(encoding="utf-8")
        except Exception:
            pass
    if not text:
        return f"[读取失败] git raw 与本地 {_WORKFLOW_SPEC_LOCAL} 均无法获取 workflow-spec.md"
    return _paginate_text(text, "workflow-spec.md", start, max_chars)


# 工作流 demo 读取（git raw，本地兜底）
_DEMO_BASE_URL = "https://raw.githubusercontent.com/vgp7758/Agt/main/.agent/workflows/"
_DEMO_LOCAL_DIR = Path(__file__).resolve().parent.parent / ".agent" / "workflows"
_DEMOS = {
    "composite_demo": "循环+批处理+单节点批处理三合一（迭代入口/continue/break 模型，多工具组合+本地变量+筛选 nth）",
    "full_demo": "全节点类型演示（意图分流→各分支处理→聚合→序列化，覆盖 15 种节点）",
}


def _fetch_demo_text(name: str) -> str:
    """从 git raw 读 demo XML，失败回退本地。"""
    text = None
    try:
        import requests
        r = requests.get(_DEMO_BASE_URL + name + ".xml", headers={"User-Agent": "agt-agent"}, timeout=15)
        if r.status_code == 200 and r.text:
            text = r.text
    except Exception:
        pass
    if text is None:
        local = _DEMO_LOCAL_DIR / (name + ".xml")
        if local.exists():
            try:
                text = local.read_text(encoding="utf-8")
            except Exception:
                pass
    return text


def _builtin_tools_reference() -> str:
    """列出工作流可用的内置工具（LIGHT_TOOLS）及示例 plugin 节点 XML。"""
    lines = ["=== 工作流内置工具（未注册给 Agent，只能在工作流 plugin 节点用）===",
             "这些轻量工具（add/split/sleep 等）Agent 不能直接调用，仅工作流编排可用。",
             "调用：<node type=\"plugin\" toolName=\"工具名\">，输出 raw（工具返回值）。",
             "入参 <in> 接上游输出 ref=\"节点ID.字段\"，或字面量 literal=\"值\"。",
             ""]
    _TS = {int: "integer", float: "number", bool: "boolean", list: "list", dict: "object"}
    for t in LIGHT_TOOLS:
        lines.append(f"【{t.name}】{t.description}")
        ins = []
        for pname, param in t._sig.parameters.items():
            ptype = t._hints.get(pname, str)
            ts = _TS.get(ptype, "string")
            ins.append(f'    <in name="{pname}" type="{ts}"/>')
        lines.append(f'  示例：<node id="N" type="plugin" toolName="{t.name}">')
        lines.extend(ins)
        lines.append(f'    <out name="raw" type="string"/>')
        lines.append('  </node>')
        lines.append("")
    return "\n".join(lines)


def read_workflow_demo(demo: str = "", start: int = 0, max_chars: int = 8000) -> str:
    """读取工作流 demo XML 示例，或列出 demo 清单 + 内置工具说明。
    【写工作流前参考】了解循环/批处理/各节点的 XML 写法和可用的内置工具。
    demo: 空则返回 demo 清单 + 内置工具（含示例节点 XML）；
          'composite_demo' 读循环+批处理三合一；'full_demo' 读全节点类型演示。
    start/max_chars: 读取指定 demo 时的分页（XML 较长可续读）。"""
    if demo:
        if demo not in _DEMOS:
            return f"[未知 demo] {demo}，可选：{', '.join(_DEMOS)}"
        text = _fetch_demo_text(demo)
        if not text:
            return f"[读取失败] {demo}.xml（git raw 与本地均不可用）"
        header = f"=== {demo}.xml —— {_DEMOS[demo]} ===\n"
        return header + _paginate_text(text, demo + ".xml", start, max_chars)
    # 无 demo：返回清单 + 内置工具说明
    parts = ["=== 工作流 demo 清单（传 demo=名称 读取完整 XML）==="]
    for name, desc in _DEMOS.items():
        parts.append(f"  - {name}: {desc}")
    parts.append("")
    parts.append(_builtin_tools_reference())
    parts.append("提示：先 read_workflow_spec 了解节点类型/字段规范，再看 demo 学写法。")
    return "\n".join(parts)


def run_shell(command: str) -> str:
    """执行一条系统 shell 命令，实时流式输出。超时由 TOOL_TIMEOUT 控制（可用 set_tool_timeout 调大）。"""
    return _run_subprocess_streaming(command, "run_shell", shell=True)


def run_script(script: str, payload: str = "") -> str:
    """运行本地 Python 脚本并返回其 stdout——用于在工作流中执行自己写的处理脚本。
    script: 脚本路径（相对 workspace，如 'tools/analyze.py' 或 '.agent/workflows/tools/x.py'）；
    payload: 传给脚本的 JSON 负载，脚本通过环境变量 PAYLOAD 读取（json.loads 后使用）。
    【工作流用法】前置一个 ToJSON 节点把若干输入组装成 JSON，output 接本节点 payload；
    脚本约定：读 os.environ['PAYLOAD'] 取参数、print 输出结果（后续可接 FromJSON 解析）。"""
    import subprocess
    import sys
    import os
    target = _resolve(script)
    if not target.exists():
        return f"[脚本不存在] {script}（相对 workspace，如 tools/xxx.py）"
    if target.suffix.lower() not in (".py", ".pyw"):
        return f"[仅支持 .py 脚本] {script}"
    env = dict(os.environ)
    env["PAYLOAD"] = payload or ""
    # 把 workspace 加入 PYTHONPATH，让脚本能 import workspace 内其它模块（如 tools/ 下的辅助模块）
    pp = str(WORKSPACE)
    if env.get("PYTHONPATH"):
        pp = pp + os.pathsep + env["PYTHONPATH"]
    env["PYTHONPATH"] = pp
    try:
        proc = subprocess.run([sys.executable, str(target)], capture_output=True, text=True,
                              timeout=TOOL_TIMEOUT, env=env, cwd=str(WORKSPACE),
                              encoding="utf-8", errors="replace")
    except subprocess.TimeoutExpired:
        return f"[脚本执行超时（>{TOOL_TIMEOUT}s），可用 set_tool_timeout 调大]"
    except Exception as e:
        return f"[执行失败] {type(e).__name__}: {e}"
    out = (proc.stdout or "").strip()
    if proc.returncode != 0:
        err = (proc.stderr or "").strip()
        return f"[脚本出错 rc={proc.returncode}]\nstderr: {err[-500:]}\nstdout: {out[-500:]}"
    return out or "(无输出)"


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


def sleep(seconds: float) -> str:
    """等待指定秒数后返回（工作流 wait 节点：轮询间隔/限速等用）。seconds: 秒数（0~300）。"""
    try:
        s = float(seconds)
    except (TypeError, ValueError):
        return f"[错误] seconds 需为数字，收到 {seconds!r}"
    if not (0 <= s <= 300):
        return f"[错误] seconds 需在 0~300 之间，收到 {s:g}"
    time.sleep(s)
    return f"已等待 {s:g} 秒"


REAL_TOOLS = Toolbox(
    Tool(run_python),
    Tool(read_file),
    Tool(write_file),
    Tool(edit),
    Tool(list_dir),
    Tool(grep),
    Tool(web_search),
    Tool(open_url),
    Tool(read_workflow_spec),
    Tool(read_workflow_demo),
    Tool(run_shell),
    Tool(run_script),
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
    Tool(sleep),
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
