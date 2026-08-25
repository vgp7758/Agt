"""Timestamp 节点插件（type N1）：输出当前时间字符串（YYYY-MM-DD HH:MM:SS）。

节点插件化的扩展面验收节点：无输入、单输出、零框架依赖——
放两个文件（本 .py + 同名 .js）即得全新节点类型，/reload nodes 热生效。
"""
from datetime import datetime


def _timestamp(node: dict, ctx) -> dict:
    return {"outputs": {"output": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}, "port": None}


def agt_node():
    return {"type": "N1", "label": "Timestamp", "handler": _timestamp, "catalog": _CATALOG}

# ===== 节点目录条目（动态聚合；用户级插件的目录声明同样生效）=====
_CATALOG = {"name": "当前时间戳", "desc": "输出当前时间字符串（用户级插件示例：.agent/nodes/ 放两个文件即得全新节点类型，零框架改动）", "xml": "<!-- timestamp：输出当前时间 -->\n<node id=\"170001\" type=\"N1\" title=\"当前时间\">\n  <in name=\"fmt\" type=\"string\">%Y-%m-%d %H:%M:%S</in>\n  <out name=\"output\" type=\"string\"/>\n</node>"}
