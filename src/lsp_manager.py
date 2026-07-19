"""lsp_manager.py —— 按需装配语言 LSP 的编排层。

ensure_lsp(lang) 工具：把内置的语言 LSP 脚本 copy 到 ~/.agt/lsp/ → pip 装依赖 →
连成 MCP server → 注册工具进 Agent（当轮即可用）→ 记 ~/.agt/mcp.json（下次启动自动连）。

注册表 LSP_REGISTRY 声明每个语言：脚本文件名 + pip 依赖 + MCP server 名。
内置脚本在 src/lsp_scripts/（随包发布，pyproject package-data）。
"""
import json
import subprocess
import sys
from pathlib import Path

from real_tools import WORKSPACE

_LSP_DIR = Path.home() / ".agt" / "lsp"
_GLOBAL_MCP = Path.home() / ".agt" / "mcp.json"
_BUNDLED = Path(__file__).resolve().parent / "lsp_scripts"

# 语言 → 装配信息。script: src/lsp_scripts/ 下的文件名；requires: pip 包名；
# server: MCP server 名（工具命名空间 __mcp__<server>__<tool>）。
LSP_REGISTRY = {
    "python": {"script": "python_lsp.py", "requires": [], "server": "python-lsp"},
    "csharp": {"script": "csharp_lsp.py", "requires": ["multilspy"], "server": "csharp-lsp"},
}


def _copy_script(script: str):
    """把内置脚本 copy 到 ~/.agt/lsp/<script>（已存在且内容一致则跳过）。返回 (dest, err)。"""
    src = _BUNDLED / script
    if not src.exists():
        return None, f"内置脚本缺失：{src}"
    _LSP_DIR.mkdir(parents=True, exist_ok=True)
    dest = _LSP_DIR / script
    txt = src.read_text(encoding="utf-8")
    if not dest.exists() or dest.read_text(encoding="utf-8") != txt:
        dest.write_text(txt, encoding="utf-8")
    return dest, ""


def _ensure_pkg(pkg: str):
    """pip 安装某包（已能 import 则跳过）。"""
    try:
        __import__(pkg.replace("-", "_"))
        return
    except ImportError:
        pass
    subprocess.run([sys.executable, "-m", "pip", "install", pkg], check=False)


def _persist(server: str, cfg: dict):
    """把 server 条目写进 ~/.agt/mcp.json（不写 cwd，启动时用当前 cwd）。"""
    try:
        data = json.loads(_GLOBAL_MCP.read_text(encoding="utf-8")) if _GLOBAL_MCP.exists() else {"mcpServers": {}}
    except Exception:
        data = {"mcpServers": {}}
    data.setdefault("mcpServers", {})[server] = {"command": cfg["command"], "args": cfg["args"]}
    _GLOBAL_MCP.parent.mkdir(parents=True, exist_ok=True)
    _GLOBAL_MCP.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _short(full: str) -> str:
    """__mcp__<server>__<tool> → <tool>。"""
    return full.rsplit("__", 1)[-1] if "__" in full else full


def make_lsp_tools(agent, mcp_mgr):
    """返回 [ensure_lsp] 工具（闭包绑定 agent + MCPManager）。"""
    from tools import Tool

    def ensure_lsp(lang: str) -> str:
        """按需装配某语言的 LSP 语义导航工具（定义/引用/符号/诊断），当轮即可用。
        首次会 copy 内置脚本到 ~/.agt/lsp/ 并 pip 装依赖（C# 装 multilspy）。
        lang: 'python' | 'csharp'(或 'cs'/'c#')。装好后工具前缀：py_*（python）/ cs_*（csharp）。
        处理某语言代码前调一次即可；之后重启 Agent 也会自动连（~/.agt/mcp.json）。"""
        key = lang.lower()
        if key in ("cs", "c#"):
            key = "csharp"
        info = LSP_REGISTRY.get(key)
        if not info:
            return f"[ensure_lsp] 不支持的语言 '{lang}'，已支持：python、csharp(cs)"
        # 1. copy 内置脚本到 ~/.agt/lsp/
        dest, err = _copy_script(info["script"])
        if err:
            return f"[ensure_lsp] {err}"
        # 2. pip 装依赖
        for pkg in info.get("requires", []):
            _ensure_pkg(pkg)
        # 3. 连接为 MCP server（command=当前 python，cwd=Agent WORKSPACE）
        server = info["server"]
        cfg = {"command": sys.executable, "args": [str(dest)], "cwd": str(WORKSPACE)}
        try:
            mcp_mgr.connect_one(server, cfg)
        except Exception as e:
            return f"[ensure_lsp] 连接 {server} 失败：{type(e).__name__}: {e}（依赖刚装，可能需重试一次）"
        # 4. 注册工具进 Agent（当轮可用）
        added = mcp_mgr.sync_to_toolbox(agent.tools)
        # 5. 持久化（下次启动自动连）
        _persist(server, cfg)
        names = ", ".join(_short(n) for n in added) or "(工具已存在)"
        return (f"✅ 已装配 {key} LSP（{server}）。\n新增/可用工具：{names}\n"
                f"提示：首次调用该语言工具时会启动语言服务器并索引工程（大工程可能几分钟）。")

    return [Tool(ensure_lsp)]
