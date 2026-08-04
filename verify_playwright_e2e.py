"""端到端：playwright MCP 截图 → <img> 标签 → 落盘 repo images/。临时 workspace，跑完即弃。

跑法：python verify_playwright_e2e.py
（首次 npx 会下载 @playwright/mcp + 浏览器，可能耗时几分钟）
"""
import sys, tempfile, json
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass
from mcp_client import MCPManager
from agent import Agent
from session import repo_images_dir

tmp = Path(tempfile.mkdtemp(prefix="agt_e2e_"))
(tmp / ".mcp.json").write_text(json.dumps({"mcpServers": {
    "playwright": {"command": "npx.cmd", "args": ["-y", "@playwright/mcp@latest"]}
}}), encoding="utf-8")
print(f"[1] 临时 workspace: {tmp}")

mgr = MCPManager()
print("[2] 连接 playwright MCP（首次 npx 下载可能 1-3 分钟）...")
try:
    mgr.connect_from_config(str(tmp / ".mcp.json"))
except Exception as e:
    print(f"❌ 连接失败: {type(e).__name__}: {e}")
    print("（Windows 上若 command 找不到，把 npx.cmd 改成 cmd + ['/c','npx',...]）")
    sys.exit(1)

tools = mgr.get_tools()
names = [t.orig_name for t in tools]
print(f"[3] playwright 提供 {len(tools)} 个工具。导航/截图相关:")
for n in names:
    if any(k in n.lower() for k in ("shot", "navigate", "snapshot")):
        print("     -", n)

def call(tool, args):
    try:
        return mgr.call_tool_sync("playwright", tool, args)
    except Exception as e:
        return f"[调用出错 {type(e).__name__}: {e}]"

print("\n[4] navigate https://example.com ...")
print("   ", call("browser_navigate", {"url": "https://example.com"})[:200])

print("\n[5] take_screenshot ...")
shot = call("browser_take_screenshot", {})
print("    含 data:image:", "data:image" in shot, " 长度:", len(shot))
print("    前 80 字符:", shot[:80])

if "data:image" in shot:
    print("\n[6] 走 _materialize_tool_result（落盘 + <img> 标签）...")
    class FS:
        def __init__(s, ws): s.workspace = ws
    class FA:
        def __init__(s, ws): s.session = FS(ws)
    mat = Agent._materialize_tool_result(FA(tmp), shot, "browser_take_screenshot", {}, "e2e1")
    print("    含 <img>e2e1_0.png</img>:", "<img>e2e1_0.png</img>" in mat)
    print("    base64 已从文本消失:", "data:image" not in mat)
    img = repo_images_dir(tmp) / "e2e1_0.png"
    print(f"    图片落盘 {img.name}:", img.exists(),
          "大小:", img.stat().st_size if img.exists() else 0, "字节")
    print("\n✅ playwright 截图 → <img> 标签 → 落盘 repo images/  全链路通过")
else:
    print("\n⚠️ 截图未返回 data URL，跳过落盘验证。看上面 navigate/screenshot 输出排障")
    print("   常见：浏览器未装 → 先跑 `npx -y playwright install chromium`")

mgr.shutdown()
