"""team_tools.py —— 团队管理工具组（night_tasks #4 2026-09-02：自动拉起成员/恢复 session/组网）。

用户痛点：多实例组网时成员的经验绑在各自 session 上——手动逐个拉起太被动。
team_up 按清单一键完成：启动成员进程（各自 repo 的 agt-web）→ 等端口就绪 →
remote_connect 组网 → 恢复指定 session。team_status 总览成员在线/模型/忙闲。

清单格式（YAML，样例见 .agent/team.example.yml）：
  members:
    - name: director          # server_id（组网标识，remote_ask/message 用它）
      repo: D:/Projects/X/director   # 成员 repo（agt-web 的 cwd）
      port: 9201
      session: ""             # 要恢复的 session 名（空=不主动恢复，保持其自动行为）
      role: 导演——把控叙事与分工

组网拓扑：星型（成员都连到本实例——本实例的 Agent 用 remote_ask/remote_message
与各成员通信；成员间经本实例中转）。成员进程独立于本进程（崩了互不影响）。
改完本文件用 /reload tools 热加载。
"""
from pathlib import Path
import json as _json

_WORKSPACE = Path.cwd()


def _is_win():
    import os
    return os.name == "nt"


def _load_manifest(path: str) -> dict:
    """读成员清单（yml）。返回 {"members": [...]}；不存在/无 members 抛 ValueError（带指引）。"""
    import yaml
    p = Path(path)
    if not p.is_absolute():
        p = _WORKSPACE / path
    if not p.exists():
        raise ValueError(f"清单不存在：{p}（参考 .agent/team.example.yml 建一份 .agent/team.yml）")
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    members = data.get("members") or []
    if not isinstance(members, list) or not members:
        raise ValueError("清单无 members 列表（或为空）")
    for m in members:
        if not m.get("name") or not m.get("repo") or not m.get("port"):
            raise ValueError(f"成员缺 name/repo/port 必填项：{m}")
    return data


def team_up(manifest: str = ".agent/team.yml", dry_run: bool = True, connect: bool = True) -> str:
    """按清单拉起团队：启动各成员的 agt-web 实例（各自 repo）→ 等端口就绪 → remote_connect 组网 → 恢复指定 session。
    dry_run=true（默认）只打印执行计划不真拉——先看计划再传 false 执行；connect=false 只启动不组网。
    成员经验绑在各自 session 上——恢复 session 后成员带着历史上下文上岗（新成员 session 留空=全新开始）。"""
    import subprocess, time
    data = _load_manifest(manifest)
    out = [f"📋 团队清单 {manifest}：{len(data['members'])} 名成员"]
    for m in data["members"]:
        port = int(m["port"]); repo = str(m["repo"]); sid = str(m["name"])
        out.append(f"  {sid} → agt-web --port {port} @ {repo}"
                   + (f"（恢复 session：{m['session']}）" if m.get("session") else ""))
    if dry_run:
        out.append("\n（dry_run——以上为执行计划；确认后传 dry_run=false 真正拉起）")
        return "\n".join(out)
    import remote_tools as rt
    results = []
    for m in data["members"]:
        port = int(m["port"]); repo = str(m["repo"]); sid = str(m["name"])
        url = f"http://127.0.0.1:{port}"
        alive = rt.probe_server(url) is not None
        if not alive:
            try:
                subprocess.Popen(["agt-web", "--port", str(port)], cwd=repo,
                                 creationflags=(subprocess.CREATE_NO_WINDOW if _is_win() else 0))
            except Exception as e:
                results.append(f"❌ {sid}：启动失败 {type(e).__name__}: {e}（检查 {repo} 的环境/agt-web 在 PATH）")
                continue
            ok = False
            for _ in range(60):   # 等端口就绪（最多 60s——冷启动含 session 恢复）
                time.sleep(1)
                if rt.probe_server(url) is not None:
                    ok = True
                    break
            if not ok:
                results.append(f"⚠️ {sid}：启动后 60s 未就绪（{url}）——稍后 team_status 复查")
                continue
        if connect:
            rt.connect(sid, url)
        sess = str(m.get("session") or "").strip()
        if sess:
            rt.send_message(sid, f"/resume {sess}")
            results.append(f"✅ {sid}：{'已在运行' if alive else '已启动'} · 已组网 · 恢复 session「{sess}」")
        else:
            results.append(f"✅ {sid}：{'已在运行' if alive else '已启动'} · {'已组网' if connect else '（跳过组网）'}")
    return "\n".join(results + ["\n（就绪后用 remote_ask(server_id, ...) 协作；team_status 看总览）"])


def team_status(manifest: str = ".agent/team.yml") -> str:
    """团队状态总览：清单成员的在线/模型/session/忙闲/轮次（POST /api/status 逐一探测，超时 4s）。"""
    import urllib.request
    data = _load_manifest(manifest)
    out = [f"👥 团队状态（{len(data['members'])} 名成员）："]
    for m in data["members"]:
        port = int(m["port"]); sid = str(m["name"])
        url = f"http://127.0.0.1:{port}/api/status"
        try:
            req = urllib.request.Request(url, data=_json.dumps({}).encode(), method="POST",
                                         headers={"Content-Type": "application/json"})
            d = _json.loads(urllib.request.urlopen(req, timeout=4).read().decode("utf-8"))
            if d.get("ready"):
                out.append(f"  🟢 {sid}：{d.get('model', '?')} · session={d.get('session_name', '?')} · "
                           f"{'⏳ busy' if d.get('busy') else '💤 空闲'} · turns={d.get('session_turns', '?')}")
            else:
                out.append(f"  🟡 {sid}：服务在但 Agent 未就绪（可能在恢复 session）")
        except Exception as e:
            out.append(f"  🔴 {sid}：不可达（{type(e).__name__}）——team_up(dry_run=false) 拉起")
    return "\n".join(out)


def agt_register(ctx=None):
    """ctx: {"cwd": ...}——引擎扫描按位置传入（无 ctx 也工作）。"""
    global _WORKSPACE
    if ctx and ctx.get("cwd"):
        _WORKSPACE = Path(ctx["cwd"])
    return [
        {"name": "team_up", "func": team_up, "group": "团队", "version": 1},
        {"name": "team_status", "func": team_status, "group": "团队", "version": 1},
    ]
