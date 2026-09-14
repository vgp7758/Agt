#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""画布格式 workflow JSON -> API 格式 prompt JSON 转换器（ComfyUI）。"""
import json
import sys

SKIP_TYPES = {"MarkdownNote", "Note"}  # 展示型节点，非执行节点


def convert(canvas_path, objinfo_path, out_path=None):
    canvas = json.load(open(canvas_path, encoding="utf-8"))
    info_raw = json.load(open(objinfo_path, encoding="utf-8"))
    info = {k: (v[k] if k in v else v) for k, v in info_raw.items()}

    links = {l[0]: l for l in canvas.get("links", [])}
    nodes = {n["id"]: n for n in canvas["nodes"]}
    out = {}
    for nid, node in nodes.items():
        ct = node["type"]
        if ct in SKIP_TYPES:
            continue
        spec = (info.get(ct) or {}).get("input", {})
        order = []
        for sec in ("required", "optional"):
            order += list((info.get(ct) or {}).get("input_order", {}).get(sec, []) or spec.get(sec, {}).keys())

        inputs = {}
        # 1) 已连接的输入端口
        for inp in node.get("inputs", []) or []:
            lid = inp.get("link")
            if lid is None:
                continue
            l = links[lid]
            # API 格式连接值是 [源节点id, 源输出slot索引(int)]——不是输出名（execution.py L935 r[val[1]]）
            inputs[inp["name"]] = [str(l[1]), l[2]]

        # 2) widget 值对位：只有「widget 型参数」才占位消费（连接型数据类型 MODEL/CLIP/VAE/
        #    IMAGE 等不在 widgets_values 里）；已连接 widget 的残留值仍占位但丢弃；
        #    带点子参数（format.codec）必占位且插到父参数后。
        spec_all = {}
        for sec in ("required", "optional"):
            spec_all.update(spec.get(sec, {}) or {})

        def is_widget_param(k):
            t = spec_all.get(k)
            if k not in spec_all:
                return True                      # 带点子参数（无 spec）默认占位
            if not (isinstance(t, list) and t):
                return False
            t0 = t[0]
            if isinstance(t0, list):
                return True                      # 枚举 COMBO（值在列表里）
            if isinstance(t0, str):
                if t0.startswith("COMFY_"):
                    return t0.startswith("COMFY_DYNAMICCOMBO")   # dynamiccombo 有 widget；autogrow 组本体无
                if t0 == "COMBO":                # 旧式 COMBO 类型名（BasicScheduler.scheduler 等）
                    return True
                if any(b in t0.split(",") for b in ("STRING", "INT", "FLOAT", "BOOLEAN")):
                    return True                  # 组合类型如 "INT, FLOAT"
            return False

        def _default_of(k):
            t = spec_all.get(k)
            if isinstance(t, list) and len(t) > 1 and isinstance(t[1], dict):
                d = t[1].get("default")
                if d is not None:
                    return d
                return t[1].get("defaultCtrl")
            return None

        port_names = {p["name"]: p.get("link") for p in (node.get("inputs") or [])}
        full = []
        for k in order:
            if not is_widget_param(k):
                continue
            full.append(k)
            for sub in port_names:              # 带点子参数插到父参数后
                if sub.startswith(k + "."):
                    full.append(sub)
        for sub in port_names:                   # 父参数不在 input_order 的子参数牍7 5尾
            if "." in sub and not any(sub == f for f in full):
                full.append(sub)
        wi = 0
        wv = list(node.get("widgets_values") or [])
        for k in full:
            if wi >= len(wv):
                break
            v = wv[wi]
            wi += 1
            if k in port_names:                 # 端口：null 才写入（连接的丢弃）
                if port_names[k] is None:
                    inputs[k] = v
            else:
                inputs.setdefault(k, v)          # 非端口普通 widget（LoadImage 等）

        # 3) 保险兜底：widget 型参数若仍缺失（对位遗漏），用 spec default 补齐
        for k in order:
            if k in inputs or not is_widget_param(k):
                continue
            d = _default_of(k)
            if d is not None:
                inputs[k] = d

        out[str(nid)] = {"class_type": ct, "inputs": inputs}

    if out_path:
        json.dump(out, open(out_path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    return out


if __name__ == "__main__":
    o = convert(sys.argv[1], sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else None)
    print("nodes:", len(o))
