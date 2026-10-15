"""一次性修复脚本：补齐残缺的 serve-web 缓存（16507d91be）。

背景（2026-10-15 事故）：
  webide 插件 _prefetch 的 shell=True + pre.kill() 杀不干净 → code-tunnel.exe 僵尸堆积
  → 多进程并发安装同一 commit → "保留最近 2 个"的 rmtree 删到正在使用/被占用的目录
  → 目录被删成只剩 node.exe + node_modules（node.exe 被占用删不掉）→ 之后的 CLI
    spawn "bin\..\out\server-main.js" 找不到路径 → 无限重试（用户看到的循环 panic）。
本脚本从微软 CDN 拉完整 server 包解压覆盖，补齐该目录。
"""
import pathlib
import sys
import time
import urllib.request
import zipfile

COMMIT = "16507d91be0dc1ce4f94c04d4d0430210b8685d9"
URL = ("https://vscode.download.prss.microsoft.com/dbazure/download/stable/"
       f"{COMMIT}/vscode-server-win32-x64-web.zip")
ZIP = pathlib.Path.home() / "AppData" / "Local" / "Temp" / f"vscode-server-web-{COMMIT[:8]}.zip"
DEST = pathlib.Path.home() / ".vscode" / "cli" / "serve-web" / COMMIT


def download():
    t0 = time.time()
    start = ZIP.stat().st_size if ZIP.exists() else 0
    req = urllib.request.Request(URL, headers={"Range": f"bytes={start}-"} if start else {})
    mode = "ab" if start else "wb"
    with urllib.request.urlopen(req, timeout=60) as r, open(ZIP, mode) as f:
        total = int(r.headers.get("Content-Length") or 0) + start
        last = start
        while True:
            chunk = r.read(1024 * 512)
            if not chunk:
                break
            f.write(chunk)
            got = f.tell()
            if got - last > 20 * 1024 * 1024:
                last = got
                print(f"  {got/1e6:.0f}/{total/1e6:.0f} MB  "
                      f"{(got-start)/max(1, time.time()-t0)/1e6:.1f} MB/s", flush=True)
    print(f"下载完成 {ZIP.stat().st_size/1e6:.1f} MB", flush=True)


def unzip():
    DEST.mkdir(parents=True, exist_ok=True)
    ok = skip = 0
    with zipfile.ZipFile(ZIP) as z:
        for info in z.infolist():
            try:
                z.extract(info, DEST)
                ok += 1
            except Exception as e:          # node.exe 被占用等 → 跳过（已存在）
                skip += 1
                if skip <= 5:
                    print(f"  跳过 {info.filename}: {type(e).__name__}", flush=True)
    print(f"解压完成：{ok} 项写入，{skip} 项跳过", flush=True)
    sm = DEST / "out" / "server-main.js"
    print("完整性:", "✅ out/server-main.js 在位" if sm.exists() else "❌ 仍缺 server-main.js", flush=True)


if __name__ == "__main__":
    print("目标:", DEST, flush=True)
    if not (ZIP.exists() and ZIP.stat().st_size > 200_000_000):
        download()
    else:
        print("已有完整 zip，跳过下载", flush=True)
    unzip()
    print("ALL-DONE", flush=True)