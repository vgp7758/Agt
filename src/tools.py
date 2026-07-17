"""tools.py —— 把普通 Python 函数变成模型可调用的"工具"。

核心：
  Tool     : 包装一个函数，从【类型注解 + docstring】自动生成 OpenAI function schema。
             写一个函数即得到一个工具，扩展到 Step 6（搜索/文件/代码执行）零成本。
  Toolbox  : 工具注册器，统一产出 API 需要的 tools 列表，并按名字派发执行。

设计原则（呼应前面的教训）：
  - 工具执行出错时不抛异常炸掉流程，而是把错误【如实以文本回传】给模型，让它有机会修正。
"""
from __future__ import annotations

import inspect
from typing import Callable, get_origin, get_type_hints

# Python 类型 → JSON Schema 类型
_PY_TO_JSON_SCHEMA = {
    str: "string",
    int: "integer",
    float: "number",
    bool: "boolean",
}


def _type_to_schema(ptype):
    """Python 类型 → JSON Schema 片段。支持 str/int/float/bool/list/dict。"""
    if ptype in _PY_TO_JSON_SCHEMA:
        return {"type": _PY_TO_JSON_SCHEMA[ptype]}
    if get_origin(ptype) is list or ptype is list:
        return {"type": "array", "items": {}}
    if get_origin(ptype) is dict or ptype is dict:
        return {"type": "object"}
    return None


class Tool:
    def __init__(self, func: Callable):
        self.func = func
        self.name = func.__name__
        # docstring 第一行作为工具描述（模型靠它判断"该不该调这个工具"）
        first_line = (func.__doc__ or "").strip().split("\n", 1)[0].strip()
        if not first_line:
            raise ValueError(f"工具 {self.name} 必须有 docstring 作为描述")
        self.description = first_line

        self._hints = get_type_hints(func)
        self._sig = inspect.signature(func)
        self.schema = self._build_schema()

    def _build_schema(self) -> dict:
        """生成 OpenAI function-calling 的 schema。支持 str/int/float/bool/list/dict。"""
        properties, required = {}, []
        for pname, param in self._sig.parameters.items():
            ptype = self._hints.get(pname, str)
            sch = _type_to_schema(ptype)
            if sch is None:
                raise TypeError(f"工具 {self.name} 参数 {pname} 类型 {ptype} 暂不支持")
            properties[pname] = sch
            # 有默认值的参数不算必填
            if param.default is inspect.Parameter.empty:
                required.append(pname)
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": properties,
                    "required": required,
                },
            },
        }

    def run(self, **kwargs) -> str:
        """执行工具，返回字符串结果。出错也返回错误文本，不抛异常。"""
        try:
            result = self.func(**kwargs)
        except Exception as e:
            result = f"[工具执行出错] {type(e).__name__}: {e}"
        return str(result)

    def __repr__(self):
        return f"Tool({self.name})"


class Toolbox:
    def __init__(self, *tools: Tool):
        self._tools: dict[str, Tool] = {}
        for t in tools:
            self.register(t)

    def register(self, tool: Tool) -> "Toolbox":
        if tool.name in self._tools:
            raise ValueError(f"工具 {tool.name} 已注册")
        self._tools[tool.name] = tool
        return self

    def register_or_replace(self, tool: Tool) -> "Toolbox":
        """注册工具，同名则覆盖（用于工作流每轮刷新：删了再加等价，但更省事）。"""
        self._tools[tool.name] = tool
        return self

    def drop(self, prefix: str) -> int:
        """删除所有名字以 prefix 开头的工具，返回删除数（工作流刷新前清掉旧的 wf_*）。"""
        gone = [k for k in self._tools if k.startswith(prefix)]
        for k in gone:
            del self._tools[k]
        return len(gone)

    def unregister(self, name: str) -> bool:
        """按名字删一个工具，返回是否删除（用于每轮清理上次注册的用户工具）。"""
        if name in self._tools:
            del self._tools[name]
            return True
        return False

    def schemas(self) -> list[dict]:
        """产出传给 API 的 tools 列表。"""
        return [t.schema for t in self._tools.values()]

    def call(self, name: str, arguments: dict) -> str:
        """按名字派发执行。未知工具也返回文本提示，不抛异常。"""
        tool = self._tools.get(name)
        if tool is None:
            return f"[未知工具] 模型想调用 '{name}'，但工具箱里没有"
        return tool.run(**arguments)

    def __iter__(self):  # 方便遍历：for t in toolbox
        return iter(self._tools.values())

    def __contains__(self, name):
        return name in self._tools

    def __repr__(self):
        return f"Toolbox({list(self._tools)})"


# === 玩具工具（演示机制用；真实强力工具在 Step 6） ===
def add(a: float, b: float) -> float:
    """两个数相加，返回它们的和。"""
    return a + b


def subtract(a: float, b: float) -> float:
    """用 a 减去 b，返回差值。"""
    return a - b


def multiply(a: float, b: float) -> float:
    """两个数相乘，返回乘积。"""
    return a * b


def divide(a: float, b: float) -> float:
    """用 a 除以 b，返回商。除数为 0 会报错。"""
    return a / b


# 开箱即用的默认工具箱
DEFAULT_TOOLS = Toolbox(Tool(add), Tool(subtract), Tool(multiply), Tool(divide))
