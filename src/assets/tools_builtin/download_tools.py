"""download_tools.py —— 随包资产下载两件套外置件（真限界上下文：资产目录自写自读）。

外置判别标准（wiki: architecture/tool-externalization-criteria.md）：
download 把随包资产复制到 manifest 声明的 default_dir（.agent/workflows/ 等）——
写目标由本组工具决定，读（exists 检查）也是自己。清单 src/assets/manifest.json 随包。

workspace 约定：inline 模式在主进程执行，cwd 即 workspace（Path.cwd()）。
框架 download.py 保留模块级 list_assets/download_asset（/download 命令用），删工厂。
改完本文件用 /reload tools 热加载（不需要重启）。
"""
from pathlib import Path

# workspace：默认 import 时捕获（兼容直接 import），agt_register(ctx) 时用引擎传入的覆盖
# （ctx["cwd"] 是引擎视角的真实 workspace——比 Path.cwd() 稳，os.chdir 后不漂移）
_WORKSPACE = Path.cwd()


def list_downloadable() -> str:
    """列出随包可下载资产（工作流/mcp/脚本），含名称/类型/描述/是否已在本地。
    需要某个随包能力时先看这个清单，再 download_asset(name) 取用。"""
    from download import list_assets
    items = list_assets(workspace=_WORKSPACE)
    if not items:
        return "(无随包资产)"
    lines = [f"共 {len(items)} 项随包资产："]
    for a in items:
        mark = "✅已在本机" if a.get("exists") else "⬇可下载"
        lines.append(f"  [{mark}] {a['name']} ({a['type']}) — {a['desc']}")
    return "\n".join(lines)


def download_asset(name: str, dir: str = "", force: bool = False) -> str:
    """下载某个随包资产到本地。name 见 list_downloadable；dir 留空=该资产默认目录；force=True 覆盖已有同名文件。"""
    from download import download_asset as _dl
    return _dl(name, target_dir=dir or None, force=force, workspace=_WORKSPACE)


def agt_register(ctx=None):
    """ctx: {"cwd": "<workspace 绝对路径>", ...}——引擎扫描时按位置传入（签名兼容：无 ctx 也工作）。"""
    global _WORKSPACE
    if ctx and ctx.get("cwd"):
        _WORKSPACE = Path(ctx["cwd"])
    return [
        {"name": "list_downloadable", "func": list_downloadable, "group": "资产下载", "version": 1},
        {"name": "download_asset", "func": download_asset, "group": "资产下载", "version": 1},
    ]
