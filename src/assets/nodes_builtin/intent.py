"""Intent 意图识别节点插件（type 22）：LLM 把 query 分到预设意图之一。

命中第 i 个 → 端口 branch_{i}，否则 default。意图选项自动并入提示词；
可声明 systemPrompt 与 model（节点级选模型）。
"""

PARAMS = [
    {"key": "query",   "type": "string", "required": True,
     "desc": "待分类文本；{{输入字段名}} 占位符"},
    {"key": "intents", "type": "list", "required": True,
     "desc": "意图名列表（画布每个意图一个 branch_N 出口，未命中走默认）"},
    {"key": "model",   "type": "string", "required": False, "default": "",
     "desc": "分类模型；空=跟随 ctx.llm"},
]

from workflow_node_api import resolve_value, resolve_input_params, get_llm


def _handle_intent(node: dict, ctx) -> dict:
    inputs = node.get("data", {}).get("inputs", {})
    params = resolve_input_params(inputs.get("inputParameters", []), ctx)
    intents = [i.get("name", "") for i in inputs.get("intents", [])]
    query = params.get("query") or next((v for v in params.values() if v), "")

    # 意图列表自动并入提示词（带编号，便于模型返回）
    list_str = "\n".join(f"{i+1}. {n}" for i, n in enumerate(intents) if n) or "(无意图)"
    prompt = (f"判断用户输入属于下列哪个意图，只回复对应编号（数字），不要任何解释。\n"
              f"可选意图：\n{list_str}\n\n用户输入：{query}\n\n"
              f"若无任何匹配，回复 0。")
    msgs = []
    sys_input = next((p for p in inputs.get("llmParam", []) if p.get("name") == "systemPrompt"), None)
    sys_text = resolve_value(sys_input.get("input"), ctx) if sys_input else ""
    if sys_text:
        msgs.append({"role": "system", "content": str(sys_text)})
    msgs.append({"role": "user", "content": prompt})
    # 支持节点级选模型
    model_name = str(resolve_value(
        next((p.get("input") for p in inputs.get("llmParam", []) if p.get("name") == "model"), {}),
        ctx) or "")
    resp = get_llm(ctx, model_name).chat(msgs)
    answer = (getattr(resp, "content", "") or "").strip()

    # 优先按编号解析，其次按意图名匹配
    idx = None
    digits = "".join(ch for ch in answer if ch.isdigit())
    if digits:
        n = int(digits)
        if 1 <= n <= len(intents):
            idx = n - 1
    if idx is None:
        for i, name in enumerate(intents):
            if name and (name == answer or name in answer):
                idx = i
                break
    if idx is None:
        return {"outputs": answer, "port": "default"}
    return {"outputs": answer, "port": f"branch_{idx}"}


def agt_node():
    return {"type": "22", "label": "Intent", "handler": _handle_intent, "catalog": _CATALOG}

# ===== 节点目录条目（list_workflow_nodes / query_workflow_node 动态聚合自插件声明）=====
_CATALOG = {"name": "意图识别 (Intent)", "desc": "用 LLM 对输入做意图分类，每个意图对应一个分支出口端口（branch_0/branch_1…），未匹配走 default", "xml": "<!-- 意图识别节点：LLM 分类 + 分支路由 -->\n<node id=\"160001\" type=\"intent\">\n  <!-- 输入：query 是要分类的文本 -->\n  <in name=\"query\" ref=\"130001.output\"/>\n\n  <!-- 意图列表：每个 intent 对应一个出口端口 -->\n  <intent name=\"提问\">用户想了解某个知识点或问\"是什么/为什么/怎么\"</intent>\n  <intent name=\"指令\">用户要求 AI 执行某个操作，如\"帮我写/帮我查/翻译\"</intent>\n  <intent name=\"闲聊\">用户只是聊天、打招呼、或表达情绪</intent>\n\n  <!-- LLM 参数（可选，不写则用默认） -->\n  <param name=\"systemPrompt\"><![CDATA[你是一个意图分类器。根据用户输入判断意图。]]></param>\n  <param name=\"temperature\" type=\"float\">0.1</param>\n\n  <!-- 输出 -->\n  <out name=\"classificationId\" type=\"string\"/>\n  <out name=\"reason\" type=\"string\"/>\n</node>\n<!--\n  出口端口：branch_0(第1个意图匹配), branch_1(第2个匹配), ... , default(都不匹配)\n  输出字段：classificationId(匹配到的意图名), reason(LLM 给出的分类理由)\n-->"}
