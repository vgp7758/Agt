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
    注意：搜索引擎可能临时限流；国内网络下可能需要代理。
    返回 JSON 字符串 {success, count, result, error}——success 为结构化输出字段，
    工作流 plugin 节点可直接引用 web_search_node.success 判断成功与否。"""
    import json as _json
    import warnings
    out = {"success": False, "count": 0, "result": "", "error": ""}
    try:
        try:
            from ddgs import DDGS               # 新包名
        except ImportError:
            from duckduckgo_search import DDGS  # 旧包名（会触发重命名警告）
    except ImportError:
        out["error"] = "未安装搜索库（pip install ddgs）"
        return _json.dumps(out, ensure_ascii=False)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")      # 屏蔽旧包的重命名警告
            results = list(DDGS().text(query, max_results=5))
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}（搜索引擎可能限流，或国内需代理）"
        return _json.dumps(out, ensure_ascii=False)
    out["success"] = True
    out["count"] = len(results)
    if results:
        lines = [f"{i}. {r.get('title','')}\n   {(r.get('href') or r.get('link') or '')}\n   {(r.get('body') or r.get('snippet') or '')}"
                 for i, r in enumerate(results, 1)]
        out["result"] = "\n\n".join(lines)
    else:
        out["result"] = f"没有搜到关于 '{query}' 的结果"
    return _json.dumps(out, ensure_ascii=False)


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
    """读取工作流 demo XML 示例，或列出可用 demo 清单。
    【写工作流前参考】了解循环/批处理/各节点的 XML 写法。
    demo: 空则返回 demo 清单；'composite_demo' 读循环+批处理三合一；'full_demo' 读全节点类型演示。
    start/max_chars: 读取指定 demo 时的分页（XML 较长可续读）。
    提示：先用 list_workflow_nodes 了解可用节点，再用 query_workflow_node 查节点 XML 示例。"""
    if demo:
        if demo not in _DEMOS:
            return f"[未知 demo] {demo}，可选：{', '.join(_DEMOS)}"
        text = _fetch_demo_text(demo)
        if not text:
            return f"[读取失败] {demo}.xml（git raw 与本地均不可用）"
        header = f"=== {demo}.xml —— {_DEMOS[demo]} ===\n"
        return header + _paginate_text(text, demo + ".xml", start, max_chars)
    # 无 demo：返回清单
    parts = ["=== 工作流 demo 清单（传 demo=名称 读取完整 XML）==="]
    for name, desc in _DEMOS.items():
        parts.append(f"  - {name}: {desc}")
    parts.append("")
    parts.append("提示：用 list_workflow_nodes 查看所有可用节点类型，用 query_workflow_node 查具体节点 XML 示例。")
    return "\n".join(parts)


# ===== 工作流节点目录 ====

_NODE_CATALOG = [
    # ===== 基础节点 =====
    {
        "type": "1", "name": "开始 (Start)",
        "desc": "工作流入口，定义外部调用时需传入的参数（即工作流工具的函数签名）",
        "xml": """<!-- 开始节点：定义工作流入参。每个工作流有且仅有一个开始节点(id=100001) -->
<node id="100001" type="start">
  <out name="query" type="string" required="true"/>
  <out name="max_results" type="integer" required="false">10</out>
</node>
<!--
  输入schema（外部->工作流）：
  | 字段        | 类型    | 必填 | 说明         |
  | query       | string  | ✓    | 查询关键词   |
  | max_results | integer |      | 最大结果数，默认 10 |
-->"""
    },
    {
        "type": "2", "name": "结束 (End)",
        "desc": "工作流出口，收集上游节点输出作为工作流返回值。支持两种模式：returnVariables（取指定字段）和 useAnswerContent（渲染模板文本）",
        "xml": """<!-- 结束节点(id=900001)：定义工作流返回值 -->
<!-- 模式1：returnVariables —— 取上游节点输出字段，组装成结构化返回值 -->
<node id="900001" type="end">
  <out name="answer" ref="130001.output"/>
  <out name="confidence" ref="140001.score"/>
