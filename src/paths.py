"""paths.py —— 全局数据目录解析（spec s_d53311f8 Step 2，唯一真源）。

三级：AGT_HOME env（测试/多实例）> 桌面模式 %APPDATA%\\Agt > ~/.agt（默认）。
此前 Path.home()/".agt" 散落在 config/session/lsp_manager/restart_watchdog/
spec_tools/updater/feedback 七处独立定义——桌面模式数据目录迁移时统一到本模块，
config.py 的迁移逻辑也移来此处（config 不再自持，避免 session→config 循环 import）。
外置工具（assets/tools_builtin，无 src 可 import）各自轻量复制三级逻辑（同款语义）。
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path


def resolve_agt_home() -> Path:
    env = os.environ.get("AGT_HOME", "").strip()
    if env:
        return Path(env)
    if os.environ.get("AGT_DESKTOP", "").strip() in ("1", "true", "yes"):
        base = os.environ.get("APPDATA")
        if base:
            new = Path(base) / "Agt"
            old = Path.home() / ".agt"
            try:
                if old.is_dir() and not new.exists():
                    shutil.copytree(old, new)   # 存量迁移：一次性（new 存在即跳过）
                    (new / ".migrated-from").write_text(str(old), encoding="utf-8")
            except OSError:
                pass   # 迁移失败不阻塞启动（空配置起步）
            return new
    return Path.home() / ".agt"


AGT_DIR = resolve_agt_home()
