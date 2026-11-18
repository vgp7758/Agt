"""Code 节点插件（type 5）：沙箱执行 Python（async def main(args)->Output）。

独立子进程跑（隔离 + 30s 超时），args.params 取 inputParameters；
return 的 dict 作为节点输出。
"""

PARAMS = [
    {"key": "code", "type": "string", "required": True,
     "desc": "沙箱 Python 代码；须定义 `async def main(args)`，args.params 取输入，返回 dict"},
]

import json

from workflow_node_api import resolve_input_params, workflow_error


def _handle_code(node: dict, ctx) -> dict:
    import os
    import subprocess
    import sys
    import tempfile
    import time

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
        "        _r = asyncio.run(_m(args))\n"
        "    else:\n"
        "        _r = _m(args)\n"
        "print('__WF_RESULT__', json.dumps(_r, ensure_ascii=False, default=str))\n"
    )
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False, encoding="utf-8") as f:
        f.write(runner)
        tmp = f.name
    env = dict(os.environ)
    env["WF_PARAMS"] = json.dumps(params, ensure_ascii=False, default=str)
    # 超时 45s + 超时重试 1 次（2026-11-18 用户报障：8000 等实例 code 节点间歇性 30s 卡满
    # ——纯组装代码也超时；闲时同进程实测 0.08s。根因是环境级尖峰（Defender 扫新 tmp 脚本
    # /IO 高峰下 python 冷启动挂起），单次重试即可自愈绝大多数尖峰，重试再超时才报错）
    proc = None
    try:
        for _attempt in (1, 2):
            try:
                proc = subprocess.run([sys.executable, tmp], capture_output=True, text=True,
                                      timeout=45, encoding="utf-8", errors="replace", env=env,
                                      creationflags=(subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0))
                break
            except subprocess.TimeoutExpired:
                if _attempt == 2:
                    raise
                time.sleep(0.5)
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
    return {"type": "5", "label": "Code", "handler": _handle_code, "catalog": _CATALOG}

# ===== 节点目录条目（list_workflow_nodes / query_workflow_node 动态聚合自插件声明）=====
_CATALOG = {"name": "代码 (Code)", "desc": "在沙箱中执行 Python 3 代码（async def main(args) -> Output），通过 args.params 取输入，return dict 作为输出", "xml": "<!-- 代码节点：Python3 沙箱执行 -->\n<node id=\"150001\" type=\"code\">\n  <!-- 模板入参：在 code 中用 {{变量名}} 引用；也可在 main() 内通过 args.params 访问 -->\n  <in name=\"x\" ref=\"140001.result\"/>\n  <in name=\"y\" ref=\"140002.result\"/>\n\n  <!-- Python3 代码（language=3）。约定：async def main(args) -> Output，return 的 dict 字段对应 out -->\n  <param name=\"code\" language=\"python3\"><![CDATA[\nimport json\n\nasync def main(args) -> dict:\n    x = float(args.params.get(\"x\", 0))\n    y = float(args.params.get(\"y\", 0))\n    result = {\n        \"sum\": x + y,\n        \"product\": x * y,\n        \"ratio\": x / y if y != 0 else None,\n    }\n    return result\n]]></param>\n\n  <!-- 输出字段：必须与 main() 返回 dict 的 key 一致 -->\n  <out name=\"sum\" type=\"number\"/>\n  <out name=\"product\" type=\"number\"/>\n  <out name=\"ratio\" type=\"number\"/>\n</node>\n<!--\n  参数类型映射：string→str, integer→int, number→float, boolean→bool, list→list, object→dict\n  args.params 是所有 in 的 dict；args.inputs 是原始 Coze InputParam 列表\n-->"}
