"""verify_parallel_filelock.py —— 验证 _run_tools_parallel 的"同文件串行、跨文件并行"。

构造会 read-modify-write（带 sleep 放大竞态窗口）的伪 edit，同文件两次 + 异文件一次：
- 同文件两次必须都落地、且保序（证明串行，未丢更新）；
- 异文件也写入（证明未误伤）；
- 结果按原顺序返回；非文件工具不崩溃。
"""
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

import agent as agent_mod  # noqa: E402
from tools import Toolbox as _Toolbox  # noqa: E402

TMP = Path(__file__).resolve().parent / "_verify_par"


class _Resp:
    def __init__(self):
        self.content = "ok"; self.tool_calls = []; self.reasoning = ""
        self.usage = {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}


class _FakeLLM:
    def __init__(self, *a, **k):
        self.model_name = "fake"; self.call_recorder = None; self.switch_model = lambda n: None
    def chat(self, msgs, tools=None):
        return _Resp()


def _appender(args):
    """racy read-modify-write：sleep 放大窗口，并行下必丢更新。"""
    p = TMP / args["path"]
    cur = p.read_text(encoding="utf-8") if p.exists() else ""
    time.sleep(0.1)
    p.write_text(cur + args["marker"] + "\n", encoding="utf-8")
    return "ok"


class _FakeTools:
    def call(self, name, args):
        if name == "edit":
            return _appender(args)
        return f"[unknown] {name}"


def main():
    if TMP.exists():
        shutil.rmtree(TMP)
    TMP.mkdir()
    passed, failed = [], []

    def check(n, c, e=""):
        (passed if c else failed).append(n)
        print(("  ✅ " if c else "  ❌ ") + n + (f"  ({e})" if e and not c else ""))

    import real_tools as rt
    rt.WORKSPACE = TMP
    orig = agent_mod.LLMClient
    agent_mod.LLMClient = _FakeLLM
    try:
        ag = agent_mod.Agent(system="sys", tools=_Toolbox(), verbose=False)
        ag.tools = _FakeTools()
        calls = [
            {"name": "edit", "arguments": {"path": "a.txt", "marker": "X"}},
            {"name": "edit", "arguments": {"path": "a.txt", "marker": "Y"}},  # 同文件 → 串行
            {"name": "edit", "arguments": {"path": "b.txt", "marker": "Z"}},  # 异文件 → 并行
        ]
        res = ag._run_tools_parallel(calls)
        a = (TMP / "a.txt").read_text(encoding="utf-8")
        b = (TMP / "b.txt").read_text(encoding="utf-8")
        check("同文件两次 edit 不丢更新", "X" in a and "Y" in a, repr(a))
        check("同文件串行保序(X在Y前)", a.index("X") < a.index("Y"), repr(a))
        check("结果按原顺序返回", len(res) == 3 and all(r == "ok" for r in res), str(res))
        check("异文件仍写入", "Z" in b, repr(b))
        # 非文件工具（web_search 不在 _FILE_TOOLS）不崩溃、并行无锁
        res2 = ag._run_tools_parallel([
            {"name": "web_search", "arguments": {"q": "x"}},
            {"name": "web_search", "arguments": {"q": "y"}},
        ])
        check("非文件工具不崩溃", len(res2) == 2 and all("unknown" in r for r in res2), str(res2))
    except Exception as e:
        check(f"环境异常 {type(e).__name__}", False, str(e)[:140])
    finally:
        agent_mod.LLMClient = orig
        shutil.rmtree(TMP, ignore_errors=True)

    print(f"\n{'='*40}\n通过 {len(passed)} / 失败 {len(failed)}")
    if failed:
        print("失败：", failed)
        sys.exit(1)
    print("全部通过 ✅")


if __name__ == "__main__":
    main()
