"""log.py —— 会话级日志（跟 session 走，<name>.log 与 <name>.json 并排）。

标准 logging + 自定义 SessionLogHandler：
  - emit 写 ~/.agt/repos/<hash>/sessions/<session_name>.log（append，跨运行保留）
  - session name 未就绪（首轮进行中）→ 内存缓冲；name 就绪（_ensure_name / set_session）→ flush 并切到直写
  - set_session(workspace, name) 切换目标文件（切 session 时跟着切）

默认：文件写全量(DEBUG+)；控制台只输出 WARNING+（不刷屏，CLI 已有事件流）。
环境变量：AGT_LOG_CONSOLE=1 让控制台输出全量；AGT_LOG_LEVEL=DEBUG/INFO/WARNING 改级别。
零新依赖（纯标准 logging）。
"""
from __future__ import annotations

import logging
import os
import threading
from pathlib import Path
from typing import Optional

from session import REPOS_DIR, _repo_hash

_ROOT = "agt"
_FORMAT = "[%(asctime)s] [%(levelname)-5s] [%(shortname)-6s] %(message)s"
_DATEFMT = "%m-%d %H:%M:%S"

_initialized = False
_session_handler: Optional["SessionLogHandler"] = None


class _Fmt(logging.Formatter):
    """格式化时把 logger 名取末段（agt.llm → llm），控制台/文件更紧凑。"""
    def format(self, record: logging.LogRecord) -> str:
        record.shortname = record.name.split(".")[-1] if "." in record.name else record.name
        return super().format(record)


def session_log_path(workspace, name: str) -> Path:
    """某 session 的日志文件路径：~/.agt/repos/<hash>/sessions/<name>.log。"""
    return REPOS_DIR / _repo_hash(workspace) / "sessions" / f"{name}.log"


class SessionLogHandler(logging.Handler):
    """写到当前 session 的 <name>.log；name 未就绪时缓冲，就绪后 flush。线程安全。
    被 agent 在 set_session / session._ensure_name 时驱动（duck typing，session 不 import 本模块）。"""

    BUFFER_CAP = 2000  # name 就绪前的内存缓冲上限（防爆）

    def __init__(self):
        super().__init__()
        self._lock = threading.Lock()
        self._workspace: Optional[Path] = None
        self._name: str = ""
        self._buffer: list[str] = []
        self.setFormatter(_Fmt(_FORMAT, _DATEFMT))

    def set_session(self, workspace, name: str) -> None:
        """设定/切换当前 session。name 非空→打开对应 .log 并 flush 缓冲；name 空→缓冲模式。"""
        with self._lock:
            self._workspace = Path(workspace) if workspace else None
            self._name = (name or "").strip()
            if self._name and self._workspace:
                self._flush_locked()

    def current_path(self) -> Optional[Path]:
        """当前目标日志文件路径（name 未就绪返回 None）。"""
        with self._lock:
            return session_log_path(self._workspace, self._name) \
                if (self._name and self._workspace) else None

    def _flush_locked(self) -> None:
        """把缓冲追加进目标文件（调用方持锁）。失败静默——日志绝不影响主流程。"""
        if not self._buffer:
            return
        try:
            p = session_log_path(self._workspace, self._name)
            p.parent.mkdir(parents=True, exist_ok=True)
            with open(p, "a", encoding="utf-8") as f:
                f.write("".join(self._buffer))
            self._buffer.clear()
        except Exception:
            pass

    def emit(self, record: logging.LogRecord) -> None:
        try:
            line = self.format(record) + "\n"
        except Exception:
            return
        with self._lock:
            if self._name and self._workspace:
                try:
                    p = session_log_path(self._workspace, self._name)
                    p.parent.mkdir(parents=True, exist_ok=True)
                    with open(p, "a", encoding="utf-8") as f:
                        f.write(line)
                except Exception:
                    self._buffer.append(line)  # 写失败也保住这条，待下次 flush
            else:
                self._buffer.append(line)
                if len(self._buffer) > self.BUFFER_CAP:
                    del self._buffer[: len(self._buffer) - self.BUFFER_CAP]


class _ConsoleHandler(logging.StreamHandler):
    pass


def get_logger(name: str = "") -> logging.Logger:
    """返回 agt.<name>（name 留空=根 agt）。各模块用它拿自己的 logger。"""
    return logging.getLogger(_ROOT if not name else f"{_ROOT}.{name}")


def configure_logging(level: Optional[str] = None,
                      console: Optional[bool] = None) -> SessionLogHandler:
    """配置根 agt logger（幂等）。挂 SessionLogHandler(文件全量) + 控制台 handler。
    level:  None→读 AGT_LOG_LEVEL，默认 INFO。
    console: True=控制台全量；False=关闭控制台；None→默认 WARNING+（AGT_LOG_CONSOLE=1 可提为全量）。
    返回 SessionLogHandler（agent 用它 set_session 跟会话走）。"""
    global _initialized, _session_handler
    if level is None:
        level = os.environ.get("AGT_LOG_LEVEL", "INFO")
    # console: 参数优先；None 时 env 显式开启才算 True，否则保持 None（=默认 WARNING+）
    if console is None:
        env_c = os.environ.get("AGT_LOG_CONSOLE", "").strip().lower()
        if env_c in ("1", "true", "yes", "on"):
            console = True

    root = logging.getLogger(_ROOT)
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    root.propagate = False  # 不向 Python root 传播，避免重复输出

    if not _initialized:
        _session_handler = SessionLogHandler()
        _session_handler.setLevel(logging.DEBUG)  # 文件收全量
        root.addHandler(_session_handler)
        ch = _ConsoleHandler()
        ch.setFormatter(_Fmt(_FORMAT, _DATEFMT))
        root.addHandler(ch)
        _initialized = True

    ch = next((h for h in root.handlers if isinstance(h, _ConsoleHandler)), None)
    if ch is not None:
        if console is True:
            ch.setLevel(logging.DEBUG)
        elif console is False:
            ch.setLevel(logging.CRITICAL + 1)  # 实质关闭
        else:  # None = 默认：只 WARNING+ 上控制台
            ch.setLevel(logging.WARNING)

    return _session_handler


def get_session_handler() -> Optional[SessionLogHandler]:
    return _session_handler
