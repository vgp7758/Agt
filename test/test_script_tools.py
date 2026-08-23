#!/usr/bin/env python3
"""test_script_tools.py —— 外置脚本工具（script_tools.py）全场景验证。
运行：python test/test_script_tools.py（在仓库根执行）

六场景（spec s_25352f88 第 5 步）：
① 扫描注册 15 个工具（名称/schema/hidden 正确）
② inline 对拍（与原内置行为逐工具一致）
③ 同名覆盖（临时目录造同名假工具 → 后扫描优先）
④ reload 热加载（改脚本描述 → 重扫生效；删除 → 摘除）
⑤ subprocess 模式协议占位（stdin JSON / NDJSON stdout 往返）
⑥ 坏脚本容错（语法错误/缺 agt_register/描述符非法 → 跳过不炸主程序）
"""
import json
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from tools import Toolbox
from real_tools import REAL_TOOLS, LIGHT_TOOLS
from script_tools import scan_script_tools, attach_script_tools, reload_script_tools

PASS, FAIL = 0, 0


def check(name, cond, detail=""):
    global PASS, FAIL
    mark = "✅" if cond else "❌"
    print(f"{mark} {name}" + (f"  {detail}" if detail and not cond else ""))
    PASS, FAIL = PASS + (1 if cond else 0), FAIL + (0 if cond else 1)


MIGRATED = {"add", "subtract", "multiply", "divide", "join", "split", "contains",
            "starts_with", "ends_with", "to_ascii", "pass_through", "list_append",
            "get_list_item", "sleep", "kw_score"}

# ========== ① 扫描注册 ==========
stb = scan_script_tools()
names = {t.name for t in stb}
check("① 注册 15 个外置工具", MIGRATED == names,
      f"缺 {MIGRATED - names} / 多 {names - MIGRATED}")
hidden_all = all(t.hidden for t in stb)
check("① hidden 全 True（不投影主 LLM）", hidden_all,
      f"未 hidden: {[t.name for t in stb if not t.hidden]}")
sch_ok = True
for t in stb:
    props = t.schema.get("function", {}).get("parameters", {}).get("properties", {})
    if t.name in ("contains", "starts_with", "ends_with") and set(props) != {"text", "keyword", "prefix", "suffix"} - ({"prefix"} if t.name == "contains" else set()) - ({"keyword"} if t.name != "contains" else set()) - ({"suffix"} if t.name != "ends_with" else set()):
        sch_ok = False
check("① schema 参数表完整（以 contains 为例）",
      stb and {p for p in next(t for t in stb if t.name == "contains")
               .schema["function"]["parameters"]["properties"]} == {"text", "keyword"})

# ========== ② inline 对拍 ==========
def norm(v):
    """Toolbox.call 统一返回字符串（bool→'True'、list/dict→JSON 串、数字→str）。
    归一化：JSON 可解析则解析回原值再比，否则按字符串比。"""
    try:
        return json.loads(v)
    except Exception:
        return v


tb = Toolbox(*(list(REAL_TOOLS) + list(LIGHT_TOOLS)))
attach_script_tools(tb)
cases = [
    ("contains", {"text": "hello world", "keyword": "world"}, True),
    ("contains", {"text": "abc", "keyword": "z"}, False),
    ("starts_with", {"text": "src/a.py", "prefix": "src/"}, True),
    ("ends_with", {"text": "main.py", "suffix": ".py"}, True),
    ("to_ascii", {"text": "中a"}, "\\u4e2da"),
    ("join", {"items": ["a", "b", "c"], "separator": "-"}, "a-b-c"),
    ("split", {"text": "a|b|c", "separator": "|"}, ["a", "b", "c"]),
    ("list_append", {"lst": ["x"], "item": "y"}, ["x", "y"]),
    ("list_append", {"lst": None, "item": 1}, [1]),
    ("get_list_item", {"lst": [10, 20, 30], "index": -1}, 30),
    ("get_list_item", {"lst": [1], "index": 5}, "[越界] index=5，列表长度 1"),
    ("pass_through", {"input": {"k": 1}}, {"k": 1}),
    ("add", {"a": 2, "b": 3}, 5),
    ("subtract", {"a": 5, "b": 3}, 2),
    ("multiply", {"a": 4, "b": 0.5}, 2),
    ("divide", {"a": 10, "b": 4}, 2.5),
    ("divide", {"a": 1, "b": 0}, "[错误] 除数不能为 0"),
    ("kw_score", {"keywords": ["分档", "投影"], "text": "分档投影机制"}, 1.0),
    ("kw_score", {"keywords": None, "text": "任意"}, 0.0),
]
fails = []
for nm, args, want in cases:
    got = tb.call(nm, args)
    g = norm(got)
    ok = (g == want) or (str(g) == str(want))
    if not ok:
        fails.append(f"{nm}({args}): got {got!r} want {want!r}")
check(f"② 对拍 {len(cases) - len(fails)}/{len(cases)} 一致", not fails, "; ".join(fails))
vis = {s["function"]["name"] for s in tb.schemas()}
check("② hidden 语义：不进主 LLM schema", not (MIGRATED & vis),
      f"泄漏: {MIGRATED & vis}")

# ========== ③ 同名覆盖 ==========
tmp = Path(tempfile.mkdtemp(prefix="agt_st_"))
try:
    (tmp / "override.py").write_text(
        'def length(obj):\n    """被外置覆盖的 length——返回 999 测试优先级。"""\n    return 999\n\n'
        'def agt_register():\n    return [{"name": "length", "func": length, "hidden": True, "version": 1}]\n',
        encoding="utf-8")
    tb3 = Toolbox(*(list(REAL_TOOLS) + list(LIGHT_TOOLS)))
    attach_script_tools(tb3, dirs=[Path("tools"), tmp])   # tmp 后扫 → 覆盖
    check("③ 同名覆盖（后扫描胜出）", norm(tb3.call("length", {"obj": "abc"})) == 999)
