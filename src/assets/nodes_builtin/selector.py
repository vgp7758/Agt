"""Selector 选择器节点插件（type 8）：按分支顺序求值，第 i 个(0起)成立的分支 →
端口 'true'(i=0) / 'true_{i}'(i>0)；都不成立 → 'false'。
"""

PARAMS = [
    {"key": "branches", "type": "list", "required": True,
     "desc": "条件分支列表；每项 {condition:{logic, conditions:[{operator,left,right}]}}，"
             "按序求值第一个真值分支（branch_N），全假走 default"},
]

from workflow_node_api import eval_condition


def _handle_selector(node: dict, ctx) -> dict:
    branches = node.get("data", {}).get("inputs", {}).get("branches", [])
    for i, br in enumerate(branches):
        if eval_condition(br.get("condition", {}), ctx):
            return {"outputs": {}, "port": "true" if i == 0 else f"true_{i}"}
    return {"outputs": {}, "port": "false"}


def agt_node():
    return {"type": "8", "label": "Selector", "handler": _handle_selector, "catalog": _CATALOG}

# ===== 节点目录条目（list_workflow_nodes / query_workflow_node 动态聚合自插件声明）=====
_CATALOG = {"name": "选择器 (Selector)", "desc": "条件分支：根据配置的 conditions 判断走哪个出口端口（true/true_1…/false），支持 Equal/Contain/Greater/Empty 等运算符", "xml": "<!-- 选择器节点：条件分支 -->\n<node id=\"170001\" type=\"selector\">\n  <!-- 输入供条件左值引用 -->\n  <in name=\"score\" ref=\"150001.score\"/>\n\n  <!-- 分支条件组：branches 数组，按顺序匹配 -->\n  <branch>\n    <!-- conditions: [{operator, left, right}]（可多条件，logic=1=OR, 2=AND） -->\n    <condition operator=\"GreaterEqual\" logic=\"2\">\n      <!-- left 引用上游输出字段（ref=节点ID.字段名） -->\n      <left ref=\"150001.score\"/>\n      <!-- right 可以是 literal 或 ref -->\n      <right literal=\"90\">90</right>\n    </condition>\n    <!-- 出口端口：true（第1个分支匹配） -->\n  </branch>\n\n  <branch>\n    <condition operator=\"GreaterEqual\" logic=\"2\">\n      <left ref=\"150001.score\"/>\n      <right literal=\"60\">60</right>\n    </condition>\n    <!-- 出口端口：true_1（第2个分支匹配） -->\n  </branch>\n\n  <!-- 都不匹配走 false 端口 -->\n</node>\n<!--\n  支持运算符：Equal(=), NotEqual(!=), Contain(包含), NotContain, Empty, NotEmpty,\n            Greater(>), GreaterEqual(>=), Less(<), LessEqual(<=),\n            True, False, LengthGreater, LengthGreaterEqual, LengthLess, LengthLessEqual\n  出口端口：true(分支1匹配), true_1(分支2匹配), ..., false(全不匹配)\n  logic: 1=OR(任一满足), 2=AND(全部满足)\n-->"}
