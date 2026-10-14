# -*- coding: utf-8 -*-
"""冒烟前置侦察：models.json 里有哪些可用模型（找免费/本地端点）+ main.yml 的 agent 装配配置。"""
import json, os, sys
sys.stdout.reconfigure(encoding="utf-8")

h = os.path.expanduser("~/.agt")

def scrub(o):
    if isinstance(o, dict):
        return {k: ("***" if any(t in k.lower() for t in ("key", "token", "secret")) else scrub(v))
                for k, v in o.items()}
    if isinstance(o, list):
        return [scrub(x) for x in o]
    return o

mp = os.path.join(h, "models.json")
if os.path.exists(mp):
    d = json.load(open(mp, encoding="utf-8"))
    print("== models.json keys:", list(d.keys()) if isinstance(d, dict) else type(d))
    print(json.dumps(scrub(d), ensure_ascii=False, indent=1)[:3000])

yp = os.path.join(h, "main.yml")
if os.path.exists(yp):
    t = open(yp, encoding="utf-8").read()
    print("== main.yml (first 1500 chars) ==")
    print(t[:1500])

sp = os.path.join(h, "settings.json")
if os.path.exists(sp):
    d = json.load(open(sp, encoding="utf-8"))
    print("== settings.json keys:", list(d.keys()))
    print(json.dumps(scrub(d), ensure_ascii=False, indent=1)[:1200])
