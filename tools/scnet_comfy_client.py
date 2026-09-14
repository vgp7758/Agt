#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SCNet 容器内 ComfyUI 的本地批量任务客户端（agt 工具侧）。

用法示例：
    # 单次：提交一个 workflow（API 格式 JSON），轮询完成，下载产物
    python tools/scnet_comfy_client.py \
        --url https://c-xxxx.zzai.scnet.cn:58043/ \
        --wf /path/to/workflow_api.json \
        --outdir ./scnet_outputs

    # 批量：同一 workflow 模板，对每个 prompt 改某个节点字段后逐个 enqueue
    python tools/scnet_comfy_client.py \
        --url https://c-xxxx.zzai.scnet.cn:58043/ \
        --wf /path/to/workflow_api.json \
        --batch 'positive::a cat::a dog::a car' --edit-node 6 --edit-field text \
        --outdir ./scnet_outputs

机制（ComfyUI HTTP API，无鉴权）：
    POST {base}/prompt          提交 workflow，返回 prompt_id（入队）
    GET  {base}/history/{id}    轮询执行结果（含 outputs 文件清单）
    GET  {base}/view?filename=&subfolder=&type=   下载产物文件
    POST {base}/upload/image    上传参考图（多图参考工作流用）

⚠️ workflow JSON 必须是「导出 API 格式」(节点 id -> 输入 dict)，不是画布存盘的普通 JSON。
"""
import argparse
import json
import time
import uuid
import urllib.request
import urllib.parse
import os
import sys


def _http(base, path, method="GET", data=None, timeout=60):
    url = base.rstrip("/") + path
    body = None
    headers = {"User-Agent": "agt-scnet-comfy"}
    if data is not None:
        if isinstance(data, (dict, list)):
            body = json.dumps(data).encode()
            headers["Content-Type"] = "application/json"
        else:
            body = data
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
        ct = r.headers.get("Content-Type", "")
        if "json" in ct:
            return json.loads(raw.decode("utf-8", "replace"))
        return raw


def enqueue(base, workflow, client_id=None):
    """提交一个 workflow，返回 prompt_id。"""
    client_id = client_id or str(uuid.uuid4())
    r = _http(base, "/prompt", method="POST", data={"prompt": workflow, "client_id": client_id})
    if "prompt_id" not in r:
        raise RuntimeError("enqueue 失败: %s" % r)
    return r["prompt_id"]


def poll(base, prompt_id, interval=5, max_wait=3600):
    """轮询直到出现结果或超时。返回 history 里该 prompt 的完整输出体。"""
    t0 = time.time()
    while time.time() - t0 < max_wait:
        try:
            h = _http(base, "/history/%s" % prompt_id, timeout=20)
            if prompt_id in h:
                return h[prompt_id]
        except Exception:
            pass
        time.sleep(interval)
    raise TimeoutError("poll 超时: %s（%ds）" % (prompt_id, max_wait))


def collect_outputs(history_entry):
    """从 history entry 里挖出全部产物文件（图片/视频/gif），返回 [(filename, subfolder, type)]。"""
    out = []
    for _nid, nd in (history_entry.get("outputs") or {}).items():
        for kind in ("images", "gifs", "videos"):
            for it in nd.get(kind, []) or []:
                out.append((it.get("filename"), it.get("subfolder", ""), kind.rstrip("s")))
    return out


def download(base, filename, subfolder, ftype, outdir):
    q = urllib.parse.urlencode({"filename": filename, "subfolder": subfolder, "type": ftype})
    raw = _http(base, "/view?%s" % q, timeout=300)
    os.makedirs(outdir, exist_ok=True)
    dst = os.path.join(outdir, filename.replace("/", "_"))
    with open(dst, "wb") as f:
        f.write(raw)
    return dst


def run_one(base, workflow, outdir):
    pid = enqueue(base, workflow)
    print("[enqueue] prompt_id=%s" % pid, file=sys.stderr)
    entry = poll(base, pid)
    outs = collect_outputs(entry)
    saved = []
    for fn, sub, typ in outs:
        p = download(base, fn, sub, typ, outdir)
        saved.append(p)
        print("[download] %s" % p, file=sys.stderr)
    if not saved:
        status = entry.get("status", {})
        print("[warn] 无产物，status=%s" % json.dumps(status, ensure_ascii=False)[:300], file=sys.stderr)
    return pid, saved


def apply_batch(workflow, edit_node, edit_field, value):
    """深拷贝 workflow 后改指定节点的指定字段。"""
    import copy
    w = copy.deepcopy(workflow)
    node = w.get(edit_node) or w.get(str(edit_node))
    if node is None:
        raise KeyError("节点 %s 不存在" % edit_node)
    node["inputs"][edit_field] = value
    return w


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", required=True, help="ComfyUI 公网 URL（如 https://c-xxx.zzai.scnet.cn:58043/）")
    ap.add_argument("--wf", required=True, help="workflow_api.json 路径")
    ap.add_argument("--outdir", default="./scnet_outputs")
    ap.add_argument("--batch", help='批量：形如 "字段::值1::值2::值3"（按 --edit-field 逐值 enqueue）')
    ap.add_argument("--edit-node", help="批量模式下要改参数的节点 id")
    ap.add_argument("--edit-field", default="text", help="批量模式下要改的字段名（默认 text）")
    ap.add_argument("--interval", type=int, default=5, help="轮询间隔秒（默认 5）")
    args = ap.parse_args()

    wf = json.load(open(args.wf, encoding="utf-8"))
    if args.batch:
        parts = args.batch.split("::")
        field, values = parts[0], parts[1:]
        for v in values:
            w = apply_batch(wf, args.edit_node, field, v)
            print("== batch %s=%r ==" % (field, v))
            run_one(args.url, w, args.outdir)
    else:
        run_one(args.url, wf, args.outdir)


if __name__ == "__main__":
    main()