# -*- coding: utf-8 -*-
"""dashboard 冒烟·启动器：隔离 AGT_HOME 起 2 个 agt-web 实例(18001/18002) + dashboard(19100)。

隔离策略：三进程各自独立 AGT_HOME（smoke/home_*），不碰生产 ~/.agt 的 session/注册表；
models.json 从 ~/.agt 复制（冒烟需真 LLM），main.yml 用极简装配 + glm-official-flash（廉价模型）。
进程 DETACHED 启动，PID 落 smoke/pids.json 供清理器 taskkill。
"""
import json
import os
import subprocess
import sys
from pathlib import Path

SMOKE = Path(__file__).resolve().parent
REPO = SMOKE.parent
SRC = REPO / "src"
PROD_HOME = Path.home() / ".agt"

DETACHED = 0x00000008 | 0x08000000   # DETACHED_PROCESS | CREATE_NO_WINDOW

NAMES = {"a": "smoke-alpha", "b": "smoke-beta", "dash": "smoke-dash"}
MODEL = "glm-official-flash"


def prepare(kind: str) -> Path:
    """建 ws_<kind> 工作区 + home_<kind> 隔离 AGT_HOME（models.json 复制 + 极简 main.yml）。"""
    (SMOKE / f"ws_{kind}").mkdir(parents=True, exist_ok=True)
    home = SMOKE / f"home_{kind}"
    home.mkdir(parents=True, exist_ok=True)
    (home / "models.json").write_bytes((PROD_HOME / "models.json").read_bytes())
    yml = (f"name: {NAMES[kind]}\n"
           f"description: dashboard 冒烟测试实例\n"
           f"model: {MODEL}\n"
           "assembly:\n"
           "- text: '你是冒烟测试实例。收到消息后用一两句话尽快完成，不使用 MCP 工具，不读写文件，"
           "不创建子 Agent。用户要求调用某个工具时直接调用。'\n")
    (home / "main.yml").write_text(yml, encoding="utf-8")
    return home


def launch(tag: str, code: str, home: Path, cwd: Path) -> int:
    env = os.environ.copy()
    env["AGT_HOME"] = str(home)
    env["PYTHONIOENCODING"] = "utf-8"
    out = open(SMOKE / f"{tag}.out.log", "ab")
    err = open(SMOKE / f"{tag}.err.log", "ab")
    p = subprocess.Popen([sys.executable, "-c", code], cwd=str(cwd), env=env,
                         stdin=subprocess.DEVNULL, stdout=out, stderr=err,
                         creationflags=DETACHED, close_fds=True)
    return p.pid


def main() -> None:
    # 隔离注册表/互动流：dashboard 的 home_dash 里 dashboard.json 从空白开始
    home_a = prepare("a")
    home_b = prepare("b")
    home_d = prepare("dash")

    web_tpl = ("import sys, webbrowser; sys.path.insert(0, r'{src}'); "
               "webbrowser.open = lambda *a, **k: None; "
               "from chat import web_main; web_main({port})").format(src=SRC, port="{port}")
    dash_code = ("import sys; sys.path.insert(0, r'{src}'); "
                 "from dashboard import serve; serve(19100, open_browser=False)"
                 ).format(src=SRC)

    pids = {
        "a": launch("inst_a", web_tpl.format(port=18001), home_a, SMOKE / "ws_a"),
        "b": launch("inst_b", web_tpl.format(port=18002), home_b, SMOKE / "ws_b"),
        "dash": launch("dash", dash_code, home_d, REPO),
    }
    (SMOKE / "pids.json").write_text(json.dumps(pids), encoding="utf-8")
    print(json.dumps(pids))


if __name__ == "__main__":
    main()
