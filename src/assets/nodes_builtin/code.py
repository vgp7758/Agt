"""Code 节点插件（type 5）：沙箱执行 Python（async def main(args)->Output）。

独立子进程跑（隔离 + 30s 超时），args.params 取 inputParameters；
return 的 dict 作为节点输出。
"""
import json

from workflow_node_api import resolve_input_params, workflow_error


def _handle_code(node: dict, ctx) -> dict:
    import os
    import subprocess
    import sys
    import tempfile

    inputs = node.get("data", {}).get("inputs", {})
    language = inputs.get("language", 3)
    if language != 3:
        raise workflow_error(f"代码节点仅支持 Python3(language=3)，收到 language={language}")
    params = resolve_input_params(inputs.get("inputParameters", []), ctx)
    code = inputs.get("code", "") or ""

    runner = (
        "import json, os, asyncio, inspect\n"
        "class _Args:\n"
        "    def __init__(self, p): self.params = p\n"
        "Output = dict\n"
        "args = _Args(json.loads(os.environ['WF_PARAMS']))\n"
        + code +
        "\n_r = None\n"
        "if 'main' in dir():\n"
        "    _m = main\n"
        "    if inspect.iscoroutinefunction(_m):\n"
        "        _r = asyncio.get_event_loop().run_until_complete(_m(args))\n"
        "    else:\n"
        "        _r = _m(args)\n"
        "print('__WF_RESULT__', json.dumps(_r, ensure_ascii=False, default=str))\n"
    )
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False, encoding="utf-8") as f:
        f.write(runner)
        tmp = f.name
    env = dict(os.environ)
    env["WF_PARAMS"] = json.dumps(params, ensure_ascii=False, default=str)
    try:
        proc = subprocess.run([sys.executable, tmp], capture_output=True, text=True,
                              timeout=30, encoding="utf-8", errors="replace", env=env,
                              creationflags=(subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0))
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass

    marker = None
    for line in (proc.stdout or "").splitlines():
        if line.startswith("__WF_RESULT__ "):
            marker = line[len("__WF_RESULT__ "):]
    if marker is None:
        tail = (proc.stderr or proc.stdout or "")[-500:]
        raise workflow_error(f"代码节点未产出结果（可能抛错）：{tail}")
    try:
        ret = json.loads(marker)
    except json.JSONDecodeError:
        ret = {"output": marker}
    if not isinstance(ret, dict):
        ret = {"output": ret}
    return {"outputs": ret, "port": None}


def agt_node():
    return {"type": "5", "label": "Code", "handler": _handle_code}
