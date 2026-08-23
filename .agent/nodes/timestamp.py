"""Timestamp 节点插件（type N1）：输出当前时间字符串（YYYY-MM-DD HH:MM:SS）。

节点插件化的扩展面验收节点：无输入、单输出、零框架依赖——
放两个文件（本 .py + 同名 .js）即得全新节点类型，/reload nodes 热生效。
"""
from datetime import datetime


def _timestamp(node: dict, ctx) -> dict:
    return {"outputs": {"output": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}, "port": None}


def agt_node():
    return {"type": "N1", "label": "Timestamp", "handler": _timestamp}
