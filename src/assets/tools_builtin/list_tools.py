"""list_tools.py —— 列表类脚本工具（原 LIGHT_TOOLS 纯函数型外置件）。

agt_register() 返回描述符列表（script_tools.py 扫描注册约定）。
改完本文件用 /reload tools 热加载（不需要重启）。
"""
from typing import Any


def list_append(lst: list = None, item=None) -> list:
    """把 item 追加到 lst 末尾并返回新列表（不修改原列表）。lst 省略/None → [item]。
    工作流里循环累积结果用：var = list_append(var, 本轮值)（配合 LoopSetVariable 累加）；
    也可首节点不连 lst（None 容错）作为数组的冷启动。"""
    return list(lst or []) + [item]


def get_list_item(lst: list = None, index: int = 0):
    """取列表第 index 个元素（0-based；负数从尾部数：-1=末元素）。
    工作流里从 all_outputs / 上游 list 输出中取单个元素用（比 selector 运算符更直接）。"""
    l = lst or []
    try:
        return l[int(index)]
    except (IndexError, TypeError, ValueError):
        return f"[越界] index={index}，列表长度 {len(l)}"


def get_list_items(lst: list = None, indices: list = None) -> list:
    """按 indices 批量取 lst 中的元素，返回新列表。"""
    l = lst or []
    if indices is None:
        return []

    result = []
    for i in indices:
        try:
            result.append(l[i])
        except (IndexError, TypeError, ValueError):
            result.append(f"[越界] index={i}，列表长度 {len(l)}")
    return result


def pass_through(input: Any) -> Any:
    """透传/组装：input 原样返回（任意类型：字符串/数字/对象/数组）。
    配合编辑器把 input 类型改成 object 后逐子字段连线（object_ref 组装），
    可把多个上游节点的输出在节点处拼成结构透传输出——中转/整形/出口整形用。"""
    return input


def agt_register():
    return [
        {"name": "list_append", "func": list_append, "hidden": True, "group": "light", "version": 1},
        {"name": "get_list_item", "func": get_list_item,
         "outputs": [{"name": "raw", "type": "any", "description": "列表元素（类型随元素；越界返回错误文本）"}],
         "hidden": True, "group": "light", "version": 1},
        {"name": "get_list_items", "func": get_list_items,
         "outputs": [{"name": "raw", "type": "list", "description": "按 indices 顺序提取的元素列表（越界位置为错误文本）"}],
         "hidden": True, "group": "light", "version": 1},
        {"name": "pass_through", "func": pass_through,
         "outputs": [{"name": "raw", "type": "any", "description": "透传值（结构与输入一致；类型可在编辑器改）"}],
         "hidden": True, "group": "light", "version": 1},
    ]