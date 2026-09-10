# -*- mode: python ; coding: utf-8 -*-
# Launcher.spec —— 瘦启动器独立 onefile 打包（spec s_37494daf Step 3）。
# 构建：pyinstaller packaging/Launcher.spec --noconfirm --distpath packaging/dist
# 产物：packaging/dist/Launcher.exe（~11MB，纯 tkinter 标准库链）
# 发布布局：Launcher.exe 与 Agt/（主程序 onedir）平级分发——launcher 用
# 同目录 Agt.exe 定位主程序（源码态开发验证走 <repo>/dist/Agt/Agt.exe）。

from pathlib import Path

_SPEC = Path(SPECPATH)

a = Analysis(
    [str(_SPEC / "launcher.py")],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=[],
    hookspath=[],
    runtime_hooks=[],
    excludes=[
        # 瘦身：launcher 只需要 tkinter 链，排除一切重依赖（引擎模块本来就不在
        # pathex，此处兜底防意外收集）
        "numpy", "PIL", "pytest", "pip", "uvicorn", "webview", "requests",
        "aiohttp", "websockets", "pydantic", "yaml", "httpx", "anyio",
    ],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    name="Launcher",
    debug=False,
    strip=False,
    upx=False,                          # upx 误杀率高（SmartScreen 雪上加霜），与主 spec 一致
    console=False,                      # GUI 启动器无控制台（--auto 模式的 stdout 走重定向仍可用）
    icon=str(_SPEC / "agt.ico"),
    version=str(_SPEC / "version_file.txt"),
)
