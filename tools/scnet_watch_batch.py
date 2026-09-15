#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""兜底轮询：监控一批 prompt_id，完成即下载产物（本机主动拉，不依赖容器 monitor）。"""
import json
import os
import sys
import time
import urllib.parse
import urllib.request

GBASE = "https://c-2099459694942883841.ksai.scnet.cn:58043"
BATCH = "scnet_batch5.json"


def g(path, timeout=30):
    req = urllib.request.Request(GBASE + path, headers={"User-Agent": "agt"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def main():
    pids = json.load(open(BATCH, encoding="utf-8"))["pids"]
    done = {}
    t0 = time.time()
    print(f"[watch] 监控 {len(pids)} 单，最长 60 分钟", flush=True)
    while len(done) < len(pids) and time.time() - t0 < 3600:
        for pid in pids:
            if pid in done:
                continue
            try:
                h = g("/history/" + pid)
            except Exception as e:
                print(f"[warn] {pid[:8]} 查询失败 {str(e)[:60]}", flush=True)
                continue
            if pid not in h:
                continue
            e = h[pid]
            st = (e.get("status") or {}).get("status_str")
            if not (e.get("status") or {}).get("completed"):
                continue
            outs = []
            for nid, nd in (e.get("outputs") or {}).items():
                for kind in ("videos", "gifs", "images"):
                    for it in (nd.get(kind) or []):
                        outs.append(it)
            saved = []
            for it in outs:
                q = urllib.parse.urlencode({"filename": it["filename"], "subfolder": it.get("subfolder", ""), "type": it.get("type")})
                try:
                    with urllib.request.urlopen(GBASE + "/view?" + q, timeout=300) as r:
                        data = r.read()
                    os.makedirs("scnet_outputs", exist_ok=True)
                    dst = os.path.join("scnet_outputs", it["filename"])
                    open(dst, "wb").write(data)
                    saved.append(f"{os.path.basename(dst)}({len(data)/1e6:.2f}MB)")
                except Exception as ex:
                    saved.append(f"下载失败 {str(ex)[:40]}")
            done[pid] = st
            print(f"[{len(done)}/{len(pids)}] {pid[:8]} {st} → {saved}", flush=True)
        if len(done) < len(pids):
            time.sleep(30)
    print(f"\n[watch] 结束：{len(done)}/{len(pids)} 单完成，用时 {(time.time()-t0)/60:.1f} 分钟", flush=True)
    for k, v in done.items():
        print(f"   {k[:8]}: {v}", flush=True)


if __name__ == "__main__":
    main()
