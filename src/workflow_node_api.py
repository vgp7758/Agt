"""workflow_node_api.py —— 节点插件 SDK（稳定依赖面）。

节点插件（.py）的 handler 只应 import 本模块——框架内部函数改名不波及插件。
当前为瘦 re-export：真实实现在 workflow.py（加载顺序：插件扫描发生在 workflow 导入后，
故此处用函数级延迟导入避免循环）。

handler 协议：
    async def handler(node: dict, ctx) -> dict
    # node：画布节点（data.inputs.inputParameters 等）
    # ctx：运行时上下文（node_outputs/batch_item/batch_index/loop_vars/emit/llm/tools）
    # 返回：{"outputs": {字段名: 值, ...}, "port": 端口名或 None}
    #   port 语义：selector/意图等分支节点返回分支端口（"true"/"false"/"branch_0"/...）；
    #   普通节点恒 None；error 边由引擎统一处理（勿在 handler 里抛 WorkflowError 以外约定）。

ctx 协议（插件可见字段）：
    ctx.node_outputs: dict   # 已执行节点的输出（ref 解析源）
    ctx.batch_item            # 节点级批处理当前元素（loop-item 引用）
    ctx.batch_index: int      # 当前元素下标
    ctx.loop_vars: dict       # 复合节点循环变量（LoopSetVariable 写）
    ctx.emit(ev: dict)        # 事件上报（workflow_message 等，观测页可见）
    ctx.llm                   # LLMClient（钩子注入的 utility 通道；独立测试可能为 None）
    ctx.tools                 # Toolbox（plugin 节点 call_tool 用；可能为 None）
"""
from __future__ import annotations


def resolve_value(inp, ctx):
    """输入绑定 → 值。literal 直接返回；ref（block-output/loop-item/loop-index/global）按 ctx 解析。"""
    from workflow import resolve_value as _f
    return _f(inp, ctx)


def dotted_get(obj, name: str):
    """按点号取子字段（'a.b.0.c'；list 下标 + .length/.is_empty 衍生）。"""
    from workflow import _dotted_get as _f
    return _f(obj, name)


def try_parse(v):
    """JSON 字符串 → 解析值；解析失败/非字符串原样返回（plugin 工具输出消费常用）。"""
    from workflow import _try_parse as _f
    return _f(v)


def resolve_input_params(params: list, ctx) -> dict:
    """inputParameters 列表 → {name: resolve_value(...)}（缺省值/字面量/ref 全兼容）。"""
    from workflow import _resolve_input_params as _f
    return _f(params, ctx)


def render_template(tmpl: str, params: dict) -> str:
    """{{name}} 占位符模板渲染（text 节点 concat / llm prompt 同款）。"""
    from workflow import render_template as _f
    return _f(tmpl, params)


def workflow_error(msg: str):
    """构造 WorkflowError（引擎会走 error 边/记录节点错误）。"""
    from workflow import WorkflowError
    return WorkflowError(msg)
