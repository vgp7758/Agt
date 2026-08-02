"""verify_edit_tools.py —— 验证新增的行号编辑工具链路 + 版本乐观锁。

覆盖：read_file 版本页脚/行号、grep 版本+上下文、insert/delete/move 正确性、
version 不匹配被拒、缺 version 被拒、跨 workspace 被拒、version 链式流转、
param_descriptions 注入 schema。不触发真实 LLM；临时文件用完即清。
"""
import re
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

import real_tools as rt  # noqa: E402
from tools import Tool  # noqa: E402

TMP = Path(__file__).resolve().parent / "_verify_edit_tmp"
_VER = re.compile(r"file_version=([0-9a-f]+)")


def _ver(text: str) -> str:
    m = _VER.search(text)
    assert m, f"未在输出中找到 file_version：{text!r}"
    return m.group(1)


def _raw(name: str) -> str:
    return (TMP / name).read_text(encoding="utf-8")


def main():
    if TMP.exists():
        shutil.rmtree(TMP)
    TMP.mkdir()
    # 把 workspace 指到临时目录，所有 _resolve 限定在内，不污染仓库根
    rt.WORKSPACE = TMP

    f = "sample.txt"
    (TMP / f).write_text("a\nb\nc\nd\ne\n", encoding="utf-8")
    passed, failed = [], []

    def check(name, cond, extra=""):
        (passed if cond else failed).append(name)
        print(("  ✅ " if cond else "  ❌ ") + name + (f"  ({extra})" if extra and not cond else ""))

    # ---- read_file：版本页脚 + 行号 ----
    out = rt.read_file(f)
    check("read_file 末尾带 file_version", "file_version=" in out)
    check("read_file 默认无行号", "│" not in out)
    out_ln = rt.read_file(f, line_numbers=True)
    check("read_file line_numbers 带行号", "│ a" in out_ln and "5│" in out_ln)
    v0 = _ver(out)
    print(f"    初始 file_version={v0}")

    # ---- grep：版本 + 上下文 + 行号 ----
    (TMP / "other.py").write_text("foo\nbar\nX\nfoo\n", encoding="utf-8")
    g = rt.grep("foo", path=".")
    check("grep 带行号", "other.py:1:" in g and "other.py:4:" in g)
    check("grep 带每文件 file_version", "file_version=" in g)
    gc = rt.grep("X", path=".", context=1)
    check("grep context 含前后行", "bar" in gc and "foo" in gc and "> other.py:3:" in gc)

    # ---- grep 默认正则：a|b 多选一（模型最自然的"或"写法）----
    (TMP / "alt.txt").write_text("dangerCost=5\noverload=true\nsafe\n", encoding="utf-8")
    ga = rt.grep("_dangerCost|dangerCost|overload", path=".")
    check("grep 默认正则支持 a|b 多选一",
          "alt.txt:1:" in ga and "alt.txt:2:" in ga and "alt.txt:3:" not in ga, ga)
    # 字面模式：| 当普通字符，搜不到 "a|b" 这个连续子串
    gl = rt.grep("a|b", path="alt.txt", regex=False)
    check("grep regex=False 按字面(| 不再特殊)", "未找到" in gl, gl)
    # grep 对【单个文件】路径也能搜（修 rglob-on-file 扫0文件的老 bug）
    gfile = rt.grep("foo", path="other.py")
    check("grep 文件路径能搜到", "other.py:1:" in gfile and "other.py:4:" in gfile, gfile)

    # ---- insert：line=2 在 b 前插 X → a,X,b,c,d,e ----
    r = rt.insert(f, 2, "X", v0)
    check("insert 成功", r.startswith("✅") and "file_version=" in r)
    check("insert 内容正确", _raw(f) == "a\nX\nb\nc\nd\ne\n", _raw(f))
    v1 = _ver(r)
    check("insert 后 version 变了", v1 != v0)

    # ---- insert 多点原子插入（倒序应用，传原始行号即可，替代 run_python 拼字符串）----
    (TMP / "multi.txt").write_text("L1\nL2\nL3\nL4\nL5\n", encoding="utf-8")
    vm = _ver(rt.read_file("multi.txt"))
    rm = rt.insert("multi.txt", [1, 3, 5], ["A", "C", "E"], vm)   # 原始行号，内部倒序
    check("insert 多点成功", rm.startswith("✅"), rm)
    check("insert 多点倒序不串位", _raw("multi.txt") == "A\nL1\nL2\nC\nL3\nL4\nE\nL5\n", _raw("multi.txt"))
    vm2 = _ver(rm)
    rb = rt.insert("multi.txt", [1, 2], ["only one"], vm2)   # 长度不一致
    check("insert 长度不一致大声报错", "长度不一致" in rb, rb)

    # ---- 用旧版本 v0 再编辑 → 必须被拒 ----
    r_stale = rt.insert(f, 1, "Y", v0)
    check("stale version 被拒", "版本过期" in r_stale, r_stale)
    check("文件未被 stale 写入", "Y" not in _raw(f))

    # ---- 用返回的新版本 v1 链式 delete：删 X,b → a,c,d,e ----
    r2 = rt.delete(f, 2, 3, v1)
    check("delete 成功", r2.startswith("✅"))
    check("delete 内容正确", _raw(f) == "a\nc\nd\ne\n", _raw(f))
    v2 = _ver(r2)

    # ---- move：把 a(行1) 搬到原行4(e) 前 → c,d,a,e ----
    r3 = rt.move(f, 1, 1, 4, v2)
    check("move 成功", r3.startswith("✅"))
    check("move 内容正确", _raw(f) == "c\nd\na\ne\n", _raw(f))
    v3 = _ver(r3)

    # ---- move 自身范围 → 无操作 ----
    r_noop = rt.move(f, 2, 3, 3, v3)
    check("move 自身范围无操作", "无操作" in r_noop)

    # ---- 缺 version（经 Tool.run，模拟真实调用；version 必填，省略 → 报缺参）----
    missing = Tool(rt.insert).run(path=f, lines=[1], contents=["Z"])
    check("缺 version 报错(点名需传 version)",
          "version" in missing and ("缺 version" in missing or "missing" in missing), missing)

    # ---- 跨 workspace 被拒（经 Tool.run 兜底 PermissionError）----
    cross = Tool(rt.insert).run(path="../../etc/evil", lines=[1], contents=["x"], version=v3)
    check("跨 workspace 被拒", "拒绝访问" in cross, cross)

    # ---- 行号越界（start 超出文件长度，不该被误报成"参数错误"）----
    oob = rt.delete(f, 100, 200, v3)
    check("行号越界被拒", "越界" in oob, oob)

    # ---- param_descriptions 注入 schema（取 REAL_TOOLS 里注册好的工具）----
    reg = {t.name: t for t in rt.REAL_TOOLS}
    lines_desc = reg["insert"].schema["function"]["parameters"]["properties"]["lines"].get("description", "")
    check("param_descriptions 注入 schema", "行号" in lines_desc, lines_desc)
    old_desc = reg["edit"].schema["function"]["parameters"]["properties"]["old_string"].get("description")
    check("现有工具 schema 不受影响", old_desc is None)

    # ===== find_function =====
    # Python：含装饰器 + 嵌套函数
    (TMP / "mod.py").write_text(
        "@log\n"
        "def outer(x):\n"
        "    a = 1\n"
        "    def inner(y):\n"
        "        return y + 1\n"
        "    return inner(x)\n"
        "\n"
        "def helper():\n"
        "    return 0\n", encoding="utf-8")
    fo = rt.find_function("outer", "mod.py")
    check("find_function(Python) 带行范围", "L1-L6" in fo, fo)
    check("find_function(Python) 含装饰器", "│ @log" in fo)
    check("find_function(Python) 含嵌套函数体", "def inner" in fo and "return inner(x)" in fo)
    check("find_function 带文件 version", "file_version=" in fo)
    fi = rt.find_function("inner", "mod.py")
    check("find_function(嵌套) 范围正确", "L4-L5" in fi, fi)
    fh = rt.find_function("helper", "mod.py")
    check("find_function(第二个函数) 范围正确", "L8-L9" in fh, fh)
    fn = rt.find_function("nope", "mod.py")
    check("find_function 找不到有提示", "未找到" in fn)

    # JS：function / 方法简写 / 箭头，且排除 foo() 调用
    (TMP / "tanks.js").write_text(
        "function foo(a, b) {\n"
        "  return a + b;\n"
        "}\n"
        "\n"
        "class C {\n"
        "  bar() {\n"
        "    return 1;\n"
        "  }\n"
        "}\n"
        "\n"
        "const baz = (x) => {\n"
        "  return x * 2;\n"
        "};\n"
        "\n"
        "foo(1, 2);\n", encoding="utf-8")
    jf = rt.find_function("foo", "tanks.js")
    check("find_function(JS) 命中 function 定义", "L1-L3" in jf, jf)
    check("find_function(JS) 排除 foo() 调用", "L15" not in jf, jf)
    jb = rt.find_function("bar", "tanks.js")
    check("find_function(JS 方法简写) 范围正确", "L6-L8" in jb, jb)
    jz = rt.find_function("baz", "tanks.js")
    check("find_function(JS 箭头) 范围正确", "L11-L13" in jz, jz)

    # 大括号里藏字符串/注释里的 } 不能干扰配对
    (TMP / "tricky.js").write_text(
        'function f() {\n'
        '  const s = "} not end"; // } also not\n'
        '  return s;\n'
        '}\n', encoding="utf-8")
    tf = rt.find_function("f", "tricky.js")
    check("find_function 串/注释里的 } 不误判", "L1-L4" in tf, tf)

    # C# 风格签名
    (TMP / "Thing.cs").write_text(
        "public void Foo() {\n"
        "  return;\n"
        "}\n", encoding="utf-8")
    cf = rt.find_function("Foo", "Thing.cs")
    check("find_function(C# 签名) 范围正确", "L1-L3" in cf, cf)

    # 目录跨文件同名 → 多结果
    fd = rt.find_function("foo", ".")
    check("find_function(目录) 跨文件找全", "tanks.js" in fd and "L1-L3" in fd, fd)

    # 清理临时
    shutil.rmtree(TMP, ignore_errors=True)
    shutil.rmtree(TMP, ignore_errors=True)

    print(f"\n{'='*40}\n通过 {len(passed)} / 失败 {len(failed)}")
    if failed:
        print("失败项：", failed)
        sys.exit(1)
    print("全部通过 ✅")


if __name__ == "__main__":
    main()
