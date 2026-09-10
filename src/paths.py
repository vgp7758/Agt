"""paths.py —— 全局数据目录解析（spec s_d53311f8，唯一真源）。

**单一数据根**：始终 `~/.agt`（用户决策 2026-09-10）——桌面版与 CLI/pip 版、多个
桌面实例、Launcher 打开的不同 workspace 全部读写同一份数据，与 VS Code 插件/CLI
共用 `~/.claude` 同构：模型配置、settings、长期记忆、跨 repo 的 wiki/RAG 只需一份。

两级：AGT_HOME env（测试/多实例隔离）> ~/.agt（默认）。

历史：曾按桌面模式把根迁到 %APPDATA%\\Agt 并全量拷贝 ~/.agt —— 用户实测裁定为错
（每台把 Agt 装到别处的机器、每次 CI 云构建都要复制 GB 级存档，且与 CLI 版数据分叉）。
已回退；_reclaim_legacy_appdata() 把迁移出去的产物搬回 ~/.agt（只搬缺失项，~/.agt 优先）。

此前 Path.home()/".agt" 散落在 config/session/lsp_manager/restart_watchdog/
spec_tools/updater/feedback 七处独立定义——统一到本模块。
外置工具（assets/tools_builtin，无 src 可 import）各自轻量复制同款逻辑。
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path


def _reclaim_legacy_appdata() -> None:
    """回收桌面版历史迁移：%APPDATA%\\Agt 的用户数据搬回 ~/.agt（目标已有则跳过，
    从不覆盖）。%APPDATA%\\Agt 保留作系统侧产物（Launcher 的 recent 列表、桌面日志）。"""
    base = os.environ.get("APPDATA")
    if not base:
        return
    legacy = Path(base) / "Agt"
    home = Path.home() / ".agt"
    if not legacy.is_dir():
        return
    for name in ("models.json", "settings.json", "mcp.json", "main.yml", "models.py",
                 "repos", "memories", "logs", "remote_instances.json"):
        src, dst = legacy / name, home / name
        try:
            if not src.exists() or dst.exists():
                continue
            home.mkdir(parents=True, exist_ok=True)
            if src.is_dir():
                shutil.copytree(src, dst)
            else:
                shutil.copy2(src, dst)
            print(f"📦 数据根统一：回收 {src} → {dst}")
        except OSError:
            pass   # 回收失败不阻塞启动（用户在 ~/.agt 侧继续）


def resolve_agt_home() -> Path:
    env = os.environ.get("AGT_HOME", "").strip()
    if env:
        return Path(env)
    if os.environ.get("AGT_DESKTOP", "").strip() in ("1", "true", "yes"):
        _reclaim_legacy_appdata()
    return Path.home() / ".agt"


AGT_DIR = resolve_agt_home()

# 版本号唯一真源（桌面平铺打包形态没有 src 包——server.api_latest 等处从这取；
# src/__init__.py 反向导入保持 pip 侧 __version__ 一致）
VERSION = "0.26.5"
