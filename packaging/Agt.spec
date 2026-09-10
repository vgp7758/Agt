# -*- mode: python ; coding: utf-8 -*-
# Agt 桌面版打包 spec（spec s_d53311f8 Step 2）——onedir 自带 Python 运行时，下载即用。
# 构建：pyinstaller packaging/Agt.spec --noconfirm   （产物 dist/Agt/）
# 发布：release.py --desktop（打包 → zip → GitHub Releases，见 release 脚本）

import os
from pathlib import Path
from PyInstaller.utils.hooks import collect_submodules

ROOT = Path(SPECPATH).parent          # 仓库根
SRC = ROOT / "src"

datas = [
    (str(SRC / "static"), "static"),                         # WebUI（index/stats/editor 等 html）——
    (str(SRC / "assets"), "assets"),                         # 播种资产（preset/manifest/tools_builtin/…）
    # ↑ 平铺形态（spec s_d53311f8 修 #1）：Analysis pathex=SRC → 模块以顶层名收集（config/session/
    # chat…），与 pip 运行时 src/__init__ 的 sys.path hack 同构；datas 也平铺到 _internal/
    # 根（config.py 的 Path(__file__).parent/"assets" 在 _internal/assets 命中）。
]
hiddenimports = (
    collect_submodules("uvicorn")                              # uvicorn 动态 worker/loop 加载
    + collect_submodules("webview")                            # pywebview 平台后端（win: edgechromium/winforms）
    + collect_submodules("anyio")
    + [
        "engineio.async_client", "engineio.sync_client",
        "encodings.utf_8", "encodings.gbk", "encodings.ascii",  # run_python/嗅探解码（Win 冻结环境常缺）
        "web_desktop",
        "workflow_node_api",                                   # 节点插件（assets/nodes/*.py 被 _import_fresh 动态
                                                               # 加载）的公共 API——静态分析看不到动态 import，须显式收集
    ]
)
# 外置工具/节点插件是 .py 数据文件（运行时 _import_fresh 动态加载）——随 assets datas 已带。

a = Analysis(
    [str(SRC / "desktop_entry.py")],
    pathex=[str(SRC)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[str(ROOT / "packaging" / "hooks")],   # 空 hook-workflow 覆盖社区 hook（同名 PyPI 包与本项目模块冲突）
    runtime_hooks=[],
    excludes=["tkinter", "matplotlib", "pytest", "pip"],       # 瘦身（未用/重）
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Agt",
    debug=False,
    strip=False,
    upx=False,                                                  # upx 误杀率高（SmartScreen 雪上加霜）
    console=False,                                              # 桌面应用无控制台（日志写文件）
    icon=str(ROOT / "packaging" / "agt.ico"),
    version=str(ROOT / "packaging" / "version_file.txt"),
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="Agt",                                                 # dist/Agt/（onedir）
)
