"""verify_img_tags.py —— 验证工具图片的 <img> 标签化 + 按模型 vision 投影。

覆盖：
  1. Agent._materialize_tool_result：data URL → 落盘 repo images/ → <img> 标签，base64 消失
  2. Session._project_imgs：非视觉→文字占位 str；视觉→list 含 image_url（读回落盘图）
  3. LLMClient._apply_profile：vision 标志正确落到 self.vision_supported

跑法：python verify_img_tags.py
"""
import sys, tempfile, base64
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from agent import Agent
from session import Session, repo_images_dir
from llm_client import LLMClient

# 1x1 红点 PNG 的 base64
RED_PNG_B64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAAC0lEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
DATA_URL = f"data:image/png;base64,{RED_PNG_B64}"

passed, failed = [], []
def check(name, cond, extra=""):
    (passed if cond else failed).append(name)
    print(("  ✅ " if cond else "  ❌ ")+name+(f"  ({extra})" if extra and not cond else ""))

tmpdir = Path(tempfile.mkdtemp())

# —— 1. _materialize_tool_result ——
print("【1】_materialize_tool_result：data URL 落盘 + <img> 标签替换")
class _FakeSession:
    def __init__(self, ws): self.workspace = ws
class _FakeAgent:
    def __init__(self, ws): self.session = _FakeSession(ws)

fa = _FakeAgent(tmpdir)
materialized = Agent._materialize_tool_result(fa, f"截图结果：{DATA_URL}\n其它文本", "shot", {}, "c7")
check("base64 已从结果文本消失", RED_PNG_B64 not in materialized, materialized[:80])
check("结果含 <img>c7_0.png</img> 标签", "<img>c7_0.png</img>" in materialized, materialized)
img_file = repo_images_dir(tmpdir) / "c7_0.png"
check("图片真落在 repo images/ 目录", img_file.exists(), str(img_file))
check("落盘文件非空（合法 PNG）", img_file.exists() and len(img_file.read_bytes()) > 0)
check("非图片文本保留", "其它文本" in materialized and "截图结果：" in materialized)
check("无图片的结果原样返回", Agent._materialize_tool_result(fa, "纯文本无图", "t", {}, "c8") == "纯文本无图")

# —— 2. _project_imgs ——
print("\n【2】_project_imgs：按当前模型 vision 投影")
class _FakeLLM:
    def __init__(self, vision): self.vision_supported = vision
s = Session.__new__(Session)
s.workspace = tmpdir

s.llm = _FakeLLM(vision=False)
out_nv = s._project_imgs("前面文本<img>c7_0.png</img>后面文本")
check("非视觉：返回 str（文字占位）", isinstance(out_nv, str), type(out_nv).__name__)
check("非视觉：占位含委托提示", "委托视觉子 agent" in out_nv and "agent_prompt" in out_nv, out_nv)
check("非视觉：保留前后文本", "前面文本" in out_nv and "后面文本" in out_nv)
check("无标签：原样返回 str", s._project_imgs("普通文本") == "普通文本")

s.llm = _FakeLLM(vision=True)
out_v = s._project_imgs("前面文本<img>c7_0.png</img>后面文本")
check("视觉：返回 list", isinstance(out_v, list), str(out_v)[:100])
check("视觉：含 image_url 块", any(isinstance(b, dict) and b.get("type") == "image_url" for b in (out_v or [])), str(out_v)[:120])
check("视觉：image_url 是 data URL", any("data:image" in b.get("image_url", {}).get("url", "") for b in (out_v or []) if isinstance(b, dict)))
check("视觉：保留前后文本块", any("前面文本" in b.get("text", "") for b in out_v) and any("后面文本" in b.get("text", "") for b in out_v))

# —— 3. LLMClient.vision_supported ——
print("\n【3】LLMClient._apply_profile：vision 标志透传")
llm = LLMClient.__new__(LLMClient)
llm._apply_profile({"base_url": "https://x", "api_tokens": ["k"], "model": "m", "vision": True})
check("profile vision=True → self.vision_supported True", llm.vision_supported is True, str(llm.vision_supported))
llm._apply_profile({"base_url": "https://x", "api_tokens": ["k"], "model": "m"})
check("profile 缺省 vision → self.vision_supported False", llm.vision_supported is False, str(llm.vision_supported))

# —— 4. read_file 读图片（原 read_image 已并入）：裸文件名从 repo images/ 找 ——
print("\n【4】read_file 读图片：裸文件名从 repo images/ 找，返回 data URL")
import real_tools
from real_tools import read_file
ws_images = repo_images_dir(real_tools.WORKSPACE)
_test_png = ws_images / "_verify_read.png"
_test_png.write_bytes(base64.b64decode(RED_PNG_B64))
_ri = read_file("_verify_read.png")
check("read_file 找到 repo images/ 里的裸文件名", _ri.startswith("data:image/png;base64,"), _ri[:40])
check("read_file 读图返回的 data URL 含原 base64", RED_PNG_B64 in _ri)
check("read_file 找不到图时给错误文本", read_file("不存在_zzz.png").startswith("[未找到图片]"))
_test_png.unlink()

print(f"\n{'🎉 全通过' if not failed else '⚠️ '+str(len(failed))+' 项失败'}（{len(passed)} 通过）")