</node>

<!-- 模式2：useAnswerContent —— 渲染一段模板文本作为单一返回值 -->
<node id="900001" type="end" useAnswerContent="true">
  <content><![CDATA[回答：{{answer}}（置信度：{{confidence}}）]]></content>
</node>
<!--
  输出schema（工作流->外部）：
  returnVariables 模式下输出各 out 字段；useAnswerContent 模式下输出 {"output": "渲染文本"}
-->"""
    },
    # ===== AI 节点 =====
    {
        "type": "3", "name": "LLM",
        "desc": "调用大语言模型，支持模板渲染 {{变量}}、systemPrompt、temperature/maxTokens 等参数配置，可声明结构化输出 schema",
        "xml": """<!-- LLM 节点：调用大模型 -->
<node id="130001" type="llm">
  <!-- 模板入参：声明后在 prompt/systemPrompt 中用 {{变量名}} 引用 -->
  <in name="query" ref="100001.query"/>
  <in name="context" ref="120001.result"/>

  <!-- LLM 参数（param）：在 Coze 中等同于 llmParam -->
  <param name="prompt"><![CDATA[根据以下上下文回答问题：{{query}}

上下文：
{{context}}]]></param>
  <param name="systemPrompt"><![CDATA[你是专业的问答助手。回答简洁准确，不超过 200 字。]]></param>
  <param name="temperature" type="float">0.7</param>
  <param name="maxTokens" type="integer">1024</param>
  <param name="modelName"><![CDATA[deepseek-chat]]></param>

  <!-- 结构化输出（可选）：声明 output 字段及其 schema，LLM 将按 JSON Schema 输出 -->
  <out name="output" type="string"/>
  <out name="answer" type="string"/>
  <out name="confidence" type="integer"/>
</node>
<!--
  llmParam 可用参数：prompt, systemPrompt, temperature, maxTokens, modelName, topP
  输入：in 声明的模板变量（在 prompt 中用 {{变量名}} 引用）
  输出：output（LLM 原始输出）；若声明了多个 out 字段，LLM 将输出符合 schema 的 JSON
-->"""
    },
    {
        "type": "4", "name": "插件 (Plugin)",
        "desc": "调用内置轻量工具（add/split/sleep 等）或用户自定义 Python 工具，输入输出通过 in/out 声明",
        "xml": """<!-- 插件节点：调用内置工具或用户工具 -->
<!-- 示例1：调用内置加法工具 -->
<node id="140001" type="plugin" toolName="add">
  <in name="a" type="number" ref="130001.x"/>
  <in name="b" type="number" literal="5"/>
  <out name="raw" type="string"/>
</node>

<!-- 示例2：调用内置分割工具 -->
<node id="140002" type="plugin" toolName="split">
  <in name="text" type="string" ref="130001.output"/>
  <in name="separator" type="string" literal=","/>
  <out name="raw" type="string"/>
</node>

<!-- 示例3：调用用户自定义工具（.agent/workflows/tools/xxx.py） -->
<node id="140003" type="plugin" toolName="my_custom_tool">
  <in name="param1" type="string" ref="130001.output"/>
  <in name="param2" type="integer" literal="42"/>
  <out name="raw" type="string"/>
