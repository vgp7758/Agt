# -*- mode: python ; coding: utf-8 -*-
# Agt 桌面版打包 spec（spec s_d53311f8 Step 2）——onedir 自带 Python 运行时，下载即用。
# 构建：pyinstaller packaging/Agt.spec --noconfirm   （产物 dist/Agt/）
# 发布：release.py --desktop（打包 → zip → GitHub Releases，见 release 脚本）

import os
from pathlib import Path
from PyInstaller.utils.hooks import collect_submodules, collect_data_files

ROOT = Path(SPECPATH).parent          # 仓库根
SRC = ROOT / "src"

datas = [
    (str(SRC / "static"), "src/static"),                      # WebUI（index/stats/editor 等 html）
    (str(SRC / "assets"), "src/assets"),                      # 播种资产（preset/manifest/tools_builtin/workflows/nodes/agents）
]
hiddenimports = (
    collect_submodules("uvicorn")                              # uvicorn 动态 worker/loop 加载
    + collect_submodules("webview")                            # pywebview 平台后端（win: edgechromium/winforms）
    + collect_submodules("anyio")
    + [
        "engineio.async_client", "engineio.sync_client",
        "encodings.utf_8", "encodings.gbk", "encodings.ascii",  # run_python/嗅探解码（Win 冻结环境 encodings 常缺）
        "src.assets.tools_builtin",                             # 外置工具按目录扫描（非 import 收集）——
        "src.web_desktop",
    ]
)
# 外置工具/节点插件是 .py 数据文件（运行时 _import_fresh 动态加载）——随 datas 已带，
# 上面 hiddenimports 的包名只为保住包路径可见。

a = Analysis(
    [str(SRC / "desktop_entry.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
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
    icon=str(ROOT / "packaging" / "agt.ico") if (ROOT / "packaging" / "agt.ico").exists() else None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="Agt",                                                 # dist/Agt/（onedir）
)
