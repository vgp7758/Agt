"""_sniff_kind 单测 + /api/file-kind 端到端（真实文件）。"""
import sys, os, json, importlib
sys.path.insert(0, r"D:\AI_Usings\Agt\src")

# ---------- 1. 函数级：magic bytes 矩阵 ----------
import server
sk = server._sniff_kind
cases = [
    (b"\x89PNG\r\n\x1a\n" + b"\x00"*100, ("image", "", "image/png")),
    (b"\xff\xd8\xff\xe0" + b"\x00"*100, ("image", "", "image/jpeg")),
    (b"GIF89a" + b"\x00"*100, ("image", "", "image/gif")),
    (b"RIFF" + b"\x00\x00\x00\x00" + b"WAVE" + b"fmt ", ("audio", "", "audio/wav")),
    (b"ID3\x04\x00" + b"\x00"*100, ("audio", "", "audio/mpeg")),
    (b"OggS" + b"\x00"*100, ("audio", "", "audio/ogg")),
    (b"\x00\x00\x00\x18ftypmp42" + b"\x00"*100, ("binary", "", "video/mp4")),
    (b"\x1aE\xdf\xa3" + b"\x01\x00\x00\x00" + b"\x00"*100, ("binary", "", "video/x-matroska")),
    (b"\xef\xbb\xbfhello world", ("text", "utf-8-sig", "")),
    (b"\xff\xfeh\x00i\x00", ("text", "utf-16", "")),
    ("你好，世界 gb".encode("gbk"), ("text", "gbk", "")),
    ("plain ascii text".encode(), ("text", "utf-8", "")),
    (b"\x00\x01\x02\x03binary\x00stuff", ("binary", "", "")),
    (b"", ("text", "utf-8", "")),
]
for head, want in cases:
    got = sk(head)
    assert got == want, f"{head[:12]!r}: want {want} got {got}"
print(f"1. _sniff_kind 矩阵 {len(cases)} 例全过 ✅")

# ---------- 2. 端到端：/api/file-kind + /api/asset 嗅探 mime ----------
from fastapi.testclient import TestClient
import server as srv
# 指向真实 workspace（测试文件写进 test/）
ws = r"D:\AI_Usings\Agt"
srv._workspace = type("P", (), {"__truediv__": lambda s, x: __import__("pathlib").Path(ws, x),
                                 "resolve": lambda s: __import__("pathlib").Path(ws)})() if not isinstance(srv._workspace, __import__("pathlib").Path) else __import__("pathlib").Path(ws)
os.makedirs(r"D:\AI_Usings\Agt\tmp_sniff", exist_ok=True)
files = {
    r"tmp_sniff/photo_noext": (b"\xff\xd8\xff\xe0" + b"\x00"*200, "image", ""),
    r"tmp_sniff/note.dat": ("中文笔记 gbk 编码".encode("gbk"), "text", "gbk"),
    r"tmp_sniff/clip.bin": (b"\x00\x00\x00\x18ftypisom" + b"\x00"*200, "binary", ""),
    r"tmp_sniff/ascii.weird": (b"just plain ascii", "text", "utf-8"),  # 后缀 .weird 不在 mimetypes 表
}
for rel, (data, want_kind, want_enc) in files.items():
    with open(os.path.join(ws, rel), "wb") as f:
        f.write(data)
client = TestClient(srv.app, transport=__import__("fastapi.testclient", fromlist=["ASGITransport"]).ASGITransport(app=srv.app)) if False else None
import asyncio
def _get(route, params):
    key = route.split("/")[-1].replace("-", "_")
    fn = {"file_kind": srv.api_file_kind, "asset": srv.api_asset}[key]
    return asyncio.run(fn(**params))
for rel, (data, want_kind, want_enc) in files.items():
    d = _get("/api/file-kind", {"path": rel})
    if hasattr(d, "json"): d = d.body.decode()   # 兜底（HTMLResponse）
    if isinstance(d, str):
        import json as _j; d = _j.loads(d)
    assert d.get("kind") == want_kind, f"{rel}: want {want_kind} got {d}"
    if want_enc:
        assert d.get("encoding") == want_enc, f"{rel}: want enc {want_enc} got {d}"
    # /api/asset 的 mime 嗅探（.weird/.dat/.bin 后缀 mimetypes 不认识）
    resp = _get("/api/asset", {"path": rel})
    ct = getattr(resp, "media_type", "") or ""
    print(f"  {rel}: kind={d['kind']} enc={d.get('encoding') or '-'} asset_ct={ct}")
    if want_kind == "image":
        assert "image/" in ct, f"{rel} asset 应给 image/*，got {ct}"
    if want_kind == "binary" and "ftyp" in data[:8].decode("latin1"):
        assert "video/mp4" in ct, f"{rel} asset 应给 video/mp4，got {ct}"
# 越界安全
r3 = _get("/api/file-kind", {"path": "../../etc/passwd"})
assert "error" in r3, "路径穿越应被拒"
print("2. /api/file-kind + /api/asset 端到端 + 越界防护 ✅")
# 清理
import shutil; shutil.rmtree(r"D:\AI_Usings\Agt\tmp_sniff", ignore_errors=True)
print("\n全部 PASS")
