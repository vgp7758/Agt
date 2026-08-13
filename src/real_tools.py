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

import base64
import hashlib
import mimetypes
import os
import queue
import re
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

# 后台任务表：超时未完成的 run_python/run_shell 子进程转后台后注册在此
_bg_tasks: dict = {}


def _resolve(path: str) -> Path:
    """把路径解析到 workspace 内；越界则抛 PermissionError（会被 Tool.run 转成文本）。"""
    base = WORKSPACE.resolve()
    target = (base / path).resolve()
    try:
        target.relative_to(base)  # 不在 base 下会抛 ValueError
    except ValueError:
        raise PermissionError(f"拒绝访问 workspace 外的路径: {path}")
    return target


# ===== 文件版本（乐观锁）=====
# 行号类编辑工具（insert/delete/move）没有 old_string 自校验，靠 version 防止
# "模型脑子里的行号已过期 → 静默写错位置"。模型 read_file/grep 时拿到当前 version，
# 编辑时原样回传；服务端重新算一次当前 version 比对：相等→安全应用，不等→拒绝要求重读。
# 服务端无需持久化 file→version 映射——version 令牌由模型持有，进程重启后旧令牌自然
# 失配→触发重读，正是我们要的安全行为。

def _file_version(target: Path) -> str:
    """文件内容的版本号 = sha256(raw bytes) 前 12 位 hex。对任意字节变换都鲁棒。"""
    return hashlib.sha256(target.read_bytes()).hexdigest()[:12]


def _check_version(target: Path, version: str) -> tuple[bool, str, str]:
    """校验文件是否仍是模型读取时的版本。
    返回 (ok, current_version, err_msg)：ok=False 时 err_msg 是给模型的提示。"""
    if not version:
        return False, "", ("[缺 version] 行号类编辑必须传 version——即你上次 "
                           "read_file/grep 返回的 file_version。先读文件拿到它。")
    current = _file_version(target)
    if current != version:
        return False, current, (
            f"[版本过期] 文件已改动（你的 version={version}，当前={current}），"
            f"行号可能已位移。请重新 read_file/grep 取最新行号与 file_version 后再编辑。")
    return True, current, ""


def _py_check(target: Path) -> str:
    """对刚写入的 Python 文件做即时语法检查（compile，不解引用，不跑代码，零开销）。
    有语法错误返回可操作的报错行；通过则返回 ✅ 提示，让 Agent 知道语法已校验、
    无需再用 run_python/ast.parse 重复查语法（import/运行验证仍需另测）。"""
    if target.suffix not in (".py", ".pyw"):
        return ""
    try:
        code = target.read_text(encoding="utf-8")
        compile(code, str(target), "exec")
    except SyntaxError as e:
        return f"\n⚠️ 语法错误 {e.filename or target.name}:{e.lineno}:{e.offset} — {e.msg}"
    return f"\n✅ {target.name} 语法检查通过（write_file 已自动校验语法，无需再 run_python/ast.parse 复查语法；import/运行仍需另测）"
