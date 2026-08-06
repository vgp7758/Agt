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
    check("read_file 默认带行号", "│" in out)
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

    # ---- insert：单点 entries=[{line,content}] ----
    r = rt.insert(f, [{"line": 2, "content": "X"}], v0)
    check("insert 成功", r.startswith("✅") and "file_version=" in r)
    check("insert 内容正确", _raw(f) == "a\nX\nb\nc\nd\ne\n", _raw(f))
    v1 = _ver(r)
    check("insert 后 version 变了", v1 != v0)

    # ---- insert 多点原子插入（先按 line 排序再降序应用，传原始行号即可，替代 run_python 拼字符串）----
    (TMP / "multi.txt").write_text("L1\nL2\nL3\nL4\nL5\n", encoding="utf-8")
    vm = _ver(rt.read_file("multi.txt"))
    rm = rt.insert("multi.txt", [{"line": 1, "content": "A"}, {"line": 3, "content": "C"}, {"line": 5, "content": "E"}], vm)
    check("insert 多点成功", rm.startswith("✅"), rm)
    check("insert 多点倒序不串位", _raw("multi.txt") == "A\nL1\nL2\nC\nL3\nL4\nE\nL5\n", _raw("multi.txt"))
    vm2 = _ver(rm)
    rb = rt.insert("multi.txt", [{"line": 1, "content": "only one"}], vm2)  # 单元素也合法
    check("insert 单元素也工作", rb.startswith("✅"), rb)

    # ---- 用旧版本 v0 再编辑 → 必须被拒 ----
    r_stale = rt.insert(f, [{"line": 1, "content": "Y"}], v0)
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

    # ---- replace_lines：按行号整段替换（多段原子 + version + 重叠/越界校验）----
    (TMP / "rep.txt").write_text("L1\nL2\nL3\nL4\nL5\n", encoding="utf-8")
    vr = _ver(rt.read_file("rep.txt"))
    rr = rt.replace_lines("rep.txt", [{"range": [2, 3], "content": "X\nY"}], vr)
    check("replace_lines 单段成功", rr.startswith("✅"), rr)
    check("replace_lines 单段内容正确", _raw("rep.txt") == "L1\nX\nY\nL4\nL5\n", _raw("rep.txt"))
    # 多段原子（降序不串位）：[4,4]→Z 与 [2,2]→A\nB 同传原始行号 → L1,A,B,L3,Z,L5
    (TMP / "m.txt").write_text("L1\nL2\nL3\nL4\nL5\n", encoding="utf-8")
    vm = _ver(rt.read_file("m.txt"))
    rm = rt.replace_lines("m.txt", [{"range": [4, 4], "content": "Z"}, {"range": [2, 2], "content": "A\nB"}], vm)
    check("replace_lines 多段成功", rm.startswith("✅"), rm)
    check("replace_lines 多段降序不串位", _raw("m.txt") == "L1\nA\nB\nL3\nZ\nL5\n", _raw("m.txt"))
    # [n,n] 替换单行 + 空 content 删行
    (TMP / "s.txt").write_text("a\nb\nc\n", encoding="utf-8")
    vs = _ver(rt.read_file("s.txt"))
    rs = rt.replace_lines("s.txt", [{"range": [2, 2], "content": "B"}], vs)
    check("replace_lines [n,n] 替换单行", _raw("s.txt") == "a\nB\nc\n", _raw("s.txt"))
    rd = rt.replace_lines("s.txt", [{"range": [1, 1], "content": ""}], _ver(rs))
    check("replace_lines 空 content 删行", _raw("s.txt") == "B\nc\n", _raw("s.txt"))
    # 越界：range 起点超出总行数
    (TMP / "o.txt").write_text("only\n", encoding="utf-8")
    roob = rt.replace_lines("o.txt", [{"range": [5, 6], "content": "x"}], _ver(rt.read_file("o.txt")))
    check("replace_lines 越界被拒", "越界" in roob, roob)
    # range 重叠被拒（用 m.txt 当前新鲜 version，确保能走到重叠校验）
    rol = rt.replace_lines("m.txt", [{"range": [2, 4], "content": "x"}, {"range": [3, 3], "content": "y"}], _ver(rm))
    check("replace_lines range 重叠被拒", "重叠" in rol, rol)
    # 旧 version 被拒（vr 是 rep.txt 单段替换前的版本）
    r_stale = rt.replace_lines("rep.txt", [{"range": [1, 1], "content": "Q"}], vr)
    check("replace_lines stale version 被拒", "版本过期" in r_stale, r_stale)
    # 缺 version（经 Tool.run，version 必填）
    missing = Tool(rt.replace_lines).run(path="rep.txt", entries=[{"range": [1, 1], "content": "Q"}])
    check("replace_lines 缺 version 报错", "version" in missing and ("缺 version" in missing or "missing" in missing), missing)
    # 跨 workspace 被拒（_resolve 在 version 校验前就拦）
    cross = Tool(rt.replace_lines).run(path="../../etc/evil", entries=[{"range": [1, 1], "content": "x"}], version=_ver(rd))
    check("replace_lines 跨 workspace 被拒", "拒绝访问" in cross, cross)

    # ---- edit：精确匹配 + 行尾空白容忍回退 ----
    (TMP / "ed.txt").write_text("def foo():\n    return 1\n", encoding="utf-8")
    re1 = rt.edit("ed.txt", "def foo():\n    return 1", "def foo():\n    return 2")
    check("edit 精确匹配成功", re1.startswith("✅"), re1)
    check("edit 精确替换正确", _raw("ed.txt") == "def foo():\n    return 2\n", _raw("ed.txt"))
    # 行尾空白容忍：文件带行尾空格，old_string 干净 → 仍匹配成功
    (TMP / "ws.txt").write_text("def foo(): \n    return 1   \n", encoding="utf-8")
    re2 = rt.edit("ws.txt", "def foo():\n    return 1", "def foo():\n    return 2")
    check("edit 行尾空白容忍匹配成功", re2.startswith("✅") and "行尾空白容忍" in re2, re2)
    check("edit 行尾空白容忍替换正确", _raw("ws.txt") == "def foo():\n    return 2\n", _raw("ws.txt"))
    # CRLF 文件仍能精确匹配（universal newlines 已规整为 LF）
    (TMP / "crlf.txt").write_bytes(b"def foo():\r\n    return 1\r\n")
    re3 = rt.edit("crlf.txt", "def foo():\n    return 1", "def foo():\n    return 2")
    check("edit CRLF 文件匹配成功", re3.startswith("✅"), re3)
    # 去行尾空白后多处 → 不唯一（要求加上下文）
    (TMP / "dup.txt").write_text("a  \nb\na  \nb\n", encoding="utf-8")
    re4 = rt.edit("dup.txt", "a\nb", "z\nz")
    check("edit rstrip 后多处→不唯一", "不唯一" in re4, re4)
    # 真无匹配 → 改进提示（点名 read_file）
    re5 = rt.edit("ed.txt", "no_such_content_here", "whatever")
    check("edit 真无匹配带提示", "未命中" in re5 and "read_file" in re5, re5)
    # tab 缩进不误配（只去行尾、不碰行首）→ 仍未命中
    (TMP / "tab.txt").write_text("def foo():\n\treturn 1\n", encoding="utf-8")
    re6 = rt.edit("tab.txt", "def foo():\n    return 1", "def foo():\n    return 2")
    check("edit tab 缩进不误配(仍未命中)", "未命中" in re6, re6)

    # ---- 缺 version（经 Tool.run，模拟真实调用；version 必填，省略 → 报缺参）----
    missing = Tool(rt.insert).run(path=f, entries=[{"line": 1, "content": "Z"}])
    check("缺 version 报错(点名需传 version)",
          "version" in missing and ("缺 version" in missing or "missing" in missing), missing)

    # ---- 跨 workspace 被拒（经 Tool.run 兜底 PermissionError）----
    cross = Tool(rt.insert).run(path="../../etc/evil", entries=[{"line": 1, "content": "x"}], version=v3)
    check("跨 workspace 被拒", "拒绝访问" in cross, cross)

    # ---- 行号越界（start 超出文件长度，不该被误报成"参数错误"）----
    oob = rt.delete(f, 100, 200, v3)
    check("行号越界被拒", "越界" in oob, oob)

    # ---- param_descriptions 注入 schema（取 REAL_TOOLS 里注册好的工具）----
    reg = {t.name: t for t in rt.REAL_TOOLS}
    entries_desc = reg["insert"].schema["function"]["parameters"]["properties"]["entries"].get("description", "")
    check("param_descriptions 注入 schema", "插入点数组" in entries_desc or "line" in entries_desc, entries_desc)
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

    # ---- _number_lines：recent-file 快照的行号化（自适应宽度 + 截断尾段用真实行号）----
    nl = rt._number_lines("a\nb\nc\n")
    check("_number_lines 小文件带行号", "1│ a" in nl and "3│ c" in nl, nl)
    check("_number_lines 小文件宽度自适应(单位数无前导空格)", nl.splitlines()[0] == "1│ a", nl)
    big = "\n".join(f"line {i}" for i in range(1, 4002)) + "\n"   # 4001 行 → 触发首尾截断
    nlb = rt._number_lines(big)
    bl = nlb.splitlines()
    check("_number_lines 超长截断有标记", "共4001行" in nlb, nlb)
    check("_number_lines 头部从行1开始(宽4)", bl[0] == "   1│ line 1", bl[0])
    # n=4001：尾段 = 第 2002..4001 行（enumerate start = n-1999 = 2002）
    check("_number_lines 尾段用真实行号", "2002│ line 2002" in nlb and "4001│ line 4001" in nlb, nlb)

    # ---- _md_snapshot：.md 结构目录 + 干净正文（frontmatter/标题→行范围，跳过代码围栏 #）----
    md = ("---\nname: explore-codebase\ndescription: 摸清代码库\n---\n\n"
          "# 探索代码库 SOP\n\n1. list_dir\n2. 读 README\n\n"
          "## 进阶\n\n用 grep\n\n# 结束\n")
    snap = rt._md_snapshot(md)
    check("_md_snapshot 含 structure/content", "<structure>" in snap and "<content>" in snap, snap)
    check("_md_snapshot frontmatter 范围", "[L1-L4] frontmatter" in snap, snap)
    check("_md_snapshot 一级标题范围", "[L6-L14] 探索代码库 SOP" in snap, snap)
    check("_md_snapshot 嵌套二级缩进+范围", "  [L11-L14] 进阶" in snap, snap)
    check("_md_snapshot 末尾单行标题", "[L15] 结束" in snap, snap)
    check("_md_snapshot 正文干净无行号", "│" not in snap and "name: explore-codebase" in snap, snap)
    # 代码围栏里的 # 不当标题（看 structure 段，content 段仍含原文）
    md2 = "# Title\n\n```python\n# 注释不是标题\nx=1\n```\n\n## Real\n"
    struct2 = rt._md_snapshot(md2).split("</structure>")[0]
    check("_md_snapshot 跳过围栏内 #", "注释不是标题" not in struct2 and "[L1-L8] Title" in struct2, struct2)
    # read_file(.md) 默认 → 结构目录 + 干净正文 + file_version
    (TMP / "doc.md").write_text(md, encoding="utf-8")
    rmd = rt.read_file("doc.md")
    check("read_file(.md) 默认出结构目录", "<structure>" in rmd and "<content>" in rmd, rmd)
    check("read_file(.md) 正文无行号", "│" not in rmd, rmd)
    check("read_file(.md) 仍带 file_version", "file_version=" in rmd, rmd)
    # read_file(.md) line_numbers=False → 纯文本、无 structure
    rmd2 = rt.read_file("doc.md", line_numbers=False)
    check("read_file(.md) line_numbers=False 无结构", "<structure>" not in rmd2 and "name: explore-codebase" in rmd2, rmd2)
    # <recent-file> 对 .md 也走结构目录（最小假 step/session 调真实采集）
    import agent as _ag
    (TMP / "note.md").write_text("# Hi\nbody\n", encoding="utf-8")
    class _C:
        def __init__(s, cid): s.call_id = cid
    class _TL:
        def view(s, cid): return ("write_file", {"path": "note.md"}, "ok")
    class _Sess: toollog = _TL()
    class _Step: tool_calls = [_C("c1")]
    class _Dummy: pass
    _du = _Dummy(); _du.session = _Sess()
    _snaps = _ag.Agent._collect_file_snapshots(_du, _Step())
    check("recent-file(.md) 走结构目录", "<structure>" in _snaps["c1"]["text"] and "<content>" in _snaps["c1"]["text"], _snaps["c1"]["text"])

    # ---- wiki_list / wiki_tree：.md 文件附标题大纲（层级缩进 + ·L行号）----
    import wiki
    wiki.WORKSPACE = TMP
    wroot = TMP / ".agent" / "wiki"
    (wroot / "auth").mkdir(parents=True)
    (wroot / "auth" / "flow.md").write_text("# 认证流程\n\n## JWT 签发\n\n## Token 刷新\n", encoding="utf-8")
    (wroot / "auth" / "notes.txt").write_text("# 不是 md 大纲\n", encoding="utf-8")
    (wroot / "arch.md").write_text("# 架构\n\n```python\n# 围栏内不算\n```\n\n## 数据流\n", encoding="utf-8")
    wt = wiki.wiki_tree()
    check("wiki_tree 含文件路径", "auth/flow.md" in wt and "arch.md" in wt, wt)
    check("wiki_tree 附 md 标题大纲", "# 认证流程 ·L1" in wt and "## JWT 签发 ·L3" in wt and "## Token 刷新 ·L5" in wt, wt)
    check("wiki_tree 非 md 不附大纲", "# 不是 md 大纲" not in wt, wt)
    check("wiki_tree 跳过围栏内 #", "围栏内不算" not in wt and "## 数据流 ·L7" in wt, wt)
    wl = wiki.wiki_list("auth")
    check("wiki_list 单层+大纲", "flow.md" in wl and "# 认证流程 ·L1" in wl and "arch.md" not in wl, wl)

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
