# -*- coding: utf-8 -*-
"""pv-producer 专属工具：SVG 渲染管线（SVG→PNG→MP4）。
依赖：pip install cairosvg；ffmpeg 自动探测（PATH 或常见安装位，含剪映自带）。"""
import subprocess, shutil, glob
from pathlib import Path


def _ffmpeg() -> str:
    """探测 ffmpeg：PATH 优先，其次常见安装位（含剪映自带——够用且免安装）。"""
    w = shutil.which("ffmpeg")
    if w:
        return w
    for pat in (r"D:\Programs\JianyingPro\*\ffmpeg.exe", r"C:\ffmpeg*\bin\ffmpeg.exe",
                r"C:\Program Files\ffmpeg*\bin\ffmpeg.exe",
                r"C:\Users\vgp77\AppData\Local\Microsoft\WinGet\**\ffmpeg.exe"):
        hits = glob.glob(pat, recursive=True)
        if hits:
            return hits[0]
    return ""


def _vcodec(ff: str) -> str:
    """视频编码器探测：libx264 → h264_mf（Win 原生）→ mpeg4 降级链。
    （剪映自带 ffmpeg 是裁剪版无 libx264——2026-10-07 实锤）"""
    try:
        r = subprocess.run([ff, "-hide_banner", "-encoders"], capture_output=True, text=True, timeout=30)
        encs = r.stdout
    except Exception:
        encs = ""
    for c in ("libx264", "h264_mf", "mpeg4"):
        if c in encs:
            return c
    return "mpeg4"


def svg_to_png(svg: str, out_png: str, width: int = 1920) -> str:
    """SVG（字符串或 .svg 文件路径）→ PNG（width 控宽，默认 1920）。
    双后端：cairosvg 优先（快）；其原生 DLL 缺失（Windows 常态）或复杂滤镜渲染错
    自动降级 Playwright 无头渲染（渐变/滤镜/字体支持更全——动态 PPT 场景反而更优）。"""
    p = Path(out_png); p.parent.mkdir(parents=True, exist_ok=True)
    src = Path(svg).read_text(encoding="utf-8") if svg.strip().endswith(".svg") and Path(svg).exists() else svg
    try:
        import cairosvg
        cairosvg.svg2png(bytestring=src.encode("utf-8"), write_to=str(p), output_width=width)
        return f"✅ {p}（cairosvg · {p.stat().st_size//1024}KB）"
    except Exception:
        pass   # cairo 原生库缺失 / 复杂滤镜 → Playwright 兜底
    from playwright.sync_api import sync_playwright
    html = "<body style='margin:0;background:#0b1220'><div id=w>" + src + "</div></body>"
    with sync_playwright() as pw:
        # channel=msedge：直接用系统 Edge（免 playwright 浏览器下载——网络断连场景稳）
        try:
            b = pw.chromium.launch(channel="msedge")
        except Exception:
            b = pw.chromium.launch()   # 兜底：已下载的 chromium
        pg = b.new_page(viewport={"width": width, "height": 1080})
        pg.set_content(html)
        pg.wait_for_timeout(80)   # 字体/布局稳定
        pg.locator("#w").screenshot(path=str(p))
        b.close()
    return f"✅ {p}（playwright · {p.stat().st_size//1024}KB）"


def pngs_to_video(png_dir: str, out_mp4: str, fps: int = 24, audio: str = "") -> str:
    """PNG 序列（目录内按文件名序）→ mp4。audio 可选混入 wav/mp3。ffmpeg 子进程。"""
    _ff = _ffmpeg()
    if not _ff:
        return "[缺依赖] ffmpeg 未找到（PATH 及常见安装位均无）"
    d = Path(png_dir); out = Path(out_mp4); out.parent.mkdir(parents=True, exist_ok=True)
    cmd = [_ff, "-y", "-framerate", str(fps), "-i", str(d / "frame_%04d.png")]
    _vc = _vcodec(_ff)
    if audio:
        cmd += ["-i", audio, "-c:v", _vc, "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-shortest", "-movflags", "+faststart"]
    else:
        cmd += ["-c:v", _vc, "-pix_fmt", "yuv420p", "-movflags", "+faststart"]
    cmd.append(str(out))
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    return f"✅ {out}（{out.stat().st_size//1024}KB）" if r.returncode == 0 else f"[ffmpeg 失败] {r.stderr[-400:]}"


def concat_videos(mp4_list: str, out_mp4: str) -> str:
    """把多个分镜 mp4 无损拼接（逗号分隔路径）。"""
    _ff = _ffmpeg()
    if not _ff:
        return "[缺依赖] ffmpeg 未找到"
    lst = Path("_concat.txt")
    lst.write_text("\n".join(f"file '{Path(x).resolve().as_posix()}'" for x in mp4_list.split(",") if x.strip()), encoding="utf-8")
    r = subprocess.run([_ff, "-y", "-f", "concat", "-safe", "0", "-i", str(lst),
                        "-c", "copy", str(Path(out_mp4))], capture_output=True, text=True, timeout=600)
    lst.unlink(missing_ok=True)
    return f"✅ {out_mp4}" if r.returncode == 0 else f"[concat 失败] {r.stderr[-300:]}"


def mix_audio(video: str, audio: str, out_mp4: str, audio_delay: float = 0.0) -> str:
    """给视频混音轨（旁白/BGM）；audio_delay 秒起播。"""
    _ff = _ffmpeg()
    if not _ff:
        return "[缺依赖] ffmpeg 未找到"
    r = subprocess.run([_ff, "-y", "-i", video, "-itsoffset", str(audio_delay), "-i", audio,
                        "-c:v", "copy", "-c:a", "aac", "-map", "0:v:0", "-map", "1:a:0",
                        "-shortest", str(Path(out_mp4))], capture_output=True, text=True, timeout=600)
    return f"✅ {out_mp4}" if r.returncode == 0 else f"[mix 失败] {r.stderr[-300:]}"


def agt_register():
    return [
        {"name": "svg_to_png", "func": svg_to_png, "hidden": False, "group": "pv", "version": 1,
         "description": "SVG（字符串或文件路径）→ PNG（width 控宽，默认 1920）"},
        {"name": "pngs_to_video", "func": pngs_to_video, "hidden": False, "group": "pv", "version": 1,
         "description": "PNG 序列目录 → mp4（fps 默认 24；audio 可选混音）"},
        {"name": "concat_videos", "func": concat_videos, "hidden": False, "group": "pv", "version": 1,
         "description": "多个分镜 mp4 无损拼接（逗号分隔路径）"},
        {"name": "mix_audio", "func": mix_audio, "hidden": False, "group": "pv", "version": 1,
         "description": "给视频混音轨（audio_delay 秒起播）"},
    ]