</node>
<!--
  内置工具列表见末尾 _builtin_tools_reference() 输出。
  自定义工具放 .agent/workflows/tools/*.py，顶层函数自动注册。
  输出固定为 raw（工具返回值字符串）。
-->"""
    },
    {
        "type": "5", "name": "代码 (Code)",
        "desc": "在沙箱中执行 Python 3 代码（async def main(args) -> Output），通过 args.params 取输入，return dict 作为输出",
        "xml": """<!-- 代码节点：Python3 沙箱执行 -->
<node id="150001" type="code">
  <!-- 模板入参：在 code 中用 {{变量名}} 引用；也可在 main() 内通过 args.params 访问 -->
  <in name="x" ref="140001.result"/>
  <in name="y" ref="140002.result"/>

  <!-- Python3 代码（language=3）。约定：async def main(args) -> Output，return 的 dict 字段对应 out -->
  <param name="code" language="python3"><![CDATA[
import json

async def main(args) -> dict:
    x = float(args.params.get("x", 0))
    y = float(args.params.get("y", 0))
    result = {
        "sum": x + y,
        "product": x * y,
        "ratio": x / y if y != 0 else None,
    }
    return result
]]></param>

  <!-- 输出字段：必须与 main() 返回 dict 的 key 一致 -->
  <out name="sum" type="number"/>
  <out name="product" type="number"/>
  <out name="ratio" type="number"/>
</node>
<!--
  参数类型映射：string→str, integer→int, number→float, boolean→bool, list→list, object→dict
  args.params 是所有 in 的 dict；args.inputs 是原始 Coze InputParam 列表
-->"""
    },
    {
        "type": "22", "name": "意图识别 (Intent)",
        "desc": "用 LLM 对输入做意图分类，每个意图对应一个分支出口端口（branch_0/branch_1…），未匹配走 default",
        "xml": """<!-- 意图识别节点：LLM 分类 + 分支路由 -->
<node id="160001" type="intent">
  <!-- 输入：query 是要分类的文本 -->
  <in name="query" ref="130001.output"/>

  <!-- 意图列表：每个 intent 对应一个出口端口 -->
  <intent name="提问">用户想了解某个知识点或问"是什么/为什么/怎么"</intent>
  <intent name="指令">用户要求 AI 执行某个操作，如"帮我写/帮我查/翻译"</intent>
  <intent name="闲聊">用户只是聊天、打招呼、或表达情绪</intent>

  <!-- LLM 参数（可选，不写则用默认） -->
  <param name="systemPrompt"><![CDATA[你是一个意图分类器。根据用户输入判断意图。]]></param>
  <param name="temperature" type="float">0.1</param>

  <!-- 输出 -->
  <out name="classificationId" type="string"/>
  <out name="reason" type="string"/>
</node>
<!--
  出口端口：branch_0(第1个意图匹配), branch_1(第2个匹配), ... , default(都不匹配)
  输出字段：classificationId(匹配到的意图名), reason(LLM 给出的分类理由)
-->"""
    },
    # ===== 流程控制 =====
    {
        "type": "8", "name": "选择器 (Selector)",
        "desc": "条件分支：根据配置的 conditions 判断走哪个出口端口（true/true_1…/false），支持 Equal/Contain/Greater/Empty 等运算符",
        "xml": """<!-- 选择器节点：条件分支 -->
<node id="170001" type="selector">
  <!-- 输入供条件左值引用 -->
  <in name="score" ref="150001.score"/>

  <!-- 分支条件组：branches 数组，按顺序匹配 -->
  <branch>
    <!-- conditions: [{operator, left, right}]（可多条件，logic=1=OR, 2=AND） -->
    <condition operator="GreaterEqual" logic="2">
      <!-- left 引用上游输出字段（ref=节点ID.字段名） -->
      <left ref="150001.score"/>
      <!-- right 可以是 literal 或 ref -->
      <right literal="90">90</right>
    </condition>
    <!-- 出口端口：true（第1个分支匹配） -->
  </branch>

  <branch>
    <condition operator="GreaterEqual" logic="2">
      <left ref="150001.score"/>
      <right literal="60">60</right>
    </condition>
    <!-- 出口端口：true_1（第2个分支匹配） -->
  </branch>

  <!-- 都不匹配走 false 端口 -->
</node>
<!--
  支持运算符：Equal(=), NotEqual(!=), Contain(包含), NotContain, Empty, NotEmpty,
            Greater(>), GreaterEqual(>=), Less(<), LessEqual(<=),
            True, False, LengthGreater, LengthGreaterEqual, LengthLess, LengthLessEqual
  出口端口：true(分支1匹配), true_1(分支2匹配), ..., false(全不匹配)
  logic: 1=OR(任一满足), 2=AND(全部满足)
-->"""
    },
    {
        "type": "32", "name": "聚合 (Aggregator)",
        "desc": "多分支汇合：将多个分支的输出汇总到一个节点，运行时只取实际执行到的那个分支的值",
        "xml": """<!-- 聚合节点：多分支汇合 -->
<node id="180001" type="aggregator">
  <!-- 每个 mergeGroup 收集一条分支的输出 -->
  <group name="branch_0">
    <variable ref="160001.output"/>   <!-- 意图分支0 的输出 -->
  </group>
  <group name="branch_1">
    <variable ref="160002.output"/>   <!-- 意图分支1 的输出 -->
  </group>
  <group name="branch_default">
    <variable ref="160003.output"/>   <!-- default 分支的输出 -->
  </group>

  <out name="branch_0" type="string"/>
  <out name="branch_1" type="string"/>
  <out name="branch_default" type="string"/>
</node>
<!--
  用途：Selector/Intent 分支后汇合，下游节点统一引用 aggregator 的输出，避免空引用
  运行时只填充实际走到的分支，其他分支字段为 null
-->"""
    },
    {
        "type": "40", "name": "赋值 (Assigner)",
        "desc": "修改全局变量或工作流变量的值，left 指向变量路径，input 是新值",
        "xml": """<!-- 赋值节点：修改变量值 -->
<node id="190001" type="assigner">
  <!-- inputParameters 声明左值（变量路径）和右值（新值） -->
  <in name="counter" left="global_variable_app.counter">
    <!-- input 是新值：可 ref 上游或 literal 字面量 -->
    <value ref="150001.sum"/>
  </in>
  <in name="username" left="global_variable_app.username">
    <value literal="Alice"/>
  </in>

  <out name="isSuccess" type="boolean"/>
</node>
<!--
  left 路径：global_variable_app.<变量名>（全局变量）
  输出：isSuccess（赋值是否成功）
-->"""
    },
    # ===== 循环 =====
    {
        "type": "21", "name": "循环 (Loop)",
        "desc": "复合节点(blocks)，对数组迭代或按次数循环。体内可用 Break(19)/Continue(29)/LoopSetVariable(20)。三种模式：array(遍历数组)、count(固定次数)、infinite(无限循环)",
        "xml": """<!-- 循环节点(composite)：迭代执行体内 blocks -->
<node id="200001" type="loop">
  <!-- loopType: array(遍历数组) | count(固定次数) | infinite(无限) -->
  <param name="loopType" literal="array">array</param>
  <!-- loopCount: count 模式下的循环次数 -->
  <param name="loopCount" type="integer">10</param>

  <!-- array 模式：声明要遍历的数组 -->
  <in name="items" ref="170001.filtered_outputs"/>

  <!-- 循环变量（可选）：初始值，体内 LoopSetVariable 节点可读写 -->
  <param name="accumulator" type="integer" initialValue="0"/>

  <!-- 体内子节点 blocks（inline canvas） -->
  <blocks>
    <!-- 体内可用的特殊节点：LoopSetVariable(20) 读写循环变量 -->
    <node id="200010" type="setvar">
      <left>accumulator</left>                             <!-- 循环变量名 -->
      <right ref="200011.output"/>                         <!-- 新值 -->
    </node>

    <!-- 体内 LLM 节点：通过 loop-item / loop-index 引用当前迭代元素和索引 -->
    <node id="200011" type="llm">
      <in name="item" loop-item="true"/>                   <!-- 当前迭代元素 -->
      <in name="index" loop-index="true"/>                 <!-- 当前索引(0-based) -->
      <param name="prompt"><![CDATA[处理第 {{index}} 项：{{item}}]]></param>
      <out name="output" type="string"/>
    </node>

    <!-- 条件退出：选择器判断后走 Break 端口 -->
    <node id="200012" type="selector">
      <in name="output" ref="200011.output"/>
      <branch>
        <condition operator="Contain" logic="2">
          <left ref="200011.output"/>
          <right literal="STOP"/>
        </condition>
        <!-- true 端口 → Break -->
      </branch>
    </node>

    <!-- Break(19): 强制退出循环 -->
    <node id="200013" type="break"/>
    <!-- Continue(29): 跳过本次迭代，进入下一次 -->
    <node id="200014" type="continue"/>

    <node id="200015" type="llm">
      <in name="item" loop-item="true"/>
      <param name="prompt"><![CDATA[正常处理：{{item}}]]></param>
      <out name="output" type="string"/>
    </node>
  </blocks>

  <out name="all_outputs" type="list"/>
</node>
<!--
  体内子节点引用迭代元素：<in name="x" loop-item="true"/>，取当前 item
  体内子节点引用迭代索引：<in name="i" loop-index="true"/>，取当前 index
  Break(19): 放在 Selector 的 true/false 出口后，满足条件时退出循环
  Continue(29): 放在 Selector 出口后，满足条件时跳过本次
  LoopSetVariable(20): left=变量名, right=新值（可 ref 上游），读写循环累加变量
  输出：all_outputs（每轮迭代的末端输出 list）、final_变量名（循环变量最终值）
-->"""
    },
    {
        "type": "20", "name": "循环变量 (LoopSetVariable)",
        "desc": "在循环体内读写循环累加变量（仅 Loop/Batch 体内有效），left=变量名，right=新值",
        "xml": """<!-- 循环变量设置节点(type=20)：仅 Loop 或 Batch 体内使用 -->
<node id="200010" type="setvar">
  <left>counter</left>              <!-- 变量名（在循环节点的 variableParameters 中声明） -->
  <right ref="200009.output"/>      <!-- 新值：ref 引用体内节点输出，或 literal 写死 -->
</node>
<!--
  left: 变量名字符串（不是 ref）
  right: 新值，ref=体内节点ID.字段 或 literal="值"
  变量的最终值会出现在循环节点的输出中（final_counter 等）
-->"""
    },
    {
        "type": "19", "name": "循环中断 (Break)",
        "desc": "在循环体内强制退出整个循环（仅 Loop/Batch 体内有效），通常放在 Selector 的某个条件出口后",
        "xml": """<!-- Break 节点(type=19)：仅 Loop 或 Batch 体内使用，无条件退出循环 -->
<node id="200013" type="break"/>
<!--
  通常用法：Selector 判断某条件→true 端口→连到 Break
  注意：Break 和 Continue 没有 in/out，只需声明节点本身
-->"""
    },
    {
        "type": "29", "name": "循环继续 (Continue)",
        "desc": "在循环体内跳过当前迭代进入下一轮（仅 Loop/Batch 体内有效），通常放在 Selector 出口后",
        "xml": """<!-- Continue 节点(type=29)：仅 Loop 或 Batch 体内使用，跳过本轮迭代 -->
<node id="200014" type="continue"/>
<!--
  通常用法：Selector 判断某条件→true 端口→连到 Continue
  注意：Break 和 Continue 没有 in/out，只需声明节点本身
-->"""
    },
    # ===== 批处理 =====
    {
        "type": "28", "name": "批处理 (Batch)",
        "desc": "复合节点(blocks)，对数组逐元素并发执行体内逻辑，支持 batchSize/concurrentSize 控制并发度，输出聚合结果列表",
        "xml": """<!-- 批处理节点(composite)：逐元素并发执行体内 blocks -->
<node id="210001" type="batch">
  <!-- batchSize: 每批处理条数；concurrentSize: 并发数 -->
  <param name="batchSize" type="integer">5</param>
  <param name="concurrentSize" type="integer">3</param>

  <!-- 输入：要批处理的数组 -->
  <in name="items" ref="170001.filtered_outputs"/>

  <blocks>
    <!-- 体内节点：通过 loop-item / loop-index 引用当前元素和索引 -->
    <node id="210010" type="llm">
      <in name="item" loop-item="true"/>
      <in name="index" loop-index="true"/>
      <param name="prompt"><![CDATA[处理第 {{index}} 项：{{item}}]]></param>
      <out name="output" type="string"/>
    </node>
  </blocks>

  <out name="all_outputs" type="list"/>
  <out name="filtered_outputs" type="list"/>
</node>
<!--
  体内引用：loop-item="true" 取当前元素，loop-index="true" 取当前索引
  输出：all_outputs(所有结果list), filtered_outputs(过滤null后的结果), nth_output(第n个结果)
  体内也支持 Break(19) 和 Continue(29)
-->"""
    },
    # ===== 数据处理 =====
    {
        "type": "15", "name": "文本处理 (Text)",
        "desc": "文本拼接(concat)或分割(split)，concat 多输入拼成一个字符串，split 按分隔符切分成列表",
        "xml": """<!-- 文本处理节点 -->
<!-- 模式1：concat —— 拼接多个输入 -->
<node id="220001" type="text" method="concat">
  <in name="part1" ref="130001.output"/>
  <in name="part2" literal=" — "/>
  <in name="part3" ref="140001.result"/>
  <out name="string" type="string"/>
</node>

<!-- 模式2：split —— 按分隔符切割 -->
<node id="220002" type="text" method="split">
  <in name="text" ref="130001.output"/>
  <param name="separator" literal=",">,</param>
  <out name="list" type="list"/>
</node>
<!--
  concat 输出：string（拼接后的文本）
  split 输出：list（切割后的字符串数组）
  separator 默认是逗号
-->"""
    },
    {
        "type": "58", "name": "ToJson",
        "desc": "将上游多个字段组装成 JSON 字符串，输入字段一一映射到 JSON 对象的 key",
        "xml": """<!-- ToJson 节点：多个输入字段 → JSON 字符串 -->
<node id="230001" type="tojson">
  <in name="name" ref="130001.output"/>
  <in name="age" ref="140001.result"/>
  <in name="scores" ref="150001.filtered_outputs"/>
  <out name="output" type="string"/>
</node>
<!--
  输入：任意多个字段，每个 in 的 name 成为 JSON key，值成为 JSON value
  输出：output（JSON 字符串，如 {"name":"Alice","age":"25","scores":[...]}）
  典型用法：组装数据 → HTTP 请求的 body，或传给 run_script 的 payload
-->"""
    },
    {
        "type": "59", "name": "FromJson",
        "desc": "将 JSON 字符串解析为结构化字段，输入一个 JSON 字符串，输出按声明的字段名提取",
        "xml": """<!-- FromJson 节点：JSON 字符串 → 结构化字段 -->
<node id="240001" type="fromjson">
  <!-- 输入：JSON 字符串 -->
  <in name="input" ref="230001.output"/>

  <!-- 输出：按需声明要从 JSON 中提取的字段 -->
  <out name="name" type="string"/>
  <out name="age" type="integer"/>
  <out name="scores" type="list"/>
</node>
<!--
  输入：input（JSON 字符串，通常来自 HTTP 响应 body 或 ToJson 输出）
  输出：按 out 声明的字段名从 JSON 中提取对应值
  解析失败时降级返回原始字符串，不中断工作流
-->"""
    },
    # ===== 外部调用 =====
    {
        "type": "45", "name": "HTTP 请求 (HTTP)",
        "desc": "发起 HTTP 请求（GET/POST/PUT/DELETE），支持 headers/params/body/auth 配置，URL 和 body 中可用 {{}} 模板引用上游输出",
        "xml": """<!-- HTTP 请求节点 -->
<node id="250001" type="http">
  <!-- API 信息：method 和 url（url 中可用 {{变量}} 模板） -->
  <param name="method" literal="POST">POST</param>
  <param name="url"><![CDATA[https://api.example.com/v1/chat/completions]]></param>

  <!-- 请求头 -->
  <param name="Content-Type" literal="application/json" header="true">application/json</param>
  <param name="Authorization" header="true"><![CDATA[Bearer {{api_key}}]]></param>

  <!-- URL 查询参数 -->
  <param name="version" literal="v1" query="true">v1</param>

  <!-- 模板入参 -->
  <in name="api_key" ref="190001.api_key"/>
  <in name="body_data" ref="230001.output"/>

  <!-- 请求体（JSON body） -->
  <param name="bodyType" literal="json">json</param>
  <body><![CDATA[{{body_data}}]]></body>

  <!-- 超时和重试 -->
  <param name="timeout" type="integer">30</param>
  <param name="retryTimes" type="integer">2</param>

  <out name="body" type="string"/>
  <out name="statusCode" type="integer"/>
  <out name="headers" type="object"/>
</node>
<!--
  header="true" 的 param 作为请求头；query="true" 的 param 作为 URL 查询参数
  body 元素内的 CDATA 为请求体，支持 {{变量}} 模板
  输出：body（响应体字符串）, statusCode（HTTP 状态码）, headers（响应头JSON对象）
-->"""
    },
    {
        "type": "9", "name": "子工作流 (SubWorkflow)",
        "desc": "调用另一个已注册的工作流作为子流程，传入参数、获取结构化返回值",
        "xml": """<!-- 子工作流节点：调用另一个工作流 -->
<node id="260001" type="subworkflow">
  <!-- workflow: 目标工作流名（.agent/workflows/ 下的文件名，不含扩展名） -->
  <param name="workflow" literal="greet">greet</param>

  <!-- 输入：传给子工作流的参数（对应子工作流开始节点的 out 声明） -->
  <in name="name" ref="130001.output"/>

  <!-- 输出：子工作流结束节点返回的字段 -->
  <out name="greeting" type="string"/>
  <out name="output" type="string"/>
</node>
<!--
  workflow 参数：目标工作流的文件名（不含扩展名）
  输入字段对应子工作流开始节点(100001)声明的 out
  输出字段对应子工作流结束节点(900001)的 out
-->"""
    },
    # ===== 交互节点（工具模式下受限） =====
    {
        "type": "13", "name": "输出发送 (OutputEmitter)",
        "desc": "交互式输出：在工作流执行中途向外部发送消息（工具模式下仅收集输出，不会真正交互）",
        "xml": """<!-- 输出发送节点：向外部发送中间结果（工具模式下仅记录） -->
<node id="270001" type="output">
  <in name="message" ref="130001.output"/>
  <in name="data" ref="150001.result"/>
</node>
<!--
  交互模式下向用户发送消息；工具模式下输出被收集到 ctx.emitMessages
  通常和 InputReceiver(30) 配对使用，实现"中间输出-等待输入-继续执行"
-->"""
    },
    {
        "type": "30", "name": "输入接收 (InputReceiver)",
        "desc": "交互式输入：暂停工作流等待外部输入（工具模式下不支持，会报错）",
        "xml": """<!-- 输入接收节点：等待外部输入（⚠ 工具模式下不支持，会报错） -->
<node id="280001" type="input">
  <out name="user_response" type="string"/>
</node>
<!--
  ⚠ 仅交互模式（如 Coze 预览）可用，工具/Agent 调用模式下会抛出 WorkflowError
  如需在工具模式下实现"确认后再继续"，改用 Selector + 条件判断
-->"""
    },
    {
        "type": "31", "name": "注释 (Comment)",
        "desc": "纯注释节点，不参与执行，用于在画布上添加说明文字",
        "xml": """<!-- 注释节点：不参与执行，仅用于画布标注 -->
<node id="290001" type="comment">
  <content>这里是对后续逻辑的说明，不会被执行</content>
</node>
<!-- 注释节点在扫描和执行时均被跳过，不会产生任何输出 -->"""
    },
]



def list_workflow_nodes() -> str:
    """列出工作流所有可用节点类型（名称 + 类型码 + 简介）。
    先调用它了解节点全景，再用 query_workflow_node(type="3") 或 query_workflow_node(name="LLM") 查具体某个节点的完整 XML 示例。"""
    lines = ["=== 工作流可用节点（共 {} 种）===".format(len(_NODE_CATALOG)),
             "{:<6} {:<20} {}".format("type", "名称", "简介"),
             "-" * 70]
    for n in _NODE_CATALOG:
        lines.append("{:<6} {:<20} {}".format(n["type"], n["name"], n["desc"]))
    lines.append("")
    lines.append('用法：query_workflow_node(type="3") 或 query_workflow_node(name="LLM") 查看某节点的完整 XML 示例。')
    return "\n".join(lines)


def query_workflow_node(type: str = "", name: str = "") -> str:
    """查询某个工作流节点的完整 XML 示例（含输入/输出 schema 和字段说明）。
    type: 节点类型码（如 "3" 表示 LLM）；name: 节点名称模糊匹配（如 "LLM" 或 "循环"）。
    二选一，type 优先。先用 list_workflow_nodes 查看所有可用节点类型。

    特殊：type="4" 或 name="plugin" 时额外列出所有可用内置工具及其参数。"""
    if type:
        matches = [n for n in _NODE_CATALOG if n["type"] == type]
    elif name:
        nl = name.lower()
        matches = [n for n in _NODE_CATALOG if nl in n["name"].lower() or nl in n["desc"].lower()]
    else:
        return '请提供 type 或 name 参数。先用 list_workflow_nodes 查看所有可用节点。\n示例：query_workflow_node(type="3") 或 query_workflow_node(name="循环")'

    if not matches:
        hint = "可用 type：" + ", ".join(sorted(set(n["type"] for n in _NODE_CATALOG), key=lambda x: int(x)))
        return f"[未匹配] type={type}, name={name}\n{hint}\n先用 list_workflow_nodes 查看所有可用节点。"

    parts = []
    for n in matches:
        parts.append(f"=== {n['name']}（type={n['type']}）===")
        parts.append(f"用途：{n['desc']}")
        parts.append("")
        parts.append("--- XML 示例 ---")
        parts.append(n["xml"])
        parts.append("")

        # plugin 节点额外列出内置工具
        if n["type"] == "4":
            parts.append("--- 可用内置工具（LIGHT_TOOLS）---")
            parts.append(_builtin_tools_reference())

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

def to_ascii(text: str) -> str:
    r"""把字符串里的非 ASCII 字符（中文等）转成 \uXXXX 转义，ASCII 字符保留。
    用于生成 ASCII 安全文本（JSON 传输/存储），如 "贵州茅台" → 贵州茅台。"""
    return "".join(ch if ord(ch) < 128 else "\\u%04x" % ord(ch) for ch in (text or ""))


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


# web_search 的结构化输出（success 作为字段，供工作流 plugin 节点引用判断成功与否）
WEB_SEARCH_OUTPUTS = [
    {"name": "success", "type": "boolean", "description": "搜索是否成功"},
    {"name": "count", "type": "integer", "description": "结果条数"},
    {"name": "result", "type": "string", "description": "格式化的结果文本（标题/链接/摘要）"},
    {"name": "error", "type": "string", "description": "失败原因（成功时为空）"},
]

REAL_TOOLS = Toolbox(
    Tool(run_python),
    Tool(read_file),
    Tool(write_file),
    Tool(edit),
    Tool(list_dir),
    Tool(grep),
    Tool(web_search, outputs=WEB_SEARCH_OUTPUTS),
    Tool(open_url),
    Tool(read_workflow_spec),
    Tool(read_workflow_demo),
    Tool(list_workflow_nodes),
    Tool(query_workflow_node),
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
    Tool(to_ascii),
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
