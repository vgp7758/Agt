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
            # 迁移判定看【用户数据】而非目录存在性——launcher 会先建 %APPDATA%\Agt 写
            # recent_workspaces.json（logs/ 亦系统产物），目录存在≠已初始化（否则迁移被
            # 短路：桌面版空配置空 session，fallback 读到 workspace 的 models.py）。
            _USER_DATA = ("models.json", "settings.json", "mcp.json", "main.yml",
                          "repos", "remote_instances.json")
            try:
                if old.is_dir() and not any((new / f).exists() for f in _USER_DATA):
                    print(f"📦 首次桌面启动：迁移 {old} → {new}（一次性，含全部 repo 存档）…")
                    shutil.copytree(old, new, dirs_exist_ok=True)   # launcher 预建目录不阻塞
                    (new / ".migrated-from").write_text(str(old), encoding="utf-8")
            except OSError:
                pass   # 迁移失败不阻塞启动（空配置起步）
            return new
    return Path.home() / ".agt"


AGT_DIR = resolve_agt_home()

# 版本号唯一真源（桌面平铺打包形态没有 src 包——server.api_latest 等处从这取；
# src/__init__.py 反向导入保持 pip 侧 __version__ 一致）
VERSION = "0.26.4"