finally:
    shutil.rmtree(tmp, ignore_errors=True)

# ========== ④ reload 热加载 ==========
class _FakeAgent:
    tools = Toolbox(*(list(REAL_TOOLS) + list(LIGHT_TOOLS)))


tmp4 = Path(tempfile.mkdtemp(prefix="agt_rl_"))
try:
    f = tmp4 / "hot_tool.py"
    f.write_text('def hot(x: int) -> int:\n    """v1"""\n    return x + 1\n\n'
                 'def agt_register():\n    return [{"name": "hot", "func": hot, "hidden": True, "version": 1}]\n',
                 encoding="utf-8")
    fa = _FakeAgent()
    attach_script_tools(fa.tools, dirs=[tmp4])
    check("④ 首次注册 hot=v1", norm(fa.tools.call("hot", {"x": 1})) == 2)
    # mtime 粒度：确保重写后 mtime 变化（Windows FAT 2s 粒度 → 主动 sleep）
    time.sleep(2.1)
    f.write_text('def hot(x: int) -> int:\n    """v2 热加载后行为变更"""\n    return x + 100\n\n'
                 'def agt_register():\n    return [{"name": "hot", "func": hot, "hidden": True, "version": 1}]\n',
                 encoding="utf-8")
    import script_tools as _st
    out = reload_script_tools(fa, dirs=[tmp4])   # 会摘旧（_LAST 含此前 attach 的全部名——含 hot）
    check("④ reload 后 hot=v2", norm(fa.tools.call("hot", {"x": 1})) == 101, out)
    # 删除文件 → reload 摘除
    f.unlink()
    out = reload_script_tools(fa, dirs=[tmp4])
    check("④ 删除后 hot 摘除", "hot" not in fa.tools, out)
finally:
    shutil.rmtree(tmp4, ignore_errors=True)
# 恢复主进程 _LAST（后续测试不受临时目录影响）
attach_script_tools(Toolbox(*(list(REAL_TOOLS) + list(LIGHT_TOOLS))))

# ========== ⑤ subprocess 协议占位 ==========
tmp5 = Path(tempfile.mkdtemp(prefix="agt_sp_"))
try:
    (tmp5 / "sp_tool.py").write_text(
        'import sys, json\n'
        'req = json.loads(sys.stdin.read())\n'
        'a = req.get("args", {}).get("a", 0)\n'
        'print(json.dumps({"type": "stream", "text": f"计算 {a}..."}))\n'
        'print(json.dumps({"type": "done", "result": a * 2}))\n',
        encoding="utf-8")
    (tmp5 / "sp_reg.py").write_text(
        'def agt_register():\n'
        '    return [{"name": "sp_double", "mode": "subprocess", "func": "run",\n'
        '             "description": "子进程双倍", "hidden": True,\n'
        '             "params": {"a": {"type": "integer", "description": "输入数"}}}]\n',
        encoding="utf-8")
    from script_tools import _run_subprocess
    r = _run_subprocess(str(tmp5 / "sp_tool.py"), "sp_double", {"a": 21})
    check("⑤ NDJSON 协议：done.result 直取", str(r) == "42", repr(r))
    # 非协议纯文本输出 → 原样返回
    (tmp5 / "plain.py").write_text('print("hello plain")\n', encoding="utf-8")
    r2 = _run_subprocess(str(tmp5 / "plain.py"), "plain", {})
    check("⑤ 纯文本降级：stdout 原样", r2 == "hello plain", repr(r2))
finally:
    shutil.rmtree(tmp5, ignore_errors=True)

# ========== ⑥ 坏脚本容错 ==========
tmp6 = Path(tempfile.mkdtemp(prefix="agt_bad_"))
try:
    (tmp6 / "_private.py").write_text("print('下划线开头应被跳过')\n", encoding="utf-8")
    (tmp6 / "syntax_err.py").write_text("def broken(:\n", encoding="utf-8")
    (tmp6 / "no_register.py").write_text("X = 1\n", encoding="utf-8")
    (tmp6 / "bad_desc.py").write_text(
        "def agt_register():\n    return [{'name': '', 'func': None}]  # 缺 name\n",
        encoding="utf-8")
    (tmp6 / "good.py").write_text(
        "def ok(x: int) -> int:\n    \"\"\"好工具\"\"\"\n    return x\n\n"
        "def agt_register():\n    return [{'name': 'ok_tool', 'func': ok, 'hidden': True, 'version': 1}]\n",
        encoding="utf-8")
    stb6 = scan_script_tools(dirs=[tmp6])
    n6 = {t.name for t in stb6}
    failed6 = list(getattr(stb6, "_scan_failed", []))
    check("⑥ 容错：仅好工具注册", n6 == {"ok_tool"}, str(n6))
    check("⑥ 容错：坏脚本进 failed 清单不炸主程序",
          len(failed6) == 2 and any("syntax_err" in f for f in failed6)
          and any("bad_desc" in f for f in failed6), str(failed6))
    check("⑥ 容错：_private 与 no_register 静默跳过",
          not any("_private" in f or "no_register" in f for f in failed6))
finally:
    shutil.rmtree(tmp6, ignore_errors=True)

print("=" * 56)
print(f"结果：{PASS} 通过 / {FAIL} 失败")
sys.exit(1 if FAIL else 0)
