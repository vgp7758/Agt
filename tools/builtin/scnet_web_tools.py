"""scnet_web_tools.py —— SCNet 网页 API 直调工具（cookie 鉴权，跨全部区域）。

为什么需要它（2026-09-23 用户实测驱动）：
- MCP 的 scnet_notebook 走 AK/SK 通道且强制单区域（clusterId）——非昆山区域报
  「818218 计算用户不存在」，看不到山东/华中的实例；
- 网页控制台 `#/notebook` 页用的是另一组接口（cookie 鉴权 + **不带 clusterId**）：
    GET /acx/aimgt/notebook/list?page=1&size=N&showGroupJob=false   → 跨全部区域实例
    GET /acx/operation/resource/group/detail/list/all?clusterIds=…  → 各卡组（资源组）状态
  本工具直调这两个接口，一次拿到"所有区域的容器状态 + 各卡组可用卡"。

cookie 来源：$SCNET_COOKIE_FILE → ~/.agt/scnet_cookie.json（浏览器登录态导出）。
"""
from __future__ import annotations

import json
import os
import time
import urllib.parse
import urllib.request
from pathlib import Path

WEB_BASE = "https://www.scnet.cn/acx"
KNOWN_CLUSTERS = "11250,20057,20091,20058,20073,20084,20095"

_STATUS_ICON = {"Running": "🟢运行中", "Terminated": "⚫已关机", "Stopped": "⚫已关机",
                "Failed": "🔴失败", "Creating": "🟡创建中", "Starting": "🟡启动中",
                "Stopping": "🟡停止中"}


def _cookie_file() -> Path:
    env = os.environ.get("SCNET_COOKIE_FILE")
    if env:
        return Path(env)
    return Path.home() / ".agt" / "scnet_cookie.json"


def _web_get(path: str, timeout: int = 30) -> dict:
    """带 cookie 的 GET（返回解析后的 JSON；失败抛异常）。"""
    cf = _cookie_file()
    if not cf.exists():
        raise RuntimeError("缺少网页登录态 cookie：%s（浏览器登录 https://www.scnet.cn 后导出）" % cf)
    cookies = json.loads(cf.read_text(encoding="utf-8"))
    if isinstance(cookies, dict):
        cookies = cookies.get("cookies") or []
    cstr = "; ".join("%s=%s" % (c.get("name"), c.get("value")) for c in cookies if c.get("name"))
    req = urllib.request.Request(WEB_BASE + path, headers={
        "Cookie": cstr,
        "accept": "application/json, text/plain, */*",
        "version": "2.7.5",
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/153.0.0.0 Safari/537.36",
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _instances(size: int = 100) -> list:
    d = _web_get("/aimgt/notebook/list?page=1&size=%d&showGroupJob=false" % size)
    data = (d or {}).get("data") or {}
    return data.get("data") or []


def scnet_web_status(limit: int = 20, include_groups: bool = True) -> str:
    """SCNet 全部区域的容器实例状态 + 各卡组（资源组）可用卡——网页 API 直调（cookie 鉴权）。

    与 MCP 的 scnet_notebook 的区别：**跨全部区域**（MCP 走 AK/SK 单区域，非昆山报"计算用户不存在"）。
    limit: 每区域最多列几个实例（默认 20；运行中的实例永远优先列出）。
    include_groups: 是否附各卡组可用卡表。
    输出为已整形文本（区域分组 → 实例状态/组号/规格/镜像/任务线索），供装配/巡检消费。
    """
    try:
        items = _instances()
    except Exception as e:
        return "[scnet_web_status 错误] %s" % e
    # 按区域分组，运行中优先
    by_region = {}
    for it in items:
        cname = it.get("clusterName") or ("区域%s" % it.get("clusterId"))
        by_region.setdefault(cname, []).append(it)
    for k in by_region:
        by_region[k].sort(key=lambda x: (x.get("notebookStatus") != "Running",
                                         str(x.get("createTime") or "")))
    total, running = len(items), sum(1 for it in items if it.get("notebookStatus") == "Running")
    lines = ["[SCNet 容器状态 · %s] 共 %d 个实例，运行中 %d 个"
             % (time.strftime("%m-%d %H:%M"), total, running)]
    for region, its in sorted(by_region.items(), key=lambda kv: -sum(
            1 for x in kv[1] if x.get("notebookStatus") == "Running")):
        n_run = sum(1 for x in its if x.get("notebookStatus") == "Running")
        lines.append("  %s（%d 个%s）" % (region, len(its), "，%d 运行中" % n_run if n_run else ""))
        for it in its[:max(1, int(limit))]:
            st = str(it.get("notebookStatus") or "?")
            mark = _STATUS_ICON.get(st, "❔" + st)
            img = str(it.get("imageName") or "").split(":")[0][:40]
            acc = "%s%s/%s/%s" % (it.get("acceleratorNumber") or 1, it.get("acceleratorType") or "",
                                  it.get("cpuNumber") or "?", it.get("ramSize") or "?")
            upd = str(it.get("updateTime") or "")[5:16]
            cmd = str(it.get("command") or "").strip()
            duty = ""
            if "monitor.py" in cmd:
                seg = cmd.split("--ids", 1)[1].split("--")[0].strip().strip(",") if "--ids" in cmd else ""
                n = len([x for x in seg.split(",") if x.strip()])
                duty = " · 跑 monitor（%s）" % ("监控 %d 个任务" % n if n else "常驻")
            elif cmd:
                duty = " · cmd: " + cmd[:60]
            err = str(it.get("errorMessage") or "").strip()
            if err:
                duty += " · ⚠️" + err[:40]
            lines.append("    %s 组%s %s（%s · %s · 更新%s）%s"
                         % (mark, it.get("resourceGroupId") or "?", it.get("notebookName") or "?",
                            acc, img, upd, duty))
    if include_groups:
        try:
            g = _web_get("/operation/resource/group/detail/list/all?clusterIds=%s&resourceId=&isCpu=false"
                         % KNOWN_CLUSTERS)
            recs = (g or {}).get("data") or []
            lines.append("  卡组可用卡（区域 · 组 · 卡型 · 可租/总数）：")
            for r in recs:
                free, mx = r.get("maxFreeNum"), r.get("maxNum")
                if mx in (0, None) and not free:
                    continue
                lines.append("    %s · 组%s %s · %s · %s/%s%s"
                             % (r.get("clusterName") or "?", r.get("id"), r.get("resourceGroupName") or "?",
                                r.get("resourceName") or "?",
                                free if free is not None else "?", mx if mx is not None else "?",
                                (" · 余额%s卡时" % r.get("resourceBalance")) if r.get("resourceBalance") else ""))
        except Exception as e:
            lines.append("  （卡组状态取失败：%s）" % str(e)[:80])
    return "\n".join(lines)


def agt_register():
    return [{
        "name": "scnet_web_status",
        "func": scnet_web_status,
        "hidden": True,          # 工作流/装配专用（不占 ReAct 工具箱注意力）
        "group": "light",
        "brief": "SCNet 全区域容器状态（网页 API 跨区域，cookie 鉴权）",
        "version": 1,
        "params": {
            "limit": "每区域最多列几个实例（默认 20；运行中优先）",
            "include_groups": "是否附各卡组可用卡表（默认 True）",
        },
        "outputs": [{"name": "raw", "type": "string", "description": "整形后的状态文本"}],
    }]