def _run_subprocess_streaming(args, name, shell=False):
    """运行子进程，实时流式输出 + 30 秒心跳进度。reader 线程兼容 Windows。
    通过 _tool_emit 回调推送 tool_stream / tool_progress 事件。"""
    proc = subprocess.Popen(
        args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
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

        # 超时 → 转后台（不杀进程，daemon 线程继续读输出）
        if elapsed > TOOL_TIMEOUT:
            # flush 最后的流式输出
            if stream_buf and _tool_emit:
                _tool_emit({"type": "tool_stream", "name": name, "text": "".join(stream_buf)})
                stream_buf = []
            # 注册后台任务，daemon 线程继续读输出
            bg_id = f"bg_{int(time.time()*1000)}"
            _bg_tasks[bg_id] = {
                "name": name, "proc": proc, "output": output_lines[:],
                "started_at": start, "finished": False, "returncode": None,
            }
            def _bg_reader(_bg_id=bg_id, _line_q=line_q, _task=_bg_tasks[bg_id]):
                while True:
                    try:
                        ln = _line_q.get(timeout=1.0)
                    except queue.Empty:
                        if _task["proc"].poll() is not None:
                            break
                        continue
                    if ln is None:
                        break
                    _task["output"].append(ln)
                _task["proc"].wait()
                _task["returncode"] = _task["proc"].returncode
                _task["finished"] = True
                if _tool_emit:
                    _tool_emit({"type": "tool_stream", "name": name,
                                "text": f"\n[后台任务 {_bg_id} 已完成，返回码={_task['returncode']}]"})
            threading.Thread(target=_bg_reader, daemon=True).start()
            return (f"[执行超过 {TOOL_TIMEOUT}s，已转后台运行（任务ID: {bg_id}）。\n"
                    f"进程继续运行，不阻塞当前工作。用 check_bg_task(\"{bg_id}\") 查看进度和结果。"
                    f"已捕获 {len(output_lines)} 行输出。]")

    proc.wait()
    # 最终 flush
    if stream_buf and _tool_emit:
        _tool_emit({"type": "tool_stream", "name": name, "text": "".join(stream_buf)})

    return "".join(output_lines).strip() or "(无输出)"


def run_python(code: str = "", file: str = "") -> str:
    """运行 Python，实时流式输出（支持长任务进度）。独立子进程执行。二选一：
    - code：一段内联 Python 代码（写临时文件再跑）；
    - file：运行一个已存在的 .py 文件（跑已保存的脚本用这个，别再用 subprocess 包壳）。
    """
    if file:
        target = _resolve(file)
        if not target.exists():
            return f"[文件不存在] {file}"
        return _run_subprocess_streaming([sys.executable, str(target)], f"run_python {file}")
    if not code:
        return "[参数缺失] run_python 需传 code（内联代码）或 file（.py 文件路径）"
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


def _number_lines(text: str) -> str:
    """给一段文本的每行加行号（宽度按总行数自适应），格式 `N│ 内容`，与 read_file/find_function 同款。
    recent-file 快照用它：模型看到带行号的最新全文 + 标签里的 version，即可直接按行号 insert/delete/move。
    超 4000 行首尾各留 2000 行；尾部用【真实行号】编号，模型能看到尾段真实位置。"""
    lines = text.splitlines()
    n = len(lines)
    w = len(str(n))
    if n <= 4000:
        return "\n".join(f"{i:>{w}}│ {ln}" for i, ln in enumerate(lines, 1))
    head = "\n".join(f"{i:>{w}}│ {ln}" for i, ln in enumerate(lines[:2000], 1))
    trail = "\n".join(f"{i:>{w}}│ {ln}" for i, ln in enumerate(lines[-2000:], n - 1999))
    return head + "\n... (共{}行，需全文调 read_file)".format(n) + "\n" + trail


def _md_headings(text: str) -> list:
    """提取 Markdown 的 ATX 标题（#~######），返回 [(行号1based, 层级, 标题), ...]。
    跳过 ``` / ~~~ 代码围栏里的 #。供 _md_snapshot 结构目录、wiki_list/wiki_tree 大纲复用。"""
    in_fence, fence = False, ""
    out = []
    for idx, raw in enumerate(text.splitlines()):
        s = raw.strip()
        if s[:3] in ("```", "~~~"):
            if not in_fence:
                in_fence, fence = True, s[:3]
            elif s == fence:
                in_fence = False
            continue
        if in_fence:
            continue
        m = re.match(r"^(#{1,6})\s+(.*?)\s*#*$", raw)
        if m:
            out.append((idx + 1, len(m.group(1)), m.group(2).strip()))
    return out


def _md_snapshot(text: str) -> str:
    """Markdown 快照：<structure> 结构目录（frontmatter + ATX 标题 → 行范围，缩进表层级，跳过代码
    围栏里的 #）+ <content> 干净正文（不带 N│ 行号——结构目录取代行号做 .md 的导航）。
    recent-file 和 read_file(.md) 都用它。超 4000 行时正文首尾截断、结构目录保持完整。"""
    lines = text.splitlines()
    n = len(lines)
    entries = []  # (start_1based, end_1based, label, level)  level=0 给 frontmatter
    frontmatter_end = 0   # 1-based：闭合 --- 所在行（0 = 无 frontmatter）
    if n >= 2 and lines[0].strip() == "---":
        close = next((j for j in range(1, n) if lines[j].strip() == "---"), None)
        if close is not None:
            entries.append((1, close + 1, "frontmatter", 0))
            frontmatter_end = close + 1
    # 标题（排除 frontmatter 区域，避免把 YAML 注释 # 当标题）
    headings = [(ln, lv, t) for (ln, lv, t) in _md_headings(text) if ln > frontmatter_end]
    for k, (ln, level, title) in enumerate(headings):
        nxt = next((ln2 for (ln2, lv2, _) in headings[k + 1:] if lv2 <= level), None)
        end = (nxt - 1) if nxt is not None else n   # 到下一个同级/更高级标题前
        entries.append((ln, end, title, level))
    # 结构目录文本（按层级缩进）
    struct_lines = []
    for a, b, label, level in entries:
        indent = "  " * (level - 1)
        rng = f"[L{a}-L{b}]" if a != b else f"[L{a}]"
        struct_lines.append(f"{indent}{rng} {label}")
    struct = "\n".join(struct_lines) or "(无 frontmatter / 标题)"
    # 正文（超长首尾截断，结构目录保持完整）
    if n <= 4000:
        body = text.rstrip("\n")
    else:
        body = ("\n".join(lines[:2000]) + f"\n... (共{n}行，需全文调 read_file)\n"
                + "\n".join(lines[-2000:])).rstrip("\n")
    return f"<structure>\n{struct}\n</structure>\n<content>\n{body}\n</content>"


def read_file(path: str, start_line: int = None, end_line: int = None,
              line_numbers: bool = True) -> str:
    """读取 workspace 内某个文件的内容（文本/Word/Excel/PDF 自动提取），末尾附 file_version。
    start_line/end_line: 只读指定行范围（1-based，含两端；不传=全文）。
    line_numbers: 默认 True，每行前加行号（宽度按本段最大行号自适应对齐），用于接下来要用
    insert/delete/move 按行号编辑的场景；传 False 得不含行号的纯文本。
    对 .md 文件特例：line_numbers=True 时返回 <structure> 结构目录 + <content> 干净正文
    （frontmatter/标题→行范围，结构取代行号做导航）；line_numbers=False 仍是纯文本。
    返回末尾的 file_version 是该文件当前的内容版本号——传给 insert/delete/move 的 version 参数；
    若编辑时版本对不上，说明文件已被改动、需重读。"""
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
    ver_footer = f"\n[file_version={_file_version(target)}]"
    if start_line is None and end_line is None:
        if line_numbers and target.suffix.lower() in {".md", ".markdown"}:
            return _md_snapshot(text) + ver_footer
        if line_numbers:
            w = len(str(total))
            body = "\n".join(f"{i+1:>{w}}│ {ln}" for i, ln in enumerate(lines))
            return f"[{path} 共 {total} 行]\n{body}{ver_footer}"
        return text + ver_footer
    start = max(1, start_line or 1) - 1
    end = min(total, end_line or total)
    if start >= total:
        return f"[行号越界] 文件共 {total} 行，请求 start_line={start_line}"
    selected = lines[start:end]
    header = f"[{path} L{start+1}-L{end}/{total}]"
    if line_numbers:
        w = len(str(end))
        body = "\n".join(f"{start+i+1:>{w}}│ {ln}" for i, ln in enumerate(selected))
    else:
        body = "\n".join(selected)
    return header + "\n" + body + ver_footer


def read_image(file: str) -> str:
    """读取一张图片文件，返回 data URL（系统会自动渲染成图片供视觉模型查看）。
    file 可以是裸文件名（如 c7_0.png：先在 cwd 找，再在 repo images/ 目录找），
    或相对/绝对路径（在 cwd 下解析，沙箱限定）。支持 png/jpg/gif/webp。
    仅视觉模型能查看返回的图；非视觉模型调用了也只会得到 <img> 占位。"""
    name = (file or "").strip().strip('"').strip("'")
    if not name:
        return "[未传文件名]"
    cands = []
    if os.sep in name or "/" in name or "\\" in name:   # 带路径：workspace 沙箱解析
        try:
            cands.append(_resolve(name))
        except Exception:
            pass
        cands.append(WORKSPACE / name)
    else:                                                # 裸文件名：cwd + repo images/
        cands.append(WORKSPACE / name)
        try:
            from session import repo_images_dir
            cands.append(repo_images_dir(WORKSPACE) / name)
        except Exception:
            pass
    for p in cands:
        try:
            if p.exists() and p.is_file():
                ext = (p.suffix.lstrip(".") or "png").lower()
                ext = "jpeg" if ext == "jpg" else ext
                mime = mimetypes.guess_type(str(p))[0] or f"image/{ext}"
                b64 = base64.b64encode(p.read_bytes()).decode()
                return f"data:{mime};base64,{b64}"
        except Exception:
            continue
    return f"[未找到图片] {file}（cwd 和 repo images/ 都没找到）"


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


def grep(pattern: str, path: str = ".", glob: str = None, regex: bool = True,
         context: int = 0, max_results: int = 50) -> str:
    """在 workspace 内搜索文件内容，返回带行号的匹配（可带上下文），并附每个文件的 file_version。
    pattern: 搜索模式，**默认按正则**（支持 a|b 多选一、. * 等元字符，与 ripgrep 一致）；
    要按字面匹配（把 pattern 当普通字符串）传 regex=False。path: 文件或目录(默认 workspace 根)。
    glob: 文件名过滤如 '*.js'；context: 每条命中前后各显示几行（默认 0=只显示匹配行）；
    max_results: 最多返回匹配数。每个文件头部的 file_version 传给 insert/delete/move 的 version 参数。"""
    import fnmatch
    import re
    root = _resolve(path)
    if not root.exists():
        return f"[路径不存在] {path}"
    try:
        rx = re.compile(pattern if regex else re.escape(pattern))
    except re.error as e:
        return f"[正则错误] {e}\n（pattern 含特殊字符？可传 regex=False 按字面匹配）"
    DOC_EXT = {".docx", ".xlsx", ".xlsm", ".xltx", ".pdf"}
    # path 是文件 → 只搜该文件；是目录 → 递归其下（rglob 在文件上返回空，故需分支）
    candidates = [root] if root.is_file() else sorted(root.rglob("*"))
    # 按文件聚合：rel -> (version, lines, [命中行号])
    files, scanned, total = {}, 0, 0
    capped = False
    for fp in candidates:
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
        lines = text.splitlines()
        hits = []
        for i, line in enumerate(lines, 1):
            if rx.search(line):
                hits.append(i)
                total += 1
                if total >= max_results:
                    capped = True
                    break
        if hits:
            rel = fp.relative_to(WORKSPACE).as_posix()
            files[rel] = (_file_version(fp), lines, hits)
        if capped:
            break
    if not files:
        return f"(扫描 {scanned} 个文件，未找到 '{pattern}')"
    c = max(0, int(context))
    parts = [f"扫描 {scanned} 个文件，匹配 {total} 处："]
    for rel, (ver, lines, hits) in files.items():
        n = len(lines)
        parts.append(f"── {rel}  (file_version={ver}) ──")
        for lineno in hits:
            if c:
                lo, hi = max(1, lineno - c), min(n, lineno + c)
                for j in range(lo, hi + 1):
                    mark = ">" if j == lineno else " "
                    parts.append(f"{mark} {rel}:{j}: {lines[j-1].rstrip()[:200]}")
            else:
                parts.append(f"> {rel}:{lineno}: {lines[lineno-1].rstrip()[:200]}")
    if capped:
        parts.append(f"...（已达 max_results={max_results}，截断；收紧 pattern 或调大 max_results）")
    return "\n".join(parts)


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
        # 精确匹配失败 → 回退：去每行【行尾】空白后做行级比对（唯一则接受；多处则不唯一）。
        # 只去行尾、不碰行首（缩进是 Python 语义）；tab↔空格不自动规整（tabstop 未知、易跨层级误配）。
        scope_lines = lines[s:e] if (start_line or end_line) else lines
        old_lines = old_string.splitlines()
        m = len(old_lines)
        fp = [ln.rstrip() for ln in scope_lines]
        ofp = [ln.rstrip() for ln in old_lines]
        starts = [i for i in range(len(fp) - m + 1) if fp[i:i + m] == ofp] if (m and m <= len(fp)) else []
        if not starts:
            where = f" L{s+1}-L{min(e,total) if (start_line or end_line) else total}" if (start_line or end_line) else ""
            return (f"[未找到]{where} 精确与去行尾空白后均未命中 old_string；"
                    f"常见原因：行尾空格、缩进 tab↔空格、或内容已改——重新 read_file 取准确文本")
        if len(starts) > 1 and not replace_all:
            return f"[不唯一] 去行尾空白后共匹配 {len(starts)} 处，请加更多上下文让 old_string 唯一，或设 replace_all=True"
        hits = starts if replace_all else starts[:1]
        repl = new_string.splitlines()
        for i in sorted(hits, reverse=True):
            scope_lines[i:i + m] = repl
        new_scope = "\n".join(scope_lines)
        if not (start_line or end_line) and content.endswith("\n") and not new_scope.endswith("\n"):
            new_scope += "\n"   # splitlines+join 吃掉了文末换行，补回（精确路径本就保留）
        new_content = (prefix + ("\n" if prefix else "") + new_scope + ("\n" if suffix else "") + suffix) if (start_line or end_line) else new_scope
        target.write_text(new_content, encoding="utf-8")
        msg = f"✅ 已替换 {len(hits)} 处（行尾空白容忍匹配，{path}" + (f" L{start_line}-L{end_line}" if start_line or end_line else "") + ")"
        if path.endswith(".py") or path.endswith(".pyw"):
            msg += _py_check(target)
        return msg
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


def _apply_lines(target: Path, new_lines: list, path: str, action_desc: str) -> str:
    """把 new_lines 写回文件（统一 \n 换行 + 末尾换行），返回带新 file_version 的确认串。"""
    target.write_text("\n".join(new_lines) + ("\n" if new_lines else ""), encoding="utf-8")
    msg = f"{action_desc}（现共 {len(new_lines)} 行）file_version={_file_version(target)}"
    if path.endswith(".py") or path.endswith(".pyw"):
        msg += _py_check(target)
    return msg


def insert(path: str, entries: list, version: str) -> str:
    """按行号在文件中【一处或多处】插入文本，单次原子写入——一次插多段用它，别在 run_python 里拼字符串。
    entries: 插入点数组，每项 {"line": 1-based 行号, "content": 文本(可多行)}；在该行之前插入；
             line <=0 或超过总行数则追加末尾。
    内部先按 line 排序、再【降序】应用（先插高位不扰动低位行号），故直接传 grep/read_file 查到的原始行号即可，无需自己算位移。
    需传 read_file/grep 返回的 file_version 校验（不匹配=文件已改、拒绝要求重读）；成功返回新 file_version。"""
    target = _resolve(path)
    if not target.exists():
        return f"[文件不存在] {path}"
    # 校验 entries
    if not isinstance(entries, list) or not entries:
        return f"[参数错误] entries 需为非空数组，收到 {type(entries).__name__}"
    norm = []
    for i, e in enumerate(entries):
        if not isinstance(e, dict):
            return f"[参数错误] entries[{i}] 需为对象 {{line, content}}，收到 {type(e).__name__}"
        ln = e.get("line")
        ct = e.get("content")
        if not isinstance(ln, int) or isinstance(ln, bool):
            return f"[参数错误] entries[{i}].line 需为整数，收到 {ln!r}"
        if not isinstance(ct, str):
            return f"[参数错误] entries[{i}].content 需为字符串，收到 {type(ct).__name__}"
        norm.append((ln, ct))
    ok, _cur, err = _check_version(target, version)
    if not ok:
        return err
    out = target.read_text(encoding="utf-8").splitlines()
    total = len(out)
    # 先按 line 排序（升序），再降序应用：先插大行号，不影响小行号位置
    norm.sort(key=lambda ec: ec[0], reverse=True)
    appended = 0
    for ln, ct in norm:
        block = (ct or "").splitlines()
        if ln <= 0 or ln > total:
            out.extend(block)             # 追加到末尾
            appended += 1
        else:
            out[ln - 1:ln - 1] = block    # 在第 ln 行之前插入
    ins_n = len(norm) - appended
    where = f"{ins_n} 处定点" + (f"+ {appended} 处追加" if appended else "")
    total_new_lines = sum(len((c or "").splitlines()) for _, c in norm)
    return "✅ " + _apply_lines(target, out, path,
                                f"已在 {path} 插入 {where}（共 {total_new_lines} 行）")


def delete(path: str, start_line: int, end_line: int, version: str) -> str:
    """按行号删除文件中一段连续的行（start_line~end_line，含两端，1-based）。删的是行不是文件。
    需传 read_file/grep 返回的 file_version 做版本校验：不匹配则文件已改动、会被拒绝要求重读。
    成功后返回新的 file_version，同轮后续编辑可直接用它当 version。"""
    target = _resolve(path)
    if not target.exists():
        return f"[文件不存在] {path}"
    ok, _cur, err = _check_version(target, version)
    if not ok:
        return err
    lines = target.read_text(encoding="utf-8").splitlines()
    total = len(lines)
    s = int(start_line)
    e = int(end_line)
    if s < 1 or s > total:
        return f"[行号越界] 文件共 {total} 行，start_line={start_line}（须 1~{total}）"
    if e < s:
        return f"[参数错误] end_line({end_line}) 不能小于 start_line({start_line})"
    e = min(total, e)   # 超出末尾则截到文件尾（友善：删到末尾）
    del lines[s - 1:e]
    return "✅ " + _apply_lines(target, lines, path,
                                f"已删除 {path} 第 {s}-{e} 行（共 {e - s + 1} 行）")


def move(path: str, start_line: int, end_line: int, dst_line: int, version: str) -> str:
    """按行号把一段行（start_line~end_line，含两端）原子搬到 dst_line 行之前——重构搬代码块用。
    需传 read_file/grep 返回的 file_version 做版本校验：不匹配则文件已改动、会被拒绝要求重读。
    dst_line 按原始行号理解（搬到自身范围内视为无操作）。成功后返回新的 file_version。"""
    target = _resolve(path)
    if not target.exists():
        return f"[文件不存在] {path}"
    ok, _cur, err = _check_version(target, version)
    if not ok:
        return err
    lines = target.read_text(encoding="utf-8").splitlines()
    total = len(lines)
    s = int(start_line)
    e = int(end_line)
    if s < 1 or s > total:
        return f"[行号越界] 文件共 {total} 行，start_line={start_line}（须 1~{total}）"
    if e < s:
        return f"[参数错误] end_line({end_line}) 不能小于 start_line({start_line})"
    e = min(total, e)
    block = lines[s - 1:e]
    nblock = len(block)
    d = int(dst_line)
    # 目标落在源块自身范围内 → 无操作（否则行号语义自相矛盾）
    if s <= d <= e + 1:
        return f"[无操作] dst_line={d} 落在源块 {s}-{e} 内，无需移动。file_version={_file_version(target)}"
    remaining = lines[:s - 1] + lines[e:]
    # 把原始行号语义映射到 remaining 的插入下标
    if d <= s:
        ins = max(0, d - 1)
    else:  # d > e：源块已在 dst 之前被整体移除，dst 在 remaining 中下标前移 nblock
        ins = max(0, d - 1 - nblock)
    ins = min(ins, len(remaining))
    remaining[ins:ins] = block
    return "✅ " + _apply_lines(target, remaining, path,
                                f"已把 {path} 第 {s}-{e} 行（{nblock} 行）搬到原第 {d} 行前")


def replace_lines(path: str, entries: list, version: str) -> str:
    """按行号【一处或多处】整段替换文件内容，单次原子写入——重写整个函数/大段代码用它（比 edit 省 token，不必重吐旧文本）。
    entries: 替换段数组，每项 {"range": [起, 止], "content": 新文本(可多行)}；range 1-based 含两端；
             [n,n] 替换第 n 行；content="" 删除该范围（等价 delete）。多处直接传 read_file/grep 查到的原始行号即可——
             内部按 range 起点降序应用（先改高位不扰动低位行号），各段 range 不许重叠。
    需传 read_file/grep 返回的 file_version 校验（不匹配=文件已改、拒绝要求重读）；成功返回新 file_version。"""
    target = _resolve(path)
    if not target.exists():
        return f"[文件不存在] {path}"
    if not isinstance(entries, list) or not entries:
        return f"[参数错误] entries 需为非空数组，收到 {type(entries).__name__}"
    norm = []
    for i, e in enumerate(entries):
        if not isinstance(e, dict):
            return f"[参数错误] entries[{i}] 需为对象 {{range, content}}，收到 {type(e).__name__}"
        rng = e.get("range")
        ct = e.get("content")
        if not (isinstance(rng, list) and len(rng) == 2
                and all(isinstance(x, int) and not isinstance(x, bool) for x in rng)):
            return f"[参数错误] entries[{i}].range 需为两个整数 [起, 止]，收到 {rng!r}"
        if not isinstance(ct, str):
            return f"[参数错误] entries[{i}].content 需为字符串，收到 {type(ct).__name__}"
        a, b = rng
        if a < 1 or a > b:
            return f"[参数错误] entries[{i}].range={rng} 非法：须 1≤起≤止"
        norm.append([a, b, ct])
    ok, _cur, err = _check_version(target, version)
    if not ok:
        return err
    lines = target.read_text(encoding="utf-8").splitlines()
    total = len(lines)
    for a, _b, _ in norm:
        if a > total:
            return f"[行号越界] 文件共 {total} 行，range 起={a}（须 ≤{total}）"
    for seg in norm:                 # 止点截到文件尾（友善，同 delete）
        seg[1] = min(total, seg[1])
    asc = sorted(norm, key=lambda x: x[0])   # 非重叠校验：升序看相邻段是否相交
    for (a1, b1, _), (a2, _b2, _) in zip(asc, asc[1:]):
        if a2 <= b1:
            return f"[参数错误] range 重叠：[{a1},{b1}] 与起点 {a2} 的段相交，请合并或调整行号"
    for a, b, ct in sorted(norm, key=lambda x: x[0], reverse=True):   # 降序：先改高位
        lines[a - 1:b] = (ct or "").splitlines()
    new_lines = sum(len((ct or "").splitlines()) for _a, _b, ct in norm)
    return "✅ " + _apply_lines(target, lines, path,
                                f"已在 {path} 整段替换 {len(norm)} 处（新内容共 {new_lines} 行）")


# ===== 函数定位（find_function）=====
# 比 grep 更适合"看某个函数的完整实现"：定位签名起始行后，按缩进(Python)或大括号配对
# (其它语言)找到函数结束行，整段带行号返回。大括号配对用字符级状态机，跳过字符串/注释，
# 避免把 "}" 或 // } 里的括号算进去。

_LANG_FAMILY = {
    ".py": "python",
    ".js": "brace", ".jsx": "brace", ".mjs": "brace", ".cjs": "brace",
    ".ts": "brace", ".tsx": "brace",
    ".cs": "brace", ".java": "brace", ".kt": "brace",
    ".c": "brace", ".h": "brace", ".cpp": "brace", ".cc": "brace",
    ".cxx": "brace", ".hpp": "brace", ".hxx": "brace", ".ino": "brace",
    ".go": "brace", ".rs": "brace", ".swift": "brace", ".php": "brace", ".scala": "brace",
}


def _lang_family(target: Path, lang: str = None) -> str:
    """返回 'python' / 'brace' / None。lang 显式指定时优先；否则按扩展名识别。"""
    if lang:
        return "python" if lang.lower() in ("py", "python") else "brace"
    return _LANG_FAMILY.get(target.suffix.lower())


def _indent(line: str) -> int:
    """行首空白（空格/Tab）的字符数。"""
    return len(line) - len(line.lstrip(" \t"))


def _extend_decorators(lines: list, idx: int) -> int:
    """Python：把 def 上方连续的、同缩进的 @装饰器 行并入起始。"""
    sig = _indent(lines[idx])
    j = idx - 1
    while j >= 0 and _indent(lines[j]) == sig and lines[j].lstrip().startswith("@"):
        j -= 1
    return j + 1


def _find_def_starts(lines: list, name: str, family: str) -> list:
    """返回所有疑似 NAME 定义的起始行 index（未确认是否有体）。"""
    import re
    nm = re.escape(name)
    starts = []
    if family == "python":
        rx = re.compile(rf"^[ \t]*(?:async[ \t]+)?def[ \t]+{nm}\b")
        starts = [i for i, ln in enumerate(lines) if rx.match(ln)]
    else:  # 大括号家族
        form_call = re.compile(rf"\b{nm}\s*(?:<[^>]*>)?\s*\(")   # 含简单泛型 <T>
        for i, ln in enumerate(lines):
            hit = False
            m = form_call.search(ln)
            if m:
                pre = ln[:m.start()].rstrip()
                if not (pre and pre[-1] == "."):   # 排除 obj.method( 这类调用
                    hit = True
            if not hit and re.search(rf"\b{nm}\s*=", ln) and \
                    ("=>" in ln or re.search(r"=\s*(?:async\s*)?function\b", ln)):
                hit = True   # const NAME = (...) =>  /  NAME = function
            if hit:
                starts.append(i)
    return starts


def _find_block_end_brace(lines: list, start_idx: int):
    """大括号家族：从 start_idx 扫，首个 { 开体、配对到其闭合 } 为止，返回结束行 index。
    扫描跳过字符串/注释；若开体前先撞到 ;（声明/调用，无体）返回 None。"""
    depth = 0
    opened = False
    in_str = None        # 当前字符串引号，或 None
    in_line = False      # // 行注释
    in_block = False     # /* */ 块注释
    i = start_idx
    while i < len(lines):
        line = lines[i]
        j, n = 0, len(line)
        while j < n:
            c = line[j]
            nxt = line[j + 1] if j + 1 < n else ""
            if in_line:
                break
            if in_block:
                if c == "*" and nxt == "/":
                    in_block = False
                    j += 2
                    continue
                j += 1
                continue
            if in_str is not None:
                if c == "\\":
                    j += 2
                    continue
                if c == in_str:
                    in_str = None
                j += 1
                continue
            if c == "/" and nxt == "/":
                in_line = True
                break
            if c == "/" and nxt == "*":
                in_block = True
                j += 2
                continue
            if c in ('"', "'", "`"):
                in_str = c
                j += 1
                continue
            if c == "{":
                depth += 1
                opened = True
            elif c == "}":
                if opened:
                    depth -= 1
                    if depth == 0:
                        return i
            elif c == ";":
                if not opened:
                    return None   # 开体前遇 ; → 声明/调用，无函数体
            j += 1
        in_line = False
        i += 1
    return None


def _find_block_end_indent(lines: list, start_idx: int) -> int:
    """Python：def 行缩进为基准；体 = 缩进更深的行(空行暂属体)，遇到缩进 ≤ 基准的非空行结束。
    末尾去掉所属的空行。返回结束行 index。"""
    sig = _indent(lines[start_idx])
    end = start_idx
    for i in range(start_idx + 1, len(lines)):
        ln = lines[i]
        if ln.strip() == "" or _indent(ln) > sig:
            end = i
        else:
            break
    while end > start_idx and lines[end].strip() == "":
        end -= 1
    return end


def find_function(name: str, path: str, lang: str = None, context: int = 0) -> str:
    """查找某个函数/方法的完整定义，返回带行号的函数体 + 行范围 + file_version（比 grep 更适合"看某函数完整实现"）。
    name: 函数/方法名（精确，非正则）；path: 文件或目录（目录则按语言扩展名扫描各文件，跨文件找定义）；
    lang: python/js/ts/cs/java/cpp/go...，不传则按扩展名识别；context: 函数体前后额外显示几行（默认0）。
    按 缩进(Python) / 大括号配对(其它) 自动定位起止行；返回的 [path L起-L止/总] 与 file_version 可直接喂给 insert/delete/move。"""
    target = _resolve(path)
    if not target.exists():
        return f"[路径不存在] {path}"
    if target.is_dir():
        exts = set(_LANG_FAMILY.keys())
        files = [p for p in sorted(target.rglob("*")) if p.is_file() and p.suffix.lower() in exts]
    else:
        files = [target]
    c = max(0, int(context))
    parts = []
    total_matches = 0
    for fp in files:
        family = _lang_family(fp, lang)
        if not family:
            continue
        try:
            text = fp.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        lines = text.splitlines()
        total = len(lines)
        blocks = []
        for s in _find_def_starts(lines, name, family):
            if family == "python":
                real_s = _extend_decorators(lines, s)
                e = _find_block_end_indent(lines, s)
            else:
                e = _find_block_end_brace(lines, s)
                if e is None:
                    continue   # 声明/调用，无体 → 跳过
                real_s = s
            blocks.append((real_s, e))
        if not blocks:
            continue
        rel = path if fp == target else fp.relative_to(WORKSPACE).as_posix()
        ver = _file_version(fp)
        for s, e in blocks:
            total_matches += 1
            lo, hi = max(0, s - c), min(total - 1, e + c)
            parts.append(f"[{rel} L{s + 1}-L{e + 1}/{total}]  file_version={ver}")
            w = len(str(hi + 1))
            for j in range(lo, hi + 1):
                parts.append(f"{j + 1:>{w}}│ {lines[j]}")
    if not parts:
        where = f"{path} 下" if target.is_dir() else f"{path} 中"
        return (f"(在{where}未找到 '{name}' 的函数定义)\n"
                f"可能：名字拼错 / 是无{{}}的表达式体箭头函数 / 仅声明无体 / 不支持的语言。可用 grep 按名字搜。")
    return f"找到 {total_matches} 处 '{name}' 的定义：\n" + "\n".join(parts)


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
    返回格式：<structure> 标题大纲（层级 + 行号）+ <content> 正文。
    从线上 git raw 读取（与本地 docs/ 同源），网络不通时回退本地 docs/。
    start: 在此标题目录中从第 start 个字符开始读正文(0-based)；
    max_chars: 本次最多返回字符数(默认 6000)。"""
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
    # .md 文件用 _md_snapshot：先输出 <structure> 标题大纲 + 行号，再 <content> 正文
    return _md_snapshot(text)


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
    Tool(run_python, param_descriptions={
        "code": "内联 Python 代码（多行，写临时文件再跑）。和 file 二选一。",
        "file": "已存在的 .py 文件路径（跑已保存的脚本传这个，别再用 subprocess 包壳）。和 code 二选一。",
    }),
    Tool(read_file, param_descriptions={
        "line_numbers": "默认 True=每行前加行号(宽度按本段最大行号自适应对齐)，便于接下来用 insert/delete/move 按行号编辑；False=纯文本",
    }),
    Tool(write_file),
    Tool(edit),
    Tool(insert, param_descriptions={
        "entries": "插入点数组，每项 {line: 1-based行号, content: 文本(可多行)}；在该行之前插入；line<=0或超过总行数=追加末尾",
        "version": "read_file/grep 返回的 file_version；不匹配说明文件已改、需重读",
    }),
    Tool(delete, param_descriptions={
        "start_line": "要删除的起始行号（1-based，含）",
        "end_line": "要删除的结束行号（1-based，含）",
        "version": "read_file/grep 返回的 file_version；不匹配说明文件已改、需重读",
    }),
    Tool(move, param_descriptions={
        "start_line": "要搬移的起始行号（1-based，含）",
        "end_line": "要搬移的结束行号（1-based，含）",
        "dst_line": "搬到这里之前（按原始行号理解）",
        "version": "read_file/grep 返回的 file_version；不匹配说明文件已改、需重读",
    }),
    Tool(replace_lines, param_descriptions={
        "entries": "替换段数组，每项 {range:[起,止](1-based含两端), content:新文本(可多行)}；[n,n]替换单行；content=\"\"删该范围；多处传原始行号即可(内部降序应用)",
        "version": "read_file/grep 返回的 file_version；不匹配说明文件已改、需重读",
    }),
    Tool(list_dir),
    Tool(grep, param_descriptions={
        "pattern": "搜索模式，默认按正则（支持 a|b 多选一、. * 等元字符，与 ripgrep 一致）",
        "regex": "True=按正则(默认)；False=按字面匹配（把 pattern 当普通字符串，不解释元字符）",
        "context": "每条命中前后各显示几行（默认 0=只显示匹配行）",
    }),
    Tool(find_function, param_descriptions={
        "name": "函数/方法名（精确匹配，非正则）",
        "path": "文件路径；也可传目录则扫描其下同语言文件（跨文件找定义）",
        "lang": "语言提示(python/js/ts/cs/java/cpp/go...)，不传按扩展名识别",
        "context": "函数体前后额外显示几行（默认 0）",
    }),
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
    Tool(read_image),
)

# 轻量工具（基础函数：plugin 节点 / 代码节点 / Agent 均可调；build_agent 注册进 agent.tools）
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
    hidden=True,
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
