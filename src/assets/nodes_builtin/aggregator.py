"""Aggregator 聚合节点插件（type 32）：多个分支汇合，取"实际执行到的那个"上游输出。

变量按声明顺序取第一个【执行过且值非空】的：
- block-output 分支未执行过 → 跳过（继续看后续变量）；
- 执行过但值为 None/空串 → 记兜底继续找后面的（var1=null 不再吞掉整组）；
- 全部执行过但都无值 → 兜底第一个执行过的值；
- 字面量/全局变量分支保持"非 None 即选"。
"""

PARAMS = [
    {"key": "mergeGroups", "type": "list", "required": True,
     "desc": "分组列表；每项 {name, variables:[{type, value(ref|literal)}]}——"
             "取第一个【执行过且值非空】的变量作为分组输出"},
]

from workflow_node_api import resolve_value


def _handle_aggregator(node: dict, ctx) -> dict:
    groups = node.get("data", {}).get("inputs", {}).get("mergeGroups", [])
    out = {}
    for g in groups:
        gname = g.get("name")
        chosen = None
        fallback = None
        has_fallback = False
        for bi in g.get("variables", []):
            val = bi.get("value", bi) if isinstance(bi, dict) else {}
            content = val.get("content") if isinstance(val, dict) else None
            if isinstance(content, dict) and content.get("source") == "block-output":
                if str(content.get("blockID", "")) not in ctx.node_outputs:
                    continue   # 该分支未执行过：跳过（继续看后续变量）
                v = resolve_value(bi, ctx)
                if v is not None and v != "":
                    chosen = v          # 执行过且有值：选定
                    break
                if not has_fallback:    # 执行过但无值：记兜底，继续找后面的
                    fallback, has_fallback = v, True
            else:
                v = resolve_value(bi, ctx)  # 字面量/全局变量：取非空者
                if v is not None:
                    chosen = v
                    break
        out[gname] = chosen if chosen is not None else fallback
    return {"outputs": out, "port": None}


def agt_node():
    return {"type": "32", "label": "Aggregator", "handler": _handle_aggregator, "catalog": _CATALOG}

# ===== 节点目录条目（list_workflow_nodes / query_workflow_node 动态聚合自插件声明）=====
_CATALOG = {"name": "聚合 (Aggregator)", "desc": "多分支汇合：将多个分支的输出汇总到一个节点，运行时只取实际执行到的那个分支的值", "xml": "<!-- 聚合节点：多分支汇合 -->\n<node id=\"180001\" type=\"aggregator\">\n  <!-- 每个 mergeGroup 收集一条分支的输出 -->\n  <group name=\"branch_0\">\n    <variable ref=\"160001.output\"/>   <!-- 意图分支0 的输出 -->\n  </group>\n  <group name=\"branch_1\">\n    <variable ref=\"160002.output\"/>   <!-- 意图分支1 的输出 -->\n  </group>\n  <group name=\"branch_default\">\n    <variable ref=\"160003.output\"/>   <!-- default 分支的输出 -->\n  </group>\n\n  <out name=\"branch_0\" type=\"string\"/>\n  <out name=\"branch_1\" type=\"string\"/>\n  <out name=\"branch_default\" type=\"string\"/>\n</node>\n<!--\n  用途：Selector/Intent 分支后汇合，下游节点统一引用 aggregator 的输出，避免空引用\n  运行时只填充实际走到的分支，其他分支字段为 null\n-->"}
